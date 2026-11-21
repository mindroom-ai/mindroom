"""Exercise GPT-Live through the installed SDK with in-memory provider transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from livekit import rtc
from livekit.plugins.openai.realtime import GPTLiveSession
from structlog.testing import capture_logs

from mindroom.matrix_rtc import live_voice_agent
from mindroom.matrix_rtc.live_voice_agent import LiveVoiceBridge
from mindroom.matrix_rtc.voice_agent import (
    CallAgentResponse,
    LiveVoiceAgentOptions,
    LiveVoiceUsage,
    RealtimeVoiceBridge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from livekit.agents import Agent, AgentSession
    from livekit.agents.llm import InputTranscriptionCompleted


class _ProviderSocket:
    """Model the server handshake and capture actual serialized client commands."""

    def __init__(self, session_id: str, *, acknowledge_start: bool = True) -> None:
        self.session_id = session_id
        self.acknowledge_start = acknowledge_start
        self.incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.closed_event = asyncio.Event()
        self.final_usage_seconds = 0.0

    def feed(self, event: dict[str, Any]) -> None:
        """Deliver a raw provider event through the SDK's receive loop."""
        self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(event), ""))

    def content_close(self, *, send_close_frame: bool = True) -> None:
        """Send the provider's close event and, by default, the graceful WebSocket close frame."""
        self.feed({"type": "session.closed", "reason": "content", "usage": {"seconds": 3}})
        if send_close_frame:
            self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, ""))

    async def receive(self) -> aiohttp.WSMessage:
        """Wait until the test or handshake produces another server event."""
        return await self.incoming.get()

    async def send_str(self, data: str) -> None:
        """Accept a serialized SDK command and answer lifecycle commands."""
        event = json.loads(data)
        self.sent.append(event)
        self.outgoing.put_nowait(event)
        if event["type"] == "session.start" and self.acknowledge_start:
            self.feed({"type": "session.started", "session": {"id": self.session_id}})
        elif event["type"] == "session.close":
            self.feed(
                {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": self.final_usage_seconds}},
            )

    async def close(self) -> None:
        """Record that the SDK closed its socket."""
        self.closed = True
        self.closed_event.set()

    async def next_event(self, event_type: str) -> dict[str, Any]:
        """Wait briefly for one named command, ignoring unrelated commands."""
        async with asyncio.timeout(2):
            while True:
                event = await self.outgoing.get()
                if event["type"] == event_type:
                    return event


class _ProviderHTTP:
    """Replace the external HTTP client while preserving SDK client ownership."""

    def __init__(self) -> None:
        self.socket = _ProviderSocket("session-test-1")
        self.sockets = [self.socket]
        self.acknowledge_reconnect = True
        self.connected_again = asyncio.Event()
        self.closed = False
        self.connection: tuple[str, dict[str, str]] | None = None

    async def ws_connect(self, *, url: str, headers: dict[str, str]) -> _ProviderSocket:
        """Capture the endpoint and credentials selected by the real model."""
        if self.connection is not None:
            self.sockets.append(
                _ProviderSocket(f"session-test-{len(self.sockets) + 1}", acknowledge_start=self.acknowledge_reconnect),
            )
            self.connected_again.set()
        self.connection = (url, headers)
        return self.sockets[-1]

    async def close(self) -> None:
        """Record release of the model-owned HTTP client."""
        self.closed = True


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _ProviderHTTP:
    """Keep model/session logic real and replace external transport only."""
    client = _ProviderHTTP()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: client)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other-provider.example.org/v1")

    async def start_headless(
        bridge: RealtimeVoiceBridge,
        session: AgentSession,
        agent: Agent,
        options: LiveVoiceAgentOptions,
        **_kwargs: object,
    ) -> None:
        # The shared media bridge has separate room/roster tests. Run the actual
        # AgentSession here without an SFU or audio hardware.
        bridge._session = session
        bridge._register_session_listeners(session, options)
        await session.start(agent)

    monkeypatch.setattr(RealtimeVoiceBridge, "_start_session", start_headless)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_close", [False, True])
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_live_sdk_retains_cumulative_voice_usage_through_close(
    provider: _ProviderHTTP,
    remote_close: bool,
    cancel_close: bool,
) -> None:
    """Repeated totals cannot inflate usage, and teardown must persist the provider's final seconds."""
    updates: list[LiveVoiceUsage] = []
    recorded = asyncio.Event()
    final_write_started = asyncio.Event()
    release_final_write = asyncio.Event()
    terminated: list[bool] = []

    async def record_usage(usage: LiveVoiceUsage) -> None:
        if usage.finalized:
            final_write_started.set()
            await release_final_write.wait()
        updates.append(usage)
        recorded.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=AsyncMock(return_value=CallAgentResponse("Done.")),
        record_usage=record_usage,
        on_session_terminated=terminated.append,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        for seconds in (10**400, 12.0, 15.5, 15.5, 13.0, 18.0):
            provider.socket.feed({"type": "session.usage.updated", "usage": {"seconds": seconds}})
        await asyncio.wait_for(recorded.wait(), 2)
        provider.socket.final_usage_seconds = 25.5
        if remote_close:
            provider.socket.feed({"type": "session.closed", "reason": "expired", "usage": {"seconds": 25.5}})
            await asyncio.wait_for(provider.socket.closed_event.wait(), 2)
    finally:
        closing = asyncio.create_task(bridge.aclose())
        await asyncio.sleep(0)
        await asyncio.wait_for(final_write_started.wait(), 2)
        assert not closing.done()
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
        release_final_write.set()
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 3)
        else:
            await asyncio.wait_for(closing, 3)

    assert updates
    assert updates[-1].duration_seconds == 25.5
    assert updates[-1].finalized is True
    assert {item.provider_session_id for item in updates} == {"session-test-1"}
    assert {item.model for item in updates} == {"gpt-live-1"}
    assert len({item.created_at for item in updates}) == 1
    assert [item.duration_seconds for item in updates] == sorted(item.duration_seconds for item in updates)
    assert terminated == ([True] if remote_close else [])


