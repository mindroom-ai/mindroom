"""Git-backed knowledge source synchronization tests.

Covers ``mindroom.knowledge.git_source``: how one knowledge base's checkout is
cloned, fetched, force-aligned and LFS-hydrated, and how credentials reach
``git`` without ever landing in the checkout's own config or in published
index metadata.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread

import pytest

import mindroom.knowledge.git_source as knowledge_git_source_module
import mindroom.knowledge.refresh_locks as knowledge_refresh_locks
from mindroom import file_locks
from mindroom.config.knowledge import KnowledgeGitConfig
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.git_source import GitKnowledgeSource, GitSyncResult
from mindroom.knowledge.github_app_auth import GitHubAppTokenProvider
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import redact_url_credentials
from mindroom.knowledge.refresh_locks import refresh_source_root_lock
from mindroom.knowledge.refresh_runner import (
    refresh_knowledge_binding,
    refresh_knowledge_binding_in_subprocess,
)
from mindroom.knowledge.registry import (
    get_published_index,
    published_index_metadata_path,
    resolve_published_index_key,
    source_root_for_published_index_key,
)
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import (
    _config,
    patch_vector_store,  # noqa: F401  # requested via pytestmark below
)

pytestmark = pytest.mark.usefixtures("patch_vector_store")


def _github_app_manager(tmp_path: Path, *, lfs: bool = False) -> tuple[KnowledgeManager, KnowledgeGitConfig]:
    knowledge_path = tmp_path / "knowledge"
    git_config = KnowledgeGitConfig(
        repo_url="https://github.com/example/private.git",
        branch="main",
        credentials_service="github_app",
        lfs=lfs,
    )
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_app",
        {
            "auth_type": "github_app",
            "app_id": 12345,
            "installation_id": 67890,
            "private_key_file": "/run/secrets/github-app/private-key.pem",
        },
    )
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths), git_config


def _assert_github_app_auth_env(env: dict[str, str] | None) -> None:
    assert env is not None
    encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:installation-token"


def _git_manager(
    tmp_path: Path,
    *,
    lfs: bool = False,
    include_extensions: list[str] | None = None,
    sync_timeout_seconds: int = 3600,
) -> KnowledgeManager:
    knowledge_path = tmp_path / "knowledge"
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                lfs=lfs,
                sync_timeout_seconds=sync_timeout_seconds,
            ),
        },
    )
    if include_extensions is not None:
        config.knowledge_bases["docs"].include_extensions = include_extensions
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))


@pytest.mark.parametrize(
    "repo_url",
    [
        pytest.param("ssh://git%3AS3CR3T-CANARY@example.com/org/repo.git", id="encoded-colon"),
        pytest.param("ssh://git%253AS3CR3T-CANARY@example.com/org/repo.git", id="double-encoded-colon"),
        pytest.param("ssh://git\uff1aS3CR3T-CANARY@example.com/org/repo.git", id="nfkc-colon"),
    ],
)
def test_hidden_ssh_password_separator_is_refused_before_persistence(repo_url: str) -> None:
    """Hidden SSH credentials must never reach the checkout's Git config."""
    with pytest.raises(RuntimeError, match="Refusing to write an unsafe remote URL") as exc_info:
        knowledge_git_source_module._persistable_remote_url(repo_url, "docs")

    assert "S3CR3T-CANARY" not in str(exc_info.value)


@pytest.mark.parametrize(
    "repo_url",
    [
        "ssh://git@example.com/org/repo.git",
        "ssh://git@example.com:2222/org:repo.git",
    ],
)
def test_passwordless_ssh_authority_forms_remain_persistable(repo_url: str) -> None:
    """Usernames, ports, and colons outside userinfo remain supported."""
    assert knowledge_git_source_module._persistable_remote_url(repo_url, "docs") == repo_url


@pytest.mark.asyncio
async def test_git_source_sync_does_not_mutate_index_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git source sync should never bypass candidate publish by mutating the live index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config, runtime_paths)

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        return {"changed.md"}, {"removed.md"}, True

    async def _git_rev_parse(_ref: str) -> str:
        return "rev-source-only"

    async def _git_checkout_present() -> bool:
        return True

    monkeypatch.setattr(manager.git_source, "_sync_once", _sync_once)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _git_rev_parse)
    monkeypatch.setattr(manager.git_source, "_checkout_present", _git_checkout_present)

    result = await manager.git_source.sync()

    assert not hasattr(manager, "remove_file")
    assert not hasattr(manager, "index_file")
    assert result == GitSyncResult(head="rev-source-only", updated=True)
    assert manager.git_source.last_synced_head == "rev-source-only"


@pytest.mark.asyncio
async def test_git_source_sync_without_source_root_ownership_preserves_index_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct sync must not classify a possibly live Git lock as orphaned."""
    manager = _git_manager(tmp_path)
    git_dir = manager.knowledge_path / ".git"
    git_dir.mkdir(parents=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        assert lock_path.exists() is True
        return set(), set(), False

    async def _rev_parse(_ref: str) -> str:
        return "unchanged"

    monkeypatch.setattr(manager.git_source, "_sync_once", _sync_once)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)

    await manager.git_source.sync()

    assert lock_path.exists() is True


@pytest.mark.asyncio
async def test_git_source_sync_with_expired_inherited_capability_preserves_index_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child task cannot retain cleanup authority after its owner's context exits."""
    manager = _git_manager(tmp_path)
    git_dir = manager.knowledge_path / ".git"
    git_dir.mkdir(parents=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")
    released = asyncio.Event()

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        assert lock_path.exists() is True
        return set(), set(), False

    async def _rev_parse(_ref: str) -> str:
        return "unchanged"

    async def _sync_after_release() -> None:
        await released.wait()
        await manager.git_source.sync()

    monkeypatch.setattr(manager.git_source, "_sync_once", _sync_once)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)
    key = resolve_published_index_key(
        "docs",
        config=manager.config,
        runtime_paths=manager.runtime_paths,
    )
    source_root = source_root_for_published_index_key(key)

    async with refresh_source_root_lock(source_root):
        inherited_task = asyncio.create_task(_sync_after_release())

    released.set()
    await inherited_task

    assert lock_path.exists() is True


