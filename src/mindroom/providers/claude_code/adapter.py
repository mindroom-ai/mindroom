"""Serve Claude subscription models through Claude Code, with MindRoom owning history, tools, and approvals."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar

from mindroom.llm.messages import stable_serialize
from mindroom.llm.provider import ModelIdentity, TurnCompleted
from mindroom.llm.request_identity import request_prefixes
from mindroom.providers.assistant_turn import TurnTimer
from mindroom.providers.claude_code.prompt import plan_continuation, rebuild_input
from mindroom.providers.claude_code.session import SESSIONS
from mindroom.providers.claude_code.turn import ResultsOutrun, read_step
from mindroom.providers.extra_kwargs import settings_params
from mindroom.providers.tool_policy import disables_tools
from mindroom.token_counting.families import counter_for_model
from mindroom.token_counting.requests import plain_request_parts, route_context_key, route_request_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import Capabilities, ModelRequest, StreamEvent, TokenCounter
    from mindroom.providers.claude_code.session import LiveSession
    from mindroom.providers.claude_code.settings import ClaudeCodeSettings
    from mindroom.token_counting.requests import RequestParts

__all__ = ["ClaudeCodeProvider"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeCodeProvider:
    """A Claude model that the user's signed-in Claude Code answers for.

    ``cli_path`` is the configured executable or the one found on PATH, or None for SDK discovery. ``scope`` names
    the conversation whose requests may continue one another's idle Claude Code process, or is None outside one.
    """

    identity: ModelIdentity
    capabilities: Capabilities
    settings: ClaudeCodeSettings
    storage_root: Path
    cli_path: str | None
    scope: str | None
    # Claude Code keeps no history MindRoom relies on.
    native_history_route: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """Claude's tokenizer estimate, whatever the configured model id looks like."""
        return counter_for_model(self.identity, family="claude")

    def _context_parts(self, request: ModelRequest, start: int, parts: RequestParts) -> None:
        """Keep the full tool and settings identity, even when the request does not offer tools."""
        plain_request_parts(request, start, parts)
        parts.flag([self.settings.effort, request.tool_choice, request.overrides.omit_thinking_setting])

    def _token_parts(self, request: ModelRequest, start: int, parts: RequestParts) -> None:
        """Count the rendered prompt text and the schemas of tools the route offers."""
        messages = request.messages[start:]
        if messages or not start:
            system, blocks = rebuild_input(messages, self.identity.provider)
            parts.text(system)
            # Media already count by allowance, before the renderer sees the messages.
            for block in blocks:
                parts.text(block["text"])
        if not start and not disables_tools(request.tool_choice):
            plain_request_parts(replace(request, messages=()), 0, parts)

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        """Estimate a cold rebuild, or the rendered suffix for a size anchored on reported usage.

        A live continuation only sends new input; the estimate excludes Claude Code's own overhead.
        """
        return route_request_tokens(self.token_counter, request, start, self._token_parts)

    def context_key(self, request: ModelRequest) -> str:
        """Identify the tools and settings of ``request``, which a Claude Code process is started with."""
        return route_context_key(self.identity, self.token_counter, request, self._context_parts)

    @property
    def _launch_options(self) -> dict[str, Any]:
        """What every Claude Code process of this model starts with."""
        return {"model": self.identity.id, "cli_path": self.cli_path, "effort": self.settings.effort}

    @property
    def _conversation(self) -> str:
        """Identify whose processes a request may continue or close: no request touches a process of another."""
        return stable_serialize([self.scope, str(self.storage_root), self.cli_path])

    async def _continue(self, request: ModelRequest, tail: list[Message], session: LiveSession) -> bool:
        """Continue with the messages from the last answer on, if they match the parked process."""
        try:
            if not tail or session.answer is None or request.portable_replay or session.broken() is not None:
                return False
            plan = await asyncio.to_thread(plan_continuation, session.answer, tail, self.identity.provider)
            if plan is None:
                return False
            results, blocks = plan
            await request.on_send({"model": self.identity.id, "mode": "continue", "results": results, "input": blocks})
            # The process may have gone on meanwhile: nothing comes between this look and what continues it.
            if session.broken() is not None:
                return False
            if results:
                session.bridge.resolve(results)
            else:
                await session.send(blocks)
        except BaseException:
            SESSIONS.retire(session)
            raise
        return True

    async def _rebuild(self, request: ModelRequest, session: LiveSession | None) -> LiveSession:
        """Send the whole conversation to a fresh process, reusing the replaced process's slot if possible."""
        no_tools = disables_tools(request.tool_choice)
        tools = () if no_tools else request.tools
        messages = request.messages
        try:
            system, blocks = await asyncio.to_thread(rebuild_input, messages, self.identity.provider)
            if not no_tools:
                # A decision request, unlike an ordinary turn, must not retire the conversation's processes.
                held = {call["id"] for message in messages for call in message.tool_calls or ()}
                stale = SESSIONS.superseded(self._conversation, held)
                if session is None and stale:
                    session = stale.pop(0)
                for other in stale:
                    SESSIONS.retire(other)
            thinking = {"type": "adaptive", "display": "summarized"}
            session = await SESSIONS.start(
                provider=self.identity.provider,
                storage_root=self.storage_root,
                system=system,
                tools=tools,
                replaces=session,
                thinking=None if request.overrides.omit_thinking_setting else thinking,
                **self._launch_options,
            )
            sent = {"mode": "rebuild", "system": system, "tools": [tool.name for tool in tools], "input": blocks}
            await request.on_send({"model": self.identity.id, **sent})
            await session.send(blocks)
        except BaseException:
            if session is not None:
                SESSIONS.retire(session)
            raise
        return session

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Answer ``request`` with the process that waits for it, or with a new one given the whole conversation."""
        timer = TurnTimer()
        # The live process already read any notice in its prefix; hashing the history belongs off the event loop.
        prefixes = await asyncio.to_thread(request_prefixes, self, request, count_queued_notices=True)
        # A process rests under the identity of the request it answered, which precedes its answer in the next one.
        answered = max((i for i, message in enumerate(request.messages) if message.role == "assistant"), default=None)
        tail = [] if answered is None else list(request.messages[answered:])
        session = None
        if answered is not None and (prefix := prefixes[answered]) is not None:
            key = (self._conversation, prefix, tuple(call["id"] for call in tail[0].tool_calls or ()))
            session = SESSIONS.claim(key)
        # A process that went on without the results it waited for is replaced by one given the whole conversation.
        for continued in (tail, []):
            if session is None or not await self._continue(request, continued, session):
                session = await self._rebuild(request, session)
            rests = False
            try:
                async for event in read_step(session, self.identity, timer):
                    answer = session.answer
                    # A process parked on tool calls waits for their results. An idle one waits for the next message
                    # of its conversation, which a request that could call no tool, such as a summary, never gets.
                    if (
                        isinstance(event, TurnCompleted)
                        and answer is not None
                        and (answer.calls or (self.scope and not disables_tools(request.tool_choice)))
                        and prefixes[-1] is not None
                    ):
                        # It rests before the turn is handed over, since the caller may stop reading there.
                        key = (self._conversation, prefixes[-1], answer.call_ids)
                        SESSIONS.rest(key, session)
                        rests = True
                    yield event
            except ResultsOutrun:
                if not continued:
                    raise
                # The process that starts over closes this one when it takes its slot, and no sooner.
                rests = True
            else:
                return
            finally:
                if not rests:
                    SESSIONS.retire(session)

    async def preflight(self) -> None:
        """Start Claude Code as a request would, send it nothing, and close it, raising what a request would."""
        await SESSIONS.retire(
            await SESSIONS.start(
                provider=self.identity.provider,
                storage_root=self.storage_root,
                **self._launch_options,
            ),
        )
