"""Services users define for their own scope, merged with the config services into the config the broker matches.

A user service has the shape of a config service (`EgressService`) and lives in the same primary-only store as
the scope's egress secrets: one document under the reserved credential name `egress__services`, holding
`{"services": {name: authored service}}`. No service's secret can take that name, since names start with `[a-z0-9]`.

`effective_config` puts the config services first and appends the scope's own services. A user service named like
a config service is ignored, so operator services never change; top-level settings such as `unmatched_hosts` come
from config only. Merged configs are cached per scope, and every save or delete in this process drops the cache.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.egress_broker import EgressService, validate_egress_service_name
from mindroom.egress_broker.secrets import delete_egress_document, load_egress_document, save_egress_document
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.egress_broker import EgressBrokerConfig
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "MAX_RULES_PER_SERVICE",
    "MAX_USER_SERVICES",
    "USER_SERVICES_CREDENTIAL_SERVICE",
    "UserServiceConflictError",
    "delete_user_service",
    "effective_config",
    "load_user_services",
    "save_user_service",
]

logger = get_logger(__name__)

USER_SERVICES_CREDENTIAL_SERVICE = "egress__services"
MAX_USER_SERVICES = 50
MAX_RULES_PER_SERVICE = 50

type _ScopeKey = tuple[object, ...]


class UserServiceConflictError(Exception):
    """A user service cannot take the name of a config service: the operator's service wins (HTTP 409)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"service name '{name}' is already used by a service your administrator configured")
        self.name = name


def load_user_services(manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> dict[str, EgressService]:
    """Return the scope's own services; `target=None` is the global store that unscoped agents read.

    An entry that no longer validates is skipped with a warning naming it, so one bad entry spares the rest.
    """
    services: dict[str, EgressService] = {}
    for name, authored in _stored_services(manager, target).items():
        try:
            services[name] = _validated(name, authored)
        except ValueError as exc:
            # The entry holds whatever the user typed, so only its name and the error type are logged.
            logger.warning("egress_broker_user_service_invalid", service=name, error_type=type(exc).__name__)
    return services


def save_user_service(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    name: str,
    service: EgressService,
    *,
    config_services: Mapping[str, EgressService],
) -> None:
    """Add or replace one of the scope's services, stored as authored so a preset stays a preset.

    Raises UserServiceConflictError when `config_services` has the name, and ValueError for an invalid name, a
    service that sets `oauth_on_shared_workers`, a service with more than 50 rules, or a 51st service in the scope.
    """
    validate_egress_service_name(name)
    if name in config_services:
        raise UserServiceConflictError(name)
    _check_user_service(service)
    with _write_lock:
        stored = _stored_services(manager, target)
        if name not in stored and len(stored) >= MAX_USER_SERVICES:
            msg = f"a scope can have at most {MAX_USER_SERVICES} services of its own; delete one before adding another"
            raise ValueError(msg)
        stored[name] = service.authored_model_dump()
        try:
            save_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE, {"services": stored})
        finally:
            # Dropped even when the write fails: it may have replaced the document before failing.
            _cache.invalidate()


def delete_user_service(manager: CredentialsManager, target: ResolvedWorkerTarget | None, name: str) -> bool:
    """Delete one of the scope's services and return whether it existed; the last one takes the document with it.

    The service's stored key, if any, stays in the scope.
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
    under is still current, so a read that a write overtook is never kept.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._entries: dict[_ScopeKey, _Merged] = {}

    def lookup(self, key: _ScopeKey, config: EgressBrokerConfig) -> tuple[int, EgressBrokerConfig | None]:
        """Return the current generation and the cached merge of `config` for `key`, if there is one."""
        with self._lock:
            entry = self._entries.get(key)
            return self._generation, entry.effective if entry is not None and entry.config is config else None

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
