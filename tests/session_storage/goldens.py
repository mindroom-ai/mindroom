"""The conversation databases Agno 3.0.9 wrote through MindRoom (``tests/fixtures/agno_parity/storage``), restored."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.store import ConversationStore

if TYPE_CHECKING:
    from mindroom.session_storage.types import SessionKind

STORAGE = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "storage"


@dataclass(frozen=True)
class GoldenStore:
    """One golden database: its dump label, path under the storage root, sessions table, and sessions."""

    label: str
    db_path: str
    names: TableNames
    sessions: dict[str, SessionKind]

    def restore(self, root: Path) -> Path:
        """Write this database under ``root`` from its dump and return the file."""
        db_file = root / self.db_path
        db_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_file)
        try:
            connection.executescript((STORAGE / f"{self.label}.sql").read_text(encoding="utf-8"))
        finally:
            connection.close()
        return db_file

    def open(self, root: Path) -> ConversationStore:
        """Restore this database under ``root`` and open it with the owned store."""
        return ConversationStore(self.restore(root), self.names)


def golden_stores() -> list[GoldenStore]:
    """Return every golden database, as ``stores.json`` lists them."""
    stores = json.loads((STORAGE / "stores.json").read_text(encoding="utf-8"))
    return [
        GoldenStore(
            label=label,
            db_path=spec["db_path"],
            names=TableNames.of_database(Path(spec["db_path"])),
            sessions=cast("dict[str, SessionKind]", spec["sessions"]),
        )
        for label, spec in stores.items()
    ]


def golden_sessions() -> list[tuple[GoldenStore, str]]:
    """Return every ``(store, session_id)`` pair of the golden databases."""
    return [(store, session_id) for store in golden_stores() for session_id in store.sessions]


@cache
def _loaded_sessions() -> dict[str, dict[str, dict[str, Any]]]:
    """Read the sessions Agno loaded once, as the dump is large and every test of a session asks for its own."""
    return json.loads((STORAGE / "loaded_sessions.json").read_text(encoding="utf-8"))


def agno_loaded_session(store: GoldenStore, session_id: str) -> dict[str, Any]:
    """Return ``AgentSession.to_dict()`` or ``TeamSession.to_dict()`` of a golden session as Agno loaded it."""
    return deepcopy(_loaded_sessions()[store.label][session_id])


def stored_rows(db_file: Path, table: str, session_id: str) -> list[dict[str, Any]]:
    """Return a session's rows of ``table`` as column dicts, JSON columns decoded, in log order for runs."""
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    try:
        order = "run_index, created_at, run_id" if table.endswith("_runs") else "id"
        rows = connection.execute(
            f'SELECT * FROM "{table}" WHERE session_id = ? ORDER BY {order}',  # noqa: S608
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {key: json.loads(row[key]) if key in {"run_data", "usage_data"} else row[key] for key in row.keys()}  # noqa: SIM118
        for row in rows
    ]
