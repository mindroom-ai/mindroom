"""Storage and issuance admission preserves durable OAuth authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import TokenError

import mindroom.mcp_gateway.store as store_module
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError
from tests.test_mcp_gateway_oauth import _Clock, issue_code, pending
from tests.test_mcp_gateway_oauth import client as client  # noqa: PLC0414
from tests.test_mcp_gateway_oauth import runtime_paths as runtime_paths  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.shared.auth import OAuthClientInformationFull

    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.asyncio


def _provider(paths: RuntimePaths, clock: _Clock, **limits: int) -> GatewayOAuthProvider:
    return GatewayOAuthProvider(
        replace(paths, process_env={f"MINDROOM_MCP_OAUTH_{key}": str(value) for key, value in limits.items()}),
        public_url="https://example.org",
        clock=clock,
    )


async def test_issuance_burst_survives_restart_and_rolls_back_current_refresh(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Initial issuance counts; a rejected seventh issuance leaves the token usable at the boundary."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    for _ in range(5):
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    assert refresh is not None
    provider = _provider(runtime_paths, clock)
    for elapsed in (0, 59.999):
        clock.now = 2_000_000_000 + elapsed
        with pytest.raises(GatewayOAuthCapacityError):
            await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    clock.now = 2_000_000_060
    rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert await provider.load_access_token(rotated.access_token) is not None


@pytest.mark.parametrize("setting", ["MAX_BYTES", "USER_MAX_BYTES"])
@pytest.mark.parametrize("issuances", [2, 6])
async def test_refresh_quota_rolls_back_but_consumed_replay_revokes(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
    setting: str,
    issuances: int,
) -> None:
    """Storage rejection preserves current refresh; old-token replay revokes even over budget."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    old = await provider.load_refresh_token(client, tokens.refresh_token)
    assert old is not None
    rotated = await provider.exchange_refresh_token(client, old, ["mcp:tools"])
    for _ in range(issuances - 2):
        refresh = await provider.load_refresh_token(client, rotated.refresh_token)
        assert refresh is not None
        rotated = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    current = await provider.load_refresh_token(client, rotated.refresh_token)
    assert current is not None
    limited = _provider(runtime_paths, clock, **{setting: 1})
    with pytest.raises(GatewayOAuthCapacityError):
        await limited.exchange_refresh_token(client, current, ["mcp:tools"])
    assert await provider.load_access_token(rotated.access_token) is not None
    with pytest.raises(TokenError):
        await limited.exchange_refresh_token(client, old, ["mcp:tools"])
    assert await provider.load_access_token(rotated.access_token) is None


async def test_consent_binding_capacity_preserves_unbound_pending(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Identity and nonce updates cannot silently bypass the global budget."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    state = await pending(provider, client)
    limited = _provider(runtime_paths, clock, MAX_BYTES=1)
    identity = {
        "requester_id": "@alice:example.org",
        "authenticated_user_id": "@alice:example.org",
        "agent_name": "personal",
    }
    with pytest.raises(GatewayOAuthCapacityError):
        await limited.begin_consent(state, **identity)
    consent = await provider.begin_consent(state, **identity)
    with pytest.raises(GatewayOAuthCapacityError):
        await limited.begin_consent(state, **identity)
    callback = await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=True, **identity)
    assert "code=" in callback


@pytest.mark.parametrize("setting", ["MAX_BYTES", "USER_MAX_BYTES"])
@pytest.mark.parametrize("value", [0, -1])
async def test_storage_budgets_must_be_positive(runtime_paths: RuntimePaths, setting: str, value: int) -> None:
    """Invalid configured budgets fail closed at startup."""
    with pytest.raises(ValueError, match="positive integer"):
        _provider(runtime_paths, _Clock(), **{setting: value})


def _reconcile(provider: GatewayOAuthProvider) -> tuple[int, dict[str, int]]:
    """Independently sum persisted fields to catch missed trigger deltas or byte charges."""
    with sqlite3.connect(provider.store.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            table: connection.execute("SELECT * FROM " + table).fetchall()  # noqa: S608
            for table in ("clients", "pending", "grants", "capabilities")
        }
        total = 1024
        for table, entries in rows.items():
            for row in entries:
                overhead = 2048 if table == "grants" else 1024
                if table == "capabilities" and row["kind"] == "refresh" and row["consumed"] and row["payload"] == "{}":
                    overhead = 256
                charge = overhead + sum(len(value.encode("utf-8")) for value in row if isinstance(value, str))
                if table == "grants":
                    charge += len(row["requester_id"].encode("utf-8"))
                assert row["accounted_bytes"] == charge
                total += charge
        clients = {row["client_id"]: row["accounted_bytes"] for row in rows["clients"]}
        users: dict[str, int] = {}
        owners = {}
        pinned = set()
        for row in rows["grants"]:
            payload = json.loads(row["payload"])
            owner = payload["requester_id"]
            assert row["requester_id"] == owner
            owners[row["grant_id"]] = owner
            pinned.add(payload["client_id"])
            charge = row["accounted_bytes"] + clients[payload["client_id"]]
            assert row["requester_charge"] == charge
            users[owner] = users.get(owner, 0) + charge
        for row in rows["capabilities"]:
            owner = owners[row["grant_id"]]
            users[owner] += row["accounted_bytes"]
        usage = connection.execute("SELECT * FROM oauth_usage").fetchone()
        assert usage["bytes_used"] == total
        assert usage["onboarding_bytes"] == sum(row["accounted_bytes"] for row in rows["pending"]) + sum(
            charge for client_id, charge in clients.items() if client_id not in pinned
        )
        assert dict(connection.execute("SELECT requester_id, bytes_used FROM requester_usage")) == users
        return total, users


