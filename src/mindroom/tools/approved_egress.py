"""Approved worker egress tool.

Hostname validation, the static allowlist, and grant-subject resolution are
owned by ``mindroom.egress.policy``; this module only drives the approval
flow against the egress proxy's policy API.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import unicodedata
from types import MethodType
from typing import TYPE_CHECKING, cast
from urllib import error, parse, request

from agno.tools import Toolkit

from mindroom.egress.policy import (
    canonical_hostname,
    is_hostname_allowed,
    load_static_allowlist,
    resolve_grant_subject,
    resolve_worker_egress_policy,
)
from mindroom.tool_system.approval_bypass import ToolApprovalBypassResult, register_tool_approval_bypass
from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from http.client import HTTPMessage
    from typing import IO, Any

    from mindroom.egress.policy import EgressGrantSubject
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_DEFAULT_MAX_TTL_SECONDS = 6 * 60 * 60
_DEFAULT_POLICY_API_URL = "http://mindroom-egress-proxy:8080"
_MAX_ALLOWLIST_ENTRIES_IN_TOOL_DESCRIPTION = 80
_MAX_REASON_CHARS = 500


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer"
        raise RuntimeError(msg) from exc


def _static_allowlist_description() -> str:
    entries = load_static_allowlist()
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


def _static_allowlist_no_grant_result(host: str) -> str:
    return f"{host} is already allowed by the static egress allowlist. No temporary grant was created."


def _request_network_access_arguments_bypass_approval(
    arguments: Mapping[str, object],
) -> bool | ToolApprovalBypassResult:
    hostname = arguments.get("hostname")
    if not isinstance(hostname, str):
        return True
    try:
        host = canonical_hostname(hostname)
    except ValueError:
        return True
    try:
        _validate_request_metadata(
            cast("int", arguments.get("ttl_minutes")),
            cast("str", arguments.get("reason")),
        )
    except (TypeError, ValueError):
        return True
    if is_hostname_allowed(host, resolve_worker_egress_policy()):
        return ToolApprovalBypassResult(_static_allowlist_no_grant_result(host))
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


def _validate_request_metadata(ttl_minutes: int, reason: str) -> tuple[str, int]:
    if type(ttl_minutes) is not int:
        msg = "ttl_minutes must be an integer"
        raise TypeError(msg)
    if ttl_minutes <= 0:
        msg = "ttl_minutes must be positive"
        raise ValueError(msg)
    return _normalize_reason(reason), ttl_minutes * 60


def _effective_ttl_seconds(requested_ttl_seconds: int) -> int:
    if requested_ttl_seconds <= 0:
        msg = "ttl_minutes must be positive"
        raise ValueError(msg)
    max_ttl = _env_int("MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS", _DEFAULT_MAX_TTL_SECONDS)
    return max(1, min(requested_ttl_seconds, max_ttl))


def _grant_subject(context: ToolRuntimeContext) -> EgressGrantSubject:
    agent_name = context.agent_name
    scope = context.config.get_agent_execution_scope(agent_name)
    execution_identity = build_execution_identity_from_runtime_context(context) if scope == "user_agent" else None
    return resolve_grant_subject(
        agent_name=agent_name,
        worker_scope=scope,
        execution_identity=execution_identity,
    )


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
        host = canonical_hostname(hostname)
        normalized_reason, requested_ttl_seconds = _validate_request_metadata(ttl_minutes, reason)
        if is_hostname_allowed(host, resolve_worker_egress_policy()):
            return _static_allowlist_no_grant_result(host)
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


def _request_network_access_bypasses_approval(
    entrypoint: Callable[..., Any],
    arguments: Mapping[str, object],
) -> bool | ToolApprovalBypassResult:
    if not isinstance(entrypoint, MethodType):
        return False
    if type(entrypoint.__self__) is not _ApprovedEgressTools:
        return False
    if entrypoint.__func__ is not _ApprovedEgressTools.request_network_access:
        return False
    return _request_network_access_arguments_bypass_approval(arguments)


register_tool_approval_bypass("request_network_access", _request_network_access_bypasses_approval)


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
