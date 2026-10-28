"""One-time conversion of stored turn records and redaction tombstones into turn rows.

Two stored shapes reach here: the ``turn_records`` table, one JSON blob per
event that indexed a turn, converted by the schema upgrade; and the
pre-journal JSON ledger file, converted when a runtime first loads the agent.
Both decode with the handled-turn codec and run the same steps, so they agree
on every rule below.

Row ownership is the authority, not a blob's declared identity. Per-index
copies of one turn can disagree (a stale pending copy beside a completed one,
an alias row a relay overwrote, a partial import), and what a reader saw for an
event was always the blob its own row held. A group of rows sharing one blob
therefore owns exactly the declared events whose row it holds, plus declared
sources no row names at all, for which the blob is the only record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.handled_turns import TurnRecordCodec
from mindroom.logging_config import get_logger
from mindroom.turn_record import SourceEventMetadata, SourceVisibleEcho, TurnRecord, canonicalize_turn_record

from . import journal, turn_rows

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from mindroom.turn_record import SourceEventRevision

    from .backend import Backend, Transaction

logger = get_logger(__name__)

_LEDGER_RECORDS_KEY = "records"


@dataclass(frozen=True, slots=True)
class _Group:
    """One stored blob, decoded, and the index entries that still point at it."""

    record: TurnRecord
    rows: frozenset[str]

    @property
    def precedence(self) -> tuple[bool, float, str]:
        return (self.record.completed, self.record.timestamp, self.record.anchor_event_id or "")


# LEGACY_COMPAT: Turn records stored as one JSON blob per indexed event.
# Legacy format: turn_records rows (agent_name, index_event_id, anchor_event_id, record_json), one copy of a turn's
# record under each of its sources and discovery aliases, read by the handled-turn codec, whose own markers cover the
# retired keys it drops on read.
# Last legacy release: v2026.10.634, the newest release when this conversion was written, still writes them (the
# table arrived in v2026.8.31); replacement: the release that ships turn rows stores turns in turns, turn_sources,
# turn_aliases, and turn_revisions.
# Handling: the schema upgrade converts every agent's rows by the rules marked below and drops turn_records, all in
# the schema transaction, so a failure leaves the old table in place for the next open to retry.
# Coverage: tests/test_turn_rows_conversion.py::test_every_oracle_legacy_input_converts_to_its_recorded_observables,
# tests/test_turn_rows_conversion.py::test_a_failed_conversion_rolls_back_and_the_next_open_converts.
def upgrade_turn_records_to_rows(
    transaction: Transaction,
    existing_tables: frozenset[str],
    outbox_columns: frozenset[str],
    *,
    schema_ddl: Sequence[str],
) -> None:
    """Convert turn records and redaction tombstones into turn rows, inside the schema transaction.

    ``outbox_columns`` is the delivery outbox as earlier upgrades left it,
    empty when there is none. ``schema_ddl`` is the whole current schema, run
    first: a redaction's effects read membership, reply, and continuation
    tables that a database from an earlier release gains only from these
    statements, and running them twice changes nothing.
    """
    if "turn_records" not in existing_tables and "redaction_tombstones" not in existing_tables:
        return
    for statement in schema_ddl:
        transaction.execute(statement)
    if "redaction_tombstones" in existing_tables:
        _copy_redaction_tombstones(transaction)
    if "turn_records" in existing_tables:
        deliveries = _outstanding_final_deliveries(transaction) if outbox_columns else {}
        agents = transaction.fetchall("SELECT agent_name FROM turn_records ORDER BY agent_name/*bytes*/")
        for agent_name in dict.fromkeys(str(row["agent_name"]) for row in agents):
            entries = transaction.fetchall(
                "SELECT index_event_id, anchor_event_id, record_json FROM turn_records "
                "WHERE agent_name = ? ORDER BY index_event_id/*bytes*/",
                (agent_name,),
            )
            groups = _groups(
                (str(entry["index_event_id"]), str(entry["anchor_event_id"]), str(entry["record_json"]))
                for entry in entries
            )
            converted = _convert(
                transaction,
                agent_name,
                groups,
                occupied=frozenset(),
                final_deliveries=deliveries.get(agent_name, {}),
            )
            logger.info("turn_records_converted", agent=agent_name, row_count=len(entries), turn_count=converted)
    transaction.execute("DROP TABLE IF EXISTS turn_records")
    transaction.execute("DROP TABLE IF EXISTS redaction_tombstones")


# LEGACY_COMPAT: Redaction tombstones scoped to one journal principal.
# Legacy format: redaction_tombstones (principal_id, room_id, redacted_event_id), one row per Matrix login of an agent,
# deleted when that login left the room.
# Last legacy release: v2026.10.634, the newest release when this conversion was written (the table arrived in
# v2026.8.31); replacement: the release that ships turn rows records each redaction once per agent in
# event_redactions and keeps it when the bot leaves.
# Handling: every row becomes (agent of its principal, room, event) and applies the redaction to every login of that
# agent, as a live redaction does; the table is dropped.
# Coverage: tests/test_turn_rows_conversion.py::test_journal_tombstones_apply_to_every_login_of_their_agent.
def _copy_redaction_tombstones(transaction: Transaction) -> None:
    for row in transaction.fetchall(
        "SELECT principal_id, room_id, redacted_event_id FROM redaction_tombstones "
        "ORDER BY principal_id/*bytes*/, room_id/*bytes*/, redacted_event_id/*bytes*/",
    ):
        if agent_name := turn_rows.agent_of_principal(str(row["principal_id"])):
            _redact(transaction, agent_name, str(row["room_id"]), str(row["redacted_event_id"]))


def _redact(transaction: Transaction, agent_name: str, room_id: str, event_id: str) -> None:
    # No redaction event is admitted here, so no receipt order exists; hydration installs use 0 the same way.
    journal.redact_for_agent(transaction, agent_name, room_id=room_id, event_id=event_id, receipt_order=0)


def _groups(entries: Iterable[tuple[str, str, str]]) -> list[_Group]:
    """Group index entries by the exact blob they hold, decoding each blob once.

    An entry the codec rejects for its own index belongs to no group, so the
    event reads as having no row, as the warm-up that dropped it read it.
    """
    indexes: dict[tuple[str, str], list[str]] = {}
    for index_event_id, anchor_event_id, record_json in entries:
        indexes.setdefault((anchor_event_id, record_json), []).append(index_event_id)
    groups: list[_Group] = []
    for (_anchor, record_json), index_event_ids in indexes.items():
        try:
            raw = json.loads(record_json)
            decoded = {index: TurnRecordCodec._from_ledger_record(index, raw) for index in index_event_ids}
        except (TypeError, ValueError):
            continue
        owned = [index for index, record in decoded.items() if record is not None]
        if owned:
            record = decoded[owned[0]]
            assert record is not None
            groups.append(_Group(record, frozenset(owned)))
    return groups


def _convert(
    transaction: Transaction,
    agent_name: str,
    groups: Sequence[_Group],
    *,
    occupied: frozenset[str],
    final_deliveries: Mapping[str, str],
) -> int:
    """Write the turns these blob groups own, after taking their redactions; return how many turns were written.

    ``occupied`` names events the row store already indexes, which keep their
    owner. ``final_deliveries`` maps an outstanding non-AI FINAL delivery to its room.
    """
    _extract_redactions(transaction, agent_name, groups)
    owned, evidence = _owned_members(groups, occupied)
    rooms = _admitted_rooms(
        transaction,
        agent_name,
        {
            source
            for group, physical, _aliases in owned
            if group.record.conversation_target is None
            for source in physical
        },
    )
    written = 0
    for group, physical, aliases in owned:
        record = group.record
        room_id = (
            record.conversation_target.room_id
            if record.conversation_target is not None
            else _single_room(rooms, physical)
        )
        converted = _converted_turn(record, physical, aliases, room_id=room_id)
        if converted is None:
            continue
        turn_key = turn_rows.insert_turn(transaction, agent_name, converted, room_id=room_id)
        _bind_outstanding_delivery(transaction, agent_name, turn_key, converted, room_id, final_deliveries)
        written += 1
    for record in evidence:
        turn_rows.insert_turn(transaction, agent_name, record, room_id=None)
        written += 1
    return written


# LEGACY_COMPAT: Redactions recorded inside turn records.
# Legacy format: a record's redacted_source_event_ids and redacted revision_replay entries, and a standalone record
# indexed by a redacted revision whose owners' flags each warm-up joined; releases up to v2026.9.383 also wrote them
# onto turns recorded in another room.
# Last legacy release: v2026.10.634, the newest release when this conversion was written (since v2026.7.197 and
# v2026.9.43); replacement: the release that ships turn rows keeps redactions only in event_redactions.
# Handling: from every blob, including ones the row ownership below discards, each redaction of a record that has a
# conversation_target is applied in that conversation's room, and a revision such a record selected or consumed is
# redacted there when the standalone record tombstones it, as the warm-up joined it. Redactions of records without a
# conversation_target are dropped: they cannot be told apart from the room-less tombstones of v2026.10.30 and earlier
# that wedge another room's thread (#2544), and the legitimate ones already age out within 30 days.
# Coverage: tests/test_turn_rows_conversion.py::test_every_oracle_legacy_input_converts_to_its_recorded_observables.
def _extract_redactions(transaction: Transaction, agent_name: str, groups: Sequence[_Group]) -> None:
    tombstoned = {index for group in groups for index in group.rows if _tombstones(group.record, index)}
    for group in groups:
        record = group.record
        if record.conversation_target is None:
            continue
        room_id = record.conversation_target.room_id
        replay = record.revision_replay or {}
        selected = {revision_id for _timestamp, revision_id in (record.source_event_revisions or {}).values()}
        redacted = {
            *record.redacted_source_event_ids,
            *(revision_id for revision_id, revision in replay.items() if revision.redacted),
            *(tombstoned & {*replay, *selected}),
        }
        for event_id in sorted(redacted):
            _redact(transaction, agent_name, room_id, event_id)
        _record_summary_provenance(transaction, agent_name, room_id, record)


def _tombstones(record: TurnRecord, index_event_id: str) -> bool:
    """Return whether the record an event indexes marks that event deleted, as warm-up reconciliation read it."""
    return index_event_id in record.redacted_source_event_ids or (
        index_event_id in record.source_event_ids and index_event_id not in record.replay_source_event_ids
    )


# LEGACY_COMPAT: Revisions consumed only through a pre-v2026.9.43 source summary.
# Legacy format: a revision_replay entry with legacy_summary_provenance, which the codec also restores for a record
# with source_event_revisions and no revision_replay key.
# Last legacy release: v2026.9.42 wrote source summaries without revision replay, and every later release carries the
# flag forward; replacement: v2026.9.43 records per-revision provenance, and turn rows keep the summary mapping in
# legacy_summary_revisions, which history cleanup of redacted events reads.
# Handling: each such revision of a record with a conversation_target becomes (agent, room, revision, source).
# Coverage: tests/test_turn_rows_conversion.py::test_every_oracle_legacy_input_converts_to_its_recorded_observables.
def _record_summary_provenance(transaction: Transaction, agent_name: str, room_id: str, record: TurnRecord) -> None:
    for revision_id, revision in (record.revision_replay or {}).items():
        if revision.legacy_summary_provenance:
            transaction.execute(
                "INSERT INTO legacy_summary_revisions (agent_name, room_id, revision_id, source_event_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (agent_name, room_id, revision_id, source_event_id) DO NOTHING",
                (agent_name, room_id, revision_id, revision.source_event_id),
            )


def legacy_summary_sources(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: Iterable[str],
) -> dict[str, str]:
    """Return revision to source for the legacy summary revisions in ``room_id`` that name one of these events."""
    ids = tuple(dict.fromkeys(event_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = transaction.fetchall(
        f"SELECT revision_id, source_event_id FROM legacy_summary_revisions "  # noqa: S608
        f"WHERE agent_name = ? AND room_id = ? "
        f"AND (revision_id IN ({placeholders}) OR source_event_id IN ({placeholders}))",
        (agent_name, room_id, *ids, *ids),
    )
    return {str(row["revision_id"]): str(row["source_event_id"]) for row in rows}


# LEGACY_COMPAT: Index rows of one turn that disagree about it.
# Legacy format: turn_records index rows whose blobs differ for one turn (a stale pending copy beside a completed one,
# an original's row a completed relay's alias row overwrote, a partially adopted legacy import) and declared sources
# no row names, including groups left with only alias rows.
# Last legacy release: v2026.10.634, the newest release when this conversion was written; replacement: the release
# that ships turn rows keeps one exclusive physical owner per source.
# Handling: a group of rows sharing one blob owns the declared sources and aliases whose row it holds, plus declared
# sources no row names, which the completed, then most recent, declaring blob claims. A completed group with only
# alias rows keeps each alias answered as content-free completion evidence; an unfinished one carries no proof and
# is discarded.
# Coverage: tests/test_turn_rows_conversion.py::test_rows_own_their_turn_and_alias_only_survivors_stay_answered,
# tests/test_turn_rows_conversion.py::test_a_declared_source_without_a_row_is_answered_by_its_blob.
def _owned_members(
    groups: Sequence[_Group],
    occupied: frozenset[str],
) -> tuple[list[tuple[_Group, list[str], list[str]]], list[TurnRecord]]:
    """Return each group's owned physical sources and aliases, and the alias evidence of source-less completed ones."""
    row_owner = {index: group for group in groups for index in group.rows}
    rowless: dict[str, _Group] = {}
    for group in groups:
        for source in group.record.source_event_ids:
            if source not in row_owner and source not in occupied:
                claimant = rowless.get(source)
                if claimant is None or group.precedence > claimant.precedence:
                    rowless[source] = group
    owned: list[tuple[_Group, list[str], list[str]]] = []
    evidence: list[TurnRecord] = []
    for group in groups:
        physical = [
            source
            for source in group.record.source_event_ids
            if source not in occupied and (row_owner.get(source) is group or rowless.get(source) is group)
        ]
        aliases = [
            alias
            for alias in group.record.discovery_event_ids
            if alias not in occupied and row_owner.get(alias) is group
        ]
        if physical:
            owned.append((group, physical, aliases))
        elif group.record.completed:
            evidence.extend(
                TurnRecord.create([alias], completed=True, timestamp=group.record.timestamp) for alias in aliases
            )
    return owned, evidence


