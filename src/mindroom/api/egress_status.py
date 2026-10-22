"""Egress service status, presets, request-log responses, and the account connection gate shared by both egress APIs."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, oauth
from mindroom.api.connection_agents import CONNECTIONS_HEADERS
from mindroom.config.egress_broker import RESERVED_PLACEHOLDER_NAMES, RESERVED_PLACEHOLDER_PREFIXES, EgressService
from mindroom.egress_broker.env import egress_broker_enabled
from mindroom.egress_broker.oauth_reuse import oauth_reuse_enabled
from mindroom.egress_broker.oauth_source import (
    broker_credential_context,
    oauth_connection_for_display,
    shared_worker_oauth,
    shared_worker_unavailable_status,
)
from mindroom.egress_broker.presets import EGRESS_PRESETS
from mindroom.egress_broker.secrets import EgressServiceStatus, OAuthStatus, service_status
from mindroom.egress_broker.user_services import (
    MAX_RULES_PER_SERVICE,
    MAX_SERVICE_BYTES,
    PLACEHOLDER_NAME,
    PLACEHOLDER_NAME_WORDS,
    PLACEHOLDER_REFUSED_PREFIX,
    PLACEHOLDER_REFUSED_WORDS,
    PLACEHOLDER_VALUE,
    PLACEHOLDER_VALUE_MAX_LENGTH,
    effective_config,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.egress_broker import EgressAuth
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.egress_broker.audit import AuditRecord
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

RESTRICT_ONLY_KEY_REFUSAL = "This service only limits paths and never sends a key, so it cannot store one"

# Service status loads that may run at once on one event loop, across every egress listing on it. A load can hold an
# OAuth store's SQLite connection open between its thread steps, so this bounds the open connections and file
# descriptors however large the config is and however many listings overlap. It is about the width of asyncio's
# default thread pool (at most 32 workers), which runs each load's file reads.
_STATUS_LOAD_LIMIT = 32
_status_load_limits: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _status_load_limit() -> asyncio.Semaphore:
    """Return the running loop's shared limit on concurrent status loads."""
    loop = asyncio.get_running_loop()
    limit = _status_load_limits.get(loop)
    if limit is None:
        limit = _status_load_limits[loop] = asyncio.Semaphore(_STATUS_LOAD_LIMIT)
    return limit


async def load_concurrently[Result](
    loads: Iterable[Callable[[], Awaitable[Result]]],
    *,
    limited: bool = False,
) -> list[Result]:
    """Run `loads` at once and return their results in order; the first failure cancels and drains the rest.

    A listing awaits many thread and OAuth store hops per service, and in series each one resumes behind the event
    loop's other work. That failure is raised itself, not as an exception group, so callers handle what a single load
    raises. Each `limited` load runs under the loop's shared limit (`_STATUS_LOAD_LIMIT`); use it for complete service
    status loads only, never for a load that waits on limited loads of its own.
    """
    limit = _status_load_limit() if limited else None

    async def run(load: Callable[[], Awaitable[Result]]) -> Result:
        if limit is None:
            return await load()
        async with limit:
            return await load()

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(run(load)) for load in loads]
    except BaseExceptionGroup as errors:
        first = errors.exceptions[0]
    else:
        return [task.result() for task in tasks]
    # Raised outside the handler, so its cause and context stay the load's own.
    raise first


async def require_egress_broker_enabled(request: Request) -> None:
    """Answer 404 on every route of both egress APIs while the broker is off.

    Both routers depend on this, so with the broker off no route reads a store. The admin router is mounted behind
    dashboard authentication, which runs before it, so an unauthenticated dashboard call still gets 401.
    """
    if not egress_broker_enabled(config_lifecycle.bind_current_request_snapshot(request).runtime_paths):
        raise HTTPException(404, "Agent Keys are not enabled", headers=CONNECTIONS_HEADERS)


def effective_services(
    config: Config,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
) -> dict[str, EgressService]:
    """Return the services one scope uses, by name: the config's first, then the scope's own.

    Both egress APIs look services up here, so what they list and accept is what the broker matches. A cache miss
    reads the credential store, so async callers run this in a thread.
    """
    return effective_config(config.egress_broker, manager, target).services


