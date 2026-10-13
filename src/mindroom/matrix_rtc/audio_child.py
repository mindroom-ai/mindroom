"""Child process that runs one GPT-Live call's media, away from the MindRoom event loop.

The main process (``audio_process.ProcessLiveVoiceBridge``) drives this
process over an inherited socket. Here ``LiveVoiceBridge`` owns the LiveKit
room, frame encryption, the agent session and GPT-Live, so
garbage collection, SQLite commits and other work on the main loop cannot
interrupt call audio. Bridge callbacks become messages back to the main
process; delegations are a request the main process answers.

Run as ``python -m mindroom.matrix_rtc.audio_child --fd N [--log-file PATH]``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import gc
import signal
import socket
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger, setup_subprocess_logging
from mindroom.matrix_rtc.audio_protocol import AudioProtocolError, decode_key, encode_frame, read_frame
from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.live_voice_agent import LiveVoiceBridge
from mindroom.matrix_rtc.voice_agent import CallAgentResponse, LiveVoiceAgentOptions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.matrix_rtc.voice_agent import LiveVoiceUsage

logger = get_logger(__name__)

_PR_SET_PDEATHSIG = 1


def _live_bridge(local_identity: str, e2ee_enabled: bool) -> LiveVoiceBridge:
    return LiveVoiceBridge(local_identity=local_identity, e2ee_enabled=e2ee_enabled)


class _AudioChild:
    """Serve one call's bridge over a stream until the main process closes it or goes away."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        bridge_factory: Callable[[str, bool], Any] = _live_bridge,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._bridge_factory = bridge_factory
        self._bridge: Any = None
        self._requests: set[asyncio.Task[None]] = set()
        self._responses: dict[int, asyncio.Future[CallAgentResponse]] = {}
        self._next_request = 0
        self._closed = False

    async def serve(self) -> None:
        """Handle messages until ``Close`` or end of stream; always close the bridge."""
        try:
            while (message := await read_frame(self._reader)) is not None:
                if await self._handle(message):
                    return
        finally:
            await self._close_bridge()

    async def _handle(self, message: dict[str, Any]) -> bool:
        """Apply one message; return whether the child should stop serving."""
        kind = message["type"]
        if kind == "Start":
            self._bridge = self._bridge_factory(message["local_identity"], message["e2ee"])
        elif kind == "SetRoster":
            self._bridge.set_participant_identities(frozenset(message["identities"]))
        elif kind == "SetFrameKey":
            self._bridge.set_frame_key(message["identity"], decode_key(message["key"]), message["index"])
        elif kind == "Connect":
            self._request(message["id"], self._bridge.connect(SfuGrant(url=message["url"], jwt=message["jwt"])))
        elif kind == "StartAgent":
            self._request(message["id"], self._bridge.start_agent(self._agent_options(message)))
        elif kind in {"RespondResult", "RespondError"}:
            self._resolve_response(message)
        elif kind == "Close":
            await self._close_bridge()
            self._send({"type": "Closed"})
            await self._writer.drain()
            return True
        else:
            msg = f"unknown message type {kind!r}"
            raise AudioProtocolError(msg)
        return False

    def _request(self, request_id: int, operation: Awaitable[None]) -> None:
        """Run one request without blocking the reader, then reply Ok or Error."""

        async def run() -> None:
            try:
                await operation
            except Exception as error:
                self._send({"type": "Error", "id": request_id, "message": str(error) or type(error).__name__})
            else:
                self._send({"type": "Ok", "id": request_id})

        task = asyncio.create_task(run())
        self._requests.add(task)
        task.add_done_callback(self._requests.discard)

    def _agent_options(self, message: dict[str, Any]) -> LiveVoiceAgentOptions:
        instructions = message["instructions"]

        async def get_instructions() -> str:
            return instructions

        return LiveVoiceAgentOptions(
            get_instructions=get_instructions,
            model=message["model"],
            api_key=message["api_key"],
            voice=message["voice"],
            respond=self._respond,
            record_usage=self._record_usage,
            greeting_instructions=message.get("greeting_instructions"),
            on_conversation_turn=lambda speaker, text: self._send({"type": "Turn", "speaker": speaker, "text": text}),
            on_session_terminated=lambda retryable: self._send({"type": "Terminated", "retryable": retryable}),
            on_session_error=lambda error: self._send({"type": "SessionError", "message": error}),
        )

    async def _respond(self, prompt: str, _on_tools_executed: object) -> CallAgentResponse:
        """Ask the main process to run one agent turn; cancelling asks it to cancel that turn."""
        request = self._next_request
        self._next_request += 1
        response = asyncio.get_running_loop().create_future()
        self._responses[request] = response
        self._send({"type": "RespondRequest", "req": request, "prompt": prompt})
        try:
            return await response
        except asyncio.CancelledError:
            # Cancelling this task also cancels the future it awaits; only an arrived answer needs no cancel.
            if response.cancelled() or not response.done():
                self._send({"type": "RespondCancel", "req": request})
            raise
        finally:
            self._responses.pop(request, None)

    def _resolve_response(self, message: dict[str, Any]) -> None:
        response = self._responses.get(message["req"])
        if response is None or response.done():
            return
        if message["type"] == "RespondResult":
            response.set_result(
                CallAgentResponse(
                    text=message["text"],
                    tool_names=tuple(message["tool_names"]),
                    turn_id=message["turn_id"],
                ),
            )
        else:
            response.set_exception(RuntimeError(message["message"]))

    async def _record_usage(self, usage: LiveVoiceUsage) -> None:
        """Hand the cumulative usage to the main process, which stores it."""
        self._send({"type": "Usage", **asdict(usage)})

    def _send(self, message: dict[str, Any]) -> None:
        """Queue one frame without waiting, so a stalled main process never blocks call audio."""
        if not self._writer.is_closing():
            self._writer.write(encode_frame(message))

    async def _close_bridge(self) -> None:
        if self._closed:
            return
        self._closed = True
        # An agent start still running holds the session lock that closing the bridge needs.
        for request in list(self._requests):
            request.cancel()
        await asyncio.gather(*self._requests, return_exceptions=True)
        if self._bridge is not None:
            await self._bridge.aclose()
        for response in self._responses.values():
            response.cancel()


def _die_with_parent() -> None:
    """Ask Linux to kill this process when the MindRoom process that started it exits.

    Linux sends the signal when the spawning thread exits, so MindRoom spawns
    this process from its event loop thread, never from a worker thread.
    """
    if sys.platform != "linux":
        return
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


async def _serve_socket(fd: int) -> int:
    sock = socket.socket(fileno=fd)
    reader, writer = await asyncio.open_unix_connection(sock=sock)
    try:
        await _AudioChild(reader, writer).serve()
    except AudioProtocolError:
        logger.exception("call_audio_child_protocol_error")
        return 1
    finally:
        writer.close()
    return 0


def _main(argv: list[str] | None = None) -> int:
    """Run one call's media until the main process closes it."""
    parser = argparse.ArgumentParser(prog="mindroom.matrix_rtc.audio_child")
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--log-file")
    args = parser.parse_args(argv)
    _die_with_parent()
    setup_subprocess_logging(log_file=Path(args.log_file) if args.log_file else None)
    # Importing the LiveKit Agents SDK takes about 1.5 s; MindRoom starts this process before a call needs it.
    # LiveKit registers its plugins only on the main thread.
    from livekit.plugins import openai  # noqa: F401, PLC0415

    # The child's import-time heap never needs collecting again.
    gc.collect()
    gc.freeze()
    return asyncio.run(_serve_socket(args.fd))


if __name__ == "__main__":
    raise SystemExit(_main())