def _admitted_rooms(transaction: Transaction, agent_name: str, event_ids: set[str]) -> dict[str, set[str]]:
    """Return the rooms each event was admitted in under any login of this agent."""
    rooms: dict[str, set[str]] = {}
    ordered = sorted(event_ids)
    for start in range(0, len(ordered), 500):
        batch = ordered[start : start + 500]
        placeholders = ", ".join("?" for _ in batch)
        for row in transaction.fetchall(
            f"SELECT principal_id, event_id, room_id FROM journal_events WHERE event_id IN ({placeholders})",  # noqa: S608
            tuple(batch),
        ):
            if turn_rows.agent_of_principal(str(row["principal_id"])) == agent_name:
                rooms.setdefault(str(row["event_id"]), set()).add(str(row["room_id"]))
    return rooms


def _single_room(rooms: Mapping[str, set[str]], sources: Sequence[str]) -> str | None:
    found = {room_id for source in sources for room_id in rooms.get(source, ())}
    return next(iter(found)) if len(found) == 1 else None


# LEGACY_COMPAT: Turn records without proof of their room, per-source attribution, or a per-source echo.
# Legacy format: a record without conversation_target; a record without source_event_metadata, which read as its
# requester's when it had one source; and one visible_echo_event_id per record, which an unfinished record's
# finalization also wrote as its response_event_id. suppressed_source_event_revisions has had no writer since
# v2026.10.228.
# Last legacy release: v2026.10.634, the newest release when this conversion was written, writes the first three
# (suppressed revisions: v2026.10.227); replacement: the release that ships turn rows names every turn's room,
# attributes each source, and keeps echoes per source.
# Handling: the room comes from the conversation target, else from the one room every login of the agent admitted
# the turn's sources in; without one, a completed turn stays only as content-free completion evidence (no room,
# prompt, voice, attribution, revisions, response, or context) and an unfinished one is dropped. A one-source record
# without metadata names its requester as its sender; a coalesced one names nobody, so its sources stay unattributed
# even when only one of them survives. The echo is copied to every source the record owns, and an unfinished record
# whose response is its echo loses that response. Suppressed revisions are dropped.
# Coverage: tests/test_turn_rows_conversion.py::test_room_less_records_keep_only_their_completion,
# tests/test_turn_rows_conversion.py::test_attribution_is_fixed_before_membership_changes,
# tests/test_turn_rows_conversion.py::test_a_legacy_echo_reaches_every_source_and_is_not_an_answer.
def _converted_turn(
    record: TurnRecord,
    physical: Sequence[str],
    aliases: Sequence[str],
    *,
    room_id: str | None,
) -> TurnRecord | None:
    echo = record.visible_echo_event_id
    echoes = {source: SourceVisibleEcho(echo, record.visible_echo_is_fallback) for source in physical} if echo else None
    if room_id is None:
        if not record.completed:
            return None
        return TurnRecord.create(
            physical,
            discovery_event_ids=aliases,
            completed=True,
            visible_echoes=echoes,
            timestamp=record.timestamp,
        )
    response_event_id = record.response_event_id
    if not record.completed and echo is not None and response_event_id == echo:
        response_event_id = None
    return canonicalize_turn_record(
        record,
        source_event_ids=physical,
        discovery_event_ids=aliases,
        anchor_event_id=physical[-1],
        redacted_source_event_ids=(),
        response_event_id=response_event_id,
        visible_echo_event_id=None,
        visible_echo_is_fallback=None,
        visible_echoes=echoes,
        source_event_revisions=_selections_by_source(record),
        suppressed_source_event_revisions=None,
        source_event_metadata=_attribution(record),
    )


