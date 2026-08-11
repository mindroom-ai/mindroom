"""Persist canonical turn records for one runtime entity.

"Has this turn finished?" is the fact this module owns, and it now lives in the
journal's own database rather than a JSON file beside it. That is the whole
point: a terminal record and the settlement of the journal sources it answers
can only agree by being in one transaction, and a separate substrate can never
join one. Everything that existed to make two substrates approximately agree --
a write-behind queue, durability barriers, a retry timer, corruption
quarantine, and a startup pass that rejoined acknowledged deliveries to records
that had not caught up -- is deleted rather than ported, because awaiting the
write *is* the durability wait.

Reads stay synchronous and are served from in-memory state shared by every
ledger for one agent, so sibling instances in a process observe each other's
writes. That map is populated once by ``load`` and not lazily: a synchronous
read cannot await a database, and pretending otherwise is exactly the
sync-over-async bridge this change removes. Callers warm the ledger during
startup, before anything can ask it a question.

The scope is the agent, not the journal principal. A turn record is the proof
that a message was already answered, and that stays true across a re-login,
while every other table here is only meaningful beside the sync that produced
it. Transactionality comes from sharing the database, not the scope key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from mindroom import constants
from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.turn_record import (
    SourceEventMetadata,
    SourceEventRevision,
    TurnRecord,
    canonical_optional_string,
    canonical_source_event_ids,
    canonicalize_turn_record,
    merge_edit_facts,
    same_turn_identity,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

    from mindroom.event_journal.store import TurnRecordStore

logger = get_logger(__name__)

__all__ = [
    "HandledTurnLedger",
    "SourceEventMetadata",
    "SourceEventRevision",
    "TurnRecord",
    "TurnRecordCodec",
    "canonicalize_turn_record",
    "legacy_responses_file_path",
    "merge_edit_facts",
    "with_user_stop",
]

_TURN_RECORD_SCHEMA_VERSION = 1
_LEDGER_RECORDS_KEY = "records"


def legacy_responses_file_path(storage_path: Path, agent_name: str) -> Path:
    """Return where a pre-journal MindRoom kept this agent's handled turns.

    Named rather than spelled out at the one call site because it is half of a
    contract with a version that is no longer in this tree: the writer is gone,
    so nothing here fails if the reader drifts off the path that writer used.
    It would simply find no file, import nothing, and re-answer the backlog of
    every installation being upgraded -- silently, and only in production.
    Giving the path a name is what lets a test pin it against the bytes the old
    version actually wrote.

    See ``HandledTurnLedger._import_legacy_ledger`` for what is done with it.
    """
    return storage_path / "tracking" / f"{agent_name}_responded.json"


def with_user_stop(
    turn_record: TurnRecord,
    response_event_id: str,
    stop_receipt_order: int,
    *,
    delivery_settled: bool = False,
) -> TurnRecord:
    """Return the monotonic durable state for one admitted STOP callback."""
    if isinstance(stop_receipt_order, bool) or stop_receipt_order <= 0:
        msg = "User-stop receipt order must be positive"
        raise ValueError(msg)
    return canonicalize_turn_record(
        turn_record,
        response_event_id=response_event_id,
        completed=True,
        user_stop_receipt_order=max(
            stop_receipt_order,
            turn_record.user_stop_receipt_order or stop_receipt_order,
        ),
        user_stop_settled_receipt_order=max(
            turn_record.user_stop_settled_receipt_order or 0,
            stop_receipt_order if delivery_settled else 0,
        )
        or None,
        timestamp=0.0,
    )


class TurnRecordCodec:
    """Encode the canonical record into its two intentional physical projections."""

    @staticmethod
    def schema_version() -> int:
        """Return the persisted schema version emitted by this codec."""
        return _TURN_RECORD_SCHEMA_VERSION

    @staticmethod
    def _to_ledger_record(record: TurnRecord) -> dict[str, object]:  # noqa: C901, PLR0912
        """Serialize one exact record for the versioned handled-turn ledger."""
        payload: dict[str, object] = {
            "anchor_event_id": record.anchor_event_id,
            "source_event_ids": list(record.source_event_ids),
            "redacted_source_event_ids": list(record.redacted_source_event_ids),
            "pending_redaction_cleanup_event_ids": list(record.pending_redaction_cleanup_event_ids),
            "response_event_id": record.response_event_id,
            "completed": record.completed,
            "timestamp": record.timestamp,
        }
        if record.discovery_event_ids:
            payload["discovery_event_ids"] = list(record.discovery_event_ids)
        if record.visible_echo_event_id is not None:
            payload["visible_echo_event_id"] = record.visible_echo_event_id
        if record.visible_echo_is_fallback is not None:
            payload["visible_echo_is_fallback"] = record.visible_echo_is_fallback
        if record.source_event_prompts is not None:
            payload["source_event_prompts"] = dict(record.source_event_prompts)
        if record.source_event_revisions is not None:
            payload["source_event_revisions"] = {
                event_id: list(revision) for event_id, revision in record.source_event_revisions.items()
            }
        if record.suppressed_source_event_revisions is not None:
            payload["suppressed_source_event_revisions"] = {
                event_id: list(revision) for event_id, revision in record.suppressed_source_event_revisions.items()
            }
        if record.latest_edit_receipt_order is not None:
            payload["latest_edit_receipt_order"] = record.latest_edit_receipt_order
        if record.user_stop_receipt_order is not None:
            payload["user_stop_receipt_order"] = record.user_stop_receipt_order
        if record.user_stop_settled_receipt_order is not None:
            payload["user_stop_settled_receipt_order"] = record.user_stop_settled_receipt_order
        if record.source_event_metadata is not None:
            payload["source_event_metadata"] = {
                event_id: metadata._to_record() for event_id, metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            payload["response_owner"] = record.response_owner
        if record.requester_id is not None:
            payload["requester_id"] = record.requester_id
        if record.correlation_id is not None:
            payload["correlation_id"] = record.correlation_id
        if record.command_execution_started:
            payload["command_execution_started"] = True
        if record.command_result_text is not None:
            payload["command_result_text"] = record.command_result_text
        if record.history_scope is not None:
            payload["history_scope"] = record.history_scope.to_metadata()
        if record.conversation_target is not None:
            payload["conversation_target"] = record.conversation_target.to_metadata()
        return payload

    @staticmethod
    def _from_ledger_record(event_id: str, raw_record: object) -> TurnRecord | None:
        """Parse one record from the current ledger schema without legacy migration.

        Every field is read by name, so a key an older writer emitted and this
        one no longer knows about is dropped rather than rejected. That is what
        lets an optional field be retired without a schema version bump, which
        would quarantine every existing ledger file and discard the live turn
        identity in it over a field nothing reads.
        """
        if not isinstance(raw_record, Mapping):
            return None
        record = typing.cast("Mapping[str, object]", raw_record)
        raw_source_event_ids = record.get("source_event_ids")
        raw_discovery_event_ids = record.get("discovery_event_ids", [])
        raw_redacted_source_event_ids = record.get("redacted_source_event_ids", [])
        raw_pending_redaction_cleanup_event_ids = record.get("pending_redaction_cleanup_event_ids", [])
        anchor_event_id = record.get("anchor_event_id")
        completed = record.get("completed")
        timestamp = record.get("timestamp")
        response_event_id = record.get("response_event_id")
        if (
            not isinstance(raw_source_event_ids, list)
            or not isinstance(raw_discovery_event_ids, list)
            or not isinstance(raw_redacted_source_event_ids, list)
            or not isinstance(raw_pending_redaction_cleanup_event_ids, list)
            or not isinstance(anchor_event_id, str)
            or not anchor_event_id
            or not isinstance(completed, bool)
            or not isinstance(timestamp, int | float)
            or isinstance(timestamp, bool)
            or (response_event_id is not None and not isinstance(response_event_id, str))
        ):
            return None
        source_event_ids = canonical_source_event_ids(raw_source_event_ids)
        if not source_event_ids:
            return None
        turn_record = TurnRecord.create(
            source_event_ids,
            discovery_event_ids=canonical_source_event_ids(raw_discovery_event_ids),
            redacted_source_event_ids=canonical_source_event_ids(raw_redacted_source_event_ids),
            pending_redaction_cleanup_event_ids=canonical_source_event_ids(
                raw_pending_redaction_cleanup_event_ids,
            ),
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=completed,
            visible_echo_event_id=canonical_optional_string(record.get("visible_echo_event_id")),
            visible_echo_is_fallback=_bool_or_none(record.get("visible_echo_is_fallback")),
            source_event_prompts=_mapping_or_none(record.get("source_event_prompts")),
            source_event_revisions=_mapping_or_none(record.get("source_event_revisions")),
            suppressed_source_event_revisions=_mapping_or_none(
                record.get("suppressed_source_event_revisions"),
            ),
            latest_edit_receipt_order=_positive_int_or_none(record.get("latest_edit_receipt_order")),
            user_stop_receipt_order=_positive_int_or_none(record.get("user_stop_receipt_order")),
            user_stop_settled_receipt_order=_positive_int_or_none(
                record.get("user_stop_settled_receipt_order"),
            ),
            source_event_metadata=_mapping_or_none(record.get("source_event_metadata")),
            response_owner=canonical_optional_string(record.get("response_owner")),
            requester_id=canonical_optional_string(record.get("requester_id")),
            correlation_id=canonical_optional_string(record.get("correlation_id")),
            command_execution_started=record.get("command_execution_started") is True,
            command_result_text=canonical_optional_string(record.get("command_result_text")),
            history_scope=HistoryScope.from_metadata(record.get("history_scope")),
            conversation_target=MessageTarget.from_metadata(record.get("conversation_target")),
            timestamp=float(timestamp),
        )
        if event_id not in turn_record.indexed_event_ids:
            return None
        return turn_record

    @staticmethod
    def to_run_metadata(record: TurnRecord) -> dict[str, object]:  # noqa: C901
        """Project one record into the recoverable subset stored with an Agno run."""
        if not record.source_event_ids:
            return {}
        metadata: dict[str, object] = {
            constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(record.source_event_ids),
        }
        if record.discovery_event_ids:
            metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] = list(record.discovery_event_ids)
        if record.redacted_source_event_ids:
            metadata[constants.MATRIX_TURN_REDACTED_SOURCE_EVENT_IDS_METADATA_KEY] = list(
                record.redacted_source_event_ids,
            )
        if record.source_event_prompts is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(record.source_event_prompts)
        if record.source_event_revisions is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY] = {
                event_id: list(revision) for event_id, revision in record.source_event_revisions.items()
            }
        if record.source_event_metadata is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_METADATA_KEY] = {
                event_id: source_metadata._to_record()
                for event_id, source_metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            metadata[constants.MATRIX_RESPONSE_OWNER_METADATA_KEY] = record.response_owner
        if record.requester_id is not None:
            metadata["requester_id"] = record.requester_id
        if record.history_scope is not None:
            metadata[constants.MATRIX_HISTORY_SCOPE_METADATA_KEY] = record.history_scope.to_metadata()
        if record.conversation_target is not None:
            metadata[constants.MATRIX_CONVERSATION_TARGET_METADATA_KEY] = record.conversation_target.to_metadata()
        return metadata

    @staticmethod
    def from_run_metadata(metadata: Mapping[str, object]) -> TurnRecord | None:
        """Parse current Agno metadata, using response linkage as terminal-delivery evidence."""
        if metadata.get(constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY) != TurnRecordCodec.schema_version():
            return None
        anchor_event_id = metadata.get(constants.MATRIX_EVENT_ID_METADATA_KEY)
        if not isinstance(anchor_event_id, str) or not anchor_event_id:
            return None
        raw_source_event_ids = metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
        raw_discovery_event_ids = metadata.get(constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY)
        raw_redacted_source_event_ids = metadata.get(
            constants.MATRIX_TURN_REDACTED_SOURCE_EVENT_IDS_METADATA_KEY,
        )
        source_event_ids = (
            canonical_source_event_ids(raw_source_event_ids)
            if isinstance(raw_source_event_ids, list)
            else (anchor_event_id,)
        ) or (anchor_event_id,)
        response_event_id = canonical_optional_string(metadata.get(constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY))
        return TurnRecord.create(
            source_event_ids,
            discovery_event_ids=(
                canonical_source_event_ids(raw_discovery_event_ids) if isinstance(raw_discovery_event_ids, list) else ()
            ),
            redacted_source_event_ids=(
                canonical_source_event_ids(raw_redacted_source_event_ids)
                if isinstance(raw_redacted_source_event_ids, list)
                else ()
            ),
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=response_event_id is not None,
            source_event_prompts=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)),
            source_event_revisions=_mapping_or_none(
                metadata.get(constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY),
            ),
            source_event_metadata=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_METADATA_KEY)),
            response_owner=canonical_optional_string(metadata.get(constants.MATRIX_RESPONSE_OWNER_METADATA_KEY)),
            requester_id=canonical_optional_string(metadata.get("requester_id")),
            correlation_id=canonical_optional_string(metadata.get("correlation_id")),
            history_scope=HistoryScope.from_metadata(metadata.get(constants.MATRIX_HISTORY_SCOPE_METADATA_KEY)),
            conversation_target=MessageTarget.from_metadata(
                metadata.get(constants.MATRIX_CONVERSATION_TARGET_METADATA_KEY),
            ),
        )


@dataclass
class _LedgerState:
    """In-memory canonical records shared by every ledger for one agent."""

    responses: dict[str, TurnRecord] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # Held across a whole update: the in-memory mutation and the row it
    # implies. Without it two concurrent updates can reach the database in the
    # opposite order from memory, and the loser durably overwrites the winner
    # with the record it computed before being overtaken -- so memory says the
    # turn is finished, storage says it is not, and the restart answers the
    # message twice. The synchronous lock cannot cover this: it is not held
    # across an await, and holding a thread lock across one would deadlock.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    loaded: bool = False


_LEDGER_STATES: dict[str, _LedgerState] = {}
_LEDGER_RUNTIME_LOCK = threading.Lock()


def _shared_ledger_state(store_key: str, agent_name: str) -> _LedgerState:
    """Return the process-wide shared state for one agent's records in one store.

    Two ledgers for one agent in one database must observe each other's writes,
    or a turn answered through one could be answered again through the other.

    The database has to be part of the key as well as the agent. Keyed by agent
    alone, two ledgers over *different* databases alias: the second binds to
    state the first already marked loaded, skips its own read, and answers
    "handled" from rows its database has never held. One process normally owns
    one database, but tests routinely open several, which is exactly where that
    aliasing turns into a green run that proves nothing.
    """
    key = f"{store_key}\x00{agent_name}"
    with _LEDGER_RUNTIME_LOCK:
        state = _LEDGER_STATES.get(key)
        if state is None:
            state = _LedgerState()
            _LEDGER_STATES[key] = state
        return state


def _reset_handled_turn_ledger_runtime() -> None:
    """Drop shared ledger state (tests and forked runtimes).

    Nothing has to be flushed first any more. Every write is awaited before
    its caller continues, so there is no queue that could still owe the
    database a record when this runs.
    """
    with _LEDGER_RUNTIME_LOCK:
        _LEDGER_STATES.clear()


@dataclass
class HandledTurnLedger:
    """Store exact canonical records without reassigning completed source identities."""

    agent_name: str
    records: TurnRecordStore
    # Where this agent's records were kept before they moved into the journal
    # database. Present so an installation that has been running can be
    # upgraded; see ``_import_legacy_ledger``. ``None`` means there is no
    # history to inherit, which is true for a fresh install and for tests that
    # start from an empty database.
    legacy_responses_file: Path | None = None
    _state: _LedgerState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind shared in-memory state for this agent without reading the database."""
        self._state = _shared_ledger_state(self.records.state_key, self.agent_name)

    @property
    def _responses(self) -> dict[str, TurnRecord]:
        return self._state.responses

    @_responses.setter
    def _responses(self, responses: dict[str, TurnRecord]) -> None:
        self._state.responses = responses

    async def load(self) -> None:
        """Read every stored record into memory, once per process.

        Every read on this class is synchronous and answers from the map this
        fills, so it has to run before anything asks a question. Lazily loading
        from inside a read is what the old file-backed ledger did, and it is
        not available here: a synchronous method cannot await a database, and
        bridging it to one deadlocks against the same executor the database
        offloads onto.

        The whole sequence runs under the write lock, not just the install.
        Two sibling ledgers for one agent -- which a hot replacement creates
        routinely -- would otherwise both pass the unloaded check, both read
        the same legacy file, and both try to rename it; the loser calls
        ``Path.replace`` on a path that no longer exists and dies with
        ``FileNotFoundError`` during startup, having done nothing wrong. The
        lock also means the second caller finds the state already loaded and
        returns without touching the database at all.
        """
        with self._state.lock:
            if self._state.loaded:
                return
        async with self._state.write_lock:
            with self._state.lock:
                if self._state.loaded:
                    return
            await self._load_locked()

    async def _load_locked(self) -> None:
        """Read storage into the shared map, with the write lock already held."""
        stored = await self.records.load_all()
        if imported := await self._import_legacy_ledger({index_event_id for index_event_id, _, _ in stored}):
            stored = imported
        with self._state.lock:
            self._responses = {
                index_event_id: record
                for index_event_id, _anchor_event_id, record_json in stored
                if (record := TurnRecordCodec._from_ledger_record(index_event_id, json.loads(record_json))) is not None
            }
            self._state.loaded = True

    async def cleanup(self, *, unsettled_source_event_ids: Collection[str] = ()) -> None:
        """Compact terminal history while retaining truth still owned by dispatch."""
        await self._cleanup_old_events(unsettled_source_event_ids=unsettled_source_event_ids)

    async def record_handled_turn(self, turn_record: TurnRecord) -> None:
        """Persist one exact record for every source event in the turn."""
        await self.update_handled_turn(
            turn_record.indexed_event_ids,
            lambda _existing_records: turn_record,
        )

    async def update_handled_turn(
        self,
        lookup_event_ids: Sequence[str],
        update: Callable[[Mapping[str, TurnRecord]], TurnRecord],
    ) -> TurnRecord | None:
        """Atomically update one record and store it before returning.

        The whole update runs under the write lock, so the order updates reach
        the database is the order they reached memory. Only the synchronous
        state lock is dropped before the write, because a reader must never
        block on one.

        There is no longer a choice about durability. The old ledger returned
        as soon as the record reached memory and persisted behind the caller,
        so a crash could lose a record that something had already acted on, and
        callers who could not tolerate that passed ``wait_for_persist=True``.
        Awaiting the write is that flag for everyone, at the cost the flag
        always had.
        """
        normalized_lookup_event_ids = canonical_source_event_ids(lookup_event_ids)
        if not normalized_lookup_event_ids:
            return None
        async with self._state.write_lock:
            with self._state.lock:
                self._require_loaded()
                existing_records = MappingProxyType(
                    {
                        event_id: record
                        for event_id in normalized_lookup_event_ids
                        if (record := self._responses.get(event_id)) is not None
                    },
                )
                updated_record = update(existing_records)
                candidate_record = canonicalize_turn_record(
                    updated_record,
                    timestamp=(updated_record.timestamp if updated_record.timestamp != 0.0 else time.time()),
                )
                if not candidate_record.source_event_ids:
                    return None
                persisted_record = _resolve_turn_record(candidate_record, self._responses)
                if persisted_record is None:
                    return None
                superseded = {
                    event_id: self._responses.get(event_id) for event_id in persisted_record.indexed_event_ids
                }
                for event_id in persisted_record.indexed_event_ids:
                    self._responses[event_id] = persisted_record
            # Canonicalization derives an anchor from the sources whenever one was
            # not supplied, and a record with no sources was already rejected above.
            assert persisted_record.anchor_event_id is not None
            # Shielded, so cancellation cannot leave the outcome unknown.
            #
            # Publishing before the commit is deliberate: it stops a
            # synchronous reader seeing "not handled" while this turn's write
            # is in flight and answering the same message twice. Undoing that
            # publication is therefore only correct when the write definitely
            # did not land.
            #
            # Cancelling a bare `await` gives no such certainty. The backend
            # runs the statement on an `asyncio.to_thread` worker that a
            # cancelled await cannot stop, so the transaction commits anyway
            # while the rollback removes the record from memory -- leaving the
            # live process answering a message it has already answered, and a
            # restart disagreeing with it. Shielding lets the write settle and
            # report, after which the cancellation propagates as it should.
            #
            # The task, not the shield, is what is kept. A shield reports its
            # own cancellation and nothing after it, so asking the shield how
            # the write ended answers "cancelled" for a write that is still
            # running -- and asking a cancelled future for its exception raises
            # instead of answering, which skipped the rollback below entirely.
            #
            # The backends draining their own worker threads does not replace
            # this, which is the tempting simplification. They answer a
            # different question: that nothing else may touch a connection
            # while a statement is on it. This one needs to know *how* its own
            # write ended, and a cancelled caller of the backend is told only
            # that it was cancelled -- correctly, because swallowing the
            # cancellation would be worse. Awaiting the upsert bare is weaker
            # still: it cancels the coroutine outright, so the write never
            # reaches a backend that could have drained it.
            write = asyncio.ensure_future(
                self.records.upsert(
                    index_event_ids=persisted_record.indexed_event_ids,
                    anchor_event_id=persisted_record.anchor_event_id,
                    record_json=json.dumps(TurnRecordCodec._to_ledger_record(persisted_record)),
                ),
            )
            try:
                await asyncio.shield(write)
            except BaseException:
                # A cancelled caller has not learned the write's fate yet: the
                # shield only detached the wait, so the write is still running
                # and has to be waited for before memory can be judged wrong.
                # Every further cancellation re-attaches rather than escaping,
                # because a caller cancelled twice would otherwise decide the
                # record's fate while the transaction is still open.
                while not write.done():
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(write)
                # Only a write that reported failure did definitely not land. A
                # cancelled write did not report anything: the backend hands the
                # statement to a writer that outlives the await, so unpublishing
                # it risks re-answering a message the database already records as
                # answered -- the very outcome publishing early exists to prevent.
                if write.cancelled() or write.exception() is None:
                    raise
                self._restore_superseded(persisted_record, superseded)
                raise
        logger.debug("handled_turn_recorded", indexed_event_count=len(persisted_record.indexed_event_ids))
        return persisted_record

    def has_responded(self, event_id: str) -> bool:
        """Return whether the source event has a terminal recorded outcome."""
        with self._state.lock:
            self._require_loaded()
            return self._has_responded_locked(event_id)

    async def _import_legacy_ledger(self, already_stored: set[str]) -> tuple[tuple[str, str, str], ...]:
        """Adopt an agent's pre-database records, once, and return them as stored rows.

        Skipping this is not a missing nicety, it is the worst failure this
        module has. An installation that has been answering messages holds all
        of its terminal truth in a JSON file; a version that reads only the new
        table sees an empty ledger, concludes nothing has ever been answered,
        and re-answers the entire backlog the first time it replays.

        The presence of the file is the only trigger, and its rename is the
        only marker. Gating on an empty table instead would look safer and be
        worse: an import that crashed partway leaves rows behind, so the gate
        would never fire again and every turn it had not yet reached would stay
        missing for good -- which for those turns is identical to never having
        imported at all.

        What keeps that safe is `adopt_missing`, which fills only the indexes
        with no record yet. A record already here was written by this runtime or
        by an earlier pass, so it is at least as current as the file's copy and
        must not be overwritten.

        Ordinary `upsert` cannot express that. A legacy record can overlap a
        stored one only *partially* -- it indexes two sources of one coalesced
        turn, the runtime has a newer record under the first, the second is
        absent -- and upserting the whole record overwrites the newer one, after
        which the file is renamed and that copy is gone. Filtering such records
        out instead leaves the absent source with no record, so a message that
        was answered can be answered again. Filling the gaps and leaving every
        occupied index alone is the only option that loses neither.

        The rename is atomic, so it either happens or does not, and a renamed
        file is never read again. That is also what stops a later compaction
        from resurrecting history it deliberately dropped: by then there is no
        file left to re-import.

        The same codec reads the file and writes the rows, so the round trip is
        lossless by construction. A field the current codec has retired is
        dropped exactly as it would be on any other load.
        """
        legacy_file = self.legacy_responses_file
        if legacy_file is None or not legacy_file.exists():
            return ()
        raw = json.loads(legacy_file.read_text())
        records = raw.get(_LEDGER_RECORDS_KEY) if isinstance(raw, Mapping) else None
        decoded = (
            {
                event_id: record
                for event_id, raw_record in records.items()
                if (record := TurnRecordCodec._from_ledger_record(event_id, raw_record)) is not None
            }
            if isinstance(records, Mapping)
            else {}
        )
        # One row per distinct turn, not per index: `upsert` already stores a
        # record under every event that indexes it, and writing it once per
        # index would re-delete and re-insert the same siblings repeatedly.
        unseen = {
            record.indexed_event_ids: record
            for record in decoded.values()
            if not already_stored.issuperset(record.indexed_event_ids)
        }
        adopted = 0
        for record in unseen.values():
            # Canonicalizing derives an anchor for a record written before one
            # was always stored, which is exactly the vintage this import
            # exists to read. Dropping such a record instead would lose the
            # proof that its message was answered.
            imported = canonicalize_turn_record(record)
            assert imported.anchor_event_id is not None
            adopted += await self.records.adopt_missing(
                index_event_ids=imported.indexed_event_ids,
                anchor_event_id=imported.anchor_event_id,
                record_json=json.dumps(TurnRecordCodec._to_ledger_record(imported)),
            )
        legacy_file.replace(legacy_file.with_suffix(f"{legacy_file.suffix}.imported"))
        logger.info(
            "handled_turn_ledger_imported",
            agent=self.agent_name,
            imported_event_count=adopted,
        )
        return await self.records.load_all()

    def _restore_superseded(
        self,
        published: TurnRecord,
        superseded: Mapping[str, TurnRecord | None],
    ) -> None:
        """Undo one failed write's publication, leaving any later one alone.

        A newer record for the same event is not rolled back. The write lock
        makes that unreachable today, but restoring an older record over a
        newer one is the kind of mistake that only shows up as a turn answered
        twice, so the check is cheap insurance rather than dead code.
        """
        with self._state.lock:
            for event_id, previous in superseded.items():
                if self._responses.get(event_id) is not published:
                    continue
                if previous is None:
                    self._responses.pop(event_id, None)
                else:
                    self._responses[event_id] = previous

    def _require_loaded(self) -> None:
        """Fail loudly if a reader arrives before the records are in memory.

        Answering "no record" from an unloaded map is the worst possible
        wrong answer: it reads as "this turn was never handled", and the bot
        answers a message it has already answered. Better to refuse than to
        guess, and the refusal is a startup-ordering bug the caller can fix.
        """
        if not self._state.loaded:
            msg = f"Turn records for {self.agent_name!r} were read before they were loaded"
            raise RuntimeError(msg)

    def _has_responded_locked(self, event_id: str) -> bool:
        record = self._responses.get(event_id)
        if record is None:
            return False
        return record.completed or event_id in record.redacted_source_event_ids

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo event ID for one source event."""
        with self._state.lock:
            self._require_loaded()
            record = self._responses.get(source_event_id)
            return record.visible_echo_event_id if record is not None else None

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the canonical record for one source event."""
        with self._state.lock:
            self._require_loaded()
            return self._responses.get(source_event_id)

    def pending_redaction_cleanup_event_ids(self) -> tuple[str, ...]:
        """Return every durable redaction cleanup intent still awaiting completion."""
        with self._state.lock:
            self._require_loaded()
            return canonical_source_event_ids(
                tuple(
                    event_id
                    for record in self._responses.values()
                    for event_id in record.pending_redaction_cleanup_event_ids
                ),
            )

    def turn_records_for_conversation(
        self,
        *,
        session_id: str,
    ) -> tuple[TurnRecord, ...]:
        """Return unique records that can identify persisted scopes for one conversation."""
        with self._state.lock:
            self._require_loaded()
            unique_records: dict[tuple[str, ...], TurnRecord] = {}
            for record in self._responses.values():
                target = record.conversation_target
                if target is None or target.session_id != session_id:
                    continue
                unique_records[record.indexed_event_ids] = record
            return tuple(unique_records.values())

    def turn_record_for_response_event_id(self, response_event_id: str) -> TurnRecord | None:
        """Return the sole turn whose visible response has this Matrix event ID."""
        with self._state.lock:
            self._require_loaded()
            matches = {
                record.indexed_event_ids: record
                for record in self._responses.values()
                if response_event_id in {record.response_event_id, record.visible_echo_event_id}
            }
        if len(matches) > 1:
            msg = f"Multiple turns own visible response {response_event_id!r}"
            raise RuntimeError(msg)
        return next(iter(matches.values()), None)

    async def _cleanup_old_events(
        self,
        max_events: int = 10000,
        max_age_days: int = 30,
        *,
        unsettled_source_event_ids: Collection[str] = (),
    ) -> None:
        """Drop stale records by age and count, in memory and in the database.

        The retained set is computed from what is already in memory rather than
        re-read first, because memory is now the authority a reader answers
        from and the database agrees with it after every awaited write.

        Only the dropped ids are deleted, rather than rewriting the whole set.
        The old ledger had to rewrite its file wholesale because that was the
        only way to remove an entry from it; a delete by key costs nothing and
        cannot lose the records it is not about.

        The delete commits *before* memory forgets, which is the opposite
        ordering to a write and for the same reason. Forgetting first would let
        a synchronous reader see "not handled" for a row the database still
        holds, and answer it again -- and if the delete then failed, that split
        would last until a restart put the row back. Deleting first can only
        leave memory holding a record the database no longer has, which
        suppresses one duplicate rather than causing one, and is corrected by
        the next load.
        """
        async with self._state.write_lock:
            with self._state.lock:
                self._require_loaded()
                retained = _cleaned_responses(
                    dict(self._responses),
                    max_events=max_events,
                    max_age_days=max_age_days,
                    unsettled_source_event_ids=unsettled_source_event_ids,
                )
                dropped = tuple(sorted(set(self._responses) - set(retained)))
            if dropped:
                await self.records.forget(index_event_ids=dropped)
            with self._state.lock:
                self._responses = retained
        logger.info(
            "handled_turn_cleanup_completed",
            agent=self.agent_name,
            kept_event_count=len(self._responses),
            dropped_event_count=len(dropped),
        )


