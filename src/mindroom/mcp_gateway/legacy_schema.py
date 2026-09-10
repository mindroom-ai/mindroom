"""One-time OAuth schema migrations and local transaction-maintained byte accounting."""

# SQL fragments use only the closed schema constants below, never request data.
# ruff: noqa: S608

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

# The allowance covers fixed columns, row/index bookkeeping, and numeric counters.
# Grants also reserve one requester-counter row and its key, conservatively once per grant.
_FIELDS = {
    "clients": ("client_id", "metadata"),
    "pending": ("state_hash", "payload", "requester_id", "authenticated_user_id", "agent_name", "csrf_hash"),
    "grants": ("grant_id", "payload", "requester_id"),
    "capabilities": ("token_hash", "kind", "grant_id", "payload"),
}
_KEYS = {"clients": "client_id", "pending": "state_hash", "grants": "grant_id", "capabilities": "token_hash"}


def _charge(table: str, prefix: str = "", *, lifecycle: bool = False) -> str:
    fields = _FIELDS[table]
    if table == "grants":
        fields += ("requester_id",)
    if lifecycle and table in {"grants", "pending"}:
        fields += ("account_id",)
    overhead = str(2048 if table == "grants" else 1024)
    if lifecycle and table == "capabilities":
        overhead = f"CASE WHEN {prefix}kind = 'refresh' AND {prefix}consumed = 1 AND {prefix}payload = '{{}}' THEN 256 ELSE 1024 END"
    return " + ".join(
        [overhead] + [f"COALESCE(length(CAST({prefix}{field} AS BLOB)), 0)" for field in fields],
    )


def _install_triggers(connection: sqlite3.Connection, table: str, *, lifecycle: bool = False) -> None:
    """Charge recomputation triggers feed delta triggers, including every deletion path."""
    key = _KEYS[table]
    charge = _charge(table, "NEW.", lifecycle=lifecycle)
    assignment = f"accounted_bytes = {charge}"
    if table == "grants":
        assignment += f""", requester_charge = {charge} + COALESCE((
            SELECT accounted_bytes FROM clients WHERE client_id = json_extract(NEW.payload, '$.client_id')
        ), 0)"""
    fields = _FIELDS[table]
    if lifecycle and table in {"grants", "pending"}:
        fields += ("account_id",)
    if lifecycle and table == "capabilities":
        fields += ("consumed",)
    for event in ("INSERT", "UPDATE OF " + ", ".join(fields)):
        name = "insert" if event == "INSERT" else "change"
        connection.execute(f"""
            CREATE TRIGGER {table}_charge_{name} AFTER {event} ON {table}
            BEGIN UPDATE {table} SET {assignment} WHERE {key} = NEW.{key}; END
        """)

    for event in (
        "UPDATE OF accounted_bytes, requester_charge" if table == "grants" else "UPDATE OF accounted_bytes",
        "DELETE",
    ):
        deleting = event == "DELETE"
        delta = "-OLD.accounted_bytes" if deleting else "NEW.accounted_bytes - OLD.accounted_bytes"
        statements = f"UPDATE oauth_usage SET bytes_used = bytes_used + ({delta}) WHERE singleton = 1;"
        if table in {"clients", "pending"}:
            condition = ""
            if table == "clients":
                condition = """AND NOT EXISTS (
                    SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') = OLD.client_id
                )"""
            statements += f"UPDATE oauth_usage SET onboarding_bytes = onboarding_bytes + ({delta}) WHERE singleton = 1 {condition};"
        else:
            if table == "grants":
                owner = "OLD.requester_id"
                user_delta = "-OLD.requester_charge" if deleting else "NEW.requester_charge - OLD.requester_charge"
            else:
                owner = "(SELECT requester_id FROM grants WHERE grant_id = OLD.grant_id)"
                user_delta = delta
            statements += f"""
                INSERT INTO requester_usage (requester_id, bytes_used) VALUES ({owner}, {user_delta})
                ON CONFLICT(requester_id) DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
                DELETE FROM requester_usage WHERE requester_id = {owner} AND bytes_used = 0;
            """
        name = "delete" if deleting else "update"
        connection.execute(f"CREATE TRIGGER {table}_usage_{name} AFTER {event} ON {table} BEGIN {statements} END")


