"""The participation check decides once, before a reply's first request, and never executes tools or alters the reply."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.judgment.state import JudgmentMessage
from mindroom.llm.events import ToolStarted
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, NativeCompactionReplay, RequestOverrides
from mindroom.participation import ParticipationDecision, ParticipationGate
from mindroom.participation_hook import parse_decision, participation_hook
from mindroom.providers.tool_policy import provider_tools_disabled
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.provider import StreamEvent
    from mindroom.llm.runs import RunRecord
    from tests.fakes.scripted_model import ScriptStep

_RESPOND = '{"action":"respond","reason":"A factual question is unanswered."}'
_SILENT = '{"action":"stay_silent","reason":"Humans are discussing plans."}'


def operate() -> str:
    """Run the operation."""
    operate.calls.append("operated")  # type: ignore[attr-defined]
    return "done"


operate.calls = []  # type: ignore[attr-defined]


class _PolicyAwareModel(ScriptedModel):
    """Records whether provider-managed tools were disabled for each request."""

    def __init__(self, turns: list[Sequence[ScriptStep]]) -> None:
        super().__init__(turns)
        self.tools_disabled: list[bool] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the scripted turn after noting the task's tool policy."""
        self.tools_disabled.append(provider_tools_disabled())
        async for event in super().stream(request):
            yield event


async def _reply(
    model: ScriptedModel,
    gate: ParticipationGate,
    messages: Sequence[Message] | None = None,
    *,
    run_id: str = "run-1",
    request_template: ModelRequest = EMPTY_REQUEST,
) -> tuple[list[object], RunRecord]:
    """Run one reply of the ``code`` agent, which offers ``operate``, under the participation gate."""
    return await collect(
        run_agent(
            spec(model, toolkit(operate)),
            list(messages or [Message(role="user", content="Can anyone explain this?")]),
            ctx=context(run_id=run_id),
            hooks=LoopHooks(before_request=(participation_hook(gate, model),)),
            request_template=request_template,
        ),
    )


@pytest.fixture(autouse=True)
def _reset_operate() -> None:
    operate.calls.clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_decision_cannot_execute_tools() -> None:
    """A decision answered with tool calls is invalid, so the reply stays silent and the tool never runs."""
    model = ScriptedModel([calls(tool_call("operate", "call-1"))])
    gate = ParticipationGate()

    events, record = await _reply(model, gate)

    assert operate.calls == []  # type: ignore[attr-defined]
    assert not any(isinstance(event, ToolStarted) for event in events)
    assert len(model.requests) == 1
    assert gate.is_silent
    assert gate.decision is not None
    assert gate.decision.reason == "invalid_decision"
    assert not record.content
    assert not record.tools


@pytest.mark.asyncio
async def test_approval_reuses_exact_normal_prefix_and_only_checks_once() -> None:
    """The decision is the reply's exact messages plus the prompt; later replies under the gate skip the check."""
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer"), answer("Continued")])
    gate = ParticipationGate(instructions="Offer concise technical help.")

    _, first = await _reply(model, gate)
    _, second = await _reply(model, gate, [Message(role="user", content="Continue")], run_id="run-2")

    decision, reply, continuation = model.requests
    assert [(m.role, m.content) for m in decision.messages[:-1]] == [(m.role, m.content) for m in reply.messages]
    assert decision.messages[-1].role == "user"
    assert "Offer concise technical help." in str(decision.messages[-1].content)
    assert decision.tools == reply.tools == continuation.tools
    assert (decision.tool_choice, decision.stream) == ("none", False)
    assert (reply.tool_choice, reply.stream) == (None, True)
    assert first.content == "Useful answer"
    assert second.content == "Continued"
    assert gate.approved
    assert gate.decided.is_set()
    # The decision prompt is transient: the stored run holds the reply's messages only.
    assert [m.role for m in first.messages] == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_the_decision_request_keeps_every_setting_of_the_reply_request() -> None:
    """The decision is the reply's request with the prompt appended, tool selection off, and one non-streaming call.

    Deferred tool search, native compaction, portable replay, and request overrides reach the decision as they reach
    the reply, so the decision shares the reply's prompt cache and replays the history the provider compacted.
    """
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])
    template = ModelRequest(
        messages=(),
        overrides=RequestOverrides(temperature=0.2, prompt_cache=True),
        deferred_tool_names=frozenset({"operate"}),
        portable_replay=True,
        native_compaction=NativeCompactionReplay(route="route-1", threshold=1000),
    )

    await _reply(model, ParticipationGate(), request_template=template)

    decision, reply = model.requests
    assert replace(reply, messages=template.messages, tools=template.tools) == template
    assert tuple(decision.messages[:-1]) == tuple(reply.messages)
    assert (decision.tool_choice, decision.stream) == ("none", False)
    assert replace(decision, messages=reply.messages, tool_choice=reply.tool_choice, stream=reply.stream) == reply


