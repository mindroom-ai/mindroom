"""Tool wrapper for privacy-safe retained usage statistics."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.requester_identity import is_platform_administrator, resolve_human_requester_alias
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
)
from mindroom.tools.native_helpers import restrict_actions
from mindroom.usage.stats import collect_admin_usage, collect_private_usage, collect_self_usage

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.tool_system.tool_function import ToolFunction

_UsageAction = Literal["self", "private", "all"]
_STANDARD_ACTIONS: tuple[_UsageAction, ...] = ("self", "private")


class UsageStatsTools(Toolkit):
    """Expose retained usage summaries for the current requester or authorized administrators."""

    def __init__(self, *, agent_name: str | None = None, admin_scope: bool = False) -> None:
        self._agent_name = agent_name
        self._admin_scope = admin_scope
        super().__init__(name="usage_stats", tools=[self.usage_stats])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Hide the admin-only `all` action from model schemas when `admin_scope` is disabled."""
        if function.name == "usage_stats" and not self._admin_scope:
            return restrict_actions(function, _STANDARD_ACTIONS)
        return function

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("usage_stats", status, **fields)

    @classmethod
    def _error(cls, code: str, message: str) -> str:
        return cls._payload("error", code=code, message=message)

    @classmethod
    def _context_or_error(cls) -> ToolRuntimeContext | str:
        context = get_tool_runtime_context()
        if context is None or not context.agent_name or not context.requester_id:
            return cls._error(
                "context_unavailable",
                "Usage statistics are unavailable without an active requester context.",
            )
        return context

    def _admin_context_or_error(self) -> tuple[ToolRuntimeContext, Config] | str:
        if not self._admin_scope:
            return self._error(
                "authorization_error",
                "Usage statistics admin scope is not enabled for this tool.",
            )
        resolved = self._context_or_error()
        if isinstance(resolved, str):
            return resolved
        if self._agent_name is None or resolved.agent_name != self._agent_name:
            return self._error(
                "authorization_error",
                "Usage statistics admin scope is not enabled for this agent.",
            )
        config = resolved.current_config
        if not is_platform_administrator(resolved.requester_id, config, resolved.runtime_paths):
            return self._error(
                "authorization_error",
                "Usage statistics admin access is not authorized for this requester.",
            )
        return resolved, config

    async def usage_stats(self, action: _UsageAction, include_daily: bool = False) -> str:
        """Return retained token usage summaries (set `include_daily=True` for UTC-day breakdowns).

        - `self`: current agent and canonical requester usage.
        - `private`: requester's private agents usage.
        - `all`: all sources (requires admin scope and platform administrator).

        """
        if action == "self":
            return await self._self_usage(include_daily=include_daily)
        if action == "private":
            return await self._private_usage(include_daily=include_daily)
        if action == "all":
            return await self._all_usage(include_daily=include_daily)
        return self._error("invalid_action", f"Unsupported usage_stats action {action!r}.")

    async def _self_usage(self, *, include_daily: bool = False) -> str:
        resolved = self._context_or_error()
        if isinstance(resolved, str):
            return resolved
        if self._agent_name is None:
            return self._error(
                "context_unavailable",
                "Usage statistics are unavailable without a bound agent identity.",
            )
        context = resolved
        config = context.current_config
        requester_id = resolve_human_requester_alias(
            context.requester_id,
            config,
            context.runtime_paths,
        )
        report = await asyncio.to_thread(
            collect_self_usage,
            agent_name=self._agent_name,
            requester_id=requester_id,
            config=config,
            runtime_paths=context.runtime_paths,
            execution_identity=context.execution_identity,
            include_daily=include_daily,
        )
        return self._payload("ok", **report.to_dict())

    async def _private_usage(self, *, include_daily: bool = False) -> str:
        context = self._context_or_error()
        if isinstance(context, str):
            return context
        report = await asyncio.to_thread(
            collect_private_usage,
            requester_id=context.requester_id,
            config=context.current_config,
            runtime_paths=context.runtime_paths,
            include_daily=include_daily,
        )
        return self._payload("ok", **report.to_dict())

    async def _all_usage(self, *, include_daily: bool = False) -> str:
        resolved = self._admin_context_or_error()
        if isinstance(resolved, str):
            return resolved
        context, config = resolved
        report = await asyncio.to_thread(
            collect_admin_usage,
            config=config,
            runtime_paths=context.runtime_paths,
            include_daily=include_daily,
        )
        return self._payload("ok", **report.to_dict())
