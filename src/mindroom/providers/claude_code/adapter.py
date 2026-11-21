"""The ``claude_code`` route: Claude on the user's own subscription, through the unmodified Claude Code CLI.

Claude Code signs in and talks to Anthropic; MindRoom never reads its credentials and sends no Anthropic request of
its own. The route drives the user's ``claude`` binary through the Claude Agent SDK with Claude Code's own tools
off and MindRoom's tools offered through an in-process MCP server, so MindRoom keeps its history, its tool loop, and
its approvals. A process stays alive across the requests of one tool loop (``session.py``), and any request it
cannot continue starts a new one from MindRoom's history (``prompt.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from mindroom.llm.provider import Capabilities, ModelIdentity, TurnCompleted
from mindroom.llm.request_identity import request_prefixes
from mindroom.providers.assistant_turn import TurnTimer
from mindroom.providers.claude_code.prompt import decide, rebuild_input
from mindroom.providers.claude_code.settings import ClaudeCodeSettings
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params
from mindroom.providers.identity import model_identity
from mindroom.providers.prompt_cache_key import derive_session_routing_key
from mindroom.token_counting.families import counter_for_model
from mindroom.token_counting.requests import plain_request_parts, route_context_key, route_request_tokens
from mindroom.tool_system.dependencies import ensure_optional_deps

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent, TokenCounter
    from mindroom.providers.claude_code.session import LiveSession
    from mindroom.token_counting.requests import RequestParts
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["ClaudeCodeProvider", "build_provider"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeCodeProvider:
    """A Claude model that the user's signed-in Claude Code answers for.

    ``scope`` names the conversation whose requests may continue one another's Claude Code process.
    """

    identity: ModelIdentity
    capabilities: Capabilities
    settings: ClaudeCodeSettings
    storage_root: Path
    scope: str
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

    def _parts(self, request: ModelRequest, start: int, parts: RequestParts) -> None:
        """Add ``request`` as text by role, with the settings that change what Claude Code is started with."""
        plain_request_parts(request, start, parts)
        parts.flag([self.settings.effort, request.tool_choice])

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        """Estimate ``request`` as text by role; what Claude Code adds of its own is not counted."""
        return route_request_tokens(self.token_counter, request, start, self._parts)

    def context_key(self, request: ModelRequest) -> str:
        """Identify the tools and settings of ``request``, which a Claude Code process is started with."""
        return route_context_key(self.identity, self.token_counter, request, self._parts)

    async def _session(self, request: ModelRequest, resting: str | None) -> LiveSession:
        """Return the process that answers ``request``: the one resting under ``resting``, continued, or a new one."""
        # The SDK loads only once a request is sent.
        from mindroom.providers.claude_code.session import SESSIONS, start  # noqa: PLC0415

        provider = self.identity.provider
        messages = request.messages
        session = None if resting is None else SESSIONS.claim(resting)
        try:
            if session is not None and session.emitted is not None and session.broken() is None:
                tail = messages[max(index for index, message in enumerate(messages) if message.role == "assistant") :]
                resume = await asyncio.to_thread(decide, session.emitted, tail, provider)
                if resume is not None:
                    sent = {"mode": "continue", "results": resume.results, "input": resume.blocks}
                    await request.on_send({"model": self.identity.id, **sent})
                    if resume.results:
                        session.resolve(resume.results)
                    else:
                        await session.send(resume.blocks)
                    return session
            if session is not None:
                SESSIONS.retire(session)
                session = None
            tools = () if request.tool_choice == "none" else request.tools
            system, blocks = await asyncio.to_thread(rebuild_input, messages, provider)
            sent = {"mode": "rebuild", "system": system, "tools": [tool.name for tool in tools], "input": blocks}
            await request.on_send({"model": self.identity.id, **sent})
            session = await start(
                model=self.identity.id,
                settings=self.settings,
                system=system,
                tools=tools,
                storage_root=self.storage_root,
                provider=provider,
            )
            await session.send(blocks)
        except BaseException:
            if session is not None:
                SESSIONS.retire(session)
            raise
        return session

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Answer ``request`` with the process that waits for it, or with a new one given the whole conversation."""
        from mindroom.providers.claude_code.session import SESSIONS  # noqa: PLC0415 - loads the SDK
        from mindroom.providers.claude_code.turn import read_step  # noqa: PLC0415 - loads the SDK

        timer = TurnTimer()
        # Digesting a long history would stall the replies that share the event loop.
        prefixes = await asyncio.to_thread(request_prefixes, self, request)
        # A process rests under the identity of the request it answered, which precedes its answer in the next one.
        answered = max((i for i, message in enumerate(request.messages) if message.role == "assistant"), default=None)
        resting = None if answered is None or prefixes[answered] is None else f"{self.scope}{prefixes[answered]}"
        session = await self._session(request, resting)
        rests = False
        try:
            async for event in read_step(session, self.identity, timer):
                if isinstance(event, TurnCompleted) and prefixes[-1] is not None:
                    # The process rests before the turn is handed over, since the caller may stop reading there.
                    SESSIONS.rest(f"{self.scope}{prefixes[-1]}", session)
                    rests = True
                yield event
        finally:
            if not rests:
                # A request that failed or was abandoned leaves a process nothing can continue.
                SESSIONS.retire(session)


def build_provider(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
) -> ClaudeCodeProvider:
    """Build the adapter for a ``claude_code`` model config; a configured API key is ignored."""
    ensure_optional_deps(["claude-agent-sdk"], "claude_agent", runtime_paths)
    storage_root = runtime_paths.storage_root
    scope = None
    if execution_identity is not None:
        scope = derive_session_routing_key(execution_identity, storage_root=storage_root)
    return ClaudeCodeProvider(
        identity=model_identity(model_config),
        capabilities=Capabilities(
            structured_output="json_mode",
            omitted_media=frozenset({"audio", "video", "file"}),
            provider_default_sampling=True,
        ),
        settings=settings_from_extra_kwargs(
            ClaudeCodeSettings,
            model_config.extra_kwargs,
            provider=model_config.provider,
        ),
        storage_root=storage_root,
        scope=f"{scope or ''}:",
    )
