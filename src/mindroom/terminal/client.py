"""The primary's client for persistent terminals in the sandbox runner where an agent's shell commands run.

``WorkerTerminals`` reaches them with the request a call of the agent's worker-routed shell toolkit would send
(``worker_call``): at the same runner, with the same authentication, and the terminal's shell starts from that
request, so it gets the environment and workspace the agent's commands get. Opening a terminal starts the agent's
dedicated worker as a shell call does; nothing else starts it, and a stopped worker has no terminals.

The terminals of one agent's workspace in one worker share an owner key, derived from the request's worker, agent
and workspace: two requesters for whom the agent's calls go to the same worker and workspace see the same
terminals, and nobody else does.

Every viewer's typing is recorded on its terminal's line (``mindroom.terminal.lines``), and a terminal's shell never
starts with a response's CLI grant: the shell outlives the response, while the grant is only for its own commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import aiohttp
from pydantic import BaseModel, TypeAdapter, ValidationError

from mindroom.agent_cli.shell_contract import unbound_agent_cli_shell_env
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.http_error_detail import validation_error_detail
from mindroom.logging_config import get_logger
from mindroom.terminal.lines import PromptMarks, TerminalLine, forget_line, terminal_line
from mindroom.terminal.session import TerminalError, TerminalEvent, TerminalInfo
from mindroom.tool_system.sandbox_proxy import worker_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.terminal.lines import LineState
    from mindroom.tool_system.sandbox_proxy import WorkerCall, WorkerStandIns
    from mindroom.workers.protocol import ExecuteRequest

logger = get_logger(__name__)

_SESSIONS_PATH = "/terminal/sessions"
# How often an attached viewer marks a dedicated worker used, so it is not stopped as idle while someone watches.
_KEEPALIVE_SECONDS = 60.0
# How long a viewer's connection may stay silent before a ping; one unanswered for half as long ends the stream, so a
# viewer whose runner vanished without closing it is told to attach again instead of waiting for output forever.
_HEARTBEAT_SECONDS = 30.0
_HTTP_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 1024 * 1024
# Above the runner's scrollback and its largest output chunk.
_MAX_FRAME_BYTES = 4 * 1024 * 1024
# A session ID as the runner makes one. 🔒 It names a path on the runner, so nothing else may pass for one.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_INFO = TypeAdapter(TerminalInfo)
_INFOS = TypeAdapter(list[TerminalInfo])


class TerminalDetachedError(Exception):
    """The viewer fell too far behind or lost its runner; attaching again replays the scrollback."""


class _Exit(BaseModel):
    type: Literal["exit"]
    code: int | None


class _Refusal(BaseModel):
    # A request validation failure lists what was wrong with each field.
    detail: str | list[object]


class TerminalStream:
    """One attached viewer: the scrollback, then live output, then the shell's exit; and its keys and size."""

    def __init__(self, websocket: aiohttp.ClientWebSocketResponse, line: TerminalLine) -> None:
        self._websocket = websocket
        self.line = line
        """The terminal's line, which this viewer's typing and the prompts it reads update."""
        self.prompt: tuple[int, int] | None = None
        """The number and exit status of the last prompt mark this viewer read."""
        self._marks = PromptMarks()

    async def receive(self) -> TerminalEvent:
        """Return the next output or the exit; raise ``TerminalDetachedError`` when the viewer must attach again."""
        message = await self._websocket.receive()
        if message.type == aiohttp.WSMsgType.BINARY:
            for number, status in self._marks.feed(message.data):
                self.line.prompted(number, status)
                self.prompt = (number, status)
            return TerminalEvent("output", data=message.data)
        if message.type == aiohttp.WSMsgType.TEXT:
            with contextlib.suppress(ValidationError):
                return TerminalEvent("exit", code=_Exit.model_validate_json(message.data).code)
        raise TerminalDetachedError

    async def send_input(self, data: str, *, judged: LineState | None = None, empty_line: bool = False) -> None:
        """Type ``data``, after the input before it, and record it on the terminal's line.

        Given ``judged``, only while the line is still the one it describes, and with ``empty_line`` only while the
        shell waits at its prompt with nothing typed; otherwise it types nothing and raises ``TerminalError`` (409).
        """
        async with self.line.lock:
            if (judged is not None and not self.line.unchanged(judged)) or (empty_line and not self.line.empty):
                raise TerminalError(409, "The terminal's line is not the one these keys were checked against.")
            self.line.type(data)
            try:
                await self._send({"type": "input", "data": data})
            except BaseException:
                self.line.lost()
                raise

    async def resize(self, cols: int, rows: int) -> None:
        """Change the terminal's size."""
        await self._send({"type": "resize", "cols": cols, "rows": rows})

    async def _send(self, frame: dict[str, object]) -> None:
        try:
            await self._websocket.send_json(frame)
        except (aiohttp.ClientError, ConnectionResetError):
            raise TerminalDetachedError from None


def _owner(request: ExecuteRequest) -> str:
    return hashlib.sha256(
        f"{request.worker_key}\n{request.routing_agent_name}\n{request.base_dir}".encode(),
    ).hexdigest()


def _session_path(session_id: str, rest: str = "") -> str:
    if not _SESSION_ID.fullmatch(session_id):
        raise TerminalError(404, "No such terminal.")
    return f"{_SESSIONS_PATH}/{session_id}{rest}"


