"""Durable desktop admission, execution receipts, and response delivery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.desktop.legacy_command_journal import parse_legacy_records
from mindroom.desktop.protocol import DesktopCommand, DesktopResponse

if TYPE_CHECKING:
    from pathlib import Path

_MAX_ENTRIES = 1024
_MAX_SESSIONS = 128


class DesktopCommandJournalError(RuntimeError):
    """The desktop journal cannot safely admit or recover work."""


class DesktopCommandJournalFullError(DesktopCommandJournalError):
    """Execution or delivery must progress before more work is admitted."""


def check_controller_binding(path: Path, controller_key: str) -> None:
    """Reject configuration incompatible with an existing journal without changing it."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        msg = "Desktop journal must be a regular file."
        raise DesktopCommandJournalError(msg)
    _require_private(path)
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as database:
            existing = database.execute("SELECT value FROM metadata WHERE key='controller'").fetchone()
    except sqlite3.DatabaseError as exc:
        msg = "Desktop journal controller binding could not be read."
        raise DesktopCommandJournalError(msg) from exc
    if existing is None or existing[0] != controller_key:
        msg = "Desktop journal belongs to a different controller."
        raise DesktopCommandJournalError(msg)


@dataclass(frozen=True, slots=True)
class DesktopCommandJournalEntry:
    """Immutable command identity and its durable execution state."""

    command_fingerprint: str
    response: DesktopResponse | None
    state: Literal["queued", "started", "completed"]
    command: DesktopCommand | None


