"""The ``claude_code`` route: Claude on the user's own subscription, through the unmodified Claude Code CLI.

Claude Code signs in and talks to Anthropic; MindRoom never reads its credentials. The route drives the user's
``claude`` binary through the Claude Agent SDK with Claude Code's own tools off and MindRoom's offered through an
in-process MCP server, so MindRoom keeps its history, its tool loop, and its approvals. A process stays alive across
the requests of one tool loop (``session.py``); a request it cannot continue starts a new one (``prompt.py``).
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, get_args

from claude_agent_sdk.types import EffortLevel

from mindroom.llm.messages import stable_serialize
from mindroom.llm.provider import Capabilities, ModelIdentity, TurnCompleted
from mindroom.llm.request_identity import request_prefixes
from mindroom.providers.assistant_turn import TurnTimer
from mindroom.providers.claude_code import session as runtime
from mindroom.providers.claude_code.prompt import plan_continuation, rebuild_input
from mindroom.providers.claude_code.settings import ClaudeCodeSettings
from mindroom.providers.claude_code.turn import ResultsOutrun, read_step
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params
from mindroom.providers.identity import model_identity
from mindroom.providers.prompt_cache_key import derive_session_routing_key
from mindroom.providers.tool_policy import disables_tools
from mindroom.token_counting.families import counter_for_model
from mindroom.token_counting.requests import plain_request_parts, route_context_key, route_request_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, StreamEvent, TokenCounter
    from mindroom.providers.claude_code.session import LiveSession
    from mindroom.token_counting.requests import RequestParts
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["ClaudeCodeProvider", "build_adapter"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeCodeProvider:
    """A Claude model that the user's signed-in Claude Code answers for.

    ``cli_path`` is the executable that runs, or None for the copy the Claude Agent SDK ships. ``scope`` names the
    conversation whose requests may continue one another's idle Claude Code process, or is None outside one.
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
            runtime.SESSIONS.retire(session)
            raise
        return True

    async def _rebuild(self, request: ModelRequest, session: LiveSession | None) -> LiveSession:
        """Send the whole conversation to a fresh process, reusing the replaced process's slot if possible."""
        no_tools = disables_tools(request.tool_choice)
        tools = () if no_tools else request.tools
        messages = request.messages
        try:
            system, blocks = await asyncio.to_thread(rebuild_input, messages, self.identity.provider)
            stale = [] if session is None else [session]
            if not no_tools:
                # No request continues what this one leaves resting in its conversation. One that may call no tool,
                # such as a decision about the conversation, is not the conversation going on.
                held = {call["id"] for message in messages for call in message.tool_calls or ()}
                stale += runtime.SESSIONS.superseded(self._conversation, held)
            for other in stale[1:]:
                runtime.SESSIONS.retire(other)
            session = stale[0] if stale else None
            thinking = {"type": "adaptive", "display": "summarized"}
            session = await runtime.start(
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
                runtime.SESSIONS.retire(session)
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
            # Taken out of the resting ones, no other request continues it.
            key = (self._conversation, prefix, tuple(call["id"] for call in tail[0].tool_calls or ()))
            session = runtime.SESSIONS.claim(key)
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
                        runtime.SESSIONS.rest(key, session)
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
                    # A request that failed or was abandoned leaves a process nothing can continue.
                    runtime.SESSIONS.retire(session)

    async def preflight(self) -> None:
        """Start Claude Code as a request would, send it nothing, and close it, raising what a request would."""
        await runtime.SESSIONS.retire(
            await runtime.start(
                provider=self.identity.provider, storage_root=self.storage_root, **self._launch_options
            ),
        )


def build_adapter(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
) -> ClaudeCodeProvider:
    """Build the adapter for a ``claude_code`` model config; a configured API key is ignored."""
    storage_root = runtime_paths.storage_root
    scope = None
    if execution_identity is not None:
        scope = derive_session_routing_key(execution_identity, storage_root=storage_root)
    settings = settings_from_extra_kwargs(ClaudeCodeSettings, model_config.extra_kwargs, provider=model_config.provider)
    efforts = get_args(EffortLevel)
    if settings.effort is not None and settings.effort not in efforts:
        msg = f"effort must be one of {', '.join(efforts)}"
        raise ValueError(msg)
    return ClaudeCodeProvider(
        identity=model_identity(model_config),
        capabilities=Capabilities(
            structured_output="json_mode",
            omitted_media=frozenset({"audio", "video", "file"}),
            provider_default_sampling=True,
        ),
        settings=settings,
        storage_root=storage_root,
        # The SDK otherwise prefers the copy it ships over the one the user signed in with and keeps updated.
        cli_path=settings.cli_path or shutil.which("claude"),
        scope=scope,
    )
