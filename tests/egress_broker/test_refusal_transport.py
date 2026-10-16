"""Tests for how every broker refusal reaches its client: the code header, the reason phrase, and git's ERR line."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from mindroom.config.egress_broker import EgressAuth, EgressRule, EgressService
from mindroom.egress_broker.secrets import SecretMissing, SecretNeedsReconnect
from tests.egress_broker.conftest import (
    CONNECT_URL,
    MANAGE_URL,
    audit_records,
    git_ls_remote,
    pkt_line,
    proxy_authorization,
    single_rule_config,
    with_user_services,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.egress_broker.audit import AuditLog
    from mindroom.egress_broker.secrets import SecretResult
    from tests.egress_broker.conftest import BrokerFactory, ProxyClient, RawProxy, Upstream

MISSING_KEY = "No svc key is set for this agent. Ask the user to add one at manage_url, then retry."
TLS_REQUIRED = "Credentials are only added over HTTPS. Use https:// for localhost."
GIT_USER_AGENT = {"User-Agent": "git/2.55.0"}


# One service, ``svc``, sending its key as git does: basic auth for ``x-access-token`` on localhost.
_BASIC = EgressAuth(type="basic", username="x-access-token")
_CONFIG = single_rule_config(auth=_BASIC)


def _request(target: str, *, token: str | None, method: str = "GET", git: bool = False) -> bytes:
    """Return a raw `method` request for `target` with Host localhost, optionally authenticated and as git."""
    lines = [f"{method} {target} HTTP/1.1", "Host: localhost"]
    if token is not None:
        lines.append(f"Proxy-Authorization: {proxy_authorization(token)}")
    if git:
        lines.append(f"User-Agent: {GIT_USER_AGENT['User-Agent']}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "with_token", "status", "code"),
    [
        ("http://localhost:1/echo", False, 407, "proxy_authentication_required"),
        ("http://localhost:1/echo", True, 403, "tls_required"),
        ("http://elsewhere.example/x", True, 403, "host_not_allowed"),
        ("ftp://localhost/x", True, 400, "bad_request"),
    ],
)
async def test_plain_refusals_carry_the_code_header_and_the_message_as_reason(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    target: str,
    with_token: bool,
    status: int,
    code: str,
) -> None:
    """Clients that print only the status line show the message; scripts read the code from X-MindRoom-Error."""
    started = await broker(single_rule_config(auth=_BASIC, unmatched_hosts="deny"))

    response = await raw_proxy(started.port, _request(target, token=broker.token() if with_token else None))

    assert response.status == status
    assert response.headers["x-mindroom-error"] == code
    assert response.json()["error"] == code
    assert response.reason == response.json()["message"]
    assert response.headers["content-type"] == "application/json"
    if status == 407:
        assert response.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'


@pytest.mark.asyncio
async def test_tunnel_refusal_carries_the_code_header_and_the_message_as_reason(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A refusal inside an intercepted tunnel has the same header and reason phrase as a plain one."""
    await broker(_CONFIG)

    response = await proxy_client(broker.token()).get(tls_upstream.url("/echo"))

    assert response.status_code == 403
    assert response.headers["x-mindroom-error"] == "credential_not_configured"
    assert response.reason_phrase == MISSING_KEY
    assert response.json()["message"] == MISSING_KEY
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_an_upstream_cannot_pass_its_response_off_as_a_broker_refusal(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    http_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """X-MindRoom-Error is removed from relayed responses, in a tunnel and on plain HTTP, so only refusals carry it."""
    await broker(_CONFIG, secrets={"svc": "s3cret"})
    client = proxy_client(broker.token())

    tunneled = await client.get(tls_upstream.url("/forged-refusal"))
    plain = await client.get(http_upstream.url("/forged-refusal").replace("localhost", "127.0.0.1"))

    for response in (tunneled, plain):
        assert response.status_code == 403
        assert response.text == "upstream says no"
        assert "x-mindroom-error" not in response.headers
    assert tls_upstream.hits == http_upstream.hits == ["/forged-refusal"]


@pytest.mark.asyncio
async def test_reason_phrase_is_printable_ascii_without_line_breaks_and_at_most_200_characters(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
) -> None:
    """A display name with CR, LF, or non-ASCII text never splits the status line; a long message is cut at 200."""
    limit = EgressService(
        restrict_only=True,
        display_name="Evil\r\nX-Injected: yes café",
        rules=[EgressRule(host="localhost", path_prefix="/echo")],
    )
    rules = with_user_services(_CONFIG, limit=limit)
    await broker(rules_provider=lambda _claims: rules)
    long_path = "/other/" + "a" * 300

    response = await proxy_client(broker.token()).get(tls_upstream.url(long_path))

    assert response.status_code == 403
    assert "x-injected" not in response.headers
    message = response.json()["message"]
    assert message.startswith(f"localhost{long_path} is outside the paths Evil\r\nX-Injected: yes café allows")
    expected = message.replace("\r", "").replace("\n", "").replace("é", "?")[:200]
    assert response.reason_phrase == expected
    assert len(response.reason_phrase) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "first_line"),
    [
        ("git-upload-pack", b"001e# service=git-upload-pack\n"),
        ("git-receive-pack", b"001f# service=git-receive-pack\n"),
    ],
)
async def test_git_discovery_refusal_in_a_tunnel_is_an_advertisement_ending_in_err(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    audit: AuditLog,
    service: str,
    first_line: bytes,
) -> None:
    """Git reads `ERR <message>` from a 200 advertisement and prints it; the audit row keeps the refusal code."""
    await broker(_CONFIG)

    response = await proxy_client(broker.token()).get(
        tls_upstream.url(f"/repo.git/info/refs?service={service}"),
        headers=GIT_USER_AGENT,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == f"application/x-{service}-advertisement"
    assert response.headers["x-mindroom-error"] == "credential_not_configured"
    assert response.reason_phrase == MISSING_KEY
    assert response.content == first_line + b"0000" + pkt_line(f"ERR {MISSING_KEY}\n".encode())
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.kind, record.status, record.code, record.path) == (
        "denied",
        403,
        "credential_not_configured",
        "/repo.git/info/refs",
    )


