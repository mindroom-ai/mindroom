"""Tests for TLS interception: per-request credential injection inside CONNECT tunnels to hosts with rules."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
import ssl
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import certifi
import pytest
from cryptography.hazmat.primitives import serialization
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker.ca import materialize_ca_bundle
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.messages import ScopeInfo
from mindroom.egress_broker.mitm import _verifying_context
from mindroom.egress_broker.secrets import Secret, SecretMissing, SecretNeedsReconnect, SecretUnavailable
from tests.egress_broker.conftest import (
    AMBIGUOUS_PATH,
    BODY_TOO_LARGE,
    BROKER_ERROR,
    CONNECT_URL,
    DEFAULT_CLAIMS,
    LOCALHOST_BLOCKED,
    LOCALHOST_UNREACHABLE,
    MALFORMED,
    MANAGE_URL,
    METHOD_NOT_ALLOWED,
    PROXY_AUTHENTICATION_REQUIRED,
    assert_audited,
    audit_records,
    connect_request,
    env_without_proxies,
    free_port,
    git_ls_remote,
    host_not_allowed,
    proxy_authorization,
    read_raw_response,
    run_command,
    single_rule_config,
    with_user_services,
)
from tests.egress_broker.raw_http import open_tunnel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.tokens import WorkerClaims
    from tests.egress_broker.conftest import BrokerFactory, ProxyClient, RawResponse, ServeFactory, Upstream, UpstreamCA

SECRET = "s3cret"  # noqa: S105 - test credential the fake upstream expects
RECONNECT_MESSAGE = (
    "The github account for svc must be connected again. Ask the user to reconnect at connect_url or manage_url."
)


async def _exchange(broker: BrokerFactory, port: int, *messages: bytes) -> list[RawResponse]:
    """Open a tunnel to ``localhost:<port>``, send each raw message in turn, and return the response read after each."""
    client = await open_tunnel(broker, port)
    try:
        responses = []
        for message in messages:
            client.send(message)
            responses.append(await read_raw_response(client.reader))
    finally:
        client.close()
    return responses


def _websocket_handshake(port: int, *, connection: str = "Upgrade", upgrade: str = "websocket") -> bytes:
    return (
        f"GET /ws HTTP/1.1\r\nHost: localhost:{port}\r\n"
        f"Upgrade: {upgrade}\r\nConnection: {connection}\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "localhost."])
async def test_connect_to_rule_host_is_intercepted(broker: BrokerFactory, host: str) -> None:
    """A host with rules (case and one trailing dot normalized) gets the broker's certificate, not a blind tunnel."""
    await broker(single_rule_config(), secrets={"svc": SECRET})

    # The client trusts only the broker CA, so the handshake alone proves the broker answered.
    client = await open_tunnel(broker, 443, host=host)
    try:
        assert ("DNS", "localhost") in client.writer.get_extra_info("peercert")["subjectAltName"]
    finally:
        client.close()
    assert broker.resolved == []


