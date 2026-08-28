"""Letta Agent SDK bridge for MindRoom-owned Matrix conversations."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_PROCESS_TERMINATION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class LettaTurn:
    """One Letta turn routed through a MindRoom conversation."""

    entity_name: str
    agent_id: str
    room_id: str
    thread_id: str | None
    prompt: str
    cwd: Path
    on_terminal: Callable[[Literal["completed", "error", "cancelled"]], None] | None = None


class LettaRuntimeAdapter:
    """Run Letta turns while persisting Matrix-to-conversation identity."""

    def __init__(self, storage_root: Path) -> None:
        self._state_path = storage_root / "letta" / "conversations.db"

    async def stream(  # noqa: C901, PLR0912, PLR0915
        self,
        turn: LettaTurn,
    ) -> AsyncIterator[StructuredStreamChunk]:
        """Yield Letta text and tool snapshots in MindRoom's native stream shape."""
        conversation_key = self._conversation_key(turn)
        conversation_id = await asyncio.to_thread(self._load_conversation, conversation_key)
        process = await asyncio.create_subprocess_exec(
            "nub",
            str(self._bridge_path()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(os.environ),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        request = {
            "agentId": turn.agent_id,
            "conversationId": conversation_id,
            "prompt": turn.prompt,
            "cwd": str(turn.cwd),
        }
        process.stdin.write(json.dumps(request).encode())
        await process.stdin.drain()
        process.stdin.close()

        tool_trace: list[ToolTraceEntry] = []
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        terminal_reported = False

        def report_terminal(status: Literal["completed", "error", "cancelled"]) -> None:
            nonlocal terminal_reported
            if terminal_reported:
                return
            terminal_reported = True
            if turn.on_terminal is not None:
                turn.on_terminal(status)

        try:
            async for raw_line in process.stdout:
                message = json.loads(raw_line)
                message_type = message.get("type")
                if message_type == "session":
                    resolved_id = message.get("conversationId")
                    if not isinstance(resolved_id, str) or not resolved_id:
                        msg = "Letta bridge returned an invalid conversation ID"
                        raise RuntimeError(msg)  # noqa: TRY301
                    await asyncio.to_thread(self._save_conversation, conversation_key, resolved_id)
                elif message_type == "assistant":
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield StructuredStreamChunk(content=content, tool_trace=tool_trace.copy())
                elif message_type == "tool_call":
                    self._update_tool_trace(tool_trace, message)
                    yield StructuredStreamChunk(content="", tool_trace=tool_trace.copy())
                elif message_type == "tool_result":
                    self._complete_tool_trace(tool_trace, message)
                    yield StructuredStreamChunk(content="", tool_trace=tool_trace.copy())
                elif message_type == "result":
                    if not message.get("success", False):
                        report_terminal("error")
                        raise RuntimeError(  # noqa: TRY301
                            str(message.get("errorDetail") or message.get("error") or "Letta turn failed"),
                        )
                    report_terminal("completed")

            return_code = await process.wait()
            stderr = (await stderr_task).decode()
            if return_code != 0:
                report_terminal("error")
                raise RuntimeError(  # noqa: TRY301
                    stderr.strip() or f"Letta bridge exited with status {return_code}",
                )
            if not terminal_reported:
                report_terminal("error")
                msg = "Letta bridge ended without a terminal result"
                raise RuntimeError(msg)  # noqa: TRY301
        except asyncio.CancelledError:
            report_terminal("cancelled")
            raise
        except Exception:
            report_terminal("error")
            raise
        finally:
            if process.returncode is None:
                await asyncio.shield(self._terminate_process(process))
            if not stderr_task.done():
                await stderr_task

    @staticmethod
    def _update_tool_trace(tool_trace: list[ToolTraceEntry], message: dict[str, Any]) -> None:
        """Create or refresh one SDK tool-call delta without duplicating its Matrix card."""
        call_id = message.get("toolCallId")
        args_preview = json.dumps(message.get("toolInput") or {}, ensure_ascii=False)
        for entry in reversed(tool_trace):
            if entry.tool_call_id == call_id:
                entry.tool_name = str(message.get("toolName") or entry.tool_name)
                entry.args_preview = args_preview
                return
        tool_trace.append(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name=str(message.get("toolName") or "tool"),
                args_preview=args_preview,
                tool_call_id=call_id,
            ),
        )

    @staticmethod
    def _complete_tool_trace(tool_trace: list[ToolTraceEntry], message: dict[str, Any]) -> None:
        call_id = message.get("toolCallId")
        for entry in reversed(tool_trace):
            if entry.tool_call_id == call_id:
                entry.type = "tool_call_completed"
                entry.result_preview = str(message.get("content") or "")
                return

    @staticmethod
    def _conversation_key(turn: LettaTurn) -> str:
        return "\u001f".join((turn.entity_name, turn.agent_id, turn.room_id, turn.thread_id or ""))

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        """Abort one bridge turn, then forcefully reap a stuck SDK process."""
        process.terminate()
        try:
            async with asyncio.timeout(_PROCESS_TERMINATION_TIMEOUT_SECONDS):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _bridge_path() -> Path:
        return Path(__file__).resolve().parent / "_letta_bridge" / "index.ts"

    def _connect(self) -> sqlite3.Connection:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._state_path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS conversations (conversation_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)",
        )
        return connection

    def _load_conversation(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_key = ?",
                (key,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def _save_conversation(self, key: str, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations (conversation_key, conversation_id) VALUES (?, ?) "
                "ON CONFLICT(conversation_key) DO UPDATE SET conversation_id = excluded.conversation_id",
                (key, conversation_id),
            )
