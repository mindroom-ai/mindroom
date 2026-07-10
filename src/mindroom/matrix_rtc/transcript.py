"""Persistent transcripts for MatrixRTC voice calls.

Each call writes a markdown transcript incrementally (so a crash keeps the
turns recorded so far). Agents using file-backed memory get the transcript
inside their canonical workspace under ``calls/``, where their file tools
can read it later; other agents get it under the runtime storage root. When
the call ends, a short summary entry is appended to the agent's daily memory.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.memory import append_agent_daily_memory
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_TRANSCRIPT_DIRNAME = "calls"


def _call_transcript_path(
    *,
    agent_name: str,
    config: Config,
    storage_path: Path,
    room_id: str,
    started_at: datetime,
) -> Path:
    """Choose the transcript location for one call.

    Agents with file-backed memory keep transcripts inside their workspace; others use
    ``<storage>/calls/<agent>/``.
    """
    if config.resolve_entity(agent_name).memory_backend == "file":
        base = agent_workspace_root_path(storage_path, agent_name) / _TRANSCRIPT_DIRNAME
    else:
        base = storage_path / _TRANSCRIPT_DIRNAME / agent_name
    safe_room = re.sub(r"[^A-Za-z0-9_.-]", "_", room_id)
    stamp = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    return base / f"{stamp}_{safe_room}.md"


@dataclass
class CallTranscript:
    """Incrementally written markdown transcript of one voice call."""

    path: Path
    agent_name: str
    room_id: str
    room_display_name: str
    started_at: datetime
    _turns: int = field(default=0, init=False)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _pending: list[str] = field(default_factory=list, init=False)
    _header_written: bool = field(default=False, init=False)
    _flush_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    @classmethod
    def start(
        cls,
        *,
        agent_name: str,
        config: Config,
        storage_path: Path,
        room_id: str,
        room_display_name: str,
    ) -> CallTranscript:
        """Create the transcript for a call starting now."""
        started_at = datetime.now(tz=UTC)
        path = _call_transcript_path(
            agent_name=agent_name,
            config=config,
            storage_path=storage_path,
            room_id=room_id,
            started_at=started_at,
        )
        return cls(
            path=path,
            agent_name=agent_name,
            room_id=room_id,
            room_display_name=room_display_name,
            started_at=started_at,
        )

    @property
    def turn_count(self) -> int:
        """Number of spoken turns recorded so far."""
        return self._turns

    def record(self, speaker: str, text: str) -> None:
        """Record one finalized conversation turn (safe from sync callbacks)."""
        text = text.strip()
        if not text:
            return
        self._turns += 1
        stamp = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._pending.append(f"- `{stamp}` **{speaker}**: {text}\n")
        self._schedule_flush()

    def record_tool_use(self, tool_names: list[str]) -> None:
        """Record one realtime tool-execution round without counting it as speech."""
        if not tool_names:
            return
        stamp = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._pending.append(f"- `{stamp}` _tools used: {', '.join(tool_names)}_\n")
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._flush_sync()
            except OSError as error:
                logger.warning(
                    "call_transcript_flush_failed",
                    agent=self.agent_name,
                    room_id=self.room_id,
                    error=str(error),
                )
            return
        task = loop.create_task(self._flush())
        self._flush_tasks.add(task)

        def _observe_flush(done: asyncio.Task[None]) -> None:
            self._flush_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.warning(
                    "call_transcript_flush_failed",
                    agent=self.agent_name,
                    room_id=self.room_id,
                    error=str(error),
                )

        task.add_done_callback(_observe_flush)

    async def _flush(self) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._flush_sync)

    def _flush_sync(self) -> None:
        lines = list(self._pending)
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            if not self._header_written:
                started = self.started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                handle.write(
                    f"# Voice call in {self.room_display_name}\n\n"
                    f"- Room: `{self.room_id}`\n- Agent: {self.agent_name}\n- Started: {started}\n\n",
                )
            handle.writelines(lines)
        del self._pending[: len(lines)]
        self._header_written = True

    async def finalize(self, *, config: Config, runtime_paths: RuntimePaths, storage_path: Path) -> None:
        """Flush remaining turns and append a daily-memory entry for the call."""
        ended_at = datetime.now(tz=UTC)
        duration_minutes = max(1, round((ended_at - self.started_at).total_seconds() / 60))
        try:
            async with self._write_lock:
                await asyncio.to_thread(self._flush_sync)
        except OSError as error:
            logger.warning(
                "call_transcript_finalize_failed",
                agent=self.agent_name,
                room_id=self.room_id,
                error=str(error),
            )
            return
        if self._turns == 0:
            return
        summary = (
            f"Joined a voice call in {self.room_display_name} ({self.room_id}): "
            f"{self._turns} spoken turns over ~{duration_minutes} min. "
            f"Transcript: {self.path}"
        )
        try:
            await asyncio.to_thread(
                _append_daily_memory_sync,
                summary,
                self.agent_name,
                storage_path,
                config,
                runtime_paths,
            )
        except Exception as error:
            logger.warning("call_daily_memory_failed", agent=self.agent_name, error=str(error))


def _append_daily_memory_sync(
    summary: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Sync helper so the daily-memory append can run in a worker thread."""
    append_agent_daily_memory(
        summary,
        agent_name,
        storage_path,
        config,
        runtime_paths,
    )
