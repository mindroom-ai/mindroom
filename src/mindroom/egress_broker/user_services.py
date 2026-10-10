"""Services users define for their own scope, merged with the config services into the config the broker matches.

A user service has the shape of a config service (`EgressService`) and lives in the same primary-only store as
the scope's egress secrets: one document under the reserved credential name `egress__services`, holding
`{"services": {name: authored service}}`. No service's secret can take that name, since names start with `[a-z0-9]`.

`effective_config` puts the config services first and appends the scope's own services. A user service named like
a config service is ignored, so operator services never change; top-level settings such as `unmatched_hosts` come
from config only. Merged configs are cached per scope, and every save or delete in this process drops the cache.

User services can narrow operator policy, never widen it: `rules.EgressRules` keeps them inside `deny` and inside an
operator's `restrict_to_rules`. They never inherit a key stored under their name, and on shared or unscoped agents
they cannot use an OAuth connection, which would let a manager route the agent's shared account to any host.
Their placeholders are limited to credential-like names and plain values, since a shared agent's services set env
in every requester's sandbox, and config placeholders win over theirs.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.egress_broker import EgressService, validate_egress_service_name
from mindroom.egress_broker.rules import config_covers_rule
from mindroom.egress_broker.secrets import (
    PERSONAL_WORKER_SCOPES,
    delete_egress_document,
    delete_secret,
    load_egress_document,
    save_egress_document,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "MAX_RULES_PER_SERVICE",
    "MAX_SERVICE_BYTES",
    "MAX_USER_SERVICES",
    "USER_SERVICES_CREDENTIAL_SERVICE",
    "UserServiceConflictError",
    "delete_user_service",
    "effective_config",
    "load_user_services",
    "save_user_service",
    "user_service_hosts_not_allowed",
]

logger = get_logger(__name__)

USER_SERVICES_CREDENTIAL_SERVICE = "egress__services"
MAX_USER_SERVICES = 50
MAX_RULES_PER_SERVICE = 50
# Serialized authored JSON: one service, and the whole stored document.
MAX_SERVICE_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 256 * 1024

# User placeholder names: one underscore-separated word must be a credential word, so names such as LD_PRELOAD,
# NODE_OPTIONS, BASH_ENV, or PYTHONPATH (which only contains PAT inside a word) never qualify.
_PLACEHOLDER_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_PLACEHOLDER_NAME_WORDS = frozenset({"TOKEN", "KEY", "SECRET", "PASSWORD", "PAT", "AUTH", "CREDENTIAL"})
_PLACEHOLDER_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,256}")

type _ScopeKey = tuple[object, ...]


class UserServiceConflictError(Exception):
    """A user service cannot take the name of a config service: the operator's service wins (HTTP 409)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"service name '{name}' is already used by a service your administrator configured")
        self.name = name


