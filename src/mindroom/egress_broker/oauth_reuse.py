"""Which OAuth connections Agent Keys may use: an explicit, revocable opt-in per service.

A connection serves the tools of its provider, whose writes ask for approval. Agent Keys hands the same token to the
agent's shell and python, which run without approval prompts, so the broker uses a connection for a service only once
someone allowed it. The opt-in lives in the store of the connection it unlocks (`oauth_source.broker_credential_context`):
the requester's own store for a requester-scoped provider such as GitHub, else the store of the service's keys. One
reserved document there, `egress__oauth_reuse`, holds
`{"services": {name: {"provider", "credential_service", "enabled_by", "enabled_at", "access"}}}`, where `access` is
`"read"` or `"read_write"`. An entry counts as off while it names a provider other than the service's `oauth_provider`,
or a connection (`credential_service`) other than the one the service uses now, so switching a service between its own
account and the tools' one never carries an opt-in across. Workers never read it: egress documents are primary-only.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.egress_broker.secrets import delete_egress_document, load_egress_document, save_egress_document

if TYPE_CHECKING:
    from mindroom.egress_broker.secrets import OAuthAccess
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = ["OAUTH_REUSE_CREDENTIAL_SERVICE", "oauth_reuse_access", "set_oauth_reuse"]

OAUTH_REUSE_CREDENTIAL_SERVICE = "egress__oauth_reuse"
# Serializes read-modify-write of the opt-in documents in this process, so concurrent changes lose nothing.
_write_lock = threading.Lock()


def oauth_reuse_access(
    context: OAuthCredentialContext,
    service: str,
    provider_id: str,
    *,
    separate_account: bool,
) -> OAuthAccess | None:
    """Return what Agent Keys may do with the context's connection for `service`, given for `provider_id`; None if off.

    `separate_account` says the context's connection is the service's own account, not the provider's own. A
    requester-scoped provider without a requester has no store to allow it in. This reads the credential store, so
    async callers run it in a thread.
    """
    if context.provider.requester_scoped_credentials and context.worker_target is None:
        return None
    match _stored(context).get(service):
        case {"provider": provider, **fields} if provider == provider_id:
            access = fields.get("access", "read_write")
            connection = fields.get("credential_service")
        case _:
            return None
    # LEGACY_COMPAT: Agent Keys account opt-ins without a credential service.
    # Legacy format: an `egress__oauth_reuse` entry without `credential_service`.
    # Last legacy release: v2026.10.601 and every release before it write entries without it, all for the provider's
    # own connection; the unreleased replacement names the connection in every entry it writes.
    # Handling: such an entry counts for the provider's own connection only, never for a service's own account.
    # Coverage: tests/egress_broker/test_oauth_source.py::test_an_opt_in_without_a_credential_service_counts_for_the_tools_connection_only.
    if connection != context.provider.credential_service and (connection is not None or separate_account):
        return None
    # LEGACY_COMPAT: Agent Keys account opt-ins without an access level.
    # Legacy format: an `egress__oauth_reuse` entry without `access`.
    # Last legacy release: unreleased replacement; v2026.10.596, the newest release when this was written, and every
    # release before it write entries without `access`, which allowed writes.
    # Handling: such an entry reads as "read_write", as it always allowed, so no opt-in changes or is migrated.
    # Coverage: tests/egress_broker/test_oauth_source.py::test_entry_without_access_reads_read_write.
    # Any other value than "read_write" reads as "read", so a damaged entry never allows writes.
    return "read_write" if access == "read_write" else "read"


def set_oauth_reuse(
    context: OAuthCredentialContext,
    service: str,
    provider_id: str,
    *,
    enabled: bool,
    by: str | None,
    access: OAuthAccess = "read_write",
) -> None:
    """Allow Agent Keys to use the context's connection for `service` at `access` (`by` names who did, if known), or stop it.

    Allowing an allowed service again replaces its entry, so it changes the level. The last entry takes the document
    with it. Blocks on the store, so async callers run it in a thread.
    """
    with _write_lock:
        stored = _stored(context)
        if enabled:
            stored[service] = {
                "provider": provider_id,
                "credential_service": context.provider.credential_service,
                "enabled_by": by,
                "enabled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "access": access,
            }
        else:
            stored.pop(service, None)
        manager, target = context.credentials_manager, _store_target(context)
        if stored:
            save_egress_document(manager, target, OAUTH_REUSE_CREDENTIAL_SERVICE, {"services": stored})
        else:
            delete_egress_document(manager, target, OAUTH_REUSE_CREDENTIAL_SERVICE)


def _store_target(context: OAuthCredentialContext) -> ResolvedWorkerTarget | None:
    """Return the egress store of the context's connection; an unscoped agent's is the global store, like its keys."""
    target = context.worker_target
    return None if target is None or target.worker_scope is None else target


def _stored(context: OAuthCredentialContext) -> dict[str, object]:
    document = load_egress_document(context.credentials_manager, _store_target(context), OAUTH_REUSE_CREDENTIAL_SERVICE)
    services = None if document is None else document.get("services")
    return dict(services) if isinstance(services, dict) else {}
