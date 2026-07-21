"""Session-scoped dynamic tool management tools."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.tool_system.dynamic_toolkits import (
    LoadToolResult,
    LoadToolValidationFailure,
    deferred_tool_catalog_entries,
    get_loaded_tools_for_session,
    load_tool_for_session,
    unload_tool_for_session,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.tool_system.dynamic_toolkits import DeferredToolCatalogEntry


_WORD_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class DynamicToolsToolkit(Toolkit):
    """Manage which configured deferred tools are loaded for the active session."""

    def __init__(
        self,
        *,
        agent_name: str,
        config: Config,
        session_id: str | None,
        stop_after_tool_call: bool = False,
        hidden_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._session_id = session_id
        self._hidden_tool_names = hidden_tool_names
        super().__init__(
            name="dynamic_tools",
            instructions=config.get_prompt("DYNAMIC_TOOLS_TOOLKIT_INSTRUCTIONS"),
            tools=[self.list_tools, self.load_tool, self.unload_tool, self.tool_search],
        )
        # Same-turn continuation is driven by the shared response-turn drivers
        # (standalone agents and materialized team members). Embedded agents
        # without such a loop run with it off, so only stop the provider loop
        # when the caller will resume the turn.
        for tool_name in ("load_tool", "unload_tool"):
            self.functions[tool_name].stop_after_tool_call = stop_after_tool_call

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "dynamic_tools"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    def _loaded_tools(self) -> list[str]:
        return self._filter_visible_tool_names(
            get_loaded_tools_for_session(
                agent_name=self._agent_name,
                config=self._config,
                session_id=self._session_id,
            ),
        )

    def _filter_visible_tool_names(self, tool_names: list[str] | tuple[str, ...]) -> list[str]:
        return [tool_name for tool_name in tool_names if tool_name not in self._hidden_tool_names]

    def _deferred_entries(self, loaded_tools: list[str] | None = None) -> list[DeferredToolCatalogEntry]:
        return [
            entry
            for entry in deferred_tool_catalog_entries(
                agent_name=self._agent_name,
                config=self._config,
                loaded_tools=loaded_tools if loaded_tools is not None else self._loaded_tools(),
            )
            if entry.name not in self._hidden_tool_names
        ]

    def _deferred_tool_names(self) -> list[str]:
        return self._filter_visible_tool_names(
            [entry.name for entry in self._config.resolve_entity(self._agent_name).authored_deferred_tool_configs],
        )

    def _initial_tools(self) -> set[str]:
        return {
            entry.name
            for entry in self._config.resolve_entity(self._agent_name).authored_deferred_tool_configs
            if entry.initial and entry.name not in self._hidden_tool_names
        }

    def _mcp_load_validation_failure(self, loaded_tools: list[str]) -> LoadToolValidationFailure | None:
        manager = require_mcp_server_manager()
        if manager is None:
            return None

        unavailable_messages = manager.mcp_tool_unavailable_messages_for_loaded_tools(self._agent_name, loaded_tools)
        if unavailable_messages:
            return LoadToolValidationFailure(
                status="tool_unavailable",
                messages=tuple(unavailable_messages),
            )

        collision_messages = manager.function_name_collision_messages_for_loaded_tools(self._agent_name, loaded_tools)
        if collision_messages:
            return LoadToolValidationFailure(
                status="function_name_collision",
                messages=tuple(collision_messages),
            )
        return None

    @staticmethod
    def _tool_entry(entry: DeferredToolCatalogEntry) -> dict[str, object]:
        return {
            "name": entry.name,
            "description": entry.description,
            "loaded": entry.loaded,
            "sticky": entry.sticky,
        }

    def _session_error(self, *, tool_name: str | None = None, loaded_tools: list[str] | None = None) -> str:
        payload: dict[str, object] = {
            "message": "Dynamic tool changes require a stable session_id.",
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if loaded_tools is not None:
            payload["loaded_tools"] = loaded_tools
        return self._payload("error", **payload)

    def _load_tool_response(self, tool_name: str, result: LoadToolResult) -> str:
        loaded_tools = self._filter_visible_tool_names(result.loaded_tools)
        if result.status == "unknown":
            response = self._payload(
                "unknown",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=f"Unknown deferred tool '{tool_name}'.",
                available_tools=self._filter_visible_tool_names(result.available_tools),
            )
        elif result.status == "scope_incompatible":
            scope_label = self._config.resolve_entity(self._agent_name).scope_label
            unsupported_tools = list(result.unsupported_tools)
            response = self._payload(
                "scope_incompatible",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                scope_label=scope_label,
                unsupported_tools=unsupported_tools,
                message=(
                    f"Tool '{tool_name}' cannot be loaded for agent '{self._agent_name}' because its expanded "
                    f"tool set includes shared-only integrations not supported for {scope_label}: "
                    f"{', '.join(unsupported_tools)}."
                ),
            )
        elif result.status == "already_loaded":
            response = self._payload(
                "already_loaded",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=f"Tool '{tool_name}' is already loaded for this session.",
            )
        elif result.status == "error":
            response = self._session_error(tool_name=tool_name, loaded_tools=loaded_tools)
        elif result.status == "function_name_collision":
            response = self._payload(
                "function_name_collision",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                collision_messages=list(result.collision_messages),
                message=(
                    f"Tool '{tool_name}' cannot be loaded because its provider-visible function names "
                    "collide with an MCP server visible to this agent."
                ),
            )
        elif result.status == "tool_unavailable":
            response = self._payload(
                "tool_unavailable",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                unavailable_messages=list(result.unavailable_messages),
                message=f"Tool '{tool_name}' cannot be loaded because its MCP server is unavailable.",
            )
        else:
            response = self._payload(
                "loaded",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=(
                    f"Tool '{tool_name}' is now loaded for this session. It becomes callable once it appears "
                    "in your available tools; do not call it in the same parallel tool-call batch as load_tool."
                ),
            )
        return response

    def list_tools(self) -> str:
        """List deferred tools for this agent and the current loaded state."""
        loaded_tools = self._loaded_tools()
        deferred_entries = self._deferred_entries(loaded_tools)
        return self._payload(
            "ok",
            loaded_tools=loaded_tools,
            total_deferred=len(deferred_entries),
            tools=[self._tool_entry(entry) for entry in deferred_entries],
        )

    def load_tool(self, tool_name: str) -> str:
        """Load one deferred tool for the current session.

        The tool becomes callable once it appears in the agent's available
        tools; it is never callable in the same parallel batch as this call.
        """
        if tool_name in self._hidden_tool_names:
            return self._payload(
                "unknown",
                tool_name=tool_name,
                loaded_tools=self._loaded_tools(),
                message=f"Unknown deferred tool '{tool_name}'.",
                available_tools=self._deferred_tool_names(),
            )

        result = load_tool_for_session(
            agent_name=self._agent_name,
            config=self._config,
            session_id=self._session_id,
            tool_name=tool_name,
            validate_loaded_tools=self._mcp_load_validation_failure,
        )
        return self._load_tool_response(tool_name, result)

    def unload_tool(self, tool_name: str) -> str:
        """Unload one deferred tool from the current session."""
        loaded_tools = self._loaded_tools()
        deferred_tools = self._deferred_tool_names()
        if tool_name not in deferred_tools:
            return self._payload(
                "unknown",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=f"Unknown deferred tool '{tool_name}'.",
                available_tools=deferred_tools,
            )

        if tool_name in self._initial_tools():
            return self._payload(
                "sticky",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=f"Tool '{tool_name}' is sticky because it is configured with initial=true.",
            )

        if tool_name not in loaded_tools:
            return self._payload(
                "not_loaded",
                tool_name=tool_name,
                loaded_tools=loaded_tools,
                message=f"Tool '{tool_name}' is not currently loaded for this session.",
            )

        if self._session_id is None:
            return self._session_error(tool_name=tool_name, loaded_tools=loaded_tools)

        saved_loaded_tools = unload_tool_for_session(
            agent_name=self._agent_name,
            config=self._config,
            session_id=self._session_id,
            tool_name=tool_name,
        )
        return self._payload(
            "unloaded",
            tool_name=tool_name,
            loaded_tools=self._filter_visible_tool_names(saved_loaded_tools),
            message=f"Tool '{tool_name}' is now unloaded for this session.",
        )

    @staticmethod
    def _search_score(entry: DeferredToolCatalogEntry, query_tokens: set[str], raw_query: str) -> int:
        if not query_tokens:
            return 0
        name = entry.name.lower()
        score = 0
        if raw_query == name:
            score = 100
        elif query_tokens & _tokens(name):
            score = 80
        else:
            display_function_tokens = _tokens(entry.display_name)
            for function_name in entry.function_names:
                display_function_tokens.update(_tokens(function_name))
            if query_tokens & display_function_tokens:
                score = 60
            elif query_tokens & _tokens(entry.description):
                score = 40
            elif query_tokens & _tokens(entry.category):
                score = 20
        return score

    def tool_search(self, query: str, max_results: int = 5) -> str:
        """Search deferred tools by exact name and plain keywords without loading schemas."""
        raw_query = query.strip().lower()
        query_tokens = _tokens(raw_query)
        max_count = max(1, min(max_results, 20))
        loaded_tools = self._loaded_tools()
        deferred_entries = self._deferred_entries(loaded_tools)
        scored_entries = [
            (self._search_score(entry, query_tokens, raw_query), index, entry)
            for index, entry in enumerate(deferred_entries)
        ]
        matches = [
            entry for score, _index, entry in sorted(scored_entries, key=lambda item: (-item[0], item[1])) if score > 0
        ][:max_count]
        return self._payload(
            "ok",
            matches=[self._tool_entry(entry) for entry in matches],
            loaded_tools=loaded_tools,
            total_deferred=len(deferred_entries),
        )
