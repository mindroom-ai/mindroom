"""One-shot helper calls send one tool-less request and return the answer with a usage-ready run record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_prompt
from mindroom.agent_loop.structured import output_format_prompt
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
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.assistant_turn import stamp_model
from tests.agno_parity.golden_files import turn_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

IDENTITY = ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic", provider_name="Anthropic")


@dataclass
class _RecordingProvider:
    """Answer every request with one canned turn, stamped as an adapter stamps it, and keep the requests."""

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
        usage = MessageMetrics(tokens=TokenCounts(input_tokens=12, output_tokens=5, total_tokens=17))
        yield UsageReported(usage)
        message = Message(role="assistant", content=self.answer, metrics=usage)
        stamp_model(message, self.identity)
        yield TurnCompleted(message=message, stop_reason="end")


class _Verdict(BaseModel):
    answer: str
    confidence: int | None = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "json_prompt"),
    [("native", False), ("json_schema", False), ("json_mode", True)],
)
async def test_a_prompt_is_sent_under_its_instructions_as_written(structured_output: str, *, json_prompt: bool) -> None:
    """Each instruction goes verbatim, a blank line apart, and the JSON prompt follows on JSON-mode routes."""
    capabilities = Capabilities(structured_output=structured_output)
    provider = _RecordingProvider(answer='{"answer": "yes"}', capabilities=capabilities)

    result = await complete_prompt(
        provider,
        "Is it raining?",
        instructions=["Decide.", "- Answer briefly.\n- Use the evidence."],
        schema=_Verdict,
        run_id="decide-1",
    )

    assert result.content == _Verdict(answer="yes")
    assert result.record.run_id == "decide-1"
    [request] = provider.requests
    json_tail = f"\n\n{output_format_prompt(capabilities, _Verdict)}" if json_prompt else ""
    assert [turn_message(message) for message in request.messages] == [
        turn_message(Message(role="system", content=f"Decide.\n\n- Answer briefly.\n- Use the evidence.{json_tail}")),
        turn_message(Message(role="user", content="Is it raining?")),
    ]
    assert request.output_schema is _Verdict


@pytest.mark.asyncio
async def test_a_prompt_without_a_schema_returns_text_with_the_callers_overrides() -> None:
    """Without a schema nothing asks for JSON, and the caller's overrides reach the request."""
    provider = _RecordingProvider(answer="Noted.")
    overrides = RequestOverrides(temperature=0.2)

    result = await complete_prompt(
        provider,
        "Remember aisle seats.",
        instructions=["Extract memories."],
        overrides=overrides,
    )

    assert result.content == "Noted."
    [request] = provider.requests
    assert request.messages[0].content == "Extract memories."
    assert (request.output_schema, request.overrides) == (None, overrides)


@pytest.mark.asyncio
async def test_a_text_answer_returns_its_text_and_a_completed_record() -> None:
    """Without a schema the answer is the text, and the record carries the request's messages, model, and usage."""
    provider = _RecordingProvider(answer="The user prefers aisle seats.")

    result = await complete_prompt(
        provider,
        "Conversation excerpt: ...",
        instructions=["Extract durable memories."],
        run_id="flush-1",
    )

    assert result.content == "The user prefers aisle seats."
    record = result.record
    assert (record.run_id, record.status, record.content) == ("flush-1", RunStatus.completed, result.content)
    assert (record.model, record.model_provider) == ("claude-sonnet-5-5", "Anthropic")
    assert [message.role for message in record.messages] == ["system", "user", "assistant"]
    assert record.messages[:2] == list(provider.requests[0].messages)
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
    provider = _RecordingProvider(answer='{"answer": "yes"}', capabilities=capabilities)

    result = await complete_prompt(provider, "Is it raining?", instructions=["Decide."], schema=_Verdict)

    assert result.content == _Verdict(answer="yes")
    assert result.record.content == '{"answer": "yes"}'
    [request] = provider.requests
    assert request.output_schema is _Verdict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "repaired"),
    [("native", False), ("json_schema", False), ("json_mode", True)],
)
async def test_only_a_json_mode_answer_is_repaired(structured_output: str, *, repaired: bool) -> None:
    """A fenced answer is repaired only on a JSON-mode route; a route that enforces the schema is read strictly."""
    fenced = '```json\n{"answer": "yes"}\n```'
    provider = _RecordingProvider(answer=fenced, capabilities=Capabilities(structured_output=structured_output))

    result = await complete_prompt(provider, "Is it raining?", instructions=["Decide."], schema=_Verdict)

    assert result.content == (_Verdict(answer="yes") if repaired else fenced)


@pytest.mark.asyncio
async def test_an_unreadable_structured_answer_stays_text() -> None:
    """An answer that does not validate is returned as its text, so callers that check the type fall back."""
    provider = _RecordingProvider(answer="I cannot decide.")

    with capture_logs() as logs:
        result = await complete_prompt(provider, "?", instructions=["Decide."], schema=_Verdict)

    assert result.content == "I cannot decide."
    assert result.record.content == "I cannot decide."
    assert {"event": "helper_structured_output_unparsed", "schema": "_Verdict", "log_level": "warning"} in logs


@pytest.mark.asyncio
async def test_a_request_carries_the_callers_overrides_and_no_tools() -> None:
    """Per-call overrides reach the request, and a helper request offers no tools."""
    provider = _RecordingProvider(answer="ok")
    overrides = RequestOverrides(temperature=0.2, caller_retries=True)

    await complete_prompt(provider, "Thread: ...", instructions=["Summarize."], overrides=overrides)

    [request] = provider.requests
    assert (request.overrides, request.tools, request.tool_choice, request.output_schema) == (
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
        await complete_prompt(provider, "Thread: ...", instructions=["Summarize."])
