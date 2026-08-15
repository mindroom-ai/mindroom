"""Strict conversion of authenticated nio batches into local admissions."""

from collections.abc import Awaitable, Callable
from hashlib import sha256
from hmac import compare_digest
from json import JSONEncoder, loads
from typing import Protocol, cast
from uuid import UUID, uuid5

import nio.ingest as ingest  # noqa: PLR0402 - carriers are owned by nio.ingest

import mindroom.event_journal as ej
from mindroom.event_journal.views import IngestionBatchAdmissionView
from mindroom.matrix.journal_ingress import ingestion_timeline_views

__all__ = [
    "consume_one_ingestion_batch",
    "run_ingestion_pump",
    "validate_ingestion_batch",
]


class _OwnedIngestionSession(Protocol):
    """The private nio session surface MindRoom's durable adapter owns."""

    def next_batch(self, *, max_records: int) -> ingest.SyncBatch | None: ...

    async def _settle_batch(
        self,
        batch: ingest.SyncBatch,
        *,
        receipt_new: bool,
        semantic_event_new: bool,
    ) -> None: ...


_ENCODER = JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True)
_ROOM_KINDS = frozenset(
    {
        ingest.RecordKind.TIMELINE,
        ingest.RecordKind.STATE,
        ingest.RecordKind.EPHEMERAL,
        ingest.RecordKind.ROOM_ACCOUNT_DATA,
        ingest.RecordKind.ROOM_LIFECYCLE,
    },
)
_MEMBERSHIPS = frozenset({"ban", "invite", "join", "knock", "leave"})
_LIFECYCLE_FIELDS = frozenset(
    {
        "event_id",
        "membership",
        "membership_epoch",
        "membership_provenance",
        "previous_membership",
        "previous_membership_epoch",
        "source_kind",
        "source_record_id",
        "timeline_provenance",
    },
)


def _require(condition: object) -> None:
    if not condition:
        raise ValueError


def _canonical_object(payload: object) -> dict[str, object]:
    _require(type(payload) is bytes)
    value = loads(
        cast("bytes", payload).decode(),
        object_pairs_hook=lambda pairs: (
            result if len(result := dict(pairs)) == len(pairs) else (_ for _ in ()).throw(ValueError())
        ),
        parse_float=lambda _text: (_ for _ in ()).throw(ValueError()),
        parse_int=lambda text: number if abs(number := int(text)) <= 2**53 - 1 else (_ for _ in ()).throw(ValueError()),
        parse_constant=lambda _text: (_ for _ in ()).throw(ValueError()),
    )
    _require(type(value) is dict and _ENCODER.encode(value).encode() == payload)
    return cast("dict[str, object]", value)


def _validate_origin(origin: object) -> None:
    if type(origin) is ingest.RecordOrigin:
        record_origin = origin
        _require(type(record_origin.transport) is ingest.TransportKind)
        _require(
            all(
                type(value) is int and value >= 0
                for value in (
                    record_origin.source_epoch,
                    record_origin.request_id,
                    record_origin.frame_index,
                )
            ),
        )
        return
    _require(type(origin) is ingest.SystemOrigin)
    system_origin = cast("ingest.SystemOrigin", origin)
    _require(
        type(system_origin.kind) is ingest.SystemOriginKind and type(system_origin.operation_id) is UUID,
    )


def _base_admission(
    batch: ingest.SyncBatch,
    digest: bytes,
    record_id: str,
    disposition: ej.IngestionRecordDisposition,
    **effects: object,
) -> ej.IngestionBatchAdmission:
    return ej.IngestionBatchAdmission(
        schema_version=1,
        consumer_generation=batch.consumer_generation,
        stream_id=batch.ref.stream_id,
        sequence=batch.ref.sequence,
        sha256=digest,
        record_id=record_id,
        disposition=disposition,
        source=cast("ej.DepartureSource | None", effects.get("source")),
        room_id=cast("str | None", effects.get("room_id")),
        previous_membership=cast(
            "str | None",
            effects.get("previous_membership"),
        ),
        membership=cast("str | None", effects.get("membership")),
        previous_membership_epoch=cast(
            "int | None",
            effects.get("previous_membership_epoch"),
        ),
        membership_epoch=cast("int | None", effects.get("membership_epoch")),
        event=cast("ej.InboundEvent | None", effects.get("event")),
        projected=cast("ej.ProjectedEvent | None", effects.get("projected")),
    )


