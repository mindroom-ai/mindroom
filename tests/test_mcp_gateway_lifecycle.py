"""Grant activity, ownership and managed-account lifetime boundaries."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizeError, TokenError

from mindroom.mcp_gateway.accounts import GatewayAccounts
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider, _GatewayAuthorizationCode
from tests.test_mcp_gateway_oauth import _Clock, issue_code, pending
from tests.test_mcp_gateway_oauth import client as client  # noqa: PLC0414
from tests.test_mcp_gateway_oauth import runtime_paths as runtime_paths  # noqa: PLC0414
from tests.test_mcp_gateway_oauth_capacity import _copy_legacy

if TYPE_CHECKING:
    from mcp.shared.auth import OAuthClientInformationFull

    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.asyncio
_DAY = 86_400
_OWNER = {
    "requester_id": "@alice:example.org",
    "authenticated_user_id": "@alice:example.org",
    "agent_name": "personal",
}


def _provider(paths: RuntimePaths, clock: _Clock, **settings: str) -> GatewayOAuthProvider:
    return GatewayOAuthProvider(
        replace(paths, process_env=settings),
        public_url="https://example.org",
        clock=clock,
    )


async def test_idle_expiry_rejects_refresh_without_activity(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """A retained refresh cannot revive a grant at its idle deadline."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock, MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS="1")
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    clock.now += _DAY
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])


