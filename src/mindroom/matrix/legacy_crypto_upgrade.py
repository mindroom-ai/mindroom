"""Retire released pre-durable transport work without changing Matrix crypto keys."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from nio import LocalProtocolError
from nio.store._sqlite_lease import FileLease

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_LEGACY_RECOVERY_TABLES = ("pendingtimelineevents", "syncrecoverygaps", "syncrecoveryabandonedrooms")


def retire_legacy_crypto_recovery(database_path: Path, *, user_id: str, device_id: str) -> None:
    """Permit first durable adoption after explicitly abandoning old transport work.

    Nio 1.0 refuses outstanding 0.40 recovery but no longer exposes its old
    settlement API. This one-time adapter touches only those retired tables,
    under the same exclusive file lease Nio uses for durable ownership. It
    never resets an existing durable stream or edits crypto/trust records.
    """
    if not database_path.exists():
        return
    with closing(FileLease(database_path)), closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            tables = {
                row[0].lower() for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if tables & {"niodurablemeta", "nioingestmeta"} or "accounts" not in tables:
                return
            identities = connection.execute("SELECT user_id, device_id FROM accounts").fetchall()
            if any(identity != (user_id, device_id) for identity in identities):
                msg = "Legacy Matrix store account/device identity mismatch"
                raise LocalProtocolError(msg)
            retired = 0
            for table in _LEGACY_RECOVERY_TABLES:
                if table in tables:
                    retired += connection.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 - fixed legacy tables
        if retired:
            logger.warning("matrix_legacy_recovery_retired", row_count=retired)
