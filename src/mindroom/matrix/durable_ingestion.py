"""Convert trusted nio batches and commit application effects before acknowledgement."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol

import nio
from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.codec import restore_event

import mindroom.event_journal as ej
from mindroom.event_journal.views import IngestionBatchAdmissionView
from mindroom.matrix.journal_ingress import ingestion_timeline_views
from mindroom.matrix.room_membership import invalidate_membership_lookups
from mindroom.matrix.transport_progress import is_transport_progress_source

__all__ = ["consume_one_ingestion_batch", "run_ingestion_pump", "validate_ingestion_batch"]


class _OwnedIngestionSession(Protocol):
    """Public durable session operations needed by the application pump."""

    async def next_batch(self) -> SyncBatch | None: ...
    async def ack(self, batch: SyncBatch) -> None: ...
    async def dispatch(self, record: SyncRecord, *, event: object | None = None) -> None: ...


type _BeforeAdmission = Callable[[ej.IngestionRecordAdmission, nio.TimelineEventProvenance | None], None]
type _AfterAdmission = Callable[
    [ej.IngestionRecordAdmission, ej.AdmissionFacts, nio.TimelineEventProvenance | None],
    Awaitable[None],
]
type _AuthenticateToDevice = Callable[[dict[str, object], object], object]


def _record_admission(
    record: SyncRecord,
    account_id: str,
    schedule_trigger_sender_is_managed: Callable[[str], bool],
) -> ej.IngestionRecordAdmission:
    admission = _record_disposition(record, account_id, schedule_trigger_sender_is_managed)
    membership = record.membership
    if membership is None:
        if record.kind is RecordKind.ROOM_LIFECYCLE:
            message = "Missing own-membership transition"
            raise ej.IngestionBatchValidationError(message)
        return admission
    return replace(
        admission,
        source=ej.DepartureSource(membership.source),
        room_id=record.room_id,
        previous_membership=membership.previous,
        membership=membership.current,
        previous_membership_epoch=membership.previous_epoch,
        membership_epoch=membership.current_epoch,
    )


def _record_disposition(
    record: SyncRecord,
    account_id: str,
    schedule_trigger_sender_is_managed: Callable[[str], bool],
) -> ej.IngestionRecordAdmission:
    if record.kind is RecordKind.ROOM_LIFECYCLE:
        return ej.IngestionRecordAdmission(ej.IngestionRecordDisposition.ROOM_LIFECYCLE)
    if record.kind is RecordKind.LOSS:
        return ej.IngestionRecordAdmission(ej.IngestionRecordDisposition.HISTORY_LOSS, room_id=record.room_id)
    member_snapshot = record.kind is RecordKind.STATE and record.source.get("type") == "m.room.member"
    if record.kind is RecordKind.TIMELINE or member_snapshot:
        source = record.clear if record.clear is not None else record.source
        if not is_transport_progress_source(source, self_sender=account_id):
            # State snapshots only seed baselines; they never authorize callbacks.
            provenance = nio.TimelineEventProvenance.HISTORY if member_snapshot else record.provenance
            if record.room_id is None or provenance is None:
                message = "Timeline observation lacks room or provenance"
                raise ej.IngestionBatchValidationError(message)
            views = ingestion_timeline_views(
                room_id=record.room_id,
                source=source,
                self_sender=account_id,
                provenance=provenance,
                schedule_trigger_sender_is_managed=schedule_trigger_sender_is_managed,
                security_metadata=(
                    {
                        "decrypted": True,
                        "verified": record.crypto.verified,
                        "sender_key": record.crypto.sender_key,
                        "session_id": record.crypto.session_id,
                    }
                    if record.clear is not None and record.crypto is not None
                    else None
                ),
            )
            if views is not None:
                event, projected = views
                return ej.IngestionRecordAdmission(
                    ej.IngestionRecordDisposition.SEMANTIC_EVENT,
                    event=event,
                    projected=projected,
                )
    return ej.IngestionRecordAdmission(ej.IngestionRecordDisposition.COMPATIBILITY_ONLY)


def validate_ingestion_batch(
    batch: SyncBatch,
    *,
    account_id: str,
    schedule_trigger_sender_is_managed: Callable[[str], bool] = lambda _sender: False,
) -> ej.IngestionBatchAdmission:
    """Convert the owned stream's typed observations into ordered application effects."""
    admission = ej.IngestionBatchAdmission(
        batch.stream_id,
        batch.sequence,
        tuple(_record_admission(record, account_id, schedule_trigger_sender_is_managed) for record in batch.records),
    )
    ej.validate_ingestion_batch_admission(admission)
    return admission


