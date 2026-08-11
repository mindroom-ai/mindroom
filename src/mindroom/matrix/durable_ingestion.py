"""Strict conversion of authenticated nio batches into local admissions."""

from hashlib import sha256
from hmac import compare_digest
from json import JSONEncoder, loads
from typing import cast
from uuid import UUID, uuid5

import nio.ingest as ingest  # noqa: PLR0402 - carriers are owned by nio.ingest
from nio import IngestionSession

import mindroom.event_journal as ej
from mindroom.event_journal.journal import _validate_ingestion_batch_admission
from mindroom.event_journal.views import IngestionBatchAdmissionView
from mindroom.matrix.journal_ingress import ingestion_event_views as event_views


def validate_ingestion_batch(  # noqa: PLR0915
    batch: ingest.SyncBatch, *, account_id: str, device_id: str  # noqa: COM812
) -> ej.IngestionBatchAdmission:
    """Authenticate and convert one exact Task 5 batch without writing."""

    def checked[T](value: T, condition: object) -> T:
        if not condition:
            raise ValueError
        return value

    def require(condition: object) -> None:
        checked(None, condition)

    def exact[T](value: object, kind: type[T]) -> T:
        return cast("T", checked(value, type(value) is kind))

    try:
        require(type(batch) is ingest.SyncBatch)
        require(type(account_id) is type(device_id) is str and account_id and device_id)
        require(type(batch.account_id) is type(batch.device_id) is str)
        require((batch.account_id, batch.device_id) == (account_id, device_id))
        require(type(batch.schema_version) is int and batch.schema_version == 1)
        ref = checked(batch.ref, type(batch.ref) is ingest.BatchRef)
        generation = batch.consumer_generation
        stream_id, sequence, batch_id = ref.stream_id, ref.sequence, ref.batch_id
        require(type(generation) is type(stream_id) is type(batch_id) is UUID)
        require(type(sequence) is type(batch.created_revision) is int)
        require(0 <= sequence <= 2**63 - 2 and batch.created_revision > 0)
        require(type(ref.sha256) is bytes and len(ref.sha256) == 32)
        payload = ingest.canonical_batch_payload(batch)
        require(len(payload) <= 16 * 1024 * 1024)
        digest = sha256(payload).digest()
        expected_id = uuid5(stream_id, f"{sequence}:{digest.hex()}")
        if not compare_digest(ref.sha256, digest) or batch_id != expected_id:
            raise ej.IngestionBatchIntegrityError  # noqa: TRY301
        records = batch.records
        require(type(records) is tuple and len(records) == 1)
        record = exact(records[0], ingest.EventRecord)
        origin = exact(record.origin, ingest.RecordOrigin)
        membership_epoch = exact(record.membership_epoch, int)
        event_id = record.event_id
        require(type(record.record_id) is str and record.record_id)
        room_id = exact(record.room_id, str)
        require(room_id)
        require(record.kind is ingest.RecordKind.TIMELINE)
        require(origin.transport is ingest.TransportKind.CLASSIC)
        counters = origin.source_epoch, origin.request_id, origin.frame_index
        require(all(type(value) is int and value >= 0 for value in counters))
        require(membership_epoch >= 0)
        require(type(record.room_sequence) is int and record.room_sequence >= 0)
        require(event_id is None or (type(event_id) is str and bool(event_id)))
        require(record.provenance is ingest.TimelineEventProvenance.LIVE)
        require(type(record.source_json) is bytes and record.clear_json is None)
        source = loads(
            record.source_json.decode(),
            object_pairs_hook=lambda p: checked(obj := dict(p), len(obj) == len(p)),
            parse_float=int,
            parse_int=lambda text: checked(value := int(text), abs(value) <= 2**53 - 1),
            parse_constant=int,
        )
        encoder = JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        encoded = encoder.encode(source).encode()
        require(type(source) is dict and encoded == record.source_json)
        require(source.get("type") != "m.room.encrypted")
        event, projected = event_views(
            room_id=room_id, source=source, self_sender=account_id  # noqa: COM812
        )
        require(event.event_id and (event_id is None or event_id == event.event_id))
        identity = (1, generation, stream_id, sequence, digest, membership_epoch)
        admission = ej.IngestionBatchAdmission(*identity, event, projected)
        _validate_ingestion_batch_admission(admission)
    except (ej.IngestionBatchIntegrityError, ej.IngestionBatchValidationError):
        raise
    except Exception as error:
        raise ej.IngestionBatchValidationError from error
    return admission


async def consume_one_ingestion_batch(session: IngestionSession, admission: IngestionBatchAdmissionView, *, account_id: str, device_id: str) -> ej.AdmissionResult | None:  # fmt: skip
    """Admit and then acknowledge at most one authenticated batch."""
    batch = session.next_batch(max_records=1)
    if batch is None:
        return None
    result = await admission.admit_ingestion_batch(validate_ingestion_batch(batch, account_id=account_id, device_id=device_id))  # fmt: skip
    if result is not ej.AdmissionResult.ADMITTED and result is not ej.AdmissionResult.DUPLICATE:  # fmt: skip
        raise ej.IngestionBatchIntegrityError
    session.acknowledge_batch(batch.ref)
    return result
