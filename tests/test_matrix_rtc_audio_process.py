"""Tests for running GPT-Live call audio in its own process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.matrix_rtc.audio_child import _AudioChild
from mindroom.matrix_rtc.audio_process import ProcessLiveVoiceBridge, _spawn_audio_child, _WarmAudioProcess
from mindroom.matrix_rtc.audio_protocol import (
    _MAX_FRAME_BYTES,
    AudioProtocolError,
    decode_key,
    encode_frame,
    encode_key,
    read_frame,
)
from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import CallAgentResponse, LiveVoiceAgentOptions, LiveVoiceUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

_SRC = Path(__file__).resolve().parents[1] / "src"
_AGENT_STACK = ("agno", "nio", "mindroom.agents", "mindroom.matrix_rtc.call_manager", "mindroom.memory")


def _loaded_agent_stack(*modules: str) -> list[str]:
    """Import ``modules`` in a fresh interpreter and return the agent-stack modules that came with them."""
    script = (
        "import importlib, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        f"print(','.join(name for name in {_AGENT_STACK!r} if name in sys.modules))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True)
    return [name for name in result.stdout.strip().split(",") if name]


def test_live_audio_modules_import_without_the_agent_stack() -> None:
    """The call audio child imports these modules, so they must not pull in agents, Matrix, or the call manager."""
    assert _loaded_agent_stack("mindroom.matrix_rtc.live_voice_agent", "mindroom.matrix_rtc.focus") == []


async def _reader_with(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_frame_round_trip() -> None:
    """A message survives encoding and reading back unchanged."""
    message = {"type": "Turn", "speaker": "user", "text": "hé"}
    assert await read_frame(await _reader_with(encode_frame(message))) == message


@pytest.mark.asyncio
async def test_read_frame_returns_none_at_eof() -> None:
    """A stream that ends between frames reads as None."""
    assert await read_frame(await _reader_with(b"")) is None


@pytest.mark.asyncio
async def test_truncated_frame_is_a_protocol_error() -> None:
    """A stream that ends inside a frame is a protocol error."""
    frame = encode_frame({"type": "Turn", "speaker": "user", "text": "hello"})
    with pytest.raises(AudioProtocolError):
        await read_frame(await _reader_with(frame[:-3]))


@pytest.mark.asyncio
async def test_oversize_frame_is_rejected() -> None:
    """A declared length above the limit is rejected before reading the payload."""
    with pytest.raises(AudioProtocolError):
        await read_frame(await _reader_with(struct.pack(">I", _MAX_FRAME_BYTES + 1), eof=False))


@pytest.mark.asyncio
async def test_frame_without_type_is_rejected() -> None:
    """Only JSON objects with a string type are messages."""
    payload = json.dumps(["not", "an", "object"]).encode()
    with pytest.raises(AudioProtocolError):
        await read_frame(await _reader_with(struct.pack(">I", len(payload)) + payload))


def test_key_round_trip() -> None:
    """Frame-key bytes survive base64 encoding."""
    key = os.urandom(16)
    assert decode_key(encode_key(key)) == key


class FakeLiveBridge:
    """Records what the child runtime asks of the media bridge."""

    def __init__(self, local_identity: str, e2ee_enabled: bool) -> None:
        self.local_identity = local_identity
        self.e2ee_enabled = e2ee_enabled
        self.calls: list[tuple[object, ...]] = []
        self.options: LiveVoiceAgentOptions | None = None
        self.connect_error: Exception | None = None
        self.on_close: Callable[[], Awaitable[None]] | None = None
        self.closed = False
        self.block_start = False
        self.start_cancelled = False
        # LiveKit's AgentSession holds one lock through both start and close.
        self._session_lock = asyncio.Lock()

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Record the roster."""
        self.calls.append(("roster", participant_identities))

    async def connect(self, grant: SfuGrant) -> None:
        """Record the grant, or fail as configured."""
        self.calls.append(("connect", grant))
        if self.connect_error is not None:
            raise self.connect_error

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Record the frame key."""
        self.calls.append(("key", participant_identity, key, key_index))

    async def start_agent(self, options: LiveVoiceAgentOptions) -> None:
        """Keep the options the child built, then hang if configured to."""
        async with self._session_lock:
            self.options = options
            try:
                if self.block_start:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.start_cancelled = True
                raise

    async def aclose(self) -> None:
        """Run the configured close hook."""
        async with self._session_lock:
            if self.on_close is not None:
                await self.on_close()
            self.closed = True


@dataclass
class ChildChannel:
    """The main-process side of a socketpair whose other end runs ``_AudioChild``."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    bridges: list[FakeLiveBridge]
    task: asyncio.Task[None]

    def send(self, message: dict[str, object]) -> None:
        """Send one frame to the child."""
        self.writer.write(encode_frame(message))

    async def recv(self) -> dict[str, object] | None:
        """Read one frame from the child."""
        return await asyncio.wait_for(read_frame(self.reader), 5)

    async def started(self) -> FakeLiveBridge:
        """Send Start and return the bridge the child built."""
        self.send({"type": "Start", "local_identity": "@bot:x:DEV", "e2ee": True})
        for _ in range(100):
            if self.bridges:
                return self.bridges[0]
            await asyncio.sleep(0.01)
        msg = "child never built its bridge"
        raise AssertionError(msg)

    async def agent_options(self, **fields: object) -> LiveVoiceAgentOptions:
        """Start the agent and return the options the child built."""
        bridge = await self.started()
        message = {
            "type": "StartAgent",
            "id": 7,
            "model": "gpt-live-1",
            "api_key": "sk-test",
            "voice": "marin",
            "instructions": "be brief",
            "greeting_instructions": "say hi",
            **fields,
        }
        self.send(message)
        assert await self.recv() == {"type": "Ok", "id": 7}
        assert bridge.options is not None
        return bridge.options


