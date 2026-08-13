"""Tests for cloud Matrix controller identity lookup."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import nio
import pytest
from nio.crypto import Olm
from nio.store import DefaultStore

from mindroom.desktop.identity import DesktopIdentityError, controller_identity_for_entity
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


_USER_ID = "@computer:example.org"
_DEVICE_ID = "CLOUDDEVICE"


def _persisted_default_store(tmp_path: Path) -> tuple[RuntimePaths, Path, str]:
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState()
    state.add_account(
        managed_account_key("computer"),
        "computer",
        "unused-password",
        domain="example.org",
        device_id=_DEVICE_ID,
        access_token="unused-token",  # noqa: S106 - Test-only Matrix state fixture.
    )
    state.save(runtime_paths)
    store_path = olm_store_dir(_USER_ID, runtime_paths)
    store_path.mkdir(parents=True, exist_ok=True)
    store = DefaultStore(
        _USER_ID,
        _DEVICE_ID,
        str(store_path),
        pickle_key=nio.AsyncClientConfig().pickle_key,
    )
    olm = Olm(_USER_ID, _DEVICE_ID, store)
    expected_fingerprint = olm.account.identity_keys["ed25519"]
    database_path = store_path / f"{_USER_ID}_{_DEVICE_ID}.db"
    store.database.close()
    return runtime_paths, database_path, expected_fingerprint


def _database_snapshot(database_path: Path) -> tuple[bytes, tuple[str, ...]]:
    database_bytes = database_path.read_bytes()
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        logical_contents = tuple(connection.iterdump())
    finally:
        connection.close()
    return database_bytes, logical_contents


def _database_tables(database_path: Path) -> set[str]:
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
    finally:
        connection.close()


def _assert_unreadable_without_mutation(runtime_paths: RuntimePaths, database_path: Path) -> None:
    before = _database_snapshot(database_path)

    with pytest.raises(DesktopIdentityError, match="unreadable local Olm identity store"):
        controller_identity_for_entity("computer", runtime_paths=runtime_paths)

    assert _database_snapshot(database_path) == before


def test_controller_identity_reads_default_store_without_mutation(tmp_path: Path) -> None:
    """Reading the pin leaves the DefaultStore database byte-for-byte intact."""
    runtime_paths, database_path, expected_fingerprint = _persisted_default_store(tmp_path)
    before = _database_snapshot(database_path)

    assert "devicetruststate" not in _database_tables(database_path)

    identity = controller_identity_for_entity("computer", runtime_paths=runtime_paths)

    assert identity.user_id == _USER_ID
    assert identity.device_id == _DEVICE_ID
    assert identity.ed25519 == expected_fingerprint
    assert _database_snapshot(database_path) == before
    assert "devicetruststate" not in _database_tables(database_path)


@pytest.mark.parametrize("versions", [(), (10, 10), (9,)])
def test_controller_identity_rejects_store_version_mismatch_without_mutation(
    tmp_path: Path,
    versions: tuple[int, ...],
) -> None:
    """The reader requires one exact store-v10 marker and never repairs it."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM storeversion")
        connection.executemany(
            "INSERT INTO storeversion (version) VALUES (?)",
            ((version,) for version in versions),
        )

    _assert_unreadable_without_mutation(runtime_paths, database_path)


def test_controller_identity_rejects_non_integer_store_version_without_mutation(tmp_path: Path) -> None:
    """A numerically equal REAL marker is not the authenticated integer version."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE storeversion")
        connection.execute("CREATE TABLE storeversion (id INTEGER PRIMARY KEY, version REAL NOT NULL)")
        connection.execute("INSERT INTO storeversion (version) VALUES (10.0)")

    _assert_unreadable_without_mutation(runtime_paths, database_path)


@pytest.mark.parametrize("account_count", [0, 2])
def test_controller_identity_rejects_account_cardinality_without_mutation(
    tmp_path: Path,
    account_count: int,
) -> None:
    """The per-device database must contain only its one expected account."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        if account_count == 0:
            connection.execute("DELETE FROM accounts")
        else:
            account_pickle, shared = connection.execute(
                "SELECT account, shared FROM accounts",
            ).fetchone()
            connection.execute(
                "INSERT INTO accounts (account, user_id, device_id, shared) VALUES (?, ?, ?, ?)",
                (account_pickle, "@other:example.org", "OTHERDEVICE", shared),
            )

    _assert_unreadable_without_mutation(runtime_paths, database_path)


@pytest.mark.parametrize(
    ("column", "value"),
    [("user_id", "@other:example.org"), ("device_id", "OTHERDEVICE")],
)
def test_controller_identity_rejects_wrong_account_identity_without_mutation(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    """A valid pickle under a different user or device cannot supply the pin."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        if column == "user_id":
            connection.execute("UPDATE accounts SET user_id = ?", (value,))
        else:
            connection.execute("UPDATE accounts SET device_id = ?", (value,))

    _assert_unreadable_without_mutation(runtime_paths, database_path)


def test_controller_identity_rejects_non_bytes_account_pickle_without_mutation(tmp_path: Path) -> None:
    """SQLite coercions cannot turn a text account pickle into trusted bytes."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE accounts SET account = ?", ("not-bytes",))

    _assert_unreadable_without_mutation(runtime_paths, database_path)


@pytest.mark.parametrize("shared", [2, "invalid"])
def test_controller_identity_rejects_invalid_shared_flag_without_mutation(
    tmp_path: Path,
    shared: object,
) -> None:
    """Only SQLite integer zero or one is accepted for the Olm shared flag."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE accounts SET shared = ?", (shared,))

    _assert_unreadable_without_mutation(runtime_paths, database_path)


def test_controller_identity_accepts_shared_integer_one_without_mutation(tmp_path: Path) -> None:
    """The authenticated SQLite integer one decodes as a shared Olm account."""
    runtime_paths, database_path, expected_fingerprint = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE accounts SET shared = 1")
    before = _database_snapshot(database_path)

    identity = controller_identity_for_entity("computer", runtime_paths=runtime_paths)

    assert identity.ed25519 == expected_fingerprint
    assert _database_snapshot(database_path) == before


def test_controller_identity_rejects_unreadable_account_pickle_without_mutation(tmp_path: Path) -> None:
    """Olm decode failures leave the original pickle and database untouched."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE accounts SET account = ?",
            (sqlite3.Binary(b"not-an-olm-account"),),
        )

    _assert_unreadable_without_mutation(runtime_paths, database_path)


def test_controller_identity_rejects_unreadable_database_without_mutation(tmp_path: Path) -> None:
    """A non-SQLite store fails closed without replacing or rewriting the file."""
    runtime_paths, database_path, _ = _persisted_default_store(tmp_path)
    database_path.write_bytes(b"not a SQLite database")
    before = database_path.read_bytes()

    with pytest.raises(DesktopIdentityError, match="unreadable local Olm identity store"):
        controller_identity_for_entity("computer", runtime_paths=runtime_paths)

    assert database_path.read_bytes() == before


def test_controller_identity_requires_a_started_entity(tmp_path: Path) -> None:
    """Missing account state produces a setup instruction instead of a partial pin."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(DesktopIdentityError, match="start MindRoom once"):
        controller_identity_for_entity("computer", runtime_paths=runtime_paths)
