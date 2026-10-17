"""MindRoom compatibility for Mem0's OpenAI memory extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import OPENAI_MAX_TOKENS_UNSUPPORTED_PREFIXES, OPENAI_UNSUPPORTED_SAMPLING_CONTROLS

if TYPE_CHECKING:
    from collections.abc import Callable

    from mem0.llms.openai import OpenAILLM


def _without_parameters(create: Callable[..., object], unsupported: frozenset[str]) -> Callable[..., object]:
    """Return a completion callable that omits the model's unsupported parameters."""

    def create_without_parameters(*args: object, **kwargs: object) -> object:
        for name in unsupported:
            kwargs.pop(name, None)
        return create(*args, **kwargs)

    return create_without_parameters


def install_mem0_openai_compatibility(llm: OpenAILLM) -> None:
    """Install model-specific request filtering on an existing Mem0 OpenAI LLM."""
    model = llm.config.model
    if not isinstance(model, str):
        return
    unsupported = OPENAI_UNSUPPORTED_SAMPLING_CONTROLS.get(model, frozenset())
    if model.startswith(OPENAI_MAX_TOKENS_UNSUPPORTED_PREFIXES):
        # Mem0 sends max_tokens to every model it does not list as a reasoning model and omits it for those it does;
        # renaming it to max_completion_tokens would also cap these models' reasoning and could cut the answer off.
        unsupported |= {"max_tokens"}
    if not unsupported:
        return
    completions = llm.client.chat.completions
    completions.create = _without_parameters(  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        completions.create,
        unsupported,
    )
