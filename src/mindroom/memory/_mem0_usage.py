"""Count the tokens of the extraction calls Mem0 makes with its own LLM client.

Mem0 sends its prompts through the provider SDK client it builds and keeps only the reply text, so the usage each
response reports is read off that client while a MindRoom memory write is in progress.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from time import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.llm.messages import ModelProvenance
from mindroom.llm.usage import HelperUsage, MessageMetrics, RequestUsage, RunMetrics, TokenCounts

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The usage of each extraction call of the memory write in progress, so each call is its own request in usage
# reports; Mem0 runs its prompts in worker threads, which inherit this context.
_calls: ContextVar[list[RequestUsage] | None] = ContextVar("mem0_usage_calls", default=None)

# The provider name budgets price each Mem0 client's usage by: that of the MindRoom route with the same name.
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
    model = ModelProvenance(model_id=str(llm.config.model), provider=_PROVIDER_LABELS.get(provider, provider))

    def counted(call: Callable[..., Any], read: Callable[[Any], MessageMetrics]) -> Callable[..., Any]:
        def counted_call(*args: object, **kwargs: object) -> object:
            response = call(*args, **kwargs)
            calls = _calls.get()
            if calls is not None and (metrics := read(response)).tokens.total_tokens:
                calls.append(RequestUsage(created_at=int(time()), metrics=metrics, model=model))
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
def counted_mem0_usage() -> Iterator[list[RequestUsage]]:
    """Collect the usage of every Mem0 extraction call made inside this block."""
    calls: list[RequestUsage] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def mem0_usage(calls: list[RequestUsage]) -> HelperUsage | None:
    """Return the calls' usage to record, or None when none reported tokens."""
    if not calls:
        return None
    # One memory write runs every call on the configured memory model, which each call records.
    model = calls[0].model
    assert model is not None
    metrics = RunMetrics()
    for call in calls:
        metrics.add(call.metrics, model_id=model.model_id, provider=model.provider)
    return HelperUsage(
        run_id=uuid4().hex,
        model=model.model_id,
        provider=model.provider,
        metrics=metrics,
        requests=tuple(calls),
        created_at=calls[0].created_at,
    )