@pytest.mark.asyncio
async def test_live_sdk_routes_delegation_and_closes_owned_work(provider: _ProviderHTTP) -> None:
    """Wrong delegation mode or listener wiring prevents a real provider-bound reply."""
    prompts: list[str] = []
    transcript: list[tuple[str, str]] = []
    order: list[str] = []
    second_started = asyncio.Event()

    async def respond(prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        prompts.append(prompt)
        if len(prompts) == 1:
            return CallAgentResponse("The task is complete.")
        second_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("delegate_cancelled")
        return CallAgentResponse("unreachable")

    async def close_responder() -> None:
        order.append("responder_closed")

    system_prompt = "You are Helper. The caller is Alice.\n" + "Preserve every context section.\n" * 1200
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value=system_prompt),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="vesper",
        respond=respond,
        close_responder=close_responder,
        on_conversation_turn=lambda speaker, text, _spoken_at: transcript.append((speaker, text)),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    room = SimpleNamespace(disconnect=AsyncMock())
    bridge._room = room
    try:
        await bridge.start_agent(options)
        start = await provider.socket.next_event("session.start")
        assert start["session"]["model"] == "gpt-live-1"
        assert start["session"]["audio"]["output"]["voice"] == "vesper"
        assert start["session"]["instructions"] == system_prompt
        assert start["session"]["delegation"] == {"type": "client"}
        assert provider.connection is not None
        url, headers = provider.connection
        assert url == "wss://api.openai.com/v1/live/sessions"
        assert headers["Authorization"] == "Bearer test-api-key"

        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Check status.", "start_ms": 0, "end_ms": 100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "first", "target": "client"}})
        reply = await provider.socket.next_event("session.commentary.append")
        assert reply["delegation_id"] == "first"
        assert reply["content"] == "The task is complete."
        assert "Check status." in prompts[0]

        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Check again.", "start_ms": 2000, "end_ms": 2100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "second", "target": "client"}})
        await asyncio.wait_for(second_started.wait(), timeout=2)
        assert ("user", "Check status.") in transcript
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)

    assert order == ["delegate_cancelled", "responder_closed"]
    assert provider.socket.closed
    assert provider.closed
    room.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize_retry", [False, True], ids=["pending-retry", "finalized-retry"])
