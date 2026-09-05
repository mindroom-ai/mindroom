"""MindRoom compatibility for Mem0's OpenAI memory extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import OPENAI_GPT_LUNA, OPENAI_GPT_TERRA

if TYPE_CHECKING:
    from collections.abc import Callable

    from mem0.llms.openai import OpenAILLM

_MEM0_OPENAI_MODELS_WITHOUT_TOP_P = frozenset({OPENAI_GPT_LUNA, OPENAI_GPT_TERRA})


def _without_top_p(create: Callable[..., object]) -> Callable[..., object]:
    """Return a completion callable that omits the unsupported ``top_p`` parameter."""

    def create_without_top_p(*args: object, **kwargs: object) -> object:
        kwargs.pop("top_p", None)
        return create(*args, **kwargs)

    return create_without_top_p


def install_mem0_openai_compatibility(llm: OpenAILLM) -> None:
    """Install model-specific request filtering on an existing Mem0 OpenAI LLM."""
    if llm.config.model not in _MEM0_OPENAI_MODELS_WITHOUT_TOP_P:
        return
    completions = llm.client.chat.completions
    completions.create = _without_top_p(  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        completions.create,
    )