async def consume_one_ingestion_batch(
    session: _OwnedIngestionSession,
    admission: IngestionBatchAdmissionView,
    *,
    account_id: str,
    before_admission: _BeforeAdmission | None = None,
    after_admission: _AfterAdmission | None = None,
    after_sync: Callable[[], Awaitable[None]] | None = None,
    authenticate_to_device: _AuthenticateToDevice | None = None,
    on_decryption_failure: Callable[[SyncRecord], None] | None = None,
    schedule_trigger_sender_is_managed: Callable[[str], bool] = lambda _sender: False,
) -> ej.AdmissionFacts | None:
    """Commit the whole batch, run ordered hooks and callbacks, then acknowledge."""
    batch = await session.next_batch()
    if batch is None:
        return None
    converted = validate_ingestion_batch(
        batch,
        account_id=account_id,
        schedule_trigger_sender_is_managed=schedule_trigger_sender_is_managed,
    )
    invalidate_membership_lookups(batch.records, account_id=account_id)
    if before_admission is not None:
        for record, converted_record in zip(batch.records, converted.records, strict=True):
            before_admission(converted_record, record.provenance)
    result = await admission.admit_ingestion_batch(converted)
    for record, converted_record, facts in zip(batch.records, converted.records, result.record_facts, strict=True):
        if after_admission is not None:
            await after_admission(converted_record, facts, record.provenance)
        if (
            record.kind is RecordKind.TIMELINE
            and record.clear is None
            and record.source.get("type") == "m.room.encrypted"
        ):
            if on_decryption_failure is not None:
                on_decryption_failure(record)
            continue
        if converted_record.disposition is ej.IngestionRecordDisposition.COMPATIBILITY_ONLY:
            await _dispatch_auxiliary_record(session, record, account_id, authenticate_to_device)
    if batch.completes_sync and after_sync is not None:
        await after_sync()
    await session.ack(batch)
    return result


async def _dispatch_auxiliary_record(
    session: _OwnedIngestionSession,
    record: SyncRecord,
    account_id: str,
    authenticate_to_device: _AuthenticateToDevice | None,
) -> None:
    """Dispatch auxiliary callbacks at least once, filtering transport-only edits."""
    if record.kind is RecordKind.TIMELINE and is_transport_progress_source(
        record.clear if record.clear is not None else record.source,
        self_sender=account_id,
    ):
        return
    event = None
    if record.kind is RecordKind.TO_DEVICE and authenticate_to_device is not None:
        event = authenticate_to_device(record.source, restore_event(record))
    await session.dispatch(record, event=event)


async def run_ingestion_pump(
    session: _OwnedIngestionSession,
    admission: IngestionBatchAdmissionView,
    *,
    account_id: str,
    wait_for_work: Callable[[], Awaitable[None]],
    wake_semantic_dispatch: Callable[[], None],
    wait_for_delivery_projection: Callable[[], Awaitable[None]] | None = None,
    before_admission: _BeforeAdmission | None = None,
    after_admission: _AfterAdmission | None = None,
    after_sync: Callable[[], Awaitable[None]] | None = None,
    after_ack: Callable[[], None] | None = None,
    authenticate_to_device: _AuthenticateToDevice | None = None,
    on_decryption_failure: Callable[[SyncRecord], None] | None = None,
    schedule_trigger_sender_is_managed: Callable[[str], bool] = lambda _sender: False,
) -> None:
    """Drain batches, waiting on work or the existing delivery projection barrier."""
    while True:
        await asyncio.sleep(0)
        try:
            facts = await consume_one_ingestion_batch(
                session,
                admission,
                account_id=account_id,
                before_admission=before_admission,
                after_admission=after_admission,
                after_sync=after_sync,
                authenticate_to_device=authenticate_to_device,
                on_decryption_failure=on_decryption_failure,
                schedule_trigger_sender_is_managed=schedule_trigger_sender_is_managed,
            )
        except ej.DeliveryProjectionPendingError:
            if wait_for_delivery_projection is None:
                raise
            await wait_for_delivery_projection()
            continue
        if facts is None:
            await wait_for_work()
        else:
            # A retry may be acknowledging work committed before the prior
            # pump could notify its dispatcher. The journal decides what runs.
            wake_semantic_dispatch()
            if after_ack is not None:
                after_ack()