def load_user_services(manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> dict[str, EgressService]:
    """Return the scope's own services; `target=None` is the global store that unscoped agents read.

    An entry that no longer validates is skipped with a warning naming it, so one bad entry spares the rest.
    A placeholder that user services may not set is dropped with a warning naming the service and the variable.
    Outside a personal scope an entry's `oauth_provider` is dropped, so no OAuth token is resolved or reported for it.
    """
    services: dict[str, EgressService] = {}
    for name, authored in _stored_services(manager, target).items():
        try:
            service = _validated(name, authored)
        except ValueError as exc:
            # The entry holds whatever the user typed, so only its name and the error type are logged.
            logger.warning("egress_broker_user_service_invalid", service=name, error_type=type(exc).__name__)
            continue
        refused = [
            variable for variable, value in service.placeholder_env.items() if _placeholder_error(variable, value)
        ]
        for variable in refused:
            logger.warning("egress_broker_user_service_placeholder_ignored", service=name, variable=variable)
        if refused:
            allowed = {key: value for key, value in service.placeholder_env.items() if key not in refused}
            service = service.model_copy(update={"placeholder_env": allowed})
        if service.oauth_provider is not None and not _personal_scope(target):
            logger.warning("egress_broker_user_service_oauth_ignored", service=name)
            service = service.model_copy(update={"oauth_provider": None})
        services[name] = service
    return services


def save_user_service(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    name: str,
    service: EgressService,
    *,
    config_services: Mapping[str, EgressService],
    oauth_providers: Collection[str],
) -> None:
    """Add or replace one of the scope's services, stored as authored so a preset stays a preset.

    A new name starts without a key: any key stored under it in the scope, for example one left by a removed config
    service, is deleted first. Replacing an existing service keeps its key. `oauth_providers` holds the ids of the
    registry's providers (`oauth.registry.load_oauth_providers`).

    Raises UserServiceConflictError when `config_services` has the name, and ValueError for an invalid name, a
    service that sets `oauth_on_shared_workers`, an OAuth provider (a preset's included) that the registry does not
    know or that a shared or unscoped agent would use, a placeholder user services may not set, a service with more
    than 50 rules or over 16 KiB, a 51st service in the scope, or a scope document over 256 KiB.
    """
    validate_egress_service_name(name)
    if name in config_services:
        raise UserServiceConflictError(name)
    _check_user_service(service)
    for variable, value in service.placeholder_env.items():
        if error := _placeholder_error(variable, value):
            raise ValueError(error)
    if service.oauth_provider is not None:
        if not _personal_scope(target):
            msg = (
                "oauth_provider is only available for services of user and user_agent agents; "
                "on a shared or unscoped agent set oauth_provider to null and use an API key"
            )
            raise ValueError(msg)
        if service.oauth_provider not in oauth_providers:
            known = ", ".join(sorted(oauth_providers)) or "none"
            msg = f"unknown oauth_provider '{service.oauth_provider}' (known providers: {known})"
            raise ValueError(msg)
    with _write_lock:
        stored = _stored_services(manager, target)
        new = name not in stored
        if new and len(stored) >= MAX_USER_SERVICES:
            msg = f"a scope can have at most {MAX_USER_SERVICES} services of its own; delete one before adding another"
            raise ValueError(msg)
        stored[name] = service.authored_model_dump()
        if _json_size({"services": stored}) > MAX_DOCUMENT_BYTES:
            msg = f"a scope's own services can take at most {MAX_DOCUMENT_BYTES // 1024} KiB together"
            raise ValueError(msg)
        try:
            if new:
                delete_secret(manager, target, name)
            save_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE, {"services": stored})
        finally:
            # Dropped even when the write fails: it may have replaced the document before failing.
            _cache.invalidate()


def delete_user_service(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    name: str,
    *,
    config_services: Mapping[str, EgressService],
) -> bool:
    """Delete one of the scope's services and its stored key; return whether the service existed.

    The last service takes the document with it. The key stays when `config_services` has the name: that config
    service shadows the entry and uses the key in this scope.
    """
    with _write_lock:
        stored = _stored_services(manager, target)
        if name not in stored:
            return False
        del stored[name]
        try:
            if stored:
                save_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE, {"services": stored})
            else:
                delete_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE)
            if name not in config_services:
                delete_secret(manager, target, name)
        finally:
            _cache.invalidate()
    return True


def effective_config(
    config: EgressBrokerConfig,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
) -> EgressBrokerConfig:
    """Return the config one scope's requests match against: `config` plus the scope's own services after it.

    Returns `config` itself for a scope without services of its own. The result is cached per scope until the next
    save or delete in this process, or until another config object is passed.
    """
    key = _scope_key(manager, target)
    generation, cached = _cache.lookup(key, config)
    if cached is not None:
        return cached
    own = {
        name: service for name, service in load_user_services(manager, target).items() if name not in config.services
    }
    merged = config.model_copy(update={"services": {**config.services, **own}}) if own else config
    _cache.store(key, generation, config, merged)
    return merged


def user_service_hosts_not_allowed(config: EgressBrokerConfig, service: EgressService) -> list[str]:
    """Return the hosts of `service`'s rules that could reach past the operator's `config`, for a save to refuse.

    Under `unmatched_hosts: deny` the broker applies a user rule only on hosts and ports a config rule names. This
    lists, in rule order without repeats, each rule host that config does not cover for every host and port the rule
    can match. Under `passthrough` every host is allowed, so the list is empty.
    """
    if config.unmatched_hosts != "deny":
        return []
    hosts = [rule.host for rule in service.rules if not config_covers_rule(config, rule)]
    return list(dict.fromkeys(hosts))


