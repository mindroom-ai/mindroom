"""Built-in synthetic model for the Agno-backed runtime; the plan itself lives in ``synthetic_adapter``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.models.base import Model
from agno.models.response import ModelResponse

from mindroom.providers.synthetic_adapter import SHELL_TOOL, ResponsePlan, SyntheticSettings, plan_response, shell_call

if TYPE_CHECKING:
    from agno.models.message import Message


@dataclass
class SyntheticModel(Model):
    """Stream seeded Lorem Ipsum and occasionally call the configured shell tool."""

    name: str | None = "Synthetic"
    provider: str | None = "Synthetic"
    seed: int = 1
    min_response_chars: int = 320
    max_response_chars: int = 960
    chunk_chars: int = 40
    chars_per_second: float = 80.0
    tool_call_probability: float = 0.2
    # The validated settings every response reads; the fields above are only the constructor surface.
    _settings: SyntheticSettings = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the configured response generation settings through the owned settings type."""
        super().__post_init__()
        self._settings = SyntheticSettings(
            seed=self.seed,
            min_response_chars=self.min_response_chars,
            max_response_chars=self.max_response_chars,
            chunk_chars=self.chunk_chars,
            chars_per_second=self.chars_per_second,
            tool_call_probability=self.tool_call_probability,
        )

    def invoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> ModelResponse:
        """Return one rate-limited synthetic response phase."""
        plan, continuation = self._execution(messages, tools, tool_choice)
        content = plan.suffix if continuation else plan.prefix
        self._sleep_sync(len(content))
        return ModelResponse(
            content=content,
            tool_calls=self._tool_calls(plan, continuation),
        )

    async def ainvoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> ModelResponse:
        """Return one asynchronously rate-limited synthetic response phase."""
        plan, continuation = self._execution(messages, tools, tool_choice)
        content = plan.suffix if continuation else plan.prefix
        await self._sleep_async(len(content))
        return ModelResponse(
            content=content,
            tool_calls=self._tool_calls(plan, continuation),
        )

    def invoke_stream(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> Iterator[ModelResponse]:
        """Stream one synthetic response phase at the configured rate."""
        plan, continuation = self._execution(messages, tools, tool_choice)
        content = plan.suffix if continuation else plan.prefix
        for chunk in self._chunks(content):
            self._sleep_sync(len(chunk))
            yield ModelResponse(content=chunk)
        if tool_calls := self._tool_calls(plan, continuation):
            yield ModelResponse(tool_calls=tool_calls)

    async def ainvoke_stream(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        """Asynchronously stream one synthetic response phase at the configured rate."""
        plan, continuation = self._execution(messages, tools, tool_choice)
        content = plan.suffix if continuation else plan.prefix
        for chunk in self._chunks(content):
            await self._sleep_async(len(chunk))
            yield ModelResponse(content=chunk)
        if tool_calls := self._tool_calls(plan, continuation):
            yield ModelResponse(tool_calls=tool_calls)

    def _parse_provider_response(self, response: object, **_kwargs: object) -> ModelResponse:
        if not isinstance(response, ModelResponse):
            msg = "SyntheticModel only parses ModelResponse values"
            raise TypeError(msg)
        return response

    def _parse_provider_response_delta(self, response: object) -> ModelResponse:
        if not isinstance(response, ModelResponse):
            msg = "SyntheticModel only parses ModelResponse values"
            raise TypeError(msg)
        return response

    def _execution(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None,
        tool_choice: str | Mapping[str, Any] | None,
    ) -> tuple[ResponsePlan, bool]:
        user_history = [message.get_content_string() for message in messages if message.role == "user"]
        shell_available = tool_choice != "none" and self._tool_available(tools)
        plan = plan_response(self._settings, user_history, shell_available=shell_available)
        last_message = messages[-1] if messages else None
        continuation = (
            plan.tool_call_id is not None
            and last_message is not None
            and last_message.role == self.tool_message_role
            and last_message.tool_call_id == plan.tool_call_id
        )
        return plan, continuation

    @staticmethod
    def _tool_available(tools: Sequence[Mapping[str, Any]] | None) -> bool:
        for tool in tools or ():
            function = tool.get("function")
            if isinstance(function, Mapping) and function.get("name") == SHELL_TOOL:
                return True
            if tool.get("name") == SHELL_TOOL:
                return True
        return False

    @staticmethod
    def _tool_calls(plan: ResponsePlan, continuation: bool) -> list[dict[str, Any]]:
        return [] if continuation or plan.tool_call_id is None else [shell_call(plan.tool_call_id)]

    def _chunks(self, content: str) -> Iterator[str]:
        chunk_chars = self._settings.chunk_chars
        for start in range(0, len(content), chunk_chars):
            yield content[start : start + chunk_chars]

    def _sleep_sync(self, character_count: int) -> None:
        if chars_per_second := self._settings.chars_per_second:
            time.sleep(character_count / chars_per_second)

    async def _sleep_async(self, character_count: int) -> None:
        if chars_per_second := self._settings.chars_per_second:
            await asyncio.sleep(character_count / chars_per_second)


__all__ = ["SyntheticModel"]
