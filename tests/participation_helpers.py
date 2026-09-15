"""Deterministic provider boundary for participation tests."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agno.models.response import ModelResponse

from mindroom.synthetic_model import SyntheticModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message


class ParticipationModel(SyntheticModel):
    """Provider boundary double; real Agno response/tool loop remains in use."""

    def __init__(self, decision: ModelResponse | BaseException) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.decision = decision
        self.requests: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> ModelResponse:
        """Capture provider inputs and return the next deterministic response."""
        self.requests.append(
            {
                "messages": deepcopy(messages),
                **{
                    key: deepcopy(kwargs.get(key))
                    for key in ("tools", "tool_choice", "response_format", "compress_tool_results")
                },
            },
        )
        if len(self.requests) == 1:
            if isinstance(self.decision, BaseException):
                raise self.decision
            return self.decision
        return ModelResponse(content="Useful answer")

    async def ainvoke_stream(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        """Stream the next deterministic response through the normal model loop."""
        yield await self.ainvoke(messages, **kwargs)
