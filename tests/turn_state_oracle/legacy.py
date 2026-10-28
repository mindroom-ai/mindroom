"""Legacy inputs: every stored turn-record shape the codec still reads, as the databases and files that hold them.

Each fixture seeds raw ``turn_records`` rows, journal ``redaction_tombstones`` rows, or a pre-journal JSON ledger
file, and lists the actions to run after opening it; the oracle observes every event id these name. ``build_inputs`` writes them
under ``inputs/`` as a SQLite dump of the current journal schema plus those rows, and as the ledger file; a later
schema opens those dumps as an upgrade does, so they are built once and kept rather than re-recorded.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mindroom.event_journal import EventJournalStore
from tests.turn_state_oracle.actions import Action, op
from tests.turn_state_oracle.adapter import AGENT, EPOCH, PRINCIPAL, REQUESTER, ROOMS, SCOPE, TARGETS
from tests.turn_state_oracle.driver import INPUTS

# A day before the oracle's clock starts: fresh for retention until a cleanup lets 30 days pass.
_WRITTEN = EPOCH - 24 * 60 * 60


@dataclass(frozen=True)
class LegacyFixture:
    """One legacy input and what the oracle does after opening it."""

    rows: tuple[tuple[str, dict[str, Any]], ...] = ()
    tombstones: tuple[tuple[str, str, str], ...] = ()
    ledger: object = None
    actions: tuple[Action, ...] = ()


def _raw(
    sources: list[str],
    *,
    response: str | None = None,
    room: str | None = None,
    **extra: object,
) -> dict[str, Any]:
    """Return one stored record in the shape the JSON ledger and early journal rows had."""
    record: dict[str, Any] = {
        "anchor_event_id": sources[-1],
        "source_event_ids": sources,
        "redacted_source_event_ids": [],
        "response_event_id": response,
        "completed": response is not None,
        "timestamp": _WRITTEN,
    }
    if room is not None:
        record |= {
            "response_owner": AGENT,
            "requester_id": REQUESTER,
            "history_scope": SCOPE.to_metadata(),
            "conversation_target": TARGETS[room].to_metadata(),
        }
    return record | extra


def _rows(record: dict[str, Any], *index_event_ids: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    indexes = index_event_ids or (*record["source_event_ids"], *record.get("discovery_event_ids", ()))
    return tuple((event_id, record) for event_id in indexes)


def _replay(source: str, ts: int, **flags: object) -> dict[str, object]:
    return {
        "source_event_id": source,
        "timestamp_ms": ts,
        "redacted": False,
        "response_event_id": None,
        "legacy_summary_provenance": False,
    } | flags


_COALESCED_ALIAS = {
    "$m2": {"sender": REQUESTER, "discovery_event_id": "$a2"},
    "$m3": {"sender": REQUESTER},
}

FIXTURES: dict[str, LegacyFixture] = {
    # v2026.9.42 and earlier: source revision summaries with no revision_replay key, rebuilt on every read.
    "no_revision_replay_key": LegacyFixture(
        rows=(
            *_rows(
                _raw(
                    ["$m1"],
                    response="$r1",
                    room="a",
                    pending_redaction_cleanup_event_ids=[],
                    source_event_prompts={"$m1": "edited prompt"},
                    source_event_revisions={"$m1": [20, "$e1"]},
                    suppressed_source_event_revisions={"$m1": [30, "$e2"]},
                ),
            ),
            *_rows(
                _raw(
                    ["$m2", "$m3"],
                    response="$r2",
                    room="a",
                    discovery_event_ids=["$a2"],
                    source_event_metadata=_COALESCED_ALIAS,
                    source_event_prompts={"$m2": "relayed edit", "$m3": "second edit"},
                    source_event_revisions={"$a2": [40, "$e4"], "$m3": [30, "$e3"]},
                ),
            ),
        ),
        actions=(
            op("reopen"),
            op("edit", source="$m1", edit="$e5", ts=50),
            op("reopen"),
            op("redact", target="$e1", room="a"),
            op("redact", target="$e4", room="a"),
        ),
    ),
    # v2026.10.145 and earlier: redaction cleanup obligations stored on the record and its replay rows.
    "cleanup_obligations": LegacyFixture(
        rows=_rows(
            _raw(
                ["$m2", "$m3"],
                response="$r2",
                room="a",
                redacted_source_event_ids=["$m2"],
                pending_redaction_cleanup_event_ids=["$m2"],
                source_event_prompts={"$m3": "kept"},
                source_event_revisions={"$m3": [30, "$e3"]},
                revision_replay={
                    "$e2": _replay("$m2", 20, redacted=True, cleanup_pending=True),
                    "$e3": _replay("$m3", 30, response_event_id="$r2", cleanup_pending=True),
                },
            ),
        ),
        actions=(
            op("cleanup", days=31),
            op("reopen"),
            op("record_edit", source="$m3", edit="$e4", ts=40),
            op("reopen"),
        ),
    ),
    # v2026.10.227 and earlier: Stop, edit order, and correlation state; suppressed revisions, whose last writer
    # went in v2026.10.228.
    "retired_fields": LegacyFixture(
        rows=(
            *_rows(
                _raw(
                    ["$m4"],
                    response="$r4",
                    room="b",
                    revision_replay={},
                    user_stop_receipt_order=7,
                    user_stop_settled_receipt_order=7,
                    latest_edit_receipt_order=9,
                    correlation_id="corr-1",
                ),
            ),
            *_rows(
                _raw(
                    ["$m5"],
                    response="$r5",
                    room="b",
                    revision_replay={"$e5": _replay("$m5", 50, response_event_id="$r5")},
                    source_event_prompts={"$m5": "selected"},
                    source_event_revisions={"$m5": [50, "$e5"]},
                    suppressed_source_event_revisions={"$m5": [60, "$e6"]},
                ),
            ),
        ),
        actions=(op("edit", source="$m4", edit="$e4", ts=40), op("terminal", sources=["$m5"]), op("reopen")),
    ),
    # v2026.10.30 and earlier: a room-less tombstone of an event another room's redaction named, beside journal
    # tombstones of this principal and of an earlier Matrix login of the same agent.
    "room_less_tombstone": LegacyFixture(
        rows=_rows(_raw(["$m6"], redacted_source_event_ids=["$m6"], revision_replay={})),
        tombstones=(
            (PRINCIPAL, ROOMS["b"], "$m6"),
            (PRINCIPAL, ROOMS["a"], "$x1"),
            (f"{AGENT}@@old_login:example.org", ROOMS["a"], "$x2"),
        ),
        actions=(
            op("admit", events=["$m6", "$m7"], room="a"),
            op("pending", sources=["$m7"], room="a"),
            op("prepare", room="a", sources=["$m7"], history=[["$m6", None, 0]]),
            op("cleanup", days=31, unsettled=["$m7"]),
            op("prepare", room="a", sources=["$m7"], history=[["$m6", None, 0]]),
            op("reopen"),
        ),
    ),
    # Per-index rows of one coalesced turn that disagree: a stale pending copy beside a completed one, a turn
    # whose second index was never adopted, and an owner whose sibling row names a source outside it.
    "coalesced_rows_disagree": LegacyFixture(
        rows=(
            ("$m1", _raw(["$m1", "$m2"], response="$r1", room="a", revision_replay={})),
            ("$m2", _raw(["$m1", "$m2"], room="a", revision_replay={}, timestamp=_WRITTEN - 10)),
            ("$m3", _raw(["$m3", "$m4"], response="$r3", room="a", revision_replay={})),
            ("$m5", _raw(["$m5", "$m6"], room="a", revision_replay={})),
            ("$m6", _raw(["$m6", "$m7"], response="$r6", room="a", revision_replay={})),
        ),
        actions=(
            op("pending", sources=["$m5"], room="a"),
            op("pending", sources=["$m5"], room="a"),
            op("final_ack", turn="$m2", response="$r2"),
            op("pending", sources=["$m3", "$m4"], room="a"),
            op("cleanup"),
            op("reopen"),
        ),
    ),
    # Revisions a pre-v2026.9.43 summary consumed through their source, one redacted on the owner and one only by
    # a standalone room-less tombstone the warm-up joins.
    "legacy_summary_provenance": LegacyFixture(
        rows=(
            *_rows(
                _raw(
                    ["$m1"],
                    response="$r1",
                    room="a",
                    source_event_prompts={"$m1": "latest"},
                    source_event_revisions={"$m1": [30, "$e3"]},
                    revision_replay={
                        "$e1": _replay(
                            "$m1",
                            10,
                            redacted=True,
                            response_event_id="$r1",
                            legacy_summary_provenance=True,
                        ),
                        "$e2": _replay("$m1", 20, legacy_summary_provenance=True),
                    },
                ),
            ),
            *_rows(_raw(["$e2"], redacted_source_event_ids=["$e2"], revision_replay={})),
        ),
        actions=(op("cleanup", days=31), op("reopen"), op("edit", source="$m1", edit="$e4", ts=40), op("reopen")),
    ),
    # The schema-version-1 JSON ledger of v2026.8.30 and earlier, overlapping one row the runtime already wrote.
    "pre_journal_ledger": LegacyFixture(
        rows=(("$m2", _raw(["$m2", "$m3"], response="$r2-db", revision_replay={})),),
        ledger={
            "schema_version": 1,
            "records": {
                "$m1": _raw(["$m1"], response="$r1", pending_redaction_cleanup_event_ids=[]),
                "$m2": (
                    coalesced := _raw(["$m2", "$m3"], response="$r2", source_event_prompts={"$m2": "a", "$m3": "b"})
                ),
                "$m3": coalesced,
                "$m4": _raw(["$m4"], room="a", source_event_revisions={"$m4": [40, "$e4"]}),
                "$m5": _raw(["$m5"], redacted_source_event_ids=["$m5"]),
                "$m6": (aliased := _raw(["$m6"], response="$r6", discovery_event_ids=["$a1"])),
                "$a1": aliased,
                "$m7": {"source_event_ids": ["$m7"], "completed": True},
            },
        },
        actions=(op("reopen"), op("cleanup", days=31), op("reopen")),
    ),
    # v2026.7.101 and earlier: an unversioned event-to-record map, processed as zero rows.
    "unversioned_ledger": LegacyFixture(
        ledger={"$m1": _raw(["$m1"], response="$r1")},
        actions=(op("reopen"),),
    ),
}


async def _seed(database: Path, fixture: LegacyFixture) -> None:
    journal = EventJournalStore.open_sqlite(database)

    def insert(transaction: Any) -> None:  # noqa: ANN401
        for index_event_id, record in fixture.rows:
            transaction.execute(
                "INSERT INTO turn_records (agent_name, index_event_id, anchor_event_id, record_json) VALUES (?, ?, ?, ?)",
                (AGENT, index_event_id, record["anchor_event_id"], json.dumps(record, sort_keys=True)),
            )
        for principal_id, room_id, event_id in fixture.tombstones:
            transaction.execute(
                "INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id) VALUES (?, ?, ?)",
                (principal_id, room_id, event_id),
            )

    await journal.backend.write(insert)
    await journal.close()


def build_inputs() -> None:
    """Write every fixture's database dump and ledger file under ``inputs/``."""
    INPUTS.mkdir(parents=True, exist_ok=True)
    for name, fixture in FIXTURES.items():
        if fixture.rows or fixture.tombstones:
            with tempfile.TemporaryDirectory() as root:
                database = Path(root) / "journal.db"
                asyncio.run(_seed(database, fixture))
                with closing(sqlite3.connect(database)) as connection:
                    dump = "\n".join(connection.iterdump()) + "\n"
            (INPUTS / f"{name}.sql").write_text(dump, encoding="utf-8")
        if fixture.ledger is not None:
            (INPUTS / f"{name}.ledger.json").write_text(
                json.dumps(fixture.ledger, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
