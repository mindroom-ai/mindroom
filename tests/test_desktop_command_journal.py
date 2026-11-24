"""Crash recovery and durable delivery for desktop commands."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.command_journal import (
    DesktopCommandJournal,
    DesktopCommandJournalError,
    check_controller_binding,
    controller_journal_key,
    desktop_journal_path,
)
from mindroom.desktop.protocol import DesktopCommand, DesktopResponse
from mindroom.matrix.device_identity import PinnedMatrixDevice

if TYPE_CHECKING:
    from pathlib import Path

COMMAND = DesktopCommand("request-1", "session-1", 1, 1000, 31000, "status", "@user:example.org", "helper")
FINGERPRINT = "a" * 64
RESPONSE = DesktopResponse("request-1", "session-1", True, result={"ready": True})


def test_queued_command_survives_restart_before_execution(tmp_path: Path) -> None:
    """Admission must survive a process exit before any desktop action starts."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path)
    journal.admit(COMMAND, FINGERPRINT)
    journal.close()

    restored = DesktopCommandJournal.load(path)
    queued = restored.queued()
    assert len(queued) == 1
    assert queued[0].command == COMMAND
    assert queued[0].state == "queued"
    assert restored.sequence_error(replace(COMMAND, request_id="other")) is not None
    restored.close()


def test_started_and_pending_response_survive_independent_restarts(tmp_path: Path) -> None:
    """Sending a result must never require re-running the recorded action."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path)
    journal.admit(COMMAND, FINGERPRINT)
    journal.remember_started(COMMAND, FINGERPRINT)
    journal.close()

    restored = DesktopCommandJournal.load(path)
    assert restored.queued() == []
    assert restored.started()[0].command == COMMAND
    restored.remember_response(COMMAND, FINGERPRINT, RESPONSE)
    restored.close()

    delivery = DesktopCommandJournal.load(path)
    assert delivery.started() == []
    pending = delivery.pending_responses()
    assert len(pending) == 1
    delivery_id, response = pending[0]
    assert response == RESPONSE
    delivery.mark_delivered(delivery_id)
    delivery.close()

    finished = DesktopCommandJournal.load(path)
    assert finished.pending_responses() == []
    assert finished.get("request-1").response == RESPONSE
    finished.queue_response(RESPONSE)
    assert finished.pending_responses() == [(delivery_id, RESPONSE)]
    finished.close()


def test_conflicting_request_cannot_replace_admitted_body(tmp_path: Path) -> None:
    """A reused ID cannot alter the action or reset its sequence receipt."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3")
    journal.admit(COMMAND, FINGERPRINT)
    with pytest.raises(DesktopCommandJournalError, match="different"):
        journal.admit(replace(COMMAND, action="list_apps"), "b" * 64)
    assert journal.get("request-1").command == COMMAND
    journal.close()