def _attribution(record: TurnRecord) -> Mapping[str, SourceEventMetadata]:
    if record.source_event_metadata is not None:
        return record.source_event_metadata
    if len(record.source_event_ids) == 1 and record.requester_id is not None:
        return {record.source_event_ids[0]: SourceEventMetadata(record.requester_id)}
    return {}


def _selections_by_source(record: TurnRecord) -> dict[str, SourceEventRevision]:
    """Key each selected revision by the physical source it edits, keeping the newest when an alias also names one."""
    selections: dict[str, SourceEventRevision] = {}
    for event_id, revision in (record.source_event_revisions or {}).items():
        source = record.prompt_source_event_id(event_id)
        selections[source] = max(selections.get(source, revision), revision)
    return selections


def _outstanding_final_deliveries(transaction: Transaction) -> dict[str, dict[str, str]]:
    """Return, per agent, each unacknowledged non-AI FINAL delivery recovery can still send, with its room.

    A delivery two logins of one agent both hold is ambiguous and left out.
    """
    found: dict[str, dict[str, list[str]]] = {}
    for row in transaction.fetchall(
        "SELECT principal_id, delivery_id, room_id FROM matrix_delivery_outbox "
        "WHERE stage = 'final' AND reply_id IS NULL AND acknowledged_event_id IS NULL "
        "AND retired = 0 AND permanent_failure_reason IS NULL",
    ):
        agent_name = turn_rows.agent_of_principal(str(row["principal_id"]))
        found.setdefault(agent_name, {}).setdefault(str(row["delivery_id"]), []).append(str(row["room_id"]))
    return {
        agent_name: {delivery_id: rooms[0] for delivery_id, rooms in deliveries.items() if len(rooms) == 1}
        for agent_name, deliveries in found.items()
    }