@asynccontextmanager
async def child_channel() -> AsyncIterator[ChildChannel]:
    """Run ``_AudioChild`` with a fake bridge on one end of a real socketpair."""
    child_socket, main_socket = socket.socketpair()
    child_reader, child_writer = await asyncio.open_unix_connection(sock=child_socket)
    main_reader, main_writer = await asyncio.open_unix_connection(sock=main_socket)
    bridges: list[FakeLiveBridge] = []

    def factory(local_identity: str, e2ee_enabled: bool) -> FakeLiveBridge:
        bridges.append(FakeLiveBridge(local_identity, e2ee_enabled))
        return bridges[-1]

    task = asyncio.create_task(
        _AudioChild(child_reader, child_writer, bridge_factory=factory).serve(),
    )
    channel = ChildChannel(main_reader, main_writer, bridges, task)
    try:
        yield channel
    finally:
        main_writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_child_connects_and_reports_errors() -> None:
    """Connect replies Ok, and a failing connect replies with its error message."""
    async with child_channel() as channel:
        bridge = await channel.started()
        assert (bridge.local_identity, bridge.e2ee_enabled) == ("@bot:x:DEV", True)
        channel.send({"type": "Connect", "id": 1, "url": "wss://sfu", "jwt": "j"})
        assert await channel.recv() == {"type": "Ok", "id": 1}
        assert bridge.calls == [("connect", SfuGrant(url="wss://sfu", jwt="j"))]
        bridge.connect_error = RuntimeError("no sfu")
        channel.send({"type": "Connect", "id": 2, "url": "wss://sfu", "jwt": "j"})
        assert await channel.recv() == {"type": "Error", "id": 2, "message": "no sfu"}


