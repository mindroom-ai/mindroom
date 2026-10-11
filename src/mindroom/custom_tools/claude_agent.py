"""Claude Agent SDK-backed tools for persistent coding sessions."""

from __future__ import annotations

import asyncio
import collections
import typing
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, ClassVar, Literal, Protocol, cast, runtime_checkable
from uuid import uuid4

from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput
from agno.tools import Toolkit
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ConversationResetMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from mindroom.helper_usage import get_helper_usage_owner, record_helper_usage
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context

_PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
_VALID_PERMISSION_MODES: tuple[_PermissionMode, ...] = (
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
)
_DEFAULT_PERMISSION_MODE: _PermissionMode = "default"
_DEFAULT_SESSION_TTL_MINUTES = 60
_DEFAULT_MAX_SESSIONS = 200
_DEFAULT_LIMITS = (_DEFAULT_SESSION_TTL_MINUTES * 60, _DEFAULT_MAX_SESSIONS)
_MAX_STDERR_LINES = 12
_START_CLEANUP_TIMEOUT_SECONDS = 5.0

logger = get_logger(__name__)


@runtime_checkable
class _Agent(Protocol):
    """Minimal agent protocol needed by this tool."""

    name: str | None


@runtime_checkable
class _AgentWithId(Protocol):
    """Agent protocol that exposes a stable id field."""

    id: str | None


@runtime_checkable
class _AgentWithModel(Protocol):
    """Agent protocol that exposes a model object."""

    model: Any | None


@runtime_checkable
class _ModelWithId(Protocol):
    """Model protocol that exposes an id field."""

    id: str | None


class _RunContext(Protocol):
    """Minimal run context protocol needed by this tool."""

    session_id: str


def _parse_csv_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _normalize_permission_mode(permission_mode: str | None) -> _PermissionMode:
    if not permission_mode:
        return _DEFAULT_PERMISSION_MODE
    normalized = permission_mode.strip()
    if normalized not in _VALID_PERMISSION_MODES:
        return _DEFAULT_PERMISSION_MODE
    return cast("_PermissionMode", normalized)


def _parse_int(value: int | None, *, default: int, minimum: int) -> int:
    if value is None:
        return default
    return max(minimum, value)


def _parse_optional_int(value: int | None, *, minimum: int) -> int | None:
    if value is None:
        return None
    return max(minimum, value)


# Input, output, cache-read, and cache-write tokens of one model.
type _UsageCounts = tuple[int, int, int, int]


@dataclass
class _ClaudeSessionState:
    """Runtime state for one persistent Claude coding session."""

    key: str
    namespace: str
    client: ClaudeSDKClient
    owner: asyncio.Task[None]
    close_requested: asyncio.Event
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=monotonic)
    last_used_at: float = field(default_factory=monotonic)
    ttl_seconds: int = _DEFAULT_SESSION_TTL_MINUTES * 60
    claude_session_id: str | None = None
    # Claude Code's running usage totals per model, as of the last counted turn.
    usage_totals: dict[str, _UsageCounts] = field(default_factory=dict)
    stderr_lines: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_MAX_STDERR_LINES),
    )