@pytest.mark.asyncio
async def test_the_decision_request_disables_provider_managed_tools() -> None:
    """Only the decision request runs under the task policy that keeps providers from selecting tools."""
    model = _PolicyAwareModel([answer(_RESPOND), answer("Useful answer")])

    await _reply(model, ParticipationGate())

    assert model.tools_disabled == [True, False]


@pytest.mark.parametrize("action", ["respond", "stay_silent"])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("", ""),
        ("My decision: ", ""),
        ("", " That is my decision."),
        ("Before.\n", "\nAfter."),
        ("```json\n", "\n```"),
        ("Before.\n```json\n", "\n```\nAfter."),
        ("[Decision]\n", ""),
        ("", " See [context](https://example.com)."),
        ("Some {informal notation}. ", ""),
    ],
)
def test_single_decision_allows_surrounding_text(action: str, prefix: str, suffix: str) -> None:
    """Prose and fences must not hide a single valid verdict."""
    reason = 'An unanswered question about {"key": "value"} needs help.'
    decision = parse_decision(prefix + json.dumps({"action": action, "reason": reason}) + suffix)
    assert (decision.action, decision.reason) == (action, reason)


@pytest.mark.parametrize(
    "content",
    [
        '{"action":"respond","reason":"Help."}{"action":"stay_silent","reason":"Wait."}',
        '{"action":"respond","reason":"Help."} More text. {"action":"respond","reason":"Help."}',
        'An example: {}. Decision: {"action":"respond","reason":"Help."}',
        'Before. [{"action":"respond","reason":"Help."}] After.',
        'Before. {"decision":{"action":"respond","reason":"Help."}} After.',
        '[{"action":"respond","reason":"Help."}',
        '{"decision":{"action":"respond","reason":"Help."}',
        '[1, {"action":"respond","reason":"Help."}',
        '[null, {"action":"respond","reason":"Help."}',
        'Before. {"action":"respond","reason":"Help.","extra":true} After.',
        'Before. {"action":"respond"} After.',
        'Before. {"action":"maybe","reason":"Help."} After.',
        'Before. {"action":"respond","reason":"Help." After.',
        '{"action":"error","reason":"Not a model verdict."}',
        '{"action":"stay_silent","action":"respond","reason":"Wait."}',
        '{"action":"respond","reason":"Wait.","reason":"Help."}',
        '{"action":"stay_silent","\\u0061ction":"respond","reason":"Wait."}',
    ],
)
def test_ambiguous_invalid_or_duplicate_key_decisions_are_rejected(content: str) -> None:
    """Extraction never picks a verdict from several objects, bypasses the schema, or accepts duplicate keys."""
    with pytest.raises(ValueError):  # noqa: PT011 - JSON, schema, and duplicate-key failures each word it differently
        parse_decision(content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "declined", "reason"),
    [
        (answer(_SILENT), True, "Humans are discussing plans."),
        (answer("Sure!"), False, "decision_failed"),
        (answer('{"action":"respond"}'), False, "decision_failed"),
        (answer('{"action":"error","reason":"Not a model verdict."}'), False, "decision_failed"),
        (answer('{"action":"respond","action":"stay_silent","reason":"Wait."}'), False, "decision_failed"),
        ([Await(AsyncMock(side_effect=RuntimeError("provider unavailable")))], False, "decision_failed"),
    ],
)
async def test_invalid_failed_or_declined_decisions_stay_quiet(
    turn: list[ScriptStep],
    declined: bool,
    reason: str,
) -> None:
    """An untrusted or failed decision never authorizes a reply; only an explicit verdict counts as declined."""
    model = ScriptedModel([turn])
    gate = ParticipationGate()

    _, record = await _reply(model, gate)

    assert not record.content
    assert len(model.requests) == 1
    assert gate.is_silent
    assert gate.is_declined is declined
    assert gate.decision is not None
    assert gate.decision.reason == reason


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_silence() -> None:
    """Stopping a deciding reply keeps its cancellation; the gate stays undecided."""

    async def cancel() -> None:
        raise asyncio.CancelledError

    gate = ParticipationGate()
    with pytest.raises(asyncio.CancelledError):
        await _reply(ScriptedModel([[Await(cancel)]]), gate)

    assert gate.decision is None


