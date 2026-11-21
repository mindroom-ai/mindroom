"""Read one model step from a live Claude Code process and turn it into the events of one request.

A step ends where MindRoom's loop takes over: at the end of a model message that calls MindRoom's tools, with the
process parked on those calls, or at the result that ends Claude Code's turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anthropic.types.beta import BetaUsage
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from mindroom.llm.errors import QUOTA_EXHAUSTED, AuthenticationFailed, ProviderError, ProviderUnavailable
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, UsageReported
from mindroom.llm.usage import add_usage
from mindroom.providers.anthropic.wire import parse_claude_usage, stop_reason_of
from mindroom.providers.assistant_turn import finished_turn
from mindroom.providers.claude_code.prompt import TOOL_PREFIX, emitted
from mindroom.providers.claude_code.session import NOT_SIGNED_IN, SERVER_NAME
from mindroom.providers.errors import classify_provider_error

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from claude_agent_sdk import Message as ClaudeMessage
    from claude_agent_sdk import RateLimitInfo

    from mindroom.llm.provider import ModelIdentity, StopReason
    from mindroom.llm.provider import StreamEvent as ModelEvent
    from mindroom.llm.usage import MessageMetrics
    from mindroom.providers.assistant_turn import TurnTimer
    from mindroom.providers.claude_code.session import LiveSession

__all__ = ["Outrun", "read_step"]

# The counters of Claude's usage, each with its name in the ledger Claude Code keeps of all a process used.
_COUNTERS = {
    "input_tokens": "inputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "output_tokens": "outputTokens",
}


class Outrun(ProviderUnavailable):
    """Claude Code's model went on before the results MindRoom gave it had reached it; none of that answer counts."""


def _metrics(usage: dict[str, Any]) -> MessageMetrics:
    return parse_claude_usage(BetaUsage.model_construct(**usage))