async def _approve_for(provider: GatewayOAuthProvider, client: OAuthClientInformationFull, requester: str) -> str:
    state = await pending(provider, client)
    identity = {"requester_id": requester, "authenticated_user_id": requester, "agent_name": "personal"}
    consent = await provider.begin_consent(state, **identity)
    return await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=True, **identity)


async def test_repeated_grants_charge_shared_client_per_requester_and_preserve_rejected_consent(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """A user cannot bypass the durable budget by opening new grant families."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock, USER_MAX_BYTES=10_000)
    first = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    await provider.get_client("desktop")
    _, before = _reconcile(provider)
    state = await pending(provider, client)
    identity = {
        "requester_id": "@alice:example.org",
        "authenticated_user_id": "@alice:example.org",
        "agent_name": "personal",
    }
    consent = await provider.begin_consent(state, **identity)
    for _ in range(3):
        with pytest.raises(GatewayOAuthCapacityError):
            await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=True, **identity)
        assert _reconcile(provider)[1] == before
    callback = await _approve_for(provider, client, "@bob:example.org")
    bob_code = await provider.load_authorization_code(client, parse_qs(urlsplit(callback).query)["code"][0])
    assert bob_code is not None
    bob = await provider.exchange_authorization_code(client, bob_code)
    await provider.get_client("desktop")
    _, users = _reconcile(provider)
    assert len(users) == 2
    assert users["@alice:example.org"] == before["@alice:example.org"]
    access = await provider.load_access_token(first.access_token)
    assert access is not None
    await provider.revoke_token(access)
    assert set(_reconcile(provider)[1]) == {"@bob:example.org"}
    # The original nonce survives repeated grant rejection and works after quota release.
    assert "code=" in await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=True, **identity)
    assert await provider.load_access_token(bob.access_token) is not None
    _reconcile(provider)
    clock.now += 2_592_000
    await provider.get_client("missing")
    assert _reconcile(provider) == (1024, {})


@pytest.mark.parametrize("setting", ["MAX_BYTES", "USER_MAX_BYTES"])
async def test_repeated_refresh_stops_at_byte_budget_and_reclamation_commits(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
    setting: str,
) -> None:
    """Slow refreshes cannot grow history indefinitely; rejected writes still collect expired consent."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock, **{setting: 20_000})
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    for _ in range(100):
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        clock.now += 900
        try:
            tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
        except GatewayOAuthCapacityError:
            break
    else:
        pytest.fail("Retained refresh history never reached its byte budget")
    total, users = _reconcile(provider)
    assert (total if setting == "MAX_BYTES" else users["@alice:example.org"]) <= 20_000
    unlimited = _provider(runtime_paths, clock)
    await pending(unlimited, client)
    clock.now += 600
    with pytest.raises(GatewayOAuthCapacityError):
        await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    with sqlite3.connect(provider.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT consumed FROM capabilities WHERE token_hash = ?",
                (hashlib.sha256(refresh.token.encode()).hexdigest(),),
            ).fetchone()[0]
            == 0
        )
    _reconcile(provider)
    rotated = await unlimited.exchange_refresh_token(client, refresh, ["mcp:tools"])
    assert await unlimited.load_access_token(rotated.access_token) is not None


