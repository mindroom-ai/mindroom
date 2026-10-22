"""Egress broker credential status tool for MindRoom agents.

Listing this tool is what opts an agent into Agent Keys: only its shell and python calls get the broker env.
When a brokered request fails with ``credential_not_configured``, the agent
calls this tool to tell the user which services the egress broker can inject
credentials for, which of them have a key or a connected account Agent Keys
may use in this agent's scope, and where to add the missing ones. Each service says whether
the administrator defined it (``source: "config"``) or the user added it in
this scope (``source: "user"``). The tool reports names and flags only; it
never reads a secret, access token, or connect link into its output.
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.env import personal_page_served
from mindroom.egress_broker.messages import CONNECTIONS_PAGE_NOT_SERVED
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import is_personal_target, service_status
from mindroom.egress_broker.service import manage_url
from mindroom.egress_broker.user_services import effective_config, service_source
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_NO_WORKER_NOTE = (
    "No worker identity is available for this agent, so there is no secret scope to check. "
    "Egress credentials require a worker-scoped agent; ask the operator to give this agent a worker scope."
)
_NO_CONFIG_NOTE = "The egress broker configuration is not available in this context, so no services can be listed."
_NO_SERVICES_NOTE = (
    "No egress services are configured. An operator defines them under `egress_broker.services` in config.yaml, "
    "and users can add services of their own on the egress page where it is available."
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
        `source` says who defined the service: `config` for the administrator, `user` for the user's own.
        `oauth_reuse` says whether Agent Keys may use the service's account (null without one), and
        `account_connected` whether one is connected. A service with nothing configured also says whether the
        user can connect an account instead of adding a key (`can_connect_account`), with the account's
        `provider` when it can or when the account still needs allowing. The result carries the link
        where the user connects and allows accounts and adds or replaces keys. Never ask the user to paste a key
        into the chat; send them to the link instead.
        """
        # Only the personal page sets a personal scope's keys and accounts, so without it there is no page to name.
        settable = not is_personal_target(self._worker_target) or personal_page_served(self._runtime_paths)
        link = manage_url(self._runtime_paths) if settable else None
        if self._worker_target is None:
            return self._payload([], link, _NO_WORKER_NOTE)
        context = get_tool_runtime_context()
        if context is None:
            return self._payload([], link, _NO_CONFIG_NOTE)
        config = context.current_config
        manager = get_runtime_credentials_manager(self._runtime_paths)
        # The config's services plus the ones the user defined in this agent's scope, as the broker matches them.
        # A restrict-only service only limits paths and never takes a credential, so it is not listed.
        services = {
            name: service
            for name, service in effective_config(config.egress_broker, manager, self._worker_target).services.items()
            if not service.restrict_only
        }
        if not services:
            return self._payload([], link, _NO_SERVICES_NOTE)
        entries = [self._entry(manager, config, name, service) for name, service in services.items()]
        note = self._note(link, settable=settable, shared=not is_personal_target(self._worker_target))
        return self._payload(entries, link, note)

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
        entry: dict[str, str | bool | None] = {
            "name": name,
            "display_name": service.display_name or name,
            "source": service_source(config.egress_broker, name),
        }
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
                **entry,
                "configured": False,
                "active_source": None,
                "oauth_reuse": None,
                "account_connected": False,
                "can_connect_account": False,
                "provider": None,
            }
        oauth = status.oauth
        # An account the broker never uses in this sandbox has nothing to allow, like a service without one.
        reuse = None if oauth is None or oauth.unavailable_reason is not None else oauth.reuse_enabled
        entry |= {
            "configured": status.configured,
            "active_source": status.active_source,
            "oauth_reuse": reuse,
            "account_connected": oauth is not None and oauth.connected,
        }
        if not status.configured:
            # An unreadable stored connection must be reset before a new one can be stored, so it is not connectable.
            # Like the broker's 403 body, name the provider only when the user can connect or allow it.
            connectable = oauth is not None and oauth.reuse_enabled and oauth.can_connect and not oauth.reset_required
            entry["can_connect_account"] = connectable
            entry["provider"] = oauth.provider if oauth is not None and (connectable or reuse is False) else None
        return entry

    @staticmethod
    def _note(link: str | None, *, settable: bool, shared: bool) -> str:
        """Explain the entries and where the user fixes a missing credential; `settable` is false without such a page.

        That is a personal scope where the personal page is not served: the note then says so, as the broker's
        refusal does, and points at the connect link in the refusal for an account. `shared` (a shared or unscoped
        agent) says who may allow an account there.
        """
        if settable:
            where = (
                f"at {link}"
                if link is not None
                else "on the page where egress credentials are managed (ask the operator for it, usually the dashboard Credentials tab)"
            )
            who = (
                "On this shared agent only its credential managers or an administrator can allow its accounts, "
                "except a per-user account such as GitHub, which each user allows for themselves. "
                if shared
                else "Only the user can allow it. "
            )
            fix = (
                f"When `can_connect_account` is true, the user can connect their `provider` account {where}; "
                "otherwise, or to use their own key instead, they can add an API key there. "
                "`oauth_reuse: false` means Agent Keys may not use the `provider` account for that service "
                "(`account_connected` says whether one is connected): allowing it there lets this agent's shell and "
                "python commands act as that account with its full access, without the approval prompts its tools "
                f"ask for. {who}"
            )
        else:
            fix = (
                f"Each user sets this agent's keys and accounts {CONNECTIONS_PAGE_NOT_SERVED} "
                "When `can_connect_account` is true, the user can still connect their `provider` account at the "
                "`connect_url` in the broker's refusal. `oauth_reuse: false` means Agent Keys may not use the "
                "`provider` account, and only that page can allow it. "
            )
        return (
            "Services with `configured: false` have neither an API key nor a connected account Agent Keys may use in "
            "this agent's scope, so brokered requests to them fail with `credential_not_configured`. "
            "`active_source` names what a configured service uses; an API key wins over a connected account. "
            "`source` is `config` for a service the administrator defined and `user` for one the user added themselves. "
            f"{fix}"
            "Never ask the user to paste a key into the chat."
        )

    @staticmethod
    def _payload(services: list[dict[str, str | bool | None]], link: str | None, note: str) -> str:
        return json.dumps(
            {"tool": "egress_credentials", "services": services, "manage_url": link, "note": note},
            sort_keys=True,
        )
