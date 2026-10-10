"""Egress broker credential status tool for MindRoom agents.

When a brokered request fails with ``credential_not_configured``, the agent
calls this tool to tell the user which services the egress broker can inject
credentials for, which of them have a key in this agent's scope, and where to
add the missing ones. The tool reports names and set or unset flags only; it
never reads a secret value into its output.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.secrets import secret_status
from mindroom.egress_broker.service import manage_url
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_NO_WORKER_NOTE = (
    "No worker identity is available for this agent, so there is no secret scope to check. "
    "Egress credentials require a worker-scoped agent; ask the operator to give this agent a worker scope."
)
_NO_CONFIG_NOTE = "The egress broker configuration is not available in this context, so no services can be listed."
_NO_SERVICES_NOTE = (
    "No egress services are configured. An operator defines them under `egress_broker.services` in config.yaml."
)


class EgressCredentialsTools(Toolkit):
    """Tool that lists the egress broker services and whether this agent has a key for each."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._worker_target = worker_target
        super().__init__(name="egress_credentials", tools=[self.list_egress_credentials])

    def list_egress_credentials(self) -> str:
        """List the API key services this agent can use through the egress broker and where to add missing keys.

        Call this when a request fails with `credential_not_configured`, or before telling the user
        which key they need. Returns each configured service with whether a key is set for this agent
        (`configured`), and the link where the user adds or replaces keys. Never ask the user to paste
        a key into the chat; send them to the link instead.
        """
        link = manage_url(self._runtime_paths)
        if self._worker_target is None:
            return self._payload([], link, _NO_WORKER_NOTE)
        context = get_tool_runtime_context()
        if context is None:
            return self._payload([], link, _NO_CONFIG_NOTE)
        services = context.current_config.egress_broker.services
        if not services:
            return self._payload([], link, _NO_SERVICES_NOTE)
        manager = get_runtime_credentials_manager(self._runtime_paths)
        entries = [
            {
                "name": name,
                "display_name": service.display_name or name,
                "configured": secret_status(manager, self._worker_target, name).configured,
            }
            for name, service in services.items()
        ]
        return self._payload(entries, link, self._note(link))

    @staticmethod
    def _note(link: str | None) -> str:
        where = (
            f"add the key at {link}"
            if link is not None
            else "ask the operator where egress credentials are managed (the dashboard Credentials tab)"
        )
        return (
            "Services with `configured: false` have no key in this agent's scope, so brokered requests "
            f"to them fail with `credential_not_configured`. To fix one, {where}. "
            "Never ask the user to paste a key into the chat."
        )

    @staticmethod
    def _payload(services: list[dict[str, str | bool]], link: str | None, note: str) -> str:
        return json.dumps(
            {"tool": "egress_credentials", "services": services, "manage_url": link, "note": note},
            sort_keys=True,
        )
