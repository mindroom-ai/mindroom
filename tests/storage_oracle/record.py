"""Record the oracle's fixtures against the Agno-layout store: ``python -m tests.storage_oracle.record``.

Every generated seed and named sequence is driven through ``OldBackend``, keeping a digest of each observable after
every action, the full observables after every cut (after every action for a named sequence) and after the last
action, and a dump of the final database.
Every storage golden and every legacy input is opened as the runtime opens it, and its sessions' observables are
kept. The recordings outlive the store they describe, so this refuses to run once that store is gone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.history import summary_rows
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import TableNames
from tests.storage_oracle.actions import CUT_KINDS, NAMED, Scenario, generate
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.legacy import EXTRA_RUN_IDS, write_legacy_inputs
from tests.storage_oracle.observe import AGNO_LAYOUT_READERS, Readers, observe
from tests.storage_oracle.old_backend import OldBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from tests.storage_oracle.backend import Driver

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "storage_oracle"
TESTS = Path(__file__).resolve().parents[1]
GENERATED_SEEDS = range(40)
GENERATED_LENGTH = 30
GOLDEN_DUMPS = (
    *sorted((TESTS / "fixtures" / "agno_parity" / "storage").glob("*.sql")),
    TESTS / "fixtures" / "agno_2_6_12_code_sessions.sql",
)
# Messages and runs are interned once per file, since the observables repeat them many times.
_INTERNED_KEYS = ("role", "run_id")
_SESSIONS_TABLE = re.compile(r'CREATE TABLE "?([A-Za-z0-9_]+)_sessions"? \(')


def digest(value: object) -> str:
    """Return a short digest of ``value``'s canonical JSON."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:12]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _intern(value: object, objects: dict[str, object]) -> object:
    if isinstance(value, dict):
        encoded = {key: _intern(item, objects) for key, item in value.items()}
        if any(key in value for key in _INTERNED_KEYS):
            key = digest(encoded)
            objects[key] = encoded
            return {"$": key}
        return encoded
    if isinstance(value, list):
        return [_intern(item, objects) for item in value]
    return value


def _resolve(value: object, objects: dict[str, Any]) -> object:
    if isinstance(value, dict):
        if value.keys() == {"$"}:
            return _resolve(objects[value["$"]], objects)
        return {key: _resolve(item, objects) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, objects) for item in value]
    return value


def encode(recording: object) -> str:
    """Return a recording as its file holds it: key-sorted JSON whose messages and runs are stored once, one a line."""
    objects: dict[str, object] = {}
    body = _intern(json.loads(_canonical(recording)), objects)
    entries = ",\n".join(f"{json.dumps(key)}:{_canonical(value)}" for key, value in sorted(objects.items()))
    return f'{{"objects":{{\n{entries}\n}},\n"recording":{_canonical(body)}}}\n'


def decode(text: str) -> dict[str, Any]:
    """Return the recording a file holds."""
    data = json.loads(text)
    return cast("dict[str, Any]", _resolve(data["recording"], data["objects"]))


def record_scenario(
    scenario: Scenario,
    root: Path,
    *,
    backend: type[Driver] = OldBackend,
    every_step: bool = False,
) -> tuple[dict[str, object], str]:
    """Drive ``scenario`` through ``backend`` under ``root``; return its recording and its final database dump.

    The recording holds each step's outcome and a digest of each observable, and the full observables after every
    cut, or after every step with ``every_step``, and after the last action.
    """
    with deterministic() as determinism:
        driver = backend(root, scenario.kind, determinism)
        steps: list[dict[str, object]] = []
        full: dict[str, object] = {}
        for index, action in enumerate(scenario.actions):
            outcome = driver.apply(action)
            observed = driver.observe()
            steps.append({"outcome": outcome, "digests": {key: digest(value) for key, value in observed.items()}})
            if every_step or action.kind in CUT_KINDS:
                full[str(index)] = observed
        recording = {"scenario": scenario.to_json(), "steps": steps, "full": full, "final": driver.observe()}
        return json.loads(_canonical(recording)), driver.dump()


def storage_name_of(dump: str) -> str:
    """Return the storage name of the database ``dump`` creates, from its sessions table."""
    match = _SESSIONS_TABLE.search(dump)
    if match is None:
        msg = "the dump creates no sessions table"
        raise ValueError(msg)
    return match.group(1)


