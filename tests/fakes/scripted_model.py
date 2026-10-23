"""A ``ModelProvider`` that replays scripted turns and records every request it receives."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeHistoryRoute,
    PortableReplayEstimator,
    StreamEvent,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.providers.assistant_turn import stamp_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence


@dataclass(frozen=True, slots=True)
class Await:
    """A scripted step the model awaits between events, so a turn can wait to be cancelled or raise mid-stream."""

    run: Callable[[], Awaitable[None]]


type ScriptStep = StreamEvent | Await

SYNTHETIC = ModelIdentity(id="scripted-model", provider="Synthetic", provider_name="Synthetic")


@dataclass
class ScriptedModel:
    """Replay one scripted turn per request, in order; ``requests`` keeps every request as the loop sent it.

    Each finished turn is stamped with the model's identity, as every adapter stamps the turns it finishes.
    """

    turns: list[Sequence[ScriptStep]]
    identity: ModelIdentity = SYNTHETIC
    capabilities: Capabilities = field(default_factory=Capabilities)
    requests: list[ModelRequest] = field(default_factory=list)
    # A scripted route keeps no provider-side history, sizes replayed history canonically, and records no settings.
    native_history_route: NativeHistoryRoute | None = None
    replay_estimator: PortableReplayEstimator | None = None
    model_params: dict[str, Any] = field(default_factory=dict)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the next scripted turn's events."""
        self.requests.append(request)
        assert self.turns, f"ScriptedModel has no turn scripted for request {len(self.requests)}"
        for step in self.turns.pop(0):
            if isinstance(step, Await):
                await step.run()
                continue
            if isinstance(step, TurnCompleted):
                stamp_model(step.message, self.identity)
            yield step


def usage(input_tokens: int, output_tokens: int, **counters: float) -> MessageMetrics:
    """Return one request's usage; ``total_tokens`` is the sum unless given."""
    counters.setdefault("total_tokens", input_tokens + output_tokens)
    return MessageMetrics(input_tokens=input_tokens, output_tokens=output_tokens, **counters)  # type: ignore[arg-type]


def tool_call(function: str, call_id: str, /, **arguments: object) -> dict[str, Any]:
    """Return one tool call of ``function`` in the stored dict shape; ``arguments`` may use any name."""
    return {"id": call_id, "type": "function", "function": {"name": function, "arguments": json.dumps(arguments)}}


def raw_tool_call(function: str, call_id: str, raw_arguments: str, /) -> dict[str, Any]:
    """Return one tool call whose arguments string is exactly ``raw_arguments``."""
    return {"id": call_id, "type": "function", "function": {"name": function, "arguments": raw_arguments}}


def answer(text: str, metrics: MessageMetrics | None = None, *, chunk: int = 4) -> list[ScriptStep]:
    """Return a turn that streams ``text`` in chunks and completes with it."""
    steps: list[ScriptStep] = [TextDelta(text[start : start + chunk]) for start in range(0, len(text), chunk)]
    final = metrics or MessageMetrics()
    steps.append(UsageReported(final))
    steps.append(TurnCompleted(message=Message(role="assistant", content=text, metrics=final), stop_reason="end"))
    return steps


def calls(
    *tool_calls: dict[str, Any],
    content: str | None = None,
    metrics: MessageMetrics | None = None,
) -> list[ScriptStep]:
    """Return a turn that streams ``content`` and completes requesting ``tool_calls``."""
    steps: list[ScriptStep] = [] if content is None else [TextDelta(content)]
    steps.extend(ToolCallReady(call) for call in tool_calls)
    final = metrics or MessageMetrics()
    steps.append(UsageReported(final))
    steps.append(
        TurnCompleted(
            message=Message(role="assistant", content=content, tool_calls=list(tool_calls), metrics=final),
            stop_reason="tool_use",
        ),
    )
    return steps


__all__ = [
    "SYNTHETIC",
    "Await",
    "ScriptStep",
    "ScriptedModel",
    "answer",
    "calls",
    "raw_tool_call",
    "tool_call",
    "usage",
]
