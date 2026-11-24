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

    from mindroom.matrix.device_identity import PinnedMatrixDevice

_MAX_ENTRIES = 1024
_MAX_SESSIONS = 128
_SCHEMA_VERSION = 2
_REQUIRED_TABLES = frozenset({"metadata", "responses", "commands", "sequences"})


class DesktopCommandJournalError(RuntimeError):
    """The desktop journal cannot safely admit or recover work."""


class DesktopCommandJournalFullError(DesktopCommandJournalError):
    """Execution or delivery must progress before more work is admitted."""


def desktop_journal_path(storage_root: Path) -> Path:
    """Return the bridge's command journal under one storage root."""
    return storage_root / "desktop_bridge" / "commands.sqlite3"


def controller_journal_key(controller: PinnedMatrixDevice) -> str:
    """Return the canonical controller identity stored in the command journal."""
    return json.dumps([controller.user_id, controller.device_id, controller.ed25519])


def check_controller_binding(storage_root: Path, controller: PinnedMatrixDevice) -> None:
    """Verify that any existing command journal is readable with a supported schema, without modifying it."""
    del controller
    path = desktop_journal_path(storage_root)
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
            version_row = database.execute("PRAGMA user_version").fetchone()
            version = version_row[0] if version_row is not None else None
            if version not in {0, 1, _SCHEMA_VERSION}:
                msg = "Desktop journal has an unsupported schema."
                raise DesktopCommandJournalError(msg)
            if version in {1, _SCHEMA_VERSION}:
                _require_schema_tables(database)
    except sqlite3.DatabaseError as exc:
        msg = "Desktop journal could not be read."
        raise DesktopCommandJournalError(msg) from exc


@dataclass(frozen=True, slots=True)
class DesktopCommandJournalEntry:
    """Immutable command identity and its durable execution state."""

    command_fingerprint: str
    response: DesktopResponse | None
    state: Literal["queued", "started", "completed"]
    command: DesktopCommand | None
    controller_key: str = ""