def _resolve_turn_record(
    turn_record: TurnRecord,
    existing_records: Mapping[str, TurnRecord],
) -> TurnRecord | None:
    """Resolve one candidate against completed identities and newer same-turn rows."""
    conflicting_source_event_ids = tuple(
        event_id
        for event_id in turn_record.source_event_ids
        if (existing_record := existing_records.get(event_id)) is not None
        and existing_record.completed
        and not same_turn_identity(existing_record, turn_record)
    )
    if conflicting_source_event_ids:
        projected_turn_record = _project_redaction_alias(turn_record, conflicting_source_event_ids)
        if projected_turn_record is None:
            return None
        turn_record = projected_turn_record
    same_identity_records = (
        existing_record
        for event_id in turn_record.indexed_event_ids
        if (existing_record := existing_records.get(event_id)) is not None
        and same_turn_identity(existing_record, turn_record)
    )
    highest_precedence_existing_record = max(
        same_identity_records,
        key=lambda record: (record.completed, record.timestamp),
        default=None,
    )
    resolved_record = (
        _merge_same_identity_records(turn_record, highest_precedence_existing_record)
        if highest_precedence_existing_record is not None
        else turn_record
    )
    discovery_event_ids = tuple(
        event_id
        for event_id in resolved_record.discovery_event_ids
        if (existing_record := existing_records.get(event_id)) is None
        or not existing_record.completed
        or same_turn_identity(existing_record, resolved_record)
    )
    return canonicalize_turn_record(resolved_record, discovery_event_ids=discovery_event_ids)