class _ClaudeSessionManager:
    """Process-wide manager for persistent ClaudeSDKClient sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ClaudeSessionState] = {}
        # Last counted running totals per Claude session ID, so resuming or forking one counts only new usage.
        self.usage_totals: dict[str, dict[str, _UsageCounts]] = {}
        self._lock = asyncio.Lock()
        self._namespace_limits: dict[str, tuple[int, int]] = {}

    def configure_namespace(
        self,
        *,
        namespace: str,
        ttl_minutes: int,
        max_sessions: int,
    ) -> None:
        """Update limits for one namespace (typically one agent)."""
        ttl_seconds = max(60, ttl_minutes * 60)
        self._namespace_limits[namespace] = (ttl_seconds, max(1, max_sessions))

    async def get_or_create(
        self,
        session_key: str,
        namespace: str,
        options: ClaudeAgentOptions,
    ) -> tuple[_ClaudeSessionState, bool]:
        """Get an existing session or create a new one for the given key."""
        stale: list[_ClaudeSessionState] = []
        try:
            async with self._lock:
                stale.extend(self._collect_expired_locked())

                existing = self._sessions.get(session_key)
                if existing is not None:
                    existing.last_used_at = monotonic()
                    existing.ttl_seconds = self._namespace_ttl_seconds(namespace)
                    return existing, False

                stale.extend(self._evict_if_needed_locked(namespace))

                client = ClaudeSDKClient(options=options)
                close_requested = asyncio.Event()
                owner = await self._open(client, close_requested)
                session = _ClaudeSessionState(
                    key=session_key,
                    namespace=namespace,
                    client=client,
                    owner=owner,
                    close_requested=close_requested,
                    ttl_seconds=self._namespace_ttl_seconds(namespace),
                    usage_totals=dict(self.usage_totals.get(options.resume, {})) if options.resume else {},
                )
                self._sessions[session_key] = session
                return session, True
        finally:
            await self._disconnect_many(stale)

    async def close(self, session_key: str) -> bool:
        """Close and remove a session by key."""
        async with self._lock:
            session = self._sessions.pop(session_key, None)
        if session is None:
            return False
        await self._disconnect(session)
        return True

    async def get(self, session_key: str) -> _ClaudeSessionState | None:
        """Get a session by key, cleaning up expired sessions first."""
        stale: list[_ClaudeSessionState] = []
        try:
            async with self._lock:
                stale.extend(self._collect_expired_locked())
                session = self._sessions.get(session_key)
                if session is not None:
                    session.last_used_at = monotonic()
                return session
        finally:
            await self._disconnect_many(stale)

    def _collect_expired_locked(self) -> list[_ClaudeSessionState]:
        now = monotonic()
        expired_keys = [
            key
            for key, session in self._sessions.items()
            # Never expire an in-flight session; it will be re-evaluated once idle.
            if now - session.last_used_at > session.ttl_seconds and not session.lock.locked()
        ]
        return [self._sessions.pop(key) for key in expired_keys]

    def _evict_if_needed_locked(self, namespace: str) -> list[_ClaudeSessionState]:
        evicted: list[_ClaudeSessionState] = []
        max_sessions = self._namespace_max_sessions(namespace)
        while True:
            namespace_sessions = [key for key, session in self._sessions.items() if session.namespace == namespace]
            if len(namespace_sessions) < max_sessions:
                break
            evictable_sessions = [key for key in namespace_sessions if not self._sessions[key].lock.locked()]
            if not evictable_sessions:
                # Avoid stalling new requests on active session locks.
                break
            oldest_key = min(evictable_sessions, key=lambda key: self._sessions[key].last_used_at)
            evicted.append(self._sessions.pop(oldest_key))
        return evicted

    def _namespace_ttl_seconds(self, namespace: str) -> int:
        return self._namespace_limits.get(namespace, _DEFAULT_LIMITS)[0]

    def _namespace_max_sessions(self, namespace: str) -> int:
        return self._namespace_limits.get(namespace, _DEFAULT_LIMITS)[1]

    @staticmethod
    async def _open(client: ClaudeSDKClient, close_requested: asyncio.Event) -> asyncio.Task[None]:
        """Connect on a task that owns the client until ``close_requested`` is set.

        The SDK's reader tasks and subprocess copy the connecting context and live as long as the session,
        which outlives the tool call and turn that open it; that turn's contextvars hold its Agent and tools.
        """
        connected = asyncio.get_running_loop().create_future()

        async def own() -> None:
            try:
                await client.connect()
            except Exception as exc:
                # The SDK has already disconnected; the caller raises the failure.
                connected.set_exception(exc)
                return
            except BaseException:
                connected.cancel()
                raise
            connected.set_result(None)
            try:
                await close_requested.wait()
            finally:
                await client.disconnect()

        owner = asyncio.create_task(own(), name="claude_session", context=Context())
        try:
            await asyncio.shield(connected)
        except BaseException:
            owner.cancel()
            await _drain_failed_start(owner)
            if connected.done() and not connected.cancelled():
                connected.exception()
            raise
        return owner

    async def _disconnect(self, session: _ClaudeSessionState) -> None:
        async with session.lock:
            session.close_requested.set()
            with suppress(Exception):
                await session.owner

    async def _disconnect_many(self, sessions: list[_ClaudeSessionState]) -> None:
        for session in sessions:
            await self._disconnect(session)


async def _drain_failed_start(owner: asyncio.Task[None]) -> None:
    """Give the SDK a bounded chance to disconnect a start that nothing tracks yet, even when cancelled again.

    The caller holds the session manager's lock and re-raises what interrupted the start, so a failing or stuck
    disconnect is only logged.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_CLEANUP_TIMEOUT_SECONDS
    while not owner.done() and (remaining := deadline - loop.time()) > 0:
        with suppress(asyncio.CancelledError):
            await asyncio.wait({owner}, timeout=remaining)
    if not owner.done():
        logger.warning("Claude session cleanup timed out", timeout_seconds=_START_CLEANUP_TIMEOUT_SECONDS)
    elif not owner.cancelled() and (error := owner.exception()) is not None:
        logger.warning("Claude session cleanup failed", error=str(error))


