"""Durable public-client OAuth grants for the MCP resource."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import stat
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import mindroom.mcp_gateway.oauth as oauth_module
from mindroom.constants import RuntimePaths
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider, _GatewayAuthorizationCode
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from pathlib import Path

pytestmark = pytest.mark.asyncio


@dataclass
class _Clock:
    now: float = 2_000_000_000.0

    def __call__(self) -> float:
        return self.now


def _row_counts(runtime_paths: RuntimePaths) -> dict[str, int]:
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]  # noqa: S608
            for table in ("clients", "pending", "grants", "capabilities")
        }


def _limited_paths(runtime_paths: RuntimePaths, budget: int) -> RuntimePaths:
    return replace(runtime_paths, process_env={"MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES": str(budget)})


def _authorization_params(client: OAuthClientInformationFull, *, state: str = "client-state") -> AuthorizationParams:
    assert client.redirect_uris
    return AuthorizationParams(
        state=state,
        scopes=["mcp:tools"],
        code_challenge="a" * 43,
        redirect_uri=client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
        resource="https://example.org/mcp",
    )


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Runtime paths."""
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
    )


@pytest.fixture
def provider(runtime_paths: RuntimePaths) -> GatewayOAuthProvider:
    """Provider."""
    return GatewayOAuthProvider(runtime_paths, public_url="https://example.org")


@pytest.fixture
def client() -> OAuthClientInformationFull:
    """Client."""
    return OAuthClientInformationFull(
        client_id="desktop",
        client_name="Desktop app",
        redirect_uris=[AnyUrl("https://client.example.org/callback")],
        token_endpoint_auth_method="none",  # noqa: S106
        scope="mcp:tools",
    )


async def pending(provider: GatewayOAuthProvider, client: OAuthClientInformationFull) -> str:
    """Pending."""
    await provider.register_client(client)
    url = await provider.authorize(
        client,
        AuthorizationParams(
            state="client-state",
            scopes=["mcp:tools"],
            code_challenge="a" * 43,
            redirect_uri=client.redirect_uris[0],
            redirect_uri_provided_explicitly=True,
            resource="https://example.org/mcp",
        ),
    )
    assert url.startswith("https://example.org/connections/mcp/authorize?")
    return parse_qs(urlsplit(url).query)["state"][0]


async def issue_code(provider: GatewayOAuthProvider, client: OAuthClientInformationFull) -> _GatewayAuthorizationCode:
    """Issue code."""
    state = await pending(provider, client)
    consent = await provider.begin_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
    )
    callback = await provider.finish_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
        csrf_token=consent.csrf_token,
        allow=True,
    )
    raw_code = parse_qs(urlsplit(callback).query)["code"][0]
    code = await provider.load_authorization_code(client, raw_code)
    assert code is not None
    return code


async def test_code_lookup_does_not_consume_and_exchange_is_one_use(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Code lookup does not consume and exchange is one use."""
    code = await issue_code(provider, client)
    assert await provider.load_authorization_code(client, code.code) == code
    tokens = await provider.exchange_authorization_code(client, code)
    access = await provider.load_access_token(tokens.access_token)
    assert access.requester_id == "@alice:example.org"
    assert access.agent_name == "personal"
    assert access.resource == "https://example.org/mcp"
    assert access.scopes == ["mcp:tools"]
    assert tokens.expires_in == 900
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, code)


async def test_consent_binds_user_agent_csrf_and_is_one_use(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Consent binds user agent csrf and is one use."""
    state = await pending(provider, client)
    consent = await provider.begin_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
    )
    assert consent.client_name == "Desktop app"
    assert consent.redirect_uri == "https://client.example.org/callback"
    for requester, agent, csrf in [
        ("@bob:example.org", "personal", consent.csrf_token),
        ("@alice:example.org", "other", consent.csrf_token),
        ("@alice:example.org", "personal", "wrong"),
    ]:
        with pytest.raises(AuthorizeError):
            await provider.finish_consent(
                state,
                requester_id=requester,
                authenticated_user_id=requester,
                agent_name=agent,
                csrf_token=csrf,
                allow=True,
            )
    with pytest.raises(AuthorizeError):
        await provider.begin_consent(
            state,
            requester_id="@bob:example.org",
            authenticated_user_id="@bob:example.org",
            agent_name="personal",
        )
    callback = await provider.finish_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
        csrf_token=consent.csrf_token,
        allow=False,
    )
    assert parse_qs(urlsplit(callback).query) == {"error": ["access_denied"], "state": ["client-state"]}
    with pytest.raises(AuthorizeError):
        await provider.finish_consent(
            state,
            requester_id="@alice:example.org",
            authenticated_user_id="@alice:example.org",
            agent_name="personal",
            csrf_token=consent.csrf_token,
            allow=True,
        )


