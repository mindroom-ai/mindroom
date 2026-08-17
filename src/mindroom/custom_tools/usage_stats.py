"""Tool wrapper for privacy-safe retained usage statistics."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
)
from mindroom.usage_stats import collect_admin_usage, collect_self_usage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config


class UsageStatsTools(Toolkit):
    """Expose retained usage summaries for the current requester or authorized administrators."""

    def __init__(self, *, agent_name: str | None = None, admin_scope: bool = False) -> None:
        self._agent_name = agent_name
        self._admin_scope = admin_scope
        functions: list[Callable[..., Awaitable[str]]] = [self.get_my_usage]
        if admin_scope:
            functions.append(self.get_all_usage)
        super().__init__(name="usage_stats", tools=functions)

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("usage_stats", status, **fields)

    @classmethod
    def _error(cls, code: str, message: str) -> str:
        return cls._payload("error", code=code, message=message)

    @classmethod
    def _context_or_error(cls) -> ToolRuntimeContext | str:
        context = get_tool_runtime_context()
        if context is None:
            return cls._error(
                "context_unavailable",
                "Usage statistics are unavailable without an active requester context.",
            )
        if (
            not context.agent_name
            or not context.requester_id
            or context.config is None
            or context.runtime_paths is None
        ):
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
        canonical_requester = config.authorization.resolve_alias(resolved.requester_id)
        if canonical_requester not in config.authorization.global_users:
            return self._error(
                "authorization_error",
                "Usage statistics admin access is not authorized for this requester.",
            )
        return resolved, config

    async def get_my_usage(self) -> str:
        """Return retained usage for the current agent and canonical requester."""
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
        requester_id = config.authorization.resolve_alias(context.requester_id)
        execution_identity = replace(
            build_execution_identity_from_runtime_context(context),
            agent_name=self._agent_name,
        )
        report = await asyncio.to_thread(
            collect_self_usage,
            agent_name=self._agent_name,
            requester_id=requester_id,
            config=config,
            runtime_paths=context.runtime_paths,
            execution_identity=execution_identity,
        )
        return self._payload("ok", **report.to_dict())

    async def get_all_usage(self) -> str:
        """Return retained usage for all sources when both admin gates grant access."""
        resolved = self._admin_context_or_error()
        if isinstance(resolved, str):
            return resolved
        context, config = resolved
        report = await asyncio.to_thread(
            collect_admin_usage,
            config=config,
            runtime_paths=context.runtime_paths,
        )
        return self._payload("ok", **report.to_dict())
