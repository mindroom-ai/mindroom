"""Tests for the store of live and recently ended calls, their lines, Pause, and app presence."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

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
        retry_at=None,
        notes_failures=0,
        transcript_done_at=None,
        transcript_event_id=None,
        row_edit=None,
        paused_since=None,
        paused_by=None,
    )
    assert other.id != first.id
    assert store.live_call(ROOM) == first
    assert store.call(first.id) == first
    assert sorted(call.id for call in store.calls()) == sorted([first.id, other.id])


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

    assert store.end_call(first.id, at=200.0)
    assert not store.end_call(first.id, at=300.0)
    assert not store.end_call("unknown", at=300.0)
    assert store.live_call(ROOM) is None
    ended = store.call(first.id)
    assert ended is not None
    assert ended.ended_at == 200.0

    second, started = store.ensure_live_call(ROOM, at=250.0)
    assert started
    assert second.id != first.id
    assert store.calls() == [ended, second]


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
    assert store.finish_transcript(call.id, at=160.0) is None
    assert len(store.lines(call.id)) == 1
    store.end_call(call.id, at=200.0)
    late = store.add_speech(call.id, user_id=BOB, said_at=198.0, seconds=1.5, text="Arrived late")

    assert late is not None
    assert [line.text for line in store.lines(call.id)] == ["Before the end", "Arrived late"]

    assert store.finish_transcript(call.id, at=225.0) == 3.5
    assert store.finish_transcript(call.id, at=230.0) is None

    assert store.lines(call.id) == []
    finished = store.call(call.id)
    assert finished is not None
    assert finished.transcript_done_at == 225.0
    assert store.add_speech(call.id, user_id=BOB, said_at=199.0, seconds=1.0, text="Too late") is None
    assert store.add_gap(call.id, user_id=BOB, said_at=199.0, seconds=1.0, reason="not-transcribed") is None
    assert store.add_speech("unknown", user_id=BOB, said_at=199.0, seconds=1.0, text="Nowhere") is None
    assert store.lines(call.id) == []


def test_a_calls_writes_wait_after_a_failure_and_its_failed_notes_are_counted(tmp_path: Path) -> None:
    """Postponing holds the call's writes until its new time; failed notes attempts are counted on their own."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(call.id, at=200.0)

    store.postpone(call.id, until=280.0)
    store.count_failed_notes(call.id)
    store.postpone(call.id, until=340.0)

    postponed = CallTranscriptStore(tmp_path).call(call.id)
    assert postponed is not None
    assert (postponed.retry_at, postponed.notes_failures) == (340.0, 1)


def test_finished_calls_are_forgotten_after_a_while(tmp_path: Path) -> None:
    """Only calls whose transcript was settled before the cutoff go; live and pending calls stay."""
    store = CallTranscriptStore(tmp_path)
    finished, _ = store.ensure_live_call(ROOM, at=100.0)
    store.end_call(finished.id, at=200.0)
    store.finish_transcript(finished.id, at=225.0)
    pending, _ = store.ensure_live_call(ROOM, at=230.0)
    store.end_call(pending.id, at=240.0)
    live, _ = store.ensure_live_call(OTHER_ROOM, at=250.0)

    assert store.forget_finished_calls(before=225.0, ended_before=1_000.0) == 0
    assert store.forget_finished_calls(before=300.0, ended_before=1_000.0) == 1

    assert store.call(finished.id) is None
    assert store.call(pending.id) is not None
    assert store.call(live.id) is not None


def test_a_finished_call_whose_row_is_still_owed_is_kept_until_it_is_made_or_given_up(tmp_path: Path) -> None:
    """A row not yet posted, or not yet saying where the transcript is, keeps its call until then or the give-up."""
    store = CallTranscriptStore(tmp_path)
    owing = []
    for room_id in (ROOM, OTHER_ROOM, "!third:example.org"):
        call, _ = store.ensure_live_call(room_id, at=100.0)
        if room_id != ROOM:
            store.set_row_event_id(call.id, "$row")
        store.end_call(call.id, at=200.0)
        store.finish_transcript(call.id, at=225.0, event_id="$transcript")
        store.record_row_edit(call.id, "ended")
        owing.append(call.id)

    assert store.forget_finished_calls(before=300.0, ended_before=200.0) == 0
    store.record_row_edit(owing[1], "transcript")
    assert store.forget_finished_calls(before=300.0, ended_before=200.0) == 1
    assert store.call(owing[1]) is None
    assert store.forget_finished_calls(before=300.0, ended_before=201.0) == 2
    assert store.calls() == []


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
    store.end_call(call.id, at=130.0)
    store.finish_transcript(call.id, at=151.0)
    store.forget_finished_calls(before=200.0, ended_before=200.0)
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

    store.end_call(call.id, at=130.0)

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
    store.end_call(call.id, at=110.0)

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


def test_an_edit_for_the_end_never_replaces_one_for_the_transcript(tmp_path: Path) -> None:
    """The row's last recorded edit is the latest it was sent: the transcript's, once that went out."""
    store = CallTranscriptStore(tmp_path)
    call, _ = store.ensure_live_call(ROOM, at=100.0)
    store.set_row_event_id(call.id, "$row")
    store.end_call(call.id, at=200.0)

    store.record_row_edit(call.id, "ended")
    store.finish_transcript(call.id, at=225.0, event_id="$transcript")
    store.record_row_edit(call.id, "transcript")
    store.record_row_edit(call.id, "ended")

    finished = CallTranscriptStore(tmp_path).call(call.id)
    assert finished is not None
    assert (finished.row_edit, finished.transcript_event_id) == ("transcript", "$transcript")