@dataclass
class DesktopCommandJournal:
    """Bounded SQLite inbox/outbox plus at most 1024 fixed bodyless legacy receipts.

    Legacy receipts cannot execute or consume new admission slots. Their missing
    command bodies are never reconstructed; exact replay can only record an outcome.
    """

    path: Path | None
    _database: sqlite3.Connection
    _max_entries: int = _MAX_ENTRIES
    _default_controller_key: str = ""

    @classmethod
    def load(
        cls,
        path: Path | None,
        *,
        controller_key: str = "",
        legacy_path: Path | None = None,
        max_entries: int = _MAX_ENTRIES,
    ) -> DesktopCommandJournal:
        """Open a journal, migrating v1 databases to v2, and import existing replay receipts once."""
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
        journal = cls(path, database, max_entries, controller_key)
        try:
            journal._initialize()
            if legacy_path is not None:
                journal._import_legacy(legacy_path)
        except BaseException:
            database.close()
            raise
        return journal

    def close(self) -> None:
        """Close after the executor and sender have stopped."""
        self._database.close()

    def _resolve_controller_key(self, controller_key: str | None) -> str:
        return self._default_controller_key if controller_key is None else controller_key

    def _initialize(self) -> None:
        try:
            version_row = self._database.execute("PRAGMA user_version").fetchone()
        except sqlite3.DatabaseError as exc:
            msg = "Desktop journal could not be read."
            raise DesktopCommandJournalError(msg) from exc
        version = version_row[0] if version_row is not None else None
        if version not in {0, 1, _SCHEMA_VERSION}:
            msg = "Desktop journal has an unsupported schema."
            raise DesktopCommandJournalError(msg)
        self._database.execute("PRAGMA synchronous=FULL")
        if version == 0:
            self._create_v2_schema()
        elif version == 1:
            self._migrate_v1_to_v2()
        else:
            _require_schema_tables(self._database)

    def _execute_v2_ddl(self) -> None:
        self._database.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "delivery_id TEXT PRIMARY KEY, "
            "controller_key TEXT NOT NULL, "
            "response TEXT NOT NULL, "
            "delivered INTEGER NOT NULL DEFAULT 0)",
        )
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS commands ("
            "ordinal INTEGER PRIMARY KEY AUTOINCREMENT, "
            "controller_key TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, "
            "command TEXT, "
            "state TEXT NOT NULL CHECK(state IN ('queued','started','completed')), "
            "delivery_id TEXT REFERENCES responses(delivery_id), "
            "UNIQUE(controller_key, request_id))",
        )
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS sequences ("
            "ordinal INTEGER PRIMARY KEY AUTOINCREMENT, "
            "controller_key TEXT NOT NULL, "
            "session_id TEXT NOT NULL, "
            "sequence INTEGER NOT NULL, "
            "UNIQUE(controller_key, session_id))",
        )

    def _create_v2_schema(self) -> None:
        self._database.execute("BEGIN IMMEDIATE")
        try:
            self._execute_v2_ddl()
            self._database.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._database.commit()
        except BaseException:
            self._database.rollback()
            raise

    def _migrate_v1_to_v2(self) -> None:
        self._database.execute("BEGIN IMMEDIATE")
        try:
            controller_row = self._database.execute("SELECT value FROM metadata WHERE key='controller'").fetchone()
            migrated_key = controller_row[0] if controller_row is not None else ""
            responses_rows = self._database.execute(
                "SELECT delivery_id,response,delivered FROM responses ORDER BY rowid",
            ).fetchall()
            commands_rows = self._database.execute(
                "SELECT ordinal,request_id,fingerprint,command,state,delivery_id FROM commands ORDER BY ordinal",
            ).fetchall()
            sequences_rows = self._database.execute(
                "SELECT ordinal,session_id,sequence FROM sequences ORDER BY ordinal",
            ).fetchall()

            self._database.execute("DROP TABLE commands")
            self._database.execute("DROP TABLE sequences")
            self._database.execute("DROP TABLE responses")
            self._execute_v2_ddl()

            delivery_map: dict[str, str] = {}
            for old_delivery_id, response_text, delivered in responses_rows:
                new_delivery_id = _delivery_id(migrated_key, response_text)
                delivery_map[old_delivery_id] = new_delivery_id
                self._database.execute(
                    "INSERT INTO responses(delivery_id,controller_key,response,delivered) VALUES(?,?,?,?) "
                    "ON CONFLICT(delivery_id) DO UPDATE SET delivered=MIN(delivered,excluded.delivered)",
                    (new_delivery_id, migrated_key, response_text, delivered),
                )
            for ordinal, request_id, fingerprint, command_text, state, old_delivery_id in commands_rows:
                new_delivery_id = _mapped_delivery_id(old_delivery_id, delivery_map)
                self._database.execute(
                    "INSERT INTO commands(ordinal,controller_key,request_id,fingerprint,command,state,delivery_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ordinal, migrated_key, request_id, fingerprint, command_text, state, new_delivery_id),
                )
            for ordinal, session_id, sequence in sequences_rows:
                self._database.execute(
                    "INSERT INTO sequences(ordinal,controller_key,session_id,sequence) VALUES(?,?,?,?)",
                    (ordinal, migrated_key, session_id, sequence),
                )
            self._database.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._database.commit()
        except sqlite3.DatabaseError as exc:
            self._database.rollback()
            msg = "Desktop journal could not be migrated."
            raise DesktopCommandJournalError(msg) from exc
        except BaseException:
            self._database.rollback()
            raise

    def get(
        self,
        request_id: str,
        controller_key: str | None = None,
    ) -> DesktopCommandJournalEntry | None:
        """Read original identity without changing replay order."""
        key = self._resolve_controller_key(controller_key)
        row = self._database.execute(
            "SELECT c.controller_key,c.fingerprint,r.response,c.state,c.command FROM commands c "
            "LEFT JOIN responses r USING(delivery_id) WHERE c.controller_key=? AND c.request_id=?",
            (key, request_id),
        ).fetchone()
        if row is None:
            return None
        return _entry_from_row(row)

    def _commands_in_state(
        self,
        state: str,
        *,
        controller_key: str | None = None,
        all_controllers: bool = False,
    ) -> list[DesktopCommandJournalEntry]:
        if all_controllers and controller_key is not None:
            msg = "Cannot combine controller_key with all_controllers=True."
            raise ValueError(msg)
        if all_controllers:
            rows = self._database.execute(
                "SELECT c.controller_key,c.fingerprint,r.response,c.state,c.command FROM commands c "
                "LEFT JOIN responses r USING(delivery_id) "
                "WHERE c.state=? AND c.command IS NOT NULL ORDER BY c.ordinal",
                (state,),
            )
        else:
            key = self._resolve_controller_key(controller_key)
            rows = self._database.execute(
                "SELECT c.controller_key,c.fingerprint,r.response,c.state,c.command FROM commands c "
                "LEFT JOIN responses r USING(delivery_id) "
                "WHERE c.controller_key=? AND c.state=? AND c.command IS NOT NULL ORDER BY c.ordinal",
                (key, state),
            )
        return [_entry_from_row(row) for row in rows]

    def queued(
        self,
        controller_key: str | None = None,
        *,
        all_controllers: bool = False,
    ) -> list[DesktopCommandJournalEntry]:
        """Read admitted commands in admission order."""
        return self._commands_in_state("queued", controller_key=controller_key, all_controllers=all_controllers)

    def started(
        self,
        controller_key: str | None = None,
        *,
        all_controllers: bool = False,
    ) -> list[DesktopCommandJournalEntry]:
        """Read started commands with bodies available for recovery."""
        return self._commands_in_state("started", controller_key=controller_key, all_controllers=all_controllers)

    def sequence_error(self, command: DesktopCommand, controller_key: str | None = None) -> str | None:
        """Reject non-increasing sequences for new work."""
        key = self._resolve_controller_key(controller_key)
        row = self._database.execute(
            "SELECT sequence FROM sequences WHERE controller_key=? AND session_id=?",
            (key, command.session_id),
        ).fetchone()
        if row is not None and command.sequence <= row[0]:
            return "Desktop command sequence was already used or arrived out of order."
        return None

    def _require_same(
        self,
        command: DesktopCommand,
        fingerprint: str,
        controller_key: str,
    ) -> DesktopCommandJournalEntry | None:
        existing = self.get(command.request_id, controller_key)
        if existing is not None and existing.command_fingerprint != fingerprint:
            msg = "Desktop request ID was journaled with different command content."
            raise DesktopCommandJournalError(msg)
        return existing

    def admit(
        self,
        command: DesktopCommand,
        command_fingerprint: str,
        controller_key: str | None = None,
    ) -> None:
        """Commit a queued body and reserve its sequence atomically."""
        key = self._resolve_controller_key(controller_key)
        with self._database:
            if self._require_same(command, command_fingerprint, key) is not None:
                return
            error = self.sequence_error(command, key)
            if error is not None:
                raise DesktopCommandJournalError(error)
            self._make_room()
            self._database.execute(
                "INSERT INTO commands(controller_key,request_id,fingerprint,command,state) VALUES(?,?,?,?,'queued')",
                (key, command.request_id, command_fingerprint, _encode(command.to_content())),
            )
            self._database.execute(
                "INSERT INTO sequences(controller_key,session_id,sequence) VALUES(?,?,?) "
                "ON CONFLICT(controller_key,session_id) DO UPDATE SET sequence=excluded.sequence,ordinal=excluded.ordinal",
                (key, command.session_id, command.sequence),
            )
            self._prune_sequences()

    def remember_started(
        self,
        command: DesktopCommand,
        command_fingerprint: str,
        controller_key: str | None = None,
    ) -> None:
        """Commit a start receipt before any local side effect."""
        key = self._resolve_controller_key(controller_key)
        with self._database:
            existing = self._require_same(command, command_fingerprint, key)
            if existing is None or existing.state != "queued":
                msg = "Desktop command must be queued before it can start."
                raise DesktopCommandJournalError(msg)
            self._database.execute(
                "UPDATE commands SET state='started' WHERE controller_key=? AND request_id=?",
                (key, command.request_id),
            )

    def remember_response(
        self,
        command: DesktopCommand,
        command_fingerprint: str,
        response: DesktopResponse,
        controller_key: str | None = None,
    ) -> None:
        """Commit the immutable outcome and pending response together."""
        if response.request_id != command.request_id or response.session_id != command.session_id:
            msg = "Desktop response does not identify its command."
            raise DesktopCommandJournalError(msg)
        key = self._resolve_controller_key(controller_key)
        with self._database:
            existing = self._require_same(command, command_fingerprint, key)
            if existing is None:
                self._make_room()
                self._database.execute(
                    "INSERT INTO commands(controller_key,request_id,fingerprint,command,state) "
                    "VALUES(?,?,?,?,'completed')",
                    (key, command.request_id, command_fingerprint, _encode(command.to_content())),
                )
            elif existing.response is not None and _encode(existing.response.to_content()) != _encode(
                response.to_content(),
            ):
                msg = "Desktop command already has a different outcome."
                raise DesktopCommandJournalError(msg)
            delivery_id = self._queue_response(response, key)
            self._database.execute(
                "UPDATE commands SET state='completed',delivery_id=? WHERE controller_key=? AND request_id=?",
                (delivery_id, key, command.request_id),
            )

    def _queue_response(self, response: DesktopResponse, controller_key: str) -> str:
        encoded = _encode(response.to_content())
        delivery_id = _delivery_id(controller_key, encoded)
        self._database.execute(
            "INSERT INTO responses(delivery_id,controller_key,response) VALUES(?,?,?) "
            "ON CONFLICT(delivery_id) DO UPDATE SET delivered=0",
            (delivery_id, controller_key, encoded),
        )
        return delivery_id

    def queue_response(self, response: DesktopResponse, controller_key: str | None = None) -> None:
        """Queue a rejection or replay without altering the original command."""
        key = self._resolve_controller_key(controller_key)
        with self._database:
            delivery_id = _delivery_id(key, _encode(response.to_content()))
            existing = self._database.execute(
                "SELECT delivered FROM responses WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            pending = self._database.execute("SELECT COUNT(*) FROM responses WHERE delivered=0").fetchone()[0]
            if (existing is None or existing[0]) and pending >= self._max_entries:
                msg = "Desktop response journal reached capacity; delivery must resume."
                raise DesktopCommandJournalFullError(msg)
            self._queue_response(response, key)

    def pending_responses(self, controller_key: str | None = None) -> list[tuple[str, DesktopResponse]]:
        """Read pending outcomes for one controller even after their command deadlines."""
        key = self._resolve_controller_key(controller_key)
        return [
            (row[0], DesktopResponse.from_content(json.loads(row[1])))
            for row in self._database.execute(
                "SELECT delivery_id,response FROM responses WHERE controller_key=? AND delivered=0 ORDER BY rowid",
                (key,),
            )
        ]

    def pending_responses_with_controller(
        self,
        controller_key: str | None = None,
    ) -> list[tuple[str, str, DesktopResponse]]:
        """Read pending outcomes with their owning controller_key (all controllers when omitted)."""
        if controller_key is None:
            rows = self._database.execute(
                "SELECT delivery_id,controller_key,response FROM responses WHERE delivered=0 ORDER BY rowid",
            )
        else:
            rows = self._database.execute(
                "SELECT delivery_id,controller_key,response FROM responses "
                "WHERE controller_key=? AND delivered=0 ORDER BY rowid",
                (controller_key,),
            )
        return [(row[0], row[1], DesktopResponse.from_content(json.loads(row[2]))) for row in rows]

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
            "DELETE FROM sequences WHERE ordinal IN ("
            "SELECT s.ordinal FROM sequences s "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM commands c LEFT JOIN responses r USING(delivery_id) "
            "WHERE c.controller_key=s.controller_key "
            "AND c.command IS NOT NULL "
            "AND json_extract(c.command,'$.session_id')=s.session_id "
            "AND (c.state!='completed' OR r.delivered=0)"
            ") ORDER BY s.ordinal LIMIT ?)",
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
        key = self._default_controller_key
        with self._database:
            for request_id, fingerprint, response in entries:
                existing = self.get(request_id, key)
                if existing is not None:
                    if existing.command_fingerprint != fingerprint:
                        msg = "Desktop legacy receipt has a different command identity."
                        raise DesktopCommandJournalError(msg)
                    continue
                delivery_id = self._queue_response(response, key) if response is not None else None
                if delivery_id is not None:
                    self._database.execute("UPDATE responses SET delivered=1 WHERE delivery_id=?", (delivery_id,))
                self._database.execute(
                    "INSERT INTO commands(controller_key,request_id,fingerprint,state,delivery_id) VALUES(?,?,?,?,?)",
                    (key, request_id, fingerprint, "completed" if response is not None else "started", delivery_id),
                )
            for session_id, sequence in sequences:
                self._database.execute(
                    "INSERT INTO sequences(controller_key,session_id,sequence) VALUES(?,?,?) "
                    "ON CONFLICT(controller_key,session_id) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
                    (key, session_id, sequence),
                )
            self._prune_sequences()
            self._database.execute("INSERT INTO metadata VALUES('legacy_imported','1')")


def _require_schema_tables(database: sqlite3.Connection) -> None:
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not tables >= _REQUIRED_TABLES:
        msg = "Desktop journal schema is incomplete or corrupt."
        raise DesktopCommandJournalError(msg)


def _mapped_delivery_id(old_delivery_id: str | None, delivery_map: dict[str, str]) -> str | None:
    if old_delivery_id is None:
        return None
    if old_delivery_id not in delivery_map:
        msg = "Desktop journal references a missing response record."
        raise DesktopCommandJournalError(msg)
    return delivery_map[old_delivery_id]


def _entry_from_row(
    row: tuple[str, str, str | None, Literal["queued", "started", "completed"], str | None],
) -> DesktopCommandJournalEntry:
    return DesktopCommandJournalEntry(
        row[1],
        DesktopResponse.from_content(json.loads(row[2])) if row[2] is not None else None,
        row[3],
        DesktopCommand.from_content(json.loads(row[4])) if row[4] is not None else None,
        controller_key=row[0],
    )


def _encode(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _delivery_id(controller_key: str, encoded_response: str) -> str:
    return hashlib.sha256(
        json.dumps([controller_key, encoded_response], separators=(",", ":"), ensure_ascii=False).encode(),
    ).hexdigest()


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
    "controller_journal_key",
    "desktop_journal_path",
]