def _personal_scope(target: ResolvedWorkerTarget | None) -> bool:
    return target is not None and target.worker_scope in PERSONAL_WORKER_SCOPES


def _placeholder_error(variable: str, value: str) -> str | None:
    """Return why a user service may not set this placeholder, never quoting the value; None when it may."""
    words = variable.split("_")
    if not _PLACEHOLDER_NAME.fullmatch(variable) or _PLACEHOLDER_NAME_WORDS.isdisjoint(words):
        allowed = ", ".join(sorted(_PLACEHOLDER_NAME_WORDS))
        return (
            f"placeholder_env name '{variable}' must match ^[A-Z][A-Z0-9_]*$ and have one of these words "
            f"between underscores: {allowed}"
        )
    if not _PLACEHOLDER_VALUE.fullmatch(value):
        return f"placeholder_env value of '{variable}' must be 1 to 256 characters from A-Z, a-z, 0-9, and . _ : -"
    return None


def _json_size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


def _stored_services(manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> dict[str, object]:
    """Return the scope's stored entries as authored, unvalidated; a malformed document reads as none."""
    document = load_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE)
    if document is None:
        return {}
    services = document.get("services")
    if not isinstance(services, dict):
        logger.warning("egress_broker_user_services_malformed")
        return {}
    return dict(services)


def _validated(name: str, authored: object) -> EgressService:
    validate_egress_service_name(name)
    service = EgressService.model_validate(authored)
    _check_user_service(service)
    return service


def _check_user_service(service: EgressService) -> None:
    """Apply the limits that user services have on top of the config model's validators."""
    if service.oauth_on_shared_workers:
        msg = "oauth_on_shared_workers can only be set by an administrator in config.yaml"
        raise ValueError(msg)
    if len(service.rules) > MAX_RULES_PER_SERVICE:
        msg = f"a service can have at most {MAX_RULES_PER_SERVICE} rules"
        raise ValueError(msg)
    if _json_size(service.authored_model_dump()) > MAX_SERVICE_BYTES:
        msg = f"a service can take at most {MAX_SERVICE_BYTES // 1024} KiB"
        raise ValueError(msg)


def _scope_key(manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> _ScopeKey:
    """Return a key at least as fine as the store `load_egress_document` reads, so two stores never share an entry.

    The store follows the manager's paths and the target's worker scope, routing agent, and requester.
    """
    stores = (manager.base_path, manager.shared_base_path)
    if target is None:
        return (*stores, "global")
    identity = target.execution_identity
    requester_id = identity.requester_id if identity is not None else None
    return (*stores, "target", target.worker_scope, target.routing_agent_name, requester_id)


@dataclass(frozen=True)
class _Merged:
    config: EgressBrokerConfig
    effective: EgressBrokerConfig


class _EffectiveConfigCache:
    """Merged configs per scope, shared by the broker thread, the API, and the OAuth lookup pool.

    Every write bumps the generation and drops all entries. A merge is stored only while the generation it was read
    under is still current, so a read that a write overtook is never kept. An entry serves an equal config too: the
    broker reads the API's config object and tool calls the orchestrator's, which are separate but equal copies
    until a reload publishes the orchestrator's object to the API.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        # Entries are evicted only on writes (and replaced when a scope's config changes); scopes are few.
        self._entries: dict[_ScopeKey, _Merged] = {}

    def lookup(self, key: _ScopeKey, config: EgressBrokerConfig) -> tuple[int, EgressBrokerConfig | None]:
        """Return the current generation and the cached merge of `config` for `key`, if there is one."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not (entry.config is config or entry.config == config):
                return self._generation, None
            return self._generation, entry.effective

    def store(self, key: _ScopeKey, generation: int, config: EgressBrokerConfig, effective: EgressBrokerConfig) -> None:
        with self._lock:
            if generation == self._generation:
                self._entries[key] = _Merged(config=config, effective=effective)

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()


_cache = _EffectiveConfigCache()
# Serializes read-modify-write of the services documents in this process, so concurrent saves lose nothing.
_write_lock = threading.Lock()
