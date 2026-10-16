"""Count the tokens of the extraction calls Mem0 makes with its own LLM client.

Mem0 sends its prompts through the provider SDK client it builds and keeps only the reply text, so the usage each
response reports is read off that client while a MindRoom memory write is in progress.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The calls of the memory write in progress; Mem0 runs its prompts in worker threads, which inherit this context.
_calls: ContextVar[list[ModelMetrics] | None] = ContextVar("mem0_usage_calls", default=None)

_PROVIDER_LABELS = {"openai": "OpenAI", "openrouter": "OpenRouter", "anthropic": "Anthropic", "ollama": "Ollama"}


def _openai_usage(response: Any) -> tuple[int, int, int]:  # noqa: ANN401 - an OpenAI SDK chat completion
    usage = response.usage
    if usage is None:
        return 0, 0, 0
    details = usage.prompt_tokens_details
    return usage.prompt_tokens or 0, usage.completion_tokens or 0, (details.cached_tokens or 0) if details else 0


def _anthropic_usage(response: Any) -> tuple[int, int, int]:  # noqa: ANN401 - an Anthropic SDK message
    usage = response.usage
    return usage.input_tokens or 0, usage.output_tokens or 0, usage.cache_read_input_tokens or 0


def _ollama_usage(response: Any) -> tuple[int, int, int]:  # noqa: ANN401 - an Ollama client chat response
    return response.prompt_eval_count or 0, response.eval_count or 0, 0


class _CountedCall:
    """A client call that records the usage its responses report while a memory write is in progress."""

    def __init__(
        self,
        call: Callable[..., Any],
        read: Callable[[Any], tuple[int, int, int]],
        model: ModelMetrics,
    ) -> None:
        self._call = call
        self._read = read
        self._model = model

    def __call__(self, *args: object, **kwargs: object) -> object:
        response = self._call(*args, **kwargs)
        if (calls := _calls.get()) is not None:
            input_tokens, output_tokens, cache_read_tokens = self._read(response)
            calls.append(
                ModelMetrics(
                    id=self._model.id,
                    provider=self._model.provider,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cache_read_tokens=cache_read_tokens,
                ),
            )
        return response


def _counted(
    call: Callable[..., Any],
    read: Callable[[Any], tuple[int, int, int]],
    model: ModelMetrics,
) -> _CountedCall:
    # Installing twice on one client would count every call twice.
    return call if isinstance(call, _CountedCall) else _CountedCall(call, read, model)


def install_mem0_usage_capture(llm: Any, provider: str) -> None:  # noqa: ANN401 - a Mem0 LLM of the configured provider
    """Read token usage off the client a Mem0 LLM of the MindRoom ``provider`` calls."""
    model = ModelMetrics(id=str(llm.config.model), provider=_PROVIDER_LABELS.get(provider, provider))
    if provider in {"openai", "openrouter"}:
        completions = llm.client.chat.completions
        completions.create = _counted(completions.create, _openai_usage, model)
    elif provider == "anthropic":
        llm.client.messages.create = _counted(llm.client.messages.create, _anthropic_usage, model)
    elif provider == "ollama":
        llm.client.chat = _counted(llm.client.chat, _ollama_usage, model)


@contextmanager
def counted_mem0_usage() -> Iterator[list[ModelMetrics]]:
    """Collect the usage of every Mem0 extraction call made inside this block."""
    calls: list[ModelMetrics] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def mem0_usage_run(calls: list[ModelMetrics]) -> RunOutput | None:
    """Return the calls' usage as one run to record, or None when they reported no tokens."""
    models: dict[tuple[str, str], ModelMetrics] = {}
    for call in calls:
        models.setdefault((call.id, call.provider), ModelMetrics(id=call.id, provider=call.provider)).accumulate(call)
    metrics = RunMetrics(details={"model": list(models.values())})
    for call in calls:
        metrics.input_tokens += call.input_tokens
        metrics.output_tokens += call.output_tokens
        metrics.total_tokens += call.total_tokens
        metrics.cache_read_tokens += call.cache_read_tokens
    if not metrics.total_tokens:
        return None
    return RunOutput(run_id=uuid4().hex, metrics=metrics)
