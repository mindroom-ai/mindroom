"""Current Nio durable storage checks for the live chaos harness."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from nio.durable.store import DurableStore

from scripts.testing import fuzz_live_matrix as live_fuzz

if TYPE_CHECKING:
    from pathlib import Path


def _durable_database(storage_path: Path) -> Path:
    store = DurableStore(
        storage_path / "encryption_keys" / "agent",
        user_id="@agent:test",
        device_id="DEVICE",
        consumer_id=uuid4(),
    )
    path = store.path
    with store.transaction():
        store.set_cursor("cursor-before-restart")
    store.close()
    return path


def test_cold_restart_resets_current_cursor_and_preserves_crypto(tmp_path: Path) -> None:
    """Cold startup must erase Nio's real cursor without discarding account identity."""
    path = _durable_database(tmp_path)
    live_fuzz._reset_durable_sync_cursors(tmp_path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT cursor FROM NioDurableMeta").fetchone() == (None,)
        assert database.execute("SELECT user_id,device_id FROM NioDurableMeta").fetchone() == ("@agent:test", "DEVICE")


def test_cold_restart_refuses_to_erase_unfinished_input(tmp_path: Path) -> None:
    """A cursor reset must never silently bypass unfinished durable input."""
    path = _durable_database(tmp_path)
    with closing(sqlite3.connect(path)) as database:
        database.execute("INSERT INTO NioDurableInput(id,body) VALUES(1,?)", (b"private event payload",))
        database.commit()
    with pytest.raises(AssertionError, match="unfinished durable input"):
        live_fuzz._reset_durable_sync_cursors(tmp_path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT cursor FROM NioDurableMeta").fetchone() == ("cursor-before-restart",)
        assert database.execute("SELECT body FROM NioDurableInput").fetchone() == (b"private event payload",)


def test_recovery_snapshot_reads_current_store_without_event_payloads(tmp_path: Path) -> None:
    """Evidence must expose pending current-format work without copying message or crypto bodies."""
    path = _durable_database(tmp_path)
    with closing(sqlite3.connect(path)) as database:
        database.execute("INSERT INTO NioDurableInput(id,body) VALUES(1,?)", (b"private event payload",))
        database.execute(
            "INSERT INTO NioDurableBatch(records,completes_sync) VALUES(?,1)",
            ('[{"private":"event payload"}]',),
        )
        database.commit()
    snapshot = live_fuzz._nio_recovery_snapshot(tmp_path)
    stores = snapshot["stores"]
    assert isinstance(stores, dict)
    state = stores[str(path.relative_to(tmp_path))]
    assert state["pending_input_count"] == 1
    assert state["pending_batch_count"] == 1
    assert state["cursor_present"] is True
    assert "private" not in json.dumps(snapshot)


def test_failure_bundle_preserves_incomplete_and_malformed_turn_rows(tmp_path: Path) -> None:
    """Failure evidence must keep rows that the terminal polling oracle cannot accept."""
    ledger = tmp_path / "event_journal.db"
    with closing(sqlite3.connect(ledger)) as database:
        database.execute(
            "CREATE TABLE turn_records(agent_name TEXT, index_event_id TEXT, anchor_event_id TEXT, record_json TEXT)",
        )
        database.executemany(
            "INSERT INTO turn_records VALUES('general',?,?,?)",
            [
                ("$pending", "$pending", '{"completed":false}'),
                ("$corrupt", "$corrupt", "not JSON"),
            ],
        )
        database.commit()
    directory = tmp_path / "bundle"
    directory.mkdir()
    bundle = live_fuzz.FailureBundle(directory)
    bundle.finalize(
        exception=AssertionError("unfinished"),
        log_path=tmp_path / "missing.log",
        ledger_path=ledger,
        nio_recovery_snapshot={},
        oracle_snapshot={},
        model_observations={},
        diagnostics={},
        tuwunel_log="",
    )
    snapshot = json.loads((directory / "handled_turns.json").read_text())
    assert snapshot["rows"] == [
        {"index_event_id": "$corrupt", "anchor_event_id": "$corrupt", "record_json": "not JSON"},
        {"index_event_id": "$pending", "anchor_event_id": "$pending", "record_json": '{"completed":false}'},
    ]