@pytest.mark.asyncio
async def test_child_forwards_roster_and_frame_keys() -> None:
    """Roster and frame-key messages reach the bridge with decoded values."""
    async with child_channel() as channel:
        bridge = await channel.started()
        key = os.urandom(16)
        channel.send({"type": "SetRoster", "identities": ["@a:x:D1", "@a:x:D2"]})
        channel.send({"type": "SetFrameKey", "identity": "@a:x:D1", "key": encode_key(key), "index": 3})
        channel.send({"type": "Connect", "id": 1, "url": "wss://sfu", "jwt": "j"})
        assert await channel.recv() == {"type": "Ok", "id": 1}
        assert bridge.calls[:2] == [("roster", frozenset({"@a:x:D1", "@a:x:D2"})), ("key", "@a:x:D1", key, 3)]


@pytest.mark.asyncio
async def test_child_starts_agent_with_resolved_instructions() -> None:
    """The child's options carry the instructions the main process resolved, plus the speech settings."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        assert await options.get_instructions() == "be brief"
        assert (options.model, options.api_key, options.voice, options.greeting_instructions) == (
            "gpt-live-1",
            "sk-test",
            "marin",
            "say hi",
        )
        assert options.close_responder is None
        assert options.on_tools_executed is None


@pytest.mark.asyncio
async def test_child_respond_round_trip() -> None:
    """A delegation becomes a RespondRequest whose result or error comes back to the bridge."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        answer = asyncio.create_task(options.respond("weather?", None))
        request = await channel.recv()
        assert request == {"type": "RespondRequest", "req": request["req"], "prompt": "weather?"}
        channel.send(
            {
                "type": "RespondResult",
                "req": request["req"],
                "text": "sunny",
                "tool_names": ["weather"],
                "turn_id": "t1",
            },
        )
        assert await answer == CallAgentResponse(text="sunny", tool_names=("weather",), turn_id="t1")
        failing = asyncio.create_task(options.respond("again?", None))
        request = await channel.recv()
        channel.send({"type": "RespondError", "req": request["req"], "message": "agent failed"})
        with pytest.raises(RuntimeError, match="agent failed"):
            await failing


@pytest.mark.asyncio
async def test_child_cancelled_respond_sends_cancel() -> None:
    """Cancelling a delegation in the child tells the main process to cancel its agent turn."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        answer = asyncio.create_task(options.respond("long task", None))
        request = await channel.recv()
        answer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await answer
        assert await channel.recv() == {"type": "RespondCancel", "req": request["req"]}


@pytest.mark.asyncio
async def test_child_callbacks_become_events() -> None:
    """Bridge callbacks become one-way event frames."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        assert options.on_conversation_turn is not None
        assert options.record_usage is not None
        assert options.on_session_error is not None
        assert options.on_session_terminated is not None
        options.on_conversation_turn("user", "hi")
        await options.record_usage(LiveVoiceUsage("sess", "gpt-live-1", 1.5, 12.0, finalized=False))
        options.on_session_error("x")
        options.on_session_terminated(True)
        assert [await channel.recv() for _ in range(4)] == [
            {"type": "Turn", "speaker": "user", "text": "hi"},
            {
                "type": "Usage",
                "provider_session_id": "sess",
                "model": "gpt-live-1",
                "created_at": 1.5,
                "duration_seconds": 12.0,
                "finalized": False,
            },
            {"type": "SessionError", "message": "x"},
            {"type": "Terminated", "retryable": True},
        ]


@pytest.mark.asyncio
async def test_events_do_not_wait_for_a_stalled_reader() -> None:
    """A main process that stops reading never blocks the child's callbacks."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        assert options.on_conversation_turn is not None
        started = time.monotonic()
        for _ in range(2000):
            options.on_conversation_turn("assistant", "x" * 500)
        assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_child_close_order() -> None:
    """Close runs the bridge's close, whose final events precede Closed, then the child stops serving."""
    async with child_channel() as channel:
        options = await channel.agent_options()
        bridge = channel.bridges[0]

        async def final_usage() -> None:
            assert options.record_usage is not None
            await options.record_usage(LiveVoiceUsage("sess", "gpt-live-1", 1.5, 30.0, finalized=True))

        bridge.on_close = final_usage
        channel.send({"type": "Close"})
        assert (await channel.recv())["type"] == "Usage"
        assert await channel.recv() == {"type": "Closed"}
        await asyncio.wait_for(channel.task, 5)
        assert bridge.closed


