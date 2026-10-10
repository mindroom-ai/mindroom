"""Egress broker credential status tool for MindRoom agents.

When a brokered request fails with ``credential_not_configured``, the agent
calls this tool to tell the user which services the egress broker can inject
credentials for, which of them have a key or a connected account in this
agent's scope, and where to add the missing ones. The tool reports names and
flags only; it never reads a secret, access token, or connect link into its
output.
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import service_status
from mindroom.egress_broker.service import manage_url
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_NO_WORKER_NOTE = (
    "No worker identity is available for this agent, so there is no secret scope to check. "
    "Egress credentials require a worker-scoped agent; ask the operator to give this agent a worker scope."
)
_NO_CONFIG_NOTE = "The egress broker configuration is not available in this context, so no services can be listed."
_NO_SERVICES_NOTE = (
    "No egress services are configured. An operator defines them under `egress_broker.services` in config.yaml."
)


class EgressCredentialsTools(Toolkit):
    """Tool that lists the egress broker services and whether this agent has a key or account for each."""

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
        """List the services this agent can use through the egress broker and how to give it credentials.

        Call this when a request fails with `credential_not_configured`, or before telling the user
        which credential they need. Returns each configured service with whether this agent has a
        credential for it (`configured`) and which one the broker uses (`active_source`: `key` or `oauth`).
        A service with nothing configured also says whether the user can connect an account instead of
        adding a key (`can_connect_account`, with the account's `provider` when it can). The result carries the link
        where the user connects accounts and adds or replaces keys. Never ask the user to paste a key into
        the chat; send them to the link instead.
        """
        link = manage_url(self._runtime_paths)
        if self._worker_target is None:
            return self._payload([], link, _NO_WORKER_NOTE)
        context = get_tool_runtime_context()
        if context is None:
            return self._payload([], link, _NO_CONFIG_NOTE)
        config = context.current_config
        services = config.egress_broker.services
        if not services:
            return self._payload([], link, _NO_SERVICES_NOTE)
        manager = get_runtime_credentials_manager(self._runtime_paths)
        entries = [self._entry(manager, config, name, service) for name, service in services.items()]
        return self._payload(entries, link, self._note(link))

    def _entry(
        self,
        manager: CredentialsManager,
        config: Config,
        name: str,
        service: EgressService,
    ) -> dict[str, str | bool | None]:
        """Describe one service's credential sources in this agent's scope, as the broker would use them.

        A service whose status cannot be read is reported as not configured and not connectable, so one failing
        provider or unreadable secret does not fail the listing of the others. Only the error type is logged.
        """
        try:
            status = service_status(
                manager,
                self._worker_target,
                service,
                name,
                oauth_status=functools.partial(
                    oauth_status,
                    service=name,
                    config=config,
                    runtime_paths=self._runtime_paths,
                    credentials_manager=manager,
                ),
            )
        except Exception as exc:
            logger.warning("egress_credentials_status_failed", service=name, error_type=type(exc).__name__)
            return {
                "name": name,
                "display_name": service.display_name or name,
                "configured": False,
                "active_source": None,
                "can_connect_account": False,
                "provider": None,
            }
        entry: dict[str, str | bool | None] = {
            "name": name,
            "display_name": service.display_name or name,
            "configured": status.configured,
            "active_source": status.active_source,
        }
        if not status.configured:
            # An unreadable stored connection must be reset before a new one can be stored, so it is not connectable.
            # Like the broker's 403 body, name the provider only when the user can connect it.
            oauth = status.oauth
            connectable = oauth is not None and oauth.can_connect and not oauth.reset_required
            entry["can_connect_account"] = connectable
            entry["provider"] = oauth.provider if oauth is not None and connectable else None
        return entry

    @staticmethod
    def _note(link: str | None) -> str:
        where = (
            f"at {link}"
            if link is not None
            else "on the page where egress credentials are managed (ask the operator for it, usually the dashboard Credentials tab)"
        )
        return (
            "Services with `configured: false` have neither an API key nor a connected account in this agent's scope, "
            "so brokered requests to them fail with `credential_not_configured`. "
            "`active_source` names what a configured service uses; an API key wins over a connected account. "
            f"When `can_connect_account` is true, the user can connect their `provider` account {where}; "
            "otherwise, or to use their own key instead, they can add an API key there. "
            "Never ask the user to paste a key into the chat."
        )

    @staticmethod
    def _payload(services: list[dict[str, str | bool | None]], link: str | None, note: str) -> str:
        return json.dumps(
            {"tool": "egress_credentials", "services": services, "manage_url": link, "note": note},
            sort_keys=True,
        )
