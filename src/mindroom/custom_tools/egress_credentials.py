"""Egress broker credential status tool for MindRoom agents.

Listing this tool opts an agent into Agent Keys: only its worker-routed shell and python calls get the broker env.
When a brokered request fails with ``credential_not_configured``, the agent
calls this tool to tell the user which services the egress broker can inject
credentials for, which of them have a key or a connected account Agent Keys
may use in this agent's scope, and where to add the missing ones. Each service says whether
the administrator defined it (``source: "config"``) or the user added it in
this scope (``source: "user"``). The tool reports names and flags only; it
never reads a secret, access token, or connect link into its output.
For a missing key the agent calls ``request_secret``, which posts a card in the
conversation; the requester's key goes from MindRoom Chat straight to the Agent
Keys store and never passes through the tool, Matrix, or the model.
"""

from __future__ import annotations

import functools
import json
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.env import egress_agent_scope, egress_tool_without_worker_commands, personal_page_served
from mindroom.egress_broker.key_access import egress_key_access, service_key_refusal
from mindroom.egress_broker.messages import CONNECTIONS_PAGE_NOT_SERVED
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import is_personal_target, service_status
from mindroom.egress_broker.service import manage_url
from mindroom.egress_broker.user_services import effective_config, service_source, services_for_agent
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.secret_request_cards import SecretRequestCard, ask_with_card, card_reason, chat_refusal
from mindroom.secret_requests import SECRET_REQUEST_TTL_SECONDS, KeyTarget, secret_request_hosts
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.egress_broker.key_access import ServiceKeyRefusal
    from mindroom.secret_requests import SecretRequestOutcome
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_NO_WORKER_NOTE = (
    "No worker identity is available for this agent, so there is no secret scope to check. "
    "Egress credentials require a worker-scoped agent; ask the operator to give this agent a worker scope."
)
_NO_CONFIG_NOTE = "The egress broker configuration is not available in this context, so no services can be listed."
_NO_WORKER_COMMANDS_NOTE = (
    "None of this agent's shell or python calls run in a worker, so Agent Keys never adds a key or account to them "
    "and the Agent Keys pages do not list this agent. Ask the operator to route `shell` or `python` to a worker "
    "with `worker_tools`."
)
_NO_SERVICES_NOTE = (
    "No egress services are configured. An operator defines them under `egress_broker.services` in config.yaml, "
    "and users can add services of their own on the egress page where it is available."
)
_NO_PERSON_NOTE = "Only a person can answer a card, and this turn comes from an agent or a bot, so no card was posted."
_OUTCOME_NOTES: dict[SecretRequestOutcome, str] = {
    "saved": (
        "The user saved the key. Your next request through Agent Keys uses it, so retry the request that failed. "
        "If `{service}` is one of the user's own services and another service's rule matches the same host, "
        "send the header `X-Agent-Keys-Service: {service}` with it."
    ),
    "declined": "The user declined, so do not ask again for this service in this turn.",
    "expired": (
        f"No key was saved: no answer came in {SECRET_REQUEST_TTL_SECONDS // 60} minutes, or the service or where this "
        "agent keeps its keys changed before the user answered. Ask again if you still need the key."
    ),
}
# Where the user adds a key when no card can be posted; the result's `link` is null where no page is served.
_ADD_ON_PAGE = (
    "The user can add the key on the Agent Keys page instead: the `link` in this result, or, when it is null, the "
    "page the operator names."
)


