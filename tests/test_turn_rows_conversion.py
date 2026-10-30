"""Stored turn records and redaction tombstones convert into turn rows once, as the schema upgrade runs.

``Journal.legacy`` opens a database as an earlier release would leave it: the conversion skipped and the old tables
created, so a test can seed them; ``Journal.upgraded`` opens it as this release does, converting in the schema
transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import psycopg
import pytest
import pytest_asyncio

from mindroom.event_journal import (
    ApprovalFailure,
    DeliveryStage,
    EventJournalStore,
    legacy_turn_rows,
    turn_rows,
    upgrades,
)
from mindroom.event_journal.identity import journal_principal_id
from mindroom.event_journal.legacy_turn_rows import (
    import_legacy_ledger_file,
    legacy_summary_sources,
)
from mindroom.reply_lifecycle import ApprovalFailureKind, ReplyState
from mindroom.response_sources import ResponseSources
from mindroom.turn_record import PreparedVoiceSource, SourceVisibleEcho, TurnRecord
from tests import test_event_journal_store as journal_tests
from tests.conftest import postgres_journal_schema_url
from tests.reply_span_helpers import paused_for_approval
from tests.test_event_journal_store import admit
from tests.turn_state_oracle.adapter import AGENT, PRINCIPAL, ROOMS, TARGETS, record_json
from tests.turn_state_oracle.legacy import FIXTURES
from tests.turn_state_oracle.record import GOLDENS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import LiteralString

    from mindroom.event_journal.backend import Transaction

pytestmark = pytest.mark.asyncio

ROOM = ROOMS["a"]
TARGET = TARGETS["a"]
EARLIER_LOGIN = journal_principal_id(AGENT, "@old_login:example.org")
CONTROL = "other@@other:example.org"
VOICE = PreparedVoiceSource.from_content("transcript", {"msgtype": "m.text", "body": "transcript"})


# The tables earlier releases wrote turn state and redactions into, as they created them.
_LEGACY_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS turn_records (
        agent_name TEXT NOT NULL,
        index_event_id TEXT NOT NULL,
        anchor_event_id TEXT NOT NULL,
        record_json TEXT NOT NULL,
        PRIMARY KEY (agent_name, index_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS redaction_tombstones (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        redacted_event_id TEXT NOT NULL,
        PRIMARY KEY (principal_id, room_id, redacted_event_id)
    )
    """,
)


@contextmanager
def without_conversion() -> Iterator[None]:
    """Open as an earlier release: the conversion does not run in the schema transaction."""
    with patch.object(upgrades, "upgrade_turn_records_to_rows", lambda *_args, **_kwargs: None):
        yield


@dataclass
class Journal:
    """One database per backend, opened without the conversion (as an earlier release) or with it."""

    connect: Callable[[], EventJournalStore]
    postgres: bool
    opened: list[EventJournalStore]

    async def legacy(self) -> EventJournalStore:
        """Open the database as an earlier release, without the conversion and with its old tables."""
        with without_conversion():
            store = self.connect()
        self.opened.append(store)

        def create(transaction: Transaction) -> None:
            for statement in _LEGACY_TABLES:
                transaction.execute(statement)

        await store.backend.write(create)
        return store

    def upgraded(self) -> EventJournalStore:
        """Open the database as this release does, converting in its schema transaction."""
        store = self.connect()
        self.opened.append(store)
        return store


@pytest_asyncio.fixture(params=("sqlite", "postgres"))
async def journal(request: pytest.FixtureRequest, tmp_path: Path) -> Any:  # noqa: ANN401
    """Return a database per backend; every store it opens is closed at the end."""
    if request.param == "sqlite":
        path = tmp_path / "journal.db"

        def connect() -> EventJournalStore:
            return EventJournalStore.open_sqlite(path)
    else:
        url = postgres_journal_schema_url(request.getfixturevalue("postgres_journal_url"))

        def connect() -> EventJournalStore:
            return EventJournalStore.open_postgres(url)

    opened: list[EventJournalStore] = []
    yield Journal(connect, request.param == "postgres", opened)
    for store in opened:
        await store.close()


def blob(
    sources: Sequence[str],
    *,
    response: str | None = None,
    completed: bool | None = None,
    room: str | None = "a",
    timestamp: float = 1_000.0,
    **extra: object,
) -> dict[str, Any]:
    """Return one stored record in the shape the codec reads."""
    record: dict[str, Any] = {
        "anchor_event_id": sources[-1],
        "source_event_ids": list(sources),
        "redacted_source_event_ids": [],
        "response_event_id": response,
        "completed": response is not None if completed is None else completed,
        "timestamp": timestamp,
        "revision_replay": {},
    }
    if room is not None:
        record |= {
            "response_owner": AGENT,
            "requester_id": "@alice:example.org",
            "history_scope": {"kind": "agent", "scope_id": AGENT},
            "conversation_target": TARGETS[room].to_metadata(),
        }
    return record | extra