class InProcessChild:
    """A ``ChildProcess`` that serves ``_AudioChild`` as a task in this event loop."""

    def __init__(
        self,
        child_socket: socket.socket,
        bridges: list[FakeLiveBridge],
        *,
        hang_on_close: bool = False,
    ) -> None:
        self.bridges = bridges
        self.hang_on_close = hang_on_close
        self.killed = False
        self._socket = child_socket
        self._opened = False
        self.task = asyncio.get_running_loop().create_task(self._serve(child_socket))

    async def _serve(self, child_socket: socket.socket) -> None:
        reader, writer = await asyncio.open_unix_connection(sock=child_socket)
        self._opened = True

        def factory(local_identity: str, e2ee_enabled: bool) -> FakeLiveBridge:
            bridge = FakeLiveBridge(local_identity, e2ee_enabled)
            if self.hang_on_close:
                bridge.on_close = asyncio.Event().wait
            self.bridges.append(bridge)
            return bridge

        try:
            await _AudioChild(reader, writer, bridge_factory=factory).serve()
        finally:
            writer.close()

    async def wait(self) -> int:
        """Return once the child task has finished."""
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        return 0

    def running(self) -> bool:
        """Return whether the child task is still serving."""
        return not self.task.done()

    def kill(self) -> None:
        """Stop the child task, closing its end of the socket."""
        self.killed = True
        self.task.cancel()
        if not self._opened:
            # A killed process closes its socket even if it never read from it.
            self._socket.close()


