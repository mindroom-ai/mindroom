"""The SQLite store of calls between people, their lines, whose apps were in them, Pause, and app check-ins.

A call's lines are kept only until its transcript message is posted; after that the call keeps only what stops the
message, or an edit of its row, being posted twice, until it is forgotten. Times are seconds since the epoch. Every
method is one short transaction, so callers on the event loop run it in a thread.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator
    from pathlib import Path

type _LineKind = Literal["speech", "gap"]
type _GapReason = Literal["paused", "not-transcribed"]
# What an ended call's row was last edited to say: that the call ended, then also where its transcript message is.
type RowEdit = Literal["ended", "transcript"]

# Each step upgrades the database from the version that is its index to the next; ``PRAGMA user_version`` records the
# version a database is at. A schema change appends a step and never edits one a released database may have run.
_UPGRADES: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE calls (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            row_event_id TEXT,
            transcript_due_at REAL,
            transcript_attempts INTEGER NOT NULL DEFAULT 0,
            transcript_done_at REAL
        )
        """,
        "CREATE UNIQUE INDEX calls_one_live_call_per_room ON calls(room_id) WHERE ended_at IS NULL",
        """
        CREATE TABLE lines (
            id TEXT PRIMARY KEY,
            call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            said_at REAL NOT NULL,
            seconds REAL NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            reason TEXT,
            CHECK (
                (kind = 'speech' AND text IS NOT NULL AND reason IS NULL)
                OR (kind = 'gap' AND text IS NULL AND reason IN ('paused', 'not-transcribed'))
            )
        )
        """,
        "CREATE INDEX lines_by_call ON lines(call_id, said_at)",
        """
        CREATE TABLE app_presence (
            room_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            present_at REAL NOT NULL,
            has_left INTEGER NOT NULL,
            PRIMARY KEY (room_id, user_id)
        )
        """,
    ),
    (
        """
        CREATE TABLE holds (
            call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            since REAL NOT NULL,
            PRIMARY KEY (call_id, user_id)
        )
        """,
        "ALTER TABLE calls ADD COLUMN paused_since REAL",
        "ALTER TABLE calls ADD COLUMN paused_by TEXT",
        """
        CREATE TABLE call_apps (
            call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            PRIMARY KEY (call_id, user_id)
        )
        """,
        "ALTER TABLE calls ADD COLUMN transcript_event_id TEXT",
        "ALTER TABLE calls ADD COLUMN row_edit TEXT",
    ),
)


@dataclass(frozen=True, slots=True)
class StoredCall:
    """One call in a room, from its start until it is forgotten after its transcript message."""

    id: str
    room_id: str
    started_at: float
    ended_at: float | None
    """None while the call is live."""
    row_event_id: str | None
    """The call's row notice, None until it is posted."""
    transcript_due_at: float | None
    """When the transcript message is next due: None while the call is live and once the message is done."""
    transcript_attempts: int
    """How many times posting the transcript message has failed."""
    transcript_done_at: float | None
    """When the transcript message was posted, or found to have nothing to post; its lines are gone from then on."""
    transcript_event_id: str | None
    """The posted transcript message, None until it is posted or when there was nothing to post."""
    row_edit: RowEdit | None
    """What the row was last edited to say, None while it is unedited."""


@dataclass(frozen=True, slots=True)
class StoredLine:
    """One stretch of speech, or one gap, in a call's transcript."""

    id: str
    call_id: str
    user_id: str
    said_at: float
    seconds: float
    kind: _LineKind
    text: str | None
    """What was said, for speech."""
    reason: _GapReason | None
    """Why nothing was transcribed, for a gap."""


@dataclass(frozen=True, slots=True)
class AppPresence:
    """When one person's app was last in a call in a room, and whether it left then."""

    present_at: float
    left: bool


