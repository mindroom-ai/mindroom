"""MindRoom compatibility for Mem0's OpenAI memory extractor."""

from __future__ import annotations

from typing import Any

from mem0.llms.openai import OpenAILLM

from mindroom.model_defaults import OPENAI_GPT_LUNA, OPENAI_GPT_TERRA

MEM0_OPENAI_MODELS_WITHOUT_TOP_P = frozenset({OPENAI_GPT_LUNA, OPENAI_GPT_TERRA})


class MindRoomMem0OpenAILLM(OpenAILLM):
    """Remove request parameters unsupported by specific OpenAI memory models."""

    def _get_common_params(self, **kwargs: object) -> dict[str, Any]:
        params = super()._get_common_params(**kwargs)
        if self.config.model in MEM0_OPENAI_MODELS_WITHOUT_TOP_P:
            params.pop("top_p", None)
        return params
