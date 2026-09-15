"""Crash recovery and durable delivery for desktop commands."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.command_journal import DesktopCommandJournal, DesktopCommandJournalError
from mindroom.desktop.protocol import DesktopCommand, DesktopResponse

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


def test_journal_cannot_be_rebound_to_another_controller(tmp_path: Path) -> None:
    """Queued work and results stay bound to the original controller pin."""
    path = tmp_path / "commands.sqlite3"
    journal = DesktopCommandJournal.load(path, controller_key="controller-a")
    journal.admit(COMMAND, FINGERPRINT)
    journal.close()
    with pytest.raises(DesktopCommandJournalError, match="controller"):
        DesktopCommandJournal.load(path, controller_key="controller-b")


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