@pytest.mark.asyncio
async def test_git_source_sync_continues_when_orphaned_lock_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure must leave Git responsible for reporting the repository error."""
    manager = _git_manager(tmp_path)
    git_dir = manager.knowledge_path / ".git"
    git_dir.mkdir(parents=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")
    sync_attempted = False
    original_unlink = Path.unlink

    def _refuse_lock_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == lock_path:
            msg = "refused"
            raise PermissionError(msg)
        original_unlink(path, *args, **kwargs)

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        nonlocal sync_attempted
        sync_attempted = True
        return set(), set(), False

    async def _rev_parse(_ref: str) -> str:
        return "unchanged"

    monkeypatch.setattr(Path, "unlink", _refuse_lock_unlink)
    monkeypatch.setattr(manager.git_source, "_sync_once", _sync_once)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)
    key = resolve_published_index_key(
        "docs",
        config=manager.config,
        runtime_paths=manager.runtime_paths,
    )
    source_root = source_root_for_published_index_key(key)

    async with refresh_source_root_lock(source_root):
        await manager.git_source.sync()

    assert sync_attempted is True
    assert lock_path.exists() is True


@pytest.mark.asyncio
async def test_run_git_inherits_owned_source_root_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git descendant must keep refresh ownership if its Python parent exits."""
    manager = _git_manager(tmp_path)
    monkeypatch.setenv(knowledge_git_source_module._REFRESH_SUBPROCESS_ENV, "1")

    class _SuccessfulProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    subprocess_kwargs: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*_args: object, **kwargs: object) -> _SuccessfulProcess:
        subprocess_kwargs.update(kwargs)
        return _SuccessfulProcess()

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        await manager.git_source._run_git(["status"])
        return set(), set(), False

    async def _rev_parse(_ref: str) -> str:
        return "unchanged"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(manager.git_source, "_sync_once", _sync_once)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)
    key = resolve_published_index_key(
        "docs",
        config=manager.config,
        runtime_paths=manager.runtime_paths,
    )
    source_root = source_root_for_published_index_key(key)

    async with refresh_source_root_lock(source_root):
        await manager.git_source.sync()

    inherited_fds = subprocess_kwargs["pass_fds"]
    assert isinstance(inherited_fds, tuple)
    assert len(inherited_fds) == 1
    assert isinstance(inherited_fds[0], int)
    assert subprocess_kwargs["start_new_session"] is False


@pytest.mark.skipif(os.name == "nt", reason="process groups and inherited file descriptors require POSIX")
@pytest.mark.asyncio
async def test_direct_run_git_cancellation_during_spawn_drains_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after spawn but before handle delivery must drain descendants."""
    manager = _git_manager(tmp_path)
    key = resolve_published_index_key(
        "docs",
        config=manager.config,
        runtime_paths=manager.runtime_paths,
    )
    source_root = source_root_for_published_index_key(key)
    refresh_lock_path = knowledge_refresh_locks._refresh_file_lock_path(source_root)
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_spawn = asyncio.Event()
    spawned_process: asyncio.subprocess.Process | None = None
    helper_ready_path = tmp_path / "spawn-race-helper-ready"
    script = f"""
import os
import signal
import time

child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    with open({str(helper_ready_path)!r}, "w") as ready_file:
        ready_file.write("ready")
    time.sleep(60)
    os._exit(0)