class EgressRuleLocation(BaseModel):
    """Where one rule applies: its host, port, and path prefix."""

    host: str
    port: int | None
    path_prefix: str


type _EgressAuthKind = Literal["bearer", "basic", "header", "query", "template"]


class EgressRuleSummary(EgressRuleLocation):
    """Where one rule applies and how it sends the key, for a client that shows what a service covers.

    `auth_type` is `bearer`, `basic`, `header`, `query`, or `template` (a header whose value is a template other than
    the bare key), and `auth_name` the header or query parameter name (null for `bearer` and `basic`). Both are null
    for a `restrict_only` service, which sends nothing. Never the key, a template's text, or a basic username.
    """

    auth_type: _EgressAuthKind | None
    auth_name: str | None


def _auth_summary(service: EgressService, auth: EgressAuth | None) -> tuple[_EgressAuthKind | None, str | None]:
    if service.restrict_only or auth is None:
        return None, None
    if auth.type in {"bearer", "basic"}:
        return auth.type, None
    if auth.type == "header" and auth.template != "{secret}":
        return "template", auth.name
    return auth.type, auth.name


def rule_summaries(service: EgressService) -> list[EgressRuleSummary]:
    """Describe where a service's rules apply and how each sends the key, in rule order."""
    summaries = []
    for rule in service.rules:
        auth_type, auth_name = _auth_summary(service, rule.auth)
        summaries.append(
            EgressRuleSummary(
                host=rule.host,
                port=rule.port,
                path_prefix=rule.path_prefix,
                auth_type=auth_type,
                auth_name=auth_name,
            ),
        )
    return summaries


class EgressPresetResponse(BaseModel):
    """One built-in service preset as the editor shows it."""

    id: str
    display_name: str
    description: str
    oauth_provider: str | None
    rules: list[EgressRuleLocation]
    placeholder_env: dict[str, str]


class EgressOAuthProviderOption(BaseModel):
    """One OAuth provider a service's account login can name."""

    id: str
    display_name: str


class PlaceholderValidation(BaseModel):
    """The placeholder rules of a service a user defines, as the server checks them on save.

    A name must match `name_pattern` completely, needs one of the `credential_words` as an underscore-separated word
    and none of the `refused_words`, must not start with `refused_prefix`, and may be none of the `reserved_names` or
    start with a `reserved_prefixes` entry, which the config model reserves for every service. A value must match
    `value_pattern` completely. Both word lists keep the order the server's error messages list them in; the other
    lists are sorted.
    """

    name_pattern: str
    credential_words: list[str]
    refused_words: list[str]
    refused_prefix: str
    reserved_names: list[str]
    reserved_prefixes: list[str]
    value_pattern: str
    value_max_length: int


class EgressValidation(BaseModel):
    """What a service a user defines may hold, so the editor can refuse a service the server would refuse.

    `max_rules_per_service` counts the rules of one service and `max_service_bytes` one service's authored JSON.
    """

    max_rules_per_service: int
    max_service_bytes: int
    placeholder: PlaceholderValidation


class PresetsResponse(BaseModel):
    """The built-in service presets, the OAuth providers an account login can name, and the user service validation."""

    presets: list[EgressPresetResponse]
    oauth_providers: list[EgressOAuthProviderOption]
    validation: EgressValidation


def _egress_validation() -> EgressValidation:
    """Describe how a user's service is validated, from the constants the broker and the config model enforce."""
    return EgressValidation(
        max_rules_per_service=MAX_RULES_PER_SERVICE,
        max_service_bytes=MAX_SERVICE_BYTES,
        placeholder=PlaceholderValidation(
            name_pattern=PLACEHOLDER_NAME.pattern,
            credential_words=list(PLACEHOLDER_NAME_WORDS),
            refused_words=list(PLACEHOLDER_REFUSED_WORDS),
            refused_prefix=PLACEHOLDER_REFUSED_PREFIX,
            reserved_names=sorted(RESERVED_PLACEHOLDER_NAMES),
            reserved_prefixes=sorted(RESERVED_PLACEHOLDER_PREFIXES),
            value_pattern=PLACEHOLDER_VALUE.pattern,
            value_max_length=PLACEHOLDER_VALUE_MAX_LENGTH,
        ),
    )