def observe_database(
    dump: str,
    root: Path,
    extra_run_ids: dict[str, list[str]] | None = None,
    *,
    open_store: Callable[[str, Path], Any] = create_state_storage,
    readers: Readers = AGNO_LAYOUT_READERS,
) -> dict[str, object]:
    """Restore ``dump`` under ``root``, open it as the runtime does, and observe every session it holds.

    Each session's folded tool calls are read for the runs ``list_runs`` returns plus ``extra_run_ids``, and the
    recording keeps those ids, so a later reading asks the same questions. ``open_store`` and ``readers`` open and read
    the database as another store does, such as the storage v2 one after the conversion.
    """
    storage_name = storage_name_of(dump)
    database = TableNames(storage_name).database_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    with deterministic():
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(dump)
        store = open_store(storage_name, root)
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                f'SELECT session_id, session_type, agent_id, team_id FROM "{storage_name}_sessions" ORDER BY session_id',  # noqa: S608
            ).fetchall()
        sessions: dict[str, object] = {}
        for session_id, session_type, agent_id, team_id in rows:
            owner = team_id if session_type == "team" else agent_id
            if not owner:
                sessions[session_id] = {"error": "the session row names no owner"}
                continue
            try:
                listed = [run.run_id for run in store.list_runs(session_id)]
            except Exception:
                # A row that does not decode fails the whole list; ``observe`` records that failure.
                listed = []
            run_ids = sorted({*listed, *(extra_run_ids or {}).get(session_id, [])})
            scope = HistoryScope(kind="team" if session_type == "team" else "agent", scope_id=owner)
            sessions[session_id] = {
                "scope": scope.key,
                "run_ids": run_ids,
                "observed": observe(store, session_id, scope, readers=readers, run_ids=run_ids),
            }
    return json.loads(_canonical({"storage_name": storage_name, "sessions": sessions}))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def keeps_every_step(group: str) -> bool:
    """Return whether a group's recordings keep the full observables after every step: the short named sequences."""
    return group == "named"


def record_sequences(out: Path, group: str, names: Collection[str] | None = None) -> None:
    """Record the ``generated`` or ``named`` group into ``out``, or only its sequences ``names``."""
    scenarios = (
        {str(seed): generate(seed, GENERATED_LENGTH) for seed in GENERATED_SEEDS} if group == "generated" else NAMED
    )
    for name, scenario in scenarios.items():
        if names is not None and name not in names:
            continue
        with tempfile.TemporaryDirectory() as root:
            recording, dump = record_scenario(scenario, Path(root), every_step=keeps_every_step(group))
        _write(out / group / f"{name}.json", encode(recording))
        _write(out / group / f"{name}.sql", dump)


def record_goldens(out: Path) -> None:
    """Record every storage golden's sessions into ``out``."""
    for dump_file in GOLDEN_DUMPS:
        with tempfile.TemporaryDirectory() as root:
            recording = observe_database(dump_file.read_text(encoding="utf-8"), Path(root))
        _write(out / "goldens" / f"{dump_file.stem}.json", encode(recording))


def record_legacy(out: Path, inputs: Path) -> None:
    """Record the observables of every legacy input dump in ``inputs`` into ``out``."""
    for dump_file in sorted((inputs / "legacy").glob("*.sql")):
        dump = dump_file.read_text(encoding="utf-8")
        if out != inputs:
            _write(out / "legacy" / dump_file.name, dump)
        with tempfile.TemporaryDirectory() as root:
            recording = observe_database(dump, Path(root), EXTRA_RUN_IDS.get(dump_file.stem))
        _write(out / "legacy" / f"{dump_file.stem}.json", encode(recording))


GROUPS = ("generated", "named", "goldens", "legacy")


def record(out: Path, group: str, names: Collection[str] | None = None) -> None:
    """Record one group of fixtures into ``out``; the legacy group reads its inputs from the committed fixtures.

    ``names`` limits a group of sequences to those sequences.
    """
    if group in ("generated", "named"):
        record_sequences(out, group, names)
    elif group == "goldens":
        record_goldens(out)
    else:
        record_legacy(out, FIXTURES)


def _require_agno_layout() -> None:
    if "Segment" not in vars(summary_rows):
        msg = (
            "The Agno-layout store is gone: its recordings can no longer be made; add new seeds against the new store."
        )
        raise SystemExit(msg)


def main() -> None:
    """Write every fixture: capture the legacy inputs, then record every group."""
    _require_agno_layout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=FIXTURES)
    parser.add_argument("--group", choices=GROUPS, action="append")
    parser.add_argument("--skip-capture", action="store_true", help="keep the committed legacy inputs")
    arguments = parser.parse_args()
    groups = arguments.group or GROUPS
    if "legacy" in groups and not arguments.skip_capture:
        write_legacy_inputs(FIXTURES)
    for group in groups:
        record(arguments.out, group)


if __name__ == "__main__":
    main()