@pytest.mark.asyncio
async def test_plain_git_discovery_refusal_is_an_advertisement_but_the_proxy_challenge_stays_a_407(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
) -> None:
    """Over plain HTTP the refusal takes git's form too, but git's curl must still see the 407 to send credentials."""
    started = await broker(_CONFIG)
    target = "http://localhost:1/repo.git/info/refs?service=git-upload-pack"

    refused = await raw_proxy(started.port, _request(target, token=broker.token(), git=True))
    challenge = await raw_proxy(started.port, _request(target, token=None, git=True))

    assert refused.status == 200
    assert refused.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    assert refused.headers["x-mindroom-error"] == "tls_required"
    assert refused.body == b"001e# service=git-upload-pack\n0000" + pkt_line(f"ERR {TLS_REQUIRED}\n".encode())
    assert challenge.status == 407
    assert challenge.headers["content-type"] == "application/json"
    assert challenge.headers["proxy-authenticate"] == 'Basic realm="mindroom-egress-broker"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/repo.git/info/refs"),
        ("GET", "/repo.git/info/refs?service=git-upload-archive"),
        ("GET", "/repo.git/info/refs?service=git-upload-pack&service=git-receive-pack"),
        ("POST", "/repo.git/info/refs?service=git-upload-pack"),
        ("GET", "/repo.git/git-upload-pack?service=git-upload-pack"),
    ],
)
async def test_other_requests_keep_the_json_refusal(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    method: str,
    path: str,
) -> None:
    """Only a GET for one smart-HTTP discovery service gets git's form; everything else keeps the JSON body."""
    started = await broker(_CONFIG)

    response = await raw_proxy(
        started.port,
        _request(f"http://localhost:1{path}", token=broker.token(), method=method, git=True),
    )

    assert response.status == 403
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"] == "tls_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "git_form"),
    [
        (GIT_USER_AGENT, True),
        ({"Git-Protocol": "version=2"}, True),
        ({"User-Agent": "curl/8.10.1"}, False),
        ({"User-Agent": "python-httpx/0.28 git/2.55.0"}, False),
    ],
    ids=["git-user-agent", "git-protocol-header", "curl", "git-later-in-user-agent"],
)
async def test_only_a_git_client_gets_the_git_form(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    headers: dict[str, str],
    git_form: bool,
) -> None:
    """A discovery URL fetched by anything but git keeps the JSON refusal its client can read."""
    await broker(_CONFIG)

    response = await proxy_client(broker.token()).get(
        tls_upstream.url("/repo.git/info/refs?service=git-upload-pack"),
        headers=headers,
    )

    assert response.headers["x-mindroom-error"] == "credential_not_configured"
    if git_form:
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    else:
        assert response.status_code == 403
        assert response.json()["message"] == MISSING_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "error_line", "json_message"),
    [
        (
            SecretMissing(),
            f"No svc key is set for this agent. Ask the user to add one at {MANAGE_URL}, then retry.",
            MISSING_KEY,
        ),
        (
            SecretMissing(provider="github", connect_url=CONNECT_URL),
            f"No svc credential is set for this agent. Ask the user to connect it at {MANAGE_URL} "
            f"or add a key at {MANAGE_URL}, then retry.",
            "No svc credential is set for this agent. "
            "Ask the user to connect it at connect_url or add a key at manage_url, then retry.",
        ),
        (
            SecretNeedsReconnect(provider="github", connect_url=CONNECT_URL),
            f"The github account for svc must be connected again. Ask the user to reconnect at {MANAGE_URL}.",
            "The github account for svc must be connected again. Ask the user to reconnect at connect_url or manage_url.",
        ),
    ],
    ids=["key-only", "connectable", "reconnect"],
)
async def test_git_error_line_names_the_manage_link_but_never_the_connect_link(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    proxy_client: ProxyClient,
    lookup: SecretResult,
    error_line: str,
    json_message: str,
) -> None:
    """Git shows only the ERR line, so it carries the manage link itself; the token-bearing connect link never."""
    await broker(_CONFIG, resolve_secret=lambda _claims, _service, _routed: lookup, manage_url=MANAGE_URL)
    client = proxy_client(broker.token())
    url = tls_upstream.url("/repo.git/info/refs?service=git-upload-pack")

    git = await client.get(url, headers=GIT_USER_AGENT)
    other = await client.get(url)

    assert git.content.endswith(pkt_line(f"ERR {error_line}\n".encode()))
    assert b"one-time-token" not in git.content
    # The JSON body and the reason phrase keep the field names: their clients read the links from the fields.
    assert git.reason_phrase == json_message[:200]
    assert other.json()["message"] == json_message
    assert other.json()["manage_url"] == MANAGE_URL