def _event_admission(
    batch: ingest.SyncBatch,
    digest: bytes,
    record: ingest.EventRecord,
    account_id: str,
) -> ej.IngestionBatchAdmission:
    _require(type(record.record_id) is str and bool(record.record_id))
    _require(type(record.kind) is ingest.RecordKind)
    _validate_origin(record.origin)
    source = _canonical_object(record.source_json)
    clear = None if record.clear_json is None else _canonical_object(record.clear_json)
    room_kind = record.kind in _ROOM_KINDS
    _require(
        ((type(record.room_id) is str and bool(record.room_id)) if room_kind else record.room_id is None),
    )
    _require(
        (
            (
                type(record.membership_epoch) is int
                and record.membership_epoch >= 0
                and type(record.room_sequence) is int
                and record.room_sequence >= 0
            )
            if room_kind
            else record.membership_epoch is None and record.room_sequence is None
        ),
    )

    if record.kind is ingest.RecordKind.ROOM_LIFECYCLE:
        return _lifecycle_admission(batch, digest, record, source)
    _require(type(record.origin) is ingest.RecordOrigin)
    if record.kind is ingest.RecordKind.TIMELINE:
        provenance = record.provenance
        _require(
            (record.event_id is None or (type(record.event_id) is str and bool(record.event_id)))
            and type(provenance) is ingest.TimelineEventProvenance,
        )
        assert type(record.room_id) is str
        views = ingestion_timeline_views(
            room_id=record.room_id,
            source=clear if clear is not None else source,
            self_sender=account_id,
            provenance=cast("ingest.TimelineEventProvenance", provenance),
            expected_event_id=record.event_id,
        )
        if views is not None:
            event, projected = views
            return _base_admission(
                batch,
                digest,
                record.record_id,
                ej.IngestionRecordDisposition.SEMANTIC_EVENT,
                event=event,
                projected=projected,
            )
    else:
        source_event_id = source.get("event_id")
        expected_event_id = source_event_id if type(source_event_id) is str and source_event_id else None
        _require(record.event_id == expected_event_id)
        _require(record.provenance is None)
        _require(clear is None or record.kind is ingest.RecordKind.TO_DEVICE)
    return _base_admission(
        batch,
        digest,
        record.record_id,
        ej.IngestionRecordDisposition.COMPATIBILITY_ONLY,
    )


