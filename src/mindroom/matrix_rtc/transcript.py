"""Persistent transcripts for MatrixRTC voice calls.

Each call writes a markdown transcript incrementally. File-memory agents keep
it in their workspace; other agents use the runtime call archive. When the
call ends, recoverable context is stored through the configured memory backend.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.logging_config import get_logger
from mindroom.memory import add_agent_memory
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TRANSCRIPT_DIRNAME = "calls"


def _new_call_transcript_path(base: Path, *, room_id: str, started_at: datetime) -> Path:
    """Create one collision-safe transcript path under an already-resolved root."""
    safe_room = re.sub(r"[^A-Za-z0-9_.-]", "_", room_id)
    stamp = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    return base / f"{stamp}_{uuid4().hex}_{safe_room}.md"


def _call_transcript_roots(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> tuple[Path, Path]:
    """Resolve transcript storage and portable-reference roots for one caller scope."""
    agent_runtime = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    if config.resolve_entity(agent_name).memory_backend == "file":
        workspace = agent_runtime.workspace
        if workspace is None:
            msg = f"File-memory agent '{agent_name}' has no resolved workspace"
            raise ValueError(msg)
        return workspace.root / _TRANSCRIPT_DIRNAME, workspace.root
    if agent_runtime.execution.is_private:
        return agent_runtime.state_root / _TRANSCRIPT_DIRNAME, agent_runtime.state_root
    storage_root = runtime_paths.storage_root.expanduser().resolve()
    return storage_root / _TRANSCRIPT_DIRNAME / agent_name, storage_root


@dataclass
class CallTranscript:
    """Incrementally written markdown transcript of one voice call."""

    path: Path
    agent_name: str
    room_id: str
    room_display_name: str
    started_at: datetime
    reference_root: Path
    execution_identity: ToolExecutionIdentity | None
    _turns: int = field(default=0, init=False)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _pending: list[str] = field(default_factory=list, init=False)
    _header_written: bool = field(default=False, init=False)
    _flush_task: asyncio.Task[None] | None = field(default=None, init=False)

    @classmethod
    def start(
        cls,
        *,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
        room_id: str,
        room_display_name: str,
    ) -> CallTranscript:
        """Create the transcript for a call starting now."""
        started_at = datetime.now(tz=UTC)
        transcript_root, reference_root = _call_transcript_roots(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        path = _new_call_transcript_path(transcript_root, room_id=room_id, started_at=started_at)
        return cls(
            path=path,
            agent_name=agent_name,
            room_id=room_id,
            room_display_name=room_display_name,
            started_at=started_at,
            reference_root=reference_root,
            execution_identity=execution_identity,
        )

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
        if self._flush_task is not None and not self._flush_task.done():
            return
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
        self._flush_task = loop.create_task(self._flush_background())

    async def _flush_background(self) -> None:
        try:
            await self._flush()
        except OSError as error:
            logger.warning(
                "call_transcript_flush_failed",
                agent=self.agent_name,
                room_id=self.room_id,
                error=str(error),
            )
        finally:
            self._flush_task = None

    async def _flush(self) -> None:
        async with self._write_lock:
            while self._pending:
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

    async def finalize(self, *, config: Config, runtime_paths: RuntimePaths) -> None:
        """Flush remaining turns and store a memory reference for the call."""
        ended_at = datetime.now(tz=UTC)
        duration_minutes = max(1, round((ended_at - self.started_at).total_seconds() / 60))
        try:
            await self._flush()
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
        memory_backend = config.resolve_entity(self.agent_name).memory_backend
        if memory_backend == "none":
            return
        transcript_path = self.path.relative_to(self.reference_root).as_posix()
        summary = (
            f"Joined a voice call in {self.room_display_name} ({self.room_id}): "
            f"{self._turns} spoken turns over ~{duration_minutes} min. "
            f"Transcript: {transcript_path}"
        )
        try:
            memory_content = summary
            if memory_backend == "mem0":
                transcript = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                memory_content = f"{summary}\n\n{transcript}"
            await add_agent_memory(
                memory_content,
                self.agent_name,
                runtime_paths.storage_root,
                config,
                runtime_paths,
                metadata={"source": "matrix_rtc_call", "transcript_path": transcript_path},
                execution_identity=self.execution_identity,
            )
        except Exception as error:
            logger.warning("call_memory_reference_failed", agent=self.agent_name, error=str(error))