@pytest.mark.asyncio
async def test_injects_bearer_and_client_never_sees_secret(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """The upstream receives the injected secret; nothing the broker relays back to the client contains it."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/ok?page=2"))

    assert response.status_code == 200
    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "proxy-authorization" not in seen.headers
    assert SECRET not in response.text
    assert not [value for value in response.headers.values() if SECRET in value]
    # A forwarded request has no refusal code, whatever the upstream answered.
    await assert_audited(
        audit,
        kind="request",
        method="GET",
        host="localhost",
        path="/ok",
        service="svc",
        status=200,
        code=None,
    )


@pytest.mark.asyncio
async def test_placeholder_authorization_is_replaced(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A client's placeholder Authorization is removed, so the upstream sees exactly one injected value."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"), headers={"Authorization": "token mindroom-brokered"})

    assert response.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
async def test_keepalive_injects_per_request(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """Each request on one tunnel is matched on its own path, over a single upstream connection."""
    await broker(single_rule_config(path_prefix="/echo"), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    assert (await client.get(tls_upstream.url("/echo"))).status_code == 200
    assert (await client.get(tls_upstream.url("/cookie"))).status_code == 200

    first, second = tls_upstream.requests
    assert first.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "authorization" not in second.headers
    assert first.peer_port == second.peer_port
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.service) for record in records} == {
        "/echo": ("request", "svc"),
        "/cookie": ("request", None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_header",
    ["evil.example", "localhost.evil.example", "localhost:evil.example", "user@localhost", "[::1]"],
)
async def test_host_mismatch_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    host_header: str,
) -> None:
    """A Host naming anything but the CONNECT host is refused before any secret lookup (no domain fronting)."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"), headers={"Host": host_header})

    assert response.status_code == 403
    assert response.headers["connection"] == "close"
    assert response.json() == {
        "error": "host_mismatch",
        "message": "The Host header does not match the host this connection was opened for.",
    }
    assert tls_upstream.hits == []
    assert broker.resolved == []
    await assert_audited(audit, kind="denied", status=403, host="localhost", path="/echo", code="host_mismatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("host_header", ["LOCALHOST.:{port}", "localhost"])
async def test_matching_host_header_is_forwarded(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    host_header: str,
) -> None:
    """A Host naming the CONNECT host (any case, port, or trailing dot) reaches the upstream unchanged."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())
    sent = host_header.format(port=tls_upstream.port)

    response = await client.get(tls_upstream.url("/echo"), headers={"Host": sent})

    assert response.status_code == 200
    echoed = response.json()["headers"]
    assert echoed["host"] == [sent]
    assert echoed["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "target"),
    [("GET", "https://localhost/echo"), ("CONNECT", "evil.example:443"), ("OPTIONS", "*")],
    ids=["absolute-form", "authority-form", "asterisk-form"],
)
async def test_non_origin_form_target_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
    method: str,
    target: str,
) -> None:
    """Inside a tunnel only origin-form targets are served; the CONNECT target alone decides the destination."""
    await broker(single_rule_config(), secrets={"svc": SECRET})

    [response] = await _exchange(
        broker,
        tls_upstream.port,
        f"{method} {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
    )

    assert response.status == 400
    assert response.json() == MALFORMED
    assert tls_upstream.hits == []
    assert broker.resolved == []
    await assert_audited(audit, kind="denied", status=400, path="", code="bad_request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "/private/../echo",
        "/private/%2e%2e/echo",
        "/private/%2E%2e/echo",
        "/private/%252e%252e/echo",
        "/private//echo",
        "/private\\..\\echo",
    ],
    ids=["dot-dot", "encoded", "mixed-case", "double-encoded", "empty-segment", "backslash"],
)
async def test_ambiguous_path_on_rule_host_is_refused(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
    target: str,
) -> None:
    """A path an upstream could resolve differently is refused before matching, so it cannot borrow a rule."""
    await broker(single_rule_config(path_prefix="/private"), secrets={"svc": SECRET})

    [response] = await _exchange(
        broker,
        tls_upstream.port,
        f"GET {target}?page=2 HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
    )

    assert response.status == 400
    assert response.headers["connection"] == "close"
    assert response.json() == AMBIGUOUS_PATH
    assert tls_upstream.hits == []
    assert broker.resolved == []
    await assert_audited(audit, kind="denied", status=400, path=target, service=None, code="bad_request")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["TRACE", "TRACK", "trace"])
async def test_trace_and_track_on_rule_host_are_refused_before_any_secret_or_dial(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
    method: str,
) -> None:
    """TRACE and TRACK would make the upstream echo the injected credential, so the broker refuses them first."""
    await broker(single_rule_config(), secrets={"svc": SECRET})

    [response] = await _exchange(
        broker,
        tls_upstream.port,
        f"{method} /echo?page=2 HTTP/1.1\r\nHost: localhost\r\n\r\n".encode(),
    )

    assert response.status == 405
    assert response.headers["x-mindroom-error"] == "method_not_allowed"
    assert response.headers["connection"] == "close"
    assert response.json() == METHOD_NOT_ALLOWED
    assert tls_upstream.hits == []
    assert broker.resolved == []
    await assert_audited(
        audit,
        kind="denied",
        method=method,
        status=405,
        path="/echo",
        service=None,
        code="method_not_allowed",
    )


@pytest.mark.asyncio
async def test_other_methods_on_rule_host_still_get_the_credential(
    broker: BrokerFactory,
    tls_upstream: Upstream,
) -> None:
    """Only TRACE and TRACK are refused: other methods, OPTIONS and DELETE included, are forwarded with the key."""
    await broker(single_rule_config(), secrets={"svc": SECRET})

    options, delete = await _exchange(
        broker,
        tls_upstream.port,
        b"OPTIONS /echo HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"DELETE /echo HTTP/1.1\r\nHost: localhost\r\n\r\n",
    )

    for response, method in ((options, "OPTIONS"), (delete, "DELETE")):
        assert response.status == 200
        echoed = response.json()
        assert echoed["method"] == method
        assert echoed["headers"]["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
async def test_restrict_to_rules_refuses_unlisted_paths_in_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A restricted service's host serves only listed paths; others get 403 instead of an unauthenticated forward."""
    await broker(
        single_rule_config(path_prefix="/echo", restrict_to_rules=True),
        secrets={"svc": SECRET},
        manage_url=MANAGE_URL,
    )
    client = proxy_client(broker.token())

    allowed = await client.get(tls_upstream.url("/echo"))
    refused = await client.get(tls_upstream.url("/ok?page=2"))

    assert allowed.status_code == 200
    assert allowed.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert refused.status_code == 403
    assert refused.headers["connection"] == "close"
    assert refused.json() == {
        "error": "path_not_allowed",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "message": "localhost/ok is outside the paths svc allows for this agent. "
        "Only an administrator can change this, at manage_url.",
    }
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["svc"]
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.status, record.service) for record in records} == {
        "/echo": ("request", 200, "svc"),
        "/ok": ("denied", 403, "svc"),
    }
    assert {record.path: record.code for record in records} == {"/echo": None, "/ok": "path_not_allowed"}


