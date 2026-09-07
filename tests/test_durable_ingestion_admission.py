"""Durable batch admission preserves atomicity, ordering and replay semantics."""
# ruff: noqa: D103, D102

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import nio
import pytest
from nio.crypto import DeviceStore, OlmDevice
from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.model import CryptoEvidence, OwnMembership

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.event_journal import (
    AdmissionFacts,
    DeliveryProjectionPendingError,
    EventJournalStore,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionBatchSequenceError,
    IngestionBatchValidationError,
    IngestionConsumerBindingError,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
    PrincipalStore,
    RoomMembershipPosition,
)
from mindroom.event_journal import store as journal_store
from mindroom.matrix import durable_ingestion
from mindroom.matrix.client_session import authenticate_to_device_event
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch, validate_ingestion_batch
from mindroom.matrix.journal_ingress import parse_journal_event
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.pending_event_worker import PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import JournalEvent
    from mindroom.event_journal.backend import Transaction

ACCOUNT = "@bot:example.org"
ROOM = "!room:example.org"


def message(event_id: str) -> SyncRecord:
    """Return one live actionable source."""
    return SyncRecord(
        RecordKind.TIMELINE,
        ROOM,
        {
            "type": "m.room.message",
            "sender": "@alice:example.org",
            "event_id": event_id,
            "origin_server_ts": 10,
            "content": {"msgtype": "m.text", "body": "hello"},
        },
        provenance=nio.TimelineEventProvenance.LIVE,
    )


class Session:
    """Keep committed input until acknowledgement, like the durable session."""

    def __init__(self, batch: SyncBatch) -> None:
        self.batch: SyncBatch | None = batch
        self.acked = []
        self.dispatched = []
        self.dispatched_events: list[object] = []
        self.next_calls = []

    async def next_batch(self) -> SyncBatch | None:
        self.next_calls.append(1)
        return self.batch

    async def ack(self, batch: SyncBatch) -> None:
        self.acked.append(batch)
        self.batch = None

    async def dispatch(self, record: SyncRecord, *, event: object = None) -> None:
        self.dispatched_events.append(event)
        self.dispatched.append(record)


async def principal_for(store: EventJournalStore, stream: UUID) -> PrincipalStore:
    principal = store.principal(ACCOUNT)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
    return principal


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_acknowledged_retry_wakes_an_idle_semantic_worker(
    journal_database: Callable[[], EventJournalStore],
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    """Failure after commit must not strand work when replay acknowledges the batch."""
    batch = SyncBatch(uuid4(), 1, (message("$interrupted"),))
    principal = await principal_for(journal_database(), batch.stream_id)
    session = Session(batch)
    worker_idle = asyncio.Event()
    pump_idle = asyncio.Event()
    handled = asyncio.Event()

    async def handle(_event: JournalEvent) -> bool:
        handled.set()
        return True

    worker = PendingEventWorker(store=principal, handle=handle)
    collect = worker._collect_dispatchable

    async def collect_and_signal() -> tuple[dict[str, list[JournalEvent]], bool]:
        result = await collect()
        worker_idle.set()
        return result

    monkeypatch.setattr(worker, "_collect_dispatchable", collect_and_signal)

    async def interrupt_after_commit(
        _record: IngestionRecordAdmission,
        _facts: AdmissionFacts,
        _provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        message = "interrupted after commit"
        raise failure(message)

    async def wait_for_work() -> None:
        pump_idle.set()
        await asyncio.Event().wait()

    worker.start()
    retry: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(worker_idle.wait(), 1)
        with pytest.raises(failure, match="interrupted after commit"):
            await durable_ingestion.run_ingestion_pump(
                session,
                principal,
                account_id=ACCOUNT,
                wait_for_work=wait_for_work,
                wake_semantic_dispatch=worker.wake,
                after_admission=interrupt_after_commit,
            )
        assert await principal.is_pending("$interrupted")
        assert session.acked == []
        retry = asyncio.create_task(
            durable_ingestion.run_ingestion_pump(
                session,
                principal,
                account_id=ACCOUNT,
                wait_for_work=wait_for_work,
                wake_semantic_dispatch=worker.wake,
            ),
        )
        await asyncio.wait_for(pump_idle.wait(), 1)
        assert session.acked == [batch]
        await asyncio.wait_for(handled.wait(), 1)
    finally:
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        await worker.stop()


@pytest.mark.asyncio
async def test_batch_projection_failure_rolls_back_all_records_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    batch = SyncBatch(uuid4(), 1, (message("$first"), message("$second")))
    principal = await principal_for(store, batch.stream_id)
    original = journal_store._snapshot_interactive_source

    def block_second(transaction: Transaction, principal_id: str, event: InboundEvent) -> None:
        if event.event_id == "$second":
            message = "projection pending"
            raise DeliveryProjectionPendingError(message)
        original(transaction, principal_id, event)

    monkeypatch.setattr(journal_store, "_snapshot_interactive_source", block_second)
    session = Session(batch)
    with pytest.raises(DeliveryProjectionPendingError):
        await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT)
    assert not await principal.pending()
    assert session.acked == []
    monkeypatch.setattr(journal_store, "_snapshot_interactive_source", original)
    facts = await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT)
    assert facts.receipt_new
    assert [event.event_id for event in await principal.pending()] == ["$first", "$second"]
    assert session.acked == [batch]
    await store.close()