def test_pending_work_applies_backpressure_instead_of_eviction(tmp_path: Path) -> None:
    """At capacity, no admitted action or undelivered result can disappear."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", max_entries=2)
    journal.admit(COMMAND, FINGERPRINT)
    journal.remember_response(COMMAND, FINGERPRINT, RESPONSE)
    second = replace(COMMAND, request_id="request-2", sequence=2)
    journal.admit(second, "b" * 64)
    with pytest.raises(DesktopCommandJournalError, match="capacity"):
        journal.admit(replace(COMMAND, request_id="request-3", sequence=3), "c" * 64)
    assert journal.get("request-1").response == RESPONSE
    assert journal.get("request-2").state == "queued"
    journal.mark_delivered(journal.pending_responses()[0][0])
    journal.admit(replace(COMMAND, request_id="request-3", sequence=3), "c" * 64)
    assert journal.get("request-2").state == "queued"
    journal.close()


def test_legacy_receipts_import_without_repeating_started_control(tmp_path: Path) -> None:
    """Upgrading must preserve unknown outcomes and sequence high-watermarks."""
    legacy = tmp_path / "command_journal.json"
    legacy.write_text(
        json.dumps(
            {
                "v": 1,
                "entries": [{"request_id": "request-1", "command_fingerprint": FINGERPRINT, "response": None}],
                "sequence_high_watermarks": [{"session_id": "session-1", "sequence": 9}],
            },
        ),
    )
    legacy.chmod(0o600)
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", legacy_path=legacy)
    entry = journal.get("request-1")
    assert entry.state == "started"
    assert entry.response is None
    assert journal.sequence_error(COMMAND) is not None
    journal.close()


def test_malformed_legacy_import_preserves_existing_work_and_can_retry(tmp_path: Path) -> None:
    """Reject the whole historical batch before adopting any receipt or sequence."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path)
    journal.admit(COMMAND, FINGERPRINT)
    journal.close()

    legacy_entry = {"request_id": "legacy-1", "command_fingerprint": FINGERPRINT, "response": None}
    payload = {
        "v": 1,
        "entries": [legacy_entry, {**legacy_entry, "request_id": "legacy-2", "command_fingerprint": "invalid"}],
        "sequence_high_watermarks": [{"session_id": COMMAND.session_id, "sequence": 9}],
    }
    legacy = tmp_path / "command_journal.json"
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    legacy.chmod(0o600)

    with pytest.raises(DesktopCommandJournalError, match="legacy command journal is malformed"):
        DesktopCommandJournal.load(path, legacy_path=legacy)

    unchanged = DesktopCommandJournal.load(path)
    assert unchanged.get("legacy-1") is None
    assert unchanged.get("legacy-2") is None
    assert unchanged.queued()[0].command == COMMAND
    next_command = replace(COMMAND, request_id="next", sequence=2)
    assert unchanged.sequence_error(next_command) is None
    unchanged.close()

    payload["entries"] = [legacy_entry]
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    retried = DesktopCommandJournal.load(path, legacy_path=legacy)
    assert retried.get("legacy-1").state == "started"
    assert retried.get("legacy-1").command is None
    assert retried.queued()[0].command == COMMAND
    assert retried.sequence_error(next_command) is not None
    retried.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix permission bits")
def test_private_journal_and_sidecars(tmp_path: Path) -> None:
    """Desktop content cannot inherit group-readable file modes."""
    path = tmp_path / "private" / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path)
    journal.admit(COMMAND, FINGERPRINT)
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    journal.close()
    path.chmod(0o644)
    with pytest.raises(DesktopCommandJournalError, match="group or other users"):
        DesktopCommandJournal.load(path)


def test_second_controller_can_open_journal_without_seeing_first_controllers_work(tmp_path: Path) -> None:
    """A second controller shares the SQLite journal with an isolated namespace."""
    path = tmp_path / "commands.sqlite3"
    journal_a = DesktopCommandJournal.load(path, controller_key="controller-a")
    journal_a.admit(COMMAND, FINGERPRINT)
    journal_a.queue_response(RESPONSE)
    journal_a.close()

    journal_b = DesktopCommandJournal.load(path, controller_key="controller-b")
    assert journal_b.queued() == []
    assert journal_b.pending_responses() == []
    assert journal_b.get(COMMAND.request_id) is None
    assert journal_b.sequence_error(COMMAND) is None
    assert journal_b.queued("controller-a")[0].command == COMMAND
    journal_b.close()


def test_delivered_rejections_do_not_accumulate_without_commands(tmp_path: Path) -> None:
    """Successful standalone deliveries must release disk records without inbox traffic."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path, max_entries=2)
    for index in range(5):
        journal.queue_response(replace(RESPONSE, request_id=f"rejected-{index}"))
        journal.mark_delivered(journal.pending_responses()[0][0])
    journal.close()

    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


def test_legacy_completed_receipts_wait_for_explicit_replay(tmp_path: Path) -> None:
    """An unbound historical receipt must not be sent to the newly pinned controller."""
    legacy = tmp_path / "command_journal.json"
    legacy.write_text(
        json.dumps(
            {
                "v": 1,
                "entries": [
                    {"request_id": "request-1", "command_fingerprint": FINGERPRINT, "response": RESPONSE.to_content()},
                ],
                "sequence_high_watermarks": [{"session_id": "session-1", "sequence": 1}],
            },
        ),
    )
    legacy.chmod(0o600)
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path, legacy_path=legacy, controller_key="new-controller")
    assert journal.pending_responses() == []
    assert journal.get(COMMAND.request_id).response == RESPONSE
    journal.close()

    restored = DesktopCommandJournal.load(path, legacy_path=legacy, controller_key="new-controller")
    assert restored.pending_responses() == []
    restored.queue_response(RESPONSE)
    assert [response for _, response in restored.pending_responses()] == [RESPONSE]
    restored.close()


@pytest.mark.parametrize("replacement_value", [1, 1.0])
def test_terminal_wire_outcome_cannot_change_type(tmp_path: Path, replacement_value: float) -> None:
    """Python equality must not let numeric values overwrite a boolean terminal result."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3")
    journal.admit(COMMAND, FINGERPRINT)
    journal.remember_response(COMMAND, FINGERPRINT, RESPONSE)

    with pytest.raises(DesktopCommandJournalError, match="different outcome"):
        journal.remember_response(COMMAND, FINGERPRINT, replace(RESPONSE, result={"ready": replacement_value}))
    assert journal.get(COMMAND.request_id).response.result["ready"] is True
    assert len(journal.pending_responses()) == 1
    journal.close()


