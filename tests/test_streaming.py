"""Direct unit tests for the streaming state machine in mindroom.streaming.

These tests drive send_streaming_response with a scripted chunk stream and a
fake Matrix seam (patched send/edit results), asserting the exact ordered
sequence of send and edit calls the state machine produces.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import patch

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.cancellation import USER_STOP_CANCEL_MSG
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
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
    send_streaming_response,
)
from mindroom.tool_system.events import _TOOL_TRACE_KEY
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

    async def send(
        self,
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        *,
        config: Config,
    ) -> DeliveredMatrixEvent:
        assert isinstance(config, Config)
        self.ops.append(_GatewayOp(kind="send", content=dict(content), display_text=content["body"]))
        return DeliveredMatrixEvent(event_id="$stream_1", content_sent=dict(content))

    async def edit(
        self,
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, Any],
        new_text: str,
        *,
        config: Config,
    ) -> DeliveredMatrixEvent:
        assert isinstance(config, Config)
        self.ops.append(_GatewayOp(kind="edit", content=dict(new_content), display_text=new_text))
        return DeliveredMatrixEvent(event_id=f"$edit_{len(self.ops)}", content_sent=dict(new_content))

    async def wait_for_ops(self, count: int) -> None:
        """Wait until the streaming machine has delivered `count` calls."""
        for _ in range(1000):
            if len(self.ops) >= count:
                return
            await asyncio.sleep(0.005)
        msg = f"Timed out waiting for {count} gateway calls; got {len(self.ops)}"
        raise AssertionError(msg)


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
) -> StreamTransportOutcome:
    return await send_streaming_response(
        client=make_matrix_client_mock(user_id="@mindroom_helper:localhost"),
        target=MessageTarget.resolve("!test:localhost", None, "$original_123", room_mode=True),
        config=config,
        runtime_paths=runtime_paths_for(config),
        response_stream=response_stream,
    )


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
    assert placeholder.content[STREAM_STATUS_KEY] == STREAM_STATUS_PENDING

    assert first_text.display_text == "Hello"
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
    assert final.display_text == more_text.display_text
    assert final.content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
    assert final.content[_TOOL_TRACE_KEY]["events"] == completed_trace

    assert outcome.terminal_status == "completed"
    assert outcome.visible_body_state == "visible_body"
    assert outcome.visible_event_id == "$stream_1"
    assert outcome.visible_body_text == final.display_text


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
    assert cancelled.display_text == f"Partial answer\n\n{_CANCELLED_RESPONSE_NOTE}"
    assert cancelled.content[STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED

    transport_outcome = exc_info.value.transport_outcome
    assert transport_outcome.terminal_status == "cancelled"
    assert transport_outcome.failure_reason == "cancelled_by_user"
    assert transport_outcome.visible_event_id == "$stream_1"
