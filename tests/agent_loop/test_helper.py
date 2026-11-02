"""One-shot helper calls send one tool-less request and return the answer with the call's usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_prompt
from mindroom.agent_loop.structured import output_format_prompt
from mindroom.llm.errors import ProviderUnavailable
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, Message, ModelProvenance
from mindroom.llm.provider import (
    STRUCTURED_OUTPUT_TOOL,
    Capabilities,
    ModelIdentity,
    ModelRequest,
    RequestOverrides,
    StreamEvent,
    TextDelta,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.assistant_turn import stamp_model
from tests.agno_parity.golden_files import turn_message
from tests.fakes.token_counter import fake_token_counter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import TokenCounter

IDENTITY = ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic")
LUNA = ModelIdentity(id="gpt-6-luna", provider="OpenAI")
_ANSWERED_AT = 1_790_000_000


@dataclass
class _RecordingProvider:
    """Answer every request with one canned turn, stamped as an adapter stamps it, and keep the requests."""

    answer: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    identity: ModelIdentity = IDENTITY
    native_history_route: None = None
    token_counter: TokenCounter = field(default_factory=fake_token_counter)
    model_params: dict[str, object] = field(default_factory=dict)
    finish: bool = True
    sums_attempts: bool = False
    requests: list[ModelRequest] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        # A ``forced_tool`` route answers a structured request with the forced call, its arguments the answer.
        forced = request.output_schema is not None and self.capabilities.structured_output == "forced_tool"
        if not forced:
            yield TextDelta(self.answer)
        if not self.finish:
            return
        usage = MessageMetrics(tokens=TokenCounts(input_tokens=12, output_tokens=5, total_tokens=17))
        yield UsageReported(usage)
        call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": STRUCTURED_OUTPUT_TOOL, "arguments": self.answer},
        }
        message = (
            Message(role="assistant", tool_calls=[call], metrics=usage, created_at=_ANSWERED_AT)
            if forced
            else Message(role="assistant", content=self.answer, metrics=usage, created_at=_ANSWERED_AT)
        )
        if self.sums_attempts:
            # The retrying stage marks a finished turn whose usage adds up failed attempts.
            message.provider_data = {AGGREGATE_USAGE_KEY: True}
        stamp_model(message, self.identity)
        yield TurnCompleted(message=message, stop_reason="end")


class _Verdict(BaseModel):
    answer: str
    confidence: int | None = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "json_prompt"),
    [("native", False), ("json_schema", False), ("forced_tool", False), ("json_mode", True)],
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
    assert result.usage.run_id == "decide-1"
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
async def test_a_text_answer_returns_its_text_and_the_calls_usage() -> None:
    """Without a schema the answer is the text, and the usage names the model and holds its one request's report."""
    provider = _RecordingProvider(answer="The user prefers aisle seats.", identity=LUNA)

    result = await complete_prompt(
        provider,
        "Conversation excerpt: ...",
        instructions=["Extract durable memories."],
        run_id="flush-1",
    )

    assert result.content == "The user prefers aisle seats."
    usage = result.usage
    assert (usage.run_id, usage.model, usage.provider) == ("flush-1", "gpt-6-luna", "OpenAI")
    totals = usage.metrics.to_dict()
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (12, 5, 17)
    [details] = totals["details"]["model"]
    assert (details["id"], details["provider"], details["total_tokens"]) == ("gpt-6-luna", "OpenAI", 17)
    [request] = usage.requests
    assert request.model == ModelProvenance(model_id="gpt-6-luna", provider="OpenAI")
    assert (request.created_at, request.metrics.tokens.total_tokens) == (_ANSWERED_AT, 17)


@pytest.mark.asyncio
async def test_a_retried_request_counts_toward_the_usage_but_is_not_listed_as_a_request() -> None:
    """A reply whose usage sums retried attempts describes no one request, so only the totals count it."""
    provider = _RecordingProvider(answer="ok", sums_attempts=True)

    result = await complete_prompt(provider, "Thread: ...", instructions=["Summarize."])

    assert result.usage.metrics.tokens.total_tokens == 17
    assert result.usage.requests == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_output", ["native", "json_schema", "forced_tool", "json_mode"])
async def test_a_structured_answer_is_read_into_the_schema(structured_output: str) -> None:
    """The request asks for the schema the provider's way, and the answer comes back as the model."""
    capabilities = Capabilities(structured_output=structured_output)
    provider = _RecordingProvider(answer='{"answer": "yes"}', capabilities=capabilities)

    result = await complete_prompt(provider, "Is it raining?", instructions=["Decide."], schema=_Verdict)

    assert result.content == _Verdict(answer="yes")
    [request] = provider.requests
    assert request.output_schema is _Verdict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "repaired"),
    [("native", False), ("json_schema", False), ("forced_tool", False), ("json_mode", True)],
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