@dataclass
class DesktopCommandJournal:
    """Bounded SQLite inbox/outbox plus at most 1024 fixed bodyless legacy receipts.

    Legacy receipts cannot execute or consume new admission slots. Their missing
    command bodies are never reconstructed; exact replay can only record an outcome.
    """

    path: Path | None
    _database: sqlite3.Connection
    _max_entries: int = _MAX_ENTRIES

    @classmethod
    def load(
        cls,
        path: Path | None,
        *,
        controller_key: str = "",
        legacy_path: Path | None = None,
        max_entries: int = _MAX_ENTRIES,
    ) -> DesktopCommandJournal:
        """Open a journal and import existing replay receipts once."""
        if max_entries < 1:
            msg = "Desktop journal capacity must be positive."
            raise ValueError(msg)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                path.parent.chmod(0o700)
            _require_private(path)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
        database = sqlite3.connect(str(path) if path is not None else ":memory:")
        journal = cls(path, database, max_entries)
        try:
            journal._initialize(controller_key)
            if legacy_path is not None:
                journal._import_legacy(legacy_path)
        except BaseException:
            database.close()
            raise
        return journal

    def close(self) -> None:
        """Close after the executor and sender have stopped."""
        self._database.close()

    def _initialize(self, controller_key: str) -> None:
        if self._database.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
            msg = "Desktop journal has an unsupported schema."
            raise DesktopCommandJournalError(msg)
        self._database.execute("PRAGMA synchronous=FULL")
        self._database.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS responses (
                delivery_id TEXT PRIMARY KEY, response TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS commands (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                command TEXT, state TEXT NOT NULL CHECK(state IN ('queued','started','completed')),
                delivery_id TEXT REFERENCES responses(delivery_id)
            );
            CREATE TABLE IF NOT EXISTS sequences (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE, sequence INTEGER NOT NULL
            );
            PRAGMA user_version=1;
        """)
        with self._database:
            existing = self._database.execute("SELECT value FROM metadata WHERE key='controller'").fetchone()
            if existing is not None and existing[0] != controller_key:
                msg = "Desktop journal belongs to a different controller."
                raise DesktopCommandJournalError(msg)
            self._database.execute("INSERT OR IGNORE INTO metadata VALUES('controller',?)", (controller_key,))

    def get(self, request_id: str) -> DesktopCommandJournalEntry | None:
        """Read original identity without changing replay order."""
        row = self._database.execute(
            "SELECT c.fingerprint,r.response,c.state,c.command FROM commands c "
            "LEFT JOIN responses r USING(delivery_id) WHERE c.request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return DesktopCommandJournalEntry(
            row[0],
            DesktopResponse.from_content(json.loads(row[1])) if row[1] is not None else None,
            row[2],
            DesktopCommand.from_content(json.loads(row[3])) if row[3] is not None else None,
        )

    def _commands_in_state(self, state: str) -> list[DesktopCommandJournalEntry]:
        entries = [
            self.get(row[0])
            for row in self._database.execute(
                "SELECT request_id FROM commands WHERE state=? AND command IS NOT NULL ORDER BY ordinal",
                (state,),
            )
        ]
        return [entry for entry in entries if entry is not None]

    def queued(self) -> list[DesktopCommandJournalEntry]:
        """Read admitted commands in admission order."""
        return self._commands_in_state("queued")

    def started(self) -> list[DesktopCommandJournalEntry]:
        """Read started commands with bodies available for recovery."""
        return self._commands_in_state("started")

    def sequence_error(self, command: DesktopCommand) -> str | None:
        """Reject non-increasing sequences for new work."""
        row = self._database.execute(
            "SELECT sequence FROM sequences WHERE session_id=?",
            (command.session_id,),
        ).fetchone()
        if row is not None and command.sequence <= row[0]:
            return "Desktop command sequence was already used or arrived out of order."
        return None

    def _require_same(self, command: DesktopCommand, fingerprint: str) -> DesktopCommandJournalEntry | None:
        existing = self.get(command.request_id)
        if existing is not None and existing.command_fingerprint != fingerprint:
            msg = "Desktop request ID was journaled with different command content."
            raise DesktopCommandJournalError(msg)
        return existing

    def admit(self, command: DesktopCommand, command_fingerprint: str) -> None:
        """Commit a queued body and reserve its sequence atomically."""
        with self._database:
            if self._require_same(command, command_fingerprint) is not None:
                return
            error = self.sequence_error(command)
            if error is not None:
                raise DesktopCommandJournalError(error)
            self._make_room()
            self._database.execute(
                "INSERT INTO commands(request_id,fingerprint,command,state) VALUES(?,?,?,'queued')",
                (command.request_id, command_fingerprint, _encode(command.to_content())),
            )
            self._database.execute(
                "INSERT INTO sequences(session_id,sequence) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET sequence=excluded.sequence,ordinal=excluded.ordinal",
                (command.session_id, command.sequence),
            )
            self._prune_sequences()

    def remember_started(self, command: DesktopCommand, command_fingerprint: str) -> None:
        """Commit a start receipt before any local side effect."""
        with self._database:
            existing = self._require_same(command, command_fingerprint)
            if existing is None or existing.state != "queued":
                msg = "Desktop command must be queued before it can start."
                raise DesktopCommandJournalError(msg)
            self._database.execute("UPDATE commands SET state='started' WHERE request_id=?", (command.request_id,))

    def remember_response(self, command: DesktopCommand, command_fingerprint: str, response: DesktopResponse) -> None:
        """Commit the immutable outcome and pending response together."""
        if response.request_id != command.request_id or response.session_id != command.session_id:
            msg = "Desktop response does not identify its command."
            raise DesktopCommandJournalError(msg)
        with self._database:
            existing = self._require_same(command, command_fingerprint)
            if existing is None:
                self._make_room()
                self._database.execute(
                    "INSERT INTO commands(request_id,fingerprint,command,state) VALUES(?,?,?,'completed')",
                    (command.request_id, command_fingerprint, _encode(command.to_content())),
                )
            elif existing.response is not None and _encode(existing.response.to_content()) != _encode(
                response.to_content(),
            ):
                msg = "Desktop command already has a different outcome."
                raise DesktopCommandJournalError(msg)
            delivery_id = self._queue_response(response)
            self._database.execute(
                "UPDATE commands SET state='completed',delivery_id=? WHERE request_id=?",
                (delivery_id, command.request_id),
            )

    def _queue_response(self, response: DesktopResponse) -> str:
        encoded = _encode(response.to_content())
        delivery_id = hashlib.sha256(encoded.encode()).hexdigest()
        self._database.execute(
            "INSERT INTO responses(delivery_id,response) VALUES(?,?) "
            "ON CONFLICT(delivery_id) DO UPDATE SET delivered=0",
            (delivery_id, encoded),
        )
        return delivery_id

    def queue_response(self, response: DesktopResponse) -> None:
        """Queue a rejection or replay without altering the original command."""
        with self._database:
            delivery_id = hashlib.sha256(_encode(response.to_content()).encode()).hexdigest()
            existing = self._database.execute(
                "SELECT delivered FROM responses WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            pending = self._database.execute("SELECT COUNT(*) FROM responses WHERE delivered=0").fetchone()[0]
            if (existing is None or existing[0]) and pending >= self._max_entries:
                msg = "Desktop response journal reached capacity; delivery must resume."
                raise DesktopCommandJournalFullError(msg)
            self._queue_response(response)

    def pending_responses(self) -> list[tuple[str, DesktopResponse]]:
        """Read pending outcomes even after their command deadlines."""
        return [
            (row[0], DesktopResponse.from_content(json.loads(row[1])))
            for row in self._database.execute(
                "SELECT delivery_id,response FROM responses WHERE delivered=0 ORDER BY rowid",
            )
        ]

    def mark_delivered(self, delivery_id: str) -> None:
        """Record successful transport while retaining the action receipt."""
        with self._database:
            self._database.execute("UPDATE responses SET delivered=1 WHERE delivery_id=?", (delivery_id,))
            self._prune_delivered_responses()

    def _prune_delivered_responses(self) -> None:
        self._database.execute(
            "DELETE FROM responses WHERE delivered=1 AND delivery_id NOT IN "
            "(SELECT delivery_id FROM commands WHERE delivery_id IS NOT NULL)",
        )

    def _prune_sequences(self) -> None:
        count = self._database.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
        if count <= _MAX_SESSIONS:
            return
        self._database.execute(
            "DELETE FROM sequences WHERE ordinal IN "
            "(SELECT ordinal FROM sequences WHERE session_id NOT IN "
            "(SELECT json_extract(c.command,'$.session_id') FROM commands c LEFT JOIN responses r USING(delivery_id) "
            "WHERE c.command IS NOT NULL AND (c.state!='completed' OR r.delivered=0)) "
            "ORDER BY ordinal LIMIT ?)",
            (count - _MAX_SESSIONS,),
        )
        if self._database.execute("SELECT COUNT(*) FROM sequences").fetchone()[0] > _MAX_SESSIONS:
            msg = "Desktop session journal reached capacity; pending work must finish."
            raise DesktopCommandJournalFullError(msg)

    def _make_room(self) -> None:
        count = self._database.execute("SELECT COUNT(*) FROM commands WHERE command IS NOT NULL").fetchone()[0]
        if count < self._max_entries:
            return
        self._database.execute(
            "DELETE FROM commands WHERE ordinal IN "
            "(SELECT c.ordinal FROM commands c JOIN responses r USING(delivery_id) "
            "WHERE c.command IS NOT NULL AND c.state='completed' AND r.delivered=1 ORDER BY c.ordinal LIMIT ?)",
            (count - self._max_entries + 1,),
        )
        self._prune_delivered_responses()
        if (
            self._database.execute("SELECT COUNT(*) FROM commands WHERE command IS NOT NULL").fetchone()[0]
            >= self._max_entries
        ):
            msg = "Desktop command journal reached capacity; pending work must finish."
            raise DesktopCommandJournalFullError(msg)

    def _import_legacy(self, path: Path) -> None:
        if self._database.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
            return
        _require_private(path)
        if not path.exists():
            return
        try:
            entries, sequences = parse_legacy_records(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            msg = "Desktop legacy command journal is malformed."
            raise DesktopCommandJournalError(msg) from exc
        with self._database:
            for request_id, fingerprint, response in entries:
                existing = self.get(request_id)
                if existing is not None:
                    if existing.command_fingerprint != fingerprint:
                        msg = "Desktop legacy receipt has a different command identity."
                        raise DesktopCommandJournalError(msg)
                    continue
                delivery_id = self._queue_response(response) if response is not None else None
                if delivery_id is not None:
                    self._database.execute("UPDATE responses SET delivered=1 WHERE delivery_id=?", (delivery_id,))
                self._database.execute(
                    "INSERT INTO commands(request_id,fingerprint,state,delivery_id) VALUES(?,?,?,?)",
                    (request_id, fingerprint, "completed" if response is not None else "started", delivery_id),
                )
            for session_id, sequence in sequences:
                self._database.execute(
                    "INSERT INTO sequences(session_id,sequence) VALUES(?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
                    (session_id, sequence),
                )
            self._prune_sequences()
            self._database.execute("INSERT INTO metadata VALUES('legacy_imported','1')")


def _encode(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _require_private(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        msg = "Desktop command journal must not grant permissions to group or other users."
        raise DesktopCommandJournalError(msg)


__all__ = [
    "DesktopCommandJournal",
    "DesktopCommandJournalEntry",
    "DesktopCommandJournalError",
    "DesktopCommandJournalFullError",
    "check_controller_binding",
]
