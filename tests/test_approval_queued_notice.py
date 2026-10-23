"""Queued-message notices at the owned loop's approval continuation boundaries.

The team continuation still runs through Agno until the team runtime cutover; its notices are tested with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.ai_runtime import (
    queued_message_signal_context,
    queued_notice_after_tool_batch,
    queued_notice_on_resume_entry,
)
from mindroom.approval.receipt import approval_receipt_context, approval_receipt_hook
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelRequest
from mindroom.llm.runs import RunStatus
from mindroom.mid_turn import QueuedMessage
from tests.agent_loop.helpers import collect, context, gated, spec
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call

if TYPE_CHECKING:
    from typing import Any

_NOTICE = "Pause tool use and summarize before handling the newer message."


@dataclass
class _QueuedState:
    pending: bool = False

    def has_pending_human_messages(self) -> bool:
        return self.pending

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]:
        return (QueuedMessage("$pending", None),) if self.has_pending_human_messages() else ()


def _tool_calls() -> list[dict[str, Any]]:
    return [tool_call("read_item", f"call-{item}", item=item) for item in (1, 2)]


def _hooks() -> LoopHooks:
    """Return the notice and receipt hooks an agent run and its resume install."""
    return LoopHooks(
        before_request=(approval_receipt_hook(Capabilities()),),
        after_tool_batch=(queued_notice_after_tool_batch(_NOTICE),),
        on_resume_entry=(queued_notice_on_resume_entry(_NOTICE),),
    )


@pytest.mark.parametrize("stream", [False, True], ids=["response", "stream"])
@pytest.mark.asyncio
async def test_approved_batch_notifies_next_real_model_request(*, stream: bool) -> None:
    """A queued follow-up reaches the model once, after every resumed tool result and the receipt."""
    state = _QueuedState()
    executed: list[int] = []

    def read_item(item: int) -> str:
        """Read one item.

        Args:
            item: Which item.

        """
        executed.append(item)
        state.pending = True
        return f"Result {item}"

    model = ScriptedModel([calls(*_tool_calls()), answer("Pausing here for the newer message.")])
    agent = spec(model, gated(read_item))
    approved = [
        ApprovalCall(
            tool_call_id=f"call-{item}",
            tool_name="read_item",
            invoking_agent="code",
            expires_at_ns=2**62,
            toolkit_name="gated",
            arguments_digest=approval_arguments_digest({"item": item}),
        )
        for item in (1, 2)
    ]

    with queued_message_signal_context(state):
        _events, paused = await collect(
            run_agent(agent, [Message(role="user", content="Read both items.")], ctx=context(), hooks=_hooks()),
        )
        assert paused.status is RunStatus.paused
        assert len(paused.requirements or []) == 2
        assert not executed
        with approval_receipt_context("Trusted approval receipt.", run_id=paused.run_id):
            _events, resumed = await collect(
                resume_agent(
                    agent,
                    paused,
                    build_resume_request(paused=paused.messages, summary=None, history=[]),
                    approved=approved,
                    ctx=context(),
                    hooks=_hooks(),
                    request_template=ModelRequest(messages=(), stream=stream),
                ),
            )

    assert resumed.status is RunStatus.completed
    assert executed == [1, 2]
    [first, request] = model.requests
    assert first.stream is True
    assert request.stream is stream
    messages = request.messages
    assert any(message.role == "system" and "Trusted approval receipt." in str(message.content) for message in messages)
    assert [message.tool_call_id for message in messages if message.role == "tool"] == ["call-1", "call-2"]
    assert sum(message.content == _NOTICE for message in messages) == 1
    assert messages[-1].content == _NOTICE


@pytest.mark.parametrize(
    "boundary",
    ["resolved", "system", "developer", "unresolved", "stop", "newer-input", "no-queue"],
)
@pytest.mark.asyncio
async def test_resume_entry_only_notifies_a_resolved_current_batch(boundary: str) -> None:
    """Resume entry must not split a batch, override stop-after, or revisit history."""
    messages = [
        Message(role="user", content="Read both items."),
        Message(role="assistant", tool_calls=_tool_calls()),
        Message(role="tool", tool_call_id="call-1", content="Result 1", stop_after_tool_call=boundary == "stop"),
    ]
    if boundary != "unresolved":
        messages.append(Message(role="tool", tool_call_id="call-2", content="Result 2"))
    if boundary in {"system", "developer"}:
        messages.insert(2, Message(role=boundary, content="Trusted runtime context."))
    if boundary == "newer-input":
        messages.append(Message(role="user", content="A different request."))

    with queued_message_signal_context(_QueuedState(pending=boundary != "no-queue")):
        await queued_notice_on_resume_entry(_NOTICE)(messages)

    notices = [message for message in messages if message.content == _NOTICE]
    assert len(notices) == (1 if boundary in {"resolved", "system", "developer"} else 0)


@pytest.mark.asyncio
async def test_a_second_batch_moves_the_notice_instead_of_repeating_it() -> None:
    """Each tool batch of one response turn carries the notice once, at the end of the live messages."""
    messages = [Message(role="assistant", tool_calls=_tool_calls())]
    results = [Message(role="tool", tool_call_id=f"call-{item}", content=f"Result {item}") for item in (1, 2)]
    messages.extend(results)
    hook = queued_notice_after_tool_batch(_NOTICE)

    with queued_message_signal_context(_QueuedState(pending=True)):
        await hook(messages, results)
        assert [message.content for message in messages[-2:]] == ["Result 2", _NOTICE]
        later = [Message(role="assistant", tool_calls=[tool_call("read_item", "call-3", item=3)])]
        later_results = [Message(role="tool", tool_call_id="call-3", content="Result 3")]
        messages.extend([*later, *later_results])
        await hook(messages, later_results)

    assert sum(message.content == _NOTICE for message in messages) == 1
    assert messages[-1].content == _NOTICE