def _project_redaction_alias(
    turn_record: TurnRecord,
    conflicting_source_event_ids: tuple[str, ...],
) -> TurnRecord | None:
    """Detach redaction markers from source aliases now owned by another completed turn."""
    if not turn_record.redacted_source_event_ids:
        return None
    conflicting_ids = set(conflicting_source_event_ids)
    retained_source_event_ids = tuple(
        event_id for event_id in turn_record.source_event_ids if event_id not in conflicting_ids
    )
    if not retained_source_event_ids:
        retained_source_event_ids = tuple(
            event_id for event_id in turn_record.redacted_source_event_ids if event_id not in conflicting_ids
        )
    if not retained_source_event_ids:
        return None
    anchor_event_id = (
        turn_record.anchor_event_id
        if turn_record.anchor_event_id in retained_source_event_ids
        else retained_source_event_ids[-1]
    )
    return canonicalize_turn_record(
        turn_record,
        source_event_ids=retained_source_event_ids,
        anchor_event_id=anchor_event_id,
        source_event_metadata=(
            {}
            if turn_record.is_coalesced and turn_record.source_event_metadata is None
            else turn_record.source_event_metadata
        ),
        # Turn-level requester context remains required for owed redaction cleanup; an explicit
        # empty source map keeps per-source replay ownership fail-closed after projection.
        requester_id=turn_record.requester_id,
    )


