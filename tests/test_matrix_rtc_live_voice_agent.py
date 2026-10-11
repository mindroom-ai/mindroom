"""GPT-Live delegation preserves caller context and owns background work."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.voice.events import ConversationItemAddedEvent
from livekit.plugins.openai.realtime import GPTLiveDelegation, GPTLiveSession
from structlog.testing import capture_logs

from mindroom.matrix_rtc.call_telemetry import LiveCallTelemetry
from mindroom.matrix_rtc.call_tools import CallAgentResponse
from mindroom.matrix_rtc.live_voice_agent import _LiveDelegationRunner, _record_assistant_reply
from mindroom.matrix_rtc.voice_agent import LiveVoiceAgentOptions

if TYPE_CHECKING:
    from collections.abc import Callable


class CommentarySink:
    """Capture the provider-bound commentary without opening a connection."""

    def __init__(self) -> None:
        self.results: list[tuple[str, str | None]] = []

    def append_commentary(self, text: str, *, delegation_id: str | None = None) -> None:
        """Record one outgoing provider message."""
        self.results.append((text, delegation_id))


def _options(respond: object, **kwargs: object) -> LiveVoiceAgentOptions:
    return LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate tasks to the backend."),
        model="gpt-live-1",
        api_key="test-key",
        voice="marin",
        respond=respond,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_live_delegations_capture_new_context_and_pending_speech_once() -> None:
    """Finalizing a previously pending utterance cannot repeat it next turn."""
    respond = AsyncMock(return_value=CallAgentResponse("Sunny."))
    sink = CommentarySink()
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", sink), LiveCallTelemetry())
    context = ChatContext.empty()
    context.add_message(role="user", content="I am in Paris.")
    runner.submit(GPTLiveDelegation("one", "What is the weather?"), context)
    # The SDK finalizes this after emitting delegation_created.
    context.add_message(role="user", content="What is the weather?")
    await asyncio.gather(*runner._tasks)
    context.add_message(role="assistant", content="It is sunny.")
    runner.submit(GPTLiveDelegation("two", "And tomorrow?"), context)
    runner.submit(GPTLiveDelegation("two", "And tomorrow?"), context)
    await asyncio.gather(*runner._tasks)

    assert respond.await_count == 2
    first, second = [call.args[0] for call in respond.await_args_list]
    assert first.count("What is the weather?") == 1
    assert "I am in Paris." in first
    assert "What is the weather?" not in second
    assert "I am in Paris." not in second
    assert "It is sunny." in second
    assert "And tomorrow?" in second
    assert sink.results == [("Sunny.", "one"), ("Sunny.", "two")]
    await runner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True], ids=["finalized", "pending"])
@pytest.mark.parametrize(
    "first_result",
    [CallAgentResponse("", tool_names=("send_message",)), RuntimeError("failed after an action")],
    ids=["empty-result", "partial-failure"],
)
async def test_live_dispatched_request_cannot_replay_an_action(
    first_result: CallAgentResponse | Exception,
    pending: bool,
) -> None:
    """An attempted request may have side effects even without a response text."""
    respond = AsyncMock(side_effect=[first_result, CallAgentResponse("Done.")])
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", CommentarySink()), LiveCallTelemetry())
    context = ChatContext.empty()
    if not pending:
        context.add_message(role="user", content="Send the update.")
    runner.submit(GPTLiveDelegation("one", "Send the update." if pending else ""), context)
    if pending:
        context.add_message(role="user", content="Send the update.")
    context.add_message(role="user", content="What is the weather?")
    runner.submit(GPTLiveDelegation("two", ""), context)
    await asyncio.gather(*runner._tasks)

    assert "Send the update." in respond.await_args_list[0].args[0]
    assert "Send the update." not in respond.await_args_list[1].args[0]
    assert "What is the weather?" in respond.await_args_list[1].args[0]
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_empty_result_still_answers_without_claiming_completion() -> None:
    """An empty result also represents denied work, so it cannot claim success."""
    sink = CommentarySink()
    runner = _LiveDelegationRunner(
        _options(AsyncMock(return_value=CallAgentResponse(""))),
        cast("GPTLiveSession", sink),
        LiveCallTelemetry(),
    )
    runner.submit(GPTLiveDelegation("one", "Perform a task."), ChatContext.empty())
    await asyncio.gather(*runner._tasks)

    assert sink.results == [("No response is available from the agent for this request.", "one")]
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_serializes_responder_admission_and_cancels_queued_work() -> None:
    """Only the active delegation enters the caller authorization boundary."""
    started = asyncio.Event()
    cancelled = asyncio.Event()
    requests: list[str] = []

    async def respond(text: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        requests.append(text)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return CallAgentResponse("unreachable")

    sink = CommentarySink()
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", sink), LiveCallTelemetry())
    runner.submit(GPTLiveDelegation("one", "First task"), ChatContext.empty())
    await started.wait()
    runner.submit(GPTLiveDelegation("two", "Second task"), ChatContext.empty())
    await runner.aclose()
    runner.submit(GPTLiveDelegation("three", "Late task"), ChatContext.empty())

    assert cancelled.is_set()
    assert len(requests) == 1
    assert not runner._tasks
    assert sink.results == []


@pytest.mark.asyncio
async def test_live_reconnect_discards_results_from_old_connection() -> None:
    """Even a responder swallowing cancellation cannot send an obsolete ID."""
    started = asyncio.Event()

    async def respond(_text: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return CallAgentResponse("stale result")
        return CallAgentResponse("unreachable")

    sink = CommentarySink()
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", sink), LiveCallTelemetry())
    runner.submit(GPTLiveDelegation("old", "Check status"), ChatContext.empty())
    await started.wait()
    runner.on_reconnected()
    await asyncio.gather(*runner._tasks)

    assert sink.results == []
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_pending_speech_growth_only_delegates_new_words() -> None:
    """Multiple delegations during one unfinished utterance share its prefix."""
    respond = AsyncMock(return_value=CallAgentResponse("Done."))
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", CommentarySink()), LiveCallTelemetry())
    runner.submit(GPTLiveDelegation("one", "Check weather"), ChatContext.empty())
    runner.submit(GPTLiveDelegation("two", "Check weather and trains"), ChatContext.empty())
    await asyncio.gather(*runner._tasks)
    assert "Check weather" in respond.await_args_list[0].args[0]
    assert "Check weather" not in respond.await_args_list[1].args[0]
    assert "and trains" in respond.await_args_list[1].args[0]
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_reconnect_retains_context_from_cancelled_queue() -> None:
    """A snapshot queued behind a cancelled request has never reached history."""
    started = asyncio.Event()

    async def wait_for_cancel(_text: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        started.set()
        await asyncio.Event().wait()
        return CallAgentResponse("unreachable")

    respond = AsyncMock(side_effect=wait_for_cancel)
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", CommentarySink()), LiveCallTelemetry())
    runner.submit(GPTLiveDelegation("one", "First"), ChatContext.empty())
    await started.wait()
    context = ChatContext.empty()
    context.add_message(role="user", content="Also check train times.")
    runner.submit(GPTLiveDelegation("queued", ""), context)
    runner.on_reconnected()
    await asyncio.gather(*runner._tasks, return_exceptions=True)
    respond.side_effect = None
    respond.return_value = CallAgentResponse("Done.")
    runner.submit(GPTLiveDelegation("new", "Please continue."), context)
    await asyncio.gather(*runner._tasks)
    assert "Also check train times." in respond.await_args.args[0]
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_splits_large_unicode_results_without_losing_content() -> None:
    """Provider commentary stays below the per-append token cap."""
    result = "Status: 晴れです。 " * 200
    sink = CommentarySink()
    respond = AsyncMock(return_value=CallAgentResponse(result))
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", sink), LiveCallTelemetry())
    runner.submit(GPTLiveDelegation("one", "Give details"), ChatContext.empty())
    await asyncio.gather(*runner._tasks)

    assert "".join(text for text, _ in sink.results) == result
    assert all(len(text.encode("utf-8")) <= 480 for text, _ in sink.results)
    assert all(identifier == "one" for _, identifier in sink.results)
    await runner.aclose()


@pytest.mark.asyncio
async def test_live_delegation_failure_returns_safe_notice_and_next_turn_works() -> None:
    """A provider exception neither leaks its payload nor poisons the queue."""
    respond = AsyncMock(side_effect=[ValueError("private-token"), CallAgentResponse("Done.")])
    notices: list[str] = []
    sink = CommentarySink()
    runner = _LiveDelegationRunner(
        _options(respond, on_session_error=notices.append),
        cast("GPTLiveSession", sink),
        LiveCallTelemetry(),
    )
    with capture_logs() as logs:
        runner.submit(GPTLiveDelegation("one", "First"), ChatContext.empty())
        runner.submit(GPTLiveDelegation("two", "Second"), ChatContext.empty())
        await asyncio.gather(*runner._tasks)

    assert len(notices) == 1
    assert "private-token" not in str(notices) + str(sink.results) + str(logs)
    failure = next(entry for entry in logs if entry["event"] == "call_live_delegation_failed")
    assert failure["error_type"] == "ValueError"
    assert any("live_voice_agent.py:" in frame for frame in failure["traceback_frames"])
    assert sink.results[-1] == ("Done.", "two")
    await runner.aclose()


def _stages(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "call_delegation_stage"]


@pytest.mark.asyncio
async def test_live_delegation_times_each_stage_and_matches_acks_by_client_event_id() -> None:
    """Queued work reports its lock wait, and each acknowledgement finds its chunk by sent event ID."""
    release = asyncio.Event()

    async def respond(_text: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        await release.wait()
        return CallAgentResponse("x" * 700)

    sink = CommentarySink()
    telemetry = LiveCallTelemetry()
    runner = _LiveDelegationRunner(_options(respond), cast("GPTLiveSession", sink), telemetry)
    with capture_logs() as logs:
        runner.submit(GPTLiveDelegation("one", "First"), ChatContext.empty())
        runner.submit(GPTLiveDelegation("two", "Second"), ChatContext.empty())
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(*runner._tasks)
        # The SDK sends commentary in call order; a greeting ask carries no delegation.
        telemetry.on_client_event({"type": "session.commentary.append", "event_id": "greet", "delegation_id": None})
        for index, (_text, delegation_id) in enumerate(sink.results):
            telemetry.on_client_event(
                {"type": "session.commentary.append", "event_id": f"append_{index}", "delegation_id": delegation_id},
            )
        for client_event_id in ("append_1", "greet", "append_0"):
            telemetry.on_server_event({"type": "session.commentary.appended", "client_event_id": client_event_id})
    await runner.aclose()

    stages = _stages(logs)
    assert [(entry["delegation_id"], entry["stage"]) for entry in stages] == [
        ("one", "created"),
        ("one", "enqueued"),
        ("two", "created"),
        ("two", "enqueued"),
        ("one", "lock_acquired"),
        ("one", "response_done"),
        ("one", "commentary_appended"),
        ("one", "commentary_appended"),
        ("two", "lock_acquired"),
        ("two", "response_done"),
        ("two", "commentary_appended"),
        ("two", "commentary_appended"),
        ("one", "commentary_acked"),
        ("one", "commentary_acked"),
    ]
    lock_waits = {
        entry["delegation_id"]: entry["queue_wait_ms"] for entry in stages if entry["stage"] == "lock_acquired"
    }
    assert lock_waits["one"] < 10 <= lock_waits["two"]
    assert {entry["response_chars"] for entry in stages if entry["stage"] == "response_done"} == {700}
    appended = [
        (entry["chunk_index"], entry["chunk_count"]) for entry in stages if entry["stage"] == "commentary_appended"
    ]
    assert appended == [(0, 2), (1, 2), (0, 2), (1, 2)]
    acked = [(entry["chunk_index"], entry["correlation"]) for entry in stages if entry["stage"] == "commentary_acked"]
    assert acked == [(1, "client_event_id"), (0, "client_event_id")]
    for delegation_id in ("one", "two"):
        elapsed = [entry["elapsed_ms"] for entry in stages if entry["delegation_id"] == delegation_id]
        assert elapsed == sorted(elapsed)


@pytest.mark.asyncio
async def test_live_delegation_acks_without_client_event_ids_follow_send_order() -> None:
    """Without IDs, acknowledgements match the oldest send, including commentary MindRoom did not delegate."""
    sink = CommentarySink()
    telemetry = LiveCallTelemetry()
    runner = _LiveDelegationRunner(
        _options(AsyncMock(side_effect=RuntimeError("delegate failed"))),
        cast("GPTLiveSession", sink),
        telemetry,
    )
    with capture_logs() as logs:
        runner.submit(GPTLiveDelegation("one", "First"), ChatContext.empty())
        await asyncio.gather(*runner._tasks)
        telemetry.on_client_event({"type": "session.commentary.append", "event_id": "greet", "delegation_id": None})
        telemetry.on_client_event({"type": "session.commentary.append", "event_id": "error", "delegation_id": "one"})
        telemetry.on_server_event({"type": "session.commentary.appended"})
        telemetry.on_server_event({"type": "session.commentary.appended"})
        telemetry.on_server_event({"type": "session.commentary.appended"})
    await runner.aclose()

    stages = _stages(logs)
    assert [entry["stage"] for entry in stages] == [
        "created",
        "enqueued",
        "lock_acquired",
        "commentary_appended",
        "commentary_acked",
    ]
    assert (stages[3]["chunk_index"], stages[3]["chunk_count"]) == (0, 1)
    assert stages[4]["correlation"] == "order"
    failure = next(entry for entry in logs if entry["event"] == "call_live_delegation_failed")
    assert failure["delegation_id"] == "one"


def test_only_spoken_assistant_messages_reach_reply_routing() -> None:
    """Caller turns and empty assistant items are not replies."""
    telemetry = LiveCallTelemetry()
    with capture_logs() as logs:
        for role, content in (("user", "Hello"), ("assistant", ""), ("assistant", "Hi there")):
            item = ChatMessage(role=role, content=[content], interrupted=role == "assistant")
            _record_assistant_reply(telemetry, ConversationItemAddedEvent(item=item))

    [reply] = [entry for entry in logs if entry["event"] == "call_assistant_reply"]
    assert (reply["reply_chars"], reply["delegated"], reply["interrupted"]) == (8, False, True)
