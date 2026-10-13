"""Main-process side of a GPT-Live call whose media runs in its own process.

``ProcessLiveVoiceBridge`` is the ``VoiceBridgeLike`` the call session drives.
It starts ``mindroom.matrix_rtc.audio_child``, which runs the LiveKit room,
frame encryption, the agent session and GPT-Live, and talks to it over an
inherited socket. The main process keeps everything that needs MindRoom state:
resolving instructions, running delegations, storing transcripts and usage,
and closing the delegate. Stalls on the main event loop then delay only those
steps, never the call audio.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.logging_config import current_log_file, get_logger, subprocess_logging_env
from mindroom.matrix_rtc.audio_protocol import AudioProtocolError, encode_frame, encode_key, read_frame
from mindroom.matrix_rtc.voice_agent import LiveVoiceAgentOptions, LiveVoiceUsage

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.matrix_rtc.focus import SfuGrant
    from mindroom.matrix_rtc.voice_agent import CallVoiceAgentOptions

logger = get_logger(__name__)

_CLOSED_TIMEOUT_S = 10.0
_EXIT_TIMEOUT_S = 5.0
_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "PYTHONPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)


class _ChildProcess(Protocol):
    """The started call audio process."""

    async def wait(self) -> int:
        """Return the exit code once the process has exited."""
        ...

    def kill(self) -> None:
        """Kill the process and its group."""
        ...


class _PopenChild:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, signal.SIGKILL)


def _spawn_audio_child(child_socket: socket.socket) -> _ChildProcess:
    """Start the call audio process on one end of a socketpair; secrets only ever cross that socket."""
    fd = child_socket.fileno()
    args = [sys.executable, "-m", "mindroom.matrix_rtc.audio_child", "--fd", str(fd)]
    if (log_file := current_log_file()) is not None:
        args += ["--log-file", str(log_file)]
    env = {key: value for key in _CHILD_ENV_KEYS if (value := os.environ.get(key)) is not None}
    env.update({key: value for key, value in os.environ.items() if key.startswith("LIVEKIT_")})
    env.update(subprocess_logging_env())
    process = subprocess.Popen(
        args,
        pass_fds=(fd,),
        start_new_session=True,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    child_socket.close()
    return _PopenChild(process)


class ProcessLiveVoiceBridge:
    """One GPT-Live call whose media runs in a child process."""

    def __init__(
        self,
        *,
        local_identity: str,
        e2ee_enabled: bool,
        spawn: Callable[[socket.socket], _ChildProcess] = _spawn_audio_child,
    ) -> None:
        main_socket, child_socket = socket.socketpair()
        self._child = spawn(child_socket)
        self._queued: list[dict[str, Any]] = [{"type": "Start", "local_identity": local_identity, "e2ee": e2ee_enabled}]
        self._writer: asyncio.StreamWriter | None = None
        self._replies: dict[int, asyncio.Future[None]] = {}
        self._next_request = 0
        self._options: LiveVoiceAgentOptions | None = None
        self._agent_started = False
        self._closing = False
        self._closed = asyncio.Event()
        self._gone = asyncio.Event()
        self._termination_reported = False
        self._responds: dict[int, asyncio.Task[None]] = {}
        self._usage_saves: asyncio.Task[None] | None = None
        self._unsaved_usage: LiveVoiceUsage | None = None
        self._channel = asyncio.get_running_loop().create_task(self._run_channel(main_socket))

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Restrict the child's media to the current Matrix call roster."""
        self._send({"type": "SetRoster", "identities": sorted(participant_identities)})

    async def connect(self, grant: SfuGrant) -> None:
        """Connect the child to the SFU."""
        await self._call({"type": "Connect", "url": grant.url, "jwt": grant.jwt})

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install one participant's media frame key in the child."""
        self._send(
            {"type": "SetFrameKey", "identity": participant_identity, "key": encode_key(key), "index": key_index},
        )

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
        """Resolve the instructions here, then start GPT-Live in the child."""
        if not isinstance(options, LiveVoiceAgentOptions):
            msg = "the call audio process only runs the live backend"
            raise TypeError(msg)
        self._options = options
        instructions = await options.get_instructions()
        await self._call(
            {
                "type": "StartAgent",
                "model": options.model,
                "api_key": options.api_key,
                "voice": options.voice,
                "instructions": instructions,
                "greeting_instructions": options.greeting_instructions,
            },
        )
        self._agent_started = True

    async def aclose(self) -> None:
        """Close the child's call, wait for its last events, then close the delegate."""
        if self._closing:
            return
        self._closing = True
        if not self._gone.is_set():
            self._send({"type": "Close"})
            if not await self._wait_any(self._closed, self._gone, limit_s=_CLOSED_TIMEOUT_S):
                logger.warning("call_audio_child_close_timed_out", timeout_s=_CLOSED_TIMEOUT_S)
                self._child.kill()
        try:
            await asyncio.wait_for(self._child.wait(), _EXIT_TIMEOUT_S)
        except TimeoutError:
            self._child.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._channel, _EXIT_TIMEOUT_S)
        for respond in list(self._responds.values()):
            respond.cancel()
        await asyncio.gather(*self._responds.values(), return_exceptions=True)
        await self._finish_usage_saves()
        if self._options is not None and self._options.close_responder is not None:
            await self._options.close_responder()

    async def _run_channel(self, main_socket: socket.socket) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(sock=main_socket)
            self._writer = writer
            for message in self._queued:
                writer.write(encode_frame(message))
            self._queued.clear()
            while (message := await read_frame(reader)) is not None:
                self._dispatch(message)
        except (AudioProtocolError, OSError):
            logger.warning("call_audio_channel_failed", exc_info=True)
        finally:
            if self._writer is not None:
                self._writer.close()
            self._child_gone()

    def _send(self, message: dict[str, Any]) -> None:
        if self._writer is None:
            self._queued.append(message)
        elif not self._writer.is_closing():
            self._writer.write(encode_frame(message))

    async def _call(self, message: dict[str, Any]) -> None:
        """Send one request and wait for the child's Ok or Error."""
        if self._gone.is_set():
            msg = "call audio process exited"
            raise RuntimeError(msg)
        request = self._next_request
        self._next_request += 1
        reply = asyncio.get_running_loop().create_future()
        self._replies[request] = reply
        self._send({**message, "id": request})
        try:
            await reply
        finally:
            self._replies.pop(request, None)

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind in {"Ok", "Error"}:
            reply = self._replies.get(message["id"])
            if reply is not None and not reply.done():
                if kind == "Ok":
                    reply.set_result(None)
                else:
                    reply.set_exception(RuntimeError(message["message"]))
        elif kind == "RespondRequest":
            self._responds[message["req"]] = asyncio.create_task(self._respond(message["req"], message["prompt"]))
        elif kind == "RespondCancel":
            if (respond := self._responds.get(message["req"])) is not None:
                respond.cancel()
        elif kind == "Closed":
            self._closed.set()
        else:
            self._dispatch_event(message)

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        """Run the main-process callback for one event from the child's bridge."""
        kind = message["type"]
        options = self._options
        if kind == "Terminated":
            self._report_termination(retryable=message["retryable"])
        elif options is None:
            return
        elif kind == "Turn" and options.on_conversation_turn is not None:
            options.on_conversation_turn(message["speaker"], message["text"])
        elif kind == "SessionError" and options.on_session_error is not None:
            options.on_session_error(message["message"])
        elif kind == "Usage":
            usage = LiveVoiceUsage(
                provider_session_id=message["provider_session_id"],
                model=message["model"],
                created_at=message["created_at"],
                duration_seconds=message["duration_seconds"],
                finalized=message["finalized"],
            )
            self._usage_saves = asyncio.create_task(self._save_usage(usage, self._usage_saves))

    async def _respond(self, request: int, prompt: str) -> None:
        """Run one delegation in the main process and answer the child."""
        options = self._options
        if options is None:
            self._responds.pop(request, None)
            self._send({"type": "RespondError", "req": request, "message": "delegation before the agent started"})
            return
        try:
            response = await options.respond(prompt, options.on_tools_executed)
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._send({"type": "RespondError", "req": request, "message": str(error) or type(error).__name__})
        else:
            self._send(
                {
                    "type": "RespondResult",
                    "req": request,
                    "text": response.text,
                    "tool_names": list(response.tool_names),
                    "turn_id": response.turn_id,
                },
            )
        finally:
            self._responds.pop(request, None)

    async def _save_usage(self, usage: LiveVoiceUsage, previous: asyncio.Task[None] | None) -> None:
        """Store usage snapshots in order; remember the newest one whose save failed."""
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        options = self._options
        if options is None or options.record_usage is None:
            return
        try:
            await options.record_usage(usage)
        except Exception:
            logger.warning("call_audio_usage_save_failed", exc_info=True)
            self._unsaved_usage = usage
        else:
            self._unsaved_usage = None

    async def _finish_usage_saves(self) -> None:
        """Wait for pending usage saves, then retry the newest snapshot once if its save failed."""
        if self._usage_saves is not None:
            await asyncio.gather(self._usage_saves, return_exceptions=True)
        usage, self._unsaved_usage = self._unsaved_usage, None
        options = self._options
        if usage is not None and options is not None and options.record_usage is not None:
            try:
                await options.record_usage(usage)
            except Exception:
                logger.warning("call_audio_usage_save_retry_failed", exc_info=True)

    def _child_gone(self) -> None:
        """Fail pending requests; an unexpected exit after start ends the call as retryable."""
        self._gone.set()
        for reply in self._replies.values():
            if not reply.done():
                reply.set_exception(RuntimeError("call audio process exited"))
        if self._closing:
            return
        for respond in list(self._responds.values()):
            respond.cancel()
        if self._agent_started:
            logger.warning("call_audio_child_exited_unexpectedly")
            self._report_termination(retryable=True)

    def _report_termination(self, *, retryable: bool) -> None:
        if self._termination_reported:
            return
        self._termination_reported = True
        if self._options is not None and self._options.on_session_terminated is not None:
            self._options.on_session_terminated(retryable)

    @staticmethod
    async def _wait_any(*events: asyncio.Event, limit_s: float) -> bool:
        """Return whether any event was set within ``limit_s`` seconds."""
        waiters = [asyncio.create_task(event.wait()) for event in events]
        try:
            done, _pending = await asyncio.wait(waiters, timeout=limit_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        return bool(done)