async def test_live_sdk_identical_spoken_retry_survives_delegate_failure(
    provider: _ProviderHTTP,
    finalize_retry: bool,
) -> None:
    """A new utterance may repeat a failed request without losing its words."""
    prompts: list[str] = []
    transcript: list[tuple[str, str]] = []
    retry_transcribed = asyncio.Event()

    async def respond(prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        prompts.append(prompt)
        if len(prompts) == 1:
            message = "delegated request failed"
            raise RuntimeError(message)
        return CallAgentResponse("Done.")

    def on_transcribed(event: InputTranscriptionCompleted) -> None:
        if not event.is_final:
            retry_transcribed.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=respond,
        on_conversation_turn=lambda speaker, text, _spoken_at: transcript.append((speaker, text)),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Send the update.", "start_ms": 0, "end_ms": 100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "first", "target": "client"}})
        failure = await provider.socket.next_event("session.commentary.append")
        assert failure["delegation_id"] == "first"
        assert "could not complete" in failure["content"]

        agent = cast("Agent", bridge._live_agent)
        session = agent.duplex_session
        assert isinstance(session, GPTLiveSession)
        session.on("input_audio_transcription_completed", on_transcribed)
        # The gap makes the SDK finalize the original before starting the
        # identical retry. Both its transcript and delegation wiring stay real.
        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Send the update.", "start_ms": 2000, "end_ms": 2100},
        )
        await asyncio.wait_for(retry_transcribed.wait(), timeout=2)
        if finalize_retry:
            session.push_audio(
                rtc.AudioFrame(data=bytes(48000), sample_rate=24000, num_channels=1, samples_per_channel=24000),
            )
        assert transcript.count(("user", "Send the update.")) == 1 + int(finalize_retry)
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "retry", "target": "client"}})
        reply = await provider.socket.next_event("session.commentary.append")

        assert reply["delegation_id"] == "retry"
        assert reply["content"] == "Done."
        assert len(prompts) == 2
        assert all(prompt.count("Send the update.") == 1 for prompt in prompts)
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["failed", "cancelled"])
async def test_live_prompt_preparation_failure_closes_responder_before_provider_start(
    provider: _ProviderHTTP,
    cancelled: bool,
) -> None:
    """Preparing the full prompt must not leak the cached agent on failed call startup."""
    error = asyncio.CancelledError if cancelled else RuntimeError
    close_responder = AsyncMock()
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(side_effect=error("prompt preparation stopped")),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=AsyncMock(),
        close_responder=close_responder,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        with pytest.raises(error, match="prompt preparation stopped"):
            await bridge.start_agent(options)
    finally:
        await bridge.aclose()
    close_responder.assert_awaited_once()
    assert provider.connection is None