time.sleep(60)
"""

    async def _spawn_git_like_process(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal spawned_process
        spawned_process = await real_create_subprocess_exec(sys.executable, "-c", script, **kwargs)
        for _attempt in range(500):
            if helper_ready_path.exists():
                break
            await asyncio.sleep(0.01)
        else:
            msg = "Git-like helper did not start"
            raise AssertionError(msg)
        spawned.set()
        try:
            await release_spawn.wait()
        except asyncio.CancelledError:
            spawned_process.kill()
            await spawned_process.wait()
            raise
        return spawned_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_git_like_process)
    try:
        async with refresh_source_root_lock(source_root):
            task = asyncio.create_task(manager.git_source._run_git(["status"]))
            await asyncio.wait_for(spawned.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            release_spawn.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert file_locks.file_lock_is_held(refresh_lock_path) is False
    finally:
        release_spawn.set()
        if spawned_process is not None:
            with suppress(ProcessLookupError):
                os.killpg(spawned_process.pid, signal.SIGKILL)
            with suppress(ProcessLookupError):
                await spawned_process.wait()
        for _attempt in range(100):
            if not file_locks.file_lock_is_held(refresh_lock_path):
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_git_credentials_service_token_stays_out_of_git_config_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CredentialsManager Git secrets should be process-local, not copied into checkout config."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/private.git",
        branch="main",
        credentials_service="github_private",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_private",
        {"token": "secret-token"},
    )
    clone_envs: list[dict[str, str] | None] = []
    clean_url = "https://example.com/org/private.git"

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = self
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "init"],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "add", "origin", args[-2]],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            (target / "doc.md").write_text("credential service content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            assert cwd is not None
            await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-auth\n"
        return ""

    monkeypatch.setattr(GitKnowledgeSource, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    git_config_text = (docs_path / ".git" / "config").read_text(encoding="utf-8")
    clone_env = clone_envs[0]

    assert result.index_published is True
    assert clone_env is not None
    assert clone_env["GIT_CONFIG_KEY_0"] == f"http.{clean_url}.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in str(clone_env)
    assert "secret-token" not in git_config_text
    assert "x-access-token" not in git_config_text
    assert clean_url in git_config_text
    assert "secret-token" not in metadata_text
    assert "x-access-token" not in metadata_text


@pytest.mark.asyncio
async def test_git_clone_resolves_github_app_credentials_for_each_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial clone must mint App credentials immediately before Git runs."""
    manager, git_config = _github_app_manager(tmp_path)
    clone_envs: list[dict[str, str] | None] = []
    resolved: list[tuple[str, dict[str, object]]] = []

    async def _resolve(
        _self: GitHubAppTokenProvider,
        repo_url: str,
        credentials: dict[str, object],
    ) -> tuple[str, str]:
        resolved.append((repo_url, credentials))
        return "x-access-token", "installation-token"

    async def _run_git(
        _self: GitKnowledgeSource,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        if args[0] == "clone":
            clone_envs.append(env)
        return ""

    monkeypatch.setattr(GitHubAppTokenProvider, "resolve", _resolve)
    monkeypatch.setattr(GitKnowledgeSource, "_run_git", _run_git)

    assert await manager.git_source._ensure_repository(git_config) is True

    assert len(resolved) == 1
    assert resolved[0][0] == git_config.repo_url
    assert resolved[0][1]["installation_id"] == 67890
    _assert_github_app_auth_env(clone_envs[0])


@pytest.mark.asyncio
async def test_git_fetch_resolves_github_app_credentials_for_each_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every fetch must resolve App credentials so expired tokens can refresh."""
    manager, git_config = _github_app_manager(tmp_path)
    fetch_envs: list[dict[str, str] | None] = []
    resolve_count = 0

    async def _resolve(
        _self: GitHubAppTokenProvider,
        _repo_url: str,
        _credentials: dict[str, object],
    ) -> tuple[str, str]:
        nonlocal resolve_count
        resolve_count += 1
        return "x-access-token", "installation-token"

    async def _ensure_repository(_git_config: KnowledgeGitConfig) -> bool:
        return False

    async def _rev_parse(ref: str) -> str | None:
        return "same-head" if ref in {"HEAD", "origin/main"} else None

    async def _run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        if args[0] == "fetch":
            fetch_envs.append(env)
        return ""

    monkeypatch.setattr(GitHubAppTokenProvider, "resolve", _resolve)
    monkeypatch.setattr(manager.git_source, "_ensure_repository", _ensure_repository)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)
    monkeypatch.setattr(manager.git_source, "_run_git", _run_git)

    changed, removed, updated = await manager.git_source._sync_once(git_config)

    assert (changed, removed, updated) == (set(), set(), False)
    assert resolve_count == 1
    _assert_github_app_auth_env(fetch_envs[0])


@pytest.mark.asyncio
async def test_git_lfs_pull_resolves_github_app_credentials_for_each_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git LFS pulls must use the same refreshable App credential path."""
    manager, git_config = _github_app_manager(tmp_path, lfs=True)
    lfs_envs: list[dict[str, str] | None] = []
    resolve_count = 0

    async def _resolve(
        _self: GitHubAppTokenProvider,
        _repo_url: str,
        _credentials: dict[str, object],
    ) -> tuple[str, str]:
        nonlocal resolve_count
        resolve_count += 1
        return "x-access-token", "installation-token"

    async def _rev_parse(_ref: str) -> str | None:
        return "head"

    async def _run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        if args[:2] == ["lfs", "pull"]:
            lfs_envs.append(env)
        return ""

    monkeypatch.setattr(GitHubAppTokenProvider, "resolve", _resolve)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _rev_parse)
    monkeypatch.setattr(manager.git_source, "_run_git", _run_git)

    await manager.git_source._hydrate_lfs_worktree(git_config)

    assert resolve_count == 1
    _assert_github_app_auth_env(lfs_envs[0])


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:example/private.git",
        "https://embedded-token@github.com/example/private.git",
        "https://example.com/example/private.git",
        "not-a-remote",
    ],
)
@pytest.mark.asyncio
async def test_github_app_credentials_fail_closed_for_noncanonical_remotes(
    tmp_path: Path,
    repo_url: str,
) -> None:
    """Selecting App auth must never fall back to ambient or embedded credentials."""
    manager, _git_config = _github_app_manager(tmp_path)

    with pytest.raises(ValueError, match=r"canonical https://github.com"):
        await knowledge_git_source_module._resolved_git_auth_env(
            repo_url,
            "github_app",
            manager.runtime_paths,
            manager.git_source._github_app_token_provider,
        )