def _lifecycle_admission(
    batch: ingest.SyncBatch,
    digest: bytes,
    record: ingest.EventRecord,
    source: dict[str, object],
) -> ej.IngestionBatchAdmission:
    _require(
        record.event_id is None
        and record.provenance is None
        and record.clear_json is None
        and set(source) == _LIFECYCLE_FIELDS,
    )
    previous = source["previous_membership"]
    current = source["membership"]
    previous_epoch = source["previous_membership_epoch"]
    current_epoch = source["membership_epoch"]
    _require(
        (previous is None or (type(previous) is str and previous in _MEMBERSHIPS))
        and type(current) is str
        and current in _MEMBERSHIPS
        and current != previous
        and type(previous_epoch) is type(current_epoch) is int,
    )
    previous_epoch_value = cast("int", previous_epoch)
    current_epoch_value = cast("int", current_epoch)
    _require(
        previous_epoch_value >= 0
        and current_epoch_value == previous_epoch_value + int(previous == "join" and current != "join")
        and record.membership_epoch == current_epoch_value,
    )
    if type(record.origin) is ingest.SystemOrigin:
        origin = record.origin
        _require(
            origin.kind is ingest.SystemOriginKind.MEMBERSHIP_CHANGE
            and previous is not None
            and current in {"join", "leave"}
            and source["membership_provenance"] == "local"
            and source["source_kind"] == "local"
            and source["event_id"] is None
            and source["source_record_id"] is None
            and source["timeline_provenance"] is None
            and record.record_id == str(uuid5(origin.operation_id, "nio:room-lifecycle:v1")),
        )
        departure_source = ej.DepartureSource.LOCAL
    else:
        source_kind = source["source_kind"]
        event_id = source["event_id"]
        source_record_id = source["source_record_id"]
        timeline_provenance = source["timeline_provenance"]
        _require(
            source["membership_provenance"] == "reported"
            and (
                (event_id is None or (type(event_id) is str and bool(event_id)))
                and (
                    (
                        source_kind == "section"
                        and event_id is None
                        and source_record_id is None
                        and timeline_provenance is None
                    )
                    or (
                        source_kind == "state"
                        and type(source_record_id) is str
                        and bool(source_record_id)
                        and timeline_provenance is None
                    )
                    or (
                        source_kind == "timeline"
                        and type(source_record_id) is str
                        and bool(source_record_id)
                        and timeline_provenance in {"history", "live"}
                    )
                )
            )
            and (previous is not None or previous_epoch_value == current_epoch_value == 0),
        )
        departure_source = ej.DepartureSource.REPORTED
    assert type(record.room_id) is str
    return _base_admission(
        batch,
        digest,
        record.record_id,
        ej.IngestionRecordDisposition.ROOM_LIFECYCLE,
        source=departure_source,
        room_id=record.room_id,
        previous_membership=previous,
        membership=current,
        previous_membership_epoch=previous_epoch_value,
        membership_epoch=current_epoch_value,
    )


def _loss_admission(
    batch: ingest.SyncBatch,
    digest: bytes,
    record: ingest.LossRecord,
) -> ej.IngestionBatchAdmission:
    _validate_origin(record.origin)
    _require(
        type(record.loss_id) is str
        and bool(record.loss_id)
        and type(record.room_id) is str
        and bool(record.room_id)
        and type(record.membership_epoch) is int
        and record.membership_epoch >= 0
        and type(record.reason) is ingest.LossReason
        and type(record.boundary) is ingest.LossBoundary,
    )
    if type(record.origin) is ingest.SystemOrigin:
        _require(record.origin.kind is not ingest.SystemOriginKind.MEMBERSHIP_CHANGE)
    boundary = record.boundary
    _require(
        (boundary.prior_event_id is None or type(boundary.prior_event_id) is str)
        and (boundary.prior_origin_server_ts is None or type(boundary.prior_origin_server_ts) is int)
        and (boundary.start_token is None or type(boundary.start_token) is str)
        and (boundary.target_token is None or type(boundary.target_token) is str),
    )
    _canonical_object(record.detail_json)
    origin = record.origin
    if type(origin) is ingest.RecordOrigin:
        origin_id = f"transport:{origin.transport.value}:{origin.source_epoch}:{origin.request_id}:{origin.frame_index}"
    else:
        system_origin = cast("ingest.SystemOrigin", origin)
        origin_id = f"system:{system_origin.kind.value}:{system_origin.operation_id}"
    boundary_digest = sha256(
        _ENCODER.encode(
            {
                "prior_event_id": boundary.prior_event_id,
                "prior_origin_server_ts": boundary.prior_origin_server_ts,
                "start_token": boundary.start_token,
                "target_token": boundary.target_token,
            },
        ).encode(),
    ).hexdigest()
    expected_id = str(
        uuid5(
            batch.ref.stream_id,
            f"{record.room_id}:{record.membership_epoch}:{origin_id}:{record.reason.value}:{boundary_digest}",
        ),
    )
    if not compare_digest(record.loss_id, expected_id):
        raise ej.IngestionBatchIntegrityError
    return _base_admission(
        batch,
        digest,
        record.loss_id,
        ej.IngestionRecordDisposition.HISTORY_LOSS,
        room_id=record.room_id,
    )


