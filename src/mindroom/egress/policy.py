"""Single owner of worker egress policy resolution: "what can this worker reach".

Three layers cooperate on worker egress and this module is the local source of
truth for the policy inputs the runtime can know about:

- This module resolves the static allowlist (env or mounted file), canonical
  hostname validation, and the grant subject derived from one agent's
  execution scope.
- ``mindroom.tools.approved_egress`` consumes this module to short-circuit
  hostnames the static allowlist already covers and to address temporary
  grant requests to the right subject.
- The egress proxy (the external ``mindroom-egress-proxy`` image deployed by
  the chart) enforces the combined policy — static allowlist plus approved
  temporary grants — on actual worker traffic. Approved grants are stored in
  the proxy's own database and are created via its policy API; they are not
  readable from this process, so ``WorkerEgressPolicy`` intentionally carries
  no grant list.

Worker provisioning (``mindroom.workers.backends``) deliberately does not
consume the allowlist: it only wires proxy credentials into worker pods, and
the proxy enforces the policy centrally. The seam shared with provisioning is
the grant subject — a ``worker_key`` grant minted here must use the same
worker key the provisioning path uses for the same scope and identity, which
is why both resolve it through ``mindroom.tool_system.worker_routing``.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.tool_system.worker_routing import resolve_worker_key

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope

_DEFAULT_ALLOWLIST_PATH = "/etc/mindroom-egress/allowed-domains.txt"
_MAX_DNS_NAME_LENGTH = 253
_MAX_DNS_LABEL_LENGTH = 63
_MIN_DNS_LABELS = 2
# Defense-in-depth deny entries. ``canonical_hostname`` rejects bare
# "localhost" earlier via the minimum-label check, so that entry is currently
# redundant on that path; it stays as a backstop so relaxing label validation
# can never re-admit it.
_FORBIDDEN_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
}
_FORBIDDEN_HOST_SUFFIXES = (
    ".localhost",
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
)


@dataclass(frozen=True, slots=True)
class EgressGrantSubject:
    """Address one approved-egress grant to an agent or a scoped worker."""

    subject_type: str
    subject: str


@dataclass(frozen=True, slots=True)
class WorkerEgressPolicy:
    """Locally-resolvable egress policy for one worker context.

    Approved temporary grants are owned by the egress proxy's policy service
    and cannot be enumerated here; this value carries the static allowlist and
    the subject new grants must be addressed to.
    """

    static_allowlist: tuple[str, ...]
    grant_subject: EgressGrantSubject | None = None


def load_static_allowlist() -> tuple[str, ...]:
    """Read the static egress allowlist from env or the mounted allowlist file."""
    inline = os.environ.get("MINDROOM_APPROVED_EGRESS_ALLOWLIST", "").strip()
    text = inline.replace(",", "\n") if inline else ""
    if not text:
        allowlist_path = (
            os.environ.get("MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH")
            or os.environ.get("MINDROOM_EGRESS_ALLOWLIST_PATH")
            or _DEFAULT_ALLOWLIST_PATH
        ).strip()
        if allowlist_path:
            try:
                text = Path(allowlist_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                text = ""
    entries = (line for raw in text.splitlines() if (line := raw.split("#", 1)[0].strip()))
    return tuple(dict.fromkeys(entries))


def _raw_hostname(value: str) -> str:
    if not isinstance(value, str):
        msg = "hostname must be a string"
        raise TypeError(msg)
    raw = value.strip().rstrip(".")
    if not raw:
        msg = "hostname must not be empty"
        raise ValueError(msg)
    if "://" in raw or any(part in raw for part in ("/", "?", "#", "@")):
        msg = "hostname must not include a scheme, path, query, or credentials"
        raise ValueError(msg)
    if "*" in raw:
        msg = "hostname wildcards are not supported"
        raise ValueError(msg)
    if ":" in raw:
        msg = "hostname must not include a port"
        raise ValueError(msg)
    if len(raw) > _MAX_DNS_NAME_LENGTH:
        msg = "hostname is too long"
        raise ValueError(msg)
    return raw


def _reject_ip_literal(raw: str) -> None:
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return
    msg = "IP literals are not valid egress hostnames"
    raise ValueError(msg)


def _idna_hostname(raw: str) -> str:
    try:
        return raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        msg = "hostname is not valid IDNA"
        raise ValueError(msg) from exc


def _validate_dns_labels(normalized: str) -> None:
    labels = normalized.split(".")
    if len(labels) < _MIN_DNS_LABELS:
        msg = "hostname must be a fully-qualified external DNS name"
        raise ValueError(msg)
    if len(normalized) > _MAX_DNS_NAME_LENGTH or any(not label for label in labels):
        msg = "hostname is not a valid DNS name"
        raise ValueError(msg)
    for label in labels:
        if len(label) > _MAX_DNS_LABEL_LENGTH or label.startswith("-") or label.endswith("-"):
            msg = "hostname is not a valid DNS name"
            raise ValueError(msg)
        if not all(char.isalnum() or char == "-" for char in label):
            msg = "hostname contains unsupported characters"
            raise ValueError(msg)


def _reject_internal_hostname(normalized: str) -> None:
    if normalized in _FORBIDDEN_HOSTNAMES or normalized.endswith(_FORBIDDEN_HOST_SUFFIXES):
        msg = "hostname points at an internal name"
        raise ValueError(msg)


def canonical_hostname(value: str) -> str:
    """Canonicalize one egress hostname, rejecting malformed and internal names."""
    raw = _raw_hostname(value)
    _reject_ip_literal(raw)
    normalized = _idna_hostname(raw)
    _validate_dns_labels(normalized)
    _reject_internal_hostname(normalized)
    return normalized


def is_hostname_allowed(hostname: str, policy: WorkerEgressPolicy) -> bool:
    """Return whether one canonical hostname matches the policy's static allowlist.

    ``hostname`` must already be canonicalized via :func:`canonical_hostname`;
    non-canonical input is not normalized here and fails closed (``False``),
    which at worst routes an already-allowed host through the grant flow.
    """
    for entry in policy.static_allowlist:
        try:
            if entry.startswith("."):
                base = canonical_hostname(entry[1:])
                if hostname == base or hostname.endswith(f".{base}"):
                    return True
            elif hostname == canonical_hostname(entry):
                return True
        except ValueError:
            continue
    return False


def resolve_grant_subject(
    *,
    agent_name: str,
    worker_scope: WorkerScope | None,
    execution_identity: ToolExecutionIdentity | None,
) -> EgressGrantSubject:
    """Resolve the grant subject one agent's execution scope maps to."""
    if worker_scope == "user_agent":
        worker_key = (
            resolve_worker_key("user_agent", execution_identity, agent_name=agent_name)
            if execution_identity is not None
            else None
        )
        if worker_key is None:
            msg = "could not resolve the user-agent worker key for this request"
            raise RuntimeError(msg)
        return EgressGrantSubject(subject_type="worker_key", subject=worker_key)
    if worker_scope == "user":
        msg = "approved egress is not supported for worker_scope=user"
        raise RuntimeError(msg)
    return EgressGrantSubject(subject_type="agent", subject=agent_name)


def resolve_worker_egress_policy(
    *,
    agent_name: str | None = None,
    worker_scope: WorkerScope | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> WorkerEgressPolicy:
    """Collect the static allowlist and the scope-derived grant subject into one policy value.

    Without ``agent_name`` this resolves the subject-less policy (static
    allowlist only), which is all the allow-decision needs.
    """
    grant_subject = (
        resolve_grant_subject(
            agent_name=agent_name,
            worker_scope=worker_scope,
            execution_identity=execution_identity,
        )
        if agent_name is not None
        else None
    )
    return WorkerEgressPolicy(static_allowlist=load_static_allowlist(), grant_subject=grant_subject)
