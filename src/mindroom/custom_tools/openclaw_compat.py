"""OpenClaw-compatible toolkit surface for incremental parity work."""

from __future__ import annotations

import json

from agno.tools import Toolkit


class OpenClawCompatTools(Toolkit):
    """OpenClaw-style tool names exposed as a single toolkit.

    The initial implementation is a contract scaffold that returns structured
    placeholder payloads. Behavior will be implemented incrementally in
    follow-up phases.
    """

    def __init__(self) -> None:
        """Initialize the OpenClaw compatibility toolkit."""
        super().__init__(
            name="openclaw_compat",
            tools=[
                self.agents_list,
                self.session_status,
                self.sessions_list,
                self.sessions_history,
                self.sessions_send,
                self.sessions_spawn,
                self.subagents,
                self.message,
                self.gateway,
                self.nodes,
                self.canvas,
            ],
        )

    @staticmethod
    def _placeholder(tool_name: str, **kwargs: object) -> str:
        """Return a structured placeholder result for unimplemented tools."""
        payload: dict[str, object] = {
            "status": "not_implemented",
            "tool": tool_name,
        }
        if kwargs:
            payload["args"] = kwargs
        return json.dumps(payload, sort_keys=True)

    async def agents_list(self) -> str:
        """List agent ids available for `sessions_spawn` targeting."""
        return self._placeholder("agents_list")

    async def session_status(
        self,
        session_key: str | None = None,
        model: str | None = None,
    ) -> str:
        """Show status information for a session and optional model override."""
        return self._placeholder("session_status", session_key=session_key, model=model)

    async def sessions_list(
        self,
        kinds: list[str] | None = None,
        limit: int | None = None,
        active_minutes: int | None = None,
        message_limit: int | None = None,
    ) -> str:
        """List sessions with optional filters and message previews."""
        return self._placeholder(
            "sessions_list",
            kinds=kinds,
            limit=limit,
            active_minutes=active_minutes,
            message_limit=message_limit,
        )

    async def sessions_history(
        self,
        session_key: str,
        limit: int | None = None,
        include_tools: bool = False,
    ) -> str:
        """Fetch transcript history for one session."""
        return self._placeholder(
            "sessions_history",
            session_key=session_key,
            limit=limit,
            include_tools=include_tools,
        )

    async def sessions_send(
        self,
        message: str,
        session_key: str | None = None,
        label: str | None = None,
        agent_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """Send a message to another session."""
        return self._placeholder(
            "sessions_send",
            message=message,
            session_key=session_key,
            label=label,
            agent_id=agent_id,
            timeout_seconds=timeout_seconds,
        )

    async def sessions_spawn(
        self,
        task: str,
        label: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        run_timeout_seconds: int | None = None,
        timeout_seconds: int | None = None,
        cleanup: str | None = None,
    ) -> str:
        """Spawn an isolated background session."""
        return self._placeholder(
            "sessions_spawn",
            task=task,
            label=label,
            agent_id=agent_id,
            model=model,
            run_timeout_seconds=run_timeout_seconds,
            timeout_seconds=timeout_seconds,
            cleanup=cleanup,
        )

    async def subagents(
        self,
        action: str = "list",
        target: str | None = None,
        message: str | None = None,
        recent_minutes: int | None = None,
    ) -> str:
        """Inspect or control spawned sub-agent runs."""
        return self._placeholder(
            "subagents",
            action=action,
            target=target,
            message=message,
            recent_minutes=recent_minutes,
        )

    async def message(
        self,
        action: str = "send",
        message: str | None = None,
        channel: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Send or manage cross-channel messages."""
        return self._placeholder(
            "message",
            action=action,
            message=message,
            channel=channel,
            target=target,
            thread_id=thread_id,
        )

    async def gateway(
        self,
        action: str,
        raw: str | None = None,
        base_hash: str | None = None,
        note: str | None = None,
    ) -> str:
        """Invoke gateway lifecycle/config operations."""
        return self._placeholder(
            "gateway",
            action=action,
            raw=raw,
            base_hash=base_hash,
            note=note,
        )

    async def nodes(
        self,
        action: str,
        node: str | None = None,
    ) -> str:
        """Invoke node discovery and control operations."""
        return self._placeholder("nodes", action=action, node=node)

    async def canvas(
        self,
        action: str,
        node: str | None = None,
        target: str | None = None,
        url: str | None = None,
        java_script: str | None = None,
    ) -> str:
        """Control canvas operations on a node."""
        return self._placeholder(
            "canvas",
            action=action,
            node=node,
            target=target,
            url=url,
            java_script=java_script,
        )
