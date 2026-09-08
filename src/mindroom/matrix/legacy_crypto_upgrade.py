"""Retire released pre-durable transport work without changing Matrix crypto keys."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from nio import LocalProtocolError
from nio.crypto import OlmAccount
from nio.store._sqlite_lease import FileLease

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_LEGACY_TRANSPORT_TABLES = ("pendingtimelineevents", "syncrecoverygaps", "syncrecoveryabandonedrooms", "synctokens")


def retire_legacy_crypto_recovery(
    database_path: Path,
    *,
    user_id: str,
    device_id: str,
    pickle_key: str = "DEFAULT_KEY",
) -> None:
    """Permit first durable adoption after explicitly abandoning old transport work.

    Nio 1.0 refuses outstanding 0.40 recovery but no longer exposes its old
    settlement API. Authenticate retained keys before retiring recovery and
    obsolete sync checkpoints in one transaction under Nio's exclusive file
    lease. Never reset an existing durable stream or edit crypto/trust records.
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
            accounts = connection.execute("SELECT user_id, device_id, account, shared FROM accounts").fetchall()
            if any(account[:2] != (user_id, device_id) for account in accounts):
                msg = "Legacy Matrix store account/device identity mismatch"
                raise LocalProtocolError(msg)
            for _, _, pickle, shared in accounts:
                OlmAccount.from_pickle(pickle, pickle_key, bool(shared))
            retired = 0
            for table in _LEGACY_TRANSPORT_TABLES:
                if table in tables:
                    retired += connection.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 - fixed legacy tables
        if retired:
            logger.warning("matrix_legacy_recovery_retired", row_count=retired)