@pytest.mark.asyncio
async def test_live_sdk_configuration_failure_releases_owned_resources(
    provider: _ProviderHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected SDK startup must not retain its HTTP client or cached responder."""

    async def reject_configuration(_session: GPTLiveSession, _instructions: str) -> None:
        message = "provider configuration rejected"
        raise RuntimeError(message)

    monkeypatch.setattr(GPTLiveSession, "_update_instructions", reject_configuration)
    close_responder = AsyncMock()
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=AsyncMock(),
        close_responder=close_responder,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    room = SimpleNamespace(disconnect=AsyncMock())
    bridge._room = room
    try:
        with pytest.raises(RuntimeError, match="provider configuration rejected"):
            await bridge.start_agent(options)
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)

    assert provider.socket.closed
    assert provider.closed
    close_responder.assert_awaited_once()
    room.disconnect.assert_awaited_once()
    assert bridge._session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("service_closed", [False, True], ids=["socket-loss", "session-expired"])
async def test_live_sdk_disconnect_returns_control_without_reusing_delegations(
    provider: _ProviderHTTP,
    service_closed: bool,
) -> None:
    """Reconnect must belong to the call manager, never reuse old provider delegation IDs."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    terminated = asyncio.Event()
    terminal_retries: list[bool] = []
    usage_updates: list[LiveVoiceUsage] = []
    usage_recorded = asyncio.Event()

    async def record_usage(usage: LiveVoiceUsage) -> None:
        usage_updates.append(usage)
        usage_recorded.set()

    async def respond(_prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        started.set()
        # A backend that finishes despite cancellation still cannot deliver
        # a result tagged with an obsolete provider delegation ID.
        with contextlib.suppress(asyncio.CancelledError):
            await release.wait()
        finished.set()
        return CallAgentResponse("Late result from the disconnected session.")

    def on_terminated(retryable: bool) -> None:
        terminal_retries.append(retryable)
        terminated.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=respond,
        on_session_terminated=on_terminated,
        record_usage=record_usage,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    observers: list[asyncio.Task[bool]] = []
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        provider.socket.feed({"type": "session.usage.updated", "usage": {"seconds": 12.5}})
        await asyncio.wait_for(usage_recorded.wait(), 2)
        provider.socket.feed({"type": "session.input_transcript.delta", "delta": "Check status."})
        provider.socket.feed(
            {"type": "session.delegation.created", "delegation": {"id": "obsolete", "target": "client"}},
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        if service_closed:
            provider.socket.feed({"type": "session.closed", "reason": "expired", "usage": {"seconds": 17.5}})
        provider.socket.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, ""))
        await asyncio.wait_for(provider.socket.closed_event.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)

        observers = [asyncio.create_task(terminated.wait()), asyncio.create_task(provider.connected_again.wait())]
        await asyncio.wait(observers, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert terminated.is_set(), "SDK must report terminal disconnect before opening another provider session"
        assert terminal_retries == [True]
        assert len(provider.sockets) == 1
        assert not any(event.get("delegation_id") == "obsolete" for socket in provider.sockets for event in socket.sent)
    finally:
        release.set()
        for observer in observers:
            observer.cancel()
        await asyncio.gather(*observers, return_exceptions=True)
        await asyncio.wait_for(bridge.aclose(), timeout=3)

    assert (usage_updates[-1].duration_seconds, usage_updates[-1].finalized) == (
        17.5 if service_closed else 12.5,
        service_closed,
    )


@pytest.mark.asyncio
async def test_live_sdk_recovers_content_close_with_history_and_drops_old_delegation(provider: _ProviderHTTP) -> None:
    """Keep the call and history, but invalidate delegation work before the SDK handles the close."""
    delegates: asyncio.Queue[asyncio.Task[None]] = asyncio.Queue()
    cancelled = asyncio.Event()
    cancelled_at_close: list[bool] = []
    terminated: list[bool] = []

    async def respond(_prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        if cancelled.is_set():
            return CallAgentResponse("The new request is complete.")
        task = asyncio.current_task()
        assert task is not None
        delegates.put_nowait(task)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        cancelled.set()
        return CallAgentResponse("Obsolete commentary.")

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=respond,
        greeting_instructions="Say hello once.",
        on_session_terminated=terminated.append,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        agent_session = bridge._session
        provider_session = cast("Agent", bridge._live_agent).duplex_session
        greeting = await provider.socket.next_event("session.commentary.append")
        assert "Say hello once." in greeting["content"]
        provider.socket.feed({"type": "session.input_transcript.delta", "delta": "Is the blue kite ready?"})
        provider.socket.feed({"type": "session.output_transcript.delta", "delta": "I can check."})
        delegation = {"type": "session.delegation.created", "delegation": {"id": "check-kite", "target": "client"}}
        provider.socket.feed(delegation)
        pending = await asyncio.wait_for(delegates.get(), 2)

        def on_server_event(event: dict[str, Any]) -> None:
            if event.get("reason") == "content":
                cancelled_at_close.append(pending.cancelling() > 0)

        provider_session.on("openai_server_event_received", on_server_event)
        provider.socket.content_close()
        await asyncio.wait_for(provider.socket.closed_event.wait(), 2)
        assert terminated == []
        await asyncio.wait_for(provider.connected_again.wait(), 2)
        second = provider.sockets[1]
        start = await second.next_event("session.start")
        assert bridge._session is agent_session
        assert start["session"]["input"] == [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Is the blue kite ready?"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I can check."}]},
        ]
        assert cancelled_at_close == [True]
        await asyncio.wait_for(cancelled.wait(), 2)

        # A fresh delegation can reuse the ID, but neither old commentary nor a second greeting may be queued.
        second.feed(delegation)
        reply = await second.next_event("session.commentary.append")
        assert reply["delegation_id"] == "check-kite"
        assert reply["content"] == "The new request is complete."
        assert terminated == []
    finally:
        await asyncio.wait_for(bridge.aclose(), 3)

    assert [event for event in second.sent if event["type"] == "session.commentary.append"] == [reply]