async def seed_rows(store: EventJournalStore, rows: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    """Store ``turn_records`` rows as earlier releases wrote them."""

    def insert(transaction: Transaction) -> None:
        for index_event_id, record in rows:
            transaction.execute(
                "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)",
                (AGENT, index_event_id, record["anchor_event_id"], json.dumps(record, sort_keys=True)),
            )

    await store.backend.write(insert)


def every_index(record: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Return the rows an earlier release stored for one record: one per source and alias."""
    return [(event_id, record) for event_id in (*record["source_event_ids"], *record.get("discovery_event_ids", ()))]


async def read(store: EventJournalStore, operation: Callable[..., Any], *args: object) -> Any:  # noqa: ANN401
    """Run one turn-row read for ``AGENT``."""
    return await store.backend.read(lambda transaction: operation(transaction, AGENT, *args))


async def turn_json(store: EventJournalStore, event_id: str) -> dict[str, Any] | None:
    """Return the turn one event answers to as the oracle's stable JSON."""
    return record_json(await read(store, turn_rows.get_turn_record, event_id))


async def legacy_tables(store: EventJournalStore) -> int | None:
    """Return how many rows the old turn_records table still holds, or None once the conversion dropped it."""
    try:
        row = await store.backend.read(
            lambda transaction: transaction.fetchone("SELECT COUNT(*) AS count FROM turn_records"),
        )
    except (sqlite3.OperationalError, psycopg.errors.UndefinedTable):
        return None
    assert row is not None
    return int(row["count"])


# The oracle's legacy inputs: after conversion, every observable the turn store showed on opening them holds again
# but for the intended differences, each named below and asserted as the new behavior.


# A revision's redaction and its legacy summary provenance live in their own tables, not in its row.
_ROW_FREE_REPLAY_FACTS = frozenset({"redacted", "legacy_summary_provenance"})


def _converted_view(recorded: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a recorded record as conversion stores it.

    Redaction facts and legacy summary provenance live in their own tables, so the record no longer carries them;
    suppressed revisions are dropped; a one-source record without attribution names its requester and a coalesced one
    names nobody; a selection is keyed by the source it edits; echoes are per source; a record without a room keeps
    only its completion, and an unfinished one is gone.
    """
    if recorded is None:
        return None
    if "target" not in recorded:
        if not recorded["completed"]:
            return None
        evidence = {
            "sources": recorded["sources"],
            "anchor": recorded["sources"][-1],
            "completed": True,
            "metadata": {},
        }
        return evidence | ({"aliases": recorded["aliases"]} if "aliases" in recorded else {})
    view = {
        key: value
        for key, value in recorded.items()
        if key not in {"redacted", "suppressed_revisions", "echo", "echo_fallback"}
    }
    if "replay" in view:
        view["replay"] = {
            revision_id: {key: value for key, value in replay.items() if key not in _ROW_FREE_REPLAY_FACTS}
            for revision_id, replay in view["replay"].items()
        }
    if "metadata" not in view:
        sources = view["sources"]
        view["metadata"] = {sources[0]: {"sender": view["requester"]}} if len(sources) == 1 else {}
    if "revisions" in view:
        mapped = {
            meta["discovery_event_id"]: source
            for source, meta in view["metadata"].items()
            if "discovery_event_id" in meta
        }
        view["revisions"] = {mapped.get(key, key): value for key, value in view["revisions"].items()}
    return view


def _member_view(recorded: Mapping[str, Any], sources: list[str]) -> dict[str, Any]:
    """Return a recorded coalesced record reduced to the sources its rows own."""
    view = _converted_view(recorded)
    assert view is not None
    return view | {"sources": sources, "anchor": sources[-1]}


def _without(view: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    assert view is not None
    assert key in view
    return {name: value for name, value in view.items() if name != key}


def _initial_state(name: str) -> dict[str, dict[str, Any]]:
    golden = json.loads((GOLDENS / "legacy" / f"{name}.json").read_text(encoding="utf-8"))
    return golden["steps"][0]["changes"]


def _intended(name: str, recorded: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return, per fixture, each observable conversion intentionally changes, with why."""
    turns = recorded.get("turn_record", {})
    match name:
        case "cleanup_obligations":
            return {
                # Revision redactions now reach background readers, and only in their own room; a redacted event
                # is handled in its room whether or not a turn indexes it.
                "redacted_event_ids": {"a": ["$e2", "$m2"], "b": None},
                "is_handled": {"$e2": True},
            }
        case "room_less_tombstone":
            return {
                # A room-less record and its tombstone are not converted; the journal tombstones are, agent-wide.
                "turn_record": {"$m6": None},
                "redacted_event_ids": {"a": ["$x1", "$x2"]},
                "redacted_history_events": {"a": {"$x1": None, "$x2": None}},
                "is_handled": {"$x1": True, "$x2": True},
            }
        case "coalesced_rows_disagree":
            return {
                # Each row keeps the blob it held, and a declared source no row named is answered by its blob.
                "turn_record": {
                    "$m1": _member_view(turns["$m1"], ["$m1"]),
                    "$m2": _member_view(turns["$m2"], ["$m2"]),
                    "$m4": _converted_view(turns["$m3"]),
                    "$m5": _member_view(turns["$m5"], ["$m5"]),
                    "$m7": _converted_view(turns["$m6"]),
                },
                "record_for_response": {"$r1": _member_view(turns["$m1"], ["$m1"])},
                "is_handled": {"$m4": True, "$m7": True},
            }
        case "legacy_summary_provenance":
            return {
                "turn_record": {"$e2": None},
                "redacted_event_ids": {"a": ["$e1", "$e2"], "b": None},
                "is_handled": {"$e1": True},
            }
        case "pre_journal_ledger":
            return {
                # Room-less completions keep only their answered proof; the stored row owns $m3, which it declares.
                # A selection is the revision a stored prompt came from, so one without a prompt is not kept; its
                # revision row still holds the watermark.
                "turn_record": {
                    "$m3": _converted_view(turns["$m2"]),
                    "$m4": _without(_converted_view(turns["$m4"]), "revisions"),
                    "$m5": None,
                },
                "record_for_response": {"$r1": None, "$r2": None, "$r2-db": None, "$r6": None},
                "is_handled": {"$m5": None},
                "redacted_event_ids": {"a": None, "b": None},
            }
        case _:
            return {}


async def _observe(store: EventJournalStore, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the oracle's observables over ``ids``, answered by the turn-row reads."""
    state: dict[str, dict[str, Any]] = {name: {} for name in _OBSERVABLES}
    for event_id in ids:
        for name, value in (await _observe_event(store, event_id)).items():
            state[name][event_id] = value
    for key, room_id in ROOMS.items():
        if redacted := sorted(await read(store, turn_rows.redacted_events, room_id, ids)):
            state["redacted_event_ids"][key] = redacted
        if history := await _redacted_history(store, room_id, ids):
            state["redacted_history_events"][key] = history
    return state


_OBSERVABLES = (
    "is_handled",
    "turn_record",
    "visible_echo",
    "finalized_echo",
    "pending_intent",
    "record_for_response",
    "prepared_voice",
    "redacted_event_ids",
    "redacted_history_events",
)


async def _observe_event(store: EventJournalStore, event_id: str) -> dict[str, Any]:
    """Return one event's per-event observables, leaving out default values as the oracle records them."""
    observed: dict[str, Any] = {}
    record = await read(store, turn_rows.get_turn_record, event_id)
    if any([await read(store, turn_rows.handled_among, room, [event_id]) for room in ROOMS.values()]):
        observed["is_handled"] = True
    if record is not None:
        observed["turn_record"] = record_json(record)
        if not record.completed and record.response_owner and record.conversation_target:
            observed["pending_intent"] = True
    echo = await read(store, turn_rows.visible_echo, event_id)
    if echo is not None:
        observed["visible_echo"] = echo.event_id
        if echo.is_fallback is not None:
            observed["finalized_echo"] = {"event_id": echo.event_id, "is_fallback": echo.is_fallback}
    if (owner := await read(store, turn_rows.turn_for_response_event, event_id)) is not None:
        observed["record_for_response"] = record_json(owner)
    if (voice := await read(store, turn_rows.prepared_voice, event_id)) is not None:
        observed["prepared_voice"] = voice.to_record()
    return observed


async def _redacted_history(store: EventJournalStore, room_id: str, ids: Sequence[str]) -> dict[str, str | None]:
    """Return the events a conversation's history cleanup removes, with the legacy summary source of each revision."""

    def history(transaction: Transaction) -> dict[str, str | None]:
        redacted: dict[str, str | None] = dict.fromkeys(
            sorted(turn_rows.redacted_events(transaction, AGENT, room_id, ids)),
        )
        for revision_id, source_event_id in legacy_summary_sources(transaction, AGENT, room_id, ids).items():
            if not turn_rows.redacted_events(transaction, AGENT, room_id, (revision_id,)):
                continue
            if source_event_id in ids:
                redacted[revision_id] = source_event_id
            elif revision_id in ids:
                redacted.setdefault(revision_id, None)
        return dict(sorted(redacted.items()))

    return await store.backend.read(history)


async def _seed_fixture(journal: Journal, name: str, tmp_path: Path) -> Path | None:
    fixture = FIXTURES[name]
    store = await journal.legacy()
    await seed_rows(store, fixture.rows)

    def tombstones(transaction: Transaction) -> None:
        for principal_id, room_id, event_id in fixture.tombstones:
            transaction.execute(
                "INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id) VALUES (?, ?, ?)",
                (principal_id, room_id, event_id),
            )

    await store.backend.write(tombstones)
    await store.close()
    journal.opened.remove(store)
    if fixture.ledger is None:
        return None
    ledger = tmp_path / "tracking" / f"{AGENT}_responded.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(fixture.ledger), encoding="utf-8")
    return ledger


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_every_oracle_legacy_input_converts_to_its_recorded_observables(
    journal: Journal,
    tmp_path: Path,
    name: str,
) -> None:
    """The converted rows answer what the turn store showed on opening each legacy input, but for named changes."""
    ledger = await _seed_fixture(journal, name, tmp_path)
    store = journal.upgraded()
    if ledger is not None:
        await import_legacy_ledger_file(store.turn_records(AGENT), path=ledger)
    golden = json.loads((GOLDENS / "legacy" / f"{name}.json").read_text(encoding="utf-8"))
    recorded = _initial_state(name)
    expected: dict[str, dict[str, Any]] = {
        observable: (
            {key: _converted_view(value) for key, value in values.items()}
            if observable in {"turn_record", "record_for_response"}
            else dict(values)
        )
        for observable, values in recorded.items()
    }
    for observable, changes in _intended(name, recorded).items():
        values = expected.setdefault(observable, {})
        for key, value in changes.items():
            if value is None:
                assert key in values or observable == "turn_record", f"{observable}[{key}] was not recorded"
                values.pop(key, None)
            else:
                values[key] = value

    actual = await _observe(store, golden["scenario"]["ids"])

    for observable in actual:
        assert actual[observable] == expected.get(observable, {}), observable
    assert await legacy_tables(store) is None
    if ledger is not None:
        assert not ledger.exists()


async def test_the_summary_provenance_and_redactions_of_every_blob_move_to_their_tables(
    journal: Journal,
    tmp_path: Path,
) -> None:
    """The facts the converted records no longer carry are where history cleanup and background readers find them."""
    await _seed_fixture(journal, "legacy_summary_provenance", tmp_path)
    store = journal.upgraded()

    sources = await store.backend.read(
        lambda transaction: legacy_summary_sources(transaction, AGENT, ROOM, ["$m1"]),
    )
    assert sources == {"$e1": "$m1", "$e2": "$m1"}
    assert await read(store, turn_rows.redacted_events, ROOM, ["$e1", "$e2", "$e3"]) == {"$e1", "$e2"}
    assert await read(store, turn_rows.redacted_events, ROOMS["b"], ["$e1", "$e2"]) == frozenset()


# The design's migration fixtures.


async def test_room_less_records_keep_only_their_completion(journal: Journal) -> None:
    """Without a provable room a completed record keeps its answered proof and nothing else; a pending one goes."""
    store = await journal.legacy()
    await admit(store.principal(PRINCIPAL), "$placed", room_id=ROOM)
    rows = [
        *every_index(blob(["$done"], response="$r", room=None, source_event_prompts={"$done": "text"})),
        *every_index(blob(["$voice"], room=None, prepared_voice_sources={"$voice": VOICE.to_record()})),
        *every_index(blob(["$placed"], room=None, source_event_prompts={"$placed": "kept"}, completed=False)),
    ]
    await seed_rows(store, rows)

    upgraded = journal.upgraded()

    done = await read(upgraded, turn_rows.get_turn_record, "$done")
    assert done is not None
    assert done.completed
    assert (done.response_event_id, done.source_event_prompts, done.conversation_target) == (None, None, None)
    assert await read(upgraded, turn_rows.get_turn_record, "$voice") is None
    assert await read(upgraded, turn_rows.prepared_voice, "$voice") is None
    placed = await read(upgraded, turn_rows.get_turn_record, "$placed")
    assert placed is not None
    assert placed.source_event_prompts == {"$placed": "kept"}
    rooms = await upgraded.backend.read(lambda transaction: transaction.fetchall("SELECT room_id FROM turns"))
    assert sorted(str(row["room_id"]) for row in rooms) == sorted([ROOM, "None"])


async def test_rows_own_their_turn_and_alias_only_survivors_stay_answered(journal: Journal, tmp_path: Path) -> None:
    """A completed group left with only an alias row keeps that alias answered beside any owner of its source."""
    store = await journal.legacy()
    beside_unfinished = blob(["$m1"], response="$r-old", discovery_event_ids=["$a1"])
    beside_completed = blob(["$m2"], response="$r-old2", discovery_event_ids=["$a2"])
    await seed_rows(
        store,
        [
            ("$m1", blob(["$m1"], completed=False)),
            ("$a1", beside_unfinished),
            ("$m2", blob(["$m2", "$m3"], response="$r-new")),
            ("$a2", beside_completed),
            ("$m4", blob(["$m4"], completed=False)),
            ("$a3", blob(["$m4"], completed=False, discovery_event_ids=["$a3"], timestamp=5.0)),
        ],
    )

    upgraded = journal.upgraded()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema_version": 1, "records": {"$a1": beside_unfinished, "$a2": beside_completed}}))
    assert await import_legacy_ledger_file(upgraded.turn_records(AGENT), path=ledger) == 0
    await upgraded.close()
    journal.opened.remove(upgraded)
    reopened = journal.upgraded()

    for alias in ("$a1", "$a2"):
        assert await read(reopened, turn_rows.is_answered, alias), alias
        evidence = await read(reopened, turn_rows.get_turn_record, alias)
        assert evidence.source_event_ids == (alias,)
        assert evidence.response_event_id is None
    unfinished = await read(reopened, turn_rows.get_turn_record, "$m1")
    assert not unfinished.completed
    assert unfinished.discovery_event_ids == ()
    assert (await read(reopened, turn_rows.get_turn_record, "$m3")).response_event_id == "$r-new"
    # An unfinished group with only an alias row carries no answered proof.
    assert await read(reopened, turn_rows.get_turn_record, "$a3") is None


async def test_a_declared_source_without_a_row_is_answered_by_its_blob(journal: Journal) -> None:
    """A declared source without a row is answered by its blob."""
    store = await journal.legacy()
    await seed_rows(
        store,
        [
            ("$m1", blob(["$m1", "$m2"], response="$r", timestamp=1.0)),
            ("$m3", blob(["$m2", "$m3"], completed=False, timestamp=2.0)),
        ],
    )

    upgraded = journal.upgraded()

    owner = await read(upgraded, turn_rows.get_turn_record, "$m2")
    assert owner.source_event_ids == ("$m1", "$m2")
    assert await read(upgraded, turn_rows.is_answered, "$m2")
    pending = await read(upgraded, turn_rows.get_turn_record, "$m3")
    assert pending.source_event_ids == ("$m3",)


async def test_an_interrupted_import_into_a_populated_store_keeps_its_owners(journal: Journal, tmp_path: Path) -> None:
    """Every occupied index keeps its owner and proof, while the file's redactions still apply to it."""
    store = journal.upgraded()
    newer = TurnRecord.create(
        ["$m1", "$m2"],
        completed=False,
        source_event_prompts={"$m1": "newer one", "$m2": "newer two"},
        conversation_target=TARGET,
        response_owner=AGENT,
    )
    current = await store.backend.write(
        lambda transaction: turn_rows.record_pending_turn(transaction, AGENT, newer, room_id=ROOM),
    )
    assert isinstance(current, turn_rows.PendingTurn)
    ledger_records = {
        "$m1": (old := blob(["$m1", "$m3"], response="$old", redacted_source_event_ids=["$m1"])),
        "$m3": old,
        "$m4": blob(["$m4"], response="$r4"),
    }
    ledger = tmp_path / "ledger.json"
    for _attempt in range(2):
        ledger.write_text(json.dumps({"schema_version": 1, "records": ledger_records}))
        await import_legacy_ledger_file(store.turn_records(AGENT), path=ledger)

    kept = await read(store, turn_rows.get_turn_record, "$m1")
    assert kept.turn_key == current.record.turn_key
    assert kept.source_event_prompts == {"$m2": "newer two"}
    assert await read(store, turn_rows.handled_among, ROOM, ["$m1"]) == {"$m1"}
    adopted = await read(store, turn_rows.get_turn_record, "$m3")
    assert adopted.source_event_ids == ("$m3",)
    assert adopted.completed
    assert await read(store, turn_rows.is_answered, "$m4")
    turn_count = await store.backend.read(lambda transaction: transaction.fetchall("SELECT turn_key FROM turns"))
    assert len(turn_count) == 3


async def test_an_unversioned_ledger_imports_nothing(journal: Journal, tmp_path: Path) -> None:
    """An unversioned ledger imports nothing."""
    store = journal.upgraded()
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"$m1": blob(["$m1"], response="$r1")}))

    assert await import_legacy_ledger_file(store.turn_records(AGENT), path=ledger) == 0

    assert ledger.with_suffix(".json.imported").exists()
    assert await read(store, turn_rows.get_turn_record, "$m1") is None


async def test_a_legacy_echo_reaches_every_source_and_is_not_an_answer(journal: Journal) -> None:
    """A router's [text, voice] record keeps its echo findable from the voice source; an echo is never a response."""
    store = await journal.legacy()
    await seed_rows(
        store,
        [
            *every_index(
                blob(
                    ["$text", "$voice"],
                    response="$routed",
                    visible_echo_event_id="$echo",
                    visible_echo_is_fallback=False,
                ),
            ),
            *every_index(
                blob(
                    ["$v1", "$v2"],
                    completed=False,
                    response_event_id="$echo-v",
                    visible_echo_event_id="$echo-v",
                    visible_echo_is_fallback=False,
                ),
            ),
        ],
    )

    upgraded = journal.upgraded()

    for source in ("$text", "$voice"):
        assert await read(upgraded, turn_rows.visible_echo, source) == SourceVisibleEcho("$echo", is_fallback=False)
    routing = await read(upgraded, turn_rows.get_turn_record, "$v2")
    assert routing.response_event_id is None
    assert not routing.completed
    assert (await read(upgraded, turn_rows.turn_for_response_event, "$echo-v")).turn_key == routing.turn_key


async def test_attribution_is_fixed_before_membership_changes(journal: Journal) -> None:
    """A coalesced record whose sibling row went elsewhere stays unattributed; a real one-source record does not."""
    store = await journal.legacy()
    await seed_rows(
        store,
        [
            ("$m1", blob(["$m1"], response="$other")),
            ("$m2", blob(["$m1", "$m2"], response="$coalesced")),
            ("$single", blob(["$single"], response="$r")),
        ],
    )

    upgraded = journal.upgraded()

    survivor = await read(upgraded, turn_rows.get_turn_record, "$m2")
    assert survivor.source_event_ids == ("$m2",)
    assert survivor.requester_id_for_source("$m2") is None
    single = await read(upgraded, turn_rows.get_turn_record, "$single")
    assert single.requester_id_for_source("$single") == "@alice:example.org"


async def test_an_outstanding_final_delivery_completes_its_turn_after_upgrade(journal: Journal) -> None:
    """A FINAL whose send failed survives the upgrade, and its recovered acknowledgement completes the turn."""
    store = await journal.legacy()
    principal = store.principal(PRINCIPAL)
    for delivery_id in ("$bound", "$elsewhere"):
        await principal.enqueue_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            room_id=ROOM if delivery_id == "$bound" else ROOMS["b"],
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
    await seed_rows(
        store,
        [
            *every_index(blob(["$first", "$bound"], completed=False)),
            *every_index(blob(["$elsewhere"], completed=False)),
        ],
    )

    upgraded = journal.upgraded()
    completed = await upgraded.backend.write(
        lambda transaction: turn_rows.complete_bound_delivery(
            transaction,
            AGENT,
            delivery_id="$bound",
            response_event_id="$ack",
        ),
    )
    unbound = await upgraded.backend.write(
        lambda transaction: turn_rows.complete_bound_delivery(
            transaction,
            AGENT,
            delivery_id="$elsewhere",
            response_event_id="$ack2",
        ),
    )
    await upgraded.close()
    journal.opened.remove(upgraded)
    reopened = journal.upgraded()

    assert completed
    assert not unbound
    found = await read(reopened, turn_rows.get_turn_record, "$first")
    assert found.source_event_ids == ("$first", "$bound")
    assert found.completed
    assert found.response_event_id == "$ack"
    assert not (await read(reopened, turn_rows.get_turn_record, "$elsewhere")).completed


async def test_journal_tombstones_apply_to_every_login_of_their_agent(journal: Journal) -> None:
    """A deletion one login saw scrubs every login of its agent and its turns, and no other agent's rows."""
    store = await journal.legacy()
    for principal_id in (PRINCIPAL, EARLIER_LOGIN, CONTROL):
        await admit(store.principal(principal_id), "$m", room_id=ROOM)
    await seed_rows(store, every_index(blob(["$m"], completed=False, source_event_prompts={"$m": "secret"})))

    def tombstone(transaction: Transaction) -> None:
        transaction.execute(
            "INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id) VALUES (?, ?, ?)",
            (PRINCIPAL, ROOM, "$m"),
        )

    await store.backend.write(tombstone)
    await store.close()
    journal.opened.remove(store)

    upgraded = journal.upgraded()
    await upgraded.close()
    journal.opened.remove(upgraded)
    reopened = journal.upgraded()

    visible = await reopened.backend.read(
        lambda transaction: transaction.fetchall(
            "SELECT principal_id FROM visible_messages WHERE logical_event_id = '$m'",
        ),
    )
    assert [str(row["principal_id"]) for row in visible] == [CONTROL]
    rows = await reopened.backend.read(
        lambda transaction: transaction.fetchall("SELECT principal_id, state, source_json FROM journal_events"),
    )
    states = {str(row["principal_id"]): (str(row["state"]), str(row["source_json"])) for row in rows}
    assert states[PRINCIPAL] == ("settled", "")
    assert states[EARLIER_LOGIN] == ("settled", "")
    assert states[CONTROL][0] == "pending"
    assert "$m" in states[CONTROL][1]
    found = await read(reopened, turn_rows.get_turn_record, "$m")
    assert found.source_event_prompts is None
    assert await read(reopened, turn_rows.redacted_events, ROOM, ["$m"]) == {"$m"}


@pytest.mark.parametrize("recorded_in", ["tombstones", "turn_record"])
async def test_a_deleted_original_ends_the_paused_reply_to_its_relay(journal: Journal, recorded_in: str) -> None:
    """Deleting the messages a router relayed, before the upgrade, ends the reply an approval holds for the relays."""
    store = await journal.legacy()
    principal = store.principal(PRINCIPAL)
    relays = ("$relay-1", "$relay-2")
    for relay in relays:
        await admit(principal, relay, room_id=ROOM)
    waiting = journal_tests.TestApprovalContinuations.continuation(state="waiting")
    waiting = replace(
        waiting,
        room_id=ROOM,
        sources=ResponseSources(relays, relays),
        execution_identity=replace(waiting.execution_identity, room_id=ROOM),
    )
    assert await paused_for_approval(principal, waiting)
    relayed = {relay: {"sender": "@alice:example.org", "discovery_event_id": f"$human-{relay[-1]}"} for relay in relays}
    await seed_rows(
        store,
        every_index(
            blob(
                list(relays),
                completed=False,
                discovery_event_ids=["$human-1", "$human-2"],
                source_event_metadata=relayed,
                redacted_source_event_ids=["$human-1", "$human-2"] if recorded_in == "turn_record" else [],
            ),
        ),
    )

    def tombstone(transaction: Transaction) -> None:
        for human in ("$human-1", "$human-2"):
            transaction.execute(
                "INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id) VALUES (?, ?, ?)",
                (PRINCIPAL, ROOM, human),
            )

    if recorded_in == "tombstones":
        await store.backend.write(tombstone)
    await store.close()
    journal.opened.remove(store)

    upgraded = journal.upgraded().principal(PRINCIPAL)
    reply = await upgraded.replies.for_sources(("$relay-1",))
    assert reply is not None
    assert reply.state is ReplyState.GONE
    failing = await upgraded.approvals.approval_continuation(waiting.approval_id)
    assert failing is not None
    assert failing.state == "failing"
    assert failing.failure == ApprovalFailure(ApprovalFailureKind.USER_STOP)


async def test_a_failed_conversion_rolls_back_and_the_next_open_converts(journal: Journal) -> None:
    """A failed conversion rolls back and the next open converts."""
    store = await journal.legacy()
    await seed_rows(store, every_index(blob(["$m"], response="$r")))
    await store.close()
    journal.opened.remove(store)

    with (
        patch("mindroom.event_journal.legacy_turn_rows._convert", side_effect=RuntimeError("conversion failed")),
        pytest.raises(RuntimeError, match="conversion failed"),
    ):
        journal.upgraded()

    untouched = await journal.legacy()
    assert await legacy_tables(untouched) == 1
    assert await read(untouched, turn_rows.get_turn_record, "$m") is None
    await untouched.close()
    journal.opened.remove(untouched)
    converted = journal.upgraded()
    assert (await read(converted, turn_rows.get_turn_record, "$m")).response_event_id == "$r"
    assert await legacy_tables(converted) is None
    await converted.close()
    journal.opened.remove(converted)
    reopened = journal.upgraded()
    assert (await read(reopened, turn_rows.get_turn_record, "$m")).response_event_id == "$r"


_PRE_NIO1_SCHEMA = Path(__file__).parent / "fixtures" / "pre_nio1_journal.sql"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_a_released_journal_without_reply_tables_converts(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    backend: str,
) -> None:
    """A database from before durable consumers and reply records converts, though its upgrade retired membership."""
    schema = _PRE_NIO1_SCHEMA.read_text()
    blob_json = json.dumps(blob(["$m"], completed=False, source_event_prompts={"$m": "deleted text"}), sort_keys=True)
    rows = f"""
        INSERT INTO journal_identity VALUES (TRUE, 'generation');
        INSERT INTO redaction_tombstones VALUES ('{PRINCIPAL}', '{ROOM}', '$m');
        INSERT INTO turn_records VALUES ('{AGENT}', '$m', '$m', '{blob_json}');
    """  # noqa: S608 - fixed test values
    if backend == "sqlite":
        path = tmp_path / "journal.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(schema + rows)

        def connect() -> EventJournalStore:
            return EventJournalStore.open_sqlite(path)
    else:
        url = postgres_journal_schema_url(request.getfixturevalue("postgres_journal_url"))
        schema = schema.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        )
        with psycopg.connect(url) as connection:
            connection.execute(cast("LiteralString", schema + rows))

        def connect() -> EventJournalStore:
            return EventJournalStore.open_postgres(url)

    store = connect()
    try:
        found = await read(store, turn_rows.get_turn_record, "$m")
        assert found is not None
        assert found.source_event_prompts is None
        assert await read(store, turn_rows.redacted_events, ROOM, ["$m"]) == {"$m"}
        assert await legacy_tables(store) is None
    finally:
        await store.close()


