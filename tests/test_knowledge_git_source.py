"""Git-backed knowledge source synchronization tests.

Covers ``mindroom.knowledge.git_source``: how one knowledge base's checkout is
cloned, fetched, force-aligned and LFS-hydrated, and how credentials reach
``git`` without ever landing in the checkout's own config or in published
index metadata.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from mindroom.config.knowledge import KnowledgeGitConfig
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge.git_source import GitKnowledgeSource, GitSyncResult
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import redact_url_credentials
from mindroom.knowledge.refresh_runner import refresh_knowledge_binding
from mindroom.knowledge.registry import (
    get_published_index,
    published_index_metadata_path,
    resolve_published_index_key,
)
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import (
    _config,
    patch_vector_store,  # noqa: F401  # requested via pytestmark below
)

pytestmark = pytest.mark.usefixtures("patch_vector_store")


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
    assert clone_env is None
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
async def test_sync_git_source_once_pulls_lfs_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LFS-enabled repos should explicitly pull LFS objects after resetting to the remote branch."""
    manager = _git_manager(tmp_path, lfs=True)
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
    assert ["lfs", "pull", "origin", "main"] in git_calls
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
async def test_ensure_git_repository_clones_lfs_repo_with_skip_smudge_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial LFS clones should hydrate even if an old hydrated-head marker matches the cloned commit."""
    manager = _git_manager(tmp_path, lfs=True)
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
    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_run_git_redacts_credentials_in_error_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git command errors should not leak embedded URL credentials."""
    manager = _git_manager(tmp_path)

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

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args, kwargs
        return process

    async def _fake_wait_for(awaitable: object, **kwargs: float) -> tuple[bytes, bytes]:
        _ = kwargs["timeout"]
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(manager.git_source, "_sync_timeout_seconds", lambda: 1.0)

    with pytest.raises(RuntimeError, match=r"Git command timed out after 1s: git fetch origin main"):
        await manager.git_source._run_git(["fetch", "origin", "main"])

    assert process.kill_called is True
    assert process.wait_called is True


@pytest.mark.asyncio
async def test_run_git_preserves_index_lock_and_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git lock failures should surface immediately without deleting the lock file."""
    manager = _git_manager(tmp_path)
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

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    task = asyncio.create_task(manager.git_source._run_git(["fetch", "origin", "main"]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.kill_called is True
    assert process.wait_called is True


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