@dataclass
class MainSide:
    """Main-process callbacks for one ``ProcessLiveVoiceBridge`` call."""

    turns: list[tuple[str, str]]
    errors: list[str]
    terminated: list[bool]
    usage: list[LiveVoiceUsage]
    events: list[str]
    responses: dict[str, CallAgentResponse]
    respond_started: asyncio.Event
    respond_cancelled: asyncio.Event
    fail_usage: bool = False
    cleanup_gate: asyncio.Event | None = None

    async def _answer(self, prompt: str) -> CallAgentResponse:
        """Answer one prompt: hang, fail, or return the configured response."""
        if prompt == "hang":
            await asyncio.Event().wait()
        if prompt == "denied":
            msg = "caller lost access"
            raise PermissionError(msg)
        return self.responses[prompt]

    async def _on_cancel(self) -> None:
        """Record the cancel, then finish cleanup like a cancelled agent turn persisting its interrupted run."""
        self.respond_cancelled.set()
        if self.cleanup_gate is not None:
            await asyncio.shield(self.cleanup_gate.wait())
            self.events.append("cleanup_done")

    def options(self) -> LiveVoiceAgentOptions:
        """Build main-side options whose callables record what the proxy does."""

        async def get_instructions() -> str:
            self.events.append("instructions")
            return "resolved in main"

        async def respond(prompt: str, on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
            assert on_tools is on_tools_executed
            self.respond_started.set()
            try:
                return await self._answer(prompt)
            except asyncio.CancelledError:
                await self._on_cancel()
                raise

        async def record_usage(usage: LiveVoiceUsage) -> None:
            if self.fail_usage:
                self.fail_usage = False
                msg = "storage busy"
                raise RuntimeError(msg)
            self.usage.append(usage)

        async def close_responder() -> None:
            self.events.append("close_responder")

        def on_tools_executed(names: list[str]) -> None:
            self.events.append(f"tools:{','.join(names)}")

        return LiveVoiceAgentOptions(
            get_instructions=get_instructions,
            model="gpt-live-1",
            api_key="sk-test",
            voice="marin",
            respond=respond,
            record_usage=record_usage,
            close_responder=close_responder,
            greeting_instructions="say hi",
            on_conversation_turn=lambda speaker, text: self.turns.append((speaker, text)),
            on_tools_executed=on_tools_executed,
            on_session_terminated=self.terminated.append,
            on_session_error=self.errors.append,
        )


def _main_side() -> MainSide:
    return MainSide([], [], [], [], [], {}, asyncio.Event(), asyncio.Event())


@asynccontextmanager
async def proxy_bridge(
    *,
    hang_on_close: bool = False,
) -> AsyncIterator[tuple[ProcessLiveVoiceBridge, list[FakeLiveBridge], list[InProcessChild]]]:
    """Build a ``ProcessLiveVoiceBridge`` whose child runs in-process with a fake media bridge."""
    bridges: list[FakeLiveBridge] = []
    children: list[InProcessChild] = []

    def start_child() -> tuple[socket.socket, InProcessChild]:
        main_socket, child_socket = socket.socketpair()
        children.append(InProcessChild(child_socket, bridges, hang_on_close=hang_on_close))
        return main_socket, children[-1]

    bridge = ProcessLiveVoiceBridge(local_identity="@bot:x:DEV", e2ee_enabled=True, start_child=start_child)
    try:
        yield bridge, bridges, children
    finally:
        await asyncio.wait_for(bridge.aclose(), 10)


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "condition never became true"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_proxy_connect_and_errors() -> None:
    """Connect reaches the child's bridge, and its failure raises in the main process."""
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.connect(SfuGrant(url="wss://sfu", jwt="j"))
        assert bridges[0].calls == [("connect", SfuGrant(url="wss://sfu", jwt="j"))]
        assert (bridges[0].local_identity, bridges[0].e2ee_enabled) == ("@bot:x:DEV", True)
        bridges[0].connect_error = RuntimeError("no sfu")
        with pytest.raises(RuntimeError, match="no sfu"):
            await bridge.connect(SfuGrant(url="wss://sfu", jwt="j"))


@pytest.mark.asyncio
async def test_proxy_queues_roster_before_connect() -> None:
    """Roster and keys set right after construction reach the child before Connect."""
    async with proxy_bridge() as (bridge, bridges, _children):
        key = os.urandom(16)
        bridge.set_participant_identities(frozenset({"@a:x:D1"}))
        bridge.set_frame_key("@a:x:D1", key, 2)
        await bridge.connect(SfuGrant(url="wss://sfu", jwt="j"))
        assert bridges[0].calls == [
            ("roster", frozenset({"@a:x:D1"})),
            ("key", "@a:x:D1", key, 2),
            ("connect", SfuGrant(url="wss://sfu", jwt="j")),
        ]


@pytest.mark.asyncio
async def test_proxy_start_agent_resolves_instructions_in_main() -> None:
    """Instructions are resolved by the main-process callable and handed to the child as text."""
    main = _main_side()
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        assert main.events == ["instructions"]
        assert await child_options.get_instructions() == "resolved in main"
        assert (child_options.model, child_options.voice, child_options.greeting_instructions) == (
            "gpt-live-1",
            "marin",
            "say hi",
        )


@pytest.mark.asyncio
async def test_proxy_respond_runs_in_main_with_tool_callback() -> None:
    """A child delegation runs the main-process respond with its tool callback and returns the answer."""
    main = _main_side()
    main.responses["weather?"] = CallAgentResponse("sunny", tool_names=("weather",), turn_id="t1")
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        assert await child_options.respond("weather?", None) == main.responses["weather?"]


@pytest.mark.asyncio
async def test_proxy_respond_cancel_reaches_main_task() -> None:
    """Cancelling a delegation in the child cancels the main-process agent turn."""
    main = _main_side()
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        delegation = asyncio.create_task(child_options.respond("hang", None))
        await asyncio.wait_for(main.respond_started.wait(), 5)
        delegation.cancel()
        await asyncio.wait_for(main.respond_cancelled.wait(), 5)


@pytest.mark.asyncio
async def test_proxy_callbacks_run_in_main() -> None:
    """Turns, errors, terminations and usage from the child run the main-process callbacks."""
    main = _main_side()
    main.fail_usage = True
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        assert child_options.on_conversation_turn is not None
        assert child_options.on_session_error is not None
        assert child_options.on_session_terminated is not None
        assert child_options.record_usage is not None
        child_options.on_conversation_turn("user", "hi")
        child_options.on_session_error("provider failed")
        await child_options.record_usage(LiveVoiceUsage("sess", "gpt-live-1", 1.0, 5.0, finalized=False))
        child_options.on_session_terminated(False)
        await _until(lambda: bool(main.terminated))
        assert main.turns == [("user", "hi")]
        assert main.errors == ["provider failed"]
        assert main.terminated == [False]
        assert main.usage == []
    # The failed save of the newest snapshot is retried once when the call closes.
    assert main.usage == [LiveVoiceUsage("sess", "gpt-live-1", 1.0, 5.0, finalized=False)]


@pytest.mark.asyncio
async def test_proxy_aclose_order() -> None:
    """The responder closes only after the child confirmed Closed and its final usage was saved."""
    main = _main_side()
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())

        async def final_usage() -> None:
            child_options = bridges[0].options
            assert child_options is not None
            assert child_options.record_usage is not None
            await child_options.record_usage(LiveVoiceUsage("sess", "gpt-live-1", 1.0, 30.0, finalized=True))
            main.events.append("child_closed")

        bridges[0].on_close = final_usage
    assert main.events == ["instructions", "child_closed", "close_responder"]
    assert main.usage == [LiveVoiceUsage("sess", "gpt-live-1", 1.0, 30.0, finalized=True)]


