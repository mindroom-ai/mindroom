"""Tests for the egress broker listener: proxy auth, blind tunnels, and plain HTTP forwarding."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker.env import broker_execution_env
from mindroom.egress_broker.tokens import TokenSigner
from mindroom.server_fetch_url import ServerFetchUrlError
from tests.egress_broker.conftest import (
    AMBIGUOUS_PATH,
    BODY_TOO_LARGE,
    BROKER_ERROR,
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
    host_not_allowed,
    plain_request,
    proxy_authorization,
    read_raw_response,
    run_command,
    single_rule_config,
    with_user_services,
)
from tests.egress_broker.raw_http import open_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.proxy import EgressBroker
    from mindroom.egress_broker.tokens import WorkerClaims
    from tests.egress_broker.conftest import BrokerFactory, ProxyClient, RawProxy, ServeFactory, Upstream, UpstreamCA


def _plain_get(upstream: Upstream, path: str, token: str, *, method: str = "GET") -> bytes:
    """Return a raw absolute-form `method` request for `path` on `upstream`, which httpx would normalize."""
    target = f"localhost:{upstream.port}"
    return plain_request(target, path=path, method=method, authorization=proxy_authorization(token))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    ["missing", "garbage", "foreign-key", "expired", "bearer-garbage", "not-base64"],
)
async def test_missing_or_bad_token_gets_407(broker: BrokerFactory, raw_proxy: RawProxy, credential: str) -> None:
    """A missing, unparseable, foreign-signed, or expired token is challenged with Basic auth and the connection closes."""
    started = await broker()
    authorization = {
        "missing": None,
        "garbage": proxy_authorization("mrb1.not.valid"),
        "foreign-key": proxy_authorization(TokenSigner(b"x" * 32).mint(DEFAULT_CLAIMS)),
        "expired": proxy_authorization(broker.signer.mint(DEFAULT_CLAIMS, now=time.time() - 10 * 604800)),
        "bearer-garbage": "Bearer mrb1.not.valid",
        "not-base64": "Basic !!!",
    }[credential]

    response = await raw_proxy(started.port, connect_request("localhost:443", authorization=authorization))

    assert response.status == 407
    assert response.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'
    assert response.headers["connection"] == "close"
    assert response.json() == PROXY_AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_bearer_token_opens_tunnel(broker: BrokerFactory, tls_upstream: Upstream, raw_proxy: RawProxy) -> None:
    """``Proxy-Authorization: Bearer <token>`` is accepted as well as Basic."""
    started = await broker()

    response = await raw_proxy(
        started.port,
        connect_request(f"localhost:{tls_upstream.port}", authorization=f"Bearer {broker.token()}"),
    )

    assert response.status == 200


@pytest.mark.asyncio
async def test_unmatched_host_is_blind_tunnel(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
) -> None:
    """A host without rules is tunnelled untouched: the client verifies the upstream's real certificate."""
    await broker()
    client = proxy_client(broker.token(), verify=upstream_ca.client_context())

    response = await client.get(tls_upstream.url("/echo"))

    assert response.status_code == 200
    echoed = response.json()
    assert echoed["path"] == "/echo"
    assert "proxy-authorization" not in echoed["headers"]
    assert "authorization" not in echoed["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["urllib", "curl"])
async def test_python_urllib_and_curl_authenticate(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
) -> None:
    """Clients configured only with the worker env's proxy URL reach a blind-tunnel host.

    Python's urllib sends proxy credentials only when the URL has both a user name and a password, so it is the
    client that fails if the broker env leaves the password empty.
    """
    started = await broker()
    proxy_url = broker_execution_env(
        broker_url=f"http://127.0.0.1:{started.port}",
        token=broker.token(),
        ca_pem=broker.ca.cert_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )["HTTPS_PROXY"]
    url = tls_upstream.url("/echo")
    if client == "urllib":
        # urllib would skip the proxy for a host this process's own NO_PROXY lists.
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy_url}),
            urllib.request.HTTPSHandler(context=upstream_ca.client_context()),
        )

        def fetch() -> bytes:
            with opener.open(url, timeout=10) as response:
                return response.read()

        body = await asyncio.to_thread(fetch)
    else:
        if shutil.which("curl") is None:
            pytest.skip("curl not installed")
        ca_file = tmp_path / "upstream-ca.pem"
        ca_file.write_text(upstream_ca.pem)
        returncode, body, stderr = await run_command(
            *("curl", "-q", "-sS", "--fail", "--cacert", str(ca_file), url),
            env=env_without_proxies(https_proxy=proxy_url),
        )
        assert returncode == 0, stderr

    echoed = json.loads(body)
    assert echoed["path"] == "/echo"
    assert "proxy-authorization" not in echoed["headers"]


