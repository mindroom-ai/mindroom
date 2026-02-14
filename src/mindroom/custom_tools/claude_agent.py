"""Claude Agent SDK-backed tools for persistent coding sessions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.tools import Toolkit
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from collections.abc import Callable as StdCallable

    from agno.agent import Agent
    from agno.run.base import RunContext
else:
    Agent = Any
    RunContext = Any
    StdCallable = Any


PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]
VALID_PERMISSION_MODES: tuple[PermissionMode, ...] = (
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
)
DEFAULT_PERMISSION_MODE: PermissionMode = "default"
DEFAULT_SESSION_TTL_MINUTES = 60
DEFAULT_MAX_SESSIONS = 200
MAX_STDERR_LINES = 12


def _parse_csv_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _normalize_permission_mode(permission_mode: str | None) -> PermissionMode:
    if not permission_mode:
        return DEFAULT_PERMISSION_MODE
    normalized = permission_mode.strip()
    if normalized not in VALID_PERMISSION_MODES:
        return DEFAULT_PERMISSION_MODE
    return cast("PermissionMode", normalized)


def _parse_int(value: int | None, *, default: int, minimum: int) -> int:
    if value is None:
        return default
    return max(minimum, value)


def _parse_optional_int(value: int | None, *, minimum: int) -> int | None:
    if value is None:
        return None
    return max(minimum, value)


@dataclass
class ClaudeSessionState:
    """Runtime state for one persistent Claude coding session."""

    key: str
    namespace: str
    client: ClaudeSDKClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=monotonic)
    last_used_at: float = field(default_factory=monotonic)
    ttl_seconds: int = DEFAULT_SESSION_TTL_MINUTES * 60
    claude_session_id: str | None = None
    stderr_lines: list[str] = field(default_factory=list)


class ClaudeSessionManager:
    """Process-wide manager for persistent ClaudeSDKClient sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClaudeSessionState] = {}
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
    ) -> tuple[ClaudeSessionState, bool]:
        """Get an existing session or create a new one for the given key."""
        async with self._lock:
            await self._cleanup_expired_locked()

            existing = self._sessions.get(session_key)
            if existing is not None:
                existing.last_used_at = monotonic()
                existing.ttl_seconds = self._namespace_ttl_seconds(namespace)
                return existing, False

            await self._evict_if_needed_locked(namespace)

            client = ClaudeSDKClient(options=options)
            await client.connect()
            session = ClaudeSessionState(
                key=session_key,
                namespace=namespace,
                client=client,
                ttl_seconds=self._namespace_ttl_seconds(namespace),
            )
            self._sessions[session_key] = session
            return session, True

    async def close(self, session_key: str) -> bool:
        """Close and remove a session by key."""
        async with self._lock:
            session = self._sessions.pop(session_key, None)
        if session is None:
            return False
        await self._disconnect(session)
        return True

    async def get(self, session_key: str) -> ClaudeSessionState | None:
        """Get a session by key, cleaning up expired sessions first."""
        async with self._lock:
            await self._cleanup_expired_locked()
            session = self._sessions.get(session_key)
            if session is not None:
                session.last_used_at = monotonic()
            return session

    async def _cleanup_expired_locked(self) -> None:
        now = monotonic()
        expired_keys = [
            key for key, session in self._sessions.items() if now - session.last_used_at > session.ttl_seconds
        ]
        for key in expired_keys:
            session = self._sessions.pop(key)
            await self._disconnect(session)

    async def _evict_if_needed_locked(self, namespace: str) -> None:
        max_sessions = self._namespace_max_sessions(namespace)
        while True:
            namespace_sessions = [key for key, session in self._sessions.items() if session.namespace == namespace]
            if len(namespace_sessions) < max_sessions:
                break
            oldest_key = min(namespace_sessions, key=lambda key: self._sessions[key].last_used_at)
            session = self._sessions.pop(oldest_key)
            await self._disconnect(session)

    def _namespace_ttl_seconds(self, namespace: str) -> int:
        return self._namespace_limits.get(namespace, (DEFAULT_SESSION_TTL_MINUTES * 60, DEFAULT_MAX_SESSIONS))[0]

    def _namespace_max_sessions(self, namespace: str) -> int:
        return self._namespace_limits.get(namespace, (DEFAULT_SESSION_TTL_MINUTES * 60, DEFAULT_MAX_SESSIONS))[1]

    async def _disconnect(self, session: ClaudeSessionState) -> None:
        async with session.lock:
            with suppress(Exception):
                await session.client.disconnect()