@pytest.mark.asyncio
async def test_redelivery_after_post_hook_failure_retries_hook_without_semantics(tmp_path: Path) -> None:
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    batch = SyncBatch(uuid4(), 1, (message("$first"), message("$second")))
    principal = await principal_for(store, batch.stream_id)
    session = Session(batch)
    seen = []
    fail = True

    async def after(
        record: IngestionRecordAdmission,
        facts: AdmissionFacts,
        provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        del provenance
        assert record.event is not None
        seen.append((record.event.event_id, facts.receipt_new))
        if fail:
            message = "post hook"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="post hook"):
        await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT, after_admission=after)
    assert session.acked == []
    fail = False
    facts = await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT, after_admission=after)
    assert not facts.receipt_new
    assert not facts.semantic_event_new
    assert seen == [("$first", True), ("$first", False), ("$second", False)]
    assert session.dispatched == []
    assert len(await principal.pending()) == 2
    await store.close()


@pytest.mark.asyncio
async def test_empty_completion_retries_until_after_sync_succeeds(tmp_path: Path) -> None:
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    batch = SyncBatch(uuid4(), 1, (), completes_sync=True)
    principal = await principal_for(store, batch.stream_id)
    session = Session(batch)
    attempts = []

    async def complete() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            message = "completion"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="completion"):
        await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT, after_sync=complete)
    assert session.acked == []
    await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT, after_sync=complete)
    assert len(attempts) == 2
    assert session.acked == [batch]
    await store.close()


@pytest.mark.parametrize(
    ("sender", "kind", "status", "replacement", "skipped"),
    [
        (ACCOUNT, "m.room.message", "pending", True, True),
        (ACCOUNT, "m.room.message", "streaming", True, True),
        (ACCOUNT, "m.room.message", "pending", False, False),
        (ACCOUNT, "m.room.message", "completed", True, False),
        (ACCOUNT, "m.room.message", "unknown", True, False),
        ("@other:example.org", "m.room.message", "streaming", True, False),
        (ACCOUNT, "m.room.redaction", "streaming", True, False),
        (ACCOUNT, "m.room.encrypted", "streaming", True, False),
    ],
)
def test_progress_filter_runs_before_classification(
    monkeypatch: pytest.MonkeyPatch,
    sender: str,
    kind: str,
    status: str,
    replacement: bool,
    skipped: bool,
) -> None:
    calls = []
    monkeypatch.setattr(durable_ingestion, "ingestion_timeline_views", lambda **kwargs: calls.append(kwargs))
    source = message("$progress").source
    content = {"msgtype": "m.text", "body": "...", STREAM_STATUS_KEY: status}
    if replacement:
        content = {"m.relates_to": {"rel_type": "m.replace", "event_id": "$original"}, "m.new_content": content}
    source.update(sender=sender, type=kind, content=content)
    batch = SyncBatch(uuid4(), 1, (replace(message("$progress"), source=source),))
    validate_ingestion_batch(batch, account_id=ACCOUNT)
    assert bool(calls) is not skipped


