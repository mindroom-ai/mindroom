"""Selection storage with transaction-maintained gateway and requester byte charges."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def initialize_selections(connection: sqlite3.Connection) -> None:
    """Create durable owner settings and cascade account deletion through byte accounting."""
    connection.execute("""CREATE TABLE IF NOT EXISTS gateway_selections (
        owner_key TEXT PRIMARY KEY, requester_id TEXT NOT NULL,
        account_id TEXT REFERENCES gateway_accounts(account_id) ON DELETE CASCADE,
        agents TEXT NOT NULL,
        accounted_bytes INTEGER GENERATED ALWAYS AS (
            1024 + length(CAST(owner_key AS BLOB)) + 2 * length(CAST(requester_id AS BLOB))
            + COALESCE(length(CAST(account_id AS BLOB)), 0) + length(CAST(agents AS BLOB))
        ) STORED
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS selections_account ON gateway_selections(account_id)")
    for event, suffix, owner, delta in (
        ("INSERT", "insert", "NEW", "NEW.accounted_bytes"),
        ("UPDATE OF agents", "update", "NEW", "NEW.accounted_bytes - OLD.accounted_bytes"),
        ("DELETE", "delete", "OLD", "-OLD.accounted_bytes"),
    ):
        # Only closed schema constants are interpolated, never request data.
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS selections_usage_{suffix} AFTER {event} ON gateway_selections
            BEGIN
                UPDATE oauth_usage SET bytes_used = bytes_used + ({delta}) WHERE singleton = 1;
                INSERT INTO requester_usage (requester_id, bytes_used) VALUES ({owner}.requester_id, {delta})
                ON CONFLICT(requester_id) DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
                DELETE FROM requester_usage WHERE requester_id = {owner}.requester_id AND bytes_used = 0;
            END""")  # noqa: S608
