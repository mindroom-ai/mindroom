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
import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.file_listing import (
    git_checkout_present,
    git_tracked_relative_paths_from_checkout,
    include_knowledge_relative_path,
)
from mindroom.knowledge.redaction import (
    MAX_REDACTABLE_TOKEN_LENGTH,
    credential_free_repo_url,
    embedded_http_userinfo,
    fully_unquoted,
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
    """Return process-local Git config that injects credentials without persisting them.

    Returns None rather than raising for a URL ``urlsplit`` refuses -- one whose
    netloc holds a codepoint that NFKC-normalises to a delimiter. Such a URL
    would raise here with the password quoted in the exception message, and no
    redactor can clean that: there is no ASCII ``@`` for one to anchor on.

    Every caller today reaches this only after ``_persistable_remote_url`` has
    already refused those URLs, so the guard is unreachable in the current call
    order. It is here anyway because that ordering is not a property of this
    function, and the recurring failure in this area has been protection that
    turned out to hold only under assumptions the next edit was free to break.
    Returning None is the right answer regardless: a URL that will be refused
    needs no credentials injected for it.
    """
    clean_url = credential_free_repo_url(repo_url)
    try:
        parsed_clean_url = urlparse(clean_url)
        parsed_repo_url = urlparse(repo_url)
    except ValueError:
        return None

    embedded_userinfo = embedded_http_userinfo(repo_url)
    if embedded_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *embedded_userinfo)

    credentials_userinfo = (
        _http_credentials(credentials_service, runtime_paths) if parsed_clean_url.scheme in {"http", "https"} else None
    )
    if credentials_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *credentials_userinfo)

    if clean_url == repo_url:
        # Nothing was stripped, so there is nothing to restore process-locally.
        return None
    if parsed_repo_url.netloc and "@" in parsed_repo_url.netloc:
        # Userinfo in the authority is handled by the two branches above; it must
        # not be rebuilt into a config key, which Git echoes verbatim on error.
        return None
    # What remains is a secret carried in the query or fragment, which
    # ``credential_free_repo_url`` strips. Restore it for this process only.
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{repo_url}.insteadOf",
        "GIT_CONFIG_VALUE_0": clean_url,
    }


#: scp-style SSH syntax (``git@github.com:org/repo.git``), which ``urlparse``
#: reports as a bare path rather than a URL, plus the same form with the
#: username left off (``github.com:org/repo.git``), which Git clones as the
#: local user. Matched positively and narrowly: the ``(?!//)`` is what stops it
#: swallowing an ordinary ``scheme://host/path``. Underscores are not valid in
#: hostnames but ssh and Git accept them, and internal hosts use them.
_SCP_STYLE_REMOTE_URL: re.Pattern[str] = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):(?!//)[^@]*$",
)


def _unwritable_remote(base_id: str, reason: str) -> RuntimeError:
    """Build the refusal raised instead of writing a remote URL to disk.

    Names no URL. This is raised into an error path that is persisted and shown
    in the dashboard, which is the very thing being prevented.
    """
    return RuntimeError(
        f"Refusing to write an unsafe remote URL for knowledge base '{base_id}' ({reason}). "
        "Use a well-formed URL such as https://host/org/repo.git or git@host:org/repo.git, "
        "and move any secret to a credentials_service instead of embedding it in repo_url. "
        "An existing checkout may still hold a remote written before this check; "
        "delete the checkout directory to clear it.",
    )


def _parsed_remote_url(clean_url: str, base_id: str) -> str:
    """Return `clean_url` when its authority resolves and carries no password."""
    try:
        parsed = urlparse(clean_url)
    except ValueError:
        # ``from None`` deliberately. ``urlsplit`` quotes the offending netloc --
        # password included -- in its message, and the scheduled refresh
        # subprocess logs failures with ``logger.exception``, which prints the
        # whole chain. The refusal above names no URL; chaining would undo that.
        raise _unwritable_remote(base_id, "the URL cannot be parsed") from None

    if not parsed.scheme:
        raise _unwritable_remote(base_id, "the URL has no scheme")

    if not parsed.netloc:
        # ``file:`` is the one scheme with no authority by design, so it has no
        # userinfo to hide; any other empty authority means the string only
        # looked like a URL.
        if parsed.scheme == "file" and "@" not in clean_url:
            return clean_url
        raise _unwritable_remote(base_id, "the URL has no host")

    if parsed.netloc.count("@") > 1:
        raise _unwritable_remote(base_id, "the URL has an ambiguous authority")

    return clean_url


