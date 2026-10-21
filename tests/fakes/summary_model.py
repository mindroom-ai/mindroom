"""Compaction summary models: a ``ScriptedModel`` that answers each summary request with its next reply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, TurnCompleted, UsageReported
from mindroom.llm.usage import MessageMetrics
from tests.fakes.scripted_model import Await, ScriptedModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.provider import ModelRequest, StopReason, StreamEvent
    from tests.fakes.scripted_model import ScriptStep

# A reply is the summary text, a finished turn, an exception the request raises, or the scripted events of the
# request, so a test can block or wait for cancellation with an ``Await`` step before (or instead of) the turn.
type Reply = str | TurnCompleted | BaseException | Sequence[ScriptStep]


def summary_turn(
    text: str | None,
    *,
    usage: MessageMetrics | None = None,
    stop_reason: StopReason | None = "end",
    reasoning: str | None = None,
) -> TurnCompleted:
    """Return one finished summary response."""
    message = Message(role="assistant", content=text, reasoning_content=reasoning, metrics=usage or MessageMetrics())
    return TurnCompleted(message=message, stop_reason=stop_reason)


def summary_steps(reply: str | TurnCompleted) -> list[ScriptStep]:
    """Return the events of one finished summary response: its usage, when it reports any, then the turn."""
    turn = summary_turn(reply) if isinstance(reply, str) else reply
    usage = turn.message.metrics
    return [UsageReported(usage), turn] if usage.to_dict() else [turn]


def _steps(reply: Reply) -> list[ScriptStep]:
    if isinstance(reply, BaseException):

        async def fail() -> None:
            raise reply

        return [Await(fail)]
    if isinstance(reply, str | TurnCompleted):
        return summary_steps(reply)
    return list(reply)


@dataclass
class ScriptedSummaryModel(ScriptedModel):
    """Answer each request with the next scripted reply, then with ``default`` once the replies run out."""

    default: Sequence[ScriptStep] | None = None

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the next reply's events."""
        if not self.turns and self.default is not None:
            self.turns.append(self.default)
        async for event in super().stream(request):
            yield event


def summary_model(
    *replies: Reply,
    model_id: str = "summary-model",
    provider: str = "fake",
    claude: bool = False,
    default: str | None = "ok",
) -> ScriptedSummaryModel:
    """Return a summary model named ``model_id``; ``claude`` gives it Claude's conservative input sizing."""
    return ScriptedSummaryModel(
        turns=[_steps(reply) for reply in replies],
        identity=ModelIdentity(id=model_id, provider=provider, provider_name=provider),
        capabilities=Capabilities(claude_family=claude),
        default=summary_steps(default) if default is not None else None,
    )


__all__ = ["Reply", "ScriptedSummaryModel", "summary_model", "summary_steps", "summary_turn"]