_SESSION_MANAGER = ClaudeSessionManager()


class ClaudeAgentTools(Toolkit):
    """Tools that let MindRoom agents run persistent Claude coding sessions."""

    def __init__(
        self,
        api_key: str | None = None,
        anthropic_base_url: str | None = None,
        anthropic_auth_token: str | None = None,
        disable_experimental_betas: bool = False,
        cwd: str | None = None,
        model: str | None = None,
        permission_mode: str | None = DEFAULT_PERMISSION_MODE,
        continue_conversation: bool = False,
        resume: str | None = None,
        fork_session: bool = False,
        allowed_tools: str | None = None,
        disallowed_tools: str | None = None,
        max_turns: int | None = None,
        system_prompt: str | None = None,
        cli_path: str | None = None,
        session_ttl_minutes: int | None = DEFAULT_SESSION_TTL_MINUTES,
        max_sessions: int | None = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self.api_key = api_key
        self.anthropic_base_url = anthropic_base_url
        self.anthropic_auth_token = anthropic_auth_token
        self.disable_experimental_betas = disable_experimental_betas
        self.cwd = cwd
        self.model = model
        self.permission_mode = _normalize_permission_mode(permission_mode)
        self.continue_conversation = continue_conversation
        self.resume = resume
        self.fork_session = fork_session
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.max_turns = _parse_optional_int(max_turns, minimum=1)
        self.system_prompt = system_prompt
        self.cli_path = cli_path
        self.session_ttl_minutes = _parse_int(
            session_ttl_minutes,
            default=DEFAULT_SESSION_TTL_MINUTES,
            minimum=1,
        )
        self.max_sessions = _parse_int(max_sessions, default=DEFAULT_MAX_SESSIONS, minimum=1)

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

    def _build_options(self, stderr_callback: StdCallable[[str], None] | None = None) -> ClaudeAgentOptions:
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
            model=self.model,
            permission_mode=self.permission_mode,
            continue_conversation=self.continue_conversation,
            resume=self.resume,
            fork_session=self.fork_session,
            allowed_tools=_parse_csv_list(self.allowed_tools),
            disallowed_tools=_parse_csv_list(self.disallowed_tools),
            max_turns=self.max_turns,
            system_prompt=self.system_prompt,
            cli_path=self.cli_path,
            env=env,
            stderr=stderr_callback,
        )

    def _build_stderr_collector(self) -> tuple[list[str], StdCallable[[str], None]]:
        stderr_lines: list[str] = []

        def _on_stderr(line: str) -> None:
            cleaned = line.strip()
            if not cleaned:
                return
            if len(stderr_lines) >= MAX_STDERR_LINES:
                stderr_lines.pop(0)
            stderr_lines.append(cleaned)

        return stderr_lines, _on_stderr

    def _format_session_error(
        self,
        message: str,
        *,
        session_key: str,
        stderr_lines: list[str],
    ) -> str:
        details = [
            "Session context:",
            f"- session_key: {session_key}",
            f"- continue_conversation: {self.continue_conversation}",
            f"- resume: {self.resume or '(none)'}",
            f"- fork_session: {self.fork_session}",
            f"- cwd: {self.cwd or '(default)'}",
        ]
        if self.resume:
            details.append("- note: `resume` must exist for the selected working-directory conversation context.")
        if stderr_lines:
            details.append("Recent Claude CLI stderr:")
            details.extend(f"- {line}" for line in stderr_lines[-5:])
        return "\n".join([message, *details])

    def _namespace(self, agent: Agent | None) -> str:
        agent_name = getattr(agent, "name", None)
        if isinstance(agent_name, str) and agent_name.strip():
            return agent_name.strip()
        return "mindroom"

    def _ensure_namespace_config(self, namespace: str) -> None:
        _SESSION_MANAGER.configure_namespace(
            namespace=namespace,
            ttl_minutes=self.session_ttl_minutes,
            max_sessions=self.max_sessions,
        )

    def _session_key(
        self,
        *,
        session_label: str | None,
        run_context: RunContext | None,
        agent: Agent | None,
    ) -> str:
        agent_name = self._namespace(agent)
        run_session = run_context.session_id if run_context is not None else "default"
        if session_label and session_label.strip():
            return f"{agent_name}:{run_session}:{session_label.strip()}"
        return f"{agent_name}:{run_session}"

    async def claude_start_session(
        self,
        session_label: str | None = None,
        run_context: RunContext | None = None,
        agent: Agent | None = None,
    ) -> str:
        """Start or reuse a persistent Claude coding session for this conversation."""
        namespace = self._namespace(agent)
        self._ensure_namespace_config(namespace)
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        stderr_lines, stderr_callback = self._build_stderr_collector()
        try:
            session, created = await _SESSION_MANAGER.get_or_create(
                session_key,
                namespace,
                self._build_options(stderr_callback),
            )
            if created:
                session.stderr_lines = stderr_lines
        except Exception as exc:
            return self._format_session_error(
                f"Failed to start Claude session: {exc}",
                session_key=session_key,
                stderr_lines=stderr_lines,
            )
        action = "Started" if created else "Reusing"
        return f"{action} Claude session `{session_key}`."

    async def claude_send(
        self,
        prompt: str,
        session_label: str | None = None,
        run_context: RunContext | None = None,
        agent: Agent | None = None,
    ) -> str:
        """Send a prompt to a persistent Claude session and return Claude's response."""
        trimmed_prompt = prompt.strip()
        if not trimmed_prompt:
            return "Prompt is required."

        namespace = self._namespace(agent)
        self._ensure_namespace_config(namespace)
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        startup_stderr_lines, stderr_callback = self._build_stderr_collector()

        try:
            session, created = await _SESSION_MANAGER.get_or_create(
                session_key,
                namespace,
                self._build_options(stderr_callback),
            )
            if created:
                session.stderr_lines = startup_stderr_lines
        except Exception as exc:
            return self._format_session_error(
                f"Failed to start Claude session: {exc}",
                session_key=session_key,
                stderr_lines=startup_stderr_lines,
            )

        response_text = ""
        tool_names: list[str] = []
        result: ResultMessage | None = None
        session_error: str | None = None
        async with session.lock:
            session.last_used_at = monotonic()
            try:
                await session.client.query(trimmed_prompt)
                response_text, tool_names, result = await self._collect_response(session)
            except ClaudeSDKError as exc:
                session_error = self._format_session_error(
                    f"Claude session error: {exc}",
                    session_key=session_key,
                    stderr_lines=session.stderr_lines,
                )
            except Exception as exc:
                session_error = self._format_session_error(
                    f"Unexpected Claude session error: {exc}",
                    session_key=session_key,
                    stderr_lines=session.stderr_lines,
                )

        if session_error is not None:
            await _SESSION_MANAGER.close(session_key)
            return session_error

        lines = [response_text]
        if result is not None and result.total_cost_usd is not None:
            lines.append(f"[Claude session cost: ${result.total_cost_usd:.4f}]")
        if tool_names:
            unique_tools = ", ".join(sorted(set(tool_names)))
            lines.append(f"[Claude tools used: {unique_tools}]")
        return "\n\n".join(line for line in lines if line)

    async def _collect_response(
        self,
        session: ClaudeSessionState,
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
            elif isinstance(message, ResultMessage):
                result = message
                session.claude_session_id = message.session_id

        response_text = "\n".join(part for part in text_parts if part).strip()
        if not response_text:
            if result is not None and result.result:
                response_text = str(result.result).strip()
            else:
                response_text = "Claude session completed without text output."
        if result is not None and result.is_error:
            return f"Claude reported an error: {response_text}", tool_names, result
        return response_text, tool_names, result

    async def claude_session_status(
        self,
        session_label: str | None = None,
        run_context: RunContext | None = None,
        agent: Agent | None = None,
    ) -> str:
        """Show status information for the current persistent Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        session = await _SESSION_MANAGER.get(session_key)
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
        run_context: RunContext | None = None,
        agent: Agent | None = None,
    ) -> str:
        """Send an interrupt signal to an active Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        session = await _SESSION_MANAGER.get(session_key)
        if session is None:
            return f"No active Claude session for `{session_key}`."

        async with session.lock:
            try:
                await session.client.interrupt()
            except ClaudeSDKError as exc:
                return f"Failed to interrupt Claude session: {exc}"
        return f"Interrupt sent to Claude session `{session_key}`."

    async def claude_end_session(
        self,
        session_label: str | None = None,
        run_context: RunContext | None = None,
        agent: Agent | None = None,
    ) -> str:
        """Close and remove an active Claude session."""
        session_key = self._session_key(session_label=session_label, run_context=run_context, agent=agent)
        removed = await _SESSION_MANAGER.close(session_key)
        if not removed:
            return f"No active Claude session for `{session_key}`."
        return f"Closed Claude session `{session_key}`."
