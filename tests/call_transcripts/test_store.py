"""Tests for the store of live and recently ended calls, their lines, Pause, and app presence."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.call_transcripts import store as call_transcript_store
from mindroom.call_transcripts.store import CallTranscriptStore, StoredCall, StoredLine, _AppPresence

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ANN = "@ann:example.org"
BOB = "@bob:example.org"


def test_the_store_keeps_its_database_private_under_the_storage_root(tmp_path: Path) -> None:
    """Calls and their lines live in one database only MindRoom's user can read."""
    store = CallTranscriptStore(tmp_path)

    assert store.path == tmp_path / "call_transcripts" / "calls.db"
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def _schema_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def test_a_new_database_is_at_the_latest_schema_version(tmp_path: Path) -> None:
    """A fresh database records the version of the schema it was created with."""
    store = CallTranscriptStore(tmp_path)

    assert _schema_version(store.path) == len(call_transcript_store._UPGRADES) == 1


def test_reopening_a_database_from_before_a_schema_change_upgrades_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database at the previous version gets only the new step, once, and keeps its calls and presence."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.record_presence(call.id, ANN, at=100.0)
    monkeypatch.setattr(
        call_transcript_store,
        "_UPGRADES",
        (
            *call_transcript_store._UPGRADES,
            (
                "CREATE TABLE notes (call_id TEXT NOT NULL PRIMARY KEY)",
                "ALTER TABLE calls ADD COLUMN topic TEXT",
            ),
        ),
    )

    upgraded = CallTranscriptStore(tmp_path)
    CallTranscriptStore(tmp_path)  # A step that ran again would fail on its duplicate table and column.

    assert _schema_version(upgraded.path) == 2
    with closing(sqlite3.connect(upgraded.path)) as connection:
        assert "topic" in {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
        assert connection.execute("SELECT count(*) FROM notes").fetchone() == (0,)
    assert upgraded.call(call.id) == call
    assert upgraded.presence(call.id) == {ANN: _AppPresence(present_at=100.0, left=False)}


def test_a_database_from_a_newer_mindroom_is_refused(tmp_path: Path) -> None:
    """A schema this code does not know is never opened, so it cannot be written in an old shape."""
    store = CallTranscriptStore(tmp_path)
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="has schema version 2, newer than this MindRoom's 1"):
        CallTranscriptStore(tmp_path)
    assert _schema_version(store.path) == 2


def test_a_room_has_at_most_one_live_call(tmp_path: Path) -> None:
    """Starting a call where one is live gives the live call; another room gets its own."""
    store = CallTranscriptStore(tmp_path)

    first, started = store.ensure_live_call(ROOM, at=100.0)
    again, started_again = store.ensure_live_call(ROOM, at=105.0)
    other, _ = store.ensure_live_call(OTHER_ROOM, at=106.0)

    assert started
    assert not started_again
    assert again == first
    assert first == StoredCall(
        id=first.id,
        room_id=ROOM,
        started_at=100.0,
        ended_at=None,
        row_event_id=None,
        transcript_due_at=None,
        transcript_attempts=0,
        transcript_done_at=None,
        transcript_event_id=None,
        row_edit=None,
        paused_since=None,
        paused_by=None,
    )
    assert other.id != first.id
    assert store.live_call(ROOM) == first
    assert store.call(first.id) == first
    assert sorted(call.id for call in store.live_calls()) == sorted([first.id, other.id])