@pytest.mark.asyncio
async def test_batch_binding_order_and_semantic_duplicates(journal_database: Callable[[], EventJournalStore]) -> None:
    store = journal_database()
    batch = SyncBatch(uuid4(), 1, (message("$first"),))
    principal = await principal_for(store, batch.stream_id)
    with pytest.raises(IngestionConsumerBindingError):
        await consume_one_ingestion_batch(Session(replace(batch, stream_id=uuid4())), principal, account_id=ACCOUNT)
    with pytest.raises(IngestionBatchSequenceError):
        await consume_one_ingestion_batch(Session(replace(batch, sequence=2)), principal, account_id=ACCOUNT)
    await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    result = await consume_one_ingestion_batch(Session(replace(batch, sequence=2)), principal, account_id=ACCOUNT)
    assert result.receipt_new
    assert not result.semantic_event_new
    assert len(await principal.pending()) == 1
    with pytest.raises(IngestionBatchSequenceError):
        await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_duplicate_cannot_answer_a_question_published_after_source(
    journal_database: Callable[[], EventJournalStore],
    duplicate: bool,
) -> None:
    """Pending input keeps the interactive meaning it had when first admitted."""
    store = journal_database()
    stream = uuid4()
    principal = store.principal("agent@@bot:example.org")
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
    turn = validate_ingestion_batch(SyncBatch(stream, 1, (message("$turn"),)), account_id=ACCOUNT)
    await principal.admit_ingestion_batch(turn)
    answer = message("$answer")
    answer.source["content"] = {
        "msgtype": "m.text",
        "body": "1",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
    }
    source = validate_ingestion_batch(SyncBatch(stream, 2, (answer,)), account_id=ACCOUNT)
    await principal.admit_ingestion_batch(source)
    assert await principal.claim_interactive_text(source_event_id="$answer") is None
    question = message("$question")
    question.source.update(
        sender=ACCOUNT,
        content={
            "msgtype": "m.text",
            "body": "Choose",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            "io.mindroom.interactive": {
                "creator_agent": "agent",
                "question_text": "Choose",
                "options": {"1": "one"},
                "option_labels": {"1": "One"},
                "source_event_id": "$turn",
            },
        },
    )
    await principal.admit_ingestion_batch(
        validate_ingestion_batch(SyncBatch(stream, 3, (question,)), account_id=ACCOUNT),
    )
    await principal.settle("$question")
    if duplicate:
        facts = await principal.admit_ingestion_batch(replace(source, sequence=4))
        assert not facts.semantic_event_new
    assert await principal.is_pending("$answer")
    assert await principal.claim_interactive_text(source_event_id="$answer") is None


