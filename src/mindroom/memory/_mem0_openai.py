"""MindRoom compatibility for Mem0's OpenAI memory extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import OPENAI_PROVIDER_DEFAULT_SAMPLING_MODEL_PREFIXES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mem0.llms.openai import OpenAILLM

# Mem0 sends temperature, top_p, and max_tokens to every model it does not list as a reasoning model. GPT-5 and GPT-6
# models reject max_tokens on Chat Completions and most of them reject those sampling values, so they get what Mem0
# sends its known reasoning models: no sampling controls and no max_tokens, which as max_completion_tokens would also
# cap their reasoning.
_REASONING_MODEL_UNSUPPORTED = frozenset({"temperature", "top_p", "max_tokens"})


def _without_unsupported_parameters(create: Callable[..., object]) -> Callable[..., object]:
    """Return a completion callable that omits the parameters these models reject."""

    def create_without_parameters(*args: object, **kwargs: object) -> object:
        for name in _REASONING_MODEL_UNSUPPORTED:
            kwargs.pop(name, None)
        return create(*args, **kwargs)

    return create_without_parameters


def install_mem0_openai_compatibility(llm: OpenAILLM) -> None:
    """Install model-specific request filtering on an existing Mem0 OpenAI LLM."""
    model = llm.config.model
    if not isinstance(model, str) or not model.startswith(OPENAI_PROVIDER_DEFAULT_SAMPLING_MODEL_PREFIXES):
        return
    completions = llm.client.chat.completions
    completions.create = _without_unsupported_parameters(  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        completions.create,
    )