@pytest.mark.asyncio
async def test_deny_ignores_user_rules_on_hosts_the_operator_does_not_name(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    tls_upstream: Upstream,
    http_upstream: Upstream,
) -> None:
    """Under deny a host only a user service names is refused on CONNECT and plain HTTP and is never intercepted."""
    operator = single_rule_config("api.github.com", unmatched_hosts="deny")
    mine = EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])
    started = await broker(
        rules_provider=lambda _claims: with_user_services(operator, mine=mine),
        secrets={"mine": "k"},
    )
    token = broker.token()

    connect = await raw_proxy(
        started.port,
        connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(token)),
    )
    plain = await raw_proxy(started.port, _plain_get(http_upstream, "/echo", token))

    denied = host_not_allowed("svc")
    assert (connect.status, connect.json()) == (403, denied)
    assert (plain.status, plain.json()) == (403, denied)
    assert broker.resolved == []
    assert tls_upstream.hits == []
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_deny_lets_user_rules_work_on_operator_hosts(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """Under deny a user rule on a host the operator names applies, and its more specific path gets the user's key."""
    operator = EgressBrokerConfig(
        unmatched_hosts="deny",
        services={"op": EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])},
    )
    mine = EgressService(rules=[EgressRule(host="localhost", path_prefix="/echo", auth=EgressAuth(type="bearer"))])
    await broker(
        rules_provider=lambda _claims: with_user_services(operator, mine=mine),
        secrets={"op": "operator-key", "mine": "user-key"},
    )

    response = await proxy_client(broker.token()).get(tls_upstream.url("/echo"))

    assert response.json()["headers"]["authorization"] == ["Bearer user-key"]
    assert broker.resolved == ["mine"]


@pytest.mark.asyncio
async def test_operator_restriction_refuses_paths_only_a_user_rule_lists(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A user rule for `/` cannot open a path that a restricting operator service leaves out."""
    operator = EgressBrokerConfig(
        services={
            "op": EgressService(
                restrict_to_rules=True,
                rules=[EgressRule(host="localhost", path_prefix="/ok", auth=EgressAuth(type="bearer"))],
            ),
        },
    )
    mine = EgressService(rules=[EgressRule(host="localhost", auth=EgressAuth(type="bearer"))])
    await broker(
        rules_provider=lambda _claims: with_user_services(operator, mine=mine),
        secrets={"op": "operator-key", "mine": "user-key"},
        manage_url=MANAGE_URL,
    )
    client = proxy_client(broker.token())

    refused = await client.get(tls_upstream.url("/echo"))
    allowed = await client.get(tls_upstream.url("/ok"))

    assert (refused.status_code, refused.json()) == (
        403,
        {
            "error": "path_not_allowed",
            "service": "op",
            "manage_url": MANAGE_URL,
            "message": "localhost/echo is outside the paths op allows for this agent. "
            "Only an administrator can change this, at manage_url.",
        },
    )
    assert allowed.status_code == 200
    assert tls_upstream.hits == ["/ok"]
    assert broker.resolved == ["op"]


@pytest.mark.asyncio
async def test_unmatched_host_denied_under_deny_policy(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """Under ``unmatched_hosts: deny`` a host without rules gets 403 with the configured service names."""
    config = EgressBrokerConfig(
        unmatched_hosts="deny",
        services={
            "github": EgressService(rules=[EgressRule(host="api.github.com", auth=EgressAuth(type="bearer"))]),
            "openai": EgressService(rules=[EgressRule(host="api.openai.com", auth=EgressAuth(type="bearer"))]),
        },
    )
    started = await broker(config)

    response = await raw_proxy(
        started.port,
        connect_request("localhost:443", authorization=proxy_authorization(broker.token())),
    )

    assert response.status == 403
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert response.json() == host_not_allowed("github", "openai")
    await assert_audited(audit, kind="denied", status=403, host="localhost", method="CONNECT", code="host_not_allowed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "host"),
    [("127.0.0.1:8766", "127.0.0.1"), ("localhost:8766", "localhost"), ("[::1]:8766", "::1")],
)
async def test_default_policy_blocks_loopback(
    broker_default_policy: EgressBroker,
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    target: str,
    host: str,
) -> None:
    """The dial guard refuses loopback tunnels, including bracketed IPv6 CONNECT targets."""
    response = await raw_proxy(
        broker_default_policy.port,
        connect_request(target, authorization=proxy_authorization(broker.token())),
    )

    assert response.status == 403
    assert response.json() == {
        "error": "destination_blocked",
        "message": f"{host} resolves to a private or internal address that the administrator's egress policy does not let agents reach.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "localhost",
        "localhost:",
        "localhost:0",
        "localhost:65536",
        "localhost:+443",
        "localhost:https",
        "::1:443",
        "[::1]",
        "[]:443",
        "[a]b]:443",
        "[::1]x:443",
        "[::1]]:443",
    ],
)
async def test_connect_without_valid_port_gets_400(broker: BrokerFactory, raw_proxy: RawProxy, target: str) -> None:
    """A CONNECT target must name a port from 1 to 65535 in digits, and IPv6 literals need one pair of brackets."""
    started = await broker()

    response = await raw_proxy(started.port, connect_request(target, authorization=proxy_authorization(broker.token())))

    assert response.status == 400
    assert response.json() == MALFORMED


@pytest.mark.asyncio
async def test_unresolvable_connect_target_gets_502(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A name that does not resolve is an upstream failure, not a policy block."""
    started = await broker()

    unresolvable = ServerFetchUrlError(reason="dns_resolution_failed")

    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=unresolvable):
        response = await raw_proxy(
            started.port,
            connect_request("nonexistent.invalid:443", authorization=proxy_authorization(broker.token())),
        )

    assert response.status == 502
    assert response.json() == {"error": "upstream_unreachable", "message": "nonexistent.invalid could not be reached."}