def _merge_same_identity_records(candidate: TurnRecord, existing: TurnRecord) -> TurnRecord:
    """Keep the newer same-turn record while preserving older echo and discovery facts."""
    if candidate.completed != existing.completed:
        newer, older = (candidate, existing) if candidate.completed else (existing, candidate)
    else:
        newer, older = (candidate, existing) if candidate.timestamp > existing.timestamp else (existing, candidate)
    return canonicalize_turn_record(
        newer,
        discovery_event_ids=(*newer.discovery_event_ids, *older.discovery_event_ids),
        redacted_source_event_ids=(
            *newer.redacted_source_event_ids,
            *older.redacted_source_event_ids,
        ),
        visible_echo_event_id=newer.visible_echo_event_id or older.visible_echo_event_id,
        visible_echo_is_fallback=(
            newer.visible_echo_is_fallback
            if newer.visible_echo_is_fallback is not None
            else older.visible_echo_is_fallback
        ),
        command_execution_started=newer.command_execution_started or older.command_execution_started,
        command_result_text=newer.command_result_text or older.command_result_text,
        latest_edit_receipt_order=max(
            newer.latest_edit_receipt_order or 0,
            older.latest_edit_receipt_order or 0,
        )
        or None,
        user_stop_receipt_order=max(
            newer.user_stop_receipt_order or 0,
            older.user_stop_receipt_order or 0,
        )
        or None,
        user_stop_settled_receipt_order=max(
            newer.user_stop_settled_receipt_order or 0,
            older.user_stop_settled_receipt_order or 0,
        )
        or None,
    )