async def test_refresh_slides_idle_without_moving_absolute_or_last_use(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Refresh updates activity only; listing and clock rollback cannot rejuvenate metadata."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock, MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS="1")
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    original = (await provider.list_grants(**_OWNER))[0]
    assert original["created_at"] == 2_000_000_000
    assert original["last_used_at"] is None
    assert original["idle_expires_at"] == 2_000_086_400
    assert original["expires_at"] == 2_002_592_000
    clock.now += _DAY - 1
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    current = (await provider.list_grants(**_OWNER))[0]
    assert current["idle_expires_at"] == 2_000_172_799
    assert current["expires_at"] == original["expires_at"]
    assert current["last_used_at"] is None
    access = await provider.load_access_token(tokens.access_token)
    assert await provider.record_use(access)
    used = (await provider.list_grants(**_OWNER))[0]
    assert used["last_used_at"] == clock.now
    clock.now -= 10
    assert await provider.record_use(access)
    assert (await provider.list_grants(**_OWNER))[0] == used
    assert not await provider.record_use(access.model_copy(update={"requester_id": "@bob:example.org"}))
    assert (await provider.list_grants(**_OWNER))[0] == used


@pytest.mark.parametrize(
    "settings",
    [
        {"MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS": "0"},
        {"MINDROOM_MCP_OAUTH_GRANT_TTL_DAYS": "181"},
        {"MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS": "31"},
        {"MINDROOM_MCP_OAUTH_GRANT_TTL_DAYS": "366", "MINDROOM_MCP_SCIM_TOKEN": "s" * 32},
        {"MINDROOM_MCP_SCIM_TOKEN": "short"},
    ],
)
async def test_invalid_lifecycle_policy_fails_closed(runtime_paths: RuntimePaths, settings: dict[str, str]) -> None:
    """Unsupported policy cannot silently issue overlong or unmanaged grants."""
    with pytest.raises(ValueError, match="MCP"):
        _provider(runtime_paths, _Clock(), **settings)


async def test_exact_owner_revoke_all_removes_pending_and_codes(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Another original identity, agent or resource cannot list or revoke a connection."""
    provider = _provider(runtime_paths, _Clock())
    code = await issue_code(provider, client)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    state = await pending(provider, client)
    consent = await provider.begin_consent(state, **_OWNER)
    own = await provider.list_grants(**_OWNER)
    assert len(own) == 2
    assert own[0]["client_name"] == "Desktop app"
    assert own[0]["redirect_uri"] == "https://client.example.org/callback"
    for field, value in [
        ("requester_id", "@bob:example.org"),
        ("authenticated_user_id", "@alias:example.org"),
        ("agent_name", "other"),
    ]:
        other = {**_OWNER, field: value}
        assert await provider.list_grants(**other) == []
        assert not await provider.revoke_grants(**other, grant_id=code.grant_id)
        assert await provider.revoke_grants(**other)
        assert len(await provider.list_grants(**_OWNER)) == 2
    assert await provider.revoke_grants(**_OWNER)
    assert await provider.list_grants(**_OWNER) == []
    assert await provider.load_access_token(tokens.access_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, code)

    with pytest.raises(AuthorizeError):
        await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=True, **_OWNER)
    assert await issue_code(provider, client)


async def test_compact_history_keeps_old_replay_and_revocation_authority(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Ordinary refresh retains compact replay evidence and drops only expired access bindings."""
    provider = _provider(runtime_paths, _Clock())
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    first = await provider.load_refresh_token(client, tokens.refresh_token)
    tokens = await provider.exchange_refresh_token(client, first, ["mcp:tools"])
    with sqlite3.connect(provider.store.path) as connection:
        charge = connection.execute(
            "SELECT accounted_bytes FROM capabilities WHERE kind = 'refresh' AND consumed = 1",
        ).fetchone()[0]
    assert charge < 512
    assert await provider.load_refresh_token(client, first.token) == first
    await provider.revoke_token(first)
    assert await provider.load_access_token(tokens.access_token) is None


async def _managed(paths: RuntimePaths, clock: _Clock) -> tuple[GatewayOAuthProvider, GatewayAccounts, str]:

    provider = _provider(paths, clock, MINDROOM_MCP_SCIM_TOKEN="s" * 32)
    directory = GatewayAccounts(provider.store)
    account = await directory.create({"userName": "alice@example.org", "active": True})
    return provider, directory, account["id"]


async def _managed_code(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
    account_id: str,
) -> _GatewayAuthorizationCode:

    state = await pending(provider, client)
    consent = await provider.begin_consent(state, account_id=account_id, **_OWNER)
    callback = await provider.finish_consent(
        state,
        account_id=account_id,
        csrf_token=consent.csrf_token,
        allow=True,
        **_OWNER,
    )
    code = await provider.load_authorization_code(client, parse_qs(urlsplit(callback).query)["code"][0])
    assert code is not None
    return code


async def test_managed_idle_and_absolute_lifetimes(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Day 29 activity permits day 179; neither refresh nor restart moves day 180."""
    clock = _Clock()
    provider, _, account_id = await _managed(runtime_paths, clock)
    code = await _managed_code(provider, client, account_id)
    tokens = await provider.exchange_authorization_code(client, code)
    for day in [29, 58, 87, 116, 145, 174, 179]:
        clock.now = 2_000_000_000 + day * _DAY
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        row = (await provider.list_grants(**_OWNER))[0]
        assert row["expires_at"] == 2_015_552_000
        assert row["last_used_at"] is None
    restarted = _provider(runtime_paths, clock, MINDROOM_MCP_SCIM_TOKEN="s" * 32)
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is not None
    current = await restarted.load_refresh_token(client, tokens.refresh_token)
    clock.now = 2_015_552_000
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is None
    assert await restarted.list_grants(**_OWNER) == []
    with pytest.raises(TokenError):
        await restarted.exchange_refresh_token(client, current, ["mcp:tools"])


async def test_managed_missing_inactive_mismatched_and_disabled_integration(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Status checks occur inside consent and bearer operations and fail closed on integration removal."""
    clock = _Clock()
    provider, directory, account_id = await _managed(runtime_paths, clock)
    state = await pending(provider, client)
    for missing in (None, "unknown"):
        with pytest.raises(AuthorizeError):
            await provider.begin_consent(state, account_id=missing, **_OWNER)
    consent = await provider.begin_consent(state, account_id=account_id, **_OWNER)
    other = await directory.create({"userName": "bob@example.org", "active": True})
    with pytest.raises(AuthorizeError):
        await provider.finish_consent(
            state,
            account_id=other["id"],
            csrf_token=consent.csrf_token,
            allow=True,
            **_OWNER,
        )
    code = await _managed_code(provider, client, account_id)
    tokens = await provider.exchange_authorization_code(client, code)
    access = await provider.load_access_token(tokens.access_token)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    unmanaged = _provider(runtime_paths, clock)
    assert await unmanaged.load_access_token(tokens.access_token) is None
    assert await unmanaged.load_refresh_token(client, tokens.refresh_token) is None
    assert not await unmanaged.record_use(access)
    # Direct status change leaves rows present to exercise checks independently of deletion.
    await provider.store.transact(
        lambda connection: connection.execute(
            "UPDATE gateway_accounts SET active = 0 WHERE account_id = ?",
            (account_id,),
        ),
    )
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    assert not await provider.record_use(access)
    assert await provider.list_grants(**_OWNER) == []
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    with pytest.raises(AuthorizeError):
        await provider.finish_consent(state, account_id=account_id, csrf_token=consent.csrf_token, allow=True, **_OWNER)


async def test_managed_mode_refuses_legacy_unbound_grant(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Enabling provisioning never promotes old unbound authority into managed access."""
    clock = _Clock()
    old = _provider(runtime_paths, clock)
    tokens = await old.exchange_authorization_code(client, await issue_code(old, client))
    managed = _provider(runtime_paths, clock, MINDROOM_MCP_SCIM_TOKEN="s" * 32)
    assert await managed.load_access_token(tokens.access_token) is None
    assert await managed.load_refresh_token(client, tokens.refresh_token) is None


@pytest.mark.timeout(300)
async def test_180_day_quarter_hour_refresh_fits_default_quota_and_retains_replay(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """One ordinary client can refresh every 15 minutes for 180 days under default quotas."""
    clock = _Clock()
    provider, _, account_id = await _managed(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await _managed_code(provider, client, account_id))
    first = await provider.load_refresh_token(client, tokens.refresh_token)
    for quarter_hour in range(1, 180 * 96):
        clock.now = 2_000_000_000 + quarter_hour * 900
        current = await provider.load_refresh_token(client, tokens.refresh_token)
        assert current is not None
        tokens = await provider.exchange_refresh_token(client, current, ["mcp:tools"])
    with sqlite3.connect(provider.store.path) as connection:
        assert connection.execute("SELECT bytes_used FROM requester_usage").fetchone()[0] < 16 * 1024 * 1024
        assert connection.execute("SELECT COUNT(*) FROM capabilities WHERE kind = 'access'").fetchone()[0] == 1
    assert await provider.load_refresh_token(client, first.token) == first
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, first, ["mcp:tools"])
    assert await provider.load_access_token(tokens.access_token) is None


async def test_listing_pages_without_omitting_owned_active_grants(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Pagination stays stable across unrelated, expired and wrong-owner rows."""
    provider = _provider(runtime_paths, _Clock())
    for _ in range(3):
        await issue_code(provider, client)
    first = await provider.list_grants(**_OWNER, limit=2)
    assert len(first) == 2
    second = await provider.list_grants(**_OWNER, after=first[-1]["id"], limit=2)
    assert len(second) == 1
    assert len({row["id"] for row in first + second}) == 3
    assert [row["id"] for row in first + second] == sorted(row["id"] for row in first + second)


async def test_managed_idle_deadline_is_day_30_without_activity(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Listing and rejected authentication never keep an unused managed grant alive."""
    clock = _Clock()
    provider, _, account_id = await _managed(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await _managed_code(provider, client, account_id))
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    original = (await provider.list_grants(**_OWNER))[0]
    clock.now += 29 * _DAY
    assert await provider.load_access_token("invalid") is None
    assert (await provider.list_grants(**_OWNER))[0] == original
    clock.now += _DAY
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])


async def test_legacy_metadata_stays_unknown_and_absolute_expiry_never_extends(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """A real old schema keeps usable bindings and original expiry, including absent account fields."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    paths = replace(runtime_paths, storage_root=runtime_paths.storage_root / "legacy")
    _copy_legacy(provider, paths)
    with sqlite3.connect(paths.storage_root / "mcp_gateway" / "oauth.sqlite3") as connection:
        connection.execute("UPDATE grants SET payload = json_remove(payload, '$.account_id', '$.redirect_uri')")
        connection.execute("UPDATE capabilities SET payload = json_remove(payload, '$.account_id')")
    clock.now += 10
    reopened = _provider(paths, clock)
    row = (await reopened.list_grants(**_OWNER))[0]
    assert row["created_at"] is None
    assert row["last_used_at"] is None
    assert row["redirect_uri"] is None
    assert row["expires_at"] == 2_002_592_000
    assert row["idle_expires_at"] == 2_002_592_000
    clock.now += 29 * _DAY
    refresh = await reopened.load_refresh_token(client, tokens.refresh_token)
    tokens = await reopened.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert (await reopened.list_grants(**_OWNER))[0]["expires_at"] == 2_002_592_000
    restarted = _provider(paths, clock)
    assert await restarted.load_access_token(tokens.access_token) is not None
    clock.now = 2_002_592_000
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is None


async def test_revocation_wins_over_previously_loaded_use_and_refresh(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Stale bearer lookups cannot restore activity or tokens after owner revocation commits."""
    provider = _provider(runtime_paths, _Clock())
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    access = await provider.load_access_token(tokens.access_token)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    assert await provider.revoke_grants(**_OWNER, grant_id=access.grant_id)
    assert not await provider.record_use(access)
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert await provider.list_grants(**_OWNER) == []
