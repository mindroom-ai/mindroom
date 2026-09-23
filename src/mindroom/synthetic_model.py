"""Built-in synthetic model for local conversations and load generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.base import Model
from agno.models.response import ModelResponse

if TYPE_CHECKING:
    from agno.models.message import Message

_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex "
    "ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat "
    "nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim "
    "id est laborum. "
)
_SHELL_TOOL = "run_shell_command"


@dataclass(frozen=True, slots=True)
class _ResponsePlan:
    body: str
    split_at: int | None
    tool_call_id: str | None

    @property
    def prefix(self) -> str:
        return self.body if self.split_at is None else self.body[: self.split_at]

    @property
    def suffix(self) -> str:
        return "" if self.split_at is None else self.body[self.split_at :]


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

    def __post_init__(self) -> None:
        """Validate configured response generation settings."""
        super().__post_init__()
        if self.min_response_chars < 2:
            msg = "min_response_chars must be at least 2"
            raise ValueError(msg)
        if self.max_response_chars < self.min_response_chars:
            msg = "max_response_chars must be greater than or equal to min_response_chars"
            raise ValueError(msg)
        if self.chunk_chars < 1:
            msg = "chunk_chars must be positive"
            raise ValueError(msg)
        if self.chars_per_second < 0:
            msg = "chars_per_second must be non-negative"
            raise ValueError(msg)
        if not 0 <= self.tool_call_probability <= 1:
            msg = "tool_call_probability must be between 0 and 1"
            raise ValueError(msg)

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
    ) -> tuple[_ResponsePlan, bool]:
        plan = self._plan(messages, shell_available=tool_choice != "none" and self._tool_available(tools))
        last_message = messages[-1] if messages else None
        continuation = (
            plan.tool_call_id is not None
            and last_message is not None
            and last_message.role == self.tool_message_role
            and last_message.tool_call_id == plan.tool_call_id
        )
        return plan, continuation

    def _plan(self, messages: Sequence[Message], *, shell_available: bool) -> _ResponsePlan:
        user_history = "\0".join(message.get_content_string() for message in messages if message.role == "user")
        digest = hashlib.sha256(f"{self.seed}\0{user_history}".encode()).digest()
        randomizer = random.Random(int.from_bytes(digest))  # noqa: S311 - replayable synthetic behavior
        response_chars = randomizer.randint(self.min_response_chars, self.max_response_chars)
        body = (_LOREM * ((response_chars // len(_LOREM)) + 1))[:response_chars]
        if not shell_available or randomizer.random() >= self.tool_call_probability:
            return _ResponsePlan(body=body, split_at=None, tool_call_id=None)
        return _ResponsePlan(
            body=body,
            split_at=randomizer.randint(1, response_chars - 1),
            tool_call_id=f"synthetic-{digest.hex()[:16]}",
        )

    @staticmethod
    def _tool_available(tools: Sequence[Mapping[str, Any]] | None) -> bool:
        for tool in tools or ():
            function = tool.get("function")
            if isinstance(function, Mapping) and function.get("name") == _SHELL_TOOL:
                return True
            if tool.get("name") == _SHELL_TOOL:
                return True
        return False

    @staticmethod
    def _tool_calls(plan: _ResponsePlan, continuation: bool) -> list[dict[str, Any]]:
        if continuation or plan.tool_call_id is None:
            return []
        return [
            {
                "id": plan.tool_call_id,
                "type": "function",
                "function": {
                    "name": _SHELL_TOOL,
                    "arguments": json.dumps({"args": ["echo", "hi"]}, separators=(",", ":")),
                },
            },
        ]

    def _chunks(self, content: str) -> Iterator[str]:
        for start in range(0, len(content), self.chunk_chars):
            yield content[start : start + self.chunk_chars]

    def _sleep_sync(self, character_count: int) -> None:
        if self.chars_per_second:
            time.sleep(character_count / self.chars_per_second)

    async def _sleep_async(self, character_count: int) -> None:
        if self.chars_per_second:
            await asyncio.sleep(character_count / self.chars_per_second)


__all__ = ["SyntheticModel"]
