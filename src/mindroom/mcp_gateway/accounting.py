"""One-time OAuth schema migration and local transaction-maintained byte accounting."""

# SQL fragments use only the closed schema constants below, never request data.
# ruff: noqa: S608

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

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


def migrate_accounting(connection: sqlite3.Connection, now: float) -> None:
    """Backfill once under the caller's writer transaction; reopening never resets timestamps."""
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


def migrate_lifecycle_accounting(connection: sqlite3.Connection) -> None:
    """Compact consumed refresh bindings and install their bounded charge once."""
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