async def test_wrong_client_and_tampered_code_binding_rejected(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Wrong client and tampered code binding rejected."""
    code = await issue_code(provider, client)
    other = client.model_copy(update={"client_id": "other"})
    assert await provider.load_authorization_code(other, code.code) is None
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(other, code)
    for field, value in [
        ("agent_name", "other"),
        ("requester_id", "@bob:example.org"),
        ("resource", "https://other.example.org/mcp"),
        ("scopes", ["admin"]),
    ]:
        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(client, code.model_copy(update={field: value}))
    assert (await provider.exchange_authorization_code(client, code)).access_token


@pytest.mark.parametrize("resource", [None, "https://other.example.org/mcp"])
async def test_authorization_rejects_other_or_missing_resource(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    resource: str | None,
) -> None:
    """Authorization rejects other or missing resource."""
    await provider.register_client(client)
    with pytest.raises(AuthorizeError):
        await provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=["mcp:tools"],
                code_challenge="a" * 43,
                redirect_uri=client.redirect_uris[0],
                redirect_uri_provided_explicitly=True,
                resource=resource,
            ),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["authorization_code"]},
        {"scope": "admin"},
        {"client_name": "x" * 257},
        {"redirect_uris": [AnyUrl("http://client.example.org/callback")]},
        {"redirect_uris": [AnyUrl("https://user:password@client.example.org/callback")]},
        {"redirect_uris": [AnyUrl("https://client.example.org/callback#fragment")]},
        {"redirect_uris": [AnyUrl("https://client.example.org/callback#")]},
        {"redirect_uris": [AnyUrl("https://client.example.org/callback")] * 11},
    ],
)
async def test_public_client_metadata_is_bounded(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    updates: dict[str, object],
) -> None:
    """Public client metadata is bounded."""
    with pytest.raises(RegistrationError):
        await provider.register_client(client.model_copy(update=updates))


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765/callback", "http://[::1]:8765/callback", "http://localhost:8765/callback"],
)
async def test_loopback_callback_supported(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    url: str,
) -> None:
    """Loopback callback supported."""
    updated = client.model_copy(update={"redirect_uris": [AnyUrl(url)]})
    await provider.register_client(updated)
    assert (await provider.get_client("desktop")).redirect_uris == [AnyUrl(url)]


async def test_expiry_and_non_sliding_grant_lifetime(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry and non sliding grant lifetime."""
    clock = [2_000_000_000.0]
    monkeypatch.setattr(oauth_module.time, "time", lambda: clock[0])
    code = await issue_code(provider, client)
    tokens = await provider.exchange_authorization_code(client, code)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    clock[0] += 900
    assert await provider.load_access_token(tokens.access_token) is None
    clock[0] = 2_000_000_000 + 2_592_000 - 10
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert rotated.expires_in == 10
    fresh = await provider.load_refresh_token(client, rotated.refresh_token)
    clock[0] += 10
    assert await provider.load_access_token(rotated.access_token) is None
    assert await provider.load_refresh_token(client, rotated.refresh_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, fresh, ["mcp:tools"])


async def test_capability_reads_ignore_writer_reservation_and_exchanges_revalidate(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """A reserved writer cannot block bearer lookups or make exchanges trust a stale read."""
    code = await issue_code(provider, client)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE grants SET revoked = 1")
    lookups = [
        asyncio.create_task(provider.load_authorization_code(client, code.code)),
        asyncio.create_task(provider.load_access_token(tokens.access_token)),
        asyncio.create_task(provider.load_refresh_token(client, tokens.refresh_token)),
    ]
    try:
        _, blocked = await asyncio.wait(lookups, timeout=2)
        assert not blocked, "Capability reads waited for an unrelated writer reservation"
        loaded_code, access, refresh = [task.result() for task in lookups]
        assert loaded_code is not None
        assert access is not None
        assert refresh is not None
        assert access.requester_id == "@alice:example.org"
        writer.execute("COMMIT")
        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(client, loaded_code)
        with pytest.raises(TokenError):
            await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        assert await provider.load_access_token(tokens.access_token) is None
    finally:
        writer.close()
        await asyncio.gather(*lookups, return_exceptions=True)


async def test_store_read_rejects_accidental_mutation(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Read callbacks cannot silently upgrade their transaction into a writer."""
    await provider.register_client(client)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await provider.store.read(lambda connection: connection.execute("DELETE FROM clients"))
    assert await provider.get_client("desktop") is not None


async def test_capability_reads_bound_wait_for_exclusive_writer(
    provider: GatewayOAuthProvider,
    runtime_paths: RuntimePaths,
) -> None:
    """Rollback-journal exclusive locks fail promptly instead of tying up lookup workers."""
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await asyncio.wait_for(provider.load_access_token("unknown"), timeout=2)
    finally:
        writer.close()


async def test_pending_and_code_expire(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending and code expire."""
    clock = [2_000_000_000.0]
    monkeypatch.setattr(oauth_module.time, "time", lambda: clock[0])
    state = await pending(provider, client)
    clock[0] += 600
    with pytest.raises(AuthorizeError):
        await provider.begin_consent(
            state,
            requester_id="@alice:example.org",
            authenticated_user_id="@alice:example.org",
            agent_name="personal",
        )
    code = await issue_code(provider, client)
    clock[0] += 300
    assert await provider.load_authorization_code(client, code.code) is None
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, code)


async def test_parallel_refresh_rotates_once_and_replay_revokes_family(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Parallel refresh rotates once and replay revokes family."""
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    results = await asyncio.gather(
        provider.exchange_refresh_token(client, refresh, ["mcp:tools"]),
        reopened.exchange_refresh_token(client, refresh, ["mcp:tools"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, TokenError) for result in results) == 1
    winner = next(result for result in results if not isinstance(result, Exception))
    assert await provider.load_access_token(winner.access_token) is None
    assert await provider.load_refresh_token(client, winner.refresh_token) is None
    assert await provider.load_access_token(tokens.access_token) is None


async def test_refresh_binding_scope_and_revocation(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Refresh binding scope and revocation."""
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    other = client.model_copy(update={"client_id": "other"})
    assert await provider.load_refresh_token(other, tokens.refresh_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(other, refresh, ["mcp:tools"])
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["admin"])
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh.model_copy(update={"agent_name": "other"}), ["mcp:tools"])
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert await provider.load_access_token(tokens.access_token) is None
    access = await provider.load_access_token(rotated.access_token)
    await provider.revoke_token(access)
    assert await provider.load_access_token(rotated.access_token) is None
    assert await provider.load_refresh_token(client, rotated.refresh_token) is None


async def test_reopen_persists_grants_without_raw_capabilities(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Reopen persists grants without raw capabilities."""
    state = await pending(provider, client)
    consent = await provider.begin_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
    )
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    callback = await reopened.finish_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
        csrf_token=consent.csrf_token,
        allow=True,
    )
    raw_code = parse_qs(urlsplit(callback).query)["code"][0]
    code = await reopened.load_authorization_code(client, raw_code)
    tokens = await reopened.exchange_authorization_code(client, code)
    again = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert (await again.get_client("desktop")).client_name == "Desktop app"
    assert (await again.load_access_token(tokens.access_token)).requester_id == "@alice:example.org"
    assert await again.load_refresh_token(client, tokens.refresh_token) is not None
    changed_origin = GatewayOAuthProvider(runtime_paths, public_url="https://other.example.org")
    assert await changed_origin.load_access_token(tokens.access_token) is None
    db_path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(db_path) as connection:
        contents = "\n".join(connection.iterdump())
    raw_bytes = db_path.read_bytes()
    for secret in [state, consent.csrf_token, raw_code, tokens.access_token, tokens.refresh_token]:
        assert secret not in contents
        assert secret.encode() not in raw_bytes


def _read_access_in_process(runtime_paths: RuntimePaths, token: str, output: Connection) -> None:
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    access = asyncio.run(provider.load_access_token(token))
    output.send(access.requester_id if access else None)
    output.close()


async def test_access_grant_survives_process_restart(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """A fresh process resolves durable bearer identity without memory state."""
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_read_access_in_process, args=(runtime_paths, tokens.access_token, child))
    process.start()
    child.close()
    try:
        await asyncio.to_thread(process.join, 15)
        assert process.exitcode == 0
        assert parent.poll()
        assert parent.recv() == "@alice:example.org"
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
        process.close()


async def test_parallel_code_exchange_creates_one_token_pair(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Concurrent code redemption cannot produce two independently valid grants."""
    code = await issue_code(provider, client)
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    results = await asyncio.gather(
        provider.exchange_authorization_code(client, code),
        reopened.exchange_authorization_code(client, code),
        return_exceptions=True,
    )
    assert sum(isinstance(result, TokenError) for result in results) == 1
    winner = next(result for result in results if not isinstance(result, Exception))
    assert await reopened.load_access_token(winner.access_token) is not None


async def test_refresh_replay_after_new_lookup_revokes_family(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """SDK lookup of a spent refresh token still permits exchange to detect replay."""
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    replay = await provider.load_refresh_token(client, tokens.refresh_token)
    assert replay is not None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, replay, ["mcp:tools"])
    assert await provider.load_access_token(rotated.access_token) is None


async def test_refresh_revocation_does_not_touch_another_grant(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> None:
    """Revoking refresh invalidates its family while another consent remains valid."""
    first = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    second = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, first.refresh_token)
    await provider.revoke_token(refresh)
    await provider.revoke_token(refresh)
    assert await provider.load_access_token(first.access_token) is None
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    assert await provider.load_access_token(second.access_token) is not None


@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.parametrize(
    ("callback", "expected_prefix"),
    [
        (
            "https://client.example.org/callback?flag=&tenant=one",
            "https://client.example.org/callback?flag=&tenant=one&",
        ),
        (
            "https://client.example.org/callback?encoded=%2f%20&bare&repeated=one&repeated=two",
            "https://client.example.org/callback?encoded=%2f%20&bare&repeated=one&repeated=two&",
        ),
        ("https://client.example.org/callback?", "https://client.example.org/callback?"),
        ("https://client.example.org/callback?tenant=one?", "https://client.example.org/callback?tenant=one?&"),
    ],
)
async def test_consent_preserves_registered_callback_query_verbatim(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    allow: bool,
    callback: str,
    expected_prefix: str,
) -> None:
    """Approval and denial append OAuth fields without rewriting callback query bytes."""
    client = client.model_copy(update={"redirect_uris": [AnyUrl(callback)]})
    state = await pending(provider, client)
    consent = await provider.begin_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
    )
    result = await provider.finish_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
        csrf_token=consent.csrf_token,
        allow=allow,
    )
    assert result.startswith(expected_prefix)
    appended = parse_qs(result[len(expected_prefix) :])
    assert appended["state"] == ["client-state"]
    response_query = parse_qs(urlsplit(result).query)
    if allow:
        assert response_query["code"] == appended["code"]
        assert await provider.load_authorization_code(client, appended["code"][0]) is not None
    else:
        assert response_query["error"] == ["access_denied"]
        assert appended == {"error": ["access_denied"], "state": ["client-state"]}


async def test_expired_onboarding_rows_are_collected(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired capabilities cannot accumulate just because lookups reject them."""
    clock = _Clock()
    monkeypatch.setattr(oauth_module.time, "time", clock)
    await pending(provider, client)
    clock.now += 600
    assert await provider.get_client("desktop") is not None
    assert _row_counts(runtime_paths)["pending"] == 0
    clock.now += 86_400
    assert await provider.get_client("desktop") is None
    assert _row_counts(runtime_paths) == {"clients": 0, "pending": 0, "grants": 0, "capabilities": 0}


async def test_retention_keeps_live_clients_and_refresh_replay_history(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Expired bearer authority must retain the live family's replay and revocation history."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    old_refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    rotated = await provider.exchange_refresh_token(client, old_refresh, ["mcp:tools"])
    clock.now += 86_400
    assert await provider.get_client("desktop") is not None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is not None
    assert await provider.load_refresh_token(client, rotated.refresh_token) is not None
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_access_token(rotated.access_token) is None
    assert _row_counts(runtime_paths) == {"clients": 1, "pending": 0, "grants": 1, "capabilities": 2}
    clock.now = 2_000_000_000 + 2_592_000
    assert await provider.get_client("desktop") is None
    assert _row_counts(runtime_paths) == {"clients": 0, "pending": 0, "grants": 0, "capabilities": 0}


async def test_expired_unredeemed_code_releases_its_empty_grant(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Abandoned approved codes cannot pin clients as active for another month."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    code = await issue_code(provider, client)
    clock.now += 300
    assert await provider.load_authorization_code(client, code.code) is None
    assert await provider.get_client("desktop") is not None
    assert _row_counts(runtime_paths) == {"clients": 1, "pending": 0, "grants": 0, "capabilities": 0}


async def test_revoked_family_is_collected_without_deleting_another_live_family(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Cleanup removes revoked token families while another grant remains usable."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    first = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    second = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    await provider.revoke_token(await provider.load_access_token(first.access_token))
    assert await provider.load_access_token(second.access_token) is not None
    assert await provider.get_client("desktop") is not None
    assert _row_counts(runtime_paths)["grants"] == 1
    assert await provider.load_refresh_token(client, first.refresh_token) is None


async def test_registration_quota_serializes_parallel_store_admission(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Concurrent anonymous registration must share one durable aggregate budget."""
    clock = _Clock()
    limited = _limited_paths(runtime_paths, 2048)
    first = GatewayOAuthProvider(limited, public_url="https://example.org", clock=clock)
    second = GatewayOAuthProvider(limited, public_url="https://example.org", clock=clock)
    results = await asyncio.gather(
        first.register_client(client),
        second.register_client(client.model_copy(update={"client_id": "second"})),
        return_exceptions=True,
    )
    assert sum(isinstance(result, GatewayOAuthCapacityError) for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert _row_counts(runtime_paths)["clients"] == 1


async def test_authorization_quota_serializes_parallel_store_admission(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Two anonymous authorization calls cannot both consume the last capacity."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    limited = _limited_paths(runtime_paths, 2048)
    first = GatewayOAuthProvider(limited, public_url="https://example.org", clock=clock)
    second = GatewayOAuthProvider(limited, public_url="https://example.org", clock=clock)
    results = await asyncio.gather(
        first.authorize(client, _authorization_params(client)),
        second.authorize(client, _authorization_params(client)),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, str) and urlsplit(result).path == "/callback"]
    assert len(failures) == 1
    assert parse_qs(urlsplit(failures[0]).query)["error"] == ["temporarily_unavailable"]
    assert (
        sum(isinstance(result, str) and urlsplit(result).path == "/connections/mcp/authorize" for result in results)
        == 1
    )
    assert _row_counts(runtime_paths)["pending"] == 1
    assert await first.load_access_token(tokens.access_token) is not None


async def test_capacity_does_not_block_existing_grant_refresh_or_revoke(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Anonymous onboarding pressure cannot evict active clients or block token operations."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    limited = GatewayOAuthProvider(
        _limited_paths(runtime_paths, 2048),
        public_url="https://example.org",
        clock=clock,
    )
    await limited.authorize(client, _authorization_params(client))
    denied = await limited.authorize(client, _authorization_params(client))
    assert parse_qs(urlsplit(denied).query)["error"] == ["temporarily_unavailable"]
    with pytest.raises(GatewayOAuthCapacityError):
        await limited.register_client(client.model_copy(update={"client_id": "other"}))
    await limited.register_client(client)
    assert await limited.get_client("desktop") is not None
    refresh = await limited.load_refresh_token(client, tokens.refresh_token)
    rotated = await limited.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert await limited.load_access_token(rotated.access_token) is not None
    await limited.revoke_token(await limited.load_refresh_token(client, rotated.refresh_token))
    assert await limited.load_access_token(rotated.access_token) is None
    assert await limited.load_refresh_token(client, rotated.refresh_token) is None


@pytest.mark.parametrize("operation", ["register", "authorize"])
async def test_rejected_admission_commits_expired_record_cleanup(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
    operation: str,
) -> None:
    """The error response must not roll back collection of abandoned pending records."""
    clock = _Clock()
    provider = GatewayOAuthProvider(
        _limited_paths(runtime_paths, 4096),
        public_url="https://example.org",
        clock=clock,
    )
    await pending(provider, client)
    assert _row_counts(runtime_paths)["pending"] == 1
    clock.now += 600
    if operation == "register":
        oversized = client.model_copy(update={"client_id": "other", "software_id": "x" * 5000})
        with pytest.raises(GatewayOAuthCapacityError):
            await provider.register_client(oversized)
    else:
        denied = await provider.authorize(client, _authorization_params(client, state="x" * 2048))
        assert parse_qs(urlsplit(denied).query)["error"] == ["temporarily_unavailable"]
    assert _row_counts(runtime_paths)["pending"] == 0
    assert _row_counts(runtime_paths)["clients"] == 1


async def test_expired_onboarding_releases_capacity_and_repeated_reads_do_not_renew(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Old anonymous registrations expire rather than indefinitely reserving the budget."""
    clock = _Clock()
    provider = GatewayOAuthProvider(
        _limited_paths(runtime_paths, 2048),
        public_url="https://example.org",
        clock=clock,
    )
    await provider.register_client(client)
    clock.now += 86_399
    assert await provider.get_client("desktop") is not None
    await provider.register_client(client)
    clock.now += 1
    other = client.model_copy(update={"client_id": "other"})
    await provider.register_client(other)
    assert await provider.get_client("desktop") is None
    assert await provider.get_client("other") is not None
    assert _row_counts(runtime_paths)["clients"] == 1


async def test_unexpired_consent_can_finish_across_registration_expiry(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """A registration's expiry cannot strand an already-started consent transaction."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    await provider.register_client(client)
    clock.now += 86_399
    state = await pending(provider, client)
    clock.now += 2
    consent = await provider.begin_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
    )
    callback = await provider.finish_consent(
        state,
        requester_id="@alice:example.org",
        authenticated_user_id="@alice:example.org",
        agent_name="personal",
        csrf_token=consent.csrf_token,
        allow=True,
    )
    code = await provider.load_authorization_code(client, parse_qs(urlsplit(callback).query)["code"][0])
    tokens = await provider.exchange_authorization_code(client, code)
    assert await provider.get_client("desktop") is not None
    assert await provider.load_access_token(tokens.access_token) is not None


async def test_legacy_registration_migration_grants_one_durable_grace_period(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Reopening a migrated store must not continually extend abandoned registrations."""
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE clients (client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
        connection.execute("INSERT INTO clients VALUES (?, ?)", (client.client_id, client.model_dump_json()))
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    assert await provider.get_client("desktop") is not None
    clock.now += 86_399
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    assert await reopened.get_client("desktop") is not None
    clock.now += 1
    assert await reopened.get_client("desktop") is None
    assert _row_counts(runtime_paths)["clients"] == 0


@pytest.mark.parametrize(("name", "accepted"), [("a" * 200, True), ("界" * 200, False)])
async def test_onboarding_quota_counts_utf8_bytes(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
    name: str,
    accepted: bool,
) -> None:
    """Equal-length Unicode metadata must consume its actual encoded storage size."""
    provider = GatewayOAuthProvider(
        _limited_paths(runtime_paths, 2048),
        public_url="https://example.org",
        clock=_Clock(),
    )
    candidate = client.model_copy(update={"client_name": name})
    if accepted:
        await provider.register_client(candidate)
        assert await provider.get_client("desktop") is not None
    else:
        with pytest.raises(GatewayOAuthCapacityError):
            await provider.register_client(candidate)
        assert _row_counts(runtime_paths)["clients"] == 0


async def test_repeated_anonymous_fill_and_expiry_reuses_database_pages(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Repeated expired floods remain bounded on disk as well as in live row accounting."""
    clock = _Clock()
    budget = 32_768
    provider = GatewayOAuthProvider(
        _limited_paths(runtime_paths, budget),
        public_url="https://example.org",
        clock=clock,
    )
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    schema_bytes = path.stat().st_size
    for cycle in range(8):
        accepted = 0
        for attempt in range(64):
            candidate = client.model_copy(update={"client_id": f"client-{cycle}-{attempt}"})
            try:
                await provider.register_client(candidate)
            except GatewayOAuthCapacityError:
                break
            accepted += 1
        else:
            pytest.fail("Anonymous registration never reached its byte budget")
        assert accepted > 0
        assert _row_counts(runtime_paths)["clients"] == accepted
        assert path.stat().st_size <= schema_bytes + 4 * budget
        clock.now += 86_400
        assert await provider.get_client("missing") is None
        assert _row_counts(runtime_paths)["clients"] == 0


async def test_legacy_schema_migration_preserves_live_grant_and_refresh_history(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Adding registration expiry to an existing store must preserve usable families."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    path = runtime_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX clients_expiry")
        connection.execute("ALTER TABLE clients DROP COLUMN expires_at")
    migrated = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    clock.now += 2 * 86_400
    assert await migrated.get_client("desktop") is not None
    assert await migrated.load_refresh_token(client, tokens.refresh_token) is not None
    current = await migrated.load_refresh_token(client, rotated.refresh_token)
    fresh = await migrated.exchange_refresh_token(client, current, ["mcp:tools"])
    assert await migrated.load_access_token(fresh.access_token) is not None


async def test_revocation_loaded_before_concurrent_refresh_still_revokes_family(
    client: OAuthClientInformationFull,
    runtime_paths: RuntimePaths,
) -> None:
    """Retention cannot erase revocation authority between SDK lookup and provider dispatch."""
    clock = _Clock()
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org", clock=clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    access = await provider.load_access_token(tokens.access_token)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    await provider.revoke_token(access)
    assert await provider.load_access_token(rotated.access_token) is None
    assert await provider.load_refresh_token(client, rotated.refresh_token) is None
