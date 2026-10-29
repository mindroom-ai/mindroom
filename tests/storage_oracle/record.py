"""The oracle's recordings, where they live, and how to read and compare them.

The recordings were made against the Agno-layout store, the one this conversation storage replaced. Every generated
seed and named sequence was driven through it, keeping each step's outcome, a digest of each observable after every
action, the full observables after every cut (after every action for a named sequence) and after the last action,
and a dump of the final database. Every storage golden and every legacy input was opened as that store opened it,
and its sessions' observables kept. That store is gone, so the recordings cannot be made again: a later change to
the vocabulary adds new seeds against the current store only (spec v4 4.7). A comparison shows the keys a stored dict
leaves out at their defaults (``with_defaults``), which a reader reads the same either way.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, cast

from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import TableNames
from mindroom.usage.storage import quote_identifier
from tests.llm.agno_dict_helpers import MESSAGE_DEFAULTS
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.observe import observe

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "storage_oracle"
TESTS = Path(__file__).resolve().parents[1]
# Every recorded sequence: the generated seeds and the named sequences, each a ``.json`` recording and a ``.sql`` dump.
SEQUENCES = sorted([*(FIXTURES / "generated").glob("*.json"), *(FIXTURES / "named").glob("*.json")])
GOLDEN_DUMPS = (
    *sorted((TESTS / "fixtures" / "agno_parity" / "storage").glob("*.sql")),
    TESTS / "fixtures" / "agno_2_6_12_code_sessions.sql",
)
_SESSIONS_TABLE = re.compile(r'CREATE TABLE "?([A-Za-z0-9_]+)_sessions"? \(')


def sequence_id(path: Path) -> str:
    """Return a recorded sequence's test id, ``<group>/<name>``."""
    return f"{path.parent.name}/{path.stem}"


def digest(value: object) -> str:
    """Return a short digest of ``value``'s canonical JSON."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:12]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _resolve(value: object, objects: dict[str, Any]) -> object:
    if isinstance(value, dict):
        if value.keys() == {"$"}:
            return _resolve(objects[value["$"]], objects)
        return {key: _resolve(item, objects) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, objects) for item in value]
    return value


def decode(text: str) -> dict[str, Any]:
    """Return the recording a file holds: key-sorted JSON whose messages and runs are stored once, under ``objects``."""
    data = json.loads(text)
    return cast("dict[str, Any]", _resolve(data["recording"], data["objects"]))


def with_defaults(observed: object) -> Any:  # noqa: ANN401 - a JSON value
    """Return ``observed`` with each message showing the keys its stored dict leaves at their defaults.

    The recordings show those keys, since the writers of their time stored every one. A reader reads an absent key as
    its default, so showing them again changes nothing a reader sees, and a key holding any other value still differs.
    """
    if isinstance(observed, list):
        return [with_defaults(item) for item in observed]
    if not isinstance(observed, dict):
        return observed
    shown = {key: with_defaults(value) for key, value in observed.items()}
    if "role" in shown and "id" in shown:
        return {**MESSAGE_DEFAULTS, **shown}
    return shown


def first_difference(expected: object, actual: object, path: str = "") -> str | None:
    """Return where ``actual`` first differs from ``expected``, or None when they are equal."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted({*expected, *actual}):
            if key not in expected or key not in actual:
                return f"{path}/{key}: present only in {'actual' if key in actual else 'expected'}"
            if (found := first_difference(expected[key], actual[key], f"{path}/{key}")) is not None:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            if (found := first_difference(left, right, f"{path}[{index}]")) is not None:
                return found
        return None if len(expected) == len(actual) else f"{path}: {len(expected)} items, now {len(actual)}"
    return None if expected == actual else f"{path}: {expected!r} != {actual!r}"


def storage_name_of(dump: str) -> str:
    """Return the storage name of the database ``dump`` creates, from its sessions table."""
    match = _SESSIONS_TABLE.search(dump)
    if match is None:
        msg = "the dump creates no sessions table"
        raise ValueError(msg)
    return match.group(1)


def observe_database(dump: str, root: Path, extra_run_ids: dict[str, list[str]] | None = None) -> dict[str, object]:
    """Restore ``dump`` under ``root`` and observe it as ``observe_store`` does."""
    storage_name = storage_name_of(dump)
    database = TableNames(storage_name).database_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(dump)
    return observe_store(storage_name, root, extra_run_ids)


def observe_store(
    storage_name: str,
    root: Path,
    extra_run_ids: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Open the database ``storage_name`` under ``root`` as the runtime does, converting it, and observe every session.

    Each session's folded tool calls are read for the runs ``list_runs`` returns plus ``extra_run_ids``, and the
    observation keeps those ids, so a later one asks the same questions.
    """
    with deterministic():
        store = create_state_storage(storage_name, root)
        with closing(sqlite3.connect(store.path)) as connection:
            rows = connection.execute(
                f"SELECT session_id, session_type, agent_id, team_id FROM {quote_identifier(store.names.sessions)} "  # noqa: S608
                "ORDER BY session_id",
            ).fetchall()
        sessions: dict[str, object] = {}
        for session_id, session_type, agent_id, team_id in rows:
            owner = team_id if session_type == "team" else agent_id
            if not owner:
                sessions[session_id] = {"error": "the session row names no owner"}
                continue
            listed = [run.run_id for run in store.list_runs(session_id)]
            run_ids = sorted({*listed, *(extra_run_ids or {}).get(session_id, [])})
            scope = HistoryScope(kind="team" if session_type == "team" else "agent", scope_id=owner)
            sessions[session_id] = {
                "scope": scope.key,
                "run_ids": run_ids,
                "observed": observe(store, session_id, scope, run_ids=run_ids),
            }
    return json.loads(_canonical({"storage_name": storage_name, "sessions": sessions}))