_SHARED_WORKER = replace(DEFAULT_CLAIMS, worker_key="worker-shared-code", worker_scope="shared")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stalling", "other"),
    [
        (replace(_SHARED_WORKER, requester_id="@bob:example.org"), _SHARED_WORKER),
        (
            replace(_SHARED_WORKER, requester_id=None),
            replace(_SHARED_WORKER, worker_key="worker-other-code", requester_id=None),
        ),
    ],
    ids=["requesters-of-one-worker", "workers-without-requester"],
)
async def test_stalled_lookups_do_not_delay_another_requester(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    raw_proxy: RawProxy,
    stalling: WorkerClaims,
    other: WorkerClaims,
) -> None:
    """One requester's connections to names whose nameservers never answer leave other requesters connecting.

    Lookups are limited per requester, or per worker when it serves no single requester. A four-thread default
    executor stands in for one these lookups could otherwise fill.
    """
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=4))
    rule_reads: list[str | None] = []
    stalled: list[str] = []
    answer = threading.Event()

    def rules_provider(claims: WorkerClaims) -> EgressBrokerConfig:
        rule_reads.append(claims.requester_id)
        return EgressBrokerConfig()

    def lookup(host: str, **_policy: object) -> list[ipaddress.IPv4Address]:
        if host == "localhost":
            return [ipaddress.IPv4Address("127.0.0.1")]
        stalled.append(host)
        answer.wait()
        raise ServerFetchUrlError(reason="dns_resolution_failed")

    started = await broker(rules_provider=rules_provider)
    # More connections than the broker has lookup threads.
    connections = 20
    clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    authorization = proxy_authorization(broker.token(stalling))
    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=lookup):
        try:
            for index in range(connections):
                reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
                writer.write(connect_request(f"{index}.stalled.example:443", authorization=authorization))
                clients.append((reader, writer))
            async with asyncio.timeout(10):
                while len(rule_reads) < connections or len(stalled) < 4:  # noqa: ASYNC110 - the broker exposes no event for this
                    await asyncio.sleep(0.01)
                response = await raw_proxy(
                    started.port,
                    connect_request(
                        f"localhost:{tls_upstream.port}",
                        authorization=proxy_authorization(broker.token(other)),
                    ),
                )
            # The requester's other connections wait for one of its own lookups to end.
            assert len(stalled) == 4
            answer.set()
            async with asyncio.timeout(10):
                for reader, _writer in clients:
                    assert (await read_raw_response(reader)).status == 502
        finally:
            answer.set()
            for _reader, writer in clients:
                writer.transport.abort()

    assert response.status == 200