@pytest.mark.asyncio
async def test_decision_usage_is_included_in_run_metrics() -> None:
    """The decision request's tokens count toward the run like any other request."""
    model = ScriptedModel([answer(_RESPOND, usage(40, 8, cache_read_tokens=30)), answer("Useful answer", usage(10, 2))])

    _, record = await _reply(model, ParticipationGate())

    assert record.metrics is not None
    assert (
        record.metrics.tokens.input_tokens,
        record.metrics.tokens.output_tokens,
        record.metrics.tokens.cache_read_tokens,
    ) == (50, 10, 30)


@pytest.mark.asyncio
async def test_an_external_decider_answers_plain_text_conversations_without_a_model_request() -> None:
    """A configured judgment backend sees the room text and, when it decides, the reply skips the model decision."""
    decider = AsyncMock(return_value=ParticipationDecision(action="respond", reason="Open question."))
    model = ScriptedModel([answer("Useful answer")])
    messages = [
        Message(role="system", content="Private rules"),
        Message(role="user", content="Hello there"),
        Message(role="assistant", content="Hi!"),
        Message(role="user", content="Can anyone explain this?"),
    ]

    _, record = await _reply(model, ParticipationGate(decider=decider), messages)

    assert record.content == "Useful answer"
    assert len(model.requests) == 1
    decider.assert_awaited_once_with(
        (
            JudgmentMessage(sender="user", text="Hello there"),
            JudgmentMessage(sender="assistant", text="Hi!"),
            JudgmentMessage(sender="user", text="Can anyone explain this?"),
        ),
    )


@pytest.mark.asyncio
async def test_an_abstaining_decider_falls_back_to_the_model_decision() -> None:
    """When the backend abstains, the reply's own model decides as it would without a backend."""
    decider = AsyncMock(return_value=None)
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])

    _, record = await _reply(model, ParticipationGate(decider=decider), run_id="run-1")

    assert record.content == "Useful answer"
    assert len(model.requests) == 2
    decider.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["media", "attachment_text", "history_attachment", "tool_calls"])
async def test_conversations_with_more_than_room_text_skip_the_external_decider(kind: str) -> None:
    """Media, attachments, and tool calls are not projected to a backend; the model decides on the full context."""
    decider = AsyncMock(return_value=ParticipationDecision(action="stay_silent", reason="Nope."))
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])
    message = Message(role="user", content="Any ideas?")
    if kind == "media":
        message.images = [Image(url="https://example.org/image.png")]
    elif kind == "attachment_text":
        message.content = "Attachments sent with the current message (use tool calls to inspect them):\nprivate.txt"
    elif kind == "history_attachment":
        message.content = "Can you inspect this? [attachments: att_123 (private.txt)]"
    else:
        message = Message(role="assistant", content="Looking.", tool_calls=[tool_call("operate", "call-0")])

    _, record = await _reply(model, ParticipationGate(decider=decider), [message])

    assert record.content == "Useful answer"
    decider.assert_not_awaited()
    assert len(model.requests) == 2