def _migrate_client_expiry(connection: sqlite3.Connection, registration_expires_at: Callable[[], float]) -> None:
    """Give legacy client registrations the current fixed retention deadline once."""
    # Legacy format: staged `clients` rows had no registration expiry column.
    # Last legacy release: pre-release schema; v2026.9.33 already completed this upgrade.
    # Handling: assign one durable registration deadline and index it without sliding on reopen.
    # Coverage: tests/test_mcp_gateway_oauth.py::test_legacy_registration_migration_grants_one_durable_grace_period.
    if "expires_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(clients)")}:
        connection.execute("ALTER TABLE clients ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
        connection.execute("UPDATE clients SET expires_at = ?", (registration_expires_at(),))
    connection.execute("CREATE INDEX IF NOT EXISTS clients_expiry ON clients(expires_at)")


def _migrate_accounting(connection: sqlite3.Connection, now: float) -> None:
    """Backfill once under the caller's writer transaction; reopening never resets timestamps."""
    # Legacy format: staged gateway tables had no byte charges, counters, requester ownership, or issuance time.
    # Last legacy release: pre-release schema; v2026.9.33 already completed this upgrade.
    # Handling: backfill fields and counters once inside the store's writer transaction.
    # Coverage: tests/test_mcp_gateway_oauth_capacity.py::test_legacy_migration_backfills_once_and_over_budget_authority_remains_revocable.
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    for table in _FIELDS:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN accounted_bytes INTEGER NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE grants ADD COLUMN requester_id TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE grants ADD COLUMN requester_charge INTEGER NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE capabilities ADD COLUMN issued_at REAL")
    connection.execute("UPDATE grants SET requester_id = json_extract(payload, '$.requester_id')")
    connection.execute("UPDATE capabilities SET issued_at = ? WHERE kind = 'access'", (now,))
    for table in _FIELDS:
        connection.execute(f"UPDATE {table} SET accounted_bytes = {_charge(table)}")
    connection.execute("""UPDATE grants SET requester_charge = accounted_bytes + COALESCE((
        SELECT accounted_bytes FROM clients WHERE client_id = json_extract(grants.payload, '$.client_id')
    ), 0)""")
    connection.execute("""CREATE TABLE oauth_usage (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1), bytes_used INTEGER NOT NULL, onboarding_bytes INTEGER NOT NULL
    )""")
    connection.execute("CREATE TABLE requester_usage (requester_id TEXT PRIMARY KEY, bytes_used INTEGER NOT NULL)")
    connection.execute("""INSERT INTO oauth_usage VALUES (1,
        1024 + (SELECT COALESCE(SUM(accounted_bytes), 0) FROM clients)
             + (SELECT COALESCE(SUM(accounted_bytes), 0) FROM pending)
             + (SELECT COALESCE(SUM(accounted_bytes), 0) FROM grants)
             + (SELECT COALESCE(SUM(accounted_bytes), 0) FROM capabilities),
        (SELECT COALESCE(SUM(accounted_bytes), 0) FROM pending)
             + (SELECT COALESCE(SUM(accounted_bytes), 0) FROM clients WHERE NOT EXISTS (
                 SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') = clients.client_id
             ))
    )""")
    connection.execute("""INSERT INTO requester_usage
        SELECT requester_id, SUM(charge) FROM (
            SELECT requester_id, requester_charge AS charge FROM grants
            UNION ALL
            SELECT g.requester_id, c.accounted_bytes FROM capabilities c JOIN grants g USING (grant_id)
        ) GROUP BY requester_id""")
    for table in _FIELDS:
        _install_triggers(connection, table)
    connection.execute("""CREATE TRIGGER grants_owner_immutable BEFORE UPDATE OF requester_id ON grants
        WHEN OLD.requester_id != NEW.requester_id
        BEGIN SELECT RAISE(ABORT, 'Grant requester is immutable'); END""")
    connection.execute("""CREATE TRIGGER grants_pin_client AFTER INSERT ON grants
        WHEN NOT EXISTS (SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') =
            json_extract(NEW.payload, '$.client_id') AND grant_id != NEW.grant_id)
        BEGIN UPDATE oauth_usage SET onboarding_bytes = onboarding_bytes - COALESCE((
            SELECT accounted_bytes FROM clients WHERE client_id = json_extract(NEW.payload, '$.client_id')
        ), 0) WHERE singleton = 1; END""")
    connection.execute("""CREATE TRIGGER grants_unpin_client AFTER DELETE ON grants
        WHEN NOT EXISTS (SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') = json_extract(OLD.payload, '$.client_id'))
        BEGIN UPDATE oauth_usage SET onboarding_bytes = onboarding_bytes + COALESCE((
            SELECT accounted_bytes FROM clients WHERE client_id = json_extract(OLD.payload, '$.client_id')
        ), 0) WHERE singleton = 1; END""")
    for statement in (
        "CREATE INDEX grants_expiry ON grants(expires_at)",
        "CREATE INDEX grants_revoked ON grants(grant_id) WHERE revoked = 1",
        "CREATE INDEX capabilities_code_expiry ON capabilities(expires_at) WHERE kind = 'code'",
        "CREATE INDEX capabilities_code_consumed ON capabilities(grant_id) WHERE kind = 'code' AND consumed = 1",
        "CREATE INDEX capabilities_live ON capabilities(grant_id) WHERE consumed = 0 AND kind IN ('access', 'refresh')",
        "CREATE INDEX capabilities_issuance ON capabilities(grant_id, issued_at) WHERE kind = 'access'",
    ):
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")


def _migrate_lifecycle(connection: sqlite3.Connection) -> None:
    """Preserve existing absolute deadlines and leave unknown creation/activity dates null."""
    # Legacy format: staged grants and pending consent lacked lifecycle dates and account bindings.
    # Last legacy release: pre-release schema; v2026.9.33 already completed this upgrade.
    # Handling: preserve absolute expiry, leave unknown history null, and add nullable account ownership.
    # Coverage: tests/test_mcp_gateway_lifecycle.py::test_legacy_metadata_stays_unknown_and_absolute_expiry_never_extends.
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(grants)")}
    if "idle_expires_at" in columns:
        return
    for declaration in (
        "created_at REAL",
        "last_used_at REAL",
        "last_activity_at REAL",
        "idle_expires_at REAL",
        "account_id TEXT",
    ):
        connection.execute("ALTER TABLE grants ADD COLUMN " + declaration)
    connection.execute("UPDATE grants SET idle_expires_at = expires_at")
    connection.execute("ALTER TABLE pending ADD COLUMN account_id TEXT")
    for statement in (
        "CREATE INDEX grants_idle_expiry ON grants(idle_expires_at)",
        "CREATE INDEX grants_account ON grants(account_id)",
        "CREATE INDEX pending_account ON pending(account_id)",
        "CREATE INDEX capabilities_access_expiry ON capabilities(expires_at) WHERE kind = 'access'",
        """CREATE INDEX grants_owner ON grants(requester_id,
            json_extract(payload, '$.authenticated_user_id'), json_extract(payload, '$.agent_name'),
            json_extract(payload, '$.resource'))""",
        "CREATE INDEX pending_owner ON pending(requester_id, authenticated_user_id, agent_name)",
    ):
        connection.execute(statement)


def _migrate_accounts(connection: sqlite3.Connection) -> None:
    """Create the directory inside the caller's migration transaction."""
    # Legacy format: staged gateway schemas had no account directory.
    # Last legacy release: pre-release schema; v2026.9.33 already created the directory during initialization.
    # Handling: create the account directory in the same migration transaction.
    # Coverage: tests/test_mcp_gateway_accounts.py.
    connection.execute("""CREATE TABLE IF NOT EXISTS gateway_accounts (
        account_id TEXT PRIMARY KEY, user_name TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0, 1)),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        profile TEXT NOT NULL DEFAULT '{}', token_valid_after REAL NOT NULL
    )""")
    # Legacy format: released six-column gateway accounts had no external-token cutoff.
    # Last legacy release: v2026.9.46; `token_valid_after` introduced in v2026.9.47.
    # Handling: assign one durable migration-time cutoff so older external tokens remain rejected.
    # Coverage: tests/test_mcp_gateway_accounts.py::test_migration_does_not_revive_preexisting_tokens.
    if "token_valid_after" not in {row["name"] for row in connection.execute("PRAGMA table_info(gateway_accounts)")}:
        connection.execute("ALTER TABLE gateway_accounts ADD COLUMN token_valid_after REAL NOT NULL DEFAULT 0")
        connection.execute("UPDATE gateway_accounts SET token_valid_after = ?", (time.time(),))


def _migrate_lifecycle_accounting(connection: sqlite3.Connection) -> None:
    """Compact consumed refresh bindings and install their bounded charge once."""
    # Legacy format: staged accounting v1 retained full payloads on consumed refresh rows.
    # Last legacy release: pre-release schema; v2026.9.33 already completed accounting v2.
    # Handling: compact only consumed refresh payloads, replace triggers, and reconcile byte counters atomically.
    # Coverage: tests/test_mcp_gateway_oauth_capacity.py::test_v1_accounting_upgrade_compacts_historical_refresh_and_preserves_replay.
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    connection.execute("UPDATE capabilities SET payload = '{}' WHERE kind = 'refresh' AND consumed = 1")
    for table in _FIELDS:
        for suffix in ("charge_insert", "charge_change", "usage_update", "usage_delete"):
            connection.execute(f"DROP TRIGGER {table}_{suffix}")
        _install_triggers(connection, table, lifecycle=True)
        field = "metadata" if table == "clients" else "payload"
        connection.execute(f"UPDATE {table} SET {field} = {field}")
    connection.execute("PRAGMA user_version = 2")


def migrate_schema(
    connection: sqlite3.Connection,
    *,
    clock: Callable[[], float],
    registration_expires_at: Callable[[], float],
) -> None:
    """Upgrade every historical gateway schema inside the caller's writer transaction."""
    _migrate_client_expiry(connection, registration_expires_at)
    _migrate_accounting(connection, clock())
    _migrate_lifecycle(connection)
    _migrate_accounts(connection)
    _migrate_lifecycle_accounting(connection)
