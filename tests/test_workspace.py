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
    load_workspace_memory,
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
