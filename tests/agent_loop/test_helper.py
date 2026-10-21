"""One-shot helper calls send the helper goldens' requests and return the answer with a usage-ready run record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_once
from mindroom.agent_loop.prompt import PromptSpec, render_system_message
from mindroom.agent_loop.structured import select_response_format
from mindroom.llm.errors import ProviderError
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
from tests.agent_loop.prompt_parity_support import run_scenario
from tests.agno_parity.prompt_scenarios import (
    SCENARIOS,
    load_golden,
    message_dict,
    normalize,
    response_format_dict,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

HELPER_GOLDENS = sorted(name for name in SCENARIOS if name.startswith("helper_"))
IDENTITY = ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic", provider_name="Anthropic")


@dataclass
class _RecordingProvider:
    """Answer every request with one canned turn and keep the requests."""

    answer: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    identity: ModelIdentity = IDENTITY
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

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        del messages
        return list(results)


class _Verdict(BaseModel):
    answer: str
    confidence: int | None = None


def _messages(spec: PromptSpec, prompt: str) -> list[Message]:
    return [render_system_message(spec), Message(role="user", content=prompt)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HELPER_GOLDENS)
async def test_helper_requests_equal_the_goldens(name: str, tmp_path: Path) -> None:
    """A helper's owned request carries the golden's messages and response format on every structured-output path."""
    [expected] = load_golden(name)["requests"]
    run = await run_scenario(name, tmp_path)
    [prompt] = run.prompts
    [call] = run.calls
    response_format = (
        None if prompt.output_schema is None else select_response_format(prompt.capabilities, prompt.output_schema)
    )
    provider = _RecordingProvider(answer="ok", capabilities=prompt.capabilities)
    # The helper's call site writes its user message; the request takes it as the Agno-era call site sent it.
    prompts = [Message.from_dict(message.to_dict()) for message in call.messages[1:]]

    await complete_once(provider, [render_system_message(prompt.spec), *prompts], response_format=response_format)

    [request] = provider.requests
    assert normalize([message_dict(message) for message in request.messages], tmp_path) == expected["messages"]
    assert response_format_dict(request.response_format) == expected["response_format"]
    assert (request.tools, request.tool_choice) == ((), None)
    assert request.stream is expected["stream"]


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
    overrides = RequestOverrides(temperature=0.2, sdk_max_retries=0)

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

    with pytest.raises(ProviderError) as raised:
        await complete_once(provider, _messages(PromptSpec(role="Summarize."), "Thread: ..."))

    assert raised.value.code == "incomplete_stream"