# LEGACY_COMPAT: Non-AI FINAL deliveries whose turn the acknowledgement looked up when it arrived.
# Legacy format: an unacknowledged FINAL outbox row without reply identity, whose acknowledgement completed the turn
# anchored at its delivery id through terminal_turn_record.
# Last legacy release: v2026.10.634, the newest release when this conversion was written; replacement: the release
# that ships turn rows binds the turn in the transaction that makes the FINAL durable.
# Handling: such a row still deliverable binds the converted turn anchored at its delivery id in its room; with no
# such turn, or two rows for one delivery, it stays unbound and its acknowledgement completes nothing, as before.
# Coverage: tests/test_turn_rows_conversion.py::test_an_outstanding_final_delivery_completes_its_turn_after_upgrade.
def _bind_outstanding_delivery(
    transaction: Transaction,
    agent_name: str,
    turn_key: str,
    record: TurnRecord,
    room_id: str | None,
    final_deliveries: Mapping[str, str],
) -> None:
    anchor = record.source_event_ids[-1]
    if room_id is not None and final_deliveries.get(anchor) == room_id:
        turn_rows.bind_final_delivery(transaction, agent_name, delivery_id=anchor, room_id=room_id, turn_key=turn_key)


def import_legacy_ledger_rows(transaction: Transaction, agent_name: str, raw_ledger: object) -> int:
    """Convert one agent's pre-journal ledger into turn rows; return how many turns were written.

    An event the row store already indexes keeps its owner, so an interrupted
    or repeated import cannot overwrite newer state, while every redaction the
    file records still applies.
    """
    # LEGACY_COMPAT: Unversioned event-to-record handled-turn JSON maps.
    # Legacy format: Top-level event-to-record handled-turn JSON map.
    # Last legacy release: v2026.7.101; replacement: v2026.7.102 intentionally rejected this shape.
    # Handling: Process as zero rows; the caller still renames the file to .imported, preserving its bytes.
    # Coverage: tests/test_turn_rows_conversion.py::test_an_unversioned_ledger_imports_nothing.
    records = (
        cast("Mapping[str, object]", raw_ledger).get(_LEDGER_RECORDS_KEY) if isinstance(raw_ledger, Mapping) else None
    )
    if not isinstance(records, Mapping):
        return 0
    # LEGACY_COMPAT: Schema-v1 handled-turn JSON before journal ownership.
    # Legacy format: Schema-version-1 handled-turn JSON ledger, one record per indexed event, before journal ownership.
    # Last legacy release: v2026.8.30; replacement: v2026.8.31 moved records into the journal database.
    # Handling: Entries group and convert as turn_records rows do; events the row store indexes keep their owner.
    # Coverage: tests/test_turn_rows_conversion.py::test_an_interrupted_import_into_a_populated_store_keeps_its_owners.
    entries = [
        (
            str(index_event_id),
            str(cast("Mapping[str, object]", raw_record).get("anchor_event_id")),
            json.dumps(raw_record, sort_keys=True),
        )
        for index_event_id, raw_record in records.items()
        if isinstance(raw_record, Mapping)
    ]
    groups = _groups(entries)
    candidates = {
        event_id
        for group in groups
        for event_id in (*group.rows, *group.record.source_event_ids, *group.record.discovery_event_ids)
    }
    occupied = frozenset(
        event_id for event_id in candidates if turn_rows.get_turn_record(transaction, agent_name, event_id) is not None
    )
    deliveries = _outstanding_final_deliveries(transaction).get(agent_name, {})
    return _convert(transaction, agent_name, groups, occupied=occupied, final_deliveries=deliveries)


async def import_legacy_ledger_file(backend: Backend, *, path: Path | None, agent_name: str) -> int:
    """Import an agent's pre-journal ledger file once, then rename it; return how many turns were written.

    The file's presence is the only trigger and its rename the only marker:
    the import commits in one transaction before the rename, and a crash in
    between repeats an import whose every event is then already indexed.
    """
    if path is None or not path.exists():
        return 0
    raw = json.loads(path.read_text())
    imported = await backend.write(lambda transaction: import_legacy_ledger_rows(transaction, agent_name, raw))
    path.replace(path.with_suffix(f"{path.suffix}.imported"))
    logger.info("handled_turn_ledger_imported", agent=agent_name, imported_turn_count=imported)
    return imported


__all__ = [
    "import_legacy_ledger_file",
    "import_legacy_ledger_rows",
    "legacy_summary_sources",
    "upgrade_turn_records_to_rows",
]