class CallTranscriptStore:
    """Calls, their lines and app presence in ``<storage_root>/call_transcripts/calls.db``."""

    def __init__(self, storage_root: Path) -> None:
        self.path = storage_root / "call_transcripts" / "calls.db"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with self._transaction() as connection:
            _upgrade(connection, self.path)
        self.path.chmod(0o600)

    def ensure_live_call(self, room_id: str, *, at: float) -> tuple[StoredCall, bool]:
        """Return the room's live call, starting one at ``at`` if there is none, and whether it started now."""
        with self._transaction() as connection:
            live = _live_call(connection, room_id)
            if live is not None:
                return live, False
            call = StoredCall(
                id=uuid.uuid4().hex,
                room_id=room_id,
                started_at=at,
                ended_at=None,
                row_event_id=None,
                transcript_due_at=None,
                transcript_attempts=0,
                transcript_done_at=None,
                transcript_event_id=None,
                row_edit=None,
            )
            connection.execute("INSERT INTO calls (id, room_id, started_at) VALUES (?, ?, ?)", (call.id, room_id, at))
            return call, True

    def live_call(self, room_id: str) -> StoredCall | None:
        """Return the room's live call, if any."""
        with closing(self._connect()) as connection:
            return _live_call(connection, room_id)

    def live_calls(self) -> list[StoredCall]:
        """Return every live call, oldest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM calls WHERE ended_at IS NULL ORDER BY started_at").fetchall()
        return [_call(row) for row in rows]

    def call(self, call_id: str) -> StoredCall | None:
        """Return one call, live or ended, unless it was forgotten."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return None if row is None else _call(row)

    def end_call(self, call_id: str, *, at: float, transcript_due_at: float) -> bool:
        """End a live call at ``at`` with its transcript message due at ``transcript_due_at``; False if not live."""
        return self._write(
            "UPDATE calls SET ended_at = ?, transcript_due_at = ? WHERE id = ? AND ended_at IS NULL",
            (at, transcript_due_at, call_id),
        )

    def set_row_event_id(self, call_id: str, event_id: str) -> None:
        """Record the event ID of the call's posted row."""
        self._write("UPDATE calls SET row_event_id = ? WHERE id = ?", (event_id, call_id))

    def due_transcripts(self, *, now: float) -> list[StoredCall]:
        """Return the ended calls whose transcript message is due by ``now``, the longest due first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM calls WHERE transcript_due_at <= ? ORDER BY transcript_due_at",
                (now,),
            ).fetchall()
        return [_call(row) for row in rows]

    def next_transcript_due_at(self) -> float | None:
        """Return when the soonest pending transcript message is due, None if none is pending."""
        with closing(self._connect()) as connection:
            (due_at,) = connection.execute("SELECT min(transcript_due_at) FROM calls").fetchone()
        return due_at

    def postpone_transcript(self, call_id: str, *, due_at: float, failed: bool = True) -> None:
        """Make the call's transcript message due again at ``due_at``, counting a failed attempt unless ``failed`` is False."""
        self._write(
            "UPDATE calls SET transcript_due_at = ?, transcript_attempts = transcript_attempts + ?"
            " WHERE id = ? AND transcript_due_at IS NOT NULL",
            (due_at, int(failed), call_id),
        )

    def finish_transcript(self, call_id: str, *, at: float, event_id: str | None = None) -> bool:
        """Mark an ended call's transcript message done at ``at``, posted as ``event_id``, and delete its lines.

        ``event_id`` is None when there was nothing to post. Returns False if the message was not pending.
        """
        with self._transaction() as connection:
            finished = connection.execute(
                "UPDATE calls SET transcript_due_at = NULL, transcript_done_at = ?, transcript_event_id = ?"
                " WHERE id = ? AND ended_at IS NOT NULL AND transcript_done_at IS NULL",
                (at, event_id, call_id),
            ).rowcount
            if finished:
                connection.execute("DELETE FROM lines WHERE call_id = ?", (call_id,))
        return finished == 1

    def rows_owing_edits(self) -> list[StoredCall]:
        """Return the ended calls whose posted row does not yet say what it should: the end, then the transcript."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM calls WHERE ended_at IS NOT NULL AND row_event_id IS NOT NULL"
                " AND (row_edit IS NULL OR (row_edit = 'ended' AND transcript_event_id IS NOT NULL))"
                " ORDER BY ended_at",
            ).fetchall()
        return [_call(row) for row in rows]

    def record_row_edit(self, call_id: str, edit: RowEdit) -> None:
        """Record what the call's row was edited to say; an edit for the end never replaces one for the transcript."""
        self._write(
            "UPDATE calls SET row_edit = ? WHERE id = ? AND (row_edit IS NULL OR ? = 'transcript')",
            (edit, call_id, edit),
        )

    def forget_finished_calls(self, *, before: float) -> int:
        """Forget the calls whose transcript message was done before ``before``; return how many."""
        with closing(self._connect()) as connection:
            return connection.execute("DELETE FROM calls WHERE transcript_done_at < ?", (before,)).rowcount

    def add_speech(
        self,
        call_id: str,
        *,
        user_id: str,
        said_at: float,
        seconds: float,
        text: str,
    ) -> StoredLine | None:
        """Add what ``user_id`` said, unless the call's transcript message is done or the call is gone."""
        return self._add_line(
            StoredLine(
                id=uuid.uuid4().hex,
                call_id=call_id,
                user_id=user_id,
                said_at=said_at,
                seconds=seconds,
                kind="speech",
                text=text,
                reason=None,
            ),
        )

    def add_gap(
        self,
        call_id: str,
        *,
        user_id: str,
        said_at: float,
        seconds: float,
        reason: _GapReason,
    ) -> StoredLine | None:
        """Add a stretch nothing was transcribed for, unless the call's transcript message is done or it is gone."""
        return self._add_line(
            StoredLine(
                id=uuid.uuid4().hex,
                call_id=call_id,
                user_id=user_id,
                said_at=said_at,
                seconds=seconds,
                kind="gap",
                text=None,
                reason=reason,
            ),
        )

    def record_call_app(self, call_id: str, user_id: str) -> None:
        """Record that ``user_id``'s app was in the call."""
        self._write("INSERT OR IGNORE INTO call_apps (call_id, user_id) VALUES (?, ?)", (call_id, user_id))

    def call_apps(self, call_id: str) -> frozenset[str]:
        """Return the people whose apps were in the call at any time."""
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT user_id FROM call_apps WHERE call_id = ?", (call_id,)).fetchall()
        return frozenset(row["user_id"] for row in rows)

    def take_hold(self, call_id: str, user_id: str, *, at: float) -> bool:
        """Record that ``user_id`` holds Pause in a live call from ``at``, beginning a pause if none is on.

        Returns False if they already hold it or the call is not live.
        """
        with self._transaction() as connection:
            live = connection.execute("SELECT 1 FROM calls WHERE id = ? AND ended_at IS NULL", (call_id,)).fetchone()
            if live is None:
                return False
            added = connection.execute(
                "INSERT OR IGNORE INTO holds (call_id, user_id, since) VALUES (?, ?, ?)",
                (call_id, user_id, at),
            ).rowcount
            if added:
                connection.execute(
                    "UPDATE calls SET paused_since = ?, paused_by = ? WHERE id = ? AND paused_since IS NULL",
                    (at, user_id, call_id),
                )
        return added == 1

    def release_holds(
        self,
        call_id: str,
        *,
        at: float,
        user_ids: Collection[str] | None = None,
    ) -> StoredLine | None:
        """Release the holds of ``user_ids`` (everyone's when None) at ``at``.

        When that leaves nobody holding Pause, the pause ends: its one ``paused`` gap, from its first hold to ``at``
        under the person who began it, is added in the same transaction and returned.
        """
        with self._transaction() as connection:
            if user_ids is None:
                connection.execute("DELETE FROM holds WHERE call_id = ?", (call_id,))
            else:
                connection.executemany(
                    "DELETE FROM holds WHERE call_id = ? AND user_id = ?",
                    [(call_id, user_id) for user_id in user_ids],
                )
            if connection.execute("SELECT 1 FROM holds WHERE call_id = ?", (call_id,)).fetchone() is not None:
                return None
            pause = connection.execute(
                "SELECT paused_since, paused_by FROM calls WHERE id = ? AND paused_since IS NOT NULL",
                (call_id,),
            ).fetchone()
            if pause is None:
                return None
            connection.execute("UPDATE calls SET paused_since = NULL, paused_by = NULL WHERE id = ?", (call_id,))
            gap = StoredLine(
                id=uuid.uuid4().hex,
                call_id=call_id,
                user_id=pause["paused_by"],
                said_at=pause["paused_since"],
                seconds=max(0.0, at - pause["paused_since"]),
                kind="gap",
                text=None,
                reason="paused",
            )
            return gap if _insert_line(connection, gap) else None

    def holds(self, call_id: str) -> list[str]:
        """Return who holds Pause in the call, the longest held first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT user_id FROM holds WHERE call_id = ? ORDER BY since, user_id",
                (call_id,),
            ).fetchall()
        return [row["user_id"] for row in rows]

    def overlaps_pause(self, call_id: str, *, start: float, end: float) -> bool:
        """Return whether a stretch said from ``start`` to ``end`` overlaps one of the call's ended pauses."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM lines WHERE call_id = ? AND kind = 'gap' AND reason = 'paused'"
                " AND said_at < ? AND said_at + seconds > ?",
                (call_id, end, start),
            ).fetchone()
        return row is not None

    def lines(self, call_id: str) -> list[StoredLine]:
        """Return the call's lines in the order they were said, ties in the order they were added."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM lines WHERE call_id = ? ORDER BY said_at, rowid",
                (call_id,),
            ).fetchall()
        return [_line(row) for row in rows]

    def record_app_presence(self, room_id: str, user_id: str, *, at: float, left: bool = False) -> None:
        """Record that ``user_id``'s app was in a call in the room at ``at``, and whether it left then.

        A record older than the one kept changes nothing, so an overtaken request cannot undo a later one.
        """
        self._write(
            "INSERT INTO app_presence (room_id, user_id, present_at, has_left) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (room_id, user_id) DO UPDATE"
            " SET present_at = excluded.present_at, has_left = excluded.has_left"
            " WHERE excluded.present_at >= app_presence.present_at",
            (room_id, user_id, at, left),
        )

    def app_presence(self, room_id: str) -> dict[str, AppPresence]:
        """Return when each person's app was last in a call in the room, by user ID."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT user_id, present_at, has_left FROM app_presence WHERE room_id = ?",
                (room_id,),
            ).fetchall()
        return {row["user_id"]: AppPresence(present_at=row["present_at"], left=bool(row["has_left"])) for row in rows}

    def _add_line(self, line: StoredLine) -> StoredLine | None:
        with closing(self._connect()) as connection:
            return line if _insert_line(connection, line) else None

    def _write(self, statement: str, parameters: tuple[object, ...]) -> bool:
        """Run one statement as its own transaction; return whether it changed a row."""
        with closing(self._connect()) as connection:
            return connection.execute(statement, parameters).rowcount > 0

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the block as one write transaction, committing when it exits normally."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        # Do not enable WAL: runtime storage may use network filesystems or single-file backups.
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _upgrade(connection: sqlite3.Connection, path: Path) -> None:
    """Bring the database to the latest schema, refusing one a newer MindRoom wrote."""
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    if version > len(_UPGRADES):
        msg = f"{path} has schema version {version}, newer than this MindRoom's {len(_UPGRADES)}."
        raise RuntimeError(msg)
    for statements in _UPGRADES[version:]:
        for statement in statements:
            connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {len(_UPGRADES)}")


