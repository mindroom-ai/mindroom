"""Tests for reaching an agent's shared directories through the file and coding tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.coding import CodingTools
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.shared_directories.tool_roots import SharedDirectoryRoot, shared_directory_roots
from mindroom.tools.file import file_tools
from tests.conftest import bind_runtime_paths, test_runtime_paths

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


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


def _file_tool(workspace: Path, *roots: SharedDirectoryRoot) -> object:
    return file_tools()(base_dir=workspace, file_access="workspace", shared_directory_roots=roots)


def test_file_tool_reads_a_member_share_and_refuses_writes(tmp_path: Path) -> None:
    """A joined share is readable, never writable, and other shares stay out of reach."""
    workspace, share, other = _layout(tmp_path)
    tool = _file_tool(workspace, SharedDirectoryRoot(path=share, writable=False))

    assert tool.read_file(str(share / "summary.md")) == "hello\nshared world\n"
    assert tool.read_file_chunk(str(share / "summary.md"), 1, 1) == "shared world"
    assert tool.list_files(str(share)) == '[\n    "summary.md"\n]'
    assert "only read" in tool.save_file("x", str(share / "new.md"))
    assert "only read" in tool.delete_file(str(share / "summary.md"))
    assert not (share / "new.md").exists()
    assert (share / "summary.md").exists()
    assert tool.read_file(str(other / "secret.md")).startswith("Error")


def test_file_tool_writes_an_owned_share(tmp_path: Path) -> None:
    """The owner writes its own share through the file tool."""
    workspace, share, _other = _layout(tmp_path)
    tool = _file_tool(workspace, SharedDirectoryRoot(path=share, writable=True))

    tool.save_file("draft", str(share / "notes" / "draft.md"))

    assert (share / "notes" / "draft.md").read_text(encoding="utf-8") == "draft"


def test_file_tool_resolves_relative_paths_that_lead_into_a_share(tmp_path: Path) -> None:
    """A relative path that walks into a share reaches it like the absolute path."""
    workspace, share, _other = _layout(tmp_path)
    tool = _file_tool(workspace, SharedDirectoryRoot(path=share, writable=False))

    assert tool.read_file("../shared_directories/alice/report/summary.md") == "hello\nshared world\n"


def test_file_tool_refuses_links_out_of_a_share(tmp_path: Path) -> None:
    """A link planted inside a share cannot lead the tool outside it."""
    workspace, share, _other = _layout(tmp_path)
    (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
    (share / "escape.md").symlink_to(tmp_path / "outside.md")
    tool = _file_tool(workspace, SharedDirectoryRoot(path=share, writable=False))

    assert tool.read_file(str(share / "escape.md")).startswith("Error")


def test_coding_tool_reads_greps_and_lists_a_share(tmp_path: Path) -> None:
    """The coding tool reads, searches, and lists a joined share and refuses to edit it."""
    workspace, share, other = _layout(tmp_path)
    tool = CodingTools(
        base_dir=str(workspace),
        file_access="workspace",
        shared_directory_roots=(SharedDirectoryRoot(path=share, writable=False),),
    )

    assert "shared world" in tool.read_file(str(share / "summary.md"))
    assert "shared world" in tool.grep("shared", str(share))
    assert "summary.md" in tool.find_files("*.md", str(share))
    assert "summary.md" in tool.ls(str(share))
    assert "only read" in tool.write_file(str(share / "new.md"), "x")
    assert "only read" in tool.edit_file(str(share / "summary.md"), "hello", "bye")
    assert (share / "summary.md").read_text(encoding="utf-8").startswith("hello")
    assert tool.read_file(str(other / "secret.md")).startswith("Error")


def test_unrestricted_tools_ignore_share_roots(tmp_path: Path) -> None:
    """Unrestricted file access already reaches shares by path, so writes are not narrowed."""
    workspace, share, _other = _layout(tmp_path)
    tool = file_tools()(
        base_dir=workspace,
        file_access="unrestricted",
        shared_directory_roots=(SharedDirectoryRoot(path=share, writable=False),),
    )

    tool.save_file("x", str(share / "new.md"))

    assert (share / "new.md").read_text(encoding="utf-8") == "x"


def test_primary_roots_follow_the_ledger(tmp_path: Path) -> None:
    """In the primary, an agent reaches what it owns read-write and what it joined read-only."""
    config = bind_runtime_paths(
        Config(
            agents={
                "alice": AgentConfig(display_name="Alice"),
                "bob": AgentConfig(display_name="Bob"),
                "dave": AgentConfig(display_name="Dave"),
                "carol": AgentConfig(display_name="Carol", private=AgentPrivateConfig(per="user")),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = test_runtime_paths(tmp_path)
    create_shared_directory(runtime_paths, owner="alice", name="report", now=_NOW)
    add_shared_directory_member(runtime_paths, owner="alice", name="report", member="bob")
    add_shared_directory_member(runtime_paths, owner="alice", name="report", member="carol")
    share = runtime_paths.storage_root / "shared_directories" / "alice" / "report"

    assert shared_directory_roots(runtime_paths, config, "alice") == (SharedDirectoryRoot(path=share, writable=True),)
    assert shared_directory_roots(runtime_paths, config, "bob") == (SharedDirectoryRoot(path=share, writable=False),)
    assert shared_directory_roots(runtime_paths, config, "dave") == ()
    assert shared_directory_roots(runtime_paths, config, "carol") == ()
    assert shared_directory_roots(runtime_paths, None, "alice") == ()


@pytest.mark.parametrize("planted", ["link", "file"])
def test_worker_roots_are_the_mounted_shares(tmp_path: Path, planted: str) -> None:
    """In a sandbox runner, the shares below the shared storage root are what the worker mounted."""
    shared_root = tmp_path / "worker"
    (shared_root / "shared_directories" / "alice" / "report").mkdir(parents=True)
    (shared_root / "shared_directories" / "carl" / "notes").mkdir(parents=True)
    if planted == "link":
        (shared_root / "shared_directories" / "carl" / "escape").symlink_to(tmp_path)
    else:
        (shared_root / "shared_directories" / "carl" / "stray.txt").write_text("x", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "dedicated",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(shared_root),
        },
    )

    assert shared_directory_roots(runtime_paths, None, None) == (
        SharedDirectoryRoot(path=shared_root / "shared_directories" / "alice" / "report", writable=True),
        SharedDirectoryRoot(path=shared_root / "shared_directories" / "carl" / "notes", writable=True),
    )


def test_static_runner_roots_are_below_its_storage_root(tmp_path: Path) -> None:
    """The shared sidecar mounts shared directories below its own storage root and sets no shared root."""
    (tmp_path / "storage" / "shared_directories" / "alice" / "report").mkdir(parents=True)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true"},
    )

    assert shared_directory_roots(runtime_paths, None, None) == (
        SharedDirectoryRoot(path=runtime_paths.storage_root / "shared_directories" / "alice" / "report", writable=True),
    )