@pytest.mark.asyncio
async def test_resolved_git_auth_loads_static_credentials_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspecting auth type must reuse the same static credential snapshot."""
    manager, _git_config = _github_app_manager(tmp_path)
    credentials_manager = get_runtime_shared_credentials_manager(manager.runtime_paths)
    credentials_manager.save_credentials("github_app", {"token": "static-token"})
    real_load_credentials = credentials_manager.load_credentials
    load_count = 0

    def _load_credentials(service: str) -> dict[str, object] | None:
        nonlocal load_count
        load_count += 1
        return real_load_credentials(service)

    monkeypatch.setattr(credentials_manager, "load_credentials", _load_credentials)

    env = await knowledge_git_source_module._resolved_git_auth_env(
        "https://github.com/example/private.git",
        "github_app",
        manager.runtime_paths,
        manager.git_source._github_app_token_provider,
    )

    assert load_count == 1
    assert env is not None
    encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:static-token"


@pytest.mark.asyncio
async def test_git_embedded_userinfo_url_is_not_reused_in_git_auth_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded Git URL userinfo should become process-local auth without echoing the raw URL."""
    docs_path = tmp_path / "docs"
    raw_url = "https://git-user:secret-token@example.com/org/private.git"
    clean_url = "https://example.com/org/private.git"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_envs: list[dict[str, str] | None] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = (self, cwd)
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "doc.md").write_text("embedded userinfo content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-userinfo\n"
        return ""

    monkeypatch.setattr(GitKnowledgeSource, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    clone_env = clone_envs[0]

    assert result.index_published is True
    assert clone_env is not None
    assert clone_env["GIT_CONFIG_KEY_0"] == f"http.{clean_url}.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert raw_url not in str(clone_env)
    assert "secret-token" not in str(clone_env)


@pytest.mark.parametrize(
    ("raw_url", "clean_url"),
    [
        ("ssh://git-user:secret-token@example.com/org/private.git", "ssh://example.com/org/private.git"),
        ("git+https://git-user:secret-token@example.com/org/private.git", "git+https://example.com/org/private.git"),
    ],
)
@pytest.mark.asyncio
async def test_git_unsupported_scheme_userinfo_is_not_copied_to_git_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_url: str,
    clean_url: str,
) -> None:
    """Unsupported embedded userinfo must not be copied into transient Git config."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_calls: list[tuple[list[str], dict[str, str] | None]] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = (self, cwd)
        if args[0] == "clone":
            clone_calls.append((list(args), env))
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "doc.md").write_text("unsupported scheme userinfo content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-unsupported-userinfo\n"
        return ""

    monkeypatch.setattr(GitKnowledgeSource, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    clone_args, clone_env = clone_calls[0]
    serialized_clone_call = json.dumps({"args": clone_args, "env": clone_env}, sort_keys=True)

    assert result.index_published is True
    assert clone_env == {"GIT_LFS_SKIP_SMUDGE": "1"}
    assert clean_url in clone_args
    assert raw_url not in serialized_clone_call
    assert "secret-token" not in serialized_clone_call
    assert raw_url not in metadata_text
    assert "secret-token" not in metadata_text


@pytest.mark.asyncio
async def test_git_query_and_fragment_tokens_stay_out_of_persistent_remote_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL query and fragment secrets should be transient auth only, never persisted."""
    docs_path = tmp_path / "docs"
    raw_url = "https://example.com/org/private.git?token=query-secret#frag-secret"
    clean_url = "https://example.com/org/private.git"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_envs: list[dict[str, str] | None] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = self
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "init"],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "add", "origin", args[-2]],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            (target / "doc.md").write_text("query credential content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            assert cwd is not None
            await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-query\n"
        return ""

    monkeypatch.setattr(GitKnowledgeSource, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    git_config_text = (docs_path / ".git" / "config").read_text(encoding="utf-8")

    assert result.index_published is True
    assert clone_envs
    assert "query-secret" in str(clone_envs[0])
    assert "frag-secret" in str(clone_envs[0])
    assert clean_url in git_config_text
    assert "query-secret" not in git_config_text
    assert "frag-secret" not in git_config_text
    assert "query-secret" not in metadata_text
    assert "frag-secret" not in metadata_text
    assert redact_url_credentials(config.knowledge_bases["docs"].git.repo_url) == clean_url


@pytest.mark.asyncio
async def test_existing_single_branch_checkout_switches_to_new_remote_branch(tmp_path: Path) -> None:
    """A checkout cloned for one branch should fetch and switch to another configured branch."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("main branch content", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "main")
    await _git(remote_work, "checkout", "-b", "release")
    (remote_work / "doc.md").write_text("release branch content", encoding="utf-8")
    await _git(remote_work, "commit", "-am", "release")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    main_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
    )
    runtime_paths = runtime_paths_for(main_config)
    await refresh_knowledge_binding("docs", config=main_config, runtime_paths=runtime_paths)
    main_lookup = get_published_index("docs", config=main_config, runtime_paths=runtime_paths)
    assert main_lookup.index is not None
    assert [document.content for document in main_lookup.index.knowledge.search("branch", max_results=5)] == [
        "main branch content",
    ]

    release_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="release")},
    )
    result = await refresh_knowledge_binding(
        "docs",
        config=release_config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )
    release_lookup = get_published_index("docs", config=release_config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert release_lookup.index is not None
    assert [document.content for document in release_lookup.index.knowledge.search("branch", max_results=5)] == [
        "release branch content",
    ]


@pytest.mark.asyncio
async def test_refresh_recovers_orphaned_git_index_lock(tmp_path: Path) -> None:
    """A crashed refresh must not leave every later Git refresh permanently wedged."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("before crash", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "before")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    (remote_work / "doc.md").write_text("after crash", encoding="utf-8")
    await _git(remote_work, "commit", "-am", "after")
    await _git(remote_work, "push", str(remote_bare), "main")
    lock_path = docs_path / ".git" / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lock_path.exists() is False
    assert result.index_published is True
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("crash", max_results=5)] == [
        "after crash",
    ]


@pytest.mark.skipif(os.name == "nt", reason="process groups and POSIX shell filters are required")
@pytest.mark.asyncio
async def test_crashed_git_subprocess_recovers_index_lock_on_next_refresh(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real crashed checkout must not block the next subprocess refresh."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    async def _git_output(cwd: Path, *args: str) -> str:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("before crash", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "before")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
        modes={"docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding_in_subprocess("docs", config=config, runtime_paths=runtime_paths)

    lock_path = docs_path / ".git" / "index.lock"
    ready_path = tmp_path / "filter-ready"
    release_path = tmp_path / "filter-release"
    filter_script = tmp_path / "blocking-smudge.sh"
    filter_script.write_text(
        """#!/bin/sh
ready_path=$1
release_path=$2
trap '' TERM
echo $$ > "$ready_path"
while [ ! -e "$release_path" ]; do
    sleep 0.05
done
cat
""",
        encoding="utf-8",
    )
    filter_script.chmod(0o755)
    filter_command = " ".join(shlex.quote(str(path)) for path in (filter_script, ready_path, release_path))
    await _git(docs_path, "config", "filter.blocking.smudge", filter_command)
    await _git(docs_path, "config", "filter.blocking.clean", "cat")
    await _git(docs_path, "config", "filter.blocking.required", "true")

    (remote_work / ".gitattributes").write_text("doc.md filter=blocking\n", encoding="utf-8")
    (remote_work / "doc.md").write_text("after crash", encoding="utf-8")
    await _git(remote_work, "add", ".gitattributes", "doc.md")
    await _git(remote_work, "commit", "-m", "after")
    await _git(remote_work, "push", str(remote_bare), "main")

    real_create_subprocess_exec = asyncio.create_subprocess_exec
    refresh_processes: list[asyncio.subprocess.Process] = []

    async def _capture_refresh_process(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real_create_subprocess_exec(*args, **kwargs)
        refresh_processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture_refresh_process)
    refresh_task = asyncio.create_task(
        refresh_knowledge_binding_in_subprocess("docs", config=config, runtime_paths=runtime_paths),
    )

    async def _wait_for_filter() -> int:
        while True:
            if ready_path.exists():
                try:
                    return int(ready_path.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            if refresh_task.done():
                await refresh_task
                pytest.fail("refresh exited before the blocking filter started")
            await asyncio.sleep(0.01)

    watchdog_stop = Event()
    watchdog_fired = Event()
    watchdog: Thread | None = None

    def _release_filter_if_cleanup_stalls() -> None:
        if not watchdog_stop.wait(timeout=20):
            watchdog_fired.set()
            release_path.touch()

    try:
        filter_pid = await asyncio.wait_for(_wait_for_filter(), timeout=30)
        assert filter_pid > 0
        assert len(refresh_processes) == 1
        watchdog = Thread(target=_release_filter_if_cleanup_stalls, daemon=True)
        watchdog.start()
        os.kill(refresh_processes[0].pid, signal.SIGKILL)
        with pytest.raises(RuntimeError, match=r"failed.*exit code"):
            await refresh_task
    finally:
        release_path.touch()
        watchdog_stop.set()
        if watchdog is not None:
            watchdog.join(timeout=1)
        if not refresh_task.done():
            refresh_task.cancel()
            done, _pending = await asyncio.wait({refresh_task}, timeout=5)
            if not done and refresh_processes:
                with suppress(ProcessLookupError):
                    os.killpg(refresh_processes[0].pid, signal.SIGKILL)
                done, _pending = await asyncio.wait({refresh_task}, timeout=5)
            if not done:
                pytest.fail("refresh task survived test cleanup")
        with suppress(asyncio.CancelledError, RuntimeError):
            refresh_task.result()

    assert watchdog is not None
    assert watchdog.is_alive() is False
    assert watchdog_fired.is_set() is False

    lock_path.write_text("", encoding="utf-8")
    await refresh_knowledge_binding_in_subprocess("docs", config=config, runtime_paths=runtime_paths)

    assert lock_path.exists() is False
    assert (docs_path / "doc.md").read_text(encoding="utf-8") == "after crash"
    assert await _git_output(docs_path, "rev-parse", "HEAD") == await _git_output(remote_work, "rev-parse", "HEAD")


@pytest.mark.skipif(os.name == "nt", reason="process groups and POSIX shell filters are required")
@pytest.mark.asyncio
async def test_direct_refresh_timeout_drains_git_descendants_before_releasing_source_lock(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct refresh timeout must not leave a descendant holding its source lock."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("before timeout", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "before")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url=str(remote_bare),
                branch="main",
                sync_timeout_seconds=5,
            ),
        },
        modes={"docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    monkeypatch.setattr(GitKnowledgeSource, "_sync_timeout_seconds", lambda _self: 1.0)

    ready_path = tmp_path / "filter-ready"
    release_path = tmp_path / "filter-release"
    filter_script = tmp_path / "blocking-smudge.sh"
    filter_script.write_text(
        """#!/bin/sh
ready_path=$1
release_path=$2
trap '' TERM
echo $$ > "$ready_path"
while [ ! -e "$release_path" ]; do
    sleep 0.05
done
cat
""",
        encoding="utf-8",
    )
    filter_script.chmod(0o755)
    filter_command = " ".join(shlex.quote(str(path)) for path in (filter_script, ready_path, release_path))
    await _git(docs_path, "config", "filter.blocking.smudge", filter_command)
    await _git(docs_path, "config", "filter.blocking.clean", "cat")
    await _git(docs_path, "config", "filter.blocking.required", "true")

    (remote_work / ".gitattributes").write_text("doc.md filter=blocking\n", encoding="utf-8")
    (remote_work / "doc.md").write_text("after timeout", encoding="utf-8")
    await _git(remote_work, "add", ".gitattributes", "doc.md")
    await _git(remote_work, "commit", "-m", "after")
    await _git(remote_work, "push", str(remote_bare), "main")

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    source_root = source_root_for_published_index_key(key)
    refresh_lock_path = knowledge_refresh_locks._refresh_file_lock_path(source_root)
    filter_pid: int | None = None
    watchdog_stop = Event()
    watchdog_fired = Event()

    def _release_filter_if_cleanup_stalls() -> None:
        if not watchdog_stop.wait(timeout=5):
            watchdog_fired.set()
            release_path.touch()

    watchdog = Thread(target=_release_filter_if_cleanup_stalls, daemon=True)
    watchdog.start()
    try:
        with pytest.raises(RuntimeError, match=r"Git command timed out after 1s"):
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
        filter_pid = int(ready_path.read_text(encoding="utf-8"))

        assert watchdog_fired.is_set() is False
        assert file_locks.file_lock_is_held(refresh_lock_path) is False
    finally:
        watchdog_stop.set()
        release_path.touch()
        watchdog.join(timeout=1)
        if filter_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(filter_pid, signal.SIGKILL)
        for _attempt in range(100):
            if not file_locks.file_lock_is_held(refresh_lock_path):
                break
            await asyncio.sleep(0.01)

    index_lock_path = docs_path / ".git" / "index.lock"
    index_lock_path.write_text("", encoding="utf-8")

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert index_lock_path.exists() is False
    assert result.index_published is True
    assert (docs_path / "doc.md").read_text(encoding="utf-8") == "after timeout"


@pytest.mark.skipif(os.name == "nt", reason="process groups require POSIX")
@pytest.mark.asyncio
async def test_direct_run_git_drains_descendant_after_successful_leader_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful Git leader must not leave a helper holding its source lock."""
    manager = _git_manager(tmp_path)
    key = resolve_published_index_key(
        "docs",
        config=manager.config,
        runtime_paths=manager.runtime_paths,
    )
    source_root = source_root_for_published_index_key(key)
    refresh_lock_path = knowledge_refresh_locks._refresh_file_lock_path(source_root)
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    spawned_process: asyncio.subprocess.Process | None = None
    script = """
import os
import signal
import time

ready_read_fd, ready_write_fd = os.pipe()
child_pid = os.fork()
if child_pid == 0:
    os.close(ready_read_fd)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    os.write(ready_write_fd, b"1")
    os.close(ready_write_fd)
    time.sleep(60)
    os._exit(0)

os.close(ready_write_fd)
os.read(ready_read_fd, 1)
os.close(ready_read_fd)
os._exit(0)
"""

    async def _spawn_git_like_process(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal spawned_process
        spawned_process = await real_create_subprocess_exec(sys.executable, "-c", script, **kwargs)
        return spawned_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_git_like_process)
    try:
        async with refresh_source_root_lock(source_root):
            assert await manager.git_source._run_git(["status"]) == ""

        assert file_locks.file_lock_is_held(refresh_lock_path) is False
    finally:
        if spawned_process is not None:
            with suppress(ProcessLookupError):
                os.killpg(spawned_process.pid, signal.SIGKILL)
        for _attempt in range(100):
            if not file_locks.file_lock_is_held(refresh_lock_path):
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_refresh_recovers_orphaned_index_lock_in_linked_worktree(tmp_path: Path) -> None:
    """Recovery must follow a linked worktree's .git pointer to its real index lock."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("linked worktree content", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "main")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    seed_checkout = tmp_path / "seed-checkout"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", str(remote_bare), str(seed_checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    docs_path = tmp_path / "worktree-checkout"
    await _git(seed_checkout, "worktree", "add", "--detach", str(docs_path), "HEAD")
    dot_git = docs_path / ".git"
    git_dir = Path(dot_git.read_text(encoding="utf-8").removeprefix("gitdir:").strip())
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
    )
    runtime_paths = runtime_paths_for(config)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lock_path.exists() is False
    assert result.index_published is True
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("linked", max_results=5)] == [
        "linked worktree content",
    ]


@pytest.mark.asyncio
async def test_sync_git_source_once_unchanged_head_skips_worktree_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling an unchanged managed checkout should not scan the working tree."""
    manager = _git_manager(tmp_path, lfs=True)
    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    async def _unexpected_git_list_tracked_files() -> set[str]:
        msg = "unchanged Git sync should not list tracked files"
        raise AssertionError(msg)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(manager.git_source, "_ensure_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager.git_source, "_list_tracked_files", _unexpected_git_list_tracked_files)
    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager.git_source._sync_once(manager.git_source._git_config())

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"] in git_calls
    assert ["lfs", "pull", "origin", "main"] in git_calls
    assert not any(call[:3] == ["diff", "--name-only", "--no-renames"] for call in git_calls)


@pytest.mark.asyncio
async def test_sync_git_source_once_skips_repeated_lfs_pull_for_already_hydrated_unchanged_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged LFS heads should hydrate once, then reuse the persisted hydration marker."""
    manager = _git_manager(tmp_path, lfs=True)
    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    async def _fake_git_list_tracked_files() -> set[str]:
        return {"doc.md"}

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(manager.git_source, "_ensure_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager.git_source, "_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager.git_source._sync_once(manager.git_source._git_config())

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] in git_calls

    hydrated_manager = _git_manager(tmp_path, lfs=True)
    repeated_git_calls: list[list[str]] = []

    async def _fake_run_git_second(args: list[str], **_: object) -> str:
        repeated_git_calls.append(args)
        return ""

    monkeypatch.setattr(hydrated_manager.git_source, "_ensure_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(hydrated_manager.git_source, "_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(hydrated_manager.git_source, "_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(hydrated_manager.git_source, "_run_git", _fake_run_git_second)

    changed_files, removed_files, updated = await hydrated_manager.git_source._sync_once(
        hydrated_manager.git_source._git_config(),
    )

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] not in repeated_git_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lfs", "expects_lfs_pull"),
    [
        pytest.param(True, True, id="lfs-enabled"),
        pytest.param(False, False, id="lfs-disabled"),
    ],
)
async def test_sync_git_source_once_controls_lfs_hydration_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lfs: bool,
    expects_lfs_pull: bool,
) -> None:
    """Updates should suppress implicit smudging and hydrate only when LFS is enabled."""
    manager = _git_manager(tmp_path, lfs=lfs)
    git_calls: list[list[str]] = []
    git_envs: list[tuple[list[str], dict[str, str] | None]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref == "HEAD":
            return "before"
        if ref == "origin/main":
            return "after"
        return None

    list_tracked_files_results = iter([{"doc.md"}, {"doc.md"}])

    async def _fake_git_list_tracked_files() -> set[str]:
        return next(list_tracked_files_results)

    async def _fake_run_git(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> str:
        git_calls.append(args)
        git_envs.append((args, env))
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return "doc.md\n"
        return ""

    monkeypatch.setattr(manager.git_source, "_ensure_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager.git_source, "_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager.git_source._sync_once(manager.git_source._git_config())

    assert updated is True
    assert changed_files == {"doc.md"}
    assert removed_files == set()
    assert (["lfs", "pull", "origin", "main"] in git_calls) is expects_lfs_pull
    assert (
        ["checkout", "--force", "-B", "main", "origin/main"],
        {"GIT_LFS_SKIP_SMUDGE": "1"},
    ) in git_envs
    assert (["reset", "--hard", "origin/main"], {"GIT_LFS_SKIP_SMUDGE": "1"}) in git_envs


@pytest.mark.asyncio
async def test_hydrate_git_lfs_worktree_ignores_index_extension_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index extension filters must not make the Git checkout incomplete."""
    manager = _git_manager(tmp_path, lfs=True, include_extensions=[".md", ".mdx", ".rst"])
    git_calls: list[list[str]] = []

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    async def _fake_git_rev_parse(_ref: str) -> str | None:
        return "head"

    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _fake_git_rev_parse)

    await manager.git_source._hydrate_lfs_worktree(manager.git_source._git_config())

    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_ensure_git_lfs_available_raises_clear_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Git LFS should raise the runtime-image guidance instead of a raw git failure."""
    manager = _git_manager(tmp_path, lfs=True)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        if args == ["lfs", "version"]:
            msg = "git: 'lfs' is not a git command"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)

    with pytest.raises(RuntimeError, match="Git LFS is required for this knowledge base"):
        await manager.git_source._ensure_lfs_available(cwd=manager.knowledge_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lfs", "expects_lfs_pull"),
    [
        pytest.param(True, True, id="lfs-enabled"),
        pytest.param(False, False, id="lfs-disabled"),
    ],
)
async def test_ensure_git_repository_clones_with_explicit_lfs_hydration_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lfs: bool,
    expects_lfs_pull: bool,
) -> None:
    """Clones should suppress implicit smudging and hydrate only when LFS is enabled."""
    manager = _git_manager(tmp_path, lfs=lfs)
    clone_envs: list[dict[str, str] | None] = []
    git_calls: list[list[str]] = []
    manager.git_source.lfs_hydrated_head_path.write_text("same", encoding="utf-8")

    async def _fake_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        git_calls.append(args)
        if args[0] == "clone":
            clone_envs.append(env)
        return ""

    async def _fake_git_rev_parse(_ref: str) -> str | None:
        return "same"

    monkeypatch.setattr(manager.git_source, "_run_git", _fake_run_git)
    monkeypatch.setattr(manager.git_source, "_rev_parse", _fake_git_rev_parse)

    cloned = await manager.git_source._ensure_repository(manager.git_source._git_config())

    assert cloned is True
    assert clone_envs == [{"GIT_LFS_SKIP_SMUDGE": "1"}]
    assert (["lfs", "pull", "origin", "main"] in git_calls) is expects_lfs_pull


@pytest.mark.asyncio
async def test_run_git_redacts_credentials_in_error_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git command errors should not leak embedded URL credentials."""
    manager = _git_manager(tmp_path)
    monkeypatch.setenv(knowledge_git_source_module._REFRESH_SUBPROCESS_ENV, "1")

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    b"fatal: unable to access "
                    b"'https://x-access-token:secret-token@github.com/example/private.git/': "
                    b"The requested URL returned error: 403"
                ),
            )

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FailingProcess:
        _ = args, kwargs
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Git command failed") as exc_info:
        await manager.git_source._run_git(
            [
                "clone",
                "https://x-access-token:secret-token@github.com/example/private.git",
                "dest",
            ],
        )

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "https://***@github.com/example/private.git" in message


@pytest.mark.asyncio
async def test_run_git_timeout_kills_subprocess_and_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed out git commands should terminate the child process and raise a redacted runtime error."""
    manager = _git_manager(tmp_path, sync_timeout_seconds=5)

    class _HangingProcess:
        pid = 12345
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = -9
            return -9

    process = _HangingProcess()
    signalled_groups: list[tuple[int, signal.Signals]] = []

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args
        assert kwargs["start_new_session"] is True
        return process

    async def _fake_wait_for(awaitable: object, **kwargs: float) -> tuple[bytes, bytes]:
        _ = kwargs["timeout"]
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    async def _fake_wait_for_process_group_exit(_process_group_id: int, *, wait_seconds: float = 1.0) -> None:
        _ = wait_seconds

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(
        knowledge_git_source_module.os,
        "killpg",
        lambda process_group_id, sig: signalled_groups.append((process_group_id, sig)),
    )
    monkeypatch.setattr(
        knowledge_git_source_module,
        "_wait_for_process_group_exit",
        _fake_wait_for_process_group_exit,
    )
    monkeypatch.setattr(manager.git_source, "_sync_timeout_seconds", lambda: 1.0)

    with pytest.raises(RuntimeError, match=r"Git command timed out after 1s: git fetch origin main"):
        await manager.git_source._run_git(["fetch", "origin", "main"])

    assert process.kill_called is False
    assert process.wait_called is True
    assert signalled_groups == [(12345, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_run_git_preserves_index_lock_and_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git lock failures should surface immediately without deleting the lock file."""
    manager = _git_manager(tmp_path)
    monkeypatch.setenv(knowledge_git_source_module._REFRESH_SUBPROCESS_ENV, "1")
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    f"fatal: Unable to create '{lock_path}': File exists.\n"
                    "Another git process seems to be running in this repository."
                ).encode(),
            )

    recorded_cwds: list[str] = []

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        _ = args
        recorded_cwds.append(str(kwargs["cwd"]))
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await manager.git_source._run_git(["checkout", "main"], cwd=repo_root)

    assert recorded_cwds == [str(repo_root)]
    assert lock_path.exists() is True


@pytest.mark.asyncio
async def test_run_git_cancellation_kills_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a git command should terminate and reap the child process."""
    manager = _git_manager(tmp_path)
    wait_forever = asyncio.Event()

    class _HangingProcess:
        pid = 12345
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await wait_forever.wait()
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = -9
            return -9

    process = _HangingProcess()
    signalled_groups: list[tuple[int, signal.Signals]] = []

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args
        assert kwargs["start_new_session"] is True
        return process

    async def _fake_wait_for_process_group_exit(_process_group_id: int, *, wait_seconds: float = 1.0) -> None:
        _ = wait_seconds

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(
        knowledge_git_source_module.os,
        "killpg",
        lambda process_group_id, sig: signalled_groups.append((process_group_id, sig)),
    )
    monkeypatch.setattr(
        knowledge_git_source_module,
        "_wait_for_process_group_exit",
        _fake_wait_for_process_group_exit,
    )

    task = asyncio.create_task(manager.git_source._run_git(["fetch", "origin", "main"]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.kill_called is False
    assert process.wait_called is True
    assert signalled_groups == [(12345, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_run_git_success_cleanup_finishes_before_cancellation_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot abandon a successful Git command's group drain."""
    manager = _git_manager(tmp_path)
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _SuccessfulProcess:
        pid = 12345
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _SuccessfulProcess:
        return _SuccessfulProcess()

    async def _fake_terminate(
        _process: _SuccessfulProcess,
        *,
        owned_process_group_id: int | None,
    ) -> None:
        assert owned_process_group_id == 12345
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_finished.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_git_source_module, "_terminate_git_process", _fake_terminate)

    task = asyncio.create_task(manager.git_source._run_git(["status"]))
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert task.done() is False
    finally:
        cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_run_git_reports_the_git_failure_when_stderr_holds_an_unparseable_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed URLs in Git output must not replace the failure with a parse error.

    Redaction runs on whatever a remote or a local ``git`` chose to print, and an
    unterminated IPv6 literal makes ``urlparse`` raise. Raising there would
    destroy the diagnostic exactly when something has already gone wrong.
    """
    manager = _git_manager(tmp_path)
    monkeypatch.setenv(knowledge_git_source_module._REFRESH_SUBPROCESS_ENV, "1")

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"fatal: unable to access 'http://[': bad address"

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FailingProcess:
        _ = args, kwargs
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Git command failed with exit code 128") as exc_info:
        await manager.git_source._run_git(["fetch", "origin", "main"])

    assert "bad address" in str(exc_info.value)