def presets_response(oauth_providers: Mapping[str, OAuthProvider]) -> PresetsResponse:
    """List the presets as the config model expands them, so the editor shows what a `preset:` entry resolves to.

    `oauth_providers` is the registry (`oauth.registry`), the same one a saved service's `oauth_provider` is checked
    against, listed in registry order. `validation` is the same for every caller.
    """
    presets = []
    for preset_id in EGRESS_PRESETS:
        service = EgressService.model_validate({"preset": preset_id})
        presets.append(
            EgressPresetResponse(
                id=preset_id,
                display_name=service.display_name or preset_id,
                description=service.description,
                oauth_provider=service.oauth_provider,
                rules=[
                    EgressRuleLocation(host=rule.host, port=rule.port, path_prefix=rule.path_prefix)
                    for rule in service.rules
                ],
                placeholder_env=service.placeholder_env,
            ),
        )
    providers = [
        EgressOAuthProviderOption(id=provider_id, display_name=provider.display_name)
        for provider_id, provider in oauth_providers.items()
    ]
    return PresetsResponse(presets=presets, oauth_providers=providers, validation=_egress_validation())


class AuditRecordResponse(BaseModel):
    """One audit log record for API responses."""

    at: str
    kind: str
    scope: str
    agent_name: str | None
    requester_id: str | None
    method: str
    host: str
    path: str
    service: str | None
    status: int
    bytes_up: int
    bytes_down: int
    duration_ms: int
    code: str | None


class AuditLogsResponse(BaseModel):
    """Audit log query response."""

    records: list[AuditRecordResponse]


def audit_logs_response(records: Iterable[AuditRecord]) -> AuditLogsResponse:
    """Describe audit records, already in the order the log returned them, for an API response."""
    return AuditLogsResponse(
        records=[AuditRecordResponse.model_validate({**asdict(rec), "at": rec.at.isoformat()}) for rec in records],
    )


class PutSecretRequest(BaseModel):
    """Request body for setting a secret (write-only)."""

    model_config = ConfigDict(extra="forbid")

    secret: str


class OAuthReuseRequest(BaseModel):
    """Body that lets Agent Keys use a service's connected account.

    The account's tools ask for approval before they write, but the agent's shell and python then act as the account
    with its full access and no prompts, so the caller must confirm that (`acknowledge_approval_bypass`).
    """

    model_config = ConfigDict(extra="forbid")

    acknowledge_approval_bypass: Literal[True]


class EgressSourceStatus(BaseModel):
    """Which secret sources a service has in one scope.

    `configured` is true when either source is available. `active_source` says which source the broker uses: an
    explicit key wins over an OAuth connection, which counts only once Agent Keys may use it (`oauth.reuse_enabled`).
    """

    configured: bool
    active_source: Literal["key", "oauth"] | None
    key_configured: bool
    key_updated_at: str | None
    oauth: OAuthStatus | None


