"""Direct unit tests for the streaming state machine in mindroom.streaming.

These tests drive send_streaming_response with a scripted chunk stream and a
fake Matrix seam (patched send/edit results), asserting the exact ordered
sequence of send and edit calls the state machine produces.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import patch

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import streaming as streaming_mod
from mindroom.cancellation import USER_STOP_CANCEL_MSG
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.streaming import (
    _CANCELLED_RESPONSE_NOTE,
    _PROGRESS_PLACEHOLDER,
    StreamingDeliveryError,
    StreamingLifecycleSuspensionError,
    StreamingResponse,
    _CommittedDeliveryState,
    send_streaming_response,
)
from mindroom.timing import DispatchPipelineTiming
from mindroom.tool_system.events import _TOOL_TRACE_KEY, StructuredStreamChunk, ToolTraceEntry
from tests.conftest import (
    bind_runtime_paths,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from mindroom.final_delivery import StreamTransportOutcome


@dataclass(frozen=True)
class _GatewayOp:
    """One recorded send or edit that reached the fake Matrix seam."""

    kind: Literal["send", "edit"]
    content: dict[str, Any]
    display_text: str


class _FakeGateway:
    """Record the ordered send/edit calls produced by the streaming machine."""

    def __init__(self) -> None:
        self.ops: list[_GatewayOp] = []
        self._op_recorded = asyncio.Event()

    def _record(self, op: _GatewayOp) -> None:
        self.ops.append(op)
        self._op_recorded.set()

    async def send(
        self,
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG002
    ) -> DeliveredMatrixEvent:
        self._record(_GatewayOp(kind="send", content=dict(content), display_text=content["body"]))
        return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

    async def edit(
        self,
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, Any],
        new_text: str,
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG002
    ) -> DeliveredMatrixEvent:
        self._record(_GatewayOp(kind="edit", content=dict(new_content), display_text=new_text))
        return DeliveredMatrixEvent(event_id=f"$edit_{len(self.ops)}", content_sent=dict(new_content))

    async def wait_for_ops(self, count: int) -> None:
        """Wait until the streaming machine has delivered `count` calls."""
        async with asyncio.timeout(30):
            while len(self.ops) < count:
                self._op_recorded.clear()
                await self._op_recorded.wait()


@pytest.fixture
def config() -> Config:
    """Minimal bound config for direct streaming tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="HelperAgent", rooms=["!test:localhost"])},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    return config


@pytest.fixture
def fake_clock() -> Iterator[None]:
    """Advance time 10s per call so every throttle window is open."""
    ticks = itertools.count(1_000_000.0, 10.0)
    with patch("mindroom.streaming.time.time", side_effect=lambda: next(ticks)):
        yield


async def _run_stream(
    config: Config,
    response_stream: AsyncIterator[object],
    *,
    show_tool_calls: bool = True,
) -> StreamTransportOutcome:
    return await send_streaming_response(
        client=make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
        response_stream=response_stream,
        show_tool_calls=show_tool_calls,
    )