def validate_ingestion_batch(
    batch: ingest.SyncBatch,
    *,
    account_id: str,
    device_id: str,
) -> ej.IngestionBatchAdmission:
    """Authenticate and convert one exact Task 5 batch without writing."""
    try:
        _require(type(batch) is ingest.SyncBatch)
        _require(
            type(account_id) is type(device_id) is str and account_id and device_id,
        )
        _require(type(batch.account_id) is type(batch.device_id) is str)
        _require((batch.account_id, batch.device_id) == (account_id, device_id))
        _require(type(batch.schema_version) is int and batch.schema_version == 1)
        _require(type(batch.ref) is ingest.BatchRef)
        ref = batch.ref
        generation = batch.consumer_generation
        stream_id, sequence, batch_id = ref.stream_id, ref.sequence, ref.batch_id
        _require(type(generation) is type(stream_id) is type(batch_id) is UUID)
        _require(type(sequence) is type(batch.created_revision) is int)
        _require(0 <= sequence <= 2**63 - 2 and batch.created_revision > 0)
        _require(type(ref.sha256) is bytes and len(ref.sha256) == 32)
        payload = ingest.canonical_batch_payload(batch)
        _require(len(payload) <= 16 * 1024 * 1024)
        digest = sha256(payload).digest()
        expected_id = uuid5(stream_id, f"{sequence}:{digest.hex()}")
        if not compare_digest(ref.sha256, digest) or batch_id != expected_id:
            raise ej.IngestionBatchIntegrityError  # noqa: TRY301
        records = batch.records
        _require(type(records) is tuple and len(records) == 1)
        record = records[0]
        _require(type(record) in (ingest.EventRecord, ingest.LossRecord))
        if type(record) is ingest.EventRecord:
            admission = _event_admission(batch, digest, record, account_id)
        else:
            admission = _loss_admission(
                batch,
                digest,
                cast("ingest.LossRecord", record),
            )
        ej.validate_ingestion_batch_admission(admission)
    except (ej.IngestionBatchIntegrityError, ej.IngestionBatchValidationError):
        raise
    except Exception as error:
        raise ej.IngestionBatchValidationError from error
    return admission


async def consume_one_ingestion_batch(session: _OwnedIngestionSession, admission: IngestionBatchAdmissionView, *, account_id: str, device_id: str) -> ej.AdmissionFacts | None:  # fmt: skip
    """Admit and then settle at most one authenticated batch."""
    batch = session.next_batch(max_records=1)
    if batch is None:
        return None
    result = await admission.admit_ingestion_batch(validate_ingestion_batch(batch, account_id=account_id, device_id=device_id))  # fmt: skip
    if (
        type(result) is not ej.AdmissionFacts
        or type(result.receipt_new) is not bool
        or type(result.semantic_event_new) is not bool
        or (result.semantic_event_new and not result.receipt_new)
    ):
        raise ej.IngestionBatchIntegrityError
    await session._settle_batch(
        batch,
        receipt_new=result.receipt_new,
        semantic_event_new=result.semantic_event_new,
    )
    return result


async def run_ingestion_pump(
    session: _OwnedIngestionSession,
    admission: IngestionBatchAdmissionView,
    *,
    account_id: str,
    device_id: str,
    wait_for_work: Callable[[], Awaitable[None]],
    wake_semantic_dispatch: Callable[[], None],
) -> None:
    """Drain one-record batches until cancellation, waiting without polling."""
    while True:
        facts = await consume_one_ingestion_batch(
            session,
            admission,
            account_id=account_id,
            device_id=device_id,
        )
        if facts is None:
            await wait_for_work()
        elif facts.semantic_event_new:
            wake_semantic_dispatch()