def _insert_line(connection: sqlite3.Connection, line: StoredLine) -> bool:
    """Add a line unless its call's transcript message is done or the call is gone; return whether it was added."""
    return (
        connection.execute(
            "INSERT INTO lines (id, call_id, user_id, said_at, seconds, kind, text, reason)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, ?"
            " WHERE EXISTS (SELECT 1 FROM calls WHERE id = ? AND transcript_done_at IS NULL)",
            (
                line.id,
                line.call_id,
                line.user_id,
                line.said_at,
                line.seconds,
                line.kind,
                line.text,
                line.reason,
                line.call_id,
            ),
        ).rowcount
        > 0
    )


def _live_call(connection: sqlite3.Connection, room_id: str) -> StoredCall | None:
    row = connection.execute("SELECT * FROM calls WHERE room_id = ? AND ended_at IS NULL", (room_id,)).fetchone()
    return None if row is None else _call(row)


def _call(row: sqlite3.Row) -> StoredCall:
    return StoredCall(
        id=row["id"],
        room_id=row["room_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        row_event_id=row["row_event_id"],
        transcript_due_at=row["transcript_due_at"],
        transcript_attempts=row["transcript_attempts"],
        transcript_done_at=row["transcript_done_at"],
        transcript_event_id=row["transcript_event_id"],
        row_edit=cast("RowEdit | None", row["row_edit"]),
    )


def _line(row: sqlite3.Row) -> StoredLine:
    return StoredLine(
        id=row["id"],
        call_id=row["call_id"],
        user_id=row["user_id"],
        said_at=row["said_at"],
        seconds=row["seconds"],
        kind=cast("_LineKind", row["kind"]),
        text=row["text"],
        reason=cast("_GapReason | None", row["reason"]),
    )