async def _index_names(store: EventJournalStore, table: str, *, postgres: bool) -> set[str]:
    sql = (
        "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema() AND tablename = ?"
        if postgres
        else "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?"
    )
    rows = await store.backend.read(lambda transaction: transaction.fetchall(sql, (table,)))
    return {str(row["name"]) for row in rows}


async def test_the_upgrade_replaces_the_dense_turn_source_indexes(journal: Journal) -> None:
    """A journal holding the full turn-source indexes keeps only their partial replacements after the upgrade."""
    store = await journal.legacy()

    def dense(transaction: Transaction) -> None:
        transaction.execute("CREATE INDEX turn_sources_selected_revision ON turn_sources (agent_name, selected_rev_id)")
        transaction.execute("CREATE INDEX turn_sources_discovery ON turn_sources (agent_name, discovery_event_id)")

    await store.backend.write(dense)
    await store.close()
    journal.opened.remove(store)

    names = await _index_names(journal.upgraded(), "turn_sources", postgres=journal.postgres)

    assert {"turn_sources_selected_revision_set", "turn_sources_discovery_set"} <= names
    assert not names & {"turn_sources_selected_revision", "turn_sources_discovery"}


async def test_the_conversion_plans_with_statistics_it_then_forgets(tmp_path: Path) -> None:
    """The redactions run with SQLite statistics for the turn rows, which the upgrade drops once they are applied."""
    path = tmp_path / "journal.db"
    journal = Journal(lambda: EventJournalStore.open_sqlite(path), postgres=False, opened=[])
    store = await journal.legacy()
    await admit(store.principal(PRINCIPAL), "$m", room_id=ROOM)
    await seed_rows(store, every_index(blob(["$m"], response="$r")))

    def tombstone(transaction: Transaction) -> None:
        transaction.execute(
            "INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id) VALUES (?, ?, ?)",
            (PRINCIPAL, ROOM, "$m"),
        )

    await store.backend.write(tombstone)
    await store.close()
    journal.opened.remove(store)
    analyzed_during_redaction: list[set[str]] = []
    apply_redaction = legacy_turn_rows._apply_redaction

    def recording(transaction: Transaction, *args: str) -> None:
        rows = transaction.fetchall("SELECT tbl FROM sqlite_stat1")
        analyzed_during_redaction.append({str(row["tbl"]) for row in rows})
        apply_redaction(transaction, *args)

    with patch.object(legacy_turn_rows, "_apply_redaction", recording):
        await journal.upgraded().close()

    with closing(sqlite3.connect(path)) as database:
        remaining = {str(row[0]) for row in database.execute("SELECT tbl FROM sqlite_stat1")}
    assert analyzed_during_redaction
    assert {"turns", "turn_sources"} <= analyzed_during_redaction[-1]
    assert not remaining & {"turns", "turn_sources", "reply_spans", "reply_span_sources", "interactive_questions"}


