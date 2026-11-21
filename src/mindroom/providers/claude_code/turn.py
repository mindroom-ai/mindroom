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

__all__ = ["read_step", "turn_failure"]


def _metrics(usage: dict[str, Any]) -> MessageMetrics:
    return parse_claude_usage(BetaUsage.model_construct(**usage))


@dataclass
class _Step:
    """What one step produced so far.

    ``usage`` sums the model messages that ended and ``current`` is the usage of the one still streaming.
    ``seen`` holds each model message's id with whether it streams, and ``spoke`` the id of the one whose text came
    last. ``error`` and ``rejected`` are what Claude Code said about a request that failed, kept until its result.
    ``stop_reason`` is set once the step ended.
    """

    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    current: dict[str, Any] | None = None
    seen: dict[str | None, bool] = field(default_factory=dict)
    streaming: str | None = None
    spoke: str | None = None
    error: str | None = None
    rejected: RateLimitInfo | None = None
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

    def accept(self, message: ClaudeMessage, session: LiveSession, provider: str) -> list[ModelEvent]:
        """Return the events of one message of the process, raising for a turn that failed or lost its isolation."""
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                session.inventoried = _only_mindrooms_tools(message.data, session)
        elif isinstance(message, RateLimitEvent):
            self.rejected = message.rate_limit_info if message.rate_limit_info.status == "rejected" else None
        elif not session.inventoried:
            # Nothing a process says counts before it said that it runs with exactly MindRoom's tools.
            msg = "Claude Code did not start its turn with exactly MindRoom's tools, so the turn was stopped."
            raise ProviderError(msg, provider=provider)
        elif isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
            return self._stream_event(message.event)
        elif isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            return self._assistant(message, session)
        elif isinstance(message, ResultMessage):
            failure = turn_failure(message, error=self.error, rejected=self.rejected, provider=provider)
            if failure is None and self.calls:
                # Without the end of the model message that made them, the process never waited on these calls.
                msg = "Claude Code ended its turn without waiting for MindRoom's tool results."
                failure = ProviderError(msg, provider=provider)
            if failure is not None:
                raise failure
            self.stop_reason = stop_reason_of(message.stop_reason) or "end"
        return []

    def _stream_event(self, event: dict[str, Any]) -> list[ModelEvent]:
        """Return the events of one piece of a model message as it streams."""
        kind = event.get("type")
        if kind == "message_start":
            message = event["message"]
            self.streaming = message["id"]
            self.seen[self.streaming] = True
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
        # A block can start with its first piece, and its deltas bring the rest.
        piece = event.get("content_block" if kind == "content_block_start" else "delta") or {}
        if kind in {"content_block_start", "content_block_delta"}:
            if piece.get("type") in {"text", "text_delta"} and piece.get("text"):
                return self._say(piece["text"], self.streaming)
            if piece.get("type") in {"thinking", "thinking_delta"} and piece.get("thinking"):
                return self._think(piece["thinking"])
        return []

    def _assistant(self, message: AssistantMessage, session: LiveSession) -> list[ModelEvent]:
        """Keep a complete block's tool call; a message that did not stream also gives its text and its usage."""
        if message.error is not None:
            # The text of a request that failed is Claude Code's diagnostic, not an answer.
            self.error = message.error
            return []
        if message.message_id not in self.seen:
            self.seen[message.message_id] = False
            self.usage = add_usage(self.usage, None if message.usage is None else _metrics(message.usage))
        events: list[ModelEvent] = []
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                short = block.name.removeprefix(TOOL_PREFIX)
                # Claude Code answers a call to a tool MindRoom did not offer itself, and the model goes on.
                if block.name.startswith(TOOL_PREFIX) and (name := session.bridge.names.get(short)) is not None:
                    session.bridge.made(block.id, short, block.input)
                    function = {"name": name, "arguments": json.dumps(block.input)}
                    self.calls.append({"id": block.id, "type": "function", "function": function})
            elif self.seen[message.message_id]:
                continue
            elif isinstance(block, TextBlock) and block.text:
                events += self._say(block.text, message.message_id)
            elif isinstance(block, ThinkingBlock) and block.thinking:
                events += self._think(block.thinking)
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
