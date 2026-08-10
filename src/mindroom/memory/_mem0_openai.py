"""MindRoom compatibility for Mem0's OpenAI memory extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mem0.llms.openai import OpenAILLM

from mindroom.model_defaults import OPENAI_GPT_LUNA, OPENAI_GPT_TERRA

if TYPE_CHECKING:
    from mem0.configs.llms.base import BaseLlmConfig

MEM0_OPENAI_MODELS_WITHOUT_TOP_P = frozenset({OPENAI_GPT_LUNA, OPENAI_GPT_TERRA})


class _TopPFilteringCompletions:
    """Filter unsupported sampling controls at the final provider boundary."""

    def __init__(self, completions: object) -> None:
        self._completions = completions

    def create(self, *args: object, **kwargs: object) -> object:
        kwargs.pop("top_p", None)
        return cast("Any", self._completions).create(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._completions, name)


class _TopPFilteringChat:
    """Preserve chat APIs while replacing only its completions resource."""

    def __init__(self, chat: object) -> None:
        self._chat = chat
        self.completions = _TopPFilteringCompletions(cast("Any", chat).completions)

    def __getattr__(self, name: str) -> object:
        return getattr(self._chat, name)


class _TopPFilteringClient:
    """Preserve OpenAI client APIs while filtering chat completions."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.chat = _TopPFilteringChat(cast("Any", client).chat)

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


class MindRoomMem0OpenAILLM(OpenAILLM):
    """Remove request parameters unsupported by specific OpenAI memory models."""

    def __init__(self, config: BaseLlmConfig) -> None:
        super().__init__(config)
        if self.config.model in MEM0_OPENAI_MODELS_WITHOUT_TOP_P:
            self.client = cast("Any", _TopPFilteringClient(self.client))