async def test_a_turn_finds_its_room_through_an_earlier_login(journal: Journal) -> None:
    """A source only an earlier login of the agent admitted still places its turn in that room."""
    store = await journal.legacy()
    # The current login admitted something else, so the agent has two logins to look the source up under.
    await admit(store.principal(PRINCIPAL), "$other", room_id=ROOMS["b"])
    await admit(store.principal(EARLIER_LOGIN), "$m", room_id=ROOM)
    # Without a conversation target the room can only come from the admitted events.
    await seed_rows(store, every_index(blob(["$m"], response="$r", room=None)))
    await store.close()
    journal.opened.remove(store)

    upgraded = journal.upgraded()
    row = await upgraded.backend.read(
        lambda transaction: transaction.fetchone("SELECT room_id FROM turns WHERE anchor_event_id = '$m'"),
    )

    assert row is not None
    assert row["room_id"] == ROOM


async def test_the_runtime_writes_through_a_connection_opened_after_the_upgrade(tmp_path: Path) -> None:
    """The writer is not the connection the schema upgrade ran on, which keeps the row counts its statistics loaded."""
    upgraded_on: list[sqlite3.Connection] = []
    upgrade = upgrades.upgrade_schema

    def recording(transaction: Transaction, inspector: Any) -> None:  # noqa: ANN401
        upgraded_on.append(transaction.connection)
        upgrade(transaction, inspector)

    with patch("mindroom.event_journal.sqlite_backend.upgrade_schema", recording):
        store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    try:
        assert len(upgraded_on) == 1
        assert store.backend._writer is not upgraded_on[0]
    finally:
        await store.close()