@pytest.mark.asyncio
async def test_user_restriction_refuses_operator_paths_in_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A user's limit also refuses paths the operator's broad service would cover, and says which service and where."""
    operator = EgressBrokerConfig(
        services={"op": EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])},
    )
    limited = EgressService(
        restrict_to_rules=True,
        rules=[EgressRule(host="localhost", path_prefix="/echo", auth=EgressAuth(type="bearer"))],
    )
    await broker(
        rules_provider=lambda _claims: with_user_services(operator, limited=limited),
        secrets={"op": "operator-key", "limited": "user-key"},
        manage_url=MANAGE_URL,
    )
    client = proxy_client(broker.token())

    allowed = await client.get(tls_upstream.url("/echo"))
    refused = await client.get(tls_upstream.url("/ok?access_token=leaked"))

    assert allowed.json()["headers"]["authorization"] == ["Bearer user-key"]
    assert refused.status_code == 403
    assert refused.json() == {
        "error": "path_not_allowed",
        "service": "limited",
        "manage_url": MANAGE_URL,
        "message": "localhost/ok is outside the paths limited allows for this agent. "
        "Ask the user to add it at manage_url if the agent needs it.",
    }
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["limited"]
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.status, record.code, record.service) for record in records} == {
        "/echo": ("request", 200, None, "limited"),
        "/ok": ("denied", 403, "path_not_allowed", "limited"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("managers", "ending"),
    [
        (("@alice:example.org",), "Only its credential managers (@alice:example.org) can change this, at manage_url."),
        ((), "Only an administrator can change this, at manage_url."),
    ],
)
async def test_limit_in_place_on_a_shared_agent_keeps_the_operator_key_and_names_who_can_change_it(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    managers: tuple[str, ...],
    ending: str,
) -> None:
    """A restrict-only limit lets listed paths use the operator's key; others get a refusal naming its display name."""
    operator = EgressBrokerConfig(
        services={"op": EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])},
    )
    limited = EgressService(
        restrict_only=True,
        display_name="Local (echo)",
        rules=[EgressRule(host="localhost", path_prefix="/echo")],
    )
    await broker(
        rules_provider=lambda _claims: with_user_services(operator, limited=limited),
        secrets={"op": "operator-key", "limited": "never-sent"},
        manage_url=MANAGE_URL,
        scope_info=lambda _claims: ScopeInfo(shared=True, managers=managers, personal_page=True),
    )
    client = proxy_client(broker.token())

    allowed = await client.get(tls_upstream.url("/echo"))
    refused = await client.get(tls_upstream.url("/ok"))

    assert allowed.json()["headers"]["authorization"] == ["Bearer operator-key"]
    assert refused.status_code == 403
    assert refused.json() == {
        "error": "path_not_allowed",
        "service": "limited",
        "manage_url": MANAGE_URL,
        "message": f"localhost/ok is outside the paths Local (echo) allows for this agent. {ending}",
    }
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["op"]
    records = await audit_records(audit, 2)
    assert {record.path: (record.kind, record.status, record.code, record.service) for record in records} == {
        "/echo": ("request", 200, None, "op"),
        "/ok": ("denied", 403, "path_not_allowed", "limited"),
    }


@pytest.mark.asyncio
async def test_redirect_is_returned_not_followed(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A redirect reaches the client as-is; following it would be a new CONNECT and a new match."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/redirect"))

    assert response.status_code == 302
    assert response.headers["location"] == "https://other.invalid/"
    assert tls_upstream.hits == ["/redirect"]


@pytest.mark.asyncio
async def test_streamed_response_is_incremental(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """Response chunks are relayed as they arrive instead of after the whole body."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())
    chunks: list[bytes] = []
    arrivals: list[float] = []

    async with client.stream("GET", tls_upstream.url("/stream")) as response:
        async for chunk in response.aiter_raw():
            chunks.append(chunk)
            arrivals.append(time.monotonic())

    assert b"".join(chunks) == b"chunk-0\nchunk-1\nchunk-2\n"
    assert arrivals[-1] - arrivals[0] >= 0.15


@pytest.mark.asyncio
async def test_set_cookie_stripped(broker: BrokerFactory, tls_upstream: Upstream, proxy_client: ProxyClient) -> None:
    """Brokered hosts never set cookies in the worker, so sessions cannot outlive the injected credential."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/cookie"))

    assert response.status_code == 200
    assert response.text == "ok"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_missing_secret_returns_403_with_manage_url(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A matched service without a secret in the worker's scope tells the agent where to set one."""
    await broker(single_rule_config(), manage_url=MANAGE_URL)
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.headers["connection"] == "close"
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "message": "No svc key is set for this agent. Ask the user to add one at manage_url, then retry.",
    }
    assert tls_upstream.hits == []
    await assert_audited(
        audit,
        kind="denied",
        status=403,
        service="svc",
        path="/echo",
        code="credential_not_configured",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_info",
    [None, lambda _claims: ScopeInfo(shared=True, managers=("@alice:example.org",), personal_page=True)],
    ids=["personal", "shared-with-managers"],
)
async def test_missing_secret_offers_oauth_connect_link(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    scope_info: Callable[[WorkerClaims], ScopeInfo] | None,
) -> None:
    """A service whose OAuth provider can be connected names it and the link beside the key page.

    A shared agent's refusal offers the link too, instead of naming its managers.
    """
    await broker(
        single_rule_config(),
        resolve_secret=lambda _claims, _service, _routed: SecretMissing(provider="github", connect_url=CONNECT_URL),
        manage_url=MANAGE_URL,
        scope_info=scope_info,
    )
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "provider": "github",
        "connect_url": CONNECT_URL,
        "message": "No svc credential is set for this agent. "
        "Ask the user to connect it at connect_url or add a key at manage_url, then retry.",
    }
    assert "one-time-token" not in str(response.json()["message"])
    assert tls_upstream.hits == []
    await assert_audited(audit, kind="denied", status=403, service="svc", code="credential_not_configured")