def test_sequence_bound_cannot_forget_queued_sessions(tmp_path: Path) -> None:
    """New sessions must backpressure before evicting an unfinished command's replay guard."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3")
    for index in range(128):
        journal.admit(replace(COMMAND, request_id=f"request-{index}", session_id=f"session-{index}"), FINGERPRINT)

    with pytest.raises(DesktopCommandJournalError, match="capacity"):
        journal.admit(replace(COMMAND, request_id="new-request", session_id="new-session"), FINGERPRINT)
    assert journal.get("new-request") is None
    assert journal.sequence_error(replace(COMMAND, session_id="session-0")) is not None
    assert len(journal.queued()) == 128
    journal.close()


def test_sequence_eviction_keeps_recently_used_sessions(tmp_path: Path) -> None:
    """A long-lived active session must survive churn from newer idle sessions."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3")
    for index in range(128):
        command = replace(COMMAND, request_id=f"request-{index}", session_id=f"session-{index}")
        journal.admit(command, FINGERPRINT)
        journal.remember_response(
            command,
            FINGERPRINT,
            replace(RESPONSE, request_id=command.request_id, session_id=command.session_id),
        )
        journal.mark_delivered(journal.pending_responses()[0][0])
    recent = replace(COMMAND, request_id="recent", session_id="session-0", sequence=2)
    journal.admit(recent, FINGERPRINT)
    journal.remember_response(
        recent,
        FINGERPRINT,
        replace(RESPONSE, request_id=recent.request_id, session_id=recent.session_id),
    )
    journal.mark_delivered(journal.pending_responses()[0][0])

    journal.admit(replace(COMMAND, request_id="new-request", session_id="new-session"), FINGERPRINT)

    assert journal.sequence_error(replace(recent, request_id="replayed-sequence")) is not None
    journal.close()


def test_full_legacy_started_cache_does_not_block_new_admission(tmp_path: Path) -> None:
    """Bodyless upgrade receipts remain replay tombstones outside the new inbox capacity."""
    legacy = tmp_path / "command_journal.json"
    legacy.write_text(
        json.dumps(
            {
                "v": 1,
                "entries": [
                    {"request_id": f"legacy-{index}", "command_fingerprint": FINGERPRINT, "response": None}
                    for index in range(1024)
                ],
                "sequence_high_watermarks": [],
            },
        ),
    )
    legacy.chmod(0o600)
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", legacy_path=legacy, max_entries=1)

    journal.admit(COMMAND, FINGERPRINT)

    assert journal.queued()[0].command == COMMAND
    old = replace(COMMAND, request_id="legacy-0")
    journal.remember_response(old, FINGERPRINT, replace(RESPONSE, request_id=old.request_id))
    assert journal.get(old.request_id).command is None
    assert journal.get("legacy-1023").state == "started"
    journal.close()


def test_pending_response_bound_includes_replayed_delivered_receipts(tmp_path: Path) -> None:
    """A cached delivered row still needs pending capacity when it is requeued."""
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", max_entries=1)
    journal.admit(COMMAND, FINGERPRINT)
    journal.remember_response(COMMAND, FINGERPRINT, RESPONSE)
    journal.mark_delivered(journal.pending_responses()[0][0])
    journal.queue_response(replace(RESPONSE, request_id="rejected"))

    with pytest.raises(DesktopCommandJournalError, match="capacity"):
        journal.queue_response(RESPONSE)
    assert len(journal.pending_responses()) == 1
    journal.close()