def _turn_usage(previous: dict[str, _UsageCounts], current: dict[str, _UsageCounts]) -> dict[str, _UsageCounts]:
    """Return each model's usage since the previous running totals; a lower total means the session was cleared."""
    turn: dict[str, _UsageCounts] = {}
    for model_id, counts in current.items():
        before = previous.get(model_id, (0, 0, 0, 0))
        if any(now < then for now, then in zip(counts, before, strict=True)):
            before = (0, 0, 0, 0)
        delta = cast("_UsageCounts", tuple(now - then for now, then in zip(counts, before, strict=True)))
        if any(delta):
            turn[model_id] = delta
    return turn


async def _record_turn_usage(turn: dict[str, _UsageCounts]) -> None:
    """Count one Claude Code turn's usage toward the conversation and requester that ran it."""
    owner = get_helper_usage_owner()
    if owner is None or not turn:
        return
    models = [
        ModelMetrics(
            id=model_id,
            provider="Anthropic",
            input_tokens=inputs,
            output_tokens=outputs,
            total_tokens=inputs + outputs,
            cache_read_tokens=cache_reads,
            cache_write_tokens=cache_writes,
        )
        for model_id, (inputs, outputs, cache_reads, cache_writes) in turn.items()
    ]
    metrics = RunMetrics(details={"model": models})
    for model in models:
        metrics.input_tokens += model.input_tokens
        metrics.output_tokens += model.output_tokens
        metrics.total_tokens += model.total_tokens
        metrics.cache_read_tokens += model.cache_read_tokens
        metrics.cache_write_tokens += model.cache_write_tokens
    context = get_tool_runtime_context()
    invocation_id = uuid4().hex
    await record_helper_usage(
        RunOutput(run_id=invocation_id, metrics=metrics),
        owner=owner,
        invocation_id=invocation_id,
        kind="claude_agent",
        requester_id=context.requester_id if context is not None else None,
    )


def _response_text(text_parts: list[str], result: ResultMessage | None) -> str:
    """Return Claude's reply text, falling back to the result summary and marking reported errors."""
    response_text = "\n".join(part for part in text_parts if part).strip()
    if not response_text:
        if result is not None and result.result:
            response_text = str(result.result).strip()
        else:
            response_text = "Claude session completed without text output."
    if result is not None and result.is_error:
        return f"Claude reported an error: {response_text}"
    return response_text