def _bool_or_none(value: object) -> bool | None:
    """Return a strict boolean or None."""
    return value if isinstance(value, bool) else None


def _positive_int_or_none(value: object) -> int | None:
    """Return one positive non-boolean integer or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    """Return a typed mapping for codec input."""
    return typing.cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class _ResponseGroup:
    """Logical handled-turn group keyed by its complete indexed identity."""

    timestamp: float
    records: dict[str, TurnRecord]


def _response_group_requires_retention(
    group: _ResponseGroup,
    unsettled_source_event_ids: frozenset[str],
) -> bool:
    """Return whether one group still owns unfinished durable work."""
    return (
        not unsettled_source_event_ids.isdisjoint(group.records)
        or any(record.pending_redaction_cleanup_event_ids for record in group.records.values())
        or any(not record.completed and record.replay_source_event_ids for record in group.records.values())
        or any(
            record.user_stop_receipt_order is not None
            and (record.user_stop_settled_receipt_order or 0) < record.user_stop_receipt_order
            for record in group.records.values()
        )
    )


def _cleaned_responses(
    responses: dict[str, TurnRecord],
    *,
    max_events: int,
    max_age_days: int,
    unsettled_source_event_ids: Collection[str] = (),
) -> dict[str, TurnRecord]:
    """Remove stale turn groups while keeping coalesced groups intact."""
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    retained_source_event_ids = frozenset(unsettled_source_event_ids)
    fresh_groups = [
        group
        for group in _response_groups(responses)
        if _response_group_requires_retention(group, retained_source_event_ids)
        or current_time - group.timestamp < max_age_seconds
    ]
    if len(fresh_groups) > max_events:
        retained_groups = [
            group for group in fresh_groups if _response_group_requires_retention(group, retained_source_event_ids)
        ]
        retained_group_ids = {id(group) for group in retained_groups}
        ordinary_groups = [group for group in fresh_groups if id(group) not in retained_group_ids]
        kept_ordinary_groups = ordinary_groups[-max_events:] if max_events else []
        fresh_groups = sorted((*retained_groups, *kept_ordinary_groups), key=lambda group: group.timestamp)
    cleaned_responses: dict[str, TurnRecord] = {}
    for group in fresh_groups:
        cleaned_responses.update(group.records)
    return cleaned_responses


def _response_groups(responses: dict[str, TurnRecord]) -> list[_ResponseGroup]:
    """Return handled turns grouped by canonical sources and discovery aliases."""
    grouped_records: dict[tuple[str, ...], dict[str, TurnRecord]] = {}
    grouped_timestamps: dict[tuple[str, ...], float] = {}
    for event_id, record in responses.items():
        grouped_records.setdefault(record.indexed_event_ids, {})[event_id] = record
        grouped_timestamps[record.indexed_event_ids] = max(
            grouped_timestamps.get(record.indexed_event_ids, 0.0),
            record.timestamp,
        )
    return sorted(
        (
            _ResponseGroup(
                timestamp=grouped_timestamps[indexed_event_ids],
                records=records,
            )
            for indexed_event_ids, records in grouped_records.items()
        ),
        key=lambda group: group.timestamp,
    )