@pytest.mark.asyncio
async def test_missing_secret_on_a_shared_agent_names_its_credential_managers(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """On a shared agent the refusal says that only its credential managers can add the key, and names them."""
    asked: list[WorkerClaims] = []

    def shared_scope(claims: WorkerClaims) -> ScopeInfo:
        asked.append(claims)
        return ScopeInfo(shared=True, managers=("@alice:example.org",), personal_page=True)

    await broker(single_rule_config(), manage_url=MANAGE_URL, scope_info=shared_scope)
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo?page=2"))

    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "message": "No svc key is set for this shared agent. "
        "Only its credential managers (@alice:example.org) can add one, at manage_url.",
    }
    assert asked == [DEFAULT_CLAIMS]
    assert tls_upstream.hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            SecretNeedsReconnect(provider="github", connect_url=CONNECT_URL),
            {
                "error": "oauth_connection_required",
                "service": "svc",
                "provider": "github",
                "connect_url": CONNECT_URL,
                "manage_url": MANAGE_URL,
                "message": RECONNECT_MESSAGE,
            },
        ),
        (
            SecretNeedsReconnect(provider="github", reset_required=True),
            {
                "error": "oauth_connection_required",
                "service": "svc",
                "provider": "github",
                "connect_url": None,
                "manage_url": MANAGE_URL,
                "reset_required": True,
                "message": RECONNECT_MESSAGE,
            },
        ),
    ],
)
async def test_oauth_reconnect_required_gets_403_without_reaching_upstream(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    result: SecretNeedsReconnect,
    expected: dict[str, object],
) -> None:
    """A connection that cannot supply a token tells the agent to reconnect, or to reset first, and is audited."""
    await broker(
        single_rule_config(),
        resolve_secret=lambda _claims, _service, _routed: result,
        manage_url=MANAGE_URL,
    )
    client = proxy_client(broker.token())

    with capture_logs() as logs:
        response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == expected
    assert tls_upstream.hits == []
    await assert_audited(
        audit,
        kind="denied",
        status=403,
        service="svc",
        path="/echo",
        code="oauth_connection_required",
    )
    assert "one-time-token" not in repr(logs)