def _detail(answer: bytes) -> str:
    """Return the runner's reason for a refusal."""
    fallback = "The agent's worker refused."
    try:
        detail = _Refusal.model_validate_json(answer).detail
    except ValidationError:
        return fallback
    return detail if isinstance(detail, str) else validation_error_detail(detail, fallback=fallback)


async def _send(worker: WorkerCall, method: str, path: str, *, body: dict[str, object] | None = None) -> bytes:
    """Send one request to the runner and return its answer, refusing as the runner refused."""
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)) as client,
            client.request(
                method,
                worker.api_root + path,
                headers=worker.headers,
                params={"owner": _owner(worker.request)},
                json=body,
                allow_redirects=False,
            ) as response,
        ):
            answer = await collect_bounded_bytes(response.content.iter_chunked(65536), max_bytes=_MAX_RESPONSE_BYTES)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError) as error:
        raise TerminalError(503, f"The agent's worker is unavailable: {type(error).__name__}") from None
    if response.status >= 400:
        raise TerminalError(response.status if response.status < 500 else 503, _detail(answer))
    return answer


def _parsed[T](adapter: TypeAdapter[T], answer: bytes) -> T:
    try:
        return adapter.validate_json(answer)
    except ValidationError:
        raise TerminalError(503, "The agent's worker answered something else than terminals.") from None


@dataclass(frozen=True)
class WorkerTerminals:
    """The terminals of one agent's workspace, in the worker where ``stand_ins`` (its shell toolkit's) send calls."""

    stand_ins: WorkerStandIns

    @property
    def scope(self) -> tuple[str | None, str | None, str | None]:
        """What the terminals' owner derives from, read without asking the worker backend: worker, agent, workspace."""
        target = self.stand_ins.worker_target
        base_dir = (self.stand_ins.tool_init_overrides or {}).get("base_dir")
        return (
            None if target is None else target.worker_key,
            None if target is None else target.routing_agent_name,
            None if base_dir is None else str(base_dir),
        )

    async def _worker(self, *, start: bool) -> WorkerCall | None:
        try:
            return await asyncio.to_thread(worker_call, self.stand_ins, "run_shell_command", start=start)
        except (RuntimeError, OSError) as error:
            raise TerminalError(503, f"The agent's worker is unavailable: {error}") from None

    async def _running_worker(self) -> WorkerCall:
        worker = await self._worker(start=False)
        if worker is None:
            raise TerminalError(404, "No such terminal.")
        return worker

    async def open(self, *, cols: int = 80, rows: int = 24, cwd: str | None = None) -> TerminalInfo:
        """Start a terminal in ``cwd``, a folder inside the workspace (the workspace itself by default)."""
        with unbound_agent_cli_shell_env():
            worker = await self._worker(start=True)
        if worker is None:
            raise TerminalError(503, "The agent's worker is not ready.")
        body: dict[str, object] = {
            "request": worker.request.model_dump(mode="json"),
            "cols": cols,
            "rows": rows,
            "cwd": cwd,
        }
        info = _parsed(_INFO, await _send(worker, "POST", _SESSIONS_PATH, body=body))
        terminal_line(self.scope, info.id, opened=True)
        return info

    async def sessions(self) -> list[TerminalInfo]:
        """Describe the workspace's terminals, oldest first."""
        worker = await self._worker(start=False)
        if worker is None:
            return []
        return _parsed(_INFOS, await _send(worker, "GET", _SESSIONS_PATH))

    async def close(self, session_id: str) -> None:
        """End a terminal."""
        path = _session_path(session_id)
        await _send(await self._running_worker(), "DELETE", path)
        forget_line(self.scope, session_id)

    @contextlib.asynccontextmanager
    async def attach(self, session_id: str) -> AsyncIterator[TerminalStream]:
        """Attach a viewer to a terminal: it gets the scrollback, then live output, then the exit."""
        path = _session_path(session_id, "/io")
        worker = await self._running_worker()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15)) as client:
            try:
                websocket = await client.ws_connect(
                    worker.api_root + path,
                    headers=worker.headers,
                    params={"owner": _owner(worker.request)},
                    max_msg_size=_MAX_FRAME_BYTES,
                    heartbeat=_HEARTBEAT_SECONDS,
                )
            except aiohttp.WSServerHandshakeError as error:
                if error.status == 403:
                    raise TerminalError(404, "No such terminal.") from None
                raise TerminalError(503, f"The agent's worker is unavailable: HTTP {error.status}") from None
            except (aiohttp.ClientError, TimeoutError) as error:
                raise TerminalError(503, f"The agent's worker is unavailable: {type(error).__name__}") from None
            async with websocket:
                keepalive = asyncio.create_task(self._keep_worker(), name="terminal-worker-keepalive")
                try:
                    yield TerminalStream(websocket, terminal_line(self.scope, session_id))
                finally:
                    keepalive.cancel()
                    await asyncio.wait({keepalive})

    async def _keep_worker(self) -> None:
        while True:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            # A failed lookup, however the worker backend fails, must not stop the next: the viewer still watches.
            try:
                await self._worker(start=False)
            except Exception:
                logger.warning("Terminal keepalive failed to look up the agent's worker", exc_info=True)


__all__ = ["TerminalDetachedError", "TerminalStream", "WorkerTerminals"]