@pytest.mark.asyncio
async def test_lifecycle_suspension_escapes_stream_delivery_unchanged(config: Config) -> None:
    """A native approval pause belongs to the response lifecycle, not stream error finalization."""
    suspension = StreamingLifecycleSuspensionError("paused")

    async def suspended_stream() -> AsyncIterator[object]:
        raise suspension
        yield  # pragma: no cover

    with pytest.raises(StreamingLifecycleSuspensionError) as raised:
        await _run_stream(config, suspended_stream())

    assert raised.value is suspension


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_lifecycle_suspension_captures_last_committed_presentation(config: Config) -> None:
    """Approval handoff retains exactly the ordered body and trace that reached Matrix."""
    suspension = StreamingLifecycleSuspensionError("paused")
    gateway = _FakeGateway()

    async def suspended_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="I checked the input.")
        await gateway.wait_for_ops(1)
        yield ToolCallStartedEvent(
            tool=ToolExecution(
                tool_call_id="call-1",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
            ),
        )
        await gateway.wait_for_ops(2)
        raise suspension

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingLifecycleSuspensionError) as raised,
    ):
        await _run_stream(config, suspended_stream())

    assert raised.value is suspension
    assert raised.value.presentation is not None
    assert raised.value.presentation.response_text == "I checked the input.\n\n🔧 `inspect` [1] ⏳\n\n"
    assert raised.value.presentation.visible_response_text == gateway.ops[-1].display_text
    assert len(raised.value.presentation.tool_trace) == 1
    trace = raised.value.presentation.tool_trace[0]
    assert trace.type == "tool_call_started"
    assert trace.tool_name == "inspect"
    assert trace.args_preview == "path=report.txt"
    assert trace.tool_call_id == "call-1"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_lifecycle_suspension_separates_raw_renderer_state_from_exact_visible_body(config: Config) -> None:
    """Resume state stays lossless even when Matrix received an interactive transform."""
    suspension = StreamingLifecycleSuspensionError("paused")
    gateway = _FakeGateway()
    raw = """Please choose.

```interactive
{
    "question": "Which option?",
    "options": [
        {"emoji": "✅", "label": "Approve", "value": "approve"}
    ]
}
```  """

    async def suspended_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content=raw)
        await gateway.wait_for_ops(1)
        raise suspension

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingLifecycleSuspensionError) as raised,
    ):
        await _run_stream(config, suspended_stream())

    assert raised.value.presentation is not None
    assert raised.value.presentation.response_text == raw
    assert raised.value.presentation.visible_response_text == gateway.ops[-1].display_text
    assert raised.value.presentation.visible_response_text != raw


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_hidden_tool_pause_captures_private_stable_identity(config: Config) -> None:
    """Hidden rendering omits public metadata but retains the pending call for exact continuation."""
    suspension = StreamingLifecycleSuspensionError("paused")
    gateway = _FakeGateway()

    async def suspended_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="Before approval.")
        await gateway.wait_for_ops(1)
        yield ToolCallStartedEvent(
            tool=ToolExecution(tool_call_id="call-hidden", tool_name="inspect", tool_args={}),
        )
        await gateway.wait_for_ops(2)
        raise suspension

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingLifecycleSuspensionError) as raised,
    ):
        await _run_stream(config, suspended_stream(), show_tool_calls=False)

    assert raised.value.presentation is not None
    assert raised.value.presentation.response_text == "Before approval.\n\n"
    assert raised.value.presentation.visible_response_text == "Before approval.\n\n"
    assert [(entry.tool_call_id, entry.type) for entry in raised.value.presentation.tool_trace] == [
        ("call-hidden", "tool_call_started"),
    ]
    assert _TOOL_TRACE_KEY not in gateway.ops[-1].content


