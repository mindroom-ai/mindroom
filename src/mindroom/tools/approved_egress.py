"""Approved worker egress tool."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib import error, parse, request

from agno.tools import Toolkit

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
)
from mindroom.tool_system.worker_routing import resolve_worker_key

if TYPE_CHECKING:
    from http.client import HTTPMessage
    from typing import IO

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_DEFAULT_MAX_TTL_SECONDS = 6 * 60 * 60
_DEFAULT_POLICY_API_URL = "http://mindroom-egress-proxy:8080"
_DEFAULT_ALLOWLIST_PATH = "/etc/mindroom-egress/allowed-domains.txt"
_MAX_ALLOWLIST_ENTRIES_IN_TOOL_DESCRIPTION = 80
_MAX_REASON_CHARS = 500
_MAX_DNS_NAME_LENGTH = 253
_MAX_DNS_LABEL_LENGTH = 63
_MIN_DNS_LABELS = 2
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
class _GrantSubject:
    subject_type: str
    subject: str


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer"
        raise RuntimeError(msg) from exc


def _static_allowlist_entries() -> list[str]:
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
            except OSError:
                text = ""
    entries = (line for raw in text.splitlines() if (line := raw.split("#", 1)[0].strip()))
    return list(dict.fromkeys(entries))


def _static_allowlist_description() -> str:
    entries = _static_allowlist_entries()
    if not entries:
        return (
            "Static egress allowlist: unavailable when this tool loaded. "
            "Only request access for external hostnames that are blocked by worker egress."
        )

    visible_entries = entries[:_MAX_ALLOWLIST_ENTRIES_IN_TOOL_DESCRIPTION]
    remaining = len(entries) - len(visible_entries)
    suffix = f"; plus {remaining} more entries" if remaining > 0 else ""
    return (
        "Static egress allowlist (do not request access for hostnames matching these patterns): "
        f"{', '.join(visible_entries)}{suffix}."
    )


def _request_network_access_description() -> str:
    return (
        "Request temporary worker egress to one exact external hostname. "
        "Use this only when the worker needs a hostname that is not already allowed.\n\n"
        f"{_static_allowlist_description()}"
    )


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


def _canonical_hostname(value: str) -> str:
    raw = _raw_hostname(value)
    _reject_ip_literal(raw)
    normalized = _idna_hostname(raw)
    _validate_dns_labels(normalized)
    _reject_internal_hostname(normalized)
    return normalized


def _static_allowlist_allows(host: str) -> bool:
    """Return whether one canonical hostname matches the static allowlist."""
    for entry in _static_allowlist_entries():
        try:
            if entry.startswith("."):
                base = _canonical_hostname(entry[1:])
                if host == base or host.endswith(f".{base}"):
                    return True
            elif host == _canonical_hostname(entry):
                return True
        except ValueError:
            continue
    return False


def _is_plain_http_api_host_allowed(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    if host in {"localhost", "mindroom-egress-proxy"}:
        return True
    if host.endswith((".svc", ".svc.cluster.local")):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _policy_api_url() -> str:
    url = (os.environ.get("MINDROOM_APPROVED_EGRESS_API_URL") or _DEFAULT_POLICY_API_URL).rstrip("/")
    parsed = parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = "MINDROOM_APPROVED_EGRESS_API_URL must be an http or https URL"
        raise RuntimeError(msg)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        msg = "MINDROOM_APPROVED_EGRESS_API_URL must not include userinfo, path, query, or fragment"
        raise RuntimeError(msg)
    hostname = parsed.hostname or ""
    if parsed.scheme == "http" and not _is_plain_http_api_host_allowed(hostname):
        msg = "plain HTTP approved egress policy API URLs must use loopback or an in-cluster service name"
        raise RuntimeError(msg)
    return url


def _policy_token() -> str:
    token = (os.environ.get("MINDROOM_APPROVED_EGRESS_TOKEN") or "").strip()
    if not token:
        msg = "MINDROOM_APPROVED_EGRESS_TOKEN is not configured"
        raise RuntimeError(msg)
    return token


def _normalize_reason(value: str) -> str:
    if not isinstance(value, str):
        msg = "reason must be a string"
        raise TypeError(msg)
    cleaned = "".join(" " if char.isspace() or unicodedata.category(char)[0] == "C" else char for char in value)
    normalized = " ".join(cleaned.split())
    if not normalized:
        msg = "reason must not be empty"
        raise ValueError(msg)
    return normalized[:_MAX_REASON_CHARS]


def _effective_ttl_seconds(requested_ttl_seconds: int) -> int:
    if requested_ttl_seconds <= 0:
        msg = "ttl_minutes must be positive"
        raise ValueError(msg)
    max_ttl = _env_int("MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS", _DEFAULT_MAX_TTL_SECONDS)
    return max(1, min(requested_ttl_seconds, max_ttl))


def _grant_subject(context: ToolRuntimeContext) -> _GrantSubject:
    agent_name = context.agent_name
    scope = context.config.get_agent_execution_scope(agent_name)
    if scope == "user_agent":
        identity = build_execution_identity_from_runtime_context(context)
        worker_key = resolve_worker_key("user_agent", identity, agent_name=agent_name)
        if worker_key is None:
            msg = "could not resolve the user-agent worker key for this request"
            raise RuntimeError(msg)
        return _GrantSubject(subject_type="worker_key", subject=worker_key)
    if scope == "user":
        msg = "approved egress is not supported for worker_scope=user"
        raise RuntimeError(msg)
    return _GrantSubject(subject_type="agent", subject=agent_name)


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = request.build_opener(_NoRedirectHandler)


def _read_policy_response(req: request.Request) -> bytes:
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=10) as response:
            return response.read(256 * 1024)
    except error.HTTPError as exc:
        response_body = exc.read(256 * 1024)
        detail = str(exc.reason)
        if response_body:
            try:
                parsed = _parse_policy_response(response_body)
            except RuntimeError:
                detail = response_body.decode("utf-8", errors="replace")[:512] or detail
            else:
                detail = str(parsed.get("error") or response_body.decode("utf-8", errors="replace")[:512] or detail)
        msg = f"approved egress policy service returned HTTP {exc.code}: {detail}"
        raise RuntimeError(msg) from exc


def _parse_policy_response(response_body: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        msg = "approved egress policy service returned invalid JSON"
        raise RuntimeError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "approved egress policy service returned a non-object response"
        raise RuntimeError(msg)  # noqa: TRY004
    return parsed


def _post_grant(payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    req = request.Request(  # noqa: S310 - _policy_api_url validates scheme and host.
        f"{_policy_api_url()}/grants",
        data=body,
        headers={
            "authorization": f"Bearer {_policy_token()}",
            "content-type": "application/json",
        },
        method="POST",
    )
    parsed = _parse_policy_response(_read_policy_response(req))
    if parsed.get("ok") is not True:
        msg = str(parsed.get("error") or "approved egress policy service rejected the grant")
        raise RuntimeError(msg)
    grant = parsed.get("grant")
    if not isinstance(grant, dict):
        msg = "approved egress policy service response is missing the grant"
        raise RuntimeError(msg)  # noqa: TRY004
    return cast("dict[str, object]", grant)


class _ApprovedEgressTools(Toolkit):
    """Request temporary hostname egress access for MindRoom workers."""

    def __init__(self) -> None:
        request_description = _request_network_access_description()
        super().__init__(
            name="approved_egress",
            instructions=(
                "Use this tool when a worker needs temporary access to an external hostname that the egress proxy "
                "blocks. Request one exact hostname, a TTL in minutes, and a concise reason. The "
                "request_network_access tool definition lists static allowlist patterns that do not need an approval "
                "request."
            ),
            tools=[self.request_network_access],
        )
        registered = self.async_functions.get("request_network_access")
        if registered is not None:
            registered.description = request_description

    async def request_network_access(
        self,
        hostname: str,
        ttl_minutes: int,
        reason: str,
    ) -> str:
        """Request temporary worker egress to one exact external hostname."""
        host = _canonical_hostname(hostname)
        if _static_allowlist_allows(host):
            return f"{host} is already allowed by the static egress allowlist. No temporary grant was created."
        normalized_reason = _normalize_reason(reason)
        requested_ttl_seconds = ttl_minutes * 60
        effective_ttl_seconds = _effective_ttl_seconds(requested_ttl_seconds)

        context = get_tool_runtime_context()
        if context is None:
            msg = "request_network_access requires a live MindRoom Matrix tool context"
            raise RuntimeError(msg)
        subject = _grant_subject(context)
        payload: dict[str, object] = {
            "hostname": host,
            "subject_type": subject.subject_type,
            "subject": subject.subject,
            "agent_name": context.agent_name,
            "requester_id": context.requester_id,
            "room_id": context.room_id,
            "thread_id": context.resolved_thread_id or context.thread_id,
            "ttl_seconds": effective_ttl_seconds,
            "approved_by": context.requester_id,
            "reason": normalized_reason,
        }
        grant = await asyncio.to_thread(
            _post_grant,
            payload,
        )
        expiry = grant.get("expires_at")
        capped = " Deployment policy capped the requested TTL." if effective_ttl_seconds < requested_ttl_seconds else ""
        return (
            f"Approved temporary network access to {host} for {effective_ttl_seconds // 60} minutes. "
            f"Expires at Unix time {expiry}.{capped}"
        )


@register_tool_with_metadata(
    name="approved_egress",
    display_name="Approved Worker Egress",
    description="Request human-approved temporary worker access to blocked external hostnames",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.SPECIAL,
    icon="FiShield",
    icon_color="text-emerald-600",
    function_names=("request_network_access",),
)
def approved_egress_tools() -> type[Toolkit]:
    """Return the approved worker egress toolkit."""
    return _ApprovedEgressTools