@pytest.mark.asyncio
async def test_child_death_mid_respond_cancels_and_terminates() -> None:
    """A child that dies during a delegation cancels the main turn and reports a retryable termination once."""
    main = _main_side()
    async with proxy_bridge() as (bridge, bridges, children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        asyncio.create_task(child_options.respond("hang", None))  # noqa: RUF006
        await asyncio.wait_for(main.respond_started.wait(), 5)
        children[0].kill()
        await asyncio.wait_for(main.respond_cancelled.wait(), 5)
        await _until(lambda: bool(main.terminated))
        assert main.terminated == [True]
    assert main.terminated == [True]


@pytest.mark.asyncio
async def test_connect_raises_when_child_is_gone() -> None:
    """Connect fails fast when the child already exited instead of waiting for a reply."""
    async with proxy_bridge() as (bridge, _bridges, children):
        await _until(lambda: bool(children))
        children[0].kill()
        with pytest.raises(RuntimeError, match="exited"):
            await asyncio.wait_for(bridge.connect(SfuGrant(url="wss://sfu", jwt="j")), 5)


@pytest.mark.asyncio
async def test_aclose_kills_a_hung_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that never answers Close is killed, and the responder still closes."""
    monkeypatch.setattr("mindroom.matrix_rtc.audio_process._CLOSED_TIMEOUT_S", 0.2)
    main = _main_side()
    async with proxy_bridge(hang_on_close=True) as (bridge, _bridges, children):
        await bridge.start_agent(main.options())
    assert children[0].killed
    assert main.events[-1] == "close_responder"


@pytest.mark.asyncio
async def test_aclose_lets_a_cancelled_delegation_finish_its_cleanup() -> None:
    """Closing never cancels a delegation a second time while it persists its interrupted run."""
    main = _main_side()
    main.cleanup_gate = asyncio.Event()
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        delegation = asyncio.create_task(child_options.respond("hang", None))
        await asyncio.wait_for(main.respond_started.wait(), 5)
        delegation.cancel()
        await asyncio.wait_for(main.respond_cancelled.wait(), 5)
        closing = asyncio.create_task(bridge.aclose())
        await _until(lambda: bridges[0].closed)
        for _ in range(20):
            await asyncio.sleep(0)
        assert "close_responder" not in main.events
        main.cleanup_gate.set()
        await asyncio.wait_for(closing, 5)
    assert main.events[-2:] == ["cleanup_done", "close_responder"]


@pytest.mark.asyncio
async def test_failed_delegation_logs_its_real_error_type_in_main() -> None:
    """A delegation that fails in the main process is logged there with its own type, not the child's."""
    main = _main_side()
    with patch("mindroom.matrix_rtc.audio_process.logger.warning") as warning:
        async with proxy_bridge() as (bridge, bridges, _children):
            await bridge.start_agent(main.options())
            child_options = bridges[0].options
            assert child_options is not None
            with pytest.raises(RuntimeError, match="PermissionError"):
                await child_options.respond("denied", None)
    failures = [call for call in warning.call_args_list if call.args == ("call_live_delegation_failed",)]
    assert len(failures) == 1
    assert failures[0].kwargs["error_type"] == "PermissionError"
    assert "caller lost access" not in str(failures[0].kwargs)


@pytest.mark.asyncio
async def test_proxy_retries_each_provider_sessions_failed_usage_at_close() -> None:
    """A failed usage save is retried at close even after another provider session saved."""
    main = _main_side()
    main.fail_usage = True
    first = LiveVoiceUsage("sess-1", "gpt-live-1", 1.0, 5.0, finalized=False)
    second = LiveVoiceUsage("sess-2", "gpt-live-1", 9.0, 3.0, finalized=False)
    async with proxy_bridge() as (bridge, bridges, _children):
        await bridge.start_agent(main.options())
        child_options = bridges[0].options
        assert child_options is not None
        assert child_options.record_usage is not None
        await child_options.record_usage(first)
        await child_options.record_usage(second)
        await _until(lambda: main.usage == [second])
    assert main.usage == [second, first]


@pytest.mark.asyncio
async def test_proxy_logs_an_unexpected_channel_failure() -> None:
    """A malformed reply that breaks the channel is logged before the call ends."""
    ends: list[socket.socket] = []

    def start_child() -> tuple[socket.socket, IdleProcess]:
        main_socket, child_socket = socket.socketpair()
        ends.append(child_socket)
        return main_socket, IdleProcess()

    with patch("mindroom.matrix_rtc.audio_process.logger.warning") as warning:
        bridge = ProcessLiveVoiceBridge(local_identity="@bot:x:DEV", e2ee_enabled=True, start_child=start_child)
        ends[0].sendall(encode_frame({"type": "Ok"}))
        await _until(lambda: warning.called)
        ends[0].close()
        await asyncio.wait_for(bridge.aclose(), 5)
    assert warning.call_args_list[0].args == ("call_audio_channel_failed",)


def test_spawn_keeps_native_library_paths_and_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audio process can load native libraries from the loader paths, but never sees provider keys."""
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "NIX_LD_LIBRARY_PATH"):
        monkeypatch.setenv(name, f"/nix/{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    launched: list[dict[str, str]] = []

    class RecordingPopen:
        pid = 1

        def __init__(self, _args: list[str], **kwargs: object) -> None:
            env = kwargs["env"]
            assert isinstance(env, dict)
            launched.append(env)

    monkeypatch.setattr("mindroom.matrix_rtc.audio_process.subprocess.Popen", RecordingPopen)
    main_socket, child_socket = socket.socketpair()
    _spawn_audio_child(child_socket)
    main_socket.close()
    assert {
        name: launched[0].get(name) for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "NIX_LD_LIBRARY_PATH")
    } == {
        "LD_LIBRARY_PATH": "/nix/LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH": "/nix/DYLD_LIBRARY_PATH",
        "NIX_LD_LIBRARY_PATH": "/nix/NIX_LD_LIBRARY_PATH",
    }
    assert "OPENAI_API_KEY" not in launched[0]


@pytest.mark.asyncio
async def test_child_close_cancels_a_pending_agent_start_first() -> None:
    """Close cancels an agent start still in flight, so teardown never waits behind it."""
    async with child_channel() as channel:
        bridge = await channel.started()
        bridge.block_start = True
        channel.send(
            {
                "type": "StartAgent",
                "id": 7,
                "model": "gpt-live-1",
                "api_key": "sk-test",
                "voice": "marin",
                "instructions": "be brief",
            },
        )
        await _until(lambda: bridge.options is not None)
        channel.send({"type": "Close"})
        assert await channel.recv() == {"type": "Closed"}
        assert bridge.start_cancelled
        assert bridge.closed


def test_audio_child_imports_without_the_agent_stack() -> None:
    """The child entrypoint itself stays clear of agents, Matrix and the call manager."""
    assert _loaded_agent_stack("mindroom.matrix_rtc.audio_child") == []


@pytest.mark.asyncio
async def test_real_child_starts_and_closes() -> None:
    """The real child process starts, builds its bridge, closes on request and exits cleanly."""
    main_socket, child_socket = socket.socketpair()
    child = _spawn_audio_child(child_socket)
    reader, writer = await asyncio.open_unix_connection(sock=main_socket)
    try:
        writer.write(encode_frame({"type": "Start", "local_identity": "@bot:x:DEV", "e2ee": False}))
        writer.write(encode_frame({"type": "Close"}))
        assert await asyncio.wait_for(read_frame(reader), 60) == {"type": "Closed"}
        assert await asyncio.wait_for(child.wait(), 30) == 0
    finally:
        child.kill()
        writer.close()


class IdleProcess:
    """A started call audio process that only reports whether it is alive."""

    def __init__(self) -> None:
        self.alive = True

    async def wait(self) -> int:
        """Return at once; the fake process never runs."""
        return 0

    def running(self) -> bool:
        """Return whether the fake process is alive."""
        return self.alive

    def kill(self) -> None:
        """Mark the fake process dead."""
        self.alive = False


def _idle_spawner() -> tuple[list[IdleProcess], Callable[[socket.socket], IdleProcess]]:
    spawned: list[IdleProcess] = []

    def spawn(child_socket: socket.socket) -> IdleProcess:
        child_socket.close()
        spawned.append(IdleProcess())
        return spawned[-1]

    return spawned, spawn


def test_warm_audio_process_hands_over_the_idle_child_and_starts_its_replacement() -> None:
    """A call takes the prewarmed process, and the next call's process starts right away."""
    spawned, spawn = _idle_spawner()
    warm = _WarmAudioProcess(spawn)
    warm.prewarm()
    warm.prewarm()
    assert len(spawned) == 1
    main_socket, child = warm.take()
    main_socket.close()
    assert child is spawned[0]
    assert len(spawned) == 2


def test_warm_audio_process_replaces_an_idle_child_that_died() -> None:
    """A prewarmed process that exited while idle is never handed to a call."""
    spawned, spawn = _idle_spawner()
    warm = _WarmAudioProcess(spawn)
    warm.prewarm()
    spawned[0].alive = False
    main_socket, child = warm.take()
    main_socket.close()
    assert child is spawned[1]
    assert len(spawned) == 3


def test_warm_audio_process_hands_over_the_idle_child_when_its_replacement_fails() -> None:
    """A call keeps the idle process even when starting the next one fails."""
    spawned, spawn = _idle_spawner()
    attempts: list[int] = []

    def flaky_spawn(child_socket: socket.socket) -> IdleProcess:
        attempts.append(1)
        if len(attempts) > 1:
            child_socket.close()
            msg = "fork failed"
            raise BlockingIOError(msg)
        return spawn(child_socket)

    warm = _WarmAudioProcess(flaky_spawn)
    warm.prewarm()
    main_socket, child = warm.take()
    main_socket.close()
    assert child is spawned[0]
    assert len(attempts) == 2