@pytest.mark.asyncio
async def test_live_sdk_ends_content_close_when_commentary_is_still_queued(
    provider: _ProviderHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked first socket write must not leave the rest of the old reply on another socket."""
    attempted: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    release_write = asyncio.Event()
    send = provider.socket.send_str

    async def blocked_send(data: str) -> None:
        event = json.loads(data)
        if event["type"] == "session.commentary.append":
            attempted.put_nowait(event)
            await release_write.wait()
        await send(data)

    monkeypatch.setattr(provider.socket, "send_str", blocked_send)
    terminated: list[bool] = []
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=AsyncMock(return_value=CallAgentResponse("x" * 480 + "second chunk")),
        on_session_terminated=terminated.append,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    with capture_logs() as logs:
        try:
            await bridge.start_agent(options)
            await provider.socket.next_event("session.start")
            provider.socket.feed(
                {"type": "session.delegation.created", "delegation": {"id": "old", "target": "client"}},
            )
            first = await asyncio.wait_for(attempted.get(), 2)
            assert first["content"] == "x" * 480
            provider.socket.content_close()
            await asyncio.wait_for(provider.socket.closed_event.wait(), 2)
            assert terminated == [True]
        finally:
            release_write.set()
            await asyncio.wait_for(bridge.aclose(), 3)

    assert len(provider.sockets) == 1
    (close,) = [entry for entry in logs if entry["event"] == "call_live_session_closed"]
    assert (close["reason"], close["action"]) == ("content", "ending")


@pytest.mark.asyncio
@pytest.mark.parametrize("send_close_frame", [False, True], ids=["close-frame-stalls", "session-start-stalls"])
async def test_live_sdk_ends_stalled_content_close_recovery(
    provider: _ProviderHTTP,
    monkeypatch: pytest.MonkeyPatch,
    send_close_frame: bool,
) -> None:
    """A graceful close without a successful replacement handshake cannot strand the call."""
    monkeypatch.setattr(live_voice_agent, "_CONTENT_CLOSE_RECOVERY_TIMEOUT_SECONDS", 0.1)
    provider.acknowledge_reconnect = False
    respond = AsyncMock(return_value=CallAgentResponse("Do not send this."))
    terminated: list[bool] = []
    did_terminate = asyncio.Event()

    def on_terminated(retryable: bool) -> None:
        terminated.append(retryable)
        did_terminate.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=respond,
        on_session_terminated=on_terminated,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        provider.socket.content_close(send_close_frame=send_close_frame)
        if not send_close_frame:
            provider.socket.feed(
                {"type": "session.delegation.created", "delegation": {"id": "late", "target": "client"}},
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(did_terminate.wait(), 1)
        assert terminated == [True]
    finally:
        await asyncio.wait_for(bridge.aclose(), 3)

    assert len(provider.sockets) == (2 if send_close_frame else 1)
    assert all(socket.closed for socket in provider.sockets)
    respond.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [299, 301], ids=["within-window", "outside-window"])
async def test_live_sdk_caps_repeated_content_closes(
    provider: _ProviderHTTP,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: int,
) -> None:
    """A second moderation close ends the call only inside the recovery window."""
    now = 0.0
    monkeypatch.setattr(live_voice_agent, "time", SimpleNamespace(time=time.time, monotonic=lambda: now))
    terminated: list[bool] = []
    notices: list[str] = []
    usages: list[LiveVoiceUsage] = []
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=AsyncMock(),
        on_session_error=notices.append,
        on_session_terminated=terminated.append,
        record_usage=AsyncMock(side_effect=usages.append),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    with capture_logs() as logs:
        try:
            await bridge.start_agent(options)
            await provider.socket.next_event("session.start")
            provider.socket.content_close()
            await asyncio.wait_for(provider.socket.closed_event.wait(), 2)
            assert terminated == []
            await asyncio.wait_for(provider.connected_again.wait(), 2)
            second = provider.sockets[1]
            await second.next_event("session.start")
            provider.connected_again.clear()

            now += elapsed
            second.content_close()
            await asyncio.wait_for(second.closed_event.wait(), 2)
            if elapsed < 300:
                assert terminated == [False]
                assert len(provider.sockets) == 2
                (notice,) = notices
                assert "moderation closed the voice session again" in notice
                assert "call ended" in notice
                assert "rejoin to start a new call" in notice
            else:
                await asyncio.wait_for(provider.connected_again.wait(), 2)
                await provider.sockets[2].next_event("session.start")
                assert terminated == []
                assert notices == []
        finally:
            await asyncio.wait_for(bridge.aclose(), 3)

    closes = [(entry["reason"], entry["action"]) for entry in logs if entry["event"] == "call_live_session_closed"]
    usage_totals = [(usage.provider_session_id, usage.duration_seconds, usage.finalized) for usage in usages]
    previous_sessions = [("session-test-1", 3.0, True), ("session-test-2", 3.0, True)]
    if elapsed < 300:
        assert closes == [("content", "recovering"), ("content", "ending")]
        assert usage_totals == previous_sessions
    else:
        assert closes == [("content", "recovering"), ("content", "recovering"), ("close_requested", "ending")]
        assert usage_totals == [*previous_sessions, ("session-test-3", 0.0, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["code", "type"])
async def test_live_sdk_reports_content_filter_once_and_preserves_other_notices(
    provider: _ProviderHTTP,
    field: str,
) -> None:
    """Only the wrapped moderation error is suppressed; the next provider error still gets a notice."""
    notices: list[str] = []
    error_seen = asyncio.Event()
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=AsyncMock(),
        on_session_error=notices.append,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        session = bridge._session
        assert session is not None
        session.on("error", lambda _event: error_seen.set())
        provider.socket.feed({"type": "error", "error": {field: "content_filter", "message": "Example payload."}})
        await asyncio.wait_for(error_seen.wait(), 2)
        (notice,) = notices
        assert "moderation stopped that reply" in notice
        assert "call continues" not in notice
        assert "rephrasing" in notice
        assert "Example payload." not in notice

        error_seen.clear()
        provider.socket.feed({"type": "error", "error": {"code": "invalid_event", "message": "Example payload."}})
        await asyncio.wait_for(error_seen.wait(), 2)
        assert len(notices) == 2
        assert "the OpenAI Realtime voice model failed" in notices[1]
    finally:
        await asyncio.wait_for(bridge.aclose(), 3)


@pytest.mark.asyncio
async def test_live_sdk_times_delegation_stages_through_the_provider_ack(provider: _ProviderHTTP) -> None:
    """Real SDK client and server events carry one delegation from creation to its commentary acknowledgement."""
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=AsyncMock(return_value=CallAgentResponse("Done.")),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    ack = "session.commentary.appended"
    acked = asyncio.Event()
    with capture_logs() as logs:
        try:
            await bridge.start_agent(options)
            await provider.socket.next_event("session.start")
            provider.socket.feed(
                {"type": "session.delegation.created", "delegation": {"id": "first", "target": "client"}},
            )
            append = await provider.socket.next_event("session.commentary.append")
            session = cast("Agent", bridge._live_agent).duplex_session
            # `emit` runs every listener before this waiter can resume, so MindRoom's listener has handled the acknowledgement.
            session.on("openai_server_event_received", lambda event: acked.set() if event["type"] == ack else None)
            provider.socket.feed({"type": ack, "client_event_id": append["event_id"]})
            await asyncio.wait_for(acked.wait(), timeout=2)
        finally:
            await asyncio.wait_for(bridge.aclose(), timeout=3)

    stages = [entry for entry in logs if entry["event"] == "call_delegation_stage"]
    assert [(entry["delegation_id"], entry["stage"]) for entry in stages] == [
        ("first", "created"),
        ("first", "lock_acquired"),
        ("first", "response_done"),
        ("first", "commentary_appended"),
        ("first", "commentary_acked"),
    ]
