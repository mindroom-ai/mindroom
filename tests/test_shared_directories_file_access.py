"""Tests for reaching an agent's shared directories through the file and coding tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.coding import CodingTools
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shared_directories import paths as shared_paths
from mindroom.shared_directories.paths import shared_directory_roots
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.shared_directories.tool_roots import SharedDirectoryRoot
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import resolve_worker_target, worker_dir_name
from mindroom.tools.file import file_tools
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    import pytest

_WORKER_KEY = "v1:tenant:shared:bob"


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    share = tmp_path / "shared_directories" / "alice" / "report"
    share.mkdir(parents=True)
    (share / "summary.md").write_text("hello\nshared world\n", encoding="utf-8")
    other = tmp_path / "shared_directories" / "carl" / "secret"
    other.mkdir(parents=True)
    (other / "secret.md").write_text("secret\n", encoding="utf-8")
    return workspace, share, other


def _root(path: Path, *, writable: bool) -> SharedDirectoryRoot:
    return SharedDirectoryRoot(path=path, writable=writable)


def _file_tool(workspace: Path, *roots: SharedDirectoryRoot) -> object:
    return file_tools()(base_dir=workspace, file_access="workspace", shared_directory_roots=lambda: roots)


def test_file_tool_reads_a_member_share_and_refuses_writes(tmp_path: Path) -> None:
    """A joined share is readable, never writable, and other shares stay out of reach."""
    workspace, share, other = _layout(tmp_path)
    tool = _file_tool(workspace, _root(share, writable=False))

    assert tool.read_file(str(share / "summary.md")) == "hello\nshared world\n"
    assert tool.read_file_chunk(str(share / "summary.md"), 1, 1) == "shared world"
    assert "only read" in tool.save_file("x", str(share / "new.md"))
    assert "only read" in tool.delete_file(str(share / "summary.md"))
    assert not (share / "new.md").exists()
    assert (share / "summary.md").exists()
    assert tool.read_file(str(other / "secret.md")).startswith("Error")


def test_listed_share_paths_read_back_the_shared_file(tmp_path: Path) -> None:
    """Listings name share files by absolute path, so reading a listed name never picks up a workspace copy."""
    workspace, share, _other = _layout(tmp_path)
    (workspace / "summary.md").write_text("stale workspace copy\n", encoding="utf-8")
    tool = _file_tool(workspace, _root(share, writable=False))

    listed = json.loads(tool.list_files(str(share)))
    found = json.loads(tool.search_files(f"{share}/*.md"))["files"]
    matched = json.loads(tool.search_content("shared", str(share)))["files"][0]["file"]

    assert listed == found == [str(share / "summary.md")]
    assert matched == str(share / "summary.md")
    assert tool.read_file(listed[0]) == "hello\nshared world\n"


def test_file_tool_writes_an_owned_share(tmp_path: Path) -> None:
    """The owner writes its own share through the file tool and is told the absolute path."""
    workspace, share, _other = _layout(tmp_path)
    tool = _file_tool(workspace, _root(share, writable=True))

    written = tool.save_file("draft", str(share / "notes" / "draft.md"))

    assert written == str(share / "notes" / "draft.md")
    assert (share / "notes" / "draft.md").read_text(encoding="utf-8") == "draft"


def test_file_tool_resolves_relative_paths_that_lead_into_a_share(tmp_path: Path) -> None:
    """A relative path that walks into a share reaches it like the absolute path."""
    workspace, share, _other = _layout(tmp_path)
    tool = _file_tool(workspace, _root(share, writable=False))

    assert tool.read_file("../shared_directories/alice/report/summary.md") == "hello\nshared world\n"


def test_file_tool_refuses_links_out_of_a_share(tmp_path: Path) -> None:
    """A link planted inside a share cannot lead the tool outside it."""
    workspace, share, _other = _layout(tmp_path)
    (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
    (share / "escape.md").symlink_to(tmp_path / "outside.md")
    tool = _file_tool(workspace, _root(share, writable=False))

    assert tool.read_file(str(share / "escape.md")).startswith("Error")


def test_shares_created_after_the_toolkit_are_reachable(tmp_path: Path) -> None:
    """Share roots are looked up on each call, so a share created during a response is usable at once."""
    workspace, share, _other = _layout(tmp_path)
    roots: list[SharedDirectoryRoot] = []
    tool = file_tools()(base_dir=workspace, file_access="workspace", shared_directory_roots=lambda: tuple(roots))
    assert tool.read_file(str(share / "summary.md")).startswith("Error")

    roots.append(_root(share, writable=True))

    assert tool.read_file(str(share / "summary.md")) == "hello\nshared world\n"


def test_coding_tool_reads_greps_and_lists_a_share(tmp_path: Path) -> None:
    """The coding tool reads, searches, and lists a joined share by absolute path and refuses to edit it."""
    workspace, share, other = _layout(tmp_path)
    tool = CodingTools(
        base_dir=str(workspace),
        file_access="workspace",
        shared_directory_roots=lambda: (_root(share, writable=False),),
    )

    assert "shared world" in tool.read_file(str(share / "summary.md"))
    assert f"{share / 'summary.md'}:2:" in tool.grep("shared", str(share))
    assert str(share / "summary.md") in tool.find_files("*.md", str(share))
    assert tool.ls(str(share)) == str(share / "summary.md")
    assert "only read" in tool.write_file(str(share / "new.md"), "x")
    assert "only read" in tool.edit_file(str(share / "summary.md"), "hello", "bye")
    assert (share / "summary.md").read_text(encoding="utf-8").startswith("hello")
    assert tool.read_file(str(other / "secret.md")).startswith("Error")


def test_tools_without_a_workspace_reach_only_their_shares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a workspace, a share stays reachable by absolute path while the working directory is refused."""
    _workspace, share, other = _layout(tmp_path)
    monkeypatch.chdir(other)
    roots = (_root(share, writable=True),)
    file_tool = file_tools()(file_access="workspace", shared_directory_roots=lambda: roots)
    coding_tool = CodingTools(file_access="workspace", shared_directory_roots=lambda: roots)

    assert file_tool.read_file(str(share / "summary.md")) == "hello\nshared world\n"
    assert not file_tool.replace_file_chunk(str(share / "summary.md"), 1, 1, "edited").startswith("Error")
    assert (share / "summary.md").read_text(encoding="utf-8") == "hello\nedited\n"
    assert coding_tool.write_file(str(share / "new.md"), "x").startswith("Wrote")
    assert coding_tool.ls(str(share)) == f"{share / 'new.md'}\n{share / 'summary.md'}"
    refusals = [
        file_tool.read_file("secret.md"),
        file_tool.read_file(str(other / "secret.md")),
        file_tool.list_files(),
        coding_tool.read_file("secret.md"),
        coding_tool.ls(),
    ]
    assert all(refusal.startswith("Error: this agent has no workspace") for refusal in refusals)


