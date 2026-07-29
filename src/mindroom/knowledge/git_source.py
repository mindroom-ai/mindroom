"""Git-backed source synchronization for one knowledge base.

The knowledge manager owns indexing; this module owns the checkout indexing
reads from. Everything that shells out to ``git`` -- cloning, fetching,
force-aligning, Git LFS hydration and credential injection -- lives here so the
manager never has to know how the source folder is kept current.

Credentials reach ``git`` only through process-local ``GIT_CONFIG_*``
environment variables, never through the checkout's own config, and every error
path that can carry a URL or a provider message is redacted before it is raised
or logged.
"""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse, urlunparse

from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.file_listing import (
    git_checkout_present,
    git_tracked_relative_paths_from_checkout,
    include_knowledge_relative_path,
)
from mindroom.knowledge.redaction import (
    credential_free_repo_url,
    embedded_http_userinfo,
    redact_credentials_in_text,
    redact_url_credentials,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

__all__ = ["GitKnowledgeSource", "GitSyncResult"]


def _http_credentials(
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> tuple[str, str] | None:
    """Return HTTP basic-auth userinfo for one credentials service, if any.

    A bare token is the common case, so a service that stores one without a
    username authenticates as ``x-access-token``. An explicit password wins over
    a token, because a service configured with both is describing a real account
    rather than a token identity.
    """
    if not credentials_service:
        return None

    credentials = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(credentials_service) or {}
    username = credentials.get("username")
    token = credentials.get("token") or credentials.get("api_key")
    password = credentials.get("password")

    if not isinstance(username, str) and token and not password:
        username = "x-access-token"

    if not isinstance(username, str) or not username:
        return None

    if isinstance(password, str) and password:
        return username, password
    if isinstance(token, str) and token:
        return username, token
    return None


def _authenticated_repo_url(
    repo_url: str,
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Inject HTTPS credentials from CredentialsManager into a repository URL."""
    userinfo = _http_credentials(credentials_service, runtime_paths)
    if userinfo is None:
        return repo_url

    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https"}:
        return repo_url

    username, secret = userinfo
    hostname = parsed.netloc.split("@")[-1]
    auth_netloc = f"{quote(username, safe='')}:{quote(secret, safe='')}@{hostname}"
    return urlunparse(parsed._replace(netloc=auth_netloc))


def _git_http_basic_auth_env(clean_url: str, username: str, secret: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{clean_url}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
    }


def _git_auth_env(
    repo_url: str,
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> dict[str, str] | None:
    """Return process-local Git config that injects credentials without persisting them."""
    clean_url = credential_free_repo_url(repo_url)
    parsed_clean_url = urlparse(clean_url)

    embedded_userinfo = embedded_http_userinfo(repo_url)
    if embedded_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *embedded_userinfo)

    credentials_userinfo = (
        _http_credentials(credentials_service, runtime_paths) if parsed_clean_url.scheme in {"http", "https"} else None
    )
    if credentials_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *credentials_userinfo)

    authenticated_url = (
        repo_url if clean_url != repo_url else _authenticated_repo_url(clean_url, credentials_service, runtime_paths)
    )
    if authenticated_url == clean_url:
        return None
    parsed_authenticated_url = urlparse(authenticated_url)
    if parsed_authenticated_url.netloc and "@" in parsed_authenticated_url.netloc:
        return None
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{authenticated_url}.insteadOf",
        "GIT_CONFIG_VALUE_0": clean_url,
    }


def _merge_git_env(*envs: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for env in envs:
        if env:
            merged.update(env)
    return merged or None


@dataclass(frozen=True)
class GitSyncResult:
    """Outcome of one Git source synchronization.

    Deliberately only what callers act on. The changed and removed path sets a
    sync computes are reported in its log line and then dropped: no caller reads
    them, and carrying them would copy every tracked path in the repository on
    the initial-clone branch, where "changed" is the whole corpus.
    """

    #: Revision the checkout sits at afterwards, or None when it cannot be read.
    head: str | None
    #: Whether this sync moved the checkout, the initial clone included.
    updated: bool


@dataclass
class GitKnowledgeSource:
    """Keep one knowledge base's Git checkout aligned with its configured remote."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    #: Resolved knowledge folder, which is the repository worktree root itself.
    source_path: Path
    #: File recording the revision whose LFS objects are already hydrated, so a
    #: restart does not re-pull every object for an unchanged checkout.
    lfs_hydrated_head_path: Path
    _sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _last_synced_head: str | None = field(default=None, init=False)
    _lfs_checked: bool = field(default=False, init=False)
    _lfs_repository_ready: bool = field(default=False, init=False)
    _tracked_relative_paths: set[str] | None = field(default=None, init=False, repr=False)

    def is_configured(self) -> bool:
        """Return whether this knowledge base is backed by a Git repository."""
        return self._git_config() is not None

    @property
    def last_synced_head(self) -> str | None:
        """Return the revision this process last synchronized, or None."""
        return self._last_synced_head

    def cached_tracked_relative_paths(self) -> set[str] | None:
        """Return tracked paths already listed in this process, without listing any.

        None means "not known here", which is exactly what the corpus-signature
        helper needs in order to decide for itself whether to read the checkout.
        """
        return self._tracked_relative_paths

    def tracked_relative_paths(self) -> set[str] | None:
        """Return the tracked paths this base manages, listing the checkout once.

        None means there is no checkout yet, so the base manages no files. This
        blocks on ``git``; call it from a worker thread on hot paths.
        """
        if self._tracked_relative_paths is None:
            if not git_checkout_present(self.source_path, timeout_seconds=self._sync_timeout_seconds()):
                return None
            self._tracked_relative_paths = git_tracked_relative_paths_from_checkout(
                self.config,
                self.base_id,
                self.source_path,
            )
        return self._tracked_relative_paths

    async def head(self) -> str | None:
        """Return the checkout's current revision, or None when it cannot be read."""
        return await self._rev_parse("HEAD")

    async def sync(self) -> GitSyncResult:
        """Fetch and force-align one configured Git repository checkout."""
        git_config = self._git_config()
        if git_config is None:
            return GitSyncResult(head=None, updated=False)

        async with self._sync_lock:
            changed_files, removed_files, updated = await self._sync_once(git_config)
            current_head = await self._rev_parse("HEAD")
            self._last_synced_head = current_head

        if updated:
            logger.info(
                "Knowledge Git repository synchronized",
                base_id=self.base_id,
                repo_url=redact_url_credentials(git_config.repo_url),
                branch=git_config.branch,
                changed_count=len(changed_files),
                removed_count=len(removed_files),
                commit=current_head,
            )
        return GitSyncResult(head=current_head, updated=updated)

    def _git_config(self) -> KnowledgeGitConfig | None:
        return self.config.get_knowledge_base_config(self.base_id).git

    def _uses_lfs(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.lfs)

    def _sync_timeout_seconds(self) -> float | None:
        git_config = self._git_config()
        if git_config is None:
            return None
        return float(git_config.sync_timeout_seconds)

    def _include_relative_path(self, relative_path: str) -> bool:
        return include_knowledge_relative_path(self.config, self.base_id, relative_path)

    def _load_lfs_hydrated_head(self) -> str | None:
        try:
            hydrated_head = self.lfs_hydrated_head_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return hydrated_head or None

    def _save_lfs_hydrated_head(self, head: str) -> None:
        self.lfs_hydrated_head_path.write_text(head, encoding="utf-8")

    def _clear_lfs_hydrated_head(self) -> None:
        self.lfs_hydrated_head_path.unlink(missing_ok=True)

    async def _checkout_present(self) -> bool:
        return await asyncio.to_thread(
            git_checkout_present,
            self.source_path,
            timeout_seconds=self._sync_timeout_seconds(),
        )

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        repo_root = cwd or self.source_path
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo_root),
            env=None if env is None else {**os.environ, **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            timeout_seconds = self._sync_timeout_seconds()
            if timeout_seconds is None:
                stdout, stderr = await process.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            raise
        except TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            command = " ".join(["git", *(redact_url_credentials(arg) for arg in args)])
            msg = f"Git command timed out after {timeout_seconds:.0f}s: {command}"
            raise RuntimeError(msg) from exc

        if process.returncode == 0:
            return stdout.decode("utf-8", errors="replace")

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        details = redact_credentials_in_text(stderr_text or stdout_text)
        command = " ".join(["git", *(redact_url_credentials(arg) for arg in args)])
        msg = f"Git command failed with exit code {process.returncode}: {command}"
        if details:
            msg = f"{msg}\n{details}"
        raise RuntimeError(msg)

    async def _ensure_lfs_available(self, *, cwd: Path) -> None:
        if not self._uses_lfs() or self._lfs_checked:
            return
        try:
            await self._run_git(["lfs", "version"], cwd=cwd)
        except RuntimeError as exc:
            msg = "Git LFS is required for this knowledge base but is not available in the runtime image"
            raise RuntimeError(msg) from exc
        self._lfs_checked = True

    async def _ensure_lfs_repository_ready(self, repo_root: Path) -> None:
        if not self._uses_lfs() or self._lfs_repository_ready:
            return
        await self._ensure_lfs_available(cwd=repo_root)
        await self._run_git(["lfs", "install", "--local"], cwd=repo_root)
        self._lfs_repository_ready = True

    def _lfs_skip_smudge_env(self, git_config: KnowledgeGitConfig) -> dict[str, str] | None:
        if not git_config.lfs:
            return None
        return {"GIT_LFS_SKIP_SMUDGE": "1"}

    def _lfs_pull_args(self, git_config: KnowledgeGitConfig) -> list[str]:
        return ["lfs", "pull", "origin", git_config.branch]

    async def _hydrate_lfs_worktree(
        self,
        git_config: KnowledgeGitConfig,
        *,
        repo_root: Path | None = None,
        current_head: str | None = None,
    ) -> None:
        if not git_config.lfs:
            return
        resolved_head = current_head or await self._rev_parse("HEAD")
        if resolved_head is not None:
            hydrated_head = await asyncio.to_thread(self._load_lfs_hydrated_head)
            if hydrated_head == resolved_head:
                return
        await self._run_git(
            self._lfs_pull_args(git_config),
            cwd=repo_root or self.source_path,
            env=_git_auth_env(git_config.repo_url, git_config.credentials_service, self.runtime_paths),
        )
        if resolved_head is None:
            resolved_head = await self._rev_parse("HEAD")
        if resolved_head is not None:
            await asyncio.to_thread(self._save_lfs_hydrated_head, resolved_head)

    async def _rev_parse(self, ref: str) -> str | None:
        try:
            output = await self._run_git(["rev-parse", ref])
        except RuntimeError:
            return None
        return output.strip() or None

    async def _list_tracked_files(self) -> set[str]:
        output = await self._run_git(["ls-files", "-z"])
        raw_paths = [entry for entry in output.split("\x00") if entry]
        tracked_files = {path for path in raw_paths if self._include_relative_path(path)}
        self._tracked_relative_paths = set(tracked_files)
        return tracked_files

    async def _ensure_repository(self, git_config: KnowledgeGitConfig) -> bool:
        runtime_paths = self.runtime_paths
        knowledge_root = self.source_path
        if await self._checkout_present():
            await self._ensure_lfs_repository_ready(knowledge_root)
            current_remote = (await self._run_git(["remote", "get-url", "origin"])).strip()
            expected_remote = credential_free_repo_url(git_config.repo_url)
            if current_remote != expected_remote:
                await self._run_git(["remote", "set-url", "origin", expected_remote])
            return False

        if knowledge_root.exists() and any(knowledge_root.iterdir()):
            msg = (
                f"Cannot clone knowledge git repository into non-empty path {knowledge_root}. "
                "Clear the folder or use a dedicated path."
            )
            raise RuntimeError(msg)

        knowledge_root.parent.mkdir(parents=True, exist_ok=True)
        if git_config.lfs:
            await self._ensure_lfs_available(cwd=knowledge_root.parent)
        clone_url = credential_free_repo_url(git_config.repo_url)
        await self._run_git(
            [
                "clone",
                "--single-branch",
                "--branch",
                git_config.branch,
                clone_url,
                str(knowledge_root),
            ],
            cwd=knowledge_root.parent,
            env=_merge_git_env(
                _git_auth_env(git_config.repo_url, git_config.credentials_service, runtime_paths),
                self._lfs_skip_smudge_env(git_config),
            ),
        )
        await self._run_git(["remote", "set-url", "origin", clone_url], cwd=knowledge_root)
        await asyncio.to_thread(self._clear_lfs_hydrated_head)
        await self._ensure_lfs_repository_ready(knowledge_root)
        await self._hydrate_lfs_worktree(git_config, repo_root=knowledge_root)
        return True

    async def _sync_once(self, git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        cloned = await self._ensure_repository(git_config)
        if cloned:
            return await self._list_tracked_files(), set(), True

        before_head = await self._rev_parse("HEAD")

        remote_ref = f"origin/{git_config.branch}"
        await self._run_git(
            ["fetch", "origin", f"+refs/heads/{git_config.branch}:refs/remotes/{remote_ref}"],
            env=_git_auth_env(git_config.repo_url, git_config.credentials_service, self.runtime_paths),
        )
        remote_head = await self._rev_parse(remote_ref)
        if remote_head is None:
            msg = f"Could not resolve remote ref '{remote_ref}' for knowledge base '{self.base_id}'"
            raise RuntimeError(msg)

        if before_head == remote_head:
            await self._hydrate_lfs_worktree(git_config, current_head=remote_head)
            return set(), set(), False

        before_files = await self._list_tracked_files()

        await self._run_git(
            ["checkout", "--force", "-B", git_config.branch, remote_ref],
            env=self._lfs_skip_smudge_env(git_config),
        )
        # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
        # intentional way to realign it with the configured remote state.
        await self._run_git(["reset", "--hard", remote_ref], env=self._lfs_skip_smudge_env(git_config))
        await self._hydrate_lfs_worktree(git_config, current_head=remote_head)

        after_files = await self._list_tracked_files()
        if before_head is None:
            changed_paths = after_files
        else:
            diff_output = await self._run_git(["diff", "--name-only", "--no-renames", f"{before_head}..HEAD"])
            changed_paths = {path for path in diff_output.splitlines() if self._include_relative_path(path)}

        removed_files = before_files - after_files
        changed_files = {path for path in changed_paths if path in after_files} | (after_files - before_files)
        return changed_files, removed_files, True