@pytest.mark.asyncio
async def test_batch_acceptance_survives_reopen_and_is_scoped_to_consumer(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Empty sync completions retain only the current replay boundary."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    other = store.principal("@other:example.org")
    other_stream = uuid4()
    consumer = await other.load_or_create_ingestion_consumer(new_generation=uuid4())
    await other.bind_ingestion_stream(generation=consumer.generation, stream_id=other_stream)
    other_batch = IngestionBatchAdmission(other_stream, 1, ())
    await other.admit_ingestion_batch(other_batch)

    for sequence in range(1, 4):
        batch = SyncBatch(stream, sequence, (), completes_sync=True)
        result = await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
        assert result is not None
        assert result.receipt_new

    reopened = journal_database().principal(ACCOUNT)
    latest = IngestionBatchAdmission(stream, 3, ())
    assert not (await reopened.admit_ingestion_batch(latest)).receipt_new
    assert not (await other.admit_ingestion_batch(other_batch)).receipt_new
    with pytest.raises(IngestionBatchSequenceError):
        await reopened.admit_ingestion_batch(IngestionBatchAdmission(stream, 1, ()))


@pytest.mark.asyncio
async def test_concurrent_batch_replay_has_one_admission(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Separate connections cannot both acquire the same batch or apply its effects."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    other_connection = journal_database().principal(ACCOUNT)
    batch = validate_ingestion_batch(SyncBatch(stream, 1, (message("$first"),)), account_id=ACCOUNT)
    facts = await asyncio.gather(*(owner.admit_ingestion_batch(batch) for owner in (principal, other_connection)))
    assert sorted(fact.receipt_new for fact in facts) == [False, True]
    assert sorted(fact.semantic_event_new for fact in facts) == [False, True]
    assert [event.event_id for event in await principal.pending()] == ["$first"]


@pytest.mark.asyncio
async def test_batch_acceptance_rolls_back_with_admission(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """A transaction failure must preserve the previous replay boundary and event state."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    first = validate_ingestion_batch(SyncBatch(stream, 1, (message("$first"),)), account_id=ACCOUNT)
    second = validate_ingestion_batch(SyncBatch(stream, 2, (message("$second"),)), account_id=ACCOUNT)
    await principal.admit_ingestion_batch(first)

    def fail_after_admission(transaction: Transaction) -> None:
        journal_store._admit_ingestion_batch(transaction, ACCOUNT, second)
        message = "abort admission transaction"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="abort admission transaction"):
        await store.backend.write(fail_after_admission)
    assert [event.event_id for event in await principal.pending()] == ["$first"]
    assert not (await principal.admit_ingestion_batch(first)).receipt_new
    assert (await principal.admit_ingestion_batch(second)).receipt_new
    assert [event.event_id for event in await principal.pending()] == ["$first", "$second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_admission", [False, True], ids=["before-hooks", "direct-store"])
async def test_malformed_batch_has_no_pre_admission_or_journal_effects(
    journal_database: Callable[[], EventJournalStore],
    direct_admission: bool,
) -> None:
    """A malformed tail must be rejected before hooks or earlier record effects."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    valid = SyncBatch(stream, 1, (message("$first"),))
    converted = validate_ingestion_batch(valid, account_id=ACCOUNT)
    before_effects = []
    session = Session(replace(valid, records=(*valid.records, SyncRecord(RecordKind.LOSS, None, {}))))
    if direct_admission:
        operation = principal.admit_ingestion_batch(
            replace(
                converted,
                records=(*converted.records, IngestionRecordAdmission(IngestionRecordDisposition.HISTORY_LOSS)),
            ),
        )
    else:
        operation = consume_one_ingestion_batch(
            session,
            principal,
            account_id=ACCOUNT,
            before_admission=lambda record, provenance: before_effects.append((record, provenance)),
        )
    with pytest.raises(IngestionBatchValidationError):
        await operation
    assert before_effects == []
    assert session.acked == []
    assert not await principal.pending()
    assert (await principal.admit_ingestion_batch(converted)).receipt_new


@pytest.mark.parametrize("single_batch", [False, True])
@pytest.mark.asyncio
async def test_membership_tenure_fences_departed_work_and_retains_rejoin_epoch(
    journal_database: Callable[[], EventJournalStore],
    single_batch: bool,
) -> None:
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    records = (
        SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership(None, "join", 0, 0)),
        message("$before"),
        SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership("join", "leave", 0, 1)),
        message("$departed"),
        SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership("leave", "join", 1, 1)),
        message("$after"),
    )
    facts = []
    batches = (records,) if single_batch else tuple((record,) for record in records)
    for sequence, batch_records in enumerate(batches, 1):
        facts.extend(
            (
                await consume_one_ingestion_batch(
                    Session(SyncBatch(stream, sequence, batch_records)),
                    principal,
                    account_id=ACCOUNT,
                )
            ).record_facts,
        )
    assert facts[1].semantic_event_new
    assert not facts[3].semantic_event_new
    assert facts[5].semantic_event_new
    position = await principal.membership_position(ROOM)
    assert position.membership == "join"
    assert position.membership_epoch == 1
    assert [event.event_id for event in await principal.pending()] == ["$after"]


