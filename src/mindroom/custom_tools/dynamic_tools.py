"""Session-scoped dynamic toolkit management tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.tool_system.dynamic_toolkits import (
    DynamicToolkitConflictError,
    get_loaded_toolkits_for_session,
    merge_runtime_tool_configs,
    save_loaded_toolkits_for_session,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config


class DynamicToolsToolkit(Toolkit):
    """Manage which configured toolkits are loaded for the active session."""

    def __init__(
        self,
        *,
        agent_name: str,
        config: Config,
        session_id: str | None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._session_id = session_id
        super().__init__(
            name="dynamic_tools",
            instructions=(
                "Manage optional toolkits for this session. "
                "Use list_toolkits() when unsure. "
                "load_tools() and unload_tools() apply on the next request in the same session."
            ),
            tools=[self.list_toolkits, self.load_tools, self.unload_tools],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "dynamic_tools"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    def _loaded_toolkits(self) -> list[str]:
        return get_loaded_toolkits_for_session(
            agent_name=self._agent_name,
            config=self._config,
            session_id=self._session_id,
        )

    def _allowed_toolkits(self) -> list[str]:
        return list(self._config.get_agent(self._agent_name).allowed_toolkits)

    def _initial_toolkits(self) -> set[str]:
        return set(self._config.get_agent(self._agent_name).initial_toolkits)

    def _scope_incompatible_tools(self, toolkit_name: str) -> list[str]:
        return self._config.get_toolkit_scope_incompatible_tools(self._agent_name, toolkit_name)

    def _toolkit_entry(self, toolkit_name: str, *, loaded_toolkits: set[str]) -> dict[str, object]:
        toolkit = self._config.get_toolkit(toolkit_name)
        return {
            "name": toolkit_name,
            "description": toolkit.description,
            "tool_names": [entry.name for entry in self._config.get_toolkit_tool_configs(toolkit_name)],
            "loaded": toolkit_name in loaded_toolkits,
            "sticky": toolkit_name in self._initial_toolkits(),
        }

    def _session_error(self, *, toolkit: str | None = None, loaded_toolkits: list[str] | None = None) -> str:
        payload: dict[str, object] = {
            "message": "Dynamic toolkit changes require a stable session_id.",
        }
        if toolkit is not None:
            payload["toolkit"] = toolkit
        if loaded_toolkits is not None:
            payload["loaded_toolkits"] = loaded_toolkits
        return self._payload("error", **payload)

    def list_toolkits(self) -> str:
        """List the agent's allowed dynamic toolkits and current loaded state."""
        loaded_toolkits = self._loaded_toolkits()
        loaded_set = set(loaded_toolkits)
        allowed_toolkits = self._allowed_toolkits()
        return self._payload(
            "ok",
            loaded_toolkits=loaded_toolkits,
            toolkits=[
                self._toolkit_entry(toolkit_name, loaded_toolkits=loaded_set) for toolkit_name in allowed_toolkits
            ],
        )

    def _load_tools_precheck(self, toolkit: str, loaded_toolkits: list[str], allowed_toolkits: list[str]) -> str | None:
        """Return an early load_tools response when the request is invalid."""
        if toolkit not in self._config.toolkits:
            return self._payload(
                "unknown",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                message=f"Unknown toolkit '{toolkit}'.",
                allowed_toolkits=allowed_toolkits,
            )

        if toolkit not in allowed_toolkits:
            return self._payload(
                "not_allowed",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                message=f"Toolkit '{toolkit}' is not allowed for agent '{self._agent_name}'.",
                allowed_toolkits=allowed_toolkits,
            )

        incompatible_tools = self._scope_incompatible_tools(toolkit)
        if incompatible_tools:
            scope_label = self._config.get_agent_scope_label(self._agent_name)
            return self._payload(
                "scope_incompatible",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                scope_label=scope_label,
                unsupported_tools=incompatible_tools,
                message=(
                    f"Toolkit '{toolkit}' cannot be loaded for agent '{self._agent_name}' because it includes "
                    f"shared-only integrations not supported for {scope_label}: {', '.join(incompatible_tools)}."
                ),
            )

        if toolkit in loaded_toolkits:
            return self._payload(
                "already_loaded",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                takes_effect="next_request",
                message=f"Toolkit '{toolkit}' is already loaded for this session.",
            )

        if self._session_id is None:
            return self._session_error(toolkit=toolkit, loaded_toolkits=loaded_toolkits)

        return None

    def load_tools(self, toolkit: str) -> str:
        """Load one allowed toolkit for the current session.

        The requested toolkit becomes available on the next request in the same
        session, not later in the current model run.
        """
        loaded_toolkits = self._loaded_toolkits()
        allowed_toolkits = self._allowed_toolkits()
        precheck = self._load_tools_precheck(toolkit, loaded_toolkits, allowed_toolkits)
        if precheck is not None:
            return precheck

        candidate_loaded_toolkits = [*loaded_toolkits, toolkit]
        try:
            merge_runtime_tool_configs(
                agent_name=self._agent_name,
                config=self._config,
                loaded_toolkits=candidate_loaded_toolkits,
            )
        except DynamicToolkitConflictError as exc:
            return self._payload(
                "conflict",
                toolkit=toolkit,
                conflicting_tool=exc.tool_name,
                loaded_toolkits=loaded_toolkits,
                message=str(exc),
                existing_overrides=exc.existing_overrides,
                candidate_overrides=exc.candidate_overrides,
            )

        save_loaded_toolkits_for_session(
            session_id=self._session_id,
            loaded_toolkits=candidate_loaded_toolkits,
        )
        saved_loaded_toolkits = self._loaded_toolkits()
        return self._payload(
            "loaded",
            toolkit=toolkit,
            loaded_toolkits=saved_loaded_toolkits,
            takes_effect="next_request",
            message=f"Toolkit '{toolkit}' will be available on the next request in this session.",
        )

    def unload_tools(self, toolkit: str) -> str:
        """Unload one toolkit from the current session.

        The toolkit stops being available on the next request in the same
        session, not later in the current model run.
        """
        loaded_toolkits = self._loaded_toolkits()
        allowed_toolkits = self._allowed_toolkits()
        if toolkit not in self._config.toolkits:
            return self._payload(
                "unknown",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                message=f"Unknown toolkit '{toolkit}'.",
                allowed_toolkits=allowed_toolkits,
            )

        if toolkit not in allowed_toolkits:
            return self._payload(
                "not_allowed",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                message=f"Toolkit '{toolkit}' is not allowed for agent '{self._agent_name}'.",
                allowed_toolkits=allowed_toolkits,
            )

        if toolkit in self._initial_toolkits():
            return self._payload(
                "sticky",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                message=f"Toolkit '{toolkit}' is sticky because it is configured in initial_toolkits.",
            )

        if toolkit not in loaded_toolkits:
            return self._payload(
                "not_loaded",
                toolkit=toolkit,
                loaded_toolkits=loaded_toolkits,
                takes_effect="next_request",
                message=f"Toolkit '{toolkit}' is not currently loaded for this session.",
            )

        if self._session_id is None:
            return self._session_error(toolkit=toolkit, loaded_toolkits=loaded_toolkits)

        save_loaded_toolkits_for_session(
            session_id=self._session_id,
            loaded_toolkits=[name for name in loaded_toolkits if name != toolkit],
        )
        saved_loaded_toolkits = self._loaded_toolkits()
        return self._payload(
            "unloaded",
            toolkit=toolkit,
            loaded_toolkits=saved_loaded_toolkits,
            takes_effect="next_request",
            message=f"Toolkit '{toolkit}' will be removed on the next request in this session.",
        )