def test_nonterminal_ack_comparison_includes_private_tool_identity(config: Config) -> None:
    """A delayed acknowledgement for one call cannot clear pending state for another identical call."""
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    streaming.accumulated_text = "Same body"
    streaming.tool_trace = [
        ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-2"),
    ]
    streaming.chars_since_last_update = 7
    committed = _CommittedDeliveryState(
        accumulated_text="Same body",
        tool_trace=[ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")],
        presentation_state=None,
        placeholder_progress_sent=False,
        rendered_body="Same body",
        visible_body_state="visible_body",
        interactive_metadata=None,
        stream_status=STREAM_STATUS_STREAMING,
    )

    streaming._mark_nonterminal_delivery(committed)

    assert streaming.chars_since_last_update == 7


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_lifecycle_suspension_captures_committed_structured_state(config: Config) -> None:
    """Team continuation state is committed atomically with the body it can reproduce."""
    suspension = StreamingLifecycleSuspensionError("paused")
    gateway = _FakeGateway()
    state = {
        "kind": "team",
        "display_names": ["GeneralAgent"],
        "members": {"GeneralAgent": "Before."},
        "consensus": "",
    }

    async def suspended_stream() -> AsyncIterator[object]:
        yield StructuredStreamChunk(
            content="🤝 **Team Response** (GeneralAgent):\n\n**GeneralAgent**: Before.",
            presentation_state=state,
        )
        await gateway.wait_for_ops(1)
        raise suspension

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingLifecycleSuspensionError) as raised,
    ):
        await _run_stream(config, suspended_stream())

    assert raised.value.presentation is not None
    assert raised.value.presentation.state == state


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_lifecycle_suspension_flushes_state_only_presentation(config: Config) -> None:
    """A controlled pause commits private structured state before lifecycle handoff."""
    suspension = StreamingLifecycleSuspensionError("paused")
    gateway = _FakeGateway()
    trace = ToolTraceEntry(
        type="tool_call_started",
        tool_name="inspect",
        tool_call_id="call-hidden",
        scope_key="team",
    )
    state = {
        "kind": "team_stream",
        "version": 2,
        "members": [],
        "consensus": "",
    }

    async def suspended_stream() -> AsyncIterator[object]:
        yield StructuredStreamChunk(
            content="",
            tool_trace=[trace],
            presentation_state=state,
        )
        raise suspension

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingLifecycleSuspensionError) as raised,
    ):
        await _run_stream(config, suspended_stream(), show_tool_calls=False)

    assert [operation.display_text for operation in gateway.ops] == ["Thinking..."]
    assert raised.value.presentation is not None
    assert raised.value.presentation.response_text == ""
    assert raised.value.presentation.visible_response_text == "Thinking..."
    assert raised.value.presentation.tool_trace == (trace,)
    assert raised.value.presentation.state == state


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_placeholder_progressive_edits_and_final_tool_trace(config: Config) -> None:
    """A scripted stream produces placeholder → progressive edits → final tool trace."""
    gateway = _FakeGateway()

    async def scripted_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="")
        await gateway.wait_for_ops(1)
        yield "Hello"
        await gateway.wait_for_ops(2)
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}))
        await gateway.wait_for_ops(3)
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="search_web", tool_args={"q": "mindroom"}, result="ok"),
            content="ok",
        )
        await gateway.wait_for_ops(4)
        yield " Done."
        await gateway.wait_for_ops(5)

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
    ):
        outcome = await _run_stream(config, scripted_stream())

    kinds = [op.kind for op in gateway.ops]
    assert kinds == ["send", "edit", "edit", "edit", "edit", "edit"]

    placeholder, first_text, tool_started, tool_completed, more_text, final = gateway.ops
    assert placeholder.content["body"] == _PROGRESS_PLACEHOLDER
    assert placeholder.content["msgtype"] == "m.notice"
    assert placeholder.content[STREAM_STATUS_KEY] == STREAM_STATUS_PENDING

    assert first_text.display_text == "Hello"
    assert first_text.content["msgtype"] == "m.notice"
    assert first_text.content[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING

    assert tool_started.display_text.startswith("Hello")
    assert "🔧 `search_web` [1] ⏳" in tool_started.display_text
    started_trace = tool_started.content[_TOOL_TRACE_KEY]["events"]
    assert [event["type"] for event in started_trace] == ["tool_call_started"]

    assert "🔧 `search_web` [1] ⏳" not in tool_completed.display_text
    assert "🔧 `search_web` [1]" in tool_completed.display_text
    completed_trace = tool_completed.content[_TOOL_TRACE_KEY]["events"]
    assert [event["type"] for event in completed_trace] == ["tool_call_completed"]
    assert completed_trace[0]["tool_name"] == "search_web"

    assert more_text.display_text.endswith("Done.")
    assert more_text.content["msgtype"] == "m.notice"
    assert final.display_text == more_text.display_text
    assert final.content["msgtype"] == "m.text"
    assert final.content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
    assert final.content[_TOOL_TRACE_KEY]["events"] == completed_trace

    assert outcome.terminal_status == "completed"
    assert outcome.visible_body_state == "visible_body"
    assert outcome.visible_event_id == "$stream_1"
    assert outcome.visible_body_text == final.display_text


@pytest.mark.asyncio
async def test_nonterminal_delivery_formats_off_event_loop_thread(config: Config) -> None:
    """Markdown and mention formatting should not block the stream owner's event loop."""
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    streaming.accumulated_text = "Hello **world**"

    loop_thread_id = threading.get_ident()
    format_thread_ids: list[int] = []
    delivered_content: dict[str, Any] = {}
    original_format = streaming_mod.format_message_with_mentions

    def recording_format(
        config: Config,
        runtime_paths: object,
        text: str,
        thread_event_id: str | None = None,
        reply_to_event_id: str | None = None,
        latest_thread_event_id: str | None = None,
        tool_trace: list[ToolTraceEntry] | None = None,
        extra_content: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        format_thread_ids.append(threading.get_ident())
        return original_format(
            config,
            runtime_paths,
            text,
            thread_event_id=thread_event_id,
            reply_to_event_id=reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
            tool_trace=tool_trace,
            extra_content=extra_content,
        )

    async def fake_send(
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        delivered_content.update(content)
        return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

    with (
        patch("mindroom.streaming.format_message_with_mentions", new=recording_format),
        patch("mindroom.streaming.send_message_result", new=fake_send),
    ):
        sent = await streaming._send_or_edit_message(
            make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
        )

    assert sent is True
    assert format_thread_ids
    assert all(thread_id != loop_thread_id for thread_id in format_thread_ids)
    assert delivered_content["body"] == "Hello **world**"
    assert "<strong>world</strong>" in delivered_content["formatted_body"]


@pytest.mark.asyncio
async def test_placeholder_ack_waits_for_answer_ack_before_marking_substantive(config: Config) -> None:
    """Substantive timing must describe the acknowledged payload, not newer buffered text."""
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!test:localhost")
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
        pipeline_timing=timing,
    )
    delivered_bodies: list[str] = []

    async def fake_send(
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        delivered_bodies.append(content["body"])
        streaming.accumulated_text = "Answer buffered while the placeholder is in flight"
        return DeliveredMatrixEvent(event_id="$placeholder", content_sent=dict(content))

    async def fake_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, Any],
        _new_text: str,
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        delivered_bodies.append(new_content["body"])
        return DeliveredMatrixEvent(event_id="$answer-edit", content_sent=dict(new_content))

    with (
        patch("mindroom.streaming.send_message_result", new=fake_send),
        patch("mindroom.streaming.edit_message_result", new=fake_edit),
    ):
        placeholder_sent = await streaming._send_or_edit_message(
            make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
            allow_empty_progress=True,
        )
        assert placeholder_sent is True
        assert delivered_bodies == [_PROGRESS_PLACEHOLDER]
        assert "first_substantive_reply" not in timing.marks
        assert "first_substantive_kind" not in timing.metadata

        answer_sent = await streaming._send_or_edit_message(
            make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
        )

    assert answer_sent is True
    assert delivered_bodies == [
        _PROGRESS_PLACEHOLDER,
        "Answer buffered while the placeholder is in flight",
    ]
    assert "first_substantive_reply" in timing.marks
    assert timing.metadata["first_substantive_kind"] == "stream_update"


@pytest.mark.parametrize(
    ("stream_status", "terminal_note"),
    [
        (STREAM_STATUS_ERROR, "**[Response interrupted by an error: boom]**"),
        (STREAM_STATUS_CANCELLED, _CANCELLED_RESPONSE_NOTE),
    ],
)
@pytest.mark.asyncio
async def test_terminal_failure_note_ack_is_visible_but_not_substantive(
    config: Config,
    stream_status: str,
    terminal_note: str,
) -> None:
    """Acknowledged failure notes are visible transport output, not substantive replies."""
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!test:localhost")
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
        pipeline_timing=timing,
    )
    streaming.event_id = "$placeholder"
    streaming.placeholder_progress_sent = True
    streaming.accumulated_text = terminal_note

    async def fake_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, Any],
        _new_text: str,
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        return DeliveredMatrixEvent(event_id="$terminal-edit", content_sent=dict(new_content))

    with patch("mindroom.streaming.edit_message_result", new=fake_edit):
        sent = await streaming._send_or_edit_message(
            make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
            is_final=True,
            stream_status=stream_status,
        )

    assert sent is True
    assert "first_visible_reply" in timing.marks
    assert timing.metadata["first_visible_kind"] == "stream_update"
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


