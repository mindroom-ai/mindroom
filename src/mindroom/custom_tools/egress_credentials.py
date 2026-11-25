"""Egress broker credential status tool for MindRoom agents.

Listing this tool opts an agent into Agent Keys: its shell calls and its worker-routed python calls get the broker env.
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

from pydantic import ValidationError

from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.config.egress_broker import EgressService
from mindroom.config.main import validation_issue_message
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.env import egress_agent_scope, egress_tool_with_only_primary_python, personal_page_served
from mindroom.egress_broker.key_access import egress_key_access, service_key_refusal
from mindroom.egress_broker.messages import CONNECTIONS_PAGE_NOT_SERVED
from mindroom.egress_broker.oauth_source import oauth_status
from mindroom.egress_broker.secrets import egress_key_path, is_personal_target, service_status
from mindroom.egress_broker.service import manage_url
from mindroom.egress_broker.user_services import (
    UserServiceConflictError,
    effective_config,
    save_user_service,
    service_source,
    services_for_agent,
    user_service_hosts_error,
)
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.secret_request_cards import (
    LINK_REFUSAL,
    SecretRequestCard,
    ask_with_card,
    card_line,
    card_link,
    card_reason,
    chat_refusal,
)
from mindroom.secret_requests import SECRET_REQUEST_TTL_SECONDS, KeyTarget, secret_request_hosts
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
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
_ONLY_PRIMARY_PYTHON_NOTE = (
    "This agent's only command tool is `python` running inside MindRoom's own process, which Agent Keys cannot reach, "
    "so Agent Keys never adds a key or account to its calls and the Agent Keys pages do not list this agent. Ask the "
    "operator to give it `shell`, or to route `python` to a worker with `worker_tools`."
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
# What a service proposed on a card may set: a key-only service of the requester's own, as the Agent Keys page adds it.
_PROPOSABLE_FIELDS = ("display_name", "description", "preset", "rules", "placeholder_env", "restrict_to_rules")
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
            "`egress_credentials` and a `shell` tool, or a `python` tool that runs in a worker."
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
            f"Unknown service '{name}'. Call `list_egress_credentials` for the services this agent can use, "
            "or propose a new one with `new_service`."
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


def _proposed_service(
    config: Config,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    name: str,
    body: object,
) -> EgressService | str:
    """Return the service the agent proposes as `name`, or why the Agent Keys page would not add it to this scope.

    It is key-only: it sets no account login, a preset's included, and none of the fields only config.yaml sets.
    """
    if not isinstance(body, dict):
        return "Give `new_service` as a JSON object with the fields of the service."
    refused = [field for field in body if field not in _PROPOSABLE_FIELDS]
    if refused:
        return (
            f"`new_service` cannot set {', '.join(map(str, refused))}: a service proposed on a card takes only an API "
            f"key, so it sets only {', '.join(_PROPOSABLE_FIELDS)}."
        )
    broker = config.egress_broker
    try:
        service = EgressService.model_validate({**body, "oauth_provider": None})
        # Checked as the Agent Keys page saves a new service, writing nothing.
        save_user_service(
            manager,
            target,
            name,
            service,
            config_services=broker.services,
            oauth_providers=(),
            only_new=True,
            write=False,
        )
    except ValidationError as error:
        issues = [
            f"{'.'.join(map(str, issue['loc']))}: {validation_issue_message(issue)}"
            if issue["loc"]
            else validation_issue_message(issue)
            for issue in error.errors(include_url=False, include_context=False, include_input=False)
        ]
        return f"New service '{name}' is invalid: {'; '.join(issues)}."
    except (ValueError, UserServiceConflictError) as error:
        return f"New service '{name}' cannot be added: {error}."
    if error := user_service_hosts_error(broker, service):
        return f"New service '{name}' cannot be added: {error}."
    return service


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
        """List Agent Keys egress services available to this agent, credential status, and `manage_url`.

        Call after `credential_not_configured` or before asking for credentials. Each service reports `configured`,
        `active_source` (`key`|`oauth`|`github_app`), `source` (`config`|`user`), `oauth_reuse` (whether Agent Keys may
        reuse the service's connected account), `oauth_access` (`read`|`read_write`|null),
        `can_connect_account`/`provider` (when unconfigured), `github_app_repositories` (the only repositories a
        `github_app` service covers; takes no key), and `separate_account` (user account-only; selected via
        `X-Agent-Keys-Service` header). Never ask the user to paste a key in chat—call `request_secret` for missing API
        keys (`manage_url` is the fallback).
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
        if agent_name is not None and egress_tool_with_only_primary_python(config, agent_name, self._runtime_paths):
            return self._payload([], None, _ONLY_PRIMARY_PYTHON_NOTE)
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

    async def request_secret(
        self,
        service: str,
        reason: str,
        url: str | None = None,
        placeholder: str | None = None,
        new_service: dict[str, object] | None = None,
    ) -> str:
        """Post a masked in-chat card requesting a missing API key for an Agent Keys service (never ask the user to paste keys in chat).

        Use after `credential_not_configured` (key never reaches you, chat, or commands; replaces any existing key—don't
        ask while it works). For an unlisted service, propose it via `new_service` instead of editing config.yaml.
        Waits up to 15m and returns `status`: `saved` (retry/test with one request; if refused, explain why and re-ask),
        `declined` (do not re-ask this turn), `expired`, or `error` (no card posted; includes fallback `link` if any).

        Args:
            service: Service `name` from `list_egress_credentials`, or new name for `new_service` (lowercase letters, digits, `-`, `_`).
            reason: 1-2 sentences shown on the card explaining why the key is needed.
            url: `http(s)` URL where the user can get/create the key (shown as a button).
            placeholder: Format hint for empty field (e.g. `sk-…`); never a real secret.
            new_service: Propose a new key-only user service (no account login); allowed fields: `display_name`, `description`, `preset`, `rules` (`host`, optional `path_prefix`/`port`, `auth`: `type` `bearer` | `basic`+`username` | `header`/`query`+`name`, optional `template` with `{secret}`), `placeholder_env`, `restrict_to_rules`.

        """
        context = get_tool_runtime_context()
        target = self._worker_target
        if context is None:
            return self._request_error("No conversation is available in this context, so no card can be posted.")
        if target is None or target.routing_agent_name is None:
            return self._request_error(_NO_WORKER_NOTE)
        agent_name = target.routing_agent_name
        link = card_link(url)
        if link is None:
            return self._request_error(LINK_REFUSAL)
        card = await self._card(
            context,
            target,
            agent_name,
            service,
            reason,
            url=link,
            placeholder=card_line(placeholder),
            new_service=new_service,
        )
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
        *,
        url: str,
        placeholder: str,
        new_service: dict[str, object] | None,
    ) -> SecretRequestCard | str:
        """Return the card asking the turn's requester for `service`'s key, or why no card may be posted.

        The requester is the person who answers, resolved as for approval cards: the canonical human, with a bridge
        alias resolved, whose scope the key goes to. The service resolves as `list_egress_credentials` lists it, and the
        requester must be one who may set this agent's keys, as on the Agent Keys pages; the card needs no personal
        page, so it works where none is served. Chat answers through this MindRoom's API with a client session, so a
        card needs what that session needs. `url` and `placeholder` are what the card shows of the agent's, already
        checked and cut, each empty when it gave none. `new_service` is the service the agent proposes as `service`,
        checked here as the Agent Keys page would check it; saving the answer adds it.
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
        if new_service is None:
            services = services_for_agent(effective_config(config.egress_broker, manager, target).services, agent_name)
            egress_service = _requestable_service(services, service)
        else:
            egress_service = _proposed_service(config, manager, target, service, new_service)
        if isinstance(egress_service, str):
            return egress_service
        if (refusal := await chat_refusal(self._runtime_paths, noun="key")) is not None:
            return f"{refusal} {_ADD_ON_PAGE}"
        agent = config.agents.get(agent_name)
        # The file a saved answer is written to: one already there is the key the answer replaces.
        key_path = egress_key_path(manager, target, service)
        return SecretRequestCard(
            requester_id=requester_id,
            agent_display_name=(agent.display_name if agent is not None else "") or agent_name,
            reason=shown_reason,
            target=KeyTarget(
                service=service,
                worker_target=target,
                hosts=secret_request_hosts(egress_service),
                new_service=None if new_service is None else egress_service,
            ),
            display_name=egress_service.display_name or service,
            # A new service starts without a key: one left under its name is deleted, not replaced.
            replaces=new_service is None and key_path is not None and key_path.exists(),
            url=url,
            placeholder=placeholder,
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