def _key_access_refusal(
    context: ToolRuntimeContext,
    requester_id: str,
    agent_name: str,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return why the requester may not set this agent's keys, or None when they may, as on the Agent Keys pages."""
    config = context.current_config
    memberships = context.agent_reply_memberships
    if memberships is None:
        return "This turn cannot check who may set this agent's keys, so no card was posted."
    if egress_agent_scope(config, agent_name, runtime_paths) is None:
        return (
            "This agent's commands do not reach Agent Keys, so no key can be stored for it: it needs "
            "`egress_credentials` and a `shell` or `python` tool that runs in a worker."
        )
    access = egress_key_access(requester_id, agent_name, config, runtime_paths, memberships)
    if access is None:
        return "The requester may not use this agent, so they cannot set its keys."
    _is_shared, can_manage = access
    if not can_manage:
        return (
            "Only this agent's credential managers or an administrator can set its keys; "
            "ask one of them to add the key on the Agent Keys page."
        )
    return None


def _requestable_service(services: dict[str, EgressService], name: str) -> EgressService | str:
    """Return the service `name` a key can be asked for, or why no key can be asked for it."""
    service = services.get(name)
    if service is None:
        return (
            f"Unknown service '{name}'. Call `list_egress_credentials` for the services this agent can use; "
            "a new service needs host rules, which the user adds on the Agent Keys page."
        )
    refusal = service_key_refusal(service)
    if refusal is None:
        return service
    display_name = service.display_name or name
    refusals: dict[ServiceKeyRefusal, str] = {
        "admin_key": (
            f"Only an administrator sets the key of service '{name}', for every agent that uses it; ask an administrator."
        ),
        "restrict_only": f"Service '{name}' only limits which paths requests may use and never takes a key.",
        "separate_account": f"{display_name} takes no key; connect an account instead.",
        "github_app": (
            f"{display_name} takes no key: it uses the administrator's GitHub App; ask an administrator to change it."
        ),
    }
    return refusals[refusal]


class EgressCredentialsTools(Toolkit):
    """Tool that lists the egress broker services, whether this agent has a key or account for each, and asks for keys."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._worker_target = worker_target
        super().__init__(name="egress_credentials", tools=[self.list_egress_credentials, self.request_secret])

    def list_egress_credentials(self) -> str:
        """List the services this agent can use through the egress broker and how to give it credentials.

        Call this when a request fails with `credential_not_configured`, or before telling the user
        which credential they need. Returns each configured service with whether this agent has a
        credential for it (`configured`) and which one the broker uses (`active_source`: `key`, `oauth`, or
        `github_app`). A `github_app` service lists the repositories its GitHub App covers in
        `github_app_repositories`, each `read` or `read_write`; it takes no key and covers no other repository.
        `source` says who defined the service: `config` for the administrator, `user` for the user's own.
        `oauth_reuse` says whether Agent Keys may use the service's account (null without one), and `oauth_access`
        whether only to read (`read`) or also to write (`read_write`; null while not allowed). A service with
        nothing configured also says whether the user can connect an account instead of adding a key
        (`can_connect_account`), with the account's
        `provider` when it can or when the account still needs allowing. `separate_account: true` marks a service
        of the user's own that takes no API key, only an account of its own, which a request uses by naming it in an
        `X-Agent-Keys-Service` header.
        The result carries the link
        where the user connects and allows accounts and adds or replaces keys. Never ask the user to paste a key
        into the chat: call `request_secret` for a missing key; the link is the fallback.
        """
        settable = self._settable()
        link = manage_url(self._runtime_paths) if settable else None
        if self._worker_target is None:
            return self._payload([], link, _NO_WORKER_NOTE)
        context = get_tool_runtime_context()
        if context is None:
            return self._payload([], link, _NO_CONFIG_NOTE)
        config = context.current_config
        agent_name = self._worker_target.routing_agent_name
        if agent_name is not None and egress_tool_without_worker_commands(config, agent_name, self._runtime_paths):
            return self._payload([], None, _NO_WORKER_COMMANDS_NOTE)
        manager = get_runtime_credentials_manager(self._runtime_paths)
        # The config's services plus the ones the user defined in this agent's scope, as the broker matches them.
        # A restrict-only service only limits paths and never takes a credential, and the broker refuses this agent a
        # service whose `agents` list leaves it out, so neither is listed.
        scope_services = effective_config(config.egress_broker, manager, self._worker_target).services
        services = {
            name: service
            for name, service in services_for_agent(scope_services, agent_name).items()
            if not service.restrict_only
        }
        if not services:
            return self._payload([], link, _NO_SERVICES_NOTE)
        entries = [self._entry(manager, config, name, service) for name, service in services.items()]
        note = self._note(link, settable=settable, shared=not is_personal_target(self._worker_target))
        return self._payload(entries, link, note)

    async def request_secret(self, service: str, reason: str) -> str:
        """Ask the user for a missing API key with a card in this conversation, and wait for their answer.

        Call this when a brokered request fails with `credential_not_configured` for a service that takes an API key
        (`list_egress_credentials` names them). The card shows the user your `reason`, the service, and the hosts the
        key is sent to; they type the key into a masked field in MindRoom Chat, and it is stored as this agent's
        Agent Keys key for the service. The key never reaches you, the chat, or your commands.
        Waits up to 15 minutes and returns `status`:
        `saved` when the key is stored: your next request through Agent Keys uses it, so retry the failed request;
        `declined` when the user refused: do not ask again for this service in this turn;
        `expired` when no answer came in 15 minutes.
        An `error` posts no card and says why, with the page where the user can add the key as `link` where one exists.

        Args:
            service: The service's `name` from `list_egress_credentials`.
            reason: What you need the key for, in a sentence or two the user reads on the card.

        """
        context = get_tool_runtime_context()
        target = self._worker_target
        if context is None:
            return self._request_error("No conversation is available in this context, so no card can be posted.")
        if target is None or target.routing_agent_name is None:
            return self._request_error(_NO_WORKER_NOTE)
        agent_name = target.routing_agent_name
        card = await self._card(context, target, agent_name, service, reason)
        if isinstance(card, str):
            return self._request_error(card)
        asked = await ask_with_card(context, card, agent_name=agent_name)
        if isinstance(asked, str):
            return self._request_error(asked)
        outcome = asked.outcome.result()
        note = _OUTCOME_NOTES[outcome].format(service=service)
        return json.dumps({"status": outcome, "service": service, "note": note}, sort_keys=True)

    async def _card(
        self,
        context: ToolRuntimeContext,
        target: ResolvedWorkerTarget,
        agent_name: str,
        service: str,
        reason: str,
    ) -> SecretRequestCard | str:
        """Return the card asking the turn's requester for `service`'s key, or why no card may be posted.

        The requester is the person who answers, resolved as for approval cards: the canonical human, with a bridge
        alias resolved, whose scope the key goes to. The service resolves as `list_egress_credentials` lists it, and the
        requester must be one who may set this agent's keys, as on the Agent Keys pages; the card needs no personal
        page, so it works where none is served. Chat answers through this MindRoom's API with a client session, so a
        card needs what that session needs.
        """
        requester_id = resolve_tool_approval_approver(context.current_config, self._runtime_paths, context.requester_id)
        if requester_id is None:
            return _NO_PERSON_NOTE
        shown_reason = card_reason(reason)
        if shown_reason is None:
            return "Give a reason: the card tells the user what you need the key for."
        refusal = _key_access_refusal(context, requester_id, agent_name, self._runtime_paths)
        if refusal is not None:
            return refusal
        config = context.current_config
        if requester_id != context.requester_id:
            identity = replace(context.execution_identity, requester_id=requester_id)
            target = resolve_agent_execution(
                agent_name,
                config,
                self._runtime_paths,
                execution_identity=identity,
            ).worker_target
        manager = get_runtime_credentials_manager(self._runtime_paths)
        services = services_for_agent(effective_config(config.egress_broker, manager, target).services, agent_name)
        egress_service = _requestable_service(services, service)
        if isinstance(egress_service, str):
            return egress_service
        if (refusal := await chat_refusal(self._runtime_paths, noun="key")) is not None:
            return f"{refusal} {_ADD_ON_PAGE}"
        agent = config.agents.get(agent_name)
        return SecretRequestCard(
            requester_id=requester_id,
            agent_display_name=(agent.display_name if agent is not None else "") or agent_name,
            reason=shown_reason,
            target=KeyTarget(service=service, worker_target=target, hosts=secret_request_hosts(egress_service)),
            display_name=egress_service.display_name or service,
        )

    def _settable(self) -> bool:
        """Return whether a page sets this agent's keys: only the personal page sets a personal scope's."""
        return not is_personal_target(self._worker_target) or personal_page_served(self._runtime_paths)

    def _request_error(self, message: str) -> str:
        link = manage_url(self._runtime_paths) if self._settable() else None
        return json.dumps({"status": "error", "message": message, "link": link}, sort_keys=True)

    def _entry(
        self,
        manager: CredentialsManager,
        config: Config,
        name: str,
        service: EgressService,
    ) -> dict[str, str | bool | dict[str, str] | None]:
        """Describe one service's credential sources in this agent's scope, as the broker would use them.

        A service whose status cannot be read is reported as not configured and not connectable, so one failing
        provider or unreadable secret does not fail the listing of the others. Only the error type is logged.
        """
        entry: dict[str, str | bool | dict[str, str] | None] = {
            "name": name,
            "display_name": service.display_name or name,
            "source": service_source(config.egress_broker, name),
        }
        if service.oauth_separate_account:
            entry["separate_account"] = True
        if service.admin_key:
            entry["admin_key"] = True
        if service.github_app is not None:
            entry["github_app_repositories"] = dict(service.github_app.repositories)
        try:
            status = service_status(
                manager,
                self._worker_target,
                service,
                name,
                oauth_status=functools.partial(
                    oauth_status,
                    service=name,
                    separate_account=service.oauth_separate_account,
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
                "oauth_access": None,
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
            "oauth_access": oauth.reuse_access if oauth is not None and reuse else None,
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
                "`oauth_reuse: false` means Agent Keys may not use the `provider` account for that service: allowing "
                "it there lets this agent's shell and python commands act as that account with its full access, "
                f"without the approval prompts its tools ask for. {who}"
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
            "`active_source` names what a configured service uses; an API key wins over a connected account, but "
            "a `separate_account: true` service takes no API key, only its own account. "
            "`source` is `config` for a service the administrator defined and `user` for one the user added themselves. "
            "`admin_key: true` marks a service whose one key only an administrator sets, for every agent that uses it, "
            "so when it is not configured ask an administrator, not the user. "
            "A service with `github_app_repositories` uses a GitHub App the administrator set up, which covers only "
            "those repositories at the listed access and takes no key; only an administrator can change them or, "
            "when it is not configured, store the App's credential. "
            f"{fix}"
            '`oauth_access: "read"` means Agent Keys may use the account only to read, so writes fail with `read_only` '
            "until whoever allows the account allows read and write there. "
            "A request uses one of the user's own services (`source: user`) by sending the header "
            "`X-Agent-Keys-Service: <name>`; without it the most specific rule decides, so a `separate_account: true` "
            "service, which logs in with an account of its own instead of the one this agent's tools use, may need it. "
            "Never ask the user to paste a key into the chat: call `request_secret` to ask for a missing API key with "
            "a card in this conversation; the link is the fallback."
        )

    @staticmethod
    def _payload(services: list[dict[str, str | bool | dict[str, str] | None]], link: str | None, note: str) -> str:
        return json.dumps(
            {"tool": "egress_credentials", "services": services, "manage_url": link, "note": note},
            sort_keys=True,
        )
