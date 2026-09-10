"""Account lifecycle changes revoke only the affected MCP authorization state."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

import mindroom.mcp_gateway.accounts as accounts_module
from mindroom.mcp_gateway.accounts import AccountConflictError, GatewayAccounts
from mindroom.mcp_gateway.store import GatewayOAuthStore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


def _directory(tmp_path: Path) -> GatewayAccounts:

    store = GatewayOAuthStore(
        tmp_path,
        onboarding_max_bytes=1000000,
        max_bytes=1000000,
        user_max_bytes=1000000,
        clock=lambda: 2_000_000_000.0,
    )
    return GatewayAccounts(store)


async def test_exact_identity_and_unknown_inactive_denial(tmp_path: Path) -> None:
    """Login lookup never merges differently cased or inactive identities."""
    directory = _directory(tmp_path)
    account = await directory.create({"userName": "Alice@example.org", "active": True})
    assert await directory.resolve_active("Alice@example.org") == account["id"]
    assert await directory.resolve_active("alice@example.org") is None
    assert await directory.resolve_active("unknown@example.org") is None
    await directory.replace(account["id"], {"userName": "Alice@example.org", "active": False})
    assert await directory.resolve_active("Alice@example.org") is None


async def test_duplicate_conflict_and_profile_allowlist(tmp_path: Path) -> None:
    """Duplicate names fail and submitted passwords never enter durable storage."""
    directory = _directory(tmp_path)
    account = await directory.create(
        {"userName": "alice@example.org", "active": True, "password": "must-never-persist", "displayName": "Alice"},
    )
    assert account["displayName"] == "Alice"
    assert "password" not in account
    with pytest.raises(AccountConflictError):
        await directory.create({"userName": "alice@example.org", "active": True})
    assert "must-never-persist" not in directory.store.path.read_bytes().decode(errors="replace")


@pytest.mark.parametrize("operation", ["deactivate", "rename", "delete"])
async def test_account_change_revokes_bound_state_without_resurrection(tmp_path: Path, operation: str) -> None:
    """Account mutations remove every bound token and pending consent only for that user."""
    directory = _directory(tmp_path)
    first = await directory.create({"userName": "alice@example.org", "active": True})
    other = await directory.create({"userName": "bob@example.org", "active": True})

    def seed(connection: sqlite3.Connection) -> None:
        for account in (first, other):
            account_id = account["id"]
            connection.execute(
                "INSERT INTO grants (grant_id, payload, expires_at, account_id) VALUES (?, ?, ?, ?)",
                (account_id, "{}", 2_100_000_000, account_id),
            )
            connection.execute(
                "INSERT INTO pending (state_hash, payload, expires_at, account_id) VALUES (?, ?, ?, ?)",
                (account_id, "{}", 2_100_000_000, account_id),
            )
            connection.execute(
                "INSERT INTO capabilities (token_hash, kind, grant_id, payload, expires_at, consumed) VALUES (?, 'refresh', ?, '{}', ?, 0)",
                (account_id, account_id, 2_100_000_000),
            )

    await directory.store.transact(seed)
    if operation == "delete":
        await directory.delete(first["id"])
        replacement = await directory.create({"userName": "alice@example.org", "active": True})
        assert replacement["id"] != first["id"]
    else:
        await directory.replace(
            first["id"],
            {
                "userName": "new@example.org" if operation == "rename" else "alice@example.org",
                "active": operation != "deactivate",
            },
        )
        await directory.replace(first["id"], {"userName": "alice@example.org", "active": True})
    with sqlite3.connect(directory.store.path) as connection:
        for table, column in [("grants", "grant_id"), ("pending", "state_hash"), ("capabilities", "grant_id")]:
            assert connection.execute(f"SELECT {column} FROM {table}").fetchall() == [(other["id"],)]  # noqa: S608


async def test_failed_mutation_rolls_back_profile_and_revocation(tmp_path: Path) -> None:
    """A duplicate rename leaves the original account active."""
    directory = _directory(tmp_path)
    first = await directory.create({"userName": "alice@example.org", "active": True})
    await directory.create({"userName": "bob@example.org", "active": True})
    with pytest.raises(AccountConflictError):
        await directory.replace(first["id"], {"userName": "bob@example.org", "active": False})
    assert await directory.resolve_active("alice@example.org") == first["id"]


@pytest.mark.parametrize("operation", ["deactivate", "rename", "delete"])
async def test_external_token_cannot_revive_after_account_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Subsecond account cutoffs reject old JWTs after rename, reactivation or recreation."""
    now = [100.1]
    monkeypatch.setattr(accounts_module.time, "time", lambda: now[0])
    directory = _directory(tmp_path)
    account = await directory.create({"userName": "Alice@example.org", "active": True})
    assert await directory.resolve_external("Alice@example.org", 100.0) is None
    assert await directory.resolve_external("Alice@example.org", 160.1) is None
    assert await directory.resolve_external("Alice@example.org", 160.2) == account["id"]
    assert await directory.resolve_external("alice@example.org", 160.2) is None
    now[0] = 160.3
    if operation == "delete":
        await directory.delete(account["id"])
        account = await directory.create({"userName": "Alice@example.org", "active": True})
    else:
        await directory.replace(
            account["id"],
            {
                "userName": "other@example.org" if operation == "rename" else "Alice@example.org",
                "active": operation != "deactivate",
            },
        )
        await directory.replace(account["id"], {"userName": "Alice@example.org", "active": True})
    assert await directory.resolve_external("Alice@example.org", 160.2) is None
    assert await directory.resolve_external("Alice@example.org", 220.3) is None
    assert await directory.resolve_external("Alice@example.org", 220.4) == account["id"]
    assert "token_valid_after" not in account
    reopened = _directory(tmp_path)
    assert await reopened.resolve_external("Alice@example.org", 160.2) is None


async def test_external_profile_update_preserves_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrelated profile updates do not invalidate an existing signed credential."""
    now = [100.1]
    monkeypatch.setattr(accounts_module.time, "time", lambda: now[0])
    directory = _directory(tmp_path)
    account = await directory.create({"userName": "Alice@example.org", "active": True})
    now[0] = 161.1
    await directory.replace(account["id"], {"userName": "Alice@example.org", "active": True, "displayName": "New Name"})
    assert await directory.resolve_external("Alice@example.org", 160.2) == account["id"]


async def test_migration_does_not_revive_preexisting_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a cutoff to a legacy account requires fresh credentials once, durably."""
    directory = _directory(tmp_path)
    account = await directory.create({"userName": "Alice@example.org", "active": True})
    with sqlite3.connect(directory.store.path) as connection:
        connection.execute("ALTER TABLE gateway_accounts DROP COLUMN token_valid_after")
    monkeypatch.setattr(accounts_module.time, "time", lambda: 200.5)
    migrated = _directory(tmp_path)
    assert await migrated.resolve_external("Alice@example.org", 200.4) is None
    assert await migrated.resolve_external("Alice@example.org", 260.5) is None
    assert await migrated.resolve_external("Alice@example.org", 260.6) == account["id"]
    monkeypatch.setattr(accounts_module.time, "time", lambda: 261.5)
    assert await _directory(tmp_path).resolve_external("Alice@example.org", 260.6) == account["id"]