async def egress_oauth_status(
    request: Request,
    name: str,
    service: EgressService,
    worker_target: ResolvedWorkerTarget | None,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    can_manage: bool,
    connection_status: Callable[[OAuthProvider], Awaitable[oauth.OAuthStatusResponse]],
    reuse_target: Callable[[OAuthProvider], ResolvedWorkerTarget | None],
) -> OAuthStatus | None:
    """Return a service's OAuth status as the broker sees it, or None without a provider the registry knows.

    `connection_status` loads the provider's state without a token refresh, so a stalled provider never stalls a page:
    the personal page passes the helper behind the portal's status route, the admin panel the dashboard's.
    `reuse_target` returns the target whose connection that status shows, where `reuse_enabled` is read through
    `broker_credential_context` as the broker reads it. A provider the broker refuses in a shared sandbox
    (`shared_worker_oauth`) is reported unavailable before any state is read.
    Without a service account the status is what the Connections portal shows the same viewer; requester-scoped
    connections (GitHub, Atlassian) belong to the requester, so every user of the agent manages their own. A service
    account is not a token the broker can inject, so with one `connected` and `account_label` only reflect a stored
    personal connection, which the broker uses once allowed, and nothing is connectable. Any failure to read one provider's state, the worker
    backend check included, shows that service as not connectable instead of failing the page.
    """
    provider = service_oauth_provider(config_lifecycle.bind_current_request_snapshot(request), service)
    if provider is None:
        return None
    try:
        access = shared_worker_oauth(
            provider,
            worker_target,
            opted_in=service.oauth_on_shared_workers,
            runtime_paths=runtime_paths,
        )
        if access == "refused":
            return shared_worker_unavailable_status(provider, runtime_paths)
        result = await connection_status(provider)
        context = broker_credential_context(provider, config, runtime_paths, manager, reuse_target(provider))
        reuse_enabled = await asyncio.to_thread(oauth_reuse_enabled, context, name, provider.id)
        if result.has_service_account_config:
            # Shown whether or not Agent Keys may use it, so the user can allow or disconnect it.
            connected, account_label = await asyncio.to_thread(oauth_connection_for_display, context)
            status = OAuthStatus(
                provider=result.provider,
                display_name=result.display_name,
                connected=connected,
                account_label=account_label,
                can_connect=False,
                reset_required=result.reset_required,
                service_account=True,
                reuse_enabled=reuse_enabled,
            )
        else:
            view: oauth.PersonalConnectionView = oauth.personal_connection_view(
                result,
                can_manage=can_manage or provider.requester_scoped_credentials,
            )
            status = OAuthStatus(
                provider=result.provider,
                display_name=result.display_name,
                connected=view.connected,
                account_label=view.account_label,
                can_connect=view.can_connect,
                reset_required=view.reset_required,
                reuse_enabled=reuse_enabled,
            )
    except Exception as exc:
        # OAuth provider code, plugin providers included, can raise anything.
        logger.warning(
            "egress_oauth_status_unavailable",
            service=name,
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        return OAuthStatus(
            provider=provider.id,
            display_name=provider.display_name,
            connected=False,
            account_label=None,
            can_connect=False,
            reset_required=False,
        )
    return replace(status, shared_worker_opt_in=access == "allowed")


async def egress_service_status(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    service: EgressService,
    name: str,
    oauth_part: OAuthStatus | None,
) -> EgressServiceStatus:
    """Combine a service's key status with its already loaded OAuth status; an explicit key wins.

    The key status reads and decrypts a credential file, so it runs in a thread.
    """
    return await asyncio.to_thread(
        service_status,
        manager,
        target,
        service,
        name,
        oauth_status=lambda _provider_id, _target: oauth_part,
    )


def service_oauth_provider(snapshot: ApiSnapshot, service: EgressService | None) -> OAuthProvider | None:
    """Return the registry's provider for an egress service, or None without a service or a known provider."""
    if service is None or service.oauth_provider is None:
        return None
    return load_oauth_providers_for_snapshot(snapshot).get(service.oauth_provider)


def oauth_provider_of_service(
    request: Request,
    service: EgressService,
    worker_target: ResolvedWorkerTarget | None,
    *,
    connecting: bool,
    headers: Mapping[str, str] | None = None,
) -> OAuthProvider:
    """Return the OAuth provider of an egress service for the connect and disconnect routes.

    A service without a provider, or whose provider the registry does not know, is a 404. A provider that the broker
    refuses for `worker_target` (a requester's account in a sandbox several requesters share, see
    `shared_worker_oauth`) is a 409 for both actions, as the status reports nothing connectable there. Connecting is
    also a 409 while a shared service account is configured for the provider, since personal accounts are not
    managed then. Disconnecting stays possible there, so a personal connection stored earlier can still be revoked.
    A worker backend that cannot be read is a 503 for both actions.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    provider = service_oauth_provider(snapshot, service)
    if provider is None:
        raise HTTPException(404, "Service has no account connection", headers=headers)
    try:
        access = shared_worker_oauth(
            provider,
            worker_target,
            opted_in=service.oauth_on_shared_workers,
            runtime_paths=snapshot.runtime_paths,
        )
    except WorkerBackendError as exc:
        # An unreadable worker backend leaves the sandbox kind unknown, so neither action can be judged safe.
        raise HTTPException(503, "Account connection is unavailable right now", headers=headers) from exc
    if access == "refused":
        detail = "Account connection is unavailable here because several users share this agent's sandbox"
        raise HTTPException(409, detail, headers=headers)
    if connecting and oauth_provider_service_account_configured(provider, snapshot.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=headers)
    return provider