def test_the_database_refuses_a_second_live_call_in_a_room(tmp_path: Path) -> None:
    """One live call per room holds even for a write that skips the store's own check."""
    store = CallTranscriptStore(tmp_path)
    store.ensure_live_call(ROOM, at=100.0)

    with closing(sqlite3.connect(store.path)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO calls (id, room_id, started_at) VALUES ('second', ?, 101.0)", (ROOM,))


def test_an_ended_call_makes_way_for_the_next_one(tmp_path: Path) -> None:
    """A call ends once, its transcript becomes due, and the room's next call is a new one."""
    store = CallTranscriptStore(tmp_path)
    first, _ = store.ensure_live_call(ROOM, at=100.0)

    assert store.end_call(first.id, at=200.0, transcript_due_at=220.0)
    assert not store.end_call(first.id, at=300.0, transcript_due_at=320.0)
    assert not store.end_call("unknown", at=300.0, transcript_due_at=320.0)
    assert store.live_call(ROOM) is None
    ended = store.call(first.id)
    assert ended is not None
    assert (ended.ended_at, ended.transcript_due_at) == (200.0, 220.0)

    second, started = store.ensure_live_call(ROOM, at=250.0)
    assert started
    assert second.id != first.id
    assert store.live_calls() == [second]


def test_the_call_row_is_remembered(tmp_path: Path) -> None:
    """The call's row is recorded once it is posted."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)

    store.set_row_event_id(call.id, "$row")

    assert store.live_call(ROOM) == replace(call, row_event_id="$row")
    assert CallTranscriptStore(tmp_path).call(call.id) == replace(call, row_event_id="$row")


def test_lines_are_read_in_the_order_they_were_added_all_or_those_after_one(tmp_path: Path) -> None:
    """A poll gets the lines added after the last one it has, whenever they were said; an unknown line gets all."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)

    later = store.add_speech(call.id, user_id=ANN, said_at=110.0, seconds=2.0, text="Second")
    earlier = store.add_speech(call.id, user_id=BOB, said_at=105.0, seconds=3.5, text="First")
    gap = store.add_gap(call.id, user_id=ANN, said_at=110.0, seconds=4.0, reason="paused")

    assert earlier is not None
    assert earlier == StoredLine(
        id=earlier.id,
        call_id=call.id,
        user_id=BOB,
        said_at=105.0,
        seconds=3.5,
        kind="speech",
        text="First",
        reason=None,
    )
    assert later is not None
    assert gap is not None
    assert (gap.kind, gap.text, gap.reason) == ("gap", None, "paused")
    assert store.lines(call.id) == [later, earlier, gap]
    assert len({line.id for line in store.lines(call.id)}) == 3
    assert store.lines(call.id, after=later.id) == [earlier, gap]
    assert store.lines(call.id, after=gap.id) == []
    assert store.lines(call.id, after="unknown") == [later, earlier, gap]
    other, _ = store.ensure_live_call(OTHER_ROOM, at=100.0)
    elsewhere = store.add_speech(other.id, user_id=ANN, said_at=101.0, seconds=1.0, text="Elsewhere")
    assert elsewhere is not None
    assert store.lines(call.id, after=elsewhere.id) == [later, earlier, gap]
    assert store.lines("unknown") == []


def test_posting_the_transcript_deletes_the_lines_and_closes_the_call(tmp_path: Path) -> None:
    """Late speech is kept until the transcript is posted; then the lines go and nothing more is accepted."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.add_speech(call.id, user_id=ANN, said_at=150.0, seconds=2.0, text="Before the end")
    assert not store.finish_transcript(call.id, at=160.0)
    assert len(store.lines(call.id)) == 1
    store.end_call(call.id, at=200.0, transcript_due_at=220.0)
    late = store.add_speech(call.id, user_id=BOB, said_at=198.0, seconds=1.5, text="Arrived late")

    assert late is not None
    assert store.due_transcripts(now=219.9) == []
    [due] = store.due_transcripts(now=220.0)
    assert due.id == call.id
    assert [line.text for line in store.lines(call.id)] == ["Before the end", "Arrived late"]

    assert store.finish_transcript(call.id, at=225.0)
    assert not store.finish_transcript(call.id, at=230.0)

    assert store.lines(call.id) == []
    assert store.due_transcripts(now=1_000.0) == []
    finished = store.call(call.id)
    assert finished is not None
    assert (finished.transcript_due_at, finished.transcript_done_at) == (None, 225.0)
    assert store.add_speech(call.id, user_id=BOB, said_at=199.0, seconds=1.0, text="Too late") is None
    assert store.add_gap(call.id, user_id=BOB, said_at=199.0, seconds=1.0, reason="not-transcribed") is None
    assert store.add_speech("unknown", user_id=BOB, said_at=199.0, seconds=1.0, text="Nowhere") is None
    assert store.lines(call.id) == []


def test_a_failed_transcript_is_due_again_later(tmp_path: Path) -> None:
    """A postponed transcript counts its failed attempt and is due again at its new time."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(call.id, at=200.0, transcript_due_at=220.0)

    store.postpone_transcript(call.id, due_at=280.0)

    assert store.due_transcripts(now=279.0) == []
    [due] = store.due_transcripts(now=280.0)
    assert (due.transcript_attempts, due.transcript_due_at) == (1, 280.0)


def test_the_next_transcript_due_is_known(tmp_path: Path) -> None:
    """The soonest due time among pending transcript messages, none once all are done."""
    store = CallTranscriptStore(tmp_path)
    assert store.next_transcript_due_at() is None
    first, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(first.id, at=200.0, transcript_due_at=220.0)
    second, _ = store.ensure_live_call(OTHER_ROOM, at=100.0)
    store.end_call(second.id, at=190.0, transcript_due_at=210.0)

    assert store.next_transcript_due_at() == 210.0
    store.finish_transcript(second.id, at=211.0)
    assert store.next_transcript_due_at() == 220.0
    store.finish_transcript(first.id, at=221.0)
    assert store.next_transcript_due_at() is None


def test_finished_calls_are_forgotten_after_a_while(tmp_path: Path) -> None:
    """Only calls whose transcript was settled before the cutoff go; live and pending calls stay."""
    store = CallTranscriptStore(tmp_path)
    finished, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(finished.id, at=200.0, transcript_due_at=220.0)
    store.finish_transcript(finished.id, at=225.0)
    pending, _ = store.ensure_live_call(ROOM, at=230.0)
    store.end_call(pending.id, at=240.0, transcript_due_at=260.0)
    live, _ = store.ensure_live_call(OTHER_ROOM, at=250.0)

    assert store.forget_finished_calls(before=225.0) == 0
    assert store.forget_finished_calls(before=300.0) == 1

    assert store.call(finished.id) is None
    assert store.call(pending.id) is not None
    assert store.call(live.id) is not None


def test_app_presence_is_kept_for_each_call_across_reopening_until_it_is_forgotten(tmp_path: Path) -> None:
    """When each person's app last checked in to each call survives a restart, and goes with its call."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    other, _ = store.ensure_live_call(OTHER_ROOM, at=100.0)
    store.record_presence(call.id, ANN, at=100.0)
    store.record_presence(call.id, ANN, at=120.0)
    store.record_presence(call.id, BOB, at=110.0, left=True)
    store.record_presence(other.id, ANN, at=90.0)

    reopened = CallTranscriptStore(tmp_path)

    assert reopened.presence(call.id) == {
        ANN: _AppPresence(present_at=120.0, left=False),
        BOB: _AppPresence(present_at=110.0, left=True),
    }
    assert reopened.presence(other.id) == {ANN: _AppPresence(present_at=90.0, left=False)}
    assert reopened.presence("unknown") == {}
    store.end_call(call.id, at=130.0, transcript_due_at=150.0)
    store.finish_transcript(call.id, at=151.0)
    store.forget_finished_calls(before=200.0)
    assert store.presence(call.id) == {}
    assert store.presence(other.id) == {ANN: _AppPresence(present_at=90.0, left=False)}


def test_an_older_check_in_does_not_overwrite_a_newer_one(tmp_path: Path) -> None:
    """A request that was overtaken (a heartbeat after the leave it preceded) changes nothing."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.record_presence(call.id, ANN, at=120.0, left=True)

    store.record_presence(call.id, ANN, at=115.0)
    assert store.presence(call.id) == {ANN: _AppPresence(present_at=120.0, left=True)}

    store.record_presence(call.id, ANN, at=130.0)
    assert store.presence(call.id) == {ANN: _AppPresence(present_at=130.0, left=False)}


def test_a_pause_runs_from_pausing_to_resuming_and_is_one_gap(tmp_path: Path) -> None:
    """Pausing a paused call changes nothing; resuming writes one gap under whoever paused, from when they did."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)

    store.set_pause(call.id, ANN, at=110.0)
    store.set_pause(call.id, BOB, at=112.0)
    paused = store.call(call.id)
    assert paused is not None
    assert (paused.paused_since, paused.paused_by) == (110.0, ANN)

    store.set_pause(call.id, None, at=120.0)

    [gap] = store.lines(call.id)
    assert (gap.user_id, gap.said_at, gap.seconds, gap.kind, gap.reason) == (ANN, 110.0, 10.0, "gap", "paused")
    assert store.call(call.id) == call
    store.set_pause(call.id, None, at=125.0)
    store.set_pause(call.id, BOB, at=130.0)
    store.set_pause(call.id, None, at=131.0)
    next_gap = store.lines(call.id)[-1]
    assert (next_gap.user_id, next_gap.said_at, next_gap.seconds) == (BOB, 130.0, 1.0)
    assert len(store.lines(call.id)) == 2


def test_ending_a_call_ends_its_pause(tmp_path: Path) -> None:
    """A call paused when it ends gets its pause's gap, up to the end, and is no longer paused."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.set_pause(call.id, ANN, at=110.0)

    store.end_call(call.id, at=130.0, transcript_due_at=150.0)

    [gap] = store.lines(call.id)
    assert (gap.user_id, gap.said_at, gap.seconds) == (ANN, 110.0, 20.0)
    ended = store.call(call.id)
    assert ended is not None
    assert (ended.paused_since, ended.paused_by) == (None, None)


def test_a_pause_is_kept_across_reopening(tmp_path: Path) -> None:
    """A restart keeps who paused and when."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.set_pause(call.id, ANN, at=110.0)

    reopened = CallTranscriptStore(tmp_path)

    assert reopened.call(call.id) == replace(call, paused_since=110.0, paused_by=ANN)
    reopened.set_pause(call.id, None, at=150.0)
    [gap] = reopened.lines(call.id)
    assert (gap.said_at, gap.seconds) == (110.0, 40.0)


def test_only_a_live_call_can_be_paused(tmp_path: Path) -> None:
    """An ended or unknown call is never paused."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(call.id, at=110.0, transcript_due_at=130.0)

    store.set_pause(call.id, ANN, at=111.0)
    store.set_pause("unknown", ANN, at=111.0)

    paused = store.call(call.id)
    assert paused is not None
    assert paused.paused_by is None


@pytest.mark.parametrize(
    ("start", "end", "overlaps"),
    [
        (100.0, 109.0, False),
        (100.0, 110.0, False),
        (100.0, 110.5, True),
        (112.0, 113.0, True),
        (119.5, 125.0, True),
        (120.0, 125.0, False),
        (105.0, 125.0, True),
    ],
    ids=["before", "ends-as-it-begins", "into-it", "inside", "out-of-it", "begins-as-it-ends", "across"],
)
def test_a_stretch_overlapping_a_past_pause_is_found(tmp_path: Path, start: float, end: float, overlaps: bool) -> None:
    """A stretch said during any part of a past pause overlaps it; one touching its ends does not."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.set_pause(call.id, ANN, at=110.0)
    store.set_pause(call.id, None, at=120.0)
    store.add_gap(call.id, user_id=BOB, said_at=130.0, seconds=5.0, reason="not-transcribed")

    assert store.overlaps_pause(call.id, start=start, end=end) is overlaps
    assert not store.overlaps_pause(call.id, start=131.0, end=134.0)


def test_an_ended_calls_row_owes_its_end_and_then_its_transcript(tmp_path: Path) -> None:
    """A posted row owes an edit once its call ended, and another once the transcript message is posted."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    without_row, _ = store.ensure_live_call(OTHER_ROOM, at=100.0)
    store.set_row_event_id(call.id, "$row")
    assert store.rows_owing_edits() == []

    store.end_call(call.id, at=200.0, transcript_due_at=220.0)
    store.end_call(without_row.id, at=200.0, transcript_due_at=220.0)
    [owing] = store.rows_owing_edits()
    assert (owing.id, owing.row_edit, owing.transcript_event_id) == (call.id, None, None)

    store.record_row_edit(call.id, "ended")
    assert CallTranscriptStore(tmp_path).rows_owing_edits() == []
    store.finish_transcript(call.id, at=225.0, event_id="$transcript")
    [owing] = store.rows_owing_edits()
    assert (owing.row_edit, owing.transcript_event_id) == ("ended", "$transcript")

    store.record_row_edit(call.id, "transcript")
    store.record_row_edit(call.id, "ended")
    finished = CallTranscriptStore(tmp_path).call(call.id)
    assert finished is not None
    assert (finished.row_edit, finished.transcript_event_id) == ("transcript", "$transcript")
    assert store.rows_owing_edits() == []


def test_a_call_with_nothing_to_post_owes_only_its_end(tmp_path: Path) -> None:
    """With no transcript message, the row's edit for the end is the last it owes."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.set_row_event_id(call.id, "$row")
    store.end_call(call.id, at=200.0, transcript_due_at=220.0)
    store.finish_transcript(call.id, at=221.0)

    [owing] = store.rows_owing_edits()
    assert owing.transcript_event_id is None
    store.record_row_edit(call.id, "ended")
    assert store.rows_owing_edits() == []
