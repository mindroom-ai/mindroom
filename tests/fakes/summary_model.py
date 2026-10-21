"""A compaction summary model: a ``ModelProvider`` that answers each summary request with its next scripted reply."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, TurnCompleted, UsageReported
from mindroom.llm.usage import MessageMetrics

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from mindroom.llm.provider import ModelRequest, StopReason, StreamEvent

SUMMARY_IDENTITY = ModelIdentity(id="summary-model", provider="fake", provider_name="fake")

type Reply = str | TurnCompleted | BaseException | Callable[[ModelRequest], Awaitable[str | TurnCompleted]]


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


@dataclass
class ScriptedSummaryModel:
    """Answer each request with the next reply, or with ``default`` once the replies run out.

    A reply is the summary text, a finished turn, an exception to raise, or an async callable of the request that
    returns either, so a test can block, wait for cancellation, or inspect the request. ``requests`` keeps every
    request in order.
    """

    replies: list[Reply] = field(default_factory=list)
    default: str | None = "ok"
    identity: ModelIdentity = SUMMARY_IDENTITY
    capabilities: Capabilities = field(default_factory=Capabilities)
    requests: list[ModelRequest] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the usage and the finished turn of the next reply."""
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else self.default
        assert reply is not None, f"ScriptedSummaryModel has no reply for request {len(self.requests)}"
        if isinstance(reply, BaseException):
            raise reply
        if not isinstance(reply, str | TurnCompleted):
            reply = await reply(request)
        turn = summary_turn(reply) if isinstance(reply, str) else reply
        if turn.message.metrics.to_dict():
            yield UsageReported(turn.message.metrics)
        yield turn

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Summary requests offer no tools, so nothing ever formats a result."""
        del messages
        return list(results)


def summary_model(
    *replies: Reply,
    model_id: str = "summary-model",
    claude: bool = False,
    default: str | None = "ok",
) -> ScriptedSummaryModel:
    """Return a summary model named ``model_id``; ``claude`` gives it Claude's conservative input sizing."""
    return ScriptedSummaryModel(
        replies=list(replies),
        default=default,
        identity=replace(SUMMARY_IDENTITY, id=model_id),
        capabilities=Capabilities(claude_family=claude),
    )


__all__ = ["SUMMARY_IDENTITY", "Reply", "ScriptedSummaryModel", "summary_model", "summary_turn"]