def _refuse_decoded_ssh_password(decoded_url: str, base_id: str) -> None:
    """Refuse an SSH password separator hidden from the sanitizing parse."""
    if not decoded_url.lower().startswith("ssh://"):
        return

    try:
        parsed = urlparse(decoded_url)
    except ValueError:
        raise _unwritable_remote(base_id, "the decoded URL cannot be parsed") from None

    if parsed.scheme == "ssh" and parsed.netloc and parsed.password is not None:
        raise _unwritable_remote(base_id, "an encoded separator hides authentication credentials")


def _persistable_remote_url(repo_url: str, base_id: str) -> str:
    """Return the remote URL safe to write to disk, or refuse to write one.

    Parse or refuse. Four shapes are accepted, each positively matched: a URL
    whose authority ``urlparse`` actually resolves, scp-style SSH syntax, an
    absolute local path, and ``file:`` with no authority. Anything else is
    refused rather than sanitized.

    The distinction matters because classifying by string surgery does not work
    here. A URL scheme and a username share a grammar, so
    ``oauth2:glpat_XXX@gitlab.com:org/repo.git`` -- GitLab's documented
    credential form with ``https://`` dropped -- is reported by ``urlparse`` as
    scheme ``oauth2`` with no authority at all. Stripping that "scheme" removes
    the password separator before anything can look for it. Requiring the parse
    to succeed makes that class unrepresentable instead of merely unmatched.

    Credentials may transit as a process-local ``GIT_CONFIG_*`` header, which is
    what ``_git_auth_env`` is for; they must never be persisted. So this checks
    the string actually about to be written rather than classifying config.
    """
    # ``credential_free_repo_url`` is total, so no guard is needed here; a URL
    # whose netloc ``urlsplit`` refuses comes back unchanged and is rejected
    # below by the authority checks, which is where it should be rejected.
    clean_url = credential_free_repo_url(repo_url)

    # Before any shape is recognised: until the decoded form agrees with the raw
    # one, no branch below is reasoning about the string a client will use. This
    # sat after the scp branch, so an encoded separator reached disk by taking a
    # different route than the one it was written to block.
    if len(clean_url) > MAX_REDACTABLE_TOKEN_LENGTH:
        # Decoding to a fixed point is quadratic in the nesting depth of the
        # input, and nothing above this call bounds it: 195 KiB of nested
        # ``%25`` took 16.8 s. Unlike the redactor's copy of this bound, the
        # input here is operator-authored config rather than remote output, so
        # this is a guard against a foot-gun rather than an attacker -- but a
        # repository URL is never this long, so refusing costs nothing real.
        raise _unwritable_remote(base_id, "the URL is implausibly long")

    decoded_url = fully_unquoted(clean_url)
    if decoded_url.count("@") != clean_url.count("@"):
        raise _unwritable_remote(base_id, "a percent-encoded separator hides part of the URL")

    _refuse_decoded_ssh_password(decoded_url, base_id)

    if clean_url.count("://") > 1:
        # A second URL nested in the path carries its own userinfo, which the
        # authority checks below cannot see.
        raise _unwritable_remote(base_id, "the URL embeds a second URL")

    if _SCP_STYLE_REMOTE_URL.match(clean_url):
        return clean_url

    if clean_url.startswith("/") and not clean_url.startswith("//"):
        # An absolute local path, which Git accepts as a remote for a local
        # clone. It has no authority and therefore no userinfo; the single
        # leading slash is what separates it from a protocol-relative URL.
        return clean_url

    return _parsed_remote_url(clean_url, base_id)


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
            expected_remote = _persistable_remote_url(git_config.repo_url, self.base_id)
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
        clone_url = _persistable_remote_url(git_config.repo_url, self.base_id)
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