def test_delivery_snapshot_isolates_tool_trace(config: Config) -> None:
    """Snapshot formatting should not observe later live tool-trace mutations."""
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    streaming.accumulated_text = "Hello"
    streaming.tool_trace = [ToolTraceEntry(type="tool_call_started", tool_name="search")]

    snapshot = streaming._delivery_snapshot(
        is_final=False,
        allow_empty_progress=False,
        stream_status=None,
    )

    assert snapshot is not None
    streaming.tool_trace[0].type = "tool_call_completed"
    streaming.tool_trace[0].result_preview = "done"
    streaming.tool_trace.append(ToolTraceEntry(type="tool_call_started", tool_name="other"))

    assert isinstance(snapshot.tool_trace, tuple)
    assert len(snapshot.tool_trace) == 1
    assert snapshot.tool_trace[0].type == "tool_call_started"
    assert snapshot.tool_trace[0].result_preview is None


def test_delivery_preparation_builds_thread_relation_only_for_initial_send(config: Config) -> None:
    """Edit payloads must not put dead thread or reply relations in m.new_content."""
    streaming = StreamingResponse(
        target=MessageTarget.resolve("!test:localhost", "$thread", "$reply"),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    streaming.accumulated_text = "Hello"
    streaming.latest_thread_event_id = "$latest"
    initial_snapshot = streaming._delivery_snapshot(
        is_final=False,
        allow_empty_progress=False,
        stream_status=None,
    )
    assert initial_snapshot is not None

    streaming.event_id = "$stream"
    edit_snapshot = streaming._delivery_snapshot(
        is_final=False,
        allow_empty_progress=False,
        stream_status=None,
    )
    assert edit_snapshot is not None

    formatting_kwargs: list[dict[str, object]] = []

    def recording_format(**kwargs: object) -> dict[str, str]:
        formatting_kwargs.append(kwargs)
        return {
            "msgtype": "m.text",
            "body": "Hello",
            "format": "org.matrix.custom.html",
            "formatted_body": "Hello",
        }

    with patch("mindroom.streaming.format_message_with_mentions", new=recording_format):
        streaming_mod._prepare_delivery_from_snapshot(initial_snapshot)
        streaming_mod._prepare_delivery_from_snapshot(edit_snapshot)

    initial_kwargs, edit_kwargs = formatting_kwargs
    assert initial_kwargs["thread_event_id"] == "$thread"
    assert initial_kwargs["reply_to_event_id"] == "$reply"
    assert initial_kwargs["latest_thread_event_id"] == "$latest"
    assert edit_kwargs["thread_event_id"] is None
    assert edit_kwargs["reply_to_event_id"] is None
    assert edit_kwargs["latest_thread_event_id"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_clock")
async def test_cancellation_mid_stream_appends_cancelled_note(config: Config) -> None:
    """User cancellation mid-stream finalizes the partial text with the cancelled note."""
    gateway = _FakeGateway()

    async def cancelling_stream() -> AsyncIterator[object]:
        yield "Partial answer"
        await gateway.wait_for_ops(1)
        raise asyncio.CancelledError(USER_STOP_CANCEL_MSG)

    with (
        patch("mindroom.streaming.send_message_result", new=gateway.send),
        patch("mindroom.streaming.edit_message_result", new=gateway.edit),
        pytest.raises(StreamingDeliveryError) as exc_info,
    ):
        await _run_stream(config, cancelling_stream())

    kinds = [op.kind for op in gateway.ops]
    assert kinds == ["send", "edit"]

    partial, cancelled = gateway.ops
    assert partial.content["body"] == "Partial answer"
    assert partial.content["msgtype"] == "m.notice"
    assert cancelled.display_text == f"Partial answer\n\n{_CANCELLED_RESPONSE_NOTE}"
    assert cancelled.content["msgtype"] == "m.text"
    assert cancelled.content[STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED

    transport_outcome = exc_info.value.transport_outcome
    assert transport_outcome.terminal_status == "cancelled"
    assert transport_outcome.failure_reason == "cancelled_by_user"
    assert transport_outcome.visible_event_id == "$stream_1"