def test_unrestricted_tools_ignore_share_roots(tmp_path: Path) -> None:
    """Unrestricted file access already reaches shares by path, so writes are not narrowed."""
    workspace, share, _other = _layout(tmp_path)
    tool = file_tools()(
        base_dir=workspace,
        file_access="unrestricted",
        shared_directory_roots=lambda: (_root(share, writable=False),),
    )

    tool.save_file("x", str(share / "new.md"))

    assert (share / "new.md").read_text(encoding="utf-8") == "x"


def _ledger_config(runtime_paths: RuntimePaths) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={
                "alice": AgentConfig(display_name="Alice"),
                "bob": AgentConfig(display_name="Bob"),
                "dave": AgentConfig(display_name="Dave"),
                "carol": AgentConfig(display_name="Carol", private=AgentPrivateConfig(per="user")),
            },
        ),
        runtime_paths,
    )
    create_shared_directory(runtime_paths, owner="alice", name="report")
    add_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")
    add_shared_directory_member(runtime_paths, owner="alice", name="report", member="carol")
    return config


def test_primary_roots_follow_the_ledger(tmp_path: Path) -> None:
    """In the primary, an agent reaches what it owns read-write and what it joined read-only."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _ledger_config(runtime_paths)
    share = runtime_paths.storage_root / "shared_directories" / "alice" / "report"

    assert shared_directory_roots(config, runtime_paths, "alice") == (_root(share, writable=True),)
    assert shared_directory_roots(config, runtime_paths, "bob") == (_root(share, writable=False),)
    assert shared_directory_roots(config, runtime_paths, "dave") == ()
    assert shared_directory_roots(config, runtime_paths, "carol") == ()
    assert shared_directory_roots(None, runtime_paths, "alice") == ()


def test_primary_tools_never_reach_shares_beside_dedicated_workers(tmp_path: Path) -> None:
    """Owners' workers write their shares, so primary-process path tools must not search anything there."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": "docker", "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test"},
    )
    config = _ledger_config(runtime_paths)

    assert shared_directory_roots(config, runtime_paths, "bob") == ()


