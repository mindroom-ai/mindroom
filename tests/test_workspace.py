"""Tests for markdown workspace behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config import Config
from mindroom.workspace import (
    AGENTS_FILENAME,
    MEMORY_FILENAME,
    SOUL_FILENAME,
    append_daily_log,
    ensure_workspace,
    get_agent_workspace_path,
    load_room_context,
    load_workspace_memory,
    workspace_context_report,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_ensure_workspace_creates_directories_and_templates(tmp_path: Path) -> None:
    """Workspace init should create expected base files and directories."""
    config = Config.from_yaml()
    ensure_workspace("agent", tmp_path, config)

    workspace_dir = get_agent_workspace_path("agent", tmp_path)
    assert workspace_dir.exists()
    assert (workspace_dir / SOUL_FILENAME).exists()
    assert (workspace_dir / AGENTS_FILENAME).exists()
    assert (workspace_dir / MEMORY_FILENAME).exists()
    assert (workspace_dir / "memory").exists()
    assert (workspace_dir / "rooms").exists()


def test_ensure_workspace_is_idempotent(tmp_path: Path) -> None:
    """Running workspace init twice should not overwrite existing files."""
    config = Config.from_yaml()
    ensure_workspace("agent", tmp_path, config)

    workspace_dir = get_agent_workspace_path("agent", tmp_path)
    soul_path = workspace_dir / SOUL_FILENAME
    soul_path.write_text("custom soul", encoding="utf-8")

    ensure_workspace("agent", tmp_path, config)
    assert soul_path.read_text(encoding="utf-8") == "custom soul"


def test_load_workspace_memory_gates_memory_md_to_dm(tmp_path: Path) -> None:
    """MEMORY.md should only be injected for DM/private contexts."""
    config = Config.from_yaml()
    ensure_workspace("agent", tmp_path, config)

    workspace_dir = get_agent_workspace_path("agent", tmp_path)
    (workspace_dir / MEMORY_FILENAME).write_text("private-memory", encoding="utf-8")

    dm_context = load_workspace_memory("agent", tmp_path, config, room_id="!room:server", is_dm=True)
    group_context = load_workspace_memory("agent", tmp_path, config, room_id="!room:server", is_dm=False)

    assert "private-memory" in dm_context
    assert "private-memory" not in group_context


def test_daily_logs_are_scoped_to_room(tmp_path: Path) -> None:
    """Daily logs should not leak between room contexts."""
    config = Config.from_yaml()

    append_daily_log("agent", tmp_path, config, "room-one-note", room_id="!room_one:server")
    append_daily_log("agent", tmp_path, config, "room-two-note", room_id="!room_two:server")

    room_one_context = load_workspace_memory("agent", tmp_path, config, room_id="!room_one:server", is_dm=False)
    room_two_context = load_workspace_memory("agent", tmp_path, config, room_id="!room_two:server", is_dm=False)

    assert "room-one-note" in room_one_context
    assert "room-two-note" not in room_one_context
    assert "room-two-note" in room_two_context
    assert "room-one-note" not in room_two_context


def test_load_room_context_reads_room_file(tmp_path: Path) -> None:
    """Room context helper should load the scoped room markdown file."""
    config = Config.from_yaml()
    ensure_workspace("agent", tmp_path, config)
    workspace_dir = get_agent_workspace_path("agent", tmp_path)
    room_path = workspace_dir / "rooms" / "_room_server.md"
    room_path.write_text("room context text", encoding="utf-8")

    loaded = load_room_context("agent", "!room:server", tmp_path, config)
    assert loaded == "room context text"


def test_workspace_context_report_warns_on_missing_required_files(tmp_path: Path) -> None:
    """Context report should include missing required file warnings."""
    config = Config.from_yaml()
    report = workspace_context_report("agent", tmp_path, config, is_dm=False)

    assert report["agent_name"] == "agent"
    assert report["loaded_files"] == []
    assert any("Missing soul" in warning for warning in report["warnings"])


def test_workspace_context_report_lists_only_loaded_context_files(tmp_path: Path) -> None:
    """Context report should exclude default templates and DM-gated memory."""
    config = Config.from_yaml()
    ensure_workspace("agent", tmp_path, config)
    workspace_dir = get_agent_workspace_path("agent", tmp_path)

    default_report = workspace_context_report("agent", tmp_path, config, is_dm=True)
    assert default_report["loaded_files"] == []

    (workspace_dir / SOUL_FILENAME).write_text("custom soul", encoding="utf-8")
    (workspace_dir / AGENTS_FILENAME).write_text("custom agents", encoding="utf-8")
    (workspace_dir / MEMORY_FILENAME).write_text("custom memory", encoding="utf-8")

    dm_report = workspace_context_report("agent", tmp_path, config, is_dm=True)
    dm_loaded = {entry["filename"] for entry in dm_report["loaded_files"]}
    assert "SOUL.md" in dm_loaded
    assert "AGENTS.md" in dm_loaded
    assert "MEMORY.md" in dm_loaded

    group_report = workspace_context_report("agent", tmp_path, config, is_dm=False)
    group_loaded = {entry["filename"] for entry in group_report["loaded_files"]}
    assert "SOUL.md" in group_loaded
    assert "AGENTS.md" in group_loaded
    assert "MEMORY.md" not in group_loaded