@dataclass
class _Step:
    """What one step produced so far.

    ``usage`` sums the model messages that ended and ``current`` is the usage of the one still streaming. ``seen``
    holds each model message's id with whether it streams, ``streaming`` the id of the one that streams now, ``ended``
    why it ended, and ``spoke`` the id of the one whose text came last. ``error`` is a failed request's kind.
    """

    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    current: dict[str, Any] | None = None
    seen: dict[str | None, bool] = field(default_factory=dict)
    streaming: str | None = None
    ended: str | None = None
    spoke: str | None = None
    error: str | None = None
    stop_reason: StopReason | None = None

    def _reported(self) -> list[ModelEvent]:
        partial = add_usage(self.usage, None if self.current is None else _metrics(self.current))
        return [] if partial is None else [UsageReported(partial, final=False)]

    def _say(self, text: str, message_id: str | None) -> list[ModelEvent]:
        """Keep a piece of the answer's text, a blank line after the text of an earlier model message."""
        if self.text and message_id != self.spoke:
            text = f"\n\n{text}"
        self.spoke = message_id
        self.text.append(text)
        return [TextDelta(text)]

    def _think(self, thinking: str) -> list[ModelEvent]:
        self.reasoning.append(thinking)
        return [ReasoningDelta(thinking)]

    def _count(self, usage: dict[str, Any], session: LiveSession) -> None:
        """Add the usage of a model message that ended."""
        self.usage = add_usage(self.usage, _metrics(usage))
        for counter in _COUNTERS:
            session.counted[counter] = session.counted.get(counter, 0) + (usage.get(counter) or 0)

    def accept(self, message: ClaudeMessage, session: LiveSession, provider: str) -> list[ModelEvent]:
        """Return the events of one message of the process, raising for a turn that failed or lost its isolation."""
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                session.inventoried = _only_mindrooms_tools(message.data, session)
        elif isinstance(message, RateLimitEvent):
            session.rejected = message.rate_limit_info
        elif isinstance(message, UserMessage):
            # Claude Code says which tool results it took before its model goes on with them.
            blocks = message.content if isinstance(message.content, list) else []
            session.bridge.unconfirmed -= {block.tool_use_id for block in blocks if isinstance(block, ToolResultBlock)}
        elif not session.inventoried:
            # Nothing a process says counts before it said that it runs with exactly MindRoom's tools.
            msg = "Claude Code did not start its turn with exactly MindRoom's tools, so the turn was stopped."
            raise ProviderError(msg, provider=provider)
        elif isinstance(message, StreamEvent | AssistantMessage | ResultMessage) and session.bridge.undelivered:
            msg = "Claude Code went on without the tool results MindRoom gave it."
            raise Outrun(msg, provider=provider)
        elif isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
            return self._stream_event(message.event, session)
        elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            return self._assistant(message, session, provider)
        elif isinstance(message, ResultMessage):
            self._end(message, session, provider)
        return []

    def _end(self, result: ResultMessage, session: LiveSession, provider: str) -> None:
        """End the step with the result of Claude Code's turn, raising for a turn that failed."""
        failure = _turn_failure(result, error=self.error, rejected=session.rejected, provider=provider)
        if failure is None and self.calls:
            # Without the end of the model message that made them, the process never waited on these calls.
            msg = "Claude Code ended its turn without waiting for MindRoom's tool results."
            failure = ProviderError(msg, provider=provider)
        if failure is not None:
            raise failure
        # What the turn used beyond its model messages, such as a request Claude Code made for itself with another
        # model: the ledger grew by more than the messages counted.
        models = (result.model_usage or {}).values()
        ledger = {counter: sum(model.get(key) or 0 for model in models) for counter, key in _COUNTERS.items()}
        before, counted = session.ledger, session.counted
        extra = {key: max(0, used - before.get(key, 0) - counted.get(key, 0)) for key, used in ledger.items()}
        session.ledger = ledger
        if any(extra.values()):
            self.usage = add_usage(replace(_metrics(extra), context=None), self.usage)
        self.stop_reason = stop_reason_of(result.stop_reason) or "end"

    def _stream_event(self, event: dict[str, Any], session: LiveSession) -> list[ModelEvent]:
        """Return the events of one piece of a model message as it streams."""
        kind = event.get("type")
        if kind == "message_start":
            message = event["message"]
            self.streaming = message["id"]
            self.seen[self.streaming] = True
            self.current, self.error, self.ended = dict(message["usage"]), None, None
            return self._reported()
        if kind == "message_delta":
            reported = {key: value for key, value in event["usage"].items() if value is not None}
            self.current = {**(self.current or {}), **reported}
            self.ended = (event.get("delta") or {}).get("stop_reason") or self.ended
            return self._reported()
        if kind == "message_stop":
            if self.current is not None:
                self._count(self.current, session)
            self.current = self.streaming = None
            if self.calls and self.ended != "tool_use":
                # Claude Code ends a model message it gave up without that reason, and its calls are not made.
                msg = "Claude Code gave up the model message that called MindRoom's tools."
                raise ProviderUnavailable(msg, provider=session.provider)
            if self.calls:
                # The model message that called MindRoom's tools has ended: MindRoom's loop runs them now.
                self.stop_reason = "tool_use"
        # A block can start with its first piece, and its deltas bring the rest.
        piece = event.get("content_block" if kind == "content_block_start" else "delta") or {}
        if kind in {"content_block_start", "content_block_delta"}:
            if piece.get("type") in {"text", "text_delta"} and piece.get("text"):
                return self._say(piece["text"], self.streaming)
            if piece.get("type") in {"thinking", "thinking_delta"} and piece.get("thinking"):
                return self._think(piece["thinking"])
        return []

    def _assistant(self, message: AssistantMessage, session: LiveSession, provider: str) -> list[ModelEvent]:
        """Keep a complete block's tool call; a message that did not stream also gives its text and its usage."""
        if message.error is not None:
            # The text of a request that failed is Claude Code's diagnostic, not an answer.
            self.error = message.error
            return []
        if message.message_id not in self.seen:
            self.seen[message.message_id] = False
            if message.usage is not None:
                self._count(message.usage, session)
        events: list[ModelEvent] = []
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                self.calls.append(_tool_call(block, session, provider))
            elif self.seen[message.message_id]:
                continue
            elif isinstance(block, TextBlock) and block.text:
                events += self._say(block.text, message.message_id)
            elif isinstance(block, ThinkingBlock) and block.thinking:
                events += self._think(block.thinking)
        if self.calls and self.streaming is None:
            # No end of a model message follows calls that did not stream, so which call is its last is not known.
            msg = "Claude Code did not stream the model message that called MindRoom's tools."
            raise ProviderUnavailable(msg, provider=provider)
        return events