@pytest.mark.asyncio
async def test_live_grant_message_departure_hooks_keep_batch_order(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    grant = SyncRecord(
        RecordKind.TIMELINE,
        ROOM,
        {
            "type": "m.room.member",
            "state_key": "@alice:example.org",
            "sender": "@alice:example.org",
            "event_id": "$grant",
            "origin_server_ts": 1,
            "content": {"membership": "join"},
        },
        provenance=nio.TimelineEventProvenance.LIVE,
    )
    departure = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership("join", "leave", 0, 1))
    allowed = False
    observed = []

    def before(record: IngestionRecordAdmission, _provenance: nio.TimelineEventProvenance | None) -> None:
        nonlocal allowed
        if record.membership == "leave":
            allowed = False

    async def after(
        record: IngestionRecordAdmission,
        facts: AdmissionFacts,
        provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        nonlocal allowed
        del facts
        if (
            record.event is not None
            and record.event.event_id == "$grant"
            and provenance is nio.TimelineEventProvenance.LIVE
        ):
            allowed = True
        if record.event is not None and record.event.event_id == "$message":
            observed.append(allowed)

    for sequence, record in enumerate((grant, message("$message"), departure), 1):
        await consume_one_ingestion_batch(
            Session(SyncBatch(stream, sequence, (record,))),
            principal,
            account_id=ACCOUNT,
            before_admission=before,
            after_admission=after,
        )
    assert observed == [True]
    assert not allowed


@pytest.mark.asyncio
async def test_encrypted_semantic_source_keeps_crypto_evidence(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    plain = message("$encrypted")
    record = replace(
        plain,
        source={"type": "m.room.encrypted"},
        clear=plain.source,
        crypto=CryptoEvidence(True, "curve", "session"),
    )
    batch = SyncBatch(uuid4(), 1, (record,))
    principal = await principal_for(store, batch.stream_id)
    await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    stored = await principal.load_event("$encrypted")
    assert stored is not None
    event = parse_journal_event(stored)
    assert event.decrypted
    assert event.verified
    assert event.sender_key == "curve"
    assert event.session_id == "session"


@pytest.mark.asyncio
async def test_auxiliary_replay_reauthenticates_removed_device(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    devices = DeviceStore()
    device = OlmDevice("@alice:example.org", "ALICE", {"curve25519": "curve", "ed25519": "signing"})
    devices.add(device)
    record = SyncRecord(
        RecordKind.TO_DEVICE,
        None,
        {
            "type": "m.room.encrypted",
            "sender": device.user_id,
            "content": {"algorithm": "m.olm.v1.curve25519-aes-sha2", "sender_key": "curve"},
        },
        clear={"type": "org.example.call", "sender": device.user_id, "content": {"key": "value"}},
        route="to_device",
    )
    batch = SyncBatch(uuid4(), 1, (record,))
    principal = await principal_for(store, batch.stream_id)
    session = Session(batch)
    authenticate = partial(authenticate_to_device_event, device_store=devices)
    await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT, authenticate_to_device=authenticate)
    assert isinstance(session.dispatched_events[0], AuthenticatedToDeviceEvent)
    device.deleted = True
    replay = Session(batch)
    facts = await consume_one_ingestion_batch(
        replay,
        principal,
        account_id=ACCOUNT,
        authenticate_to_device=authenticate,
    )
    assert facts is not None
    assert not facts.receipt_new
    assert len(replay.dispatched_events) == 1
    assert not isinstance(replay.dispatched_events[0], AuthenticatedToDeviceEvent)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "invite", "ban"])
async def test_initial_nonjoined_producer_position_stays_fenced(
    journal_database: Callable[[], EventJournalStore],
    membership: str,
) -> None:
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    record = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership(None, membership, 0, 0))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (record,))), principal, account_id=ACCOUNT)
    position = await principal.membership_position(ROOM)
    assert (position.membership, position.membership_epoch) == ("leave", 0)
    record = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership(membership, "join", 0, 0))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (record,))), principal, account_id=ACCOUNT)
    position = await principal.membership_position(ROOM)
    assert (position.membership, position.membership_epoch) == ("join", 0)


@pytest.mark.asyncio
async def test_producer_membership_position_rolls_back_with_failed_admission(
    journal_database: Callable[[], EventJournalStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed batch cannot advance command position ahead of journal ownership."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    record = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership(None, "join", 0, 0))
    batch = SyncBatch(stream, 1, (record, message("$blocked")))
    snapshot = journal_store._snapshot_interactive_source

    def blocked(_transaction: Transaction, _principal_id: str, _event: InboundEvent) -> None:
        message = "pending projection"
        raise DeliveryProjectionPendingError(message)

    monkeypatch.setattr(journal_store, "_snapshot_interactive_source", blocked)
    with pytest.raises(DeliveryProjectionPendingError):
        await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    assert await principal.membership_position(ROOM) == RoomMembershipPosition("leave", 0)
    assert await principal.ingestion_membership_position(ROOM) is None
    monkeypatch.setattr(journal_store, "_snapshot_interactive_source", snapshot)
    await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    assert await principal.membership_position(ROOM) == RoomMembershipPosition("join", 0)
    assert await principal.ingestion_membership_position(ROOM) == RoomMembershipPosition("join", 0)
    assert [event.event_id for event in await principal.pending()] == ["$blocked"]


@pytest.mark.asyncio
async def test_membership_rejects_a_skipped_producer_epoch(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """A skipped producer epoch must not retire the current membership."""
    store = journal_database()
    stream = uuid4()
    principal = await principal_for(store, stream)
    record = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership(None, "join", 0, 0))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (record,))), principal, account_id=ACCOUNT)
    invalid = SyncRecord(RecordKind.ROOM_LIFECYCLE, ROOM, {}, membership=OwnMembership("join", "leave", 1, 2))
    with pytest.raises(IngestionBatchIntegrityError):
        await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (invalid,))), principal, account_id=ACCOUNT)
    assert await principal.membership_position(ROOM) == RoomMembershipPosition("join", 0)
    assert await principal.ingestion_membership_position(ROOM) == RoomMembershipPosition("join", 0)
