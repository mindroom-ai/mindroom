"""Read one model step from a live Claude Code process and turn it into the events of one request.

A step ends where MindRoom's loop takes over: at the end of a model message that calls MindRoom's tools, with the
process parked on those calls, or at the result that ends Claude Code's turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    ToolUseBlock,
)

from mindroom.llm.errors import QUOTA_EXHAUSTED, AuthenticationFailed, ProviderError, ProviderUnavailable
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, UsageReported
from mindroom.llm.usage import add_usage
from mindroom.providers.anthropic.wire import parse_claude_usage, stop_reason_of
from mindroom.providers.assistant_turn import finished_turn
from mindroom.providers.claude_code.prompt import TOOL_PREFIX, Emitted
from mindroom.providers.claude_code.session import NOT_SIGNED_IN
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

__all__ = ["read_step", "turn_failure"]


def _metrics(usage: dict[str, Any]) -> MessageMetrics:
    return parse_claude_usage(BetaUsage.construct(**usage))


@dataclass
class _Step:
    """What one step produced so far.

    ``usage`` sums the model messages that ended and ``current`` is the usage of the one still streaming.
    ``error`` and ``rejected`` are what Claude Code said about a request that failed, kept until its result.
    ``stop_reason`` is set once the step ended.
    """

    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    current: dict[str, Any] | None = None
    streamed: set[str] = field(default_factory=set)
    error: str | None = None
    rejected: RateLimitInfo | None = None
    stop_reason: StopReason | None = None

    def _reported(self) -> list[ModelEvent]:
        partial = add_usage(self.usage, None if self.current is None else _metrics(self.current))
        return [] if partial is None else [UsageReported(partial, final=False)]

    def accept(self, message: ClaudeMessage, session: LiveSession, provider: str) -> list[ModelEvent]:
        """Return the events of one message of the process, raising for a turn that failed or lost its isolation."""
        if isinstance(message, SystemMessage) and message.subtype == "init":
            if (failure := _isolation_error(message.data, session, provider)) is not None:
                raise failure
        elif isinstance(message, RateLimitEvent):
            self.rejected = message.rate_limit_info if message.rate_limit_info.status == "rejected" else None
        elif isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
            return self._stream_event(message.event)
        elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            return self._assistant(message, session)
        elif isinstance(message, ResultMessage):
            failure = turn_failure(message, error=self.error, rejected=self.rejected, provider=provider)
            if failure is not None:
                raise failure
            self.stop_reason = stop_reason_of(message.stop_reason) or "end"
        return []

    def _stream_event(self, event: dict[str, Any]) -> list[ModelEvent]:
        """Return the events of one piece of a model message as it streams."""
        kind = event.get("type")
        if kind == "message_start":
            message = event["message"]
            self.streamed.add(message["id"])
            self.current, self.error = dict(message["usage"]), None
            return self._reported()
        if kind == "message_delta":
            reported = {key: value for key, value in event["usage"].items() if value is not None}
            self.current = {**(self.current or {}), **reported}
            return self._reported()
        if kind == "message_stop":
            if self.current is not None:
                self.usage, self.current = add_usage(self.usage, _metrics(self.current)), None
            if self.calls:
                # The model message that called MindRoom's tools has ended: MindRoom's loop runs them now.
                self.stop_reason = "tool_use"
        delta = event.get("delta") or {}
        if kind == "content_block_delta" and delta.get("type") == "text_delta" and delta["text"]:
            self.text.append(delta["text"])
            return [TextDelta(delta["text"])]
        if kind == "content_block_delta" and delta.get("type") == "thinking_delta" and delta["thinking"]:
            self.reasoning.append(delta["thinking"])
            return [ReasoningDelta(delta["thinking"])]
        return []

    def _assistant(self, message: AssistantMessage, session: LiveSession) -> list[ModelEvent]:
        """Keep a complete block's tool call, and return its text only when none of its message streamed."""
        if message.error is not None:
            # The text of a request that failed is Claude Code's diagnostic, not an answer.
            self.error = message.error
            return []
        events: list[ModelEvent] = []
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                short = block.name.removeprefix(TOOL_PREFIX)
                # Claude Code answers a call to a tool MindRoom did not offer itself, and the model goes on.
                if block.name.startswith(TOOL_PREFIX) and (name := session.bridge.names.get(short)) is not None:
                    session.called[block.id] = short
                    function = {"name": name, "arguments": json.dumps(block.input)}
                    self.calls.append({"id": block.id, "type": "function", "function": function})
            elif message.message_id in self.streamed:
                continue
            elif isinstance(block, TextBlock) and block.text:
                self.text.append(block.text)
                events.append(TextDelta(block.text))
            elif isinstance(block, ThinkingBlock) and block.thinking:
                self.reasoning.append(block.thinking)
                events.append(ReasoningDelta(block.thinking))
        return events


def turn_failure(
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


def _isolation_error(init: dict[str, Any], session: LiveSession, provider: str) -> ProviderError | None:
    """Return the error of a process whose first message lists anything but MindRoom's tools, connected."""
    offered = {f"{TOOL_PREFIX}{name}" for name in session.bridge.names}
    servers = {server.get("name"): server.get("status") for server in init.get("mcp_servers") or ()}
    if set(init.get("tools") or ()) <= offered and (not offered or servers == {"mindroom": "connected"}):
        return None
    msg = "Claude Code did not start with exactly MindRoom's tools, so the request was not sent on."
    return ProviderError(msg, provider=provider)


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
    if step.calls and (broken := session.broken()) is not None:
        raise ProviderUnavailable(broken, provider=identity.provider)
    for call in step.calls:
        yield timer.output(ToolCallReady(call))
    content = "".join(step.text) or None
    session.emitted = Emitted(content=content, tool_call_ids=tuple(call["id"] for call in step.calls))
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
