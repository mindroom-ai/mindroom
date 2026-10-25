"""Count the tokens of the extraction calls Mem0 makes with its own LLM client.

Mem0 sends its prompts through the provider SDK client it builds and keeps only the reply text, so the usage each
response reports is read off that client while a MindRoom memory write is in progress.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.llm.messages import Message, model_provenance, read_model_provenance
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# One assistant message per extraction call of the memory write in progress, so each call is its own request in
# usage reports; Mem0 runs its prompts in worker threads, which inherit this context.
_calls: ContextVar[list[Message] | None] = ContextVar("mem0_usage_calls", default=None)

_PROVIDER_LABELS = {"openai": "OpenAI", "openrouter": "OpenRouter", "anthropic": "Anthropic", "ollama": "Ollama"}


def _openai_usage(response: Any) -> MessageMetrics:  # noqa: ANN401 - an OpenAI SDK chat completion
    usage = response.usage
    if usage is None:
        return MessageMetrics()
    prompt_details = usage.prompt_tokens_details
    completion_details = usage.completion_tokens_details
    counts = TokenCounts(
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        cache_read_tokens=(prompt_details.cached_tokens or 0) if prompt_details else 0,
        reasoning_tokens=(completion_details.reasoning_tokens or 0) if completion_details else 0,
    )
    return MessageMetrics(tokens=counts.with_totals())


def _anthropic_usage(response: Any) -> MessageMetrics:  # noqa: ANN401 - an Anthropic SDK message
    usage = response.usage
    counts = TokenCounts(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )
    return MessageMetrics(tokens=counts.with_totals())


def _ollama_usage(response: Any) -> MessageMetrics:  # noqa: ANN401 - an Ollama client chat response
    counts = TokenCounts(input_tokens=response.prompt_eval_count or 0, output_tokens=response.eval_count or 0)
    return MessageMetrics(tokens=counts.with_totals())


def install_mem0_usage_capture(llm: Any, provider: str) -> None:  # noqa: ANN401 - a Mem0 LLM of the configured provider
    """Read token usage off the client a newly built Mem0 LLM of the MindRoom ``provider`` calls."""
    model_id, label = str(llm.config.model), _PROVIDER_LABELS.get(provider, provider)

    def counted(call: Callable[..., Any], read: Callable[[Any], MessageMetrics]) -> Callable[..., Any]:
        def counted_call(*args: object, **kwargs: object) -> object:
            response = call(*args, **kwargs)
            calls = _calls.get()
            if calls is not None and (metrics := read(response)).tokens.total_tokens:
                provenance = model_provenance(model_id, label)
                calls.append(Message(role="assistant", metrics=metrics, provider_data=provenance))
            return response

        return counted_call

    if provider in {"openai", "openrouter"}:
        completions = llm.client.chat.completions
        completions.create = counted(completions.create, _openai_usage)
    elif provider == "anthropic":
        llm.client.messages.create = counted(llm.client.messages.create, _anthropic_usage)
    elif provider == "ollama":
        llm.client.chat = counted(llm.client.chat, _ollama_usage)


@contextmanager
def counted_mem0_usage() -> Iterator[list[Message]]:
    """Collect the usage of every Mem0 extraction call made inside this block."""
    calls: list[Message] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def mem0_usage_run(calls: list[Message]) -> RunRecord | None:
    """Return the calls as one run to record, or None when none reported tokens."""
    if not calls:
        return None
    # One memory write runs every call on the configured memory model, which each call's message records.
    model = read_model_provenance(calls[0].provider_data)
    assert model is not None
    metrics = RunMetrics()
    for call in calls:
        metrics.add(call.metrics, model_id=model.model_id, provider=model.provider)
    return RunRecord(
        run_id=uuid4().hex,
        model=model.model_id,
        model_provider=model.provider,
        created_at=calls[0].created_at,
        metrics=metrics,
        messages=calls,
    )