def _write_v1_journal(
    path: Path,
    controller_key: str | None,
) -> tuple[DesktopCommand, DesktopCommand, DesktopCommand, DesktopResponse, DesktopResponse]:
    completed_cmd = replace(COMMAND, request_id="req-completed", session_id="session-1", sequence=1)
    queued_cmd = replace(COMMAND, request_id="req-queued", session_id="session-1", sequence=2)
    started_cmd = replace(COMMAND, request_id="req-started", session_id="session-2", sequence=1)
    completed_resp = replace(RESPONSE, request_id="req-completed", session_id="session-1")
    pending_resp = DesktopResponse("req-pending", "session-3", False, error="denied")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE responses (
                delivery_id TEXT PRIMARY KEY, response TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE commands (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                command TEXT, state TEXT NOT NULL CHECK(state IN ('queued','started','completed')),
                delivery_id TEXT REFERENCES responses(delivery_id)
            );
            CREATE TABLE sequences (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL
            );
            PRAGMA user_version=1;
            """,
        )
        if controller_key is not None:
            db.execute("INSERT INTO metadata VALUES('controller',?)", (controller_key,))
        db.execute(
            "INSERT INTO responses(delivery_id,response,delivered) VALUES(?,?,1)",
            ("old-delivered-id", json.dumps(completed_resp.to_content(), sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO responses(delivery_id,response,delivered) VALUES(?,?,0)",
            ("old-pending-id", json.dumps(pending_resp.to_content(), sort_keys=True, separators=(",", ":"))),
        )
        db.execute(
            "INSERT INTO commands(request_id,fingerprint,command,state,delivery_id) VALUES(?,?,?,'completed',?)",
            (
                completed_cmd.request_id,
                FINGERPRINT,
                json.dumps(completed_cmd.to_content(), sort_keys=True, separators=(",", ":")),
                "old-delivered-id",
            ),
        )
        db.execute(
            "INSERT INTO commands(request_id,fingerprint,command,state) VALUES(?,?,?,'queued')",
            (
                queued_cmd.request_id,
                "b" * 64,
                json.dumps(queued_cmd.to_content(), sort_keys=True, separators=(",", ":")),
            ),
        )
        db.execute(
            "INSERT INTO commands(request_id,fingerprint,command,state) VALUES(?,?,?,'started')",
            (
                started_cmd.request_id,
                "c" * 64,
                json.dumps(started_cmd.to_content(), sort_keys=True, separators=(",", ":")),
            ),
        )
        db.execute("INSERT INTO sequences(session_id,sequence) VALUES('session-1',2)")
        db.execute("INSERT INTO sequences(session_id,sequence) VALUES('session-2',1)")
    path.chmod(0o600)
    return completed_cmd, queued_cmd, started_cmd, completed_resp, pending_resp


@pytest.mark.parametrize("stored_controller", ['["@ctrl-a:example.org", "DEV_A", "key-a"]', None])
def test_migrates_v1_journal_to_v2_and_preserves_receipts(tmp_path: Path, stored_controller: str | None) -> None:
    """Opening a user_version=1 database migrates atomically to v2 and preserves all controller receipts."""
    path = tmp_path / "commands.sqlite3"
    completed_cmd, queued_cmd, started_cmd, completed_resp, pending_resp = _write_v1_journal(path, stored_controller)
    expected_key = "" if stored_controller is None else stored_controller
    key_b = '["@ctrl-b:example.org", "DEV_B", "key-b"]'

    journal = DesktopCommandJournal.load(path, controller_key=expected_key)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    completed = journal.get(completed_cmd.request_id, expected_key)
    assert completed is not None
    assert completed.response == completed_resp
    assert completed.controller_key == expected_key
    assert [entry.command for entry in journal.queued(expected_key)] == [queued_cmd]
    assert [entry.command for entry in journal.started(expected_key)] == [started_cmd]
    pending = journal.pending_responses_with_controller()
    assert len(pending) == 1
    assert pending[0][1] == expected_key
    assert pending[0][2] == pending_resp
    assert journal.sequence_error(replace(queued_cmd, request_id="replay"), expected_key) is not None

    # Replaying the migrated completed receipt updates its migrated response row in place.
    journal.queue_response(completed_resp, expected_key)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 2

    # Controller B can independently admit the exact same request_id and session_id.
    assert journal.sequence_error(completed_cmd, key_b) is None
    journal.admit(completed_cmd, "d" * 64, key_b)
    entry_b = journal.get(completed_cmd.request_id, key_b)
    assert entry_b is not None
    assert entry_b.controller_key == key_b
    assert entry_b.command_fingerprint == "d" * 64
    journal.close()


def test_two_controllers_isolated_replay_and_sequences(tmp_path: Path) -> None:
    """Identical request_id, session_id, and response payloads stay isolated across two controllers."""
    key_a = controller_journal_key(PinnedMatrixDevice("@a:example.org", "DEV_A", "key-a"))
    key_b = controller_journal_key(PinnedMatrixDevice("@b:example.org", "DEV_B", "key-b"))
    journal = DesktopCommandJournal.load(tmp_path / "commands.sqlite3", controller_key=key_a)

    journal.admit(COMMAND, "a" * 64, key_a)
    journal.admit(COMMAND, "b" * 64, key_b)
    assert journal.get(COMMAND.request_id, key_a).command_fingerprint == "a" * 64
    assert journal.get(COMMAND.request_id, key_b).command_fingerprint == "b" * 64
    assert [entry.controller_key for entry in journal.queued(all_controllers=True)] == [key_a, key_b]

    # Advancing controller A's sequence does not block controller B on the same session_id.
    journal.admit(replace(COMMAND, request_id="req-a-5", sequence=5), "c" * 64, key_a)
    assert journal.sequence_error(replace(COMMAND, request_id="req-a-2", sequence=2), key_a) is not None
    assert journal.sequence_error(replace(COMMAND, request_id="req-b-2", sequence=2), key_b) is None

    journal.remember_started(COMMAND, "a" * 64, key_a)
    assert [entry.controller_key for entry in journal.started(all_controllers=True)] == [key_a]
    assert [entry.controller_key for entry in journal.queued(key_b)] == [key_b]

    # Identical DesktopResponse payloads for A and B get distinct delivery_ids and route independently.
    journal.remember_response(COMMAND, "a" * 64, RESPONSE, key_a)
    journal.remember_response(COMMAND, "b" * 64, RESPONSE, key_b)
    all_pending = journal.pending_responses_with_controller()
    assert len(all_pending) == 2
    (delivery_a, ctrl_a, resp_a), (delivery_b, ctrl_b, resp_b) = all_pending
    assert (ctrl_a, resp_a) == (key_a, RESPONSE)
    assert (ctrl_b, resp_b) == (key_b, RESPONSE)
    assert delivery_a != delivery_b

    journal.mark_delivered(delivery_a)
    assert journal.pending_responses(key_a) == []
    assert journal.pending_responses(key_b) == [(delivery_b, RESPONSE)]
    assert journal.pending_responses_with_controller() == [(delivery_b, key_b, RESPONSE)]
    journal.close()


def test_check_controller_binding_validates_schema_without_rejecting_other_controllers(tmp_path: Path) -> None:
    """Multi-controller journals accept any controller while rejecting corrupt or unsupported schemas."""
    ctrl_a = PinnedMatrixDevice("@a:example.org", "DEV_A", "key-a")
    ctrl_b = PinnedMatrixDevice("@b:example.org", "DEV_B", "key-b")
    path = desktop_journal_path(tmp_path)

    # Missing journal is allowed.
    check_controller_binding(tmp_path, ctrl_a)

    # Valid v1 journal accepts any controller without modifying bytes.
    _write_v1_journal(path, controller_journal_key(ctrl_a))
    v1_bytes = path.read_bytes()
    check_controller_binding(tmp_path, ctrl_a)
    check_controller_binding(tmp_path, ctrl_b)
    assert path.read_bytes() == v1_bytes

    # Valid v2 journal accepts any controller.
    DesktopCommandJournal.load(path, controller_key=controller_journal_key(ctrl_a)).close()
    check_controller_binding(tmp_path, ctrl_b)

    # Unsupported user_version fails closed.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(DesktopCommandJournalError, match="unsupported schema"):
        check_controller_binding(tmp_path, ctrl_b)

    # Corrupt file fails closed.
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(DesktopCommandJournalError):
        check_controller_binding(tmp_path, ctrl_a)


def test_failed_v1_migration_rolls_back_atomically(tmp_path: Path) -> None:
    """A corrupt v1 database with a dangling delivery_id rolls back cleanly without partial migration."""
    path = tmp_path / "commands.sqlite3"
    _write_v1_journal(path, "controller-a")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE commands SET delivery_id='missing-id' WHERE request_id='req-completed'")

    with pytest.raises(DesktopCommandJournalError, match="missing response"):
        DesktopCommandJournal.load(path, controller_key="controller-a")

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 3
