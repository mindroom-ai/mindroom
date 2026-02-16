"""Workspace-backed memory writing tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.workspace import MEMORY_FILENAME, append_daily_log, get_agent_workspace_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config import Config


class WorkspaceMemoryTools(Toolkit):
    """Tool for explicit writes to workspace memory markdown files."""

    def __init__(self, agent_name: str, storage_path: Path, config: Config) -> None:
        self._agent_name = agent_name
        self._storage_path = storage_path
        self._config = config
        super().__init__(
            name="write_memory",
            tools=[self.write_memory],
        )

    def _append_memory_md(self, content: str) -> None:
        workspace_dir = get_agent_workspace_path(self._agent_name, self._storage_path)
        memory_path = workspace_dir / MEMORY_FILENAME
        memory_path.parent.mkdir(parents=True, exist_ok=True)

        existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        entry = f"{separator}## {timestamp}\n\n{content.strip()}\n\n"
        updated = f"{existing}{entry}"

        max_file_size = self._config.memory.workspace.max_file_size
        updated_size = len(updated.encode("utf-8"))
        if updated_size > max_file_size:
            msg = f"MEMORY.md write exceeds max file size ({max_file_size} bytes)"
            raise ValueError(msg)

        memory_path.write_text(updated, encoding="utf-8")

    async def write_memory(
        self,
        content: str,
        target: str = "daily",
        room_id: str | None = None,
    ) -> str:
        """Write content to daily log or long-term MEMORY.md.

        Args:
            content: Memory content to persist.
            target: `daily` (default) or `memory`.
            room_id: Optional room scope for daily logs.

        Returns:
            Confirmation message or validation error.

        """
        if not content.strip():
            return "No content provided."
        if not self._config.memory.workspace.enabled:
            return "Workspace memory is disabled."
        if target not in {"daily", "memory"}:
            return "Invalid target. Use 'daily' or 'memory'."

        try:
            if target == "daily":
                timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                entry = f"## {timestamp}\n\n{content.strip()}\n\n"
                append_daily_log(
                    self._agent_name,
                    self._storage_path,
                    self._config,
                    entry,
                    room_id=room_id,
                )
            else:
                self._append_memory_md(content)
        except ValueError as exc:
            return f"Failed to write memory: {exc}"

        if target == "daily":
            scope = room_id or "_global"
            return f"Wrote memory to daily log ({scope})."
        return "Wrote memory to MEMORY.md."