async def test_pending_identity_counts_utf8_and_nonce_rotation_has_constant_charge(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Every added pending string counts, including both identities and the nonce digest."""
    provider = _provider(runtime_paths, _Clock())
    state = await pending(provider, client)
    initial, _ = _reconcile(provider)
    identity = {"requester_id": "界", "authenticated_user_id": "界界", "agent_name": "界界界"}
    await provider.begin_consent(state, **identity)
    assert _reconcile(provider)[0] == initial + 3 + 6 + 9 + 64
    await provider.begin_consent(state, **identity)
    assert _reconcile(provider)[0] == initial + 82


async def test_parallel_refresh_admits_exactly_one_pair_at_exact_global_budget(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Separate store instances cannot race the last pair's shared byte allowance."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    first = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    second = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    await provider.get_client("desktop")
    total, _ = _reconcile(provider)
    one = await provider.load_refresh_token(client, first.refresh_token)
    two = await provider.load_refresh_token(client, second.refresh_token)
    assert one is not None
    assert two is not None
    with sqlite3.connect(provider.store.path) as connection:
        access_bytes = connection.execute(
            "SELECT accounted_bytes FROM capabilities WHERE grant_id = ? AND kind = 'access'",
            (one.grant_id,),
        ).fetchone()[0]
        pair_bytes = access_bytes + 256 + 64 + len(one.grant_id) + len("refresh") + 2
    first_store = _provider(runtime_paths, clock, MAX_BYTES=total + pair_bytes)
    second_store = _provider(runtime_paths, clock, MAX_BYTES=total + pair_bytes)
    results = await asyncio.gather(
        first_store.exchange_refresh_token(client, one, ["mcp:tools"]),
        second_store.exchange_refresh_token(client, two, ["mcp:tools"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, GatewayOAuthCapacityError) for result in results) == 1
    assert _reconcile(provider)[0] == total + pair_bytes
    for result in results:
        if not isinstance(result, BaseException):
            assert await provider.load_access_token(result.access_token) is not None


def _copy_legacy(source: GatewayOAuthProvider, paths: RuntimePaths) -> None:
    """Build the prior schema from real persisted authority, without new counters or timestamps."""
    path = paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(source.store.path) as original, sqlite3.connect(path) as legacy:
        legacy.executescript("""
            CREATE TABLE clients (client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE pending (state_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL,
                requester_id TEXT, authenticated_user_id TEXT, agent_name TEXT, csrf_hash TEXT);
            CREATE TABLE grants (grant_id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE capabilities (token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
                payload TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (grant_id) REFERENCES grants(grant_id));
        """)
        for table in ("clients", "pending", "grants", "capabilities"):
            fields = [row[1] for row in legacy.execute("PRAGMA table_info(" + table + ")")]
            rows = original.execute("SELECT " + ", ".join(fields) + " FROM " + table).fetchall()  # noqa: S608
            legacy.executemany("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in fields) + ")", rows)  # noqa: S608


async def test_legacy_migration_backfills_once_and_over_budget_authority_remains_revocable(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Legacy access timestamps use migration time without changing expiry or rebuilding on restart."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    original = await provider.load_refresh_token(client, tokens.refresh_token)
    assert original is not None
    for _ in range(5):
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    await provider.get_client("desktop")
    legacy_paths = replace(runtime_paths, storage_root=runtime_paths.storage_root / "legacy")
    _copy_legacy(provider, legacy_paths)
    clock.now += 10
    limited = _provider(legacy_paths, clock, MAX_BYTES=1, USER_MAX_BYTES=1)
    usage = _reconcile(limited)
    with sqlite3.connect(limited.store.path) as connection:
        timestamps = connection.execute(
            "SELECT issued_at, expires_at FROM capabilities WHERE kind = 'access'",
        ).fetchall()
    assert timestamps == [(2_000_000_010, 2_000_000_900)] * 6
    current = await limited.load_refresh_token(client, tokens.refresh_token)
    assert current is not None
    assert await limited.load_access_token(tokens.access_token) is not None
    clock.now += 59.999
    restarted = _provider(legacy_paths, clock)
    assert _reconcile(restarted) == usage
    with pytest.raises(GatewayOAuthCapacityError):
        await restarted.exchange_refresh_token(client, current, ["mcp:tools"])
    with sqlite3.connect(restarted.store.path) as connection:
        assert (
            connection.execute("SELECT issued_at, expires_at FROM capabilities WHERE kind = 'access'").fetchall()
            == timestamps
        )
    clock.now += 0.001
    fresh = await restarted.exchange_refresh_token(client, current, ["mcp:tools"])
    # A retained old refresh remains valid revocation authority even at exhausted budgets.
    await limited.revoke_token(original)
    assert await restarted.load_access_token(fresh.access_token) is None
    assert _reconcile(restarted)[1] == {}


async def test_parallel_issuance_burst_has_one_winner_and_backwards_clock_does_not_reset(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Serialized issuance cannot exceed six successes; historical rows survive rotation unchanged."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    first = await provider.load_refresh_token(client, tokens.refresh_token)
    assert first is not None
    for _ in range(4):
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    current = await provider.load_refresh_token(client, tokens.refresh_token)
    assert current is not None
    other = _provider(runtime_paths, clock)
    results = await asyncio.gather(
        provider.exchange_refresh_token(client, current, ["mcp:tools"]),
        other.exchange_refresh_token(client, current, ["mcp:tools"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, TokenError) for result in results) == 1
    winner = next(result for result in results if not isinstance(result, BaseException))
    assert await provider.load_access_token(winner.access_token) is None
    assert _reconcile(provider)[1] == {}
    # A fresh family reaches its limit even if the injected clock moves backwards.
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    for _ in range(5):
        refresh = await provider.load_refresh_token(client, tokens.refresh_token)
        assert refresh is not None
        tokens = await provider.exchange_refresh_token(client, refresh, ["mcp:tools"])
    current = await provider.load_refresh_token(client, tokens.refresh_token)
    assert current is not None
    clock.now -= 10
    with pytest.raises(GatewayOAuthCapacityError):
        await provider.exchange_refresh_token(client, current, ["mcp:tools"])
    _reconcile(provider)


async def test_pruning_and_refresh_queries_use_indexes_without_history_summation(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine write-lock work uses due/live/range indexes instead of retained capability scans."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    current = await provider.load_refresh_token(client, tokens.refresh_token)
    assert current is not None
    statements: list[str] = []
    connect = sqlite3.connect

    def traced(database: str | Path, *, timeout: float = 5, isolation_level: str | None = "") -> sqlite3.Connection:
        connection = connect(database, timeout=timeout, isolation_level=isolation_level)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    await provider.get_client("missing")
    await provider.exchange_refresh_token(client, current, ["mcp:tools"])
    queries = {sql for sql in statements if sql.lstrip().startswith(("SELECT", "DELETE", "UPDATE"))}
    assert not any("SUM(" in sql.upper() for sql in queries)
    plans = {}
    with connect(provider.store.path) as connection:
        for query in queries:
            plans[query] = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
    allowed_scans = {
        "SCAN capabilities USING COVERING INDEX capabilities_code_consumed",
        "SCAN capabilities USING INDEX capabilities_code_consumed",
        "SCAN grants USING COVERING INDEX grants_revoked",
        "SCAN grants USING INDEX grants_revoked",
    }
    assert not any(
        detail.startswith("SCAN ") and detail not in allowed_scans for plan in plans.values() for detail in plan
    )
    details = "\n".join(detail for plan in plans.values() for detail in plan)
    for index in (
        "capabilities_issuance",
        "capabilities_live",
        "capabilities_code_expiry",
        "capabilities_code_consumed",
        "grants_expiry",
        "grants_revoked",
    ):
        assert index in details


async def test_failed_migration_rolls_back_schema_counters_and_marker(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interruption after backfill must leave the old schema intact and safely retryable."""
    clock = _Clock()
    source = _provider(runtime_paths, clock)
    tokens = await source.exchange_authorization_code(client, await issue_code(source, client))
    legacy_paths = replace(runtime_paths, storage_root=runtime_paths.storage_root / "legacy")
    _copy_legacy(source, legacy_paths)
    migrate = store_module.migrate_accounting

    def interrupted(connection: sqlite3.Connection, now: float) -> None:
        migrate(connection, now)
        msg = "Synthetic interruption before commit"
        raise RuntimeError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "migrate_accounting", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic interruption"):
            _provider(legacy_paths, clock)
    path = legacy_paths.storage_root / "mcp_gateway" / "oauth.sqlite3"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "accounted_bytes" not in {row[1] for row in connection.execute("PRAGMA table_info(grants)")}
        assert connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0] == 3
    reopened = _provider(legacy_paths, clock)
    assert await reopened.load_access_token(tokens.access_token) is not None
    _reconcile(reopened)


async def test_counters_reconcile_denial_code_expiry_and_revoked_family_pruning(
    runtime_paths: RuntimePaths,
    client: OAuthClientInformationFull,
) -> None:
    """Every prune and consent-denial deletion removes its exact global and requester charge."""
    clock = _Clock()
    provider = _provider(runtime_paths, clock)
    await issue_code(provider, client)
    _reconcile(provider)
    clock.now += 300
    await provider.get_client("desktop")
    assert _reconcile(provider)[1] == {}
    state = await pending(provider, client)
    identity = {
        "requester_id": "@alice:example.org",
        "authenticated_user_id": "@alice:example.org",
        "agent_name": "personal",
    }
    consent = await provider.begin_consent(state, **identity)
    await provider.finish_consent(state, csrf_token=consent.csrf_token, allow=False, **identity)
    _reconcile(provider)
    tokens = await provider.exchange_authorization_code(client, await issue_code(provider, client))
    with sqlite3.connect(provider.store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE grants SET requester_id = '@bob:example.org'")
        connection.execute("UPDATE grants SET revoked = 1")
    await provider.get_client("desktop")
    assert await provider.load_access_token(tokens.access_token) is None
    assert _reconcile(provider)[1] == {}