@pytest.mark.asyncio
async def test_a_target_that_does_not_parse_as_a_url_keeps_the_json_refusal(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
) -> None:
    """Choosing the refusal's form never fails on a malformed target: it gets the plain 400, not a broker error."""
    started = await broker(_CONFIG)

    response = await raw_proxy(
        started.port,
        _request("http://[::1]localhost/repo.git/info/refs?service=git-upload-pack", token=broker.token(), git=True),
    )

    assert response.status == 400
    assert response.headers["x-mindroom-error"] == "bad_request"
    assert response.json() == {"error": "bad_request", "message": "The request is malformed and was refused."}


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_git_ls_remote_prints_the_refusal_message(
    broker: BrokerFactory,
    tls_upstream: Upstream,
    tmp_path: Path,
    audit: AuditLog,
) -> None:
    """A real git client shows `remote error: <message>` instead of a bare 403 when the broker refuses."""
    await broker(_CONFIG)

    returncode, _stdout, stderr = await git_ls_remote(broker, tls_upstream.url("/repo.git"), tmp_path)

    assert returncode != 0
    assert f"remote error: {MISSING_KEY}" in stderr
    assert tls_upstream.hits == []
    [record] = await audit_records(audit, 1)
    assert (record.status, record.code, record.path) == (403, "credential_not_configured", "/repo.git/info/refs")
