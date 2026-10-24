"""Built-in synthetic provider for local conversations and load generation, on the provider protocol.

It streams seeded Lorem Ipsum at a fixed rate and occasionally calls the shell tool, so a response replays
exactly from the seed and the conversation's user messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from mindroom.llm.provider import Capabilities, ModelIdentity, TextDelta, ToolCallReady, TurnCompleted
from mindroom.providers.assistant_turn import finished_assistant_message
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent

_PROVIDER = "Synthetic"
_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex "
    "ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat "
    "nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim "
    "id est laborum. "
)
SHELL_TOOL = "run_shell_command"


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticSettings:
    """The ``extra_kwargs`` a synthetic model accepts."""

    seed: int = 1
    min_response_chars: int = 320
    max_response_chars: int = 960
    chunk_chars: int = 40
    chars_per_second: float = 80.0
    tool_call_probability: float = 0.2

    def __post_init__(self) -> None:
        """Reject generation settings that cannot produce a response."""
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


@dataclass(frozen=True, slots=True)
class _ResponsePlan:
    """One conversation's response, split around its shell call; a response without one is all prefix."""

    prefix: str
    suffix: str
    tool_call_id: str | None


def _plan_response(settings: SyntheticSettings, user_history: Sequence[str], *, shell_available: bool) -> _ResponsePlan:
    """Return the response for the seed and the user messages so far; the plan is the same on every replay."""
    digest = hashlib.sha256(f"{settings.seed}\0{'\0'.join(user_history)}".encode()).digest()
    randomizer = random.Random(int.from_bytes(digest))  # noqa: S311 - replayable synthetic behavior
    response_chars = randomizer.randint(settings.min_response_chars, settings.max_response_chars)
    body = (_LOREM * ((response_chars // len(_LOREM)) + 1))[:response_chars]
    if not shell_available or randomizer.random() >= settings.tool_call_probability:
        return _ResponsePlan(prefix=body, suffix="", tool_call_id=None)
    split_at = randomizer.randint(1, response_chars - 1)
    return _ResponsePlan(prefix=body[:split_at], suffix=body[split_at:], tool_call_id=f"synthetic-{digest.hex()[:16]}")


def _shell_call(tool_call_id: str) -> dict[str, Any]:
    """Return the stored-shape tool call that runs ``echo hi`` through the shell tool."""
    arguments = json.dumps({"args": ["echo", "hi"]}, separators=(",", ":"))
    return {"id": tool_call_id, "type": "function", "function": {"name": SHELL_TOOL, "arguments": arguments}}


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticProvider:
    """A model that needs no network: the reply depends only on the seed and the user messages."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: SyntheticSettings
    # The synthetic model keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Stream the planned text at the configured rate, then the shell call if this turn makes one.

        After the shell call's result, the next request streams the rest of the same response. A request with
        ``stream=False`` waits as long and yields only the finished turn.
        """
        settings = self.settings
        shell_available = request.tool_choice != "none" and any(tool.name == SHELL_TOOL for tool in request.tools)
        user_history = [message.get_content_string() for message in request.messages if message.role == "user"]
        plan = _plan_response(settings, user_history, shell_available=shell_available)
        last_message = request.messages[-1] if request.messages else None
        continuation = (
            plan.tool_call_id is not None
            and last_message is not None
            and last_message.role == "tool"
            and last_message.tool_call_id == plan.tool_call_id
        )
        content = plan.suffix if continuation else plan.prefix
        started = time.monotonic()
        first_text_at: float | None = None
        for start in range(0, len(content), settings.chunk_chars):
            chunk = content[start : start + settings.chunk_chars]
            if settings.chars_per_second:
                await asyncio.sleep(len(chunk) / settings.chars_per_second)
            if first_text_at is None:
                first_text_at = time.monotonic()
            if request.stream:
                yield TextDelta(chunk)
        tool_calls = [] if continuation or plan.tool_call_id is None else [_shell_call(plan.tool_call_id)]
        if request.stream:
            for call in tool_calls:
                yield ToolCallReady(call)
        message = finished_assistant_message(
            self.identity,
            content=content or None,
            tool_calls=tool_calls,
            usage=None,
            started=started,
            first_output_at=first_text_at,
        )
        yield TurnCompleted(message=message, stop_reason="tool_use" if tool_calls else "end")


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> SyntheticProvider:
    """Build the adapter for a ``synthetic`` model config; a configured API key is ignored."""
    del runtime_paths
    extra_kwargs = {key: value for key, value in (model_config.extra_kwargs or {}).items() if key != "api_key"}
    return SyntheticProvider(
        identity=ModelIdentity(id=model_config.id, provider=_PROVIDER, provider_name=_PROVIDER),
        capabilities=Capabilities(),
        settings=settings_from_extra_kwargs(SyntheticSettings, extra_kwargs, provider=model_config.provider),
    )


__all__ = ["SHELL_TOOL", "SyntheticProvider", "SyntheticSettings", "build_provider"]
