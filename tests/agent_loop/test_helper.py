"""One-shot helper calls send one tool-less request and return the answer with a usage-ready run record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_once, complete_prompt
from mindroom.agent_loop.prompt import PromptSpec, render_system_message
from mindroom.agent_loop.structured import select_response_format
from mindroom.llm.errors import ProviderUnavailable
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    RequestOverrides,
    StreamEvent,
    TextDelta,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunStatus
from mindroom.llm.usage import MessageMetrics
from tests.agno_parity.prompt_scenarios import message_dict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

IDENTITY = ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic", provider_name="Anthropic")


@dataclass
class _RecordingProvider:
    """Answer every request with one canned turn and keep the requests."""

    answer: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    identity: ModelIdentity = IDENTITY
    native_history_route: None = None
    replay_estimator: None = None
    model_params: dict[str, object] = field(default_factory=dict)
    finish: bool = True
    requests: list[ModelRequest] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield TextDelta(self.answer)
        if not self.finish:
            return
        usage = MessageMetrics(input_tokens=12, output_tokens=5, total_tokens=17)
        yield UsageReported(usage)
        yield TurnCompleted(message=Message(role="assistant", content=self.answer, metrics=usage), stop_reason="end")


class _Verdict(BaseModel):
    answer: str
    confidence: int | None = None


def _messages(spec: PromptSpec, prompt: str) -> list[Message]:
    return [render_system_message(spec), Message(role="user", content=prompt)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "json_prompt"),
    [("native", False), ("json_schema", False), ("json_mode", True)],
)
async def test_a_prompt_is_sent_under_its_rendered_system_message(structured_output: str, *, json_prompt: bool) -> None:
    """The role and instructions render as the system message, which ends with the JSON prompt on JSON-mode routes."""
    capabilities = Capabilities(structured_output=structured_output)
    provider = _RecordingProvider(answer='{"answer": "yes"}', capabilities=capabilities)

    result = await complete_prompt(
        provider,
        "Is it raining?",
        role="Decide.",
        instructions=["Answer briefly.", "Use the evidence."],
        schema=_Verdict,
        run_id="decide-1",
    )

    assert result.content == _Verdict(answer="yes")
    assert result.record.run_id == "decide-1"
    [request] = provider.requests
    selected = select_response_format(capabilities, _Verdict)
    spec = PromptSpec(
        role="Decide.",
        instructions=("Answer briefly.", "Use the evidence."),
        output_format_prompt=selected.prompt,
    )
    assert [message_dict(message) for message in request.messages] == [
        message_dict(render_system_message(spec)),
        message_dict(Message(role="user", content="Is it raining?")),
    ]
    assert request.response_format == selected.request_format
    assert ("Provide your output as a JSON" in str(request.messages[0].content)) is json_prompt


@pytest.mark.asyncio
async def test_a_prompt_without_a_schema_returns_text_with_the_callers_overrides() -> None:
    """Without a schema nothing asks for JSON, and the caller's overrides reach the request."""
    provider = _RecordingProvider(answer="Noted.")
    overrides = RequestOverrides(temperature=0.2)

    result = await complete_prompt(provider, "Remember aisle seats.", role="Extract memories.", overrides=overrides)

    assert result.content == "Noted."
    [request] = provider.requests
    assert request.messages[0].content == "<your_role>\nExtract memories.\n</your_role>"
    assert (request.response_format, request.overrides) == (None, overrides)


@pytest.mark.asyncio
async def test_a_text_answer_returns_its_text_and_a_completed_record() -> None:
    """Without a schema the answer is the text, and the record carries the request's messages, model, and usage."""
    provider = _RecordingProvider(answer="The user prefers aisle seats.")
    messages = _messages(PromptSpec(role="Extract durable memories."), "Conversation excerpt: ...")

    result = await complete_once(provider, messages, run_id="flush-1")

    assert result.content == "The user prefers aisle seats."
    record = result.record
    assert (record.run_id, record.status, record.content) == ("flush-1", RunStatus.completed, result.content)
    assert (record.model, record.model_provider) == ("claude-sonnet-5-5", "Anthropic")
    assert [message.role for message in record.messages] == ["system", "user", "assistant"]
    assert record.messages[:2] == messages
    assert record.metrics is not None
    usage = record.metrics.to_dict()
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (12, 5, 17)
    [details] = usage["details"]["model"]
    assert (details["id"], details["provider"], details["total_tokens"]) == ("claude-sonnet-5-5", "Anthropic", 17)
    assert record.to_dict()["content"] == "The user prefers aisle seats."


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_output", ["native", "json_schema", "json_mode"])
async def test_a_structured_answer_is_read_into_the_schema(structured_output: str) -> None:
    """The request asks for the schema the provider's way, and the answer comes back as the model."""
    capabilities = Capabilities(structured_output=structured_output)
    response_format = select_response_format(capabilities, _Verdict)
    provider = _RecordingProvider(answer='```json\n{"answer": "yes"}\n```', capabilities=capabilities)
    spec = PromptSpec(role="Decide.", output_format_prompt=response_format.prompt)

    result = await complete_once(provider, _messages(spec, "Is it raining?"), response_format=response_format)

    assert result.content == _Verdict(answer="yes")
    assert result.record.content == {"answer": "yes"}
    [request] = provider.requests
    assert request.response_format == response_format.request_format
    assert (response_format.prompt is not None) == (structured_output == "json_mode")


@pytest.mark.asyncio
async def test_an_unreadable_structured_answer_stays_text() -> None:
    """An answer that does not validate is returned as its text, so callers that check the type fall back."""
    response_format = select_response_format(Capabilities(structured_output="native"), _Verdict)
    provider = _RecordingProvider(answer="I cannot decide.")

    with capture_logs() as logs:
        result = await complete_once(
            provider,
            _messages(PromptSpec(role="Decide."), "?"),
            response_format=response_format,
        )

    assert result.content == "I cannot decide."
    assert result.record.content == "I cannot decide."
    assert {"event": "helper_structured_output_unparsed", "schema": "_Verdict", "log_level": "warning"} in logs


@pytest.mark.asyncio
async def test_a_request_carries_the_callers_overrides_and_no_tools() -> None:
    """Per-call overrides reach the request, and a helper request offers no tools."""
    provider = _RecordingProvider(answer="ok")
    overrides = RequestOverrides(temperature=0.2, caller_retries=True)

    await complete_once(provider, _messages(PromptSpec(role="Summarize."), "Thread: ..."), overrides=overrides)

    [request] = provider.requests
    assert (request.overrides, request.tools, request.tool_choice, request.response_format) == (
        overrides,
        (),
        None,
        None,
    )


@pytest.mark.asyncio
async def test_a_stream_that_never_finishes_its_turn_raises() -> None:
    """A provider stream that ends without a finished turn is a provider error, not an empty answer."""
    provider = _RecordingProvider(answer="partial", finish=False)

    with pytest.raises(ProviderUnavailable, match="without completing the turn"):
        await complete_once(provider, _messages(PromptSpec(role="Summarize."), "Thread: ..."))
