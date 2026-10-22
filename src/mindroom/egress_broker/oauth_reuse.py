"""Which OAuth connections Agent Keys may use: an explicit, revocable opt-in per service.

A connection serves the tools of its provider, whose writes ask for approval. Agent Keys hands the same token to the
agent's shell and python, which run without approval prompts, so the broker uses a connection for a service only once
someone allowed it. The opt-in lives in the store of the connection it unlocks (`oauth_source.broker_credential_context`):
the requester's own store for a requester-scoped provider such as GitHub, else the store of the service's keys. One
reserved document there, `egress__oauth_reuse`, holds `{"services": {name: {"provider", "enabled_by", "enabled_at"}}}`.
An entry naming another provider counts as off, so changing a service's `oauth_provider` turns it off. Workers never
read it: egress documents are primary-only.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.egress_broker.secrets import delete_egress_document, load_egress_document, save_egress_document

if TYPE_CHECKING:
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = ["OAUTH_REUSE_CREDENTIAL_SERVICE", "oauth_reuse_enabled", "set_oauth_reuse"]

OAUTH_REUSE_CREDENTIAL_SERVICE = "egress__oauth_reuse"
# Serializes read-modify-write of the opt-in documents in this process, so concurrent changes lose nothing.
_write_lock = threading.Lock()


def oauth_reuse_enabled(context: OAuthCredentialContext, service: str, provider_id: str) -> bool:
    """Return whether Agent Keys may use the context's connection for `service`, given for `provider_id`.

    A requester-scoped provider without a requester has no store to allow it in. This reads the credential store, so
    async callers run it in a thread.
    """
    if context.provider.requester_scoped_credentials and context.worker_target is None:
        return False
    match _stored(context).get(service):
        case {"provider": provider} if provider == provider_id:
            return True
        case _:
            return False


def set_oauth_reuse(
    context: OAuthCredentialContext,
    service: str,
    provider_id: str,
    *,
    enabled: bool,
    by: str | None,
) -> None:
    """Allow Agent Keys to use the context's connection for `service` (`by` names who did, if known), or stop it.

    The last entry takes the document with it. Blocks on the store, so async callers run it in a thread.
    """
    with _write_lock:
        stored = _stored(context)
        if enabled:
            stored[service] = {
                "provider": provider_id,
                "enabled_by": by,
                "enabled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
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