@pytest.mark.asyncio
async def test_slow_request_head_is_dropped(broker: BrokerFactory) -> None:
    """A client that never finishes its request head is disconnected after the head deadline."""
    started = await broker(head_timeout=0.2)
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(b"CONNECT localhost:443 HTTP/1.1\r\nHost: localhost:443\r\n")
    await writer.drain()

    async with asyncio.timeout(5):
        remaining = await reader.read()

    writer.transport.abort()
    assert remaining == b""


@pytest.mark.asyncio
async def test_plain_http_matched_rule_requires_tls(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A plain request a rule matches is refused, so a secret never crosses an unencrypted connection."""
    await broker(single_rule_config("localhost"), secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"))

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert response.json() == {
        "error": "tls_required",
        "service": "svc",
        "message": "Credentials are only added over HTTPS. Use https:// for localhost.",
    }
    assert http_upstream.hits == []
    await assert_audited(audit, kind="denied", service="svc", status=403, path="/echo", code="tls_required")


@pytest.mark.asyncio
async def test_plain_http_unmatched_path_on_rule_host_forwards_unmodified(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A path no rule matches is forwarded by URL without credentials, and the Host header cannot reroute it."""
    await broker(single_rule_config("localhost", path_prefix="/private"), secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"), headers={"Host": "evil.example"})

    assert response.status_code == 200
    echoed = response.json()
    assert "authorization" not in echoed["headers"]
    assert "proxy-authorization" not in echoed["headers"]
    assert echoed["headers"]["host"] == [f"localhost:{http_upstream.port}"]
    assert echoed["query"] == {"page": "2"}
    await assert_audited(
        audit,
        kind="request",
        service=None,
        status=200,
        path="/echo",
        code=None,
        scope="user_agent",
        agent_name="code",
        requester_id="@alice:example.org",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["/private/../echo", "/private/%2E%2e/echo", "/private/%252e%252e/echo", "/private//echo", "/private\\echo"],
)
async def test_plain_http_ambiguous_path_on_rule_host_gets_400(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
    target: str,
) -> None:
    """Plain requests use the same route decision as tunnels: an ambiguous path on a rule host is refused."""
    started = await broker(single_rule_config("localhost", path_prefix="/private"), secrets={"svc": "s3cret"})

    response = await raw_proxy(started.port, _plain_get(http_upstream, f"{target}?page=2", broker.token()))

    assert response.status == 400
    assert response.json() == AMBIGUOUS_PATH
    assert http_upstream.hits == []
    assert broker.resolved == []
    await assert_audited(audit, kind="denied", status=400, path=target, service=None, code="bad_request")


@pytest.mark.asyncio
async def test_plain_http_ambiguous_path_on_host_without_rules_is_forwarded(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """Hosts without rules are untouched: the broker forwards their paths as sent and leaves them to the upstream."""
    started = await broker(single_rule_config("api.github.com", path_prefix="/private"))

    response = await raw_proxy(started.port, _plain_get(http_upstream, "/private/../echo", broker.token()))

    # The fake upstream does not resolve dot segments, so its 404 shows the path arrived exactly as sent.
    assert response.status == 404
    assert http_upstream.hits == ["/private/../echo"]
    # The upstream's own 404 is a forwarded answer, not a refusal.
    await assert_audited(audit, kind="request", status=404, path="/private/../echo", code=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["TRACE", "TRACK", "trace"])
async def test_plain_http_trace_and_track_on_rule_host_are_refused(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
    method: str,
) -> None:
    """TRACE and TRACK get 405 on a rule host before the rule match, any secret lookup, or an upstream dial."""
    started = await broker(single_rule_config("localhost"), secrets={"svc": "s3cret"})
    response = await raw_proxy(started.port, _plain_get(http_upstream, "/echo", broker.token(), method=method))

    assert response.status == 405
    assert response.headers["x-mindroom-error"] == "method_not_allowed"
    assert response.json() == METHOD_NOT_ALLOWED
    assert http_upstream.hits == []
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
async def test_plain_http_other_methods_on_rule_host_are_forwarded(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
) -> None:
    """Only TRACE and TRACK are refused on a rule host: DELETE to an unmatched path is forwarded as before."""
    started = await broker(single_rule_config("localhost", path_prefix="/private"))
    response = await raw_proxy(started.port, _plain_get(http_upstream, "/echo", broker.token(), method="DELETE"))

    assert (response.status, response.json()["method"]) == (200, "DELETE")
    assert http_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_plain_http_trace_on_host_without_rules_is_forwarded(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """Hosts without rules are untouched: nothing is injected there, so TRACE is forwarded like any method."""
    started = await broker(single_rule_config("api.github.com"))
    response = await raw_proxy(started.port, _plain_get(http_upstream, "/echo", broker.token(), method="TRACE"))

    assert (response.status, response.json()["method"]) == (200, "TRACE")
    assert http_upstream.hits == ["/echo"]
    await assert_audited(audit, kind="request", method="TRACE", status=200, path="/echo", code=None)


@pytest.mark.asyncio
async def test_plain_http_restricted_host_refuses_unlisted_path(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """An unlisted path on a restricted host is refused over plain HTTP too, not forwarded without credentials."""
    await broker(single_rule_config(path_prefix="/private", restrict_to_rules=True), manage_url=MANAGE_URL)
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo?page=2"))

    assert response.status_code == 403
    assert response.json() == {
        "error": "path_not_allowed",
        "service": "svc",
        "manage_url": MANAGE_URL,
        "message": "localhost/echo is outside the paths svc allows for this agent. "
        "Only an administrator can change this, at manage_url.",
    }
    assert http_upstream.hits == []
    await assert_audited(audit, kind="denied", status=403, path="/echo", service="svc", code="path_not_allowed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "cookie_kept"),
    [(None, True), (single_rule_config("localhost", path_prefix="/private"), False)],
    ids=["unmatched_host", "rule_host"],
)
async def test_plain_http_set_cookie_stripped_only_for_rule_hosts(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
    config: EgressBrokerConfig | None,
    cookie_kept: bool,
) -> None:
    """Hosts with rules lose Set-Cookie so sessions stay brokered; other hosts keep their cookies."""
    await broker(config)
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/cookie"))

    assert response.status_code == 200
    assert ("set-cookie" in response.headers) is cookie_kept


@pytest.mark.asyncio
async def test_plain_http_denied_under_deny_policy(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """``unmatched_hosts: deny`` applies to plain requests too."""
    await broker(single_rule_config("api.github.com", unmatched_hosts="deny"))
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == host_not_allowed("svc")
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_blocked_destination(
    broker_default_policy: EgressBroker,
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """The dial guard covers plain requests: the default policy refuses loopback."""
    assert broker.latest is broker_default_policy
    client = proxy_client(broker.token())

    response = await client.get(http_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.json() == LOCALHOST_BLOCKED
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_response_content_length_beside_chunked_is_reframed(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
    serve: ServeFactory,
) -> None:
    """An upstream response with both framings reaches the client whole, not cut at the Content-Length."""

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
        )
        await writer.drain()
        writer.close()

    port = await serve(respond)
    await broker()
    client = proxy_client(broker.token())

    response = await client.get(f"http://localhost:{port}/")

    assert response.status_code == 200
    assert response.text == "hello"
    assert "content-length" not in response.headers


@pytest.mark.asyncio
async def test_plain_http_keepalive_reauthenticates_each_request(
    broker: BrokerFactory,
    http_upstream: Upstream,
) -> None:
    """Every request on a kept-alive connection must carry a valid token, not just the first."""
    started = await broker()
    target = f"localhost:{http_upstream.port}"
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    try:
        writer.write(plain_request(target, path="/echo", authorization=proxy_authorization(broker.token())))
        first = await read_raw_response(reader)
        writer.write(plain_request(target, path="/echo", authorization=proxy_authorization("mrb1.not.valid")))
        second = await read_raw_response(reader)
    finally:
        writer.transport.abort()

    assert first.status == 200
    assert "connection" not in first.headers
    assert second.status == 407
    assert http_upstream.hits == ["/echo"]


@pytest.mark.asyncio
async def test_plain_http_streamed_body_over_limit_gets_413(
    broker: BrokerFactory,
    http_upstream: Upstream,
    raw_proxy: RawProxy,
    audit: AuditLog,
) -> None:
    """A chunked body that crosses the limit mid-stream is cut off with 413."""
    started = await broker(max_body_bytes=1024)
    chunk = b"x" * 512
    request = (
        (
            f"POST {http_upstream.url('/upload')} HTTP/1.1\r\n"
            f"Host: localhost:{http_upstream.port}\r\n"
            f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
        ).encode()
        + b"".join(b"200\r\n" + chunk + b"\r\n" for _ in range(4))
        + b"0\r\n\r\n"
    )

    response = await raw_proxy(started.port, request)

    assert response.status == 413
    assert response.json() == BODY_TOO_LARGE
    await assert_audited(audit, kind="request", status=413, code="request_body_too_large")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    ["connect", "plain"],
)
async def test_rules_provider_failure_gets_502_and_listener_survives(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    audit: AuditLog,
    transport: str,
) -> None:
    """A failing rule provider fails the request with 502 broker_error and an audit row, not the listener."""

    def failing_provider(_claims: WorkerClaims) -> EgressBrokerConfig:
        msg = "config unavailable"
        raise RuntimeError(msg)

    started = await broker(rules_provider=failing_provider)
    authorization = proxy_authorization(broker.token())
    request = (
        connect_request("localhost:443", authorization=authorization)
        if transport == "connect"
        else plain_request("localhost:1", path="/echo", authorization=authorization)
    )

    first = await raw_proxy(started.port, request)
    second = await raw_proxy(started.port, request)

    assert (first.status, first.json()) == (502, BROKER_ERROR)
    assert (second.status, second.json()) == (502, BROKER_ERROR)
    records = await audit_records(audit, 2)
    assert {(record.kind, record.status) for record in records} == {
        ("tunnel" if transport == "connect" else "request", 502),
    }


@pytest.mark.asyncio
async def test_plain_http_chunked_upload_is_streamed(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A chunked request body is re-framed for the upstream and arrives whole."""
    await broker()
    client = proxy_client(broker.token())

    async def body() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 100_000

    response = await client.post(http_upstream.url("/upload"), content=body())

    assert response.status_code == 200
    assert response.json() == {"length": 400_000}


@pytest.mark.asyncio
async def test_plain_http_body_over_limit_gets_413(
    broker: BrokerFactory,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A declared body over the broker limit is refused before the upstream is dialed."""
    await broker(max_body_bytes=1024)
    client = proxy_client(broker.token())

    response = await client.post(http_upstream.url("/upload"), content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json() == BODY_TOO_LARGE
    assert http_upstream.hits == []


@pytest.mark.asyncio
async def test_plain_http_unreachable_upstream_gets_502(
    broker: BrokerFactory,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A refused upstream connection is reported as 502 and audited."""
    await broker()
    client = proxy_client(broker.token())

    response = await client.get(f"http://localhost:{free_port()}/echo")

    assert response.status_code == 502
    assert response.json() == LOCALHOST_UNREACHABLE
    await assert_audited(audit, kind="request", status=502, path="/echo", code="upstream_unreachable")


@pytest.mark.asyncio
async def test_tunnel_audited_without_query(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    upstream_ca: UpstreamCA,
    proxy_client: ProxyClient,
    audit: AuditLog,
) -> None:
    """A closed blind tunnel leaves one metadata record that never contains the request path or query."""
    await broker()
    client = proxy_client(broker.token(), verify=upstream_ca.client_context())
    assert (await client.get(tls_upstream.url("/echo?token=abc"))).status_code == 200
    await client.aclose()

    record = await assert_audited(
        audit,
        kind="tunnel",
        host="localhost",
        path="",
        method="CONNECT",
        status=200,
        service=None,
        scope="user_agent",
    )

    assert record.bytes_up > 0
    assert record.bytes_down > 0


@pytest.mark.asyncio
async def test_malformed_request_does_not_break_listener(broker: BrokerFactory, raw_proxy: RawProxy) -> None:
    """A client sending garbage gets 400 and the listener keeps serving others."""
    started = await broker()

    malformed = await raw_proxy(started.port, b"\x16\x03\x01 not http\r\n\r\n")
    following = await raw_proxy(started.port, connect_request("localhost:443"))

    assert (malformed.status, malformed.json()) == (400, MALFORMED)
    assert following.status == 407


@pytest.mark.asyncio
async def test_close_stops_listener(broker: BrokerFactory) -> None:
    """Close drops in-flight connections, stops accepting, and is idempotent."""
    started = await broker()
    port = started.port
    client = await open_client(port)
    client.send(b"CONNECT localhost:443 HTTP/1.1\r\n")
    await client.writer.drain()

    await started.close()

    assert await client.closed() is True
    client.close()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)
    await started.close()


@pytest.mark.asyncio
async def test_close_drops_established_tunnel(broker: BrokerFactory, tls_upstream: Upstream) -> None:
    """Close also ends tunnels that are already relaying."""
    started = await broker()
    client = await open_client(started.port)
    client.send(connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(broker.token())))
    assert (await read_raw_response(client.reader)).status == 200

    await started.close()

    closed = await client.closed()
    client.close()
    assert closed is True