def test_primary_tools_never_reach_shares_beside_the_static_runner_sidecar(tmp_path: Path) -> None:
    """The sidecar writes shared_directories/, so primary-process path tools must not follow anything there."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={SANDBOX_RUNTIME_ENV_BY_KEY["proxy_url"]: "http://sandbox:8766"},
    )
    config = _ledger_config(runtime_paths)

    assert shared_directory_roots(config, runtime_paths, "alice") == ()


def _mount_shares(root: Path) -> tuple[Path, Path]:
    report = root / "shared_directories" / "alice" / "report"
    notes = root / "shared_directories" / "carl" / "notes"
    report.mkdir(parents=True)
    notes.mkdir(parents=True)
    (root / "shared_directories" / "carl" / "escape").symlink_to(root)
    (root / "shared_directories" / "carl" / "stray.txt").write_text("x", encoding="utf-8")
    return report, notes


def test_docker_worker_roots_are_its_mounted_shares(tmp_path: Path) -> None:
    """A Docker worker finds its shares below the shared storage root it is told, skipping links and files."""
    report, notes = _mount_shares(tmp_path / "worker")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "dedicated",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(tmp_path / "worker"),
        },
    )

    assert shared_directory_roots(None, runtime_paths, None) == (
        _root(report, writable=True),
        _root(notes, writable=True),
    )


def test_kubernetes_worker_roots_are_its_mounted_shares(tmp_path: Path) -> None:
    """A Kubernetes worker has no shared-root variable, so its shares are found above its own worker root."""
    mount = tmp_path / "mount"
    report, notes = _mount_shares(mount)
    dedicated_root = mount / "workers" / worker_dir_name(_WORKER_KEY)
    dedicated_root.mkdir(parents=True)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=dedicated_root,
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: _WORKER_KEY,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(dedicated_root),
        },
    )

    assert shared_directory_roots(None, runtime_paths, None) == (
        _root(report, writable=True),
        _root(notes, writable=True),
    )


def test_static_runner_roots_are_below_its_storage_root(tmp_path: Path) -> None:
    """The shared sidecar mounts shared directories below its own storage root and sets no shared root."""
    report, notes = _mount_shares(tmp_path / "storage")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true"},
    )

    assert shared_directory_roots(None, runtime_paths, None) == (
        _root(report, writable=True),
        _root(notes, writable=True),
    )


def test_worker_roots_mounted_read_only_refuse_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A share a worker mounts read-only is reported as read-only, so writes get the same refusal as elsewhere."""
    report, notes = _mount_shares(tmp_path / "worker")
    real_statvfs = os.statvfs

    def statvfs(path: os.PathLike[str] | str) -> os.statvfs_result:
        result = real_statvfs(path)
        if Path(path) != notes:
            return result
        fields = list(result)
        fields[8] |= os.ST_RDONLY
        return os.statvfs_result(fields)

    monkeypatch.setattr(shared_paths.os, "statvfs", statvfs)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "dedicated",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(tmp_path / "worker"),
        },
    )

    assert shared_directory_roots(None, runtime_paths, None) == (
        _root(report, writable=True),
        _root(notes, writable=False),
    )


def test_worker_without_shared_directories_has_no_roots(tmp_path: Path) -> None:
    """A worker that mounts no shared directory reaches none."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "dedicated",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(tmp_path / "worker"),
        },
    )

    assert shared_directory_roots(None, runtime_paths, None) == ()


def test_built_file_and_coding_tools_reach_the_calling_agents_shares(tmp_path: Path) -> None:
    """Toolkits built for an agent get its shared directories, so the owner writes and a member only reads."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _ledger_config(runtime_paths)
    share = runtime_paths.storage_root / "shared_directories" / "alice" / "report"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def build(tool_name: str, agent_name: str) -> object:
        return get_tool_by_name(
            tool_name,
            runtime_paths,
            disable_sandbox_proxy=True,
            runtime_config=config,
            tool_init_overrides={"base_dir": workspace},
            worker_target=resolve_worker_target(None, agent_name, None),
        )

    assert build("coding", "alice").write_file(str(share / "summary.md"), "shared\n").startswith("Wrote")
    assert build("file", "bob").read_file(str(share / "summary.md")) == "shared\n"
    assert "only read" in build("file", "bob").save_file("x", str(share / "summary.md"))
    assert "only read" in build("coding", "bob").write_file(str(share / "summary.md"), "x")
    assert build("file", "dave").read_file(str(share / "summary.md")).startswith("Error")
