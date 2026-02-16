"""Tests for workspace markdown memory writing tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config import Config
from mindroom.custom_tools.workspace_memory import WorkspaceMemoryTools
from mindroom.tools_metadata import TOOL_METADATA
from mindroom.workspace import get_agent_workspace_path

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config() -> Config:
    """Load app config for tests."""
    return Config.from_yaml()


@pytest.fixture
def tools(tmp_path: Path, config: Config) -> WorkspaceMemoryTools:
    """Create a workspace memory toolkit for testing."""
    return WorkspaceMemoryTools(agent_name="test_agent", storage_path=tmp_path, config=config)


def test_toolkit_name(tools: WorkspaceMemoryTools) -> None:
    """Toolkit should register with the expected name."""
    assert tools.name == "write_memory"
    assert "write_memory" in [f.name for f in tools.async_functions.values()]


@pytest.mark.asyncio
async def test_write_daily_memory(tools: WorkspaceMemoryTools, tmp_path: Path) -> None:
    """Daily target should append into today's scoped daily log."""
    result = await tools.write_memory("Keep this in daily log", target="daily", room_id="!room:server")
    assert "Wrote memory to daily log" in result

    workspace_dir = get_agent_workspace_path("test_agent", tmp_path)
    today = datetime.now(UTC).date().isoformat()
    daily_files = list((workspace_dir / "memory").rglob(f"{today}.md"))
    assert len(daily_files) == 1
    assert "Keep this in daily log" in daily_files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_memory_md(tools: WorkspaceMemoryTools, tmp_path: Path) -> None:
    """Memory target should append to MEMORY.md."""
    result = await tools.write_memory("Durable long-term fact", target="memory")
    assert result == "Wrote memory to MEMORY.md."

    memory_path = get_agent_workspace_path("test_agent", tmp_path) / "MEMORY.md"
    assert memory_path.exists()
    assert "Durable long-term fact" in memory_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_memory_invalid_target(tools: WorkspaceMemoryTools) -> None:
    """Unknown write targets should be rejected."""
    result = await tools.write_memory("test", target="unknown")
    assert result == "Invalid target. Use 'daily' or 'memory'."


@pytest.mark.asyncio
async def test_write_memory_respects_size_limit(tmp_path: Path, config: Config) -> None:
    """Writes above configured size should return a validation error."""
    config.memory.workspace.max_file_size = 20
    tools = WorkspaceMemoryTools(agent_name="test_agent", storage_path=tmp_path, config=config)

    result = await tools.write_memory("This message is too long for MEMORY.md", target="memory")
    assert "Failed to write memory" in result
    assert "max file size" in result


def test_write_memory_tool_registered() -> None:
    """Tool metadata should expose write_memory."""
    assert "write_memory" in TOOL_METADATA
    meta = TOOL_METADATA["write_memory"]
    assert meta.display_name == "Write Workspace Memory"
    assert meta.status.value == "available"
    assert meta.setup_type.value == "none"