@pytest.mark.asyncio
async def test_oauth_refresh_outage_gets_retryable_503(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A provider outage is a retryable 503 that names the service and provider, with no reconnect prompt."""
    await broker(
        single_rule_config(),
        resolve_secret=lambda _claims, _service, _routed: SecretUnavailable(provider="github"),
        manage_url=MANAGE_URL,
    )
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 503
    assert response.json() == {
        "error": "oauth_refresh_failed",
        "service": "svc",
        "provider": "github",
        "manage_url": MANAGE_URL,
        "message": "github did not refresh the account token. "
        "Retry in a minute; if it keeps failing, ask the user to reconnect at manage_url.",
    }
    assert tls_upstream.hits == []
    await assert_audited(audit, kind="request", status=503, service="svc", path="/echo", code="oauth_refresh_failed")


# The end of a personal scope's refusal where the personal page, the only place that sets its keys, is not served.
_PAGE_NOT_SERVED = (
    "on the Connections page, which nobody can sign in to here. "
    "An administrator can enable Connections sign-in or make the agent shared."
)
_WITHOUT_PAGE = ScopeInfo(shared=False, managers=(), personal_page=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "status", "expected"),
    [
        (
            SecretMissing(),
            403,
            {
                "error": "credential_not_configured",
                "service": "svc",
                "message": "No svc key: each user sets this agent's keys " + _PAGE_NOT_SERVED,
            },
        ),
        (
            SecretMissing(provider="github", connect_url=CONNECT_URL),
            403,
            {
                "error": "credential_not_configured",
                "service": "svc",
                "provider": "github",
                "connect_url": CONNECT_URL,
                "message": "No svc credential is set for this agent. Ask the user to connect it at connect_url, then retry.",
            },
        ),
        (
            SecretNeedsReconnect(provider="github", connect_url=CONNECT_URL),
            403,
            {
                "error": "oauth_connection_required",
                "service": "svc",
                "provider": "github",
                "connect_url": CONNECT_URL,
                "message": "The github account for svc must be connected again. Ask the user to reconnect at connect_url.",
            },
        ),
        (
            SecretNeedsReconnect(provider="github", reset_required=True),
            403,
            {
                "error": "oauth_connection_required",
                "service": "svc",
                "provider": "github",
                "connect_url": None,
                "reset_required": True,
                "message": "Reconnect the github account for svc: each user does it " + _PAGE_NOT_SERVED,
            },
        ),
        (
            SecretUnavailable(provider="github"),
            503,
            {
                "error": "oauth_refresh_failed",
                "service": "svc",
                "provider": "github",
                "message": "github did not refresh the account token. "
                "Retry in a minute; if it keeps failing, tell an administrator.",
            },
        ),
    ],
    ids=["key", "connectable", "reconnect", "reconnect-without-link", "refresh"],
)
async def test_personal_scope_without_the_personal_page_links_no_page(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    result: SecretMissing | SecretNeedsReconnect | SecretUnavailable,
    status: int,
    expected: dict[str, object],
) -> None:
    """Where the personal page is not served, a personal scope's refusal never names the dashboard, which cannot help."""
    await broker(
        single_rule_config(),
        resolve_secret=lambda _claims, _service, _routed: result,
        manage_url="https://m.test/",
        scope_info=lambda _claims: _WITHOUT_PAGE,
    )
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert (response.status_code, response.json()) == (status, expected)
    # The reason phrase is cut at 200 characters and carries each of these messages whole.
    assert response.reason_phrase == expected["message"]
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_user_limit_without_the_personal_page_links_no_page(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A limit from the scope's own services is edited only on the personal page, so its refusal links no page either."""
    operator = EgressBrokerConfig(
        services={"op": EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])},
    )
    limited = EgressService(restrict_only=True, rules=[EgressRule(host="localhost", path_prefix="/echo")])
    await broker(
        rules_provider=lambda _claims: with_user_services(operator, limited=limited),
        secrets={"op": "operator-key"},
        manage_url="https://m.test/",
        scope_info=lambda _claims: _WITHOUT_PAGE,
    )
    client = proxy_client(broker.token())

    refused = await client.get(tls_upstream.url("/ok"))

    assert refused.status_code == 403
    assert refused.json() == {
        "error": "path_not_allowed",
        "service": "limited",
        "message": "localhost/ok is outside the paths limited allows for this agent. "
        "Each user sets this agent's services " + _PAGE_NOT_SERVED,
    }
    assert tls_upstream.hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["resolve_secret", "scope_info"])
async def test_resolve_secret_failure_gets_502_and_audit_row(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    failing: str,
) -> None:
    """A failing secret store or scope callback fails the request with 502, never the tunnel listener."""

    def fail(*_args: object) -> str:
        msg = "store unavailable"
        raise RuntimeError(msg)

    await broker(single_rule_config(), **{failing: fail})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo?page=2"))
    following = await client.get(tls_upstream.url("/echo"))

    assert (response.status_code, response.json()) == (502, BROKER_ERROR)
    assert following.status_code == 502
    assert tls_upstream.hits == []
    records = await audit_records(audit, 2)
    assert {(record.kind, record.status, record.service, record.path) for record in records} == {
        ("request", 502, "svc", "/echo"),
    }
    assert {record.code for record in records} == {"broker_error"}