def _tool_call(block: ToolUseBlock, session: LiveSession, provider: str) -> dict[str, Any]:
    """Return the call of a MindRoom tool the model wrote, which the process then waits on."""
    short = block.name.removeprefix(TOOL_PREFIX)
    name = session.bridge.names.get(short) if block.name.startswith(TOOL_PREFIX) else None
    # Claude Code would answer a call MindRoom cannot run itself, and the model would go on.
    if name is None:
        # The model may not name it again in a new process, which the retry of the request starts.
        msg = f"Claude called {block.name}, which MindRoom did not offer."
        raise ProviderUnavailable(msg, provider=provider)
    if not session.bridge.made(block.id, short, block.input):
        msg = f"Claude wrote the tool call {block.id} twice."
        raise ProviderError(msg, provider=provider)
    return {"id": block.id, "type": "function", "function": {"name": name, "arguments": json.dumps(block.input)}}


def _turn_failure(
    result: ResultMessage,
    *,
    error: str | None,
    rejected: RateLimitInfo | None,
    provider: str,
) -> ProviderError | None:
    """Return the error of a Claude Code turn that ``result`` ended in failure, or None for one that succeeded.

    ``error`` is the kind Claude Code gave the failed request and ``rejected`` the plan limit it reported. Claude
    Code retries a request itself before it gives up, so no failure here is retryable.
    """
    status = result.api_error_status
    if not result.is_error and (status is None or status < 400):
        return None
    text = result.result or "\n".join(result.errors or ()) or "Claude Code could not finish its reply"
    if error == "authentication_failed" or "/login" in text:
        return AuthenticationFailed(NOT_SIGNED_IN, provider=provider, status_code=401)
    rejected = rejected if rejected is not None and rejected.status == "rejected" else None
    if error == "rate_limit" or (rejected is not None and status == 429):
        window = (rejected.rate_limit_type or "").replace("_", " ") if rejected is not None else ""
        message = f"Claude subscription limit reached ({window})." if window else "Claude subscription limit reached."
        if rejected is not None and rejected.resets_at:
            message += f" Resets at {datetime.fromtimestamp(rejected.resets_at, UTC).isoformat()}."
        return ProviderError(message, provider=provider, status_code=429, code=QUOTA_EXHAUSTED)
    classified = classify_provider_error(text, provider=provider, status_code=status)
    if not classified.retryable:
        return classified
    return ProviderError(f"{text} (Claude Code already retried the request.)", provider=provider, status_code=status)


def _only_mindrooms_tools(init: dict[str, Any], session: LiveSession) -> bool:
    """Return whether the message that starts a turn lists exactly the offered tools and their server, connected."""
    tools, servers = init.get("tools"), init.get("mcp_servers")
    if not isinstance(tools, list) or not isinstance(servers, list):
        return False
    offered = sorted(f"{TOOL_PREFIX}{name}" for name in session.bridge.names)
    connected = [(server.get("name"), server.get("status")) for server in servers]
    return sorted(map(str, tools)) == offered and connected == ([(SERVER_NAME, "connected")] if offered else [])


async def read_step(session: LiveSession, identity: ModelIdentity, timer: TurnTimer) -> AsyncIterator[ModelEvent]:
    """Yield the events of the next model step of ``session``; the finished turn is the last one.

    ``session.emitted`` then holds the step's answer, whose tool calls the process is parked on.
    """
    step = _Step()
    while step.stop_reason is None:
        message = await session.bridge.messages.get()
        if isinstance(message, Exception):
            msg = f"Claude Code stopped: {message}"
            raise ProviderUnavailable(msg, provider=identity.provider) from message
        for event in timer.chunk(step.accept(message, session, identity.provider)):
            yield event
    if (failure := session.bridge.failure) is not None:
        raise ProviderUnavailable(failure, provider=identity.provider)
    for call in step.calls:
        yield timer.output(ToolCallReady(call))
    content = "".join(step.text) or None
    session.emitted = emitted(content, step.calls)
    for event in finished_turn(
        identity,
        timer,
        content=content,
        reasoning="".join(step.reasoning) or None,
        tool_calls=step.calls,
        usage=step.usage,
        stop_reason=step.stop_reason,
    ):
        yield event