class ClaudeAgentTools(Toolkit):
    """Tools that let MindRoom agents run persistent Claude coding sessions."""

    _session_manager: ClassVar[_ClaudeSessionManager] = _ClaudeSessionManager()

    def __init__(
        self,
        api_key: str | None = None,
        anthropic_base_url: str | None = None,
        anthropic_auth_token: str | None = None,
        disable_experimental_betas: bool = False,
        cwd: str | None = None,
        model: str | None = None,
        permission_mode: str | None = _DEFAULT_PERMISSION_MODE,
        continue_conversation: bool = False,
        allowed_tools: str | None = None,
        disallowed_tools: str | None = None,
        max_turns: int | None = None,
        system_prompt: str | None = None,
        cli_path: str | None = None,
        session_ttl_minutes: int | None = _DEFAULT_SESSION_TTL_MINUTES,
        max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self.api_key = api_key
        self.anthropic_base_url = anthropic_base_url
        self.anthropic_auth_token = anthropic_auth_token
        self.disable_experimental_betas = disable_experimental_betas
        self.cwd = cwd
        self.model = model
        self.permission_mode = _normalize_permission_mode(permission_mode)
        self.continue_conversation = continue_conversation
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.max_turns = _parse_optional_int(max_turns, minimum=1)
        self.system_prompt = system_prompt
        self.cli_path = cli_path
        self.session_ttl_minutes = _parse_int(
            session_ttl_minutes,
            default=_DEFAULT_SESSION_TTL_MINUTES,
            minimum=1,
        )
        self.max_sessions = _parse_int(max_sessions, default=_DEFAULT_MAX_SESSIONS, minimum=1)

        super().__init__(
            name="claude_agent",
            tools=[
                self.claude_start_session,
                self.claude_send,
                self.claude_session_status,
                self.claude_interrupt,
                self.claude_end_session,
            ],
        )

    def _build_options(
        self,
        stderr_callback: typing.Callable[[str], None] | None = None,
        *,
        model: str | None = None,
        resume: str | None = None,
        fork_session: bool = False,
    ) -> ClaudeAgentOptions:
        env: dict[str, str] = {}
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        if self.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        if self.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_auth_token
        if self.disable_experimental_betas:
            env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

        return ClaudeAgentOptions(
            cwd=self.cwd,
            model=model,
            permission_mode=self.permission_mode,
            continue_conversation=self.continue_conversation,
            resume=resume,
            fork_session=fork_session,
            allowed_tools=_parse_csv_list(self.allowed_tools),
            disallowed_tools=_parse_csv_list(self.disallowed_tools),
            max_turns=self.max_turns,
            system_prompt=self.system_prompt,
            cli_path=self.cli_path,
            env=env,
            stderr=stderr_callback,
        )

    @staticmethod
    def _build_stderr_callback(
        target: collections.deque[str],
    ) -> typing.Callable[[str], None]:
        def _on_stderr(line: str) -> None:
            cleaned = line.strip()
            if cleaned:
                target.append(cleaned)

        return _on_stderr

    def _format_session_error(
        self,
        message: str,
        *,
        session_key: str,
        model: str | None,
        resume: str | None,
        fork_session: bool,
        stderr_lines: collections.deque[str] | list[str],
    ) -> str:
        details = [
            "Session context:",
            f"- session_key: {session_key}",
            f"- model: {model or '(sdk default)'}",
            f"- continue_conversation: {self.continue_conversation}",
            f"- resume: {resume or '(none)'}",
            f"- fork_session: {fork_session}",
            f"- cwd: {self.cwd or '(default)'}",
        ]
        if resume:
            details.append("- note: `resume` must exist for the selected working-directory conversation context.")
        if stderr_lines:
            details.append("Recent Claude CLI stderr:")
            details.extend(f"- {line}" for line in list(stderr_lines)[-5:])
        return "\n".join([message, *details])

    def _namespace(self, agent: _Agent | None) -> str:
        if isinstance(agent, _AgentWithId):
            agent_id = agent.id
            if agent_id and agent_id.strip():
                return agent_id.strip()
        if not isinstance(agent, _Agent):
            return "mindroom"
        agent_name = agent.name
        if agent_name and agent_name.strip():
            return agent_name.strip()
        return "mindroom"

    def _ensure_namespace_config(self, namespace: str) -> None:
        self._session_manager.configure_namespace(
            namespace=namespace,
            ttl_minutes=self.session_ttl_minutes,
            max_sessions=self.max_sessions,
        )

    def _session_key(
        self,
        *,
        session_label: str | None,
        run_context: _RunContext | None,
        agent: _Agent | None,
    ) -> str:
        agent_name = self._namespace(agent)
        run_session = run_context.session_id if run_context is not None else "default"
        if session_label and session_label.strip():
            return f"{agent_name}:{run_session}:{session_label.strip()}"
        return f"{agent_name}:{run_session}"

    def _resolve_model(self, agent: _Agent | None) -> str | None:
        if self.model and self.model.strip():
            return self.model.strip()

        if not isinstance(agent, _AgentWithModel):
            return None
        model_obj = agent.model
        if not isinstance(model_obj, _ModelWithId):
            return None
        model_id = model_obj.id
        if model_id and model_id.strip():
            return model_id.strip()
        return None

    async def _get_or_create_session(
        self,
        *,
        session_label: str | None,
        resume: str | None,
        fork_session: bool,
        run_context: _RunContext | None,
        agent: _Agent | None,
    ) -> tuple[_ClaudeSessionState, bool, str, str | None] | str:
        """Shared session acquisition logic.

        Returns ``(session, created, session_key, resolved_model)`` on success,
        or an error string on failure.
        """
        normalized_resume = resume.strip() if isinstance(resume, str) else None
        if fork_session and not normalized_resume:
            return "Invalid session options: `fork_session` requires a non-empty `resume` session ID."

        namespace = self._namespace(agent)
        resolved_model = self._resolve_model(agent)
        self._ensure_namespace_config(namespace)
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)

        stderr_lines: collections.deque[str] = collections.deque(maxlen=_MAX_STDERR_LINES)
        stderr_callback = self._build_stderr_callback(stderr_lines)

        try:
            session, created = await self._session_manager.get_or_create(
                session_key,
                namespace,
                self._build_options(
                    stderr_callback,
                    model=resolved_model,
                    resume=normalized_resume,
                    fork_session=fork_session,
                ),
            )
            if created:
                session.stderr_lines = stderr_lines
            elif normalized_resume or fork_session:
                return (
                    f"Session `{session_key}` already exists; runtime `resume`/`fork_session` apply only when creating "
                    "a new session. Use a different `session_label` or call `claude_end_session` first."
                )
        except Exception as exc:
            return self._format_session_error(
                f"Failed to start Claude session: {exc}",
                session_key=session_key,
                model=resolved_model,
                resume=normalized_resume,
                fork_session=fork_session,
                stderr_lines=stderr_lines,
            )
        return session, created, session_key, resolved_model

    async def claude_start_session(
        self,
        session_label: str | None = None,
        resume: str | None = None,
        fork_session: bool = False,
        run_context: _RunContext | None = None,
        agent: _Agent | None = None,
    ) -> str:
        """Start or reuse a persistent Claude coding session for this conversation."""
        result = await self._get_or_create_session(
            session_label=session_label,
            resume=resume,
            fork_session=fork_session,
            run_context=run_context,
            agent=agent,
        )
        if isinstance(result, str):
            return result
        _session, created, session_key, _resolved_model = result
        action = "Started" if created else "Reusing"
        return f"{action} Claude session `{session_key}`."

    async def claude_send(
        self,
        prompt: str,
        session_label: str | None = None,
        resume: str | None = None,
        fork_session: bool = False,
        run_context: _RunContext | None = None,
        agent: _Agent | None = None,
    ) -> str:
        """Send a prompt to a persistent Claude session and return Claude's response."""
        trimmed_prompt = prompt.strip()
        if not trimmed_prompt:
            return "Prompt is required."

        acquire_result = await self._get_or_create_session(
            session_label=session_label,
            resume=resume,
            fork_session=fork_session,
            run_context=run_context,
            agent=agent,
        )
        if isinstance(acquire_result, str):
            return acquire_result
        session, _created, session_key, resolved_model = acquire_result

        normalized_resume = resume.strip() if isinstance(resume, str) else None
        response_text = ""
        tool_names: list[str] = []
        msg_result: ResultMessage | None = None
        turn_usage: dict[str, _UsageCounts] = {}
        session_error: str | None = None
        async with session.lock:
            session.last_used_at = monotonic()
            try:
                await session.client.query(trimmed_prompt)
                response_text, tool_names, msg_result = await self._collect_response(session)
                session.last_used_at = monotonic()
                if msg_result is not None and msg_result.model_usage is not None:
                    # Claude Code reports running session totals, so count only this turn's increase.
                    totals = {
                        model_id: (
                            usage["inputTokens"],
                            usage["outputTokens"],
                            usage["cacheReadInputTokens"],
                            usage["cacheCreationInputTokens"],
                        )
                        for model_id, usage in msg_result.model_usage.items()
                    }
                    turn_usage = _turn_usage(session.usage_totals, totals)
                    session.usage_totals = totals
                    self._session_manager.usage_totals[msg_result.session_id] = totals
            except ClaudeSDKError as exc:
                session_error = self._format_session_error(
                    f"Claude session error: {exc}",
                    session_key=session_key,
                    model=resolved_model,
                    resume=normalized_resume,
                    fork_session=fork_session,
                    stderr_lines=session.stderr_lines,
                )

        if session_error is not None:
            await self._session_manager.close(session_key)
            return session_error

        try:
            await _record_turn_usage(turn_usage)
        except Exception as error:
            # Claude has already done the work, so a usage write failure must not hide its reply.
            logger.warning("claude_session_usage_save_failed", error_type=type(error).__name__)
        return self._format_response_output(response_text, tool_names, msg_result)

    async def _collect_response(
        self,
        session: _ClaudeSessionState,
    ) -> tuple[str, list[str], ResultMessage | None]:
        text_parts: list[str] = []
        tool_names: list[str] = []
        result: ResultMessage | None = None

        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_names.append(block.name)
            elif isinstance(message, ConversationResetMessage):
                # /clear zeroes Claude Code's running totals, so the next totals start from nothing.
                session.usage_totals = {}
            elif isinstance(message, ResultMessage):
                result = message
                session.claude_session_id = message.session_id

        return _response_text(text_parts, result), tool_names, result

    def _format_response_output(
        self,
        response_text: str,
        tool_names: list[str],
        result: ResultMessage | None,
    ) -> str:
        lines = [response_text]
        if result is not None and result.total_cost_usd is not None:
            lines.append(f"[Claude session cost: ${result.total_cost_usd:.4f}]")
        if tool_names:
            unique_tools = ", ".join(sorted(set(tool_names)))
            lines.append(f"[Claude tools used: {unique_tools}]")
        return "\n\n".join(line for line in lines if line)

    async def claude_session_status(
        self,
        session_label: str | None = None,
        run_context: _RunContext | None = None,
        agent: _Agent | None = None,
    ) -> str:
        """Show status information for the current persistent Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        session = await self._session_manager.get(session_key)
        if session is None:
            return f"No active Claude session for `{session_key}`."

        now = monotonic()
        age_seconds = int(now - session.created_at)
        idle_seconds = int(now - session.last_used_at)
        claude_id = session.claude_session_id or "(not available yet)"
        return (
            f"Claude session `{session_key}` is active.\n"
            f"- age: {age_seconds}s\n"
            f"- idle: {idle_seconds}s\n"
            f"- claude_session_id: {claude_id}"
        )

    async def claude_interrupt(
        self,
        session_label: str | None = None,
        run_context: _RunContext | None = None,
        agent: _Agent | None = None,
    ) -> str:
        """Send an interrupt signal to an active Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        session = await self._session_manager.get(session_key)
        if session is None:
            return f"No active Claude session for `{session_key}`."

        try:
            await session.client.interrupt()
            session.last_used_at = monotonic()
        except ClaudeSDKError as exc:
            return f"Failed to interrupt Claude session: {exc}"
        return f"Interrupt sent to Claude session `{session_key}`."

    async def claude_end_session(
        self,
        session_label: str | None = None,
        run_context: _RunContext | None = None,
        agent: _Agent | None = None,
    ) -> str:
        """Close and remove an active Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        removed = await self._session_manager.close(session_key)
        if not removed:
            return f"No active Claude session for `{session_key}`."
        return f"Closed Claude session `{session_key}`."