@pytest.mark.asyncio
async def test_audit_write_failure_is_logged_and_the_request_still_succeeds(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error writing the audit row is logged by type only; the client still gets the upstream's answer."""

    def failing_record(_record: object) -> None:
        msg = "database or disk is full"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(audit, "record", failing_record)
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    with capture_logs() as logs:
        response = await client.get(tls_upstream.url("/ok"))
        following = await client.get(tls_upstream.url("/ok"))

    assert (response.status_code, following.status_code) == (200, 200)
    assert len(tls_upstream.requests) == 2
    failures = [log for log in logs if log["event"] == "egress_broker_audit_write_failed"]
    assert failures
    assert {log["error_type"] for log in failures} == {"OperationalError"}


@pytest.mark.asyncio
async def test_expect_continue_upload(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """The broker answers ``Expect: 100-continue`` itself and then streams the whole body upstream."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    size = 2 * 1024 * 1024
    head = (
        f"POST /upload HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\n"
        f"Content-Length: {size}\r\nExpect: 100-continue\r\n\r\n"
    ).encode()

    interim, final = await _exchange(broker, tls_upstream.port, head, b"x" * size)

    assert interim.status == 100
    assert final.status == 200
    assert final.json() == {"length": size}
    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    assert "expect" not in seen.headers


@pytest.mark.asyncio
async def test_body_over_limit_gets_413(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A declared body over the broker limit is refused before the upstream sees anything."""
    await broker(single_rule_config(), secrets={"svc": SECRET}, max_body_bytes=1024)
    client = proxy_client(broker.token())

    response = await client.post(tls_upstream.url("/upload"), content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_upstream_unreachable_gets_502(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A refused upstream connection inside an intercepted tunnel is a 502 for that request."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(f"https://localhost:{free_port()}/echo")

    assert response.status_code == 502
    assert response.json() == LOCALHOST_UNREACHABLE
    await assert_audited(audit, kind="request", status=502, path="/echo", service="svc", code="upstream_unreachable")


@pytest.mark.asyncio
async def test_blocked_upstream_gets_403(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """The dial guard covers intercepted upstreams: the default policy refuses loopback."""
    await broker(single_rule_config(), secrets={"svc": SECRET}, dial_policy=DialPolicy())
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == LOCALHOST_BLOCKED
    assert tls_upstream.hits == []
    await assert_audited(audit, kind="denied", status=403, code="destination_blocked")


@pytest.mark.asyncio
async def test_websocket_upgrade_spliced_with_injection(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
) -> None:
    """The websocket handshake is injected, then frames pass through untouched in both directions."""
    started = await broker(single_rule_config(), secrets={"svc": SECRET})
    url = tls_upstream.url("/ws").replace("https://", "wss://")

    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(
            url,
            proxy=f"http://127.0.0.1:{started.port}",
            proxy_auth=aiohttp.BasicAuth(broker.token(), ""),
            ssl=ssl.create_default_context(cadata=broker.ca.cert_pem),
        ) as socket_,
    ):
        await socket_.send_str("hello")
        assert await socket_.receive_str() == "hello"
        await socket_.send_bytes(b"\x00\x01")
        assert await socket_.receive_bytes() == b"\x00\x01"

    [seen] = tls_upstream.requests
    assert seen.headers["authorization"] == [f"Bearer {SECRET}"]
    record = await assert_audited(audit, kind="request", status=101, path="/ws", service="svc")
    assert record.bytes_up > 0
    assert record.bytes_down > 0


@pytest.mark.asyncio
async def test_websocket_splice_ends_when_client_closes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    audit: AuditLog,
) -> None:
    """Once the client goes away the upstream is closed too, even though the upstream would keep waiting."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = await open_tunnel(broker, tls_upstream.port)
    client.send(_websocket_handshake(tls_upstream.port))
    switched = await read_raw_response(client.reader)

    client.writer.close()
    with contextlib.suppress(OSError):
        await client.writer.wait_closed()

    assert switched.status == 101
    assert switched.headers["upgrade"].lower() == "websocket"
    # The tunnel is audited only after the splice ends, so the record proves the upstream side was closed.
    await assert_audited(audit, kind="request", status=101)


@pytest.mark.asyncio
async def test_query_auth_type(broker: BrokerFactory, tls_upstream: Upstream, proxy_client: ProxyClient) -> None:
    """Query auth replaces the client's parameter value and keeps the others."""
    await broker(single_rule_config(auth=EgressAuth(type="query", name="key")), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    response = await client.get(tls_upstream.url("/echo?key=client-value&page=2"))

    assert response.json()["query"] == {"key": SECRET, "page": "2"}


@pytest.mark.asyncio
async def test_upstream_connection_is_reused_until_upstream_closes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A tunnel keeps one upstream connection and dials a new one after the upstream answers ``Connection: close``."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    for path in ("/echo", "/echo", "/close", "/echo"):
        response = await client.get(tls_upstream.url(path))
        assert response.status_code == 200
        assert "connection" not in response.headers

    ports = [seen.peer_port for seen in tls_upstream.requests]
    assert ports[0] == ports[1] == ports[2] != ports[3]
    assert {seen.headers["authorization"][0] for seen in tls_upstream.requests} == {f"Bearer {SECRET}"}


@pytest.mark.asyncio
async def test_client_connection_close_ends_tunnel(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """A client's ``Connection: close`` is honored: the broker answers and then closes the tunnel."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    client = await open_tunnel(broker, tls_upstream.port)
    try:
        client.send(f"GET /echo HTTP/1.1\r\nHost: localhost:{tls_upstream.port}\r\nConnection: close\r\n\r\n".encode())
        response = await read_raw_response(client.reader)
        async with asyncio.timeout(5):
            remaining = await client.reader.read()
    finally:
        client.close()

    assert response.status == 200
    assert response.headers["connection"] == "close"
    assert remaining == b""


@pytest.mark.asyncio
async def test_open_tunnel_follows_config_changes(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """Rules are read per request, so a config change applies to tunnels that are already open."""
    configs = [single_rule_config()]
    await broker(rules_provider=lambda _claims: configs[-1], secrets={"svc": SECRET})
    client = proxy_client(broker.token())

    first = await client.get(tls_upstream.url("/echo"))
    configs.append(single_rule_config("api.github.com", unmatched_hosts="deny"))
    second = await client.get(tls_upstream.url("/echo"))

    assert first.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert second.status_code == 403
    assert second.json() == host_not_allowed("svc")
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_rules_come_from_the_requesters_own_config(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    upstream_ca: UpstreamCA,
) -> None:
    """The broker reads rules for each requester's verified claims: only Alice's config intercepts the host."""
    alice = DEFAULT_CLAIMS
    bob = replace(DEFAULT_CLAIMS, worker_key="worker-bob-code", requester_id="@bob:example.org")
    seen: list[str | None] = []

    def per_requester(claims: WorkerClaims) -> EgressBrokerConfig:
        seen.append(claims.requester_id)
        return single_rule_config() if claims == alice else EgressBrokerConfig()

    await broker(rules_provider=per_requester, secrets={"svc": SECRET})
    alice_response = await proxy_client(broker.token(alice)).get(tls_upstream.url("/echo"))
    # Bob's CONNECT is a blind tunnel, so his client sees the upstream's own certificate.
    bob_client = proxy_client(broker.token(bob), verify=upstream_ca.client_context())
    bob_response = await bob_client.get(tls_upstream.url("/echo"))

    assert alice_response.json()["headers"]["authorization"] == [f"Bearer {SECRET}"]
    assert bob_response.status_code == 200
    assert "authorization" not in bob_response.json()["headers"]
    # Alice's CONNECT and her request each read her config; Bob's CONNECT reads his.
    assert seen == [alice.requester_id, alice.requester_id, bob.requester_id]


@pytest.mark.asyncio
async def test_client_rejecting_broker_ca_closes_without_secret_or_denial(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A client that does not trust the broker CA just fails its handshake: no secret lookup, no denied row."""
    started = await broker(single_rule_config(), secrets={"svc": SECRET})
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(connect_request("localhost:443", authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(reader)).status == 200

    with pytest.raises(ssl.SSLCertVerificationError):
        await writer.start_tls(upstream_ca.client_context(), server_hostname="localhost")
    writer.transport.abort()

    assert broker.resolved == []
    # The listener keeps serving, and the request below is the only audited event.
    response = await proxy_client(broker.token()).get(tls_upstream.url("/echo"))
    assert response.status_code == 200
    records = await audit_records(audit, 1)
    assert [(record.kind, record.path) for record in records] == [("request", "/echo")]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
async def test_curl_through_broker(broker: BrokerFactory, tls_upstream: Upstream, tmp_path: Path) -> None:
    """Curl with only the proxy URL and the trust bundle reaches the upstream with the secret injected."""
    started = await broker(single_rule_config(), secrets={"svc": SECRET})
    bundle, _ca_only = materialize_ca_bundle(broker.ca.cert_pem, tmp_path / "trust")

    returncode, stdout, stderr = await run_command(
        "curl",
        "-q",
        "-sS",
        "--fail",
        "--proxy",
        f"http://{broker.token()}:@127.0.0.1:{started.port}",
        "--cacert",
        str(bundle),
        tls_upstream.url("/echo"),
        env=env_without_proxies(),
    )

    assert returncode == 0, stderr
    assert json.loads(stdout)["headers"]["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_git_ls_remote_basic_auth(broker: BrokerFactory, tls_upstream: Upstream, tmp_path: Path) -> None:
    """git, with no credentials configured anywhere, authenticates through the injected Basic header."""
    await broker(single_rule_config(auth=EgressAuth(type="basic", username="x-access-token")), secrets={"svc": SECRET})

    returncode, stdout, stderr = await git_ls_remote(broker, tls_upstream.url("/repo.git"), tmp_path)

    assert returncode == 0, stderr
    assert f"{'1' * 40}\trefs/heads/main" in stdout


@pytest.mark.asyncio
async def test_client_hello_sent_before_connect_answer_still_reaches_tls(
    broker: BrokerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ClientHello that arrives while the broker is still preparing its 200 is handed to the TLS handshake."""
    started = await broker(single_rule_config(), secrets={"svc": SECRET})
    preparing, release = threading.Event(), threading.Event()
    server_context = broker.ca.server_context

    def slow_server_context(host: str) -> ssl.SSLContext:
        preparing.set()
        release.wait(5)
        return server_context(host)

    monkeypatch.setattr(broker.ca, "server_context", slow_server_context)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context(cadata=broker.ca.cert_pem).wrap_bio(
        incoming,
        outgoing,
        server_hostname="localhost",
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    try:
        writer.write(connect_request("localhost:443", authorization=proxy_authorization(broker.token())))
        assert await asyncio.to_thread(preparing.wait, 5)
        with contextlib.suppress(ssl.SSLWantReadError):
            tls.do_handshake()
        writer.write(outgoing.read())
        # Give the broker's event loop the chance to read the early ClientHello before the leaf is ready.
        await asyncio.sleep(0.05)
        release.set()
        async with asyncio.timeout(5):
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    writer.write(outgoing.read())
                    data = await reader.read(65536)
                    assert data
                    incoming.write(data)
        writer.write(outgoing.read())
    finally:
        release.set()
        writer.transport.abort()

    assert ("DNS", "localhost") in tls.getpeercert()["subjectAltName"]


@pytest.mark.asyncio
async def test_secret_with_line_break_gets_502_without_leaking(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A stored secret that is not a valid header value fails the request without reaching logs or the upstream."""
    await broker(
        single_rule_config(),
        resolve_secret=lambda _claims, _service, _routed: Secret(f"{SECRET}\r\nX-Injected: y"),
    )
    client = proxy_client(broker.token())

    with capture_logs() as logs:
        response = await client.get(tls_upstream.url("/echo"))

    assert (response.status_code, response.json()) == (502, BROKER_ERROR)
    assert tls_upstream.hits == []
    await assert_audited(audit, kind="request", status=502, service="svc")
    assert {"event": "egress_broker_secret_unusable", "log_level": "warning", "service": "svc"} in logs
    assert SECRET not in repr(logs)


@pytest.mark.parametrize("trust_env", [None, "SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_default_upstream_context_adds_certifi_unless_trust_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    upstream_ca: UpstreamCA,
    tmp_path: Path,
    trust_env: str | None,
) -> None:
    """Without a configured trust store certifi's roots are added; SSL_CERT_FILE or SSL_CERT_DIR is left to OpenSSL."""
    operator_file = certifi.where()
    stand_in = tmp_path / "certifi.pem"
    stand_in.write_text(upstream_ca.pem)
    monkeypatch.setattr(certifi, "where", lambda: str(stand_in))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    if trust_env == "SSL_CERT_FILE":
        monkeypatch.setenv("SSL_CERT_FILE", operator_file)
    elif trust_env == "SSL_CERT_DIR":
        (tmp_path / "certs").mkdir()
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "certs"))

    loaded = _verifying_context().get_ca_certs(binary_form=True)

    assert (upstream_ca.cert.public_bytes(serialization.Encoding.DER) in loaded) is (trust_env is None)


@pytest.mark.asyncio
async def test_websocket_handshake_forwards_only_connection_upgrade(
    broker: BrokerFactory,
    tls_upstream: Upstream,
) -> None:
    """A websocket handshake reaches the upstream with ``Connection: Upgrade`` instead of the client's value."""
    await broker(single_rule_config(), secrets={"svc": SECRET})
    handshake = _websocket_handshake(tls_upstream.port, connection="keep-alive, Upgrade", upgrade="WebSocket")

    [switched] = await _exchange(broker, tls_upstream.port, handshake)

    assert switched.status == 101
    [seen] = tls_upstream.requests
    assert seen.headers["connection"] == ["Upgrade"]
    assert seen.headers["upgrade"] == ["websocket"]


@pytest.mark.asyncio
async def test_expired_token_stops_open_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """The token is checked on every intercepted request, so a busy tunnel stops working when the token expires."""
    clock = [time.time()]
    await broker(single_rule_config(), secrets={"svc": SECRET})

    with patch("mindroom.egress_broker.tokens.time", SimpleNamespace(time=lambda: clock[0])):
        client = proxy_client(broker.token())
        first = await client.get(tls_upstream.url("/echo"))
        clock[0] += 604800 + 1
        second = await client.get(tls_upstream.url("/echo"))

    assert first.status_code == 200
    assert second.status_code == 407
    assert second.headers["connection"] == "close"
    assert second.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'
    assert second.json() == PROXY_AUTHENTICATION_REQUIRED
    assert tls_upstream.hits == ["/echo"]
    assert broker.resolved == ["svc"]


@pytest.mark.asyncio
async def test_chunked_body_over_limit_in_tunnel_gets_413_and_closes_upstream(
    broker: BrokerFactory,
    upstream_ca: UpstreamCA,
    audit: AuditLog,
    serve: ServeFactory,
    tmp_path: Path,
) -> None:
    """A streamed body crossing the limit gets 413, and the half-sent upstream request's connection is closed."""
    upstream_closed = asyncio.Event()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(65536):
                pass
        except OSError:
            pass
        finally:
            upstream_closed.set()
            writer.transport.abort()

    port = await serve(receive, ssl_context=upstream_ca.server_context(tmp_path))
    await broker(single_rule_config(), secrets={"svc": SECRET}, max_body_bytes=1024)
    client = await open_tunnel(broker, port)
    try:
        client.send(
            f"POST /upload HTTP/1.1\r\nHost: localhost:{port}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
            + b"".join(b"200\r\n" + b"x" * 512 + b"\r\n" for _ in range(4))
            + b"0\r\n\r\n",
        )
        response = await read_raw_response(client.reader)
        async with asyncio.timeout(5):
            await upstream_closed.wait()
    finally:
        client.close()

    assert response.status == 413
    assert response.json() == BODY_TOO_LARGE
    await assert_audited(audit, kind="request", status=413, service="svc")


@pytest.mark.asyncio
async def test_http10_request_without_host_gets_tunnel_authority(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """An HTTP/1.0 request without Host is forwarded with the CONNECT authority as its Host."""
    await broker(single_rule_config(), secrets={"svc": SECRET})

    [response] = await _exchange(broker, tls_upstream.port, b"GET /echo HTTP/1.0\r\n\r\n")

    assert response.status == 200
    seen = response.json()["headers"]
    assert seen["host"] == [f"localhost:{tls_upstream.port}"]
    assert seen["authorization"] == [f"Bearer {SECRET}"]
