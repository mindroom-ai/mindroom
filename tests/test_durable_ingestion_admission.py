"""Strict validation for one durable nio ingestion batch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4, uuid5

import nio
import pytest
import pytest_asyncio
from nio.ingest import (
    BatchRef,
    EventRecord,
    RecordKind,
    RecordOrigin,
    SyncBatch,
    TimelineEventProvenance,
    TransportKind,
    canonical_batch_payload,
)
from nio.ingest.classic import ClassicSource
from nio.ingest.config import ClassicSourceConfig, IngestionConfig
from nio.ingest.ports import NetworkResult, StagedSourceResponse
from nio.ingest.serialization import batch_from_records
from nio.ingest.source import ClassicCursor, canonical_classic_cursor
from nio.ingest.state import SourceState, StagedFrame
from nio.store._sync_journal_plan import (
    _canonical_work_plaintext,
)  # READY fixture seam.
from nio.store._sync_journal_rows import _canonical_internal
from nio.store._sync_journal_values import MaterializerLimits, MaterializeStatus
from nio.store.sync_journal import open_ingestion_store

from mindroom.event_journal import (
    AdmissionResult,
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionBatchSequenceError,
    IngestionBatchValidationError,
    IngestionConsumer,
    IngestionConsumerBindingError,
    ProjectedEvent,
)
from mindroom.matrix import client_session
from mindroom.matrix import durable_ingestion as durable_ingestion_module
from mindroom.matrix.durable_ingestion import (
    consume_one_ingestion_batch,
    validate_ingestion_batch,
)
from mindroom.matrix.journal_ingress import ingestion_event_views
from tests.conftest import (
    CrashError,
    DiesAfterNextWriteCommit,
    postgres_journal_schema_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence

    from nio.ingest.state import OwnerView
    from nio.store._sync_journal import SqliteIngestionJournal

    from mindroom.event_journal import PrincipalStore
    from mindroom.event_journal.backend import Backend, Operation, Transaction

ACCOUNT_ID = "@bot:example.org"
DEVICE_ID = "DEVICE"
SENDER = "@alice:example.org"
ROOM_ID = "!room:example.org"
EVENT_ID = "$event"
RECORD_ID = "00000000-0000-4000-8000-000000000001"
CONSUMER_GENERATION = UUID("22222222-2222-4222-8222-222222222222")
STREAM_ID = UUID("44444444-4444-4444-8444-444444444444")
SOURCE_JSON = (
    b'{"content":{"body":"hello","msgtype":"m.text"},'
    b'"event_id":"$event","origin_server_ts":1000,'
    b'"sender":"@alice:example.org","type":"m.room.message"}'
)
GOLDEN_BATCH = (
    b'{"schema_version":1,"account_id":"@bot:example.org","device_id":"DEVICE",'
    b'"consumer_generation":"22222222-2222-4222-8222-222222222222",'
    b'"stream_id":"44444444-4444-4444-8444-444444444444","sequence":0,'
    b'"created_revision":1,"records":[{"record_type":"event",'
    b'"record_id":"00000000-0000-4000-8000-000000000001","kind":"timeline",'
    b'"origin":{"origin_type":"transport","transport":"classic",'
    b'"source_epoch":1,"request_id":2,"frame_index":3},'
    b'"room_id":"!room:example.org","membership_epoch":0,"room_sequence":0,'
    b'"event_id":"$event","provenance":"live",'
    b'"source_json":"eyJjb250ZW50Ijp7ImJvZHkiOiJoZWxsbyIsIm1zZ3R5cGUiOiJtLnRleHQifSwiZXZlbnRfaWQiOiIkZXZlbnQiLCJvcmlnaW5fc2VydmVyX3RzIjoxMDAwLCJzZW5kZXIiOiJAYWxpY2U6ZXhhbXBsZS5vcmciLCJ0eXBlIjoibS5yb29tLm1lc3NhZ2UifQ==",'
    b'"clear_json":null}]}'
)
GOLDEN_SHA256 = "4e1eb87df166562e921aad9ccda0ad2023cb206ee3a3fe802d8711925a3940cf"
GOLDEN_BATCH_ID = UUID("02b67409-f182-58f3-9d27-f9b4c857969c")
CANARY_ROOM_ID = "!canary:example.org"
CANARY_AGENT_NAME = "canary-agent"
CANARY_REQUESTED_GENERATION = UUID("55555555-5555-4555-8555-555555555555")
CANARY_GENERATION = UUID("66666666-6666-4666-8666-666666666666")
CANARY_FILTER = (
    b'{"account_data":{"not_types":["*"]},"presence":{"not_types":["*"]},'
    b'"room":{"account_data":{"not_types":["*"]},"ephemeral":{"not_types":'
    b'["*"]},"rooms":["!canary:example.org"],"state":{"not_types":["*"]},'
    b'"timeline":{"limit":1,"not_senders":["@bot:example.org"],"types":'
    b'["m.room.encrypted","m.room.message"]}}}'
)
CANARY_BOUNDARIES = {
    "consumer": "load_or_create_ingestion_consumer",
    "bootstrap": "open_ingestion_store",
    "bind": "bind_ingestion_stream",
    "cold-stage": "frame_insert",
    "live-stage": "frame_insert",
    "cold-materialize": "frame_delete",
    "live-materialize": "frame_delete",
    "hydration-apply": "aggregate_update",
    "claim": "delivery_claim_meta_cas",
    "admission": "admit_ingestion_batch",
    "ack": "delivery_ack_meta_cas",
    "idle": "batch_empty",
}
CANARY_TRANSITIONS = {
    phase: CANARY_BOUNDARIES[phase]
    for phase in (
        "cold-stage",
        "live-stage",
        "cold-materialize",
        "live-materialize",
        "hydration-apply",
        "claim",
        "ack",
    )
}


def _record() -> EventRecord:
    return EventRecord(
        RECORD_ID,
        RecordKind.TIMELINE,
        RecordOrigin(TransportKind.CLASSIC, 1, 2, 3),
        ROOM_ID,
        0,
        0,
        EVENT_ID,
        TimelineEventProvenance.LIVE,
        SOURCE_JSON,
        None,
    )


def _batch() -> SyncBatch:
    return batch_from_records(
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        consumer_generation=CONSUMER_GENERATION,
        stream_id=STREAM_ID,
        sequence=0,
        created_revision=1,
        records=(_record(),),
    )


def _set(batch: SyncBatch, target: str, field: str, value: object) -> None:
    owner: object = batch
    if target == "ref":
        owner = batch.ref
    elif target == "record":
        owner = batch.records[0]
    elif target == "origin":
        owner = batch.records[0].origin
    object.__setattr__(owner, field, value)


def _resign(batch: SyncBatch) -> None:
    digest = hashlib.sha256(canonical_batch_payload(batch)).digest()
    object.__setattr__(batch.ref, "sha256", digest)
    object.__setattr__(
        batch.ref,
        "batch_id",
        uuid5(batch.ref.stream_id, f"{batch.ref.sequence}:{digest.hex()}"),
    )


def _expected_admission() -> IngestionBatchAdmission:
    source = {
        "content": {"body": "hello", "msgtype": "m.text"},
        "event_id": EVENT_ID,
        "origin_server_ts": 1000,
        "sender": SENDER,
        "type": "m.room.message",
    }
    event = InboundEvent(
        EVENT_ID,
        ROOM_ID,
        None,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
        SENDER,
        1000,
        source,
    )
    projected = ProjectedEvent(
        EVENT_ID,
        ROOM_ID,
        None,
        SENDER,
        1000,
        source["content"],
        None,
        None,
    )
    return IngestionBatchAdmission(
        1,
        CONSUMER_GENERATION,
        STREAM_ID,
        0,
        bytes.fromhex(GOLDEN_SHA256),
        0,
        event,
        projected,
    )


def _admission(
    *,
    sequence: int = 0,
    event_id: str = EVENT_ID,
    sha256: bytes = bytes.fromhex(GOLDEN_SHA256),
) -> IngestionBatchAdmission:
    admission = _expected_admission()
    source = dict(admission.event.source)
    source["event_id"] = event_id
    assert admission.projected is not None
    return replace(
        admission,
        sequence=sequence,
        sha256=sha256,
        event=replace(admission.event, event_id=event_id, source=source),
        projected=replace(admission.projected, event_id=event_id),
    )


def test_validation_freezes_task5_golden_and_exact_conversion() -> None:
    batch = _batch()

    assert nio.__file__ == "/tmp/nio-task5-install.yiFetw/nio/__init__.py"
    assert canonical_batch_payload(batch) == GOLDEN_BATCH
    assert batch.ref == BatchRef(
        STREAM_ID,
        0,
        GOLDEN_BATCH_ID,
        bytes.fromhex(GOLDEN_SHA256),
    )
    assert (
        validate_ingestion_batch(
            batch,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
        == _expected_admission()
    )


@pytest.mark.parametrize(
    ("target", "field", "value", "error"),
    (
        ("batch", "schema_version", True, IngestionBatchValidationError),
        ("batch", "schema_version", 2, IngestionBatchValidationError),
        ("batch", "account_id", "", IngestionBatchValidationError),
        ("batch", "account_id", 1, IngestionBatchValidationError),
        ("batch", "account_id", "@other:example.org", IngestionBatchValidationError),
        ("batch", "device_id", "", IngestionBatchValidationError),
        ("batch", "device_id", 1, IngestionBatchValidationError),
        ("batch", "device_id", "OTHER", IngestionBatchValidationError),
        (
            "batch",
            "consumer_generation",
            str(CONSUMER_GENERATION),
            IngestionBatchValidationError,
        ),
        ("batch", "ref", object(), IngestionBatchValidationError),
        ("ref", "stream_id", str(STREAM_ID), IngestionBatchValidationError),
        ("ref", "sequence", True, IngestionBatchValidationError),
        ("ref", "sequence", -1, IngestionBatchValidationError),
        ("ref", "sequence", 2**63 - 1, IngestionBatchValidationError),
        ("ref", "sha256", b"short", IngestionBatchValidationError),
        ("ref", "sha256", bytearray(32), IngestionBatchValidationError),
        ("ref", "sha256", b"x" * 32, IngestionBatchIntegrityError),
        ("ref", "batch_id", str(UUID(int=9)), IngestionBatchValidationError),
        ("ref", "batch_id", UUID(int=9), IngestionBatchIntegrityError),
        ("batch", "created_revision", True, IngestionBatchValidationError),
        ("batch", "created_revision", 0, IngestionBatchValidationError),
        ("record", "record_id", "", IngestionBatchValidationError),
        ("record", "record_id", 1, IngestionBatchValidationError),
        ("record", "kind", RecordKind.STATE, IngestionBatchValidationError),
        ("record", "origin", object(), IngestionBatchValidationError),
        ("origin", "transport", TransportKind.SLIDING, IngestionBatchValidationError),
        ("origin", "source_epoch", -1, IngestionBatchValidationError),
        ("origin", "request_id", True, IngestionBatchValidationError),
        ("origin", "frame_index", -1, IngestionBatchValidationError),
        ("record", "room_id", "", IngestionBatchValidationError),
        ("record", "room_id", 1, IngestionBatchValidationError),
        ("record", "membership_epoch", True, IngestionBatchValidationError),
        ("record", "membership_epoch", -1, IngestionBatchValidationError),
        ("record", "room_sequence", -1, IngestionBatchValidationError),
        ("record", "room_sequence", True, IngestionBatchValidationError),
        ("record", "event_id", 1, IngestionBatchValidationError),
        ("record", "event_id", "", IngestionBatchValidationError),
        (
            "record",
            "provenance",
            TimelineEventProvenance.HISTORY,
            IngestionBatchValidationError,
        ),
        ("record", "clear_json", b"{}", IngestionBatchValidationError),
        ("record", "source_json", "{}", IngestionBatchValidationError),
    ),
)
def test_validation_rejects_mutated_carrier_fields(
    target: str,
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    batch = _batch()
    _set(batch, target, field, value)
    if (target, field) in {
        ("record", "record_id"),
        ("record", "kind"),
        ("record", "room_id"),
        ("record", "membership_epoch"),
        ("record", "room_sequence"),
        ("record", "event_id"),
        ("record", "provenance"),
        ("record", "clear_json"),
        ("origin", "transport"),
        ("origin", "source_epoch"),
        ("origin", "request_id"),
        ("origin", "frame_index"),
    } and not (
        type(value) is bool
        and (target, field)
        in {("record", "membership_epoch"), ("record", "room_sequence")}
    ):
        _resign(batch)

    with pytest.raises(error):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


@pytest.mark.parametrize(
    ("account_id", "device_id"),
    (
        (True, DEVICE_ID),
        ("", DEVICE_ID),
        (ACCOUNT_ID, True),
        (ACCOUNT_ID, ""),
    ),
)
def test_validation_rejects_invalid_authenticated_identity(
    account_id: object,
    device_id: object,
) -> None:
    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(
            _batch(),
            account_id=account_id,  # type: ignore[arg-type]
            device_id=device_id,  # type: ignore[arg-type]
        )


def test_validation_accepts_the_largest_sqlite_sequence() -> None:
    batch = _batch()
    object.__setattr__(batch.ref, "sequence", 2**63 - 2)
    _resign(batch)

    admission = validate_ingestion_batch(
        batch,
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
    )

    assert admission.sequence == 2**63 - 2


def test_validation_rejects_a_string_subclass_identity() -> None:
    class Text(str):
        pass

    batch = _batch()
    object.__setattr__(batch, "account_id", Text(ACCOUNT_ID))

    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_validation_accepts_an_absent_record_event_id() -> None:
    batch = _batch()
    object.__setattr__(batch.records[0], "event_id", None)
    _resign(batch)

    admission = validate_ingestion_batch(
        batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID
    )

    assert admission.event.event_id == EVENT_ID


@pytest.mark.parametrize("shape", ("empty", "multiple", "list", "wrong"))
def test_validation_rejects_noncanonical_record_cardinality(shape: str) -> None:
    batch = _batch()
    records: object = {
        "empty": (),
        "multiple": batch.records * 2,
        "list": list(batch.records),
        "wrong": (object(),),
    }[shape]
    object.__setattr__(batch, "records", records)
    if shape != "wrong":
        _resign(batch)

    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


_DEEP_JSON = (
    b'{"content":{"body":'
    + b"[" * 1_100
    + b"0"
    + b"]" * 1_100
    + b',"msgtype":"m.text"},"event_id":"$event","origin_server_ts":1000,'
    b'"sender":"@alice:example.org","type":"m.room.message"}'
)


@pytest.mark.parametrize(
    "source_json",
    (
        b"\xff",
        b"[]",
        b'{"content":{},"event_id":"$first","event_id":"$event",'
        b'"origin_server_ts":1000,"sender":"@alice:example.org",'
        b'"type":"m.room.message"}',
        SOURCE_JSON.replace(b'"origin_server_ts":1000', b'"origin_server_ts":1.0'),
        SOURCE_JSON.replace(b'"origin_server_ts":1000', b'"origin_server_ts":NaN'),
        SOURCE_JSON.replace(
            b'"origin_server_ts":1000', b'"origin_server_ts":9007199254740992'
        ),
        SOURCE_JSON.replace(
            b'"origin_server_ts":1000', b'"origin_server_ts":-9007199254740992'
        ),
        SOURCE_JSON.replace(b'"body":"hello"', rb'"body":"caf\u00e9"'),
        b'{"event_id":"$event","content":{"body":"hello","msgtype":"m.text"},'
        b'"origin_server_ts":1000,"sender":"@alice:example.org",'
        b'"type":"m.room.message"}',
        SOURCE_JSON.replace(b'"type":"m.room.message"', b'"type":"m.room.encrypted"'),
        SOURCE_JSON.replace(b'"type":"m.room.message"', b'"type":"m.room.topic"'),
        SOURCE_JSON.replace(b'"event_id":"$event",', b""),
        _DEEP_JSON,
    ),
)
def test_validation_rejects_noncanonical_or_unsupported_source_json(
    source_json: bytes,
) -> None:
    batch = _batch()
    object.__setattr__(batch.records[0], "source_json", source_json)
    _resign(batch)

    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_validation_rejects_record_event_id_disagreement() -> None:
    batch = _batch()
    object.__setattr__(batch.records[0], "event_id", "$other")
    _resign(batch)

    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_validation_authenticates_bytes_before_record_grammar() -> None:
    batch = _batch()
    object.__setattr__(batch.records[0], "kind", RecordKind.STATE)

    with pytest.raises(IngestionBatchIntegrityError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_validation_rejects_a_canonical_payload_over_16_mib() -> None:
    batch = _batch()
    source_json = SOURCE_JSON.replace(
        b'"body":"hello"', b'"body":"' + b"x" * (16 * 1024 * 1024) + b'"'
    )
    object.__setattr__(batch.records[0], "source_json", source_json)
    _resign(batch)

    with pytest.raises(IngestionBatchValidationError):
        validate_ingestion_batch(batch, account_id=ACCOUNT_ID, device_id=DEVICE_ID)


def test_validation_accepts_sorted_canonical_utf8_source_json() -> None:
    batch = _batch()
    source_json = SOURCE_JSON.replace(b'"body":"hello"', b'"body":"caf\xc3\xa9"')
    object.__setattr__(batch.records[0], "source_json", source_json)
    _resign(batch)

    admission = validate_ingestion_batch(
        batch,
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
    )

    assert admission.event.source["content"] == {
        "body": "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "msgtype": "m.text",
    }


@pytest.mark.parametrize(
    "source",
    (
        {
            "event_id": "$member",
            "sender": SENDER,
            "origin_server_ts": 1,
            "type": "m.room.member",
            "state_key": SENDER,
            "content": {"membership": "join"},
        },
        {
            "event_id": "$topic",
            "sender": SENDER,
            "origin_server_ts": 1,
            "type": "m.room.topic",
            "state_key": "",
            "content": {"topic": "unsupported"},
        },
    ),
)
def test_conversion_rejects_membership_and_unsupported_events(
    source: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ingestion_event_views(room_id=ROOM_ID, source=source, self_sender=ACCOUNT_ID)


def test_conversion_uses_the_existing_media_parser() -> None:
    source = {
        "event_id": "$image",
        "sender": SENDER,
        "origin_server_ts": 2,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.image",
            "body": "photo.png",
            "url": "mxc://example.org/photo",
        },
    }

    event, projected = ingestion_event_views(
        room_id=ROOM_ID,
        source=source,
        self_sender=ACCOUNT_ID,
    )

    assert event.kind is EventKind.MEDIA
    assert event.event_class is EventClass.ACTIONABLE
    assert projected is not None and projected.content == source["content"]


async def _bound_principal(store: EventJournalStore) -> PrincipalStore:
    principal = store.principal(ACCOUNT_ID)
    await principal.load_or_create_ingestion_consumer(
        new_generation=CONSUMER_GENERATION
    )
    await principal.bind_ingestion_stream(
        generation=CONSUMER_GENERATION,
        stream_id=STREAM_ID,
    )
    return principal


def _graph_values(
    transaction: Transaction,
    sql: str,
    columns: tuple[str, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row[column] for column in columns) for row in transaction.fetchall(sql)
    )


def _ingestion_graph(
    transaction: Transaction,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        "consumers": _graph_values(
            transaction,
            "SELECT principal_id, consumer_generation, stream_id, next_sequence "
            "FROM matrix_sync_consumers ORDER BY principal_id",
            ("principal_id", "consumer_generation", "stream_id", "next_sequence"),
        ),
        "events": _graph_values(
            transaction,
            "SELECT receipt_order, principal_id, event_id, room_id, thread_id, "
            "kind, sender, origin_server_ts, source_json, semantic_consumer, "
            "membership_epoch, state "
            "FROM journal_events ORDER BY principal_id, event_id",
            (
                "receipt_order",
                "principal_id",
                "event_id",
                "room_id",
                "thread_id",
                "kind",
                "sender",
                "origin_server_ts",
                "source_json",
                "semantic_consumer",
                "membership_epoch",
                "state",
            ),
        ),
        "projection": _graph_values(
            transaction,
            "SELECT principal_id, room_id, logical_event_id, thread_id, sender, "
            "created_ts, revision_event_id, revision_ts, content_json, "
            "refresh_token, membership_epoch FROM visible_messages "
            "ORDER BY principal_id, room_id, logical_event_id",
            (
                "principal_id",
                "room_id",
                "logical_event_id",
                "thread_id",
                "sender",
                "created_ts",
                "revision_event_id",
                "revision_ts",
                "content_json",
                "refresh_token",
                "membership_epoch",
            ),
        ),
        "receipts": _graph_values(
            transaction,
            "SELECT principal_id, consumer_generation, stream_id, sequence, "
            "schema_version, batch_sha256, event_id "
            "FROM matrix_ingestion_receipts "
            "ORDER BY principal_id, consumer_generation, stream_id, sequence",
            (
                "principal_id",
                "consumer_generation",
                "stream_id",
                "sequence",
                "schema_version",
                "batch_sha256",
                "event_id",
            ),
        ),
        "membership": _graph_values(
            transaction,
            "SELECT principal_id, room_id, membership_epoch, departure_fenced, "
            "owed_departure_reports FROM room_membership "
            "ORDER BY principal_id, room_id",
            (
                "principal_id",
                "room_id",
                "membership_epoch",
                "departure_fenced",
                "owed_departure_reports",
            ),
        ),
    }


async def _graph(store: EventJournalStore) -> dict[str, tuple[tuple[object, ...], ...]]:
    return await store.backend.read(_ingestion_graph)


def _fresh_graph() -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        "consumers": ((ACCOUNT_ID, str(CONSUMER_GENERATION), str(STREAM_ID), 1),),
        "events": (
            (
                1,
                ACCOUNT_ID,
                EVENT_ID,
                ROOM_ID,
                "",
                EventKind.MESSAGE.value,
                SENDER,
                1000,
                SOURCE_JSON.decode(),
                None,
                0,
                "pending",
            ),
        ),
        "projection": (
            (
                ACCOUNT_ID,
                ROOM_ID,
                EVENT_ID,
                "",
                SENDER,
                1000,
                EVENT_ID,
                1000,
                '{"body":"hello","msgtype":"m.text"}',
                None,
                0,
            ),
        ),
        "receipts": (
            (
                ACCOUNT_ID,
                str(CONSUMER_GENERATION),
                str(STREAM_ID),
                0,
                1,
                GOLDEN_SHA256,
                EVENT_ID,
            ),
        ),
        "membership": ((ACCOUNT_ID, ROOM_ID, 0, 0, 0),),
    }


def _old_graph() -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        "consumers": ((ACCOUNT_ID, str(CONSUMER_GENERATION), str(STREAM_ID), 0),),
        "events": (),
        "projection": (),
        "receipts": (),
        "membership": (),
    }


def _generic_graph() -> dict[str, tuple[tuple[object, ...], ...]]:
    graph = _fresh_graph()
    graph["consumers"] = ((ACCOUNT_ID, str(CONSUMER_GENERATION), str(STREAM_ID), 0),)
    graph["receipts"] = ()
    graph["membership"] = ()
    return graph


def _fenced_fresh_graph() -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        "consumers": ((ACCOUNT_ID, str(CONSUMER_GENERATION), str(STREAM_ID), 1),),
        "events": (
            (
                1,
                ACCOUNT_ID,
                EVENT_ID,
                ROOM_ID,
                "",
                EventKind.MESSAGE.value,
                SENDER,
                1000,
                "",
                None,
                0,
                "settled",
            ),
        ),
        "projection": (),
        "receipts": (
            (
                ACCOUNT_ID,
                str(CONSUMER_GENERATION),
                str(STREAM_ID),
                0,
                1,
                GOLDEN_SHA256,
                EVENT_ID,
            ),
        ),
        "membership": ((ACCOUNT_ID, ROOM_ID, 1, 1, 0),),
    }


def _fence_only_graph() -> dict[str, tuple[tuple[object, ...], ...]]:
    graph = _old_graph()
    graph["membership"] = ((ACCOUNT_ID, ROOM_ID, 1, 1, 0),)
    return graph


class _ExplodingTransaction:
    def __init__(self) -> None:
        self.statements = 0

    def _explode(self) -> None:
        self.statements += 1
        raise AssertionError("local admission reached SQL before validation")

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self._explode()

    def fetchone(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> Mapping[str, object] | None:
        self._explode()

    def fetchall(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> tuple[Mapping[str, object], ...]:
        self._explode()


@dataclass
class _DirectBackend:
    transaction: _ExplodingTransaction = field(default_factory=_ExplodingTransaction)

    async def write[T](self, operation: Operation[T]) -> T:
        return operation(self.transaction)

    async def read[T](self, operation: Operation[T]) -> T:
        return operation(self.transaction)

    async def close(self) -> None:
        pass


class _ObservedTransaction:
    def __init__(
        self,
        inner: Transaction,
        *,
        trace: list[str] | None = None,
        statement_matches: Callable[[str], bool] | None = None,
        after_statement: Callable[[], object] | None = None,
    ) -> None:
        self._inner = inner
        self._trace = trace
        self._statement_matches = statement_matches
        self._after_statement = after_statement
        self._matched = False

    def _after(self, sql: str) -> None:
        if self._trace is not None:
            self._trace.append(" ".join(sql.split()))
        if (
            not self._matched
            and self._statement_matches is not None
            and self._statement_matches(sql)
        ):
            self._matched = True
            assert self._after_statement is not None
            self._after_statement()

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self._inner.execute(sql, params)
        self._after(sql)

    def fetchone(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> Mapping[str, object] | None:
        row = self._inner.fetchone(sql, params)
        self._after(sql)
        return row

    def fetchall(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> tuple[Mapping[str, object], ...]:
        rows = self._inner.fetchall(sql, params)
        self._after(sql)
        return rows


class _MutatingReturnTransaction:
    def __init__(
        self,
        inner: Transaction,
        *,
        statement_fragment: str,
        field_name: str,
        value: object,
    ) -> None:
        self._inner = inner
        self._statement_fragment = statement_fragment
        self._field_name = field_name
        self._value = value

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self._inner.execute(sql, params)

    def fetchone(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> Mapping[str, object] | None:
        row = self._inner.fetchone(sql, params)
        if row is None or self._statement_fragment not in sql:
            return row
        mutated = dict(row)
        mutated[self._field_name] = self._value
        return mutated

    def fetchall(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> tuple[Mapping[str, object], ...]:
        return self._inner.fetchall(sql, params)


class _NoClaimTransaction:
    def __init__(self, inner: Transaction) -> None:
        self._inner = inner

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self._inner.execute(sql, params)

    def fetchone(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> Mapping[str, object] | None:
        if sql.lstrip().startswith("UPDATE matrix_sync_consumers SET next_sequence"):
            return None
        return self._inner.fetchone(sql, params)

    def fetchall(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> tuple[Mapping[str, object], ...]:
        return self._inner.fetchall(sql, params)


@dataclass(frozen=True, slots=True)
class _ObservedBackend:
    inner: Backend
    wrap: Callable[[Transaction], Transaction]

    async def write[T](self, operation: Operation[T]) -> T:
        return await self.inner.write(
            lambda transaction: operation(self.wrap(transaction))
        )

    async def read[T](self, operation: Operation[T]) -> T:
        return await self.inner.read(operation)

    async def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _WriteEntryBackend:
    inner: Backend
    entered: asyncio.Event

    async def write[T](self, operation: Operation[T]) -> T:
        self.entered.set()
        return await self.inner.write(operation)

    async def read[T](self, operation: Operation[T]) -> T:
        return await self.inner.read(operation)

    async def close(self) -> None:
        pass


class _InjectedBoundaryError(RuntimeError):
    pass


def _raise_injected_boundary() -> None:
    raise _InjectedBoundaryError


class _Text(str):
    pass


def _mutated_admission(target: str, field_name: str, value: object) -> object:
    admission = _expected_admission()
    if target == "self":
        return value
    if target == "admission":
        return replace(admission, **{field_name: value})
    if target == "event":
        return replace(
            admission,
            event=replace(admission.event, **{field_name: value}),
        )
    assert admission.projected is not None
    return replace(
        admission,
        projected=replace(admission.projected, **{field_name: value}),
    )


_CLASSIFIER_SQL = (
    "SELECT c.consumer_generation AS c_generation, c.stream_id AS c_stream, "
    "c.next_sequence AS c_next, r.schema_version AS r_schema, "
    "r.batch_sha256 AS r_sha256, r.event_id AS r_event_id "
    "FROM matrix_sync_consumers AS c LEFT JOIN matrix_ingestion_receipts AS r "
    "ON r.principal_id = c.principal_id "
    "AND r.consumer_generation = c.consumer_generation "
    "AND r.stream_id = c.stream_id AND r.sequence = ? WHERE c.principal_id = ?"
)


def _assert_classifier_trace(trace: list[str]) -> None:
    assert len(trace) == 2
    assert trace[0].startswith("UPDATE matrix_sync_consumers SET next_sequence")
    assert trace[1] == _CLASSIFIER_SQL


@pytest.mark.asyncio
async def test_admit_ingestion_batch_persists_fresh_unit_on_both_backends(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)

    result = await principal.admit_ingestion_batch(_expected_admission())

    assert result is AdmissionResult.ADMITTED
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "field_name", "value"),
    (
        ("self", "", object()),
        ("admission", "schema_version", True),
        ("admission", "schema_version", 2),
        ("admission", "consumer_generation", str(CONSUMER_GENERATION)),
        ("admission", "stream_id", str(STREAM_ID)),
        ("admission", "sequence", True),
        ("admission", "sequence", -1),
        ("admission", "sequence", 2**63 - 1),
        ("admission", "sha256", bytearray(32)),
        ("admission", "sha256", b"short"),
        ("admission", "membership_epoch", True),
        ("admission", "membership_epoch", -1),
        ("admission", "event", object()),
        ("admission", "projected", object()),
        ("event", "event_id", 1),
        ("event", "event_id", ""),
        ("event", "room_id", 1),
        ("event", "room_id", ""),
        ("event", "sender", 1),
        ("event", "sender", ""),
        ("event", "thread_id", 1),
        ("event", "thread_id", ""),
        ("event", "kind", EventKind.ROOM_LIFECYCLE),
        ("event", "kind", EventKind.DECRYPTION_FAILURE),
        ("event", "kind", EventKind.MESSAGE.value),
        ("event", "event_class", EventClass.ACTIONABLE.value),
        ("event", "origin_server_ts", True),
        ("event", "source", []),
        ("projected", "event_id", 1),
        ("projected", "event_id", "$other"),
        ("projected", "room_id", 1),
        ("projected", "room_id", "!other:example.org"),
        ("projected", "sender", 1),
        ("projected", "sender", "@other:example.org"),
        ("projected", "thread_id", "thread"),
        ("projected", "origin_server_ts", True),
        ("projected", "origin_server_ts", 1001),
        ("projected", "content", []),
        ("projected", "replaces_event_id", ""),
        ("projected", "redacts_event_id", 1),
    ),
)
async def test_local_admission_grammar_rejects_invalid_direct_values_before_sql(
    target: str,
    field_name: str,
    value: object,
) -> None:
    backend = _DirectBackend()
    principal = EventJournalStore(backend=backend).principal(ACCOUNT_ID)

    with pytest.raises(IngestionBatchValidationError):
        await principal.admit_ingestion_batch(  # type: ignore[arg-type]
            _mutated_admission(target, field_name, value)
        )

    assert backend.transaction.statements == 0


@pytest.mark.asyncio
async def test_admit_ingestion_batch_membership_epoch_mismatch_rolls_back(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    await principal.fence_departure(ROOM_ID, source=DepartureSource.REPORTED)
    before = await _graph(store)

    with pytest.raises(IngestionBatchValidationError):
        await principal.admit_ingestion_batch(_expected_admission())

    assert await _graph(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fragment", "field_name", "value", "error"),
    (
        (
            "UPDATE matrix_sync_consumers",
            "consumer_generation",
            str(UUID(int=9)),
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "consumer_generation",
            CONSUMER_GENERATION,
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "consumer_generation",
            _Text(str(CONSUMER_GENERATION)),
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "stream_id",
            str(UUID(int=9)),
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "stream_id",
            STREAM_ID,
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "stream_id",
            _Text(str(STREAM_ID)),
            IngestionConsumerBindingError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "next_sequence",
            True,
            IngestionBatchIntegrityError,
        ),
        (
            "UPDATE matrix_sync_consumers",
            "next_sequence",
            2,
            IngestionBatchIntegrityError,
        ),
        (
            "INSERT INTO room_membership",
            "membership_epoch",
            True,
            IngestionBatchIntegrityError,
        ),
        (
            "INSERT INTO room_membership",
            "membership_epoch",
            1,
            IngestionBatchValidationError,
        ),
    ),
)
async def test_admit_ingestion_batch_fresh_unit_rejects_invalid_returned_row_and_rolls_back(
    journal_database: Callable[[], EventJournalStore],
    fragment: str,
    field_name: str,
    value: object,
    error: type[Exception],
) -> None:
    store = journal_database()
    await _bound_principal(store)
    backend = _ObservedBackend(
        store.backend,
        lambda transaction: _MutatingReturnTransaction(
            transaction,
            statement_fragment=fragment,
            field_name=field_name,
            value=value,
        ),
    )
    principal = EventJournalStore(backend=backend).principal(ACCOUNT_ID)

    with pytest.raises(error):
        await principal.admit_ingestion_batch(_expected_admission())

    assert await _graph(store) == _old_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize("different_content", (False, True), ids=("same", "different"))
async def test_admit_ingestion_batch_same_id_collision_rolls_back(
    journal_database: Callable[[], EventJournalStore],
    different_content: bool,
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    admission = _expected_admission()
    event = admission.event
    projected = admission.projected
    if different_content:
        source = dict(event.source)
        source["content"] = {"body": "different", "msgtype": "m.text"}
        event = replace(event, source=source)
        assert projected is not None
        projected = replace(projected, content=source["content"])
    assert await principal.admit(event, projected) is AdmissionResult.ADMITTED
    before = await _graph(store)

    with pytest.raises(IngestionBatchIntegrityError):
        await principal.admit_ingestion_batch(admission)

    assert await _graph(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "fragment"),
    (
        ("cas", "UPDATE matrix_sync_consumers"),
        ("event", "INSERT INTO journal_events"),
        ("projection", "DELETE FROM unresolved_edits"),
        ("receipt", "INSERT INTO matrix_ingestion_receipts"),
    ),
)
async def test_admit_ingestion_batch_rolls_back_each_injected_boundary(
    journal_database: Callable[[], EventJournalStore],
    boundary: str,
    fragment: str,
) -> None:
    store = journal_database()
    await _bound_principal(store)
    backend = _ObservedBackend(
        store.backend,
        lambda transaction: _ObservedTransaction(
            transaction,
            statement_matches=lambda sql: fragment in sql,
            after_statement=_raise_injected_boundary,
        ),
    )
    principal = EventJournalStore(backend=backend).principal(ACCOUNT_ID)

    with pytest.raises(_InjectedBoundaryError):
        await principal.admit_ingestion_batch(_expected_admission())

    assert await _graph(store) == _old_graph(), boundary


@pytest.mark.asyncio
async def test_admit_ingestion_batch_injected_boundary_after_commit_leaves_new_graph(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    await _bound_principal(store)
    backend = DiesAfterNextWriteCommit(store.backend, armed=True)
    principal = EventJournalStore(backend=backend).principal(ACCOUNT_ID)

    with pytest.raises(CrashError, match="crashed the instant"):
        await principal.admit_ingestion_batch(_expected_admission())

    assert backend.commits == 1
    reopened = journal_database()
    assert await _graph(reopened) == _fresh_graph()


@pytest.mark.asyncio
async def test_admit_ingestion_batch_hides_enum_until_commit(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    await _bound_principal(store)
    inside_transaction = threading.Event()
    release_transaction = threading.Event()

    def hold_before_commit() -> None:
        inside_transaction.set()
        assert release_transaction.wait(
            20
        ), "the receipt transaction was never released"

    backend = _ObservedBackend(
        store.backend,
        lambda transaction: _ObservedTransaction(
            transaction,
            statement_matches=lambda sql: "INSERT INTO matrix_ingestion_receipts"
            in sql,
            after_statement=hold_before_commit,
        ),
    )
    principal = EventJournalStore(backend=backend).principal(ACCOUNT_ID)
    admitting = asyncio.create_task(
        principal.admit_ingestion_batch(_expected_admission())
    )
    try:
        assert await asyncio.to_thread(
            inside_transaction.wait, 20
        ), "admission never reached the pre-commit receipt boundary"
        assert not admitting.done()
        assert await _graph(store) == _old_graph()
    finally:
        release_transaction.set()
        with suppress(Exception):
            await admitting
    assert admitting.result() is AdmissionResult.ADMITTED
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
async def test_fresh_sql_starts_with_cas_without_consumer_preread(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    await _bound_principal(store)
    trace: list[str] = []
    backend = _ObservedBackend(
        store.backend,
        lambda transaction: _ObservedTransaction(transaction, trace=trace),
    )

    result = (
        await EventJournalStore(backend=backend)
        .principal(ACCOUNT_ID)
        .admit_ingestion_batch(_expected_admission())
    )

    assert result is AdmissionResult.ADMITTED
    assert trace[0].startswith("UPDATE matrix_sync_consumers SET next_sequence")


_POSTGRES_WAIT_SECONDS = 20.0
_POSTGRES_LOCK_QUERY = """
    SELECT count(*) FROM pg_stat_activity
    WHERE application_name = %s AND wait_event_type = 'Lock'
"""


@dataclass(frozen=True, slots=True)
class _PostgresIngestionStores:
    first: EventJournalStore
    second: EventJournalStore
    database_url: str
    application_name: str


@pytest_asyncio.fixture
async def postgres_ingestion_stores(
    postgres_journal_url: str,
) -> AsyncGenerator[_PostgresIngestionStores, None]:
    database_url = postgres_journal_schema_url(postgres_journal_url)
    application_name = f"mindroom-ingestion-race-{uuid4().hex}"
    racer_url = f"{database_url}&application_name={application_name}"
    first = EventJournalStore.open_postgres(racer_url)
    second = EventJournalStore.open_postgres(racer_url)
    try:
        yield _PostgresIngestionStores(
            first,
            second,
            database_url,
            application_name,
        )
    finally:
        await first.close()
        await second.close()


def _wait_for_postgres_lock(database_url: str, application_name: str) -> None:
    import psycopg  # noqa: PLC0415 - exercised only by the PostgreSQL fixture

    deadline = time.monotonic() + _POSTGRES_WAIT_SECONDS
    with psycopg.connect(database_url, autocommit=True) as connection:
        while True:
            row = connection.execute(
                _POSTGRES_LOCK_QUERY,
                (application_name,),
            ).fetchone()
            if row is not None and int(row[0]) > 0:
                return
            if time.monotonic() >= deadline:
                raise AssertionError("the competing PostgreSQL writer never waited")
            time.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    ("ingestion_first", "fence_first"),
    ids=("ingestion-first", "fence-first"),
)
async def test_postgres_membership_race_fences_or_cleans_up(
    postgres_ingestion_stores: _PostgresIngestionStores,
    order: str,
) -> None:
    stores = postgres_ingestion_stores
    await _bound_principal(stores.first)
    lock_held = threading.Event()
    wait_observed = threading.Event()

    def hold_membership_lock() -> None:
        lock_held.set()
        _wait_for_postgres_lock(stores.database_url, stores.application_name)
        wait_observed.set()

    held_backend = _ObservedBackend(
        stores.first.backend,
        lambda transaction: _ObservedTransaction(
            transaction,
            statement_matches=lambda sql: "INSERT INTO room_membership" in sql,
            after_statement=hold_membership_lock,
        ),
    )
    held = EventJournalStore(backend=held_backend).principal(ACCOUNT_ID)
    other = stores.second.principal(ACCOUNT_ID)

    if order == "ingestion_first":
        admission = asyncio.create_task(
            held.admit_ingestion_batch(_expected_admission())
        )
        assert await asyncio.to_thread(
            lock_held.wait, _POSTGRES_WAIT_SECONDS
        ), "ingestion never locked membership"
        fence = asyncio.create_task(
            other.fence_departure(ROOM_ID, source=DepartureSource.REPORTED)
        )
        result, outcome = await asyncio.gather(admission, fence)
        assert result is AdmissionResult.ADMITTED
        assert outcome.membership_epoch == 1
        graph = await _graph(stores.first)
        assert graph == _fenced_fresh_graph()
    else:
        fence = asyncio.create_task(
            held.fence_departure(ROOM_ID, source=DepartureSource.REPORTED)
        )
        assert await asyncio.to_thread(
            lock_held.wait, _POSTGRES_WAIT_SECONDS
        ), "fence never locked membership"
        try:
            admission = asyncio.create_task(
                other.admit_ingestion_batch(_expected_admission())
            )
        except BaseException:
            with suppress(Exception):
                await fence
            raise
        with pytest.raises(IngestionBatchValidationError):
            await admission
        outcome = await fence
        assert outcome.membership_epoch == 1
        graph = await _graph(stores.first)
        assert graph == _fence_only_graph()

    assert wait_observed.is_set(), "the competing PostgreSQL writer did not wait"


@pytest.mark.asyncio
async def test_immediate_exact_receipt_replay_is_duplicate(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    admission = _expected_admission()
    assert await principal.admit_ingestion_batch(admission) is AdmissionResult.ADMITTED
    trace: list[str] = []
    replay = EventJournalStore(
        backend=_ObservedBackend(
            store.backend,
            lambda transaction: _ObservedTransaction(transaction, trace=trace),
        )
    ).principal(ACCOUNT_ID)

    assert await replay.admit_ingestion_batch(admission) is AdmissionResult.DUPLICATE

    _assert_classifier_trace(trace)
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize("disagreement", ("digest", "event", "missing"))
async def test_immediate_receipt_disagreement_is_integrity_error(
    journal_database: Callable[[], EventJournalStore],
    disagreement: str,
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    assert (
        await principal.admit_ingestion_batch(_expected_admission())
        is AdmissionResult.ADMITTED
    )
    trace: list[str] = []

    def wrap(transaction: Transaction) -> Transaction:
        inner = transaction
        if disagreement == "missing":
            for field_name in ("r_schema", "r_sha256", "r_event_id"):
                inner = _MutatingReturnTransaction(
                    inner,
                    statement_fragment="LEFT JOIN matrix_ingestion_receipts",
                    field_name=field_name,
                    value=None,
                )
        return _ObservedTransaction(inner, trace=trace)

    replay = EventJournalStore(backend=_ObservedBackend(store.backend, wrap)).principal(
        ACCOUNT_ID
    )
    admission = _expected_admission()
    if disagreement == "digest":
        admission = replace(admission, sha256=b"x" * 32)
    elif disagreement == "event":
        admission = _admission(event_id="$other")

    with pytest.raises(IngestionBatchIntegrityError):
        await replay.admit_ingestion_batch(admission)

    _assert_classifier_trace(trace)
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    (
        (
            "c_generation",
            _Text(str(CONSUMER_GENERATION)),
            IngestionConsumerBindingError,
        ),
        ("c_generation", CONSUMER_GENERATION, IngestionConsumerBindingError),
        ("c_stream", _Text(str(STREAM_ID)), IngestionConsumerBindingError),
        ("c_next", True, IngestionBatchIntegrityError),
        ("c_next", -1, IngestionBatchIntegrityError),
        ("c_next", 2**63, IngestionBatchIntegrityError),
        ("r_schema", True, IngestionBatchIntegrityError),
        ("r_schema", 2, IngestionBatchIntegrityError),
        ("r_sha256", _Text(GOLDEN_SHA256), IngestionBatchIntegrityError),
        ("r_sha256", GOLDEN_SHA256.upper(), IngestionBatchIntegrityError),
        ("r_event_id", _Text(EVENT_ID), IngestionBatchIntegrityError),
    ),
)
async def test_immediate_receipt_disagreement_rejects_invalid_classifier_alias_type(
    journal_database: Callable[[], EventJournalStore],
    field_name: str,
    value: object,
    error: type[Exception],
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    admission = _expected_admission()
    assert await principal.admit_ingestion_batch(admission) is AdmissionResult.ADMITTED
    trace: list[str] = []
    classified = EventJournalStore(
        backend=_ObservedBackend(
            store.backend,
            lambda transaction: _ObservedTransaction(
                _MutatingReturnTransaction(
                    transaction,
                    statement_fragment="LEFT JOIN matrix_ingestion_receipts",
                    field_name=field_name,
                    value=value,
                ),
                trace=trace,
            ),
        )
    ).principal(ACCOUNT_ID)

    with pytest.raises(error):
        await classified.admit_ingestion_batch(admission)

    _assert_classifier_trace(trace)
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "error"),
    (
        ("missing", IngestionConsumerBindingError),
        ("inactive", IngestionConsumerBindingError),
        ("generation", IngestionConsumerBindingError),
        ("stream", IngestionConsumerBindingError),
        ("older", IngestionBatchSequenceError),
        ("future", IngestionBatchSequenceError),
        ("current-no-row", IngestionBatchIntegrityError),
    ),
)
async def test_fifo_and_binding_taxonomy(
    journal_database: Callable[[], EventJournalStore],
    case: str,
    error: type[Exception],
) -> None:
    store = journal_database()
    principal = store.principal(ACCOUNT_ID)
    admission = _expected_admission()
    if case == "inactive":
        await principal.load_or_create_ingestion_consumer(
            new_generation=CONSUMER_GENERATION
        )
    elif case != "missing":
        principal = await _bound_principal(store)
    if case == "generation":
        admission = replace(admission, consumer_generation=UUID(int=9))
    elif case == "stream":
        admission = replace(admission, stream_id=UUID(int=9))
    elif case == "older":
        assert (
            await principal.admit_ingestion_batch(admission) is AdmissionResult.ADMITTED
        )
        assert (
            await principal.admit_ingestion_batch(
                _admission(sequence=1, event_id="$second", sha256=b"2" * 32)
            )
            is AdmissionResult.ADMITTED
        )
    elif case == "future":
        admission = _admission(sequence=1, event_id="$future", sha256=b"3" * 32)

    before = await _graph(store)
    trace: list[str] = []

    def wrap(transaction: Transaction) -> Transaction:
        inner = (
            _NoClaimTransaction(transaction)
            if case == "current-no-row"
            else transaction
        )
        return _ObservedTransaction(inner, trace=trace)

    classified = EventJournalStore(
        backend=_ObservedBackend(store.backend, wrap)
    ).principal(ACCOUNT_ID)
    with pytest.raises(error):
        await classified.admit_ingestion_batch(admission)

    _assert_classifier_trace(trace)
    assert await _graph(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting", (False, True), ids=("identical", "conflicting"))
async def test_independent_stores_same_and_conflicting_races(
    journal_database: Callable[[], EventJournalStore],
    conflicting: bool,
) -> None:
    stores = (journal_database(), journal_database())
    await _bound_principal(stores[0])
    traces: tuple[list[str], list[str]] = ([], [])
    principals = tuple(
        EventJournalStore(
            backend=_ObservedBackend(
                store.backend,
                lambda transaction, trace=trace: _ObservedTransaction(
                    transaction,
                    trace=trace,
                ),
            )
        ).principal(ACCOUNT_ID)
        for store, trace in zip(stores, traces, strict=True)
    )
    admissions = (
        _expected_admission(),
        (
            replace(_expected_admission(), sha256=b"x" * 32)
            if conflicting
            else _expected_admission()
        ),
    )

    results = await asyncio.gather(
        *(
            principal.admit_ingestion_batch(admission)
            for principal, admission in zip(principals, admissions, strict=True)
        ),
        return_exceptions=True,
    )

    winners = [
        index
        for index, result in enumerate(results)
        if result is AdmissionResult.ADMITTED
    ]
    assert len(winners) == 1
    winner = winners[0]
    loser = 1 - winner
    if conflicting:
        assert type(results[loser]) is IngestionBatchIntegrityError
    else:
        assert results[loser] is AdmissionResult.DUPLICATE
    _assert_classifier_trace(traces[loser])
    expected = _fresh_graph()
    expected["receipts"] = (
        (
            ACCOUNT_ID,
            str(CONSUMER_GENERATION),
            str(STREAM_ID),
            0,
            1,
            admissions[winner].sha256.hex(),
            EVENT_ID,
        ),
    )
    assert await _graph(stores[0]) == expected


@pytest.mark.asyncio
async def test_concurrent_generic_same_id_rolls_back_ingestion(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    stores = (journal_database(), journal_database())
    await _bound_principal(stores[0])
    projection_written = threading.Event()
    release_generic = threading.Event()

    def hold_generic() -> None:
        projection_written.set()
        assert release_generic.wait(20), "the generic transaction was never released"

    held = EventJournalStore(
        backend=_ObservedBackend(
            stores[0].backend,
            lambda transaction: _ObservedTransaction(
                transaction,
                statement_matches=lambda sql: "DELETE FROM unresolved_edits" in sql,
                after_statement=hold_generic,
            ),
        )
    ).principal(ACCOUNT_ID)
    ingestion_entered = asyncio.Event()
    contender = EventJournalStore(
        backend=_WriteEntryBackend(stores[1].backend, ingestion_entered)
    ).principal(ACCOUNT_ID)
    generic: asyncio.Task[AdmissionResult] | None = None
    ingestion: asyncio.Task[AdmissionResult] | None = None
    try:
        admission = _expected_admission()
        generic = asyncio.create_task(held.admit(admission.event, admission.projected))
        assert await asyncio.to_thread(
            projection_written.wait, 20
        ), "generic admission never completed its projection"
        ingestion = asyncio.create_task(contender.admit_ingestion_batch(admission))
        await asyncio.wait_for(ingestion_entered.wait(), 20)
        assert not ingestion.done()
        release_generic.set()
        assert await generic is AdmissionResult.ADMITTED
        with pytest.raises(IngestionBatchIntegrityError):
            await ingestion
    finally:
        release_generic.set()
        for task in (generic, ingestion):
            if task is not None:
                with suppress(BaseException):
                    await task

    assert await _graph(stores[0]) == _generic_graph()


@pytest.mark.asyncio
async def test_postgres_loser_waits_then_classifies(
    postgres_ingestion_stores: _PostgresIngestionStores,
) -> None:
    stores = postgres_ingestion_stores
    await _bound_principal(stores.first)
    receipt_written = threading.Event()
    release_winner = threading.Event()
    loser_trace: list[str] = []

    def hold_receipt() -> None:
        receipt_written.set()
        assert release_winner.wait(
            20
        ), "the winning receipt transaction was never released"

    winner = EventJournalStore(
        backend=_ObservedBackend(
            stores.first.backend,
            lambda transaction: _ObservedTransaction(
                transaction,
                statement_matches=lambda sql: "INSERT INTO matrix_ingestion_receipts"
                in sql,
                after_statement=hold_receipt,
            ),
        )
    ).principal(ACCOUNT_ID)
    loser = EventJournalStore(
        backend=_ObservedBackend(
            stores.second.backend,
            lambda transaction: _ObservedTransaction(transaction, trace=loser_trace),
        )
    ).principal(ACCOUNT_ID)
    winning: asyncio.Task[AdmissionResult] | None = None
    losing: asyncio.Task[AdmissionResult] | None = None
    try:
        admission = _expected_admission()
        winning = asyncio.create_task(winner.admit_ingestion_batch(admission))
        assert await asyncio.to_thread(
            receipt_written.wait, _POSTGRES_WAIT_SECONDS
        ), "winner never reached its receipt"
        losing = asyncio.create_task(loser.admit_ingestion_batch(admission))
        await asyncio.to_thread(
            _wait_for_postgres_lock,
            stores.database_url,
            stores.application_name,
        )
        assert not losing.done()
        release_winner.set()
        assert await winning is AdmissionResult.ADMITTED
        assert await losing is AdmissionResult.DUPLICATE
    finally:
        release_winner.set()
        for task in (winning, losing):
            if task is not None:
                with suppress(BaseException):
                    await task

    _assert_classifier_trace(loser_trace)
    assert await _graph(stores.first) == _fresh_graph()


@dataclass
class _AdapterSession:
    batch: SyncBatch | None
    ack_error: BaseException | None = None
    next_calls: list[dict[str, object]] = field(default_factory=list)
    ack_attempts: list[BatchRef] = field(default_factory=list)

    def next_batch(self, **limits: object) -> SyncBatch | None:
        self.next_calls.append(limits)
        return self.batch

    def acknowledge_batch(self, ref: BatchRef) -> None:
        self.ack_attempts.append(ref)
        if self.ack_error is not None:
            error, self.ack_error = self.ack_error, None
            raise error
        self.batch = None


@dataclass
class _AdapterAdmission:
    result: object = AdmissionResult.ADMITTED
    error: BaseException | None = None
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None
    calls: list[IngestionBatchAdmission] = field(default_factory=list)

    async def admit_ingestion_batch(
        self,
        admission: IngestionBatchAdmission,
    ) -> AdmissionResult:
        self.calls.append(admission)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_adapter_empty_session_is_noop() -> None:
    session = _AdapterSession(None)
    admission = _AdapterAdmission()

    assert (
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            admission,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
        is None
    )
    assert session.next_calls == [{"max_records": 1}]
    assert admission.calls == []
    assert session.ack_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("kind", RecordKind.STATE),
        ("source_json", b"{}"),
        (
            "source_json",
            b'{"content":{},"event_id":"$event","origin_server_ts":1000,'
            b'"sender":"@alice:example.org","type":"m.room.encrypted"}',
        ),
    ),
)
async def test_adapter_invalid_matrix_never_admits_or_acks(
    field_name: str,
    value: object,
) -> None:
    batch = _batch()
    _set(batch, "record", field_name, value)
    _resign(batch)
    session = _AdapterSession(batch)
    admission = _AdapterAdmission()

    with pytest.raises(IngestionBatchValidationError):
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            admission,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )

    assert admission.calls == []
    assert session.ack_attempts == []


@pytest.mark.asyncio
async def test_adapter_waits_for_committed_terminal_result(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    await _bound_principal(store)
    inside_transaction = threading.Event()
    release_transaction = threading.Event()

    def hold_receipt() -> None:
        inside_transaction.set()
        assert release_transaction.wait(20), "adapter never released the transaction"

    principal = EventJournalStore(
        backend=_ObservedBackend(
            store.backend,
            lambda transaction: _ObservedTransaction(
                transaction,
                statement_matches=lambda sql: "INSERT INTO matrix_ingestion_receipts"
                in sql,
                after_statement=hold_receipt,
            ),
        )
    ).principal(ACCOUNT_ID)
    batch = _batch()
    session = _AdapterSession(batch)
    consuming = asyncio.create_task(
        consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            principal,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
    )
    try:
        assert await asyncio.to_thread(
            inside_transaction.wait, 20
        ), "adapter admission never reached its receipt"
        assert not consuming.done()
        assert session.ack_attempts == []
        assert await _graph(store) == _old_graph()
    finally:
        release_transaction.set()
        with suppress(BaseException):
            await consuming

    assert consuming.result() is AdmissionResult.ADMITTED
    assert session.ack_attempts == [batch.ref]
    assert await _graph(store) == _fresh_graph()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", ("admitted", object()), ids=("equal-string", "unknown")
)
async def test_adapter_rejects_nonterminal_results_without_ack(result: object) -> None:
    session = _AdapterSession(_batch())
    admission = _AdapterAdmission(result=result)

    with pytest.raises(IngestionBatchIntegrityError):
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            admission,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )

    assert len(admission.calls) == 1
    assert session.ack_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", (AdmissionResult.ADMITTED, AdmissionResult.DUPLICATE)
)
async def test_adapter_acks_each_exact_terminal_enum_once(
    result: AdmissionResult,
) -> None:
    batch = _batch()
    session = _AdapterSession(batch)
    admission = _AdapterAdmission(result=result)

    returned = await consume_one_ingestion_batch(
        session,  # type: ignore[arg-type]
        admission,
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
    )

    assert returned is result
    assert admission.calls == [_expected_admission()]
    assert session.ack_attempts == [batch.ref]
    assert session.ack_attempts[0] is batch.ref


@pytest.mark.asyncio
async def test_adapter_backend_error_never_acks() -> None:
    error = RuntimeError("backend failed")
    session = _AdapterSession(_batch())
    admission = _AdapterAdmission(error=error)

    with pytest.raises(RuntimeError) as raised:
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            admission,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )

    assert raised.value is error
    assert session.ack_attempts == []


@pytest.mark.asyncio
async def test_adapter_cancellation_never_acks() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    session = _AdapterSession(_batch())
    admission = _AdapterAdmission(entered=entered, release=release)
    consuming = asyncio.create_task(
        consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            admission,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
    )
    await entered.wait()
    consuming.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consuming
    assert session.ack_attempts == []


@pytest.mark.asyncio
async def test_adapter_ack_failure_redelivers_exact_duplicate(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    store = journal_database()
    principal = await _bound_principal(store)
    batch = _batch()
    error = RuntimeError("ack failed")
    session = _AdapterSession(batch, ack_error=error)

    with pytest.raises(RuntimeError) as raised:
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            principal,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
    assert raised.value is error
    assert session.batch is batch
    assert await _graph(store) == _fresh_graph()

    assert (
        await consume_one_ingestion_batch(
            session,  # type: ignore[arg-type]
            principal,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
        is AdmissionResult.DUPLICATE
    )
    assert session.next_calls == [{"max_records": 1}, {"max_records": 1}]
    assert session.ack_attempts == [batch.ref, batch.ref]
    assert all(ref is batch.ref for ref in session.ack_attempts)
    assert session.batch is None
    assert await _graph(store) == _fresh_graph()


@dataclass(frozen=True, slots=True)
class _SeededReadyBatch:
    batch: SyncBatch
    owner: OwnerView


@dataclass(frozen=True, slots=True)
class _AdmissionPostCrashState:
    source: ClassicSourceConfig
    nio_root: Path
    mindroom_path: Path
    seeded: _SeededReadyBatch
    source_at_s0: SourceState
    work_rows: tuple[tuple[object, ...], ...]
    delivery_frontier: tuple[tuple[object, ...], ...]
    expected_mindroom_graph: dict[str, tuple[tuple[object, ...], ...]]


def _nio_delivery_graph(
    database_path: Path,
) -> tuple[
    tuple[tuple[object, ...], ...],
    tuple[tuple[object, ...], ...],
]:
    with sqlite3.connect(database_path) as connection:
        work_rows = connection.execute(
            "SELECT * FROM NioIngestWork "
            "ORDER BY ready_revision, ready_ordinal, work_id",
        ).fetchall()
        frontier = connection.execute(
            "SELECT delivery_next_sequence, delivery_acknowledged_sha256, "
            "delivery_outstanding_work_id, delivery_outstanding_ready_revision, "
            "delivery_outstanding_ready_ordinal, delivery_outstanding_batch_sha256 "
            "FROM NioIngestMeta LIMIT 2",
        ).fetchall()
    return tuple(tuple(row) for row in work_rows), tuple(tuple(row) for row in frontier)


def _seed_ready_batch(
    journal: SqliteIngestionJournal,
    source: ClassicSourceConfig,
) -> _SeededReadyBatch:
    owner = journal.load_owner()
    prior = journal.load_source()
    adapter = ClassicSource(owner.stream_id, source, ACCOUNT_ID)
    request = adapter.plan_request(prior, prior.next_request_id)
    assert request is not None
    response_body = b'{"next_batch":"s0","rooms":{}}'
    normalized = adapter.normalize(
        request,
        NetworkResult(
            request.stream_id,
            request.transport,
            request.source_epoch,
            request.request_id,
            200,
            response_body,
            None,
            None,
        ),
    )
    assert normalized.frame is not None
    staged = StagedFrame(
        normalized.frame.frame_id,
        StagedSourceResponse(
            request,
            normalized.response_body,
            normalized.frame.source_sha256,
        ),
    )
    staged_result = journal.stage_source_response(
        source=SourceState(
            prior.source_epoch,
            prior.transport_kind,
            normalized.frame.candidate_cursor_json,
            request.request_id + 1,
            prior.active,
        ),
        frame=staged,
    )
    materialized = journal.materialize_oldest_frame(limits=MaterializerLimits())
    assert materialized.status is MaterializeStatus.MATERIALIZED
    assert materialized.frame_id == staged.frame_id
    assert materialized.revision == staged_result.revision + 1
    assert journal.load_source() == SourceState(
        prior.source_epoch,
        TransportKind.CLASSIC,
        canonical_classic_cursor(ClassicCursor("s0")),
        request.request_id + 1,
        prior.active,
    )
    assert journal.list_frames(2) == ()

    owner = journal.load_owner()
    record = replace(_record(), event_id=None)
    clear = (
        record.record_id,
        "event",
        "ready",
        str(UUID(int=10_000 + record.origin.frame_index)),
        record.room_id,
        record.membership_epoch,
        record.room_sequence,
        owner.revision,
        0,
        owner.revision,
    )
    payload, digest = journal._payload(
        owner,
        "NioIngestWork",
        _canonical_work_plaintext("event", record),
        header=_canonical_internal(clear),
    )
    with journal._owner.journal_write():
        journal._execute(
            "INSERT INTO NioIngestWork VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ACCOUNT_ID, *clear, payload, digest),
        )

    batch = journal.next_batch(max_records=1)
    assert batch is not None
    expected = batch_from_records(
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        consumer_generation=CONSUMER_GENERATION,
        stream_id=owner.stream_id,
        sequence=0,
        created_revision=owner.revision,
        records=(record,),
    )
    assert batch == expected
    return _SeededReadyBatch(batch, journal.load_owner())


def _expected_mindroom_graph(
    seeded: _SeededReadyBatch,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    graph = _fresh_graph()
    graph["consumers"] = (
        (ACCOUNT_ID, str(CONSUMER_GENERATION), str(seeded.owner.stream_id), 1),
    )
    graph["receipts"] = (
        (
            ACCOUNT_ID,
            str(CONSUMER_GENERATION),
            str(seeded.owner.stream_id),
            0,
            1,
            seeded.batch.ref.sha256.hex(),
            EVENT_ID,
        ),
    )
    return graph


async def _prepare_admission_post_crash(
    tmp_path: Path,
) -> _AdmissionPostCrashState:
    source = ClassicSourceConfig(timeout_ms=30_000, filter_json=b"{}")
    nio_root = tmp_path / "nio"
    bootstrap = open_ingestion_store(
        nio_root,
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        consumer_generation=CONSUMER_GENERATION,
        source=source,
        database_name="journal.db",
    )
    mindroom: EventJournalStore | None = None
    try:
        seeded = _seed_ready_batch(bootstrap._journal, source)
        source_at_s0 = bootstrap._journal.load_source()
        work_rows, delivery_frontier = _nio_delivery_graph(nio_root / "journal.db")
        assert len(work_rows) == 1
        assert delivery_frontier == (
            (
                1,
                None,
                seeded.batch.records[0].record_id,
                seeded.batch.created_revision,
                0,
                seeded.batch.ref.sha256,
            ),
        )
        mindroom_path = tmp_path / "event_journal.db"
        mindroom = EventJournalStore.open_sqlite(mindroom_path)
        principal = mindroom.principal(ACCOUNT_ID)
        await principal.load_or_create_ingestion_consumer(
            new_generation=CONSUMER_GENERATION,
        )
        await principal.bind_ingestion_stream(
            generation=CONSUMER_GENERATION,
            stream_id=seeded.owner.stream_id,
        )
        admission = validate_ingestion_batch(
            seeded.batch,
            account_id=ACCOUNT_ID,
            device_id=DEVICE_ID,
        )
        assert (
            await principal.admit_ingestion_batch(admission) is AdmissionResult.ADMITTED
        )
        expected = _expected_mindroom_graph(seeded)
        assert await _graph(mindroom) == expected
        return _AdmissionPostCrashState(
            source,
            nio_root,
            mindroom_path,
            seeded,
            source_at_s0,
            work_rows,
            delivery_frontier,
            expected,
        )
    finally:
        try:
            bootstrap.close()
        finally:
            if mindroom is not None:
                await mindroom.close()


def _assert_source_requests(
    request_calls: list[tuple[nio.AsyncClient, tuple[object, ...], dict[str, object]]],
    client: nio.AsyncClient,
    expected_paths: tuple[str, ...],
) -> None:
    assert len(request_calls) == len(expected_paths)
    for call, expected_path in zip(request_calls, expected_paths, strict=True):
        selected_client, args, kwargs = call
        assert selected_client is client
        assert args == (
            "GET",
            expected_path,
            None,
            {"Authorization": "Bearer access-token"},
            None,
            30.0,
        )
        assert kwargs == {}


class _AdmissionResponseContent:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.position = 0

    async def read(self, size: int = -1) -> bytes:
        end = len(self.body) if size < 0 else self.position + size
        chunk = self.body[self.position : end]
        self.position += len(chunk)
        return chunk


class _AdmissionResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _AdmissionResponseContent(body)
        self.headers: dict[str, str] = {}
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


@dataclass(slots=True)
class _AdmissionSchedulingProxy:
    principal: PrincipalStore
    admission_entered: asyncio.Event
    allow_duplicate_admission: asyncio.Event

    async def admit_ingestion_batch(
        self,
        admission: IngestionBatchAdmission,
    ) -> AdmissionResult:
        self.admission_entered.set()
        await self.allow_duplicate_admission.wait()
        return await self.principal.admit_ingestion_batch(admission)


_EMPTY_FRAME_STAGED_TRANSITIONS = (
    "frame_collision_probe",
    "meta_revision_epoch_cas",
    "source_state_upsert",
    "frame_insert",
    "commit",
)
_EMPTY_FRAME_RETIRED_TRANSITIONS = (
    *_EMPTY_FRAME_STAGED_TRANSITIONS,
    "meta_revision_epoch_cas",
    "frame_delete",
    "before_commit",
    "commit",
)
_EMPTY_FRAME_ACKED_TRANSITIONS = (
    *_EMPTY_FRAME_RETIRED_TRANSITIONS,
    "delivery_work_delete",
    "delivery_ack_meta_cas",
    "before_commit",
    "commit",
)


async def _assert_blocked_empty_frame_barrier(
    session: nio.IngestionSession,
    state: _AdmissionPostCrashState,
    reopened_mindroom: EventJournalStore,
    response: _AdmissionResponse,
    expected_source: SourceState,
    transitions: list[str],
    request_calls: list[tuple[nio.AsyncClient, tuple[object, ...], dict[str, object]]],
    client: nio.AsyncClient,
) -> None:
    assert response.release_calls == 1
    assert session._journal.load_source() == expected_source
    frames = session._journal.list_frames(2)
    assert len(frames) == 1
    assert frames[0].response.response_body == response.content.body
    assert _nio_delivery_graph(state.nio_root / "journal.db") == (
        state.work_rows,
        state.delivery_frontier,
    )
    assert await _graph(reopened_mindroom) == state.expected_mindroom_graph
    assert tuple(transitions) == _EMPTY_FRAME_STAGED_TRANSITIONS
    _assert_source_requests(
        request_calls,
        client,
        ("/_matrix/client/v3/sync?since=s0&timeout=30000&filter=%7B%7D",),
    )


async def _assert_retired_empty_frame_barrier(
    session: nio.IngestionSession,
    state: _AdmissionPostCrashState,
    reopened_mindroom: EventJournalStore,
    response: _AdmissionResponse,
    expected_source: SourceState,
    transitions: list[str],
    request_calls: list[tuple[nio.AsyncClient, tuple[object, ...], dict[str, object]]],
    client: nio.AsyncClient,
) -> None:
    assert response.release_calls == 1
    assert session._journal.load_source() == expected_source
    assert session._journal.list_frames(2) == ()
    assert _nio_delivery_graph(state.nio_root / "journal.db") == (
        state.work_rows,
        state.delivery_frontier,
    )
    assert await _graph(reopened_mindroom) == state.expected_mindroom_graph
    assert tuple(transitions) == _EMPTY_FRAME_RETIRED_TRANSITIONS
    _assert_source_requests(
        request_calls,
        client,
        (
            "/_matrix/client/v3/sync?since=s0&timeout=30000&filter=%7B%7D",
            "/_matrix/client/v3/sync?since=s1&timeout=30000&filter=%7B%7D",
        ),
    )


class _SourceRequestSchedule:
    def __init__(self) -> None:
        self.calls: list[
            tuple[nio.AsyncClient, tuple[object, ...], dict[str, object]]
        ] = []
        self.first_response = _AdmissionResponse(
            200,
            b'{"next_batch":"s1","rooms":{}}',
        )
        self.second_entered = asyncio.Event()
        self.second_cancelled = asyncio.Event()

    async def send(
        self,
        selected_client: nio.AsyncClient,
        *args: object,
        **kwargs: object,
    ) -> _AdmissionResponse:
        self.calls.append((selected_client, args, kwargs))
        if len(self.calls) == 1:
            return self.first_response
        if len(self.calls) != 2:
            raise AssertionError
        self.second_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.second_cancelled.set()
            raise
        raise AssertionError


async def _source_run_blocked(
    run_task: asyncio.Task[None],
    second_request_waiter: asyncio.Task[bool],
) -> bool:
    async with asyncio.timeout(5):
        completed, _pending = await asyncio.wait(
            {run_task, second_request_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
    if run_task not in completed:
        assert second_request_waiter in completed
        return False
    with pytest.raises(nio.IngestionBlockedError):
        await run_task
    return True


async def _finish_duplicate_ack(
    allow_duplicate_admission: asyncio.Event,
    consume_task: asyncio.Task[AdmissionResult],
    run_task: asyncio.Task[None],
    transitions: list[str],
    state: _AdmissionPostCrashState,
    reopened_mindroom: EventJournalStore,
) -> None:
    allow_duplicate_admission.set()
    async with asyncio.timeout(5):
        assert await consume_task is AdmissionResult.DUPLICATE
    assert not run_task.done()
    assert tuple(transitions) == _EMPTY_FRAME_ACKED_TRANSITIONS
    await _assert_reopened_delivery_state(state, reopened_mindroom)


async def _cancel_and_join(task: asyncio.Task[object] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def _exercise_concurrent_duplicate_ack(
    session: nio.IngestionSession,
    principal: PrincipalStore,
    client: nio.AsyncClient,
    state: _AdmissionPostCrashState,
    reopened_mindroom: EventJournalStore,
    transitions: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission_entered = asyncio.Event()
    allow_duplicate_admission = asyncio.Event()
    schedule = _SourceRequestSchedule()
    expected_source_s1 = replace(
        state.source_at_s0,
        cursor_json=canonical_classic_cursor(ClassicCursor("s1")),
        next_request_id=state.source_at_s0.next_request_id + 1,
    )
    scheduling_proxy = _AdmissionSchedulingProxy(
        principal,
        admission_entered,
        allow_duplicate_admission,
    )

    async def scheduled_send(
        selected_client: nio.AsyncClient,
        *args: object,
        **kwargs: object,
    ) -> _AdmissionResponse:
        return await schedule.send(selected_client, *args, **kwargs)

    monkeypatch.setattr(nio.AsyncClient, "send", scheduled_send)
    assert session._client is client
    run_task: asyncio.Task[None] | None = None
    async with session:
        consume_task = asyncio.create_task(
            consume_one_ingestion_batch(
                session,
                scheduling_proxy,
                account_id=ACCOUNT_ID,
                device_id=DEVICE_ID,
            ),
        )
        second_request_waiter: asyncio.Task[bool] | None = None
        try:
            async with asyncio.timeout(5):
                await admission_entered.wait()
            assert not consume_task.done()

            run_task = asyncio.create_task(session.run())
            second_request_waiter = asyncio.create_task(schedule.second_entered.wait())
            if await _source_run_blocked(run_task, second_request_waiter):
                await _assert_blocked_empty_frame_barrier(
                    session,
                    state,
                    reopened_mindroom,
                    schedule.first_response,
                    expected_source_s1,
                    transitions,
                    schedule.calls,
                    client,
                )
                assert admission_entered.is_set()
                assert not consume_task.done()
                pytest.fail(
                    "Task7 empty Classic frame blocked before ACK scheduling barrier",
                )
            assert schedule.second_entered.is_set()
            assert not run_task.done()
            await _assert_retired_empty_frame_barrier(
                session,
                state,
                reopened_mindroom,
                schedule.first_response,
                expected_source_s1,
                transitions,
                schedule.calls,
                client,
            )
            assert admission_entered.is_set()
            assert not consume_task.done()
            await _finish_duplicate_ack(
                allow_duplicate_admission,
                consume_task,
                run_task,
                transitions,
                state,
                reopened_mindroom,
            )
        finally:
            await _cancel_and_join(second_request_waiter)
            await _cancel_and_join(consume_task)
    assert run_task is not None
    assert run_task.cancelled()
    assert schedule.second_cancelled.is_set()


async def _assert_reopened_delivery_state(
    state: _AdmissionPostCrashState,
    reopened_mindroom: EventJournalStore,
) -> None:
    work_rows, frontier = _nio_delivery_graph(state.nio_root / "journal.db")
    assert work_rows == ()
    assert frontier == ((1, state.seeded.batch.ref.sha256, None, None, None, None),)
    assert await _graph(reopened_mindroom) == state.expected_mindroom_graph


async def _replay_concurrently_after_reopen(
    state: _AdmissionPostCrashState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[str] = []
    reopened_bootstrap = open_ingestion_store(
        state.nio_root,
        account_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        consumer_generation=CONSUMER_GENERATION,
        source=state.source,
        database_name="journal.db",
        transition_statement_hook=transitions.append,
    )
    reopened_mindroom: EventJournalStore | None = None
    session: nio.IngestionSession | None = None
    client: nio.AsyncClient | None = None
    try:
        reopened_owner = reopened_bootstrap._journal.load_owner()
        assert reopened_owner == replace(
            state.seeded.owner,
            writer_epoch=reopened_owner.writer_epoch,
        )
        assert reopened_owner.writer_epoch != state.seeded.owner.writer_epoch
        assert reopened_bootstrap._journal.load_source() == state.source_at_s0
        assert reopened_bootstrap._journal.list_frames(2) == ()
        assert _nio_delivery_graph(state.nio_root / "journal.db") == (
            state.work_rows,
            state.delivery_frontier,
        )
        reopened_mindroom = EventJournalStore.open_sqlite(state.mindroom_path)
        assert await _graph(reopened_mindroom) == state.expected_mindroom_graph
        client = _authenticated_runner_client()
        session = nio.open_ingestion(
            client,
            reopened_bootstrap,
            config=IngestionConfig(state.source),
            consumer_generation=CONSUMER_GENERATION,
            stream_id=state.seeded.owner.stream_id,
            room_id=ROOM_ID,
        )
        transitions.clear()
        await _exercise_concurrent_duplicate_ack(
            session,
            reopened_mindroom.principal(ACCOUNT_ID),
            client,
            state,
            reopened_mindroom,
            transitions,
            monkeypatch,
        )
    finally:
        try:
            if session is None:
                reopened_bootstrap.close()
            else:
                await session.close()
        finally:
            try:
                if client is not None:
                    await client.close()
            finally:
                if reopened_mindroom is not None:
                    await reopened_mindroom.close()


@pytest.mark.asyncio
async def test_adapter_replays_admission_post_crash_and_acks_after_nio_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay a committed admission while the reopened source runner is active."""
    state = await _prepare_admission_post_crash(tmp_path)
    await _replay_concurrently_after_reopen(state, monkeypatch)


@dataclass(slots=True)
class _RunnerMatrixSync:
    mode: str = "classic"


@dataclass(slots=True)
class _RunnerEventJournal:
    backend: str = "sqlite"


@dataclass(slots=True)
class _RunnerConfig:
    matrix_sync: _RunnerMatrixSync = field(default_factory=_RunnerMatrixSync)
    event_journal: _RunnerEventJournal = field(default_factory=_RunnerEventJournal)


@dataclass(frozen=True, slots=True)
class _RunnerPaths:
    storage_root: Path


@dataclass(slots=True)
class _RunnerDispatcher:
    trace: list[object]
    wake_calls: int = 0

    def wake(self) -> None:
        self.wake_calls += 1
        self.trace.append(("dispatcher-wake", self.wake_calls))


@dataclass(slots=True)
class _RunnerPrincipal:
    trace: list[object]
    generation: UUID = CANARY_GENERATION
    loaded_stream_id: UUID | None = None
    load_error: BaseException | None = None
    bind_error: BaseException | None = None
    bind_entered: asyncio.Event | None = None
    bind_release: asyncio.Event | None = None
    admissions: list[IngestionBatchAdmission] = field(default_factory=list)
    admission_results: list[AdmissionResult] = field(
        default_factory=lambda: [AdmissionResult.ADMITTED]
    )

    async def load_or_create_ingestion_consumer(
        self,
        *,
        new_generation: UUID,
    ) -> IngestionConsumer:
        self.trace.append(("load-consumer", new_generation))
        if self.load_error is not None:
            raise self.load_error
        return IngestionConsumer(self.generation, self.loaded_stream_id)

    async def bind_ingestion_stream(
        self,
        *,
        generation: UUID,
        stream_id: UUID,
    ) -> IngestionConsumer:
        self.trace.append(("bind-stream", generation, stream_id))
        if self.bind_entered is not None:
            self.bind_entered.set()
        if self.bind_release is not None:
            try:
                await self.bind_release.wait()
            except asyncio.CancelledError:
                self.trace.append("bind-cancelled")
                raise
        if self.bind_error is not None:
            raise self.bind_error
        return IngestionConsumer(generation, stream_id)

    async def admit_ingestion_batch(
        self,
        admission: IngestionBatchAdmission,
    ) -> AdmissionResult:
        self.admissions.append(admission)
        self.trace.append("principal-admit-committed")
        return self.admission_results.pop(0)


@dataclass(slots=True)
class _RunnerBot:
    runtime_paths: _RunnerPaths
    client: object
    config: _RunnerConfig
    approval_room_ids: frozenset[str]
    principal: _RunnerPrincipal
    _journal_dispatcher: _RunnerDispatcher
    trace: list[object]
    agent_name: str = CANARY_AGENT_NAME
    principal_error: BaseException | None = None

    def _journal_principal(self) -> _RunnerPrincipal:
        assert (self.runtime_paths.storage_root / "tracking" / "nio_ingestion").is_dir()
        self.trace.append("journal-principal")
        if self.principal_error is not None:
            raise self.principal_error
        return self.principal


@dataclass(slots=True)
class _RunnerBootstrap:
    trace: list[object]
    stream_id: UUID = STREAM_ID
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1
        self.trace.append("bootstrap-close")


@dataclass(slots=True)
class _RunnerSession:
    trace: list[object]
    bootstrap: _RunnerBootstrap
    run_body: Callable[[], Awaitable[None]]
    batches: list[SyncBatch | None] = field(default_factory=list)
    batch_error: BaseException | None = None
    next_calls: list[dict[str, object]] = field(default_factory=list)
    ack_attempts: list[BatchRef] = field(default_factory=list)
    enter_calls: int = 0
    exit_calls: int = 0

    def next_batch(self, **limits: object) -> SyncBatch | None:
        self.next_calls.append(limits)
        if self.batches:
            return self.batches.pop(0)
        if self.batch_error is not None:
            raise self.batch_error
        return None

    def acknowledge_batch(self, ref: BatchRef) -> None:
        self.ack_attempts.append(ref)
        self.trace.append(("session-ack", len(self.ack_attempts), ref))

    async def __aenter__(self) -> _RunnerSession:
        self.enter_calls += 1
        self.trace.append("session-enter")
        return self

    async def __aexit__(
        self,
        _error_type: object,
        _error: object,
        _traceback: object,
    ) -> bool:
        self.exit_calls += 1
        self.trace.append("session-exit")
        self.bootstrap.close()
        return False

    async def run(self) -> None:
        self.trace.append("session-run")
        try:
            await self.run_body()
        except asyncio.CancelledError:
            self.trace.append("session-run-cancelled")
            raise


@dataclass(slots=True)
class _RunnerHarness:
    bot: _RunnerBot
    principal: _RunnerPrincipal
    bootstrap: _RunnerBootstrap
    session: _RunnerSession
    store_calls: list[tuple[Path, dict[str, object]]]
    ingestion_calls: list[tuple[object, object, dict[str, object]]]
    trace: list[object]


@dataclass(slots=True)
class _LatchRecorder:
    offers: list[tuple[str, str, str]] = field(default_factory=list)

    def offer(self, phase: str, boundary: str, side: str) -> None:
        self.offers.append((phase, boundary, side))


class _RunnerFailure(RuntimeError):
    pass


def _authenticated_runner_client() -> nio.AsyncClient:
    client = client_session._MindRoomAsyncClient(
        "https://example.org",
        ACCOUNT_ID,
        device_id=DEVICE_ID,
    )
    client.user_id = ACCOUNT_ID
    client.device_id = DEVICE_ID
    client.access_token = "access-token"
    return client


def _runner_bot(
    storage_root: Path,
    *,
    trace: list[object] | None = None,
    principal: _RunnerPrincipal | None = None,
) -> _RunnerBot:
    observed = [] if trace is None else trace
    selected_principal = principal or _RunnerPrincipal(observed)
    return _RunnerBot(
        _RunnerPaths(storage_root),
        _authenticated_runner_client(),
        _RunnerConfig(),
        frozenset({CANARY_ROOM_ID}),
        selected_principal,
        _RunnerDispatcher(observed),
        observed,
    )


def _set_canary_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str | None = CANARY_AGENT_NAME,
    stop: str | None = None,
    trace: Path | str | None = None,
) -> None:
    values = {
        "MINDROOM_INGESTION_CANARY_AGENT": agent,
        "MINDROOM_INGESTION_CANARY_STOP": stop,
        "MINDROOM_INGESTION_CANARY_TRACE": None if trace is None else str(trace),
    }
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _canary_trace(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o600)
    return path


def _install_runner_harness(
    monkeypatch: pytest.MonkeyPatch,
    storage_root: Path,
    *,
    run_body: Callable[[], Awaitable[None]],
    principal_error: BaseException | None = None,
    loaded_stream_id: UUID | None = None,
    load_error: BaseException | None = None,
    store_error: BaseException | None = None,
    bind_error: BaseException | None = None,
    bind_entered: asyncio.Event | None = None,
    bind_release: asyncio.Event | None = None,
    open_error: BaseException | None = None,
    batches: list[SyncBatch | None] | None = None,
    batch_error: BaseException | None = None,
    admission_results: list[AdmissionResult] | None = None,
    stop: str | None = None,
    trace_path: Path | None = None,
) -> _RunnerHarness:
    trace: list[object] = []
    principal = _RunnerPrincipal(
        trace,
        loaded_stream_id=loaded_stream_id,
        load_error=load_error,
        bind_error=bind_error,
        bind_entered=bind_entered,
        bind_release=bind_release,
        admission_results=(
            [AdmissionResult.ADMITTED]
            if admission_results is None
            else list(admission_results)
        ),
    )
    bot = _runner_bot(storage_root, trace=trace, principal=principal)
    bot.principal_error = principal_error
    bootstrap = _RunnerBootstrap(trace)
    session = _RunnerSession(
        trace,
        bootstrap,
        run_body,
        [] if batches is None else list(batches),
        batch_error,
    )
    store_calls: list[tuple[Path, dict[str, object]]] = []
    ingestion_calls: list[tuple[object, object, dict[str, object]]] = []

    def open_store(path: object, **kwargs: object) -> _RunnerBootstrap:
        store_calls.append((Path(path), kwargs))
        trace.append("open-store")
        assert Path(path).is_dir()
        if store_error is not None:
            raise store_error
        return bootstrap

    def open_ingestion(
        client: object,
        selected_bootstrap: object,
        **kwargs: object,
    ) -> _RunnerSession:
        ingestion_calls.append((client, selected_bootstrap, kwargs))
        trace.append("open-ingestion")
        if open_error is not None:
            raise open_error
        return session

    _set_canary_environment(
        monkeypatch,
        stop=stop,
        trace=trace_path,
    )
    monkeypatch.setattr(
        durable_ingestion_module,
        "uuid4",
        lambda: CANARY_REQUESTED_GENERATION,
    )
    monkeypatch.setattr(nio.store, "open_ingestion_store", open_store)
    monkeypatch.setattr(nio, "open_ingestion", open_ingestion)
    return _RunnerHarness(
        bot,
        principal,
        bootstrap,
        session,
        store_calls,
        ingestion_calls,
        trace,
    )


def _exception_group_contains(group: BaseException, target: BaseException) -> bool:
    if group is target:
        return True
    return isinstance(group, BaseExceptionGroup) and any(
        _exception_group_contains(item, target) for item in group.exceptions
    )


def _trace_position(trace: Sequence[object], value: object) -> int:
    return next(index for index, item in enumerate(trace) if item == value)


def _task7_interface(name: str) -> object:
    interface = getattr(durable_ingestion_module, name, None)
    if interface is None:
        pytest.fail(f"Task7 missing interface: {name}", pytrace=False)
    return interface


async def _task7_bounded(awaitable: Awaitable[object]) -> object:
    async with asyncio.timeout(20):
        return await awaitable


@pytest.mark.asyncio
async def test_canary_filter_is_exact_canonical_classic_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _task7_interface("run_durable_ingestion")
    error = _RunnerFailure("stop after filter capture")

    async def unused_run() -> None:
        raise AssertionError("session must not be created")

    harness = _install_runner_harness(
        monkeypatch,
        tmp_path / "storage",
        run_body=unused_run,
        open_error=error,
    )
    monkeypatch.setenv(
        "MINDROOM_INGESTION_DATABASE",
        str(tmp_path / "operator-controlled.db"),
    )

    with pytest.raises(_RunnerFailure) as raised:
        await _task7_bounded(runner(harness.bot))  # type: ignore[arg-type]

    assert raised.value is error
    assert len(harness.store_calls) == len(harness.ingestion_calls) == 1
    store_path, store_kwargs = harness.store_calls[0]
    client, bootstrap, ingestion_kwargs = harness.ingestion_calls[0]
    source = store_kwargs["source"]
    config = ingestion_kwargs["config"]
    assert type(source) is ClassicSourceConfig
    assert source.timeout_ms == 30_000
    assert source.filter_json == CANARY_FILTER
    assert type(config) is IngestionConfig
    assert config.source is source
    assert client is harness.bot.client
    assert bootstrap is harness.bootstrap
    assert store_path == tmp_path / "storage" / "tracking" / "nio_ingestion"
    assert store_kwargs["database_name"] == f"{CANARY_AGENT_NAME}.db"
    assert harness.bootstrap.close_calls == 1


@pytest.mark.asyncio
async def test_canary_latch_rejects_controls_before_nio_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latch_type = _task7_interface("_CanaryLatch")
    runner = _task7_interface("run_durable_ingestion")
    external_calls: list[str] = []
    real_open, real_fstat, real_close = os.open, os.fstat, os.close
    basic_trace = _canary_trace(tmp_path / "basic-validation.log")
    assert basic_trace.is_absolute()
    assert stat.S_IMODE(basic_trace.stat().st_mode) == 0o600

    def forbidden_store(*_args: object, **_kwargs: object) -> None:
        external_calls.append("store")
        raise AssertionError("nio storage opened before validation")

    def forbidden_ingestion(*_args: object, **_kwargs: object) -> None:
        external_calls.append("http")
        raise AssertionError("nio HTTP opened before validation")

    monkeypatch.setattr(nio.store, "open_ingestion_store", forbidden_store)
    monkeypatch.setattr(nio, "open_ingestion", forbidden_ingestion)

    def invalid_bots() -> list[_RunnerBot]:
        bots: list[_RunnerBot] = []
        for index in range(8):
            bots.append(_runner_bot(tmp_path / f"invalid-{index}"))
        bots[0].config.matrix_sync.mode = "sliding"
        bots[1].config.event_journal.backend = "postgres"
        bots[2].client = object()
        assert isinstance(bots[3].client, nio.AsyncClient)
        bots[3].client.user_id = ""
        assert isinstance(bots[4].client, nio.AsyncClient)
        bots[4].client.device_id = ""
        assert isinstance(bots[5].client, nio.AsyncClient)
        bots[5].client.access_token = ""
        bots[6].approval_room_ids = frozenset()
        bots[7].approval_room_ids = frozenset({CANARY_ROOM_ID, "!other:example.org"})
        return bots

    basic_cases = [
        *((bot, CANARY_AGENT_NAME) for bot in invalid_bots()),
        (_runner_bot(tmp_path / "absent-agent"), None),
        (_runner_bot(tmp_path / "mismatched-agent"), "other"),
    ]
    for bot, agent in basic_cases:
        open_attempts: list[object] = []

        def basic_open(path: object, flags: int, mode: int = 0o777) -> int:
            open_attempts.append(path)
            return real_open(path, flags, mode)

        assert not basic_trace.resolve().is_relative_to(
            bot.runtime_paths.storage_root.resolve()
        )
        with monkeypatch.context() as basic_patch:
            basic_patch.setattr(durable_ingestion_module.os, "open", basic_open)
            _set_canary_environment(
                basic_patch,
                agent=agent,
                stop="consumer.pre",
                trace=basic_trace,
            )
            with pytest.raises((TypeError, ValueError)):
                await _task7_bounded(runner(bot))  # type: ignore[arg-type]
        assert open_attempts == []
        assert basic_trace.read_bytes() == b""
        assert not (bot.runtime_paths.storage_root / "tracking").exists()
        assert external_calls == []

    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    valid_trace = _canary_trace(tmp_path / "trace.log")

    noop_opens: list[object] = []

    def noop_open(path: object, flags: int, mode: int = 0o777) -> int:
        noop_opens.append(path)
        return real_open(path, flags, mode)

    with monkeypatch.context() as noop_patch:
        noop_patch.setattr(durable_ingestion_module.os, "open", noop_open)
        _set_canary_environment(noop_patch, agent=None)
        noop = latch_type.from_environment(storage_root=storage_root)
        noop.offer("consumer", CANARY_BOUNDARIES["consumer"], "pre")
        noop.transition("commit")
        noop.close()
    assert noop_opens == []

    rejected = (
        ("consumer.pre", None),
        (None, valid_trace),
        ("unknown.pre", valid_trace),
        ("consumer.middle", valid_trace),
        ("idle.pre", valid_trace),
        ("consumer.pre", "relative.log"),
    )
    for stop, trace_path in rejected:
        open_attempts: list[object] = []

        def rejected_open(path: object, flags: int, mode: int = 0o777) -> int:
            open_attempts.append(path)
            return real_open(path, flags, mode)

        with monkeypatch.context() as rejected_patch:
            rejected_patch.setattr(
                durable_ingestion_module.os,
                "open",
                rejected_open,
            )
            _set_canary_environment(
                rejected_patch,
                agent=None,
                stop=stop,
                trace=trace_path,
            )
            with pytest.raises((OSError, TypeError, ValueError)):
                latch_type.from_environment(storage_root=storage_root)
        assert open_attempts == []

    wrong_mode = _canary_trace(tmp_path / "wrong-mode.log")
    wrong_mode.chmod(0o640)
    under_storage = _canary_trace(storage_root / "trace.log")
    aliased_parent = tmp_path / "storage-alias"
    aliased_parent.symlink_to(storage_root, target_is_directory=True)
    aliased_under_storage = aliased_parent / under_storage.name
    symlink = tmp_path / "trace-link.log"
    symlink.symlink_to(valid_trace)
    fifo = tmp_path / "trace.fifo"
    os.mkfifo(fifo, 0o600)

    for unsafe_path in (under_storage, aliased_under_storage):
        open_attempts = []

        def contained_open(path: object, flags: int, mode: int = 0o777) -> int:
            open_attempts.append(path)
            return real_open(path, flags, mode)

        with monkeypatch.context() as contained_patch:
            contained_patch.setattr(
                durable_ingestion_module.os,
                "open",
                contained_open,
            )
            _set_canary_environment(
                contained_patch,
                agent=None,
                stop="consumer.pre",
                trace=unsafe_path,
            )
            with pytest.raises((OSError, TypeError, ValueError)):
                latch_type.from_environment(storage_root=storage_root)
        assert open_attempts == []

    for unsafe_path in (symlink, fifo):
        open_attempts: list[Path] = []
        opened: list[int] = []
        closed: list[int] = []
        reader_fd = (
            real_open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            if unsafe_path == fifo
            else None
        )

        def unsafe_open(path: object, flags: int, mode: int = 0o777) -> int:
            open_attempts.append(Path(os.fspath(path)))  # type: ignore[arg-type]
            assert flags & os.O_NONBLOCK
            assert flags & os.O_NOFOLLOW
            fd = real_open(path, flags, mode)
            opened.append(fd)
            return fd

        def unsafe_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        try:
            with monkeypatch.context() as unsafe_patch:
                unsafe_patch.setattr(durable_ingestion_module.os, "open", unsafe_open)
                unsafe_patch.setattr(durable_ingestion_module.os, "close", unsafe_close)
                _set_canary_environment(
                    unsafe_patch,
                    agent=None,
                    stop="consumer.pre",
                    trace=unsafe_path,
                )
                with pytest.raises((OSError, TypeError, ValueError)):
                    latch_type.from_environment(storage_root=storage_root)
        finally:
            if reader_fd is not None:
                real_close(reader_fd)
        assert open_attempts == [unsafe_path]
        assert opened == closed
        assert len(opened) == int(unsafe_path == fifo)

    def wrong_owner(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result((*result[:4], result.st_uid + 1, *result[5:]))

    for invalid_fstat in ("mode", "owner"):
        opened: list[int] = []
        closed: list[int] = []

        def observed_invalid_open(
            path: object,
            flags: int,
            mode: int = 0o777,
        ) -> int:
            fd = real_open(path, flags, mode)
            opened.append(fd)
            return fd

        def observed_invalid_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with monkeypatch.context() as fstat_patch:
            fstat_patch.setattr(
                durable_ingestion_module.os,
                "open",
                observed_invalid_open,
            )
            fstat_patch.setattr(
                durable_ingestion_module.os,
                "close",
                observed_invalid_close,
            )
            if invalid_fstat == "owner":
                fstat_patch.setattr(
                    durable_ingestion_module.os,
                    "fstat",
                    wrong_owner,
                )
            _set_canary_environment(
                fstat_patch,
                agent=None,
                stop="consumer.pre",
                trace=valid_trace if invalid_fstat == "owner" else wrong_mode,
            )
            with pytest.raises((OSError, TypeError, ValueError)):
                latch_type.from_environment(storage_root=storage_root)
        assert opened == closed and len(opened) == 1

    missing_pairs = (("consumer.pre", None), (None, valid_trace))
    for index, (stop, trace_path) in enumerate(missing_pairs):
        bot = _runner_bot(tmp_path / f"runner-missing-pair-{index}")
        _set_canary_environment(monkeypatch, stop=stop, trace=trace_path)
        with pytest.raises((OSError, TypeError, ValueError)):
            await _task7_bounded(runner(bot))  # type: ignore[arg-type]
        assert not (bot.runtime_paths.storage_root / "tracking").exists()

    for invalid_fstat in ("mode", "owner"):
        bot = _runner_bot(tmp_path / f"runner-{invalid_fstat}")
        opened = []
        closed = []

        def runner_invalid_open(
            path: object,
            flags: int,
            mode: int = 0o777,
        ) -> int:
            fd = real_open(path, flags, mode)
            opened.append(fd)
            return fd

        def runner_invalid_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with monkeypatch.context() as runner_invalid_patch:
            runner_invalid_patch.setattr(
                durable_ingestion_module.os,
                "open",
                runner_invalid_open,
            )
            runner_invalid_patch.setattr(
                durable_ingestion_module.os,
                "close",
                runner_invalid_close,
            )
            if invalid_fstat == "owner":
                runner_invalid_patch.setattr(
                    durable_ingestion_module.os,
                    "fstat",
                    wrong_owner,
                )
            _set_canary_environment(
                runner_invalid_patch,
                stop="consumer.pre",
                trace=valid_trace if invalid_fstat == "owner" else wrong_mode,
            )
            with pytest.raises((OSError, TypeError, ValueError)):
                await _task7_bounded(runner(bot))  # type: ignore[arg-type]
        assert opened == closed and len(opened) == 1
        assert not (bot.runtime_paths.storage_root / "tracking").exists()

    for index, invalid_stop in enumerate(("unknown.pre", "consumer.middle")):
        _set_canary_environment(
            monkeypatch,
            stop=invalid_stop,
            trace=valid_trace,
        )
        invalid_control_bot = _runner_bot(tmp_path / f"invalid-control-{index}")
        with pytest.raises((OSError, TypeError, ValueError)):
            await _task7_bounded(runner(invalid_control_bot))  # type: ignore[arg-type]
        assert external_calls == []
        assert not (
            invalid_control_bot.runtime_paths.storage_root / "tracking"
        ).exists()

    opened_flags: list[int] = []

    def observed_open(path: object, flags: int, mode: int = 0o777) -> int:
        opened_flags.append(flags)
        return real_open(path, flags, mode)

    with monkeypatch.context() as flags_patch:
        flags_patch.setattr(durable_ingestion_module.os, "open", observed_open)
        for phase in tuple(CANARY_BOUNDARIES)[:-1]:
            for side in ("pre", "post"):
                _set_canary_environment(
                    flags_patch,
                    agent=None,
                    stop=f"{phase}.{side}",
                    trace=valid_trace,
                )
                latch_type.from_environment(storage_root=storage_root).close()
        _set_canary_environment(
            flags_patch,
            agent=None,
            stop="idle.post",
            trace=valid_trace,
        )
        latch_type.from_environment(storage_root=storage_root).close()
    expected_flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    assert opened_flags == [expected_flags] * 23

    positive_kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        durable_ingestion_module.os,
        "kill",
        lambda pid, selected_signal: positive_kills.append((pid, selected_signal)),
    )
    operation_sides = (
        *(
            (phase, side)
            for phase in ("consumer", "bootstrap", "bind", "admission")
            for side in ("pre", "post")
        ),
        ("idle", "post"),
    )
    for index, (phase, side) in enumerate(operation_sides, start=1):
        trace_path = _canary_trace(tmp_path / f"operation-{phase}-{side}.log")
        _set_canary_environment(
            monkeypatch,
            agent=None,
            stop=f"{phase}.{side}",
            trace=trace_path,
        )
        selected_latch = latch_type.from_environment(storage_root=storage_root)
        selected_latch.offer(phase, CANARY_BOUNDARIES[phase], side)
        assert json.loads(trace_path.read_bytes()) == {
            "boundary": CANARY_BOUNDARIES[phase],
            "phase": phase,
            "pid": os.getpid(),
            "side": side,
        }
        selected_latch.close()
        assert positive_kills == [(os.getpid(), signal.SIGSTOP)] * index


def test_canary_latch_writes_one_allowlisted_fsynced_stop_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latch_type = _task7_interface("_CanaryLatch")
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    trace_path = _canary_trace(tmp_path / "trace.log")
    _set_canary_environment(
        monkeypatch,
        agent=None,
        stop="consumer.pre",
        trace=trace_path,
    )
    real_write, real_fsync, real_close = os.write, os.fsync, os.close
    order: list[tuple[str, object]] = []
    kill_calls: list[tuple[int, signal.Signals]] = []
    holder: dict[str, object] = {}

    for invalid_count in (0, -1):
        invalid_path = _canary_trace(tmp_path / f"write-{invalid_count}.log")
        invalid_closes: list[int] = []
        invalid_kills: list[tuple[int, signal.Signals]] = []
        invalid_writes: list[int] = []

        def invalid_write(_fd: int, _value: bytes) -> int:
            invalid_writes.append(invalid_count)
            if len(invalid_writes) > 1:
                raise AssertionError("nonpositive write retried")
            return invalid_count

        def invalid_close(fd: int) -> None:
            invalid_closes.append(fd)
            real_close(fd)

        with monkeypatch.context() as invalid_write_patch:
            _set_canary_environment(
                invalid_write_patch,
                agent=None,
                stop="consumer.pre",
                trace=invalid_path,
            )
            invalid_write_patch.setattr(
                durable_ingestion_module.os,
                "write",
                invalid_write,
            )
            invalid_write_patch.setattr(
                durable_ingestion_module.os,
                "close",
                invalid_close,
            )
            invalid_write_patch.setattr(
                durable_ingestion_module.os,
                "kill",
                lambda pid, selected_signal: invalid_kills.append(
                    (pid, selected_signal)
                ),
            )
            invalid = latch_type.from_environment(storage_root=storage_root)
            with pytest.raises(ValueError):
                invalid.offer(
                    "consumer",
                    CANARY_BOUNDARIES["consumer"],
                    "pre",
                )
            assert len(invalid_closes) == 1
            assert invalid_writes == [invalid_count]
            invalid.close()
            assert len(invalid_closes) == 1
        assert invalid_kills == []
        assert invalid_path.read_bytes() == b""

    def partial_write(fd: int, value: bytes) -> int:
        count = real_write(fd, value[:7])
        order.append(("write", count))
        return count

    def observed_fsync(fd: int) -> None:
        real_fsync(fd)
        order.append(("fsync", fd))

    def observed_close(fd: int) -> None:
        real_close(fd)
        order.append(("close", fd))

    def observed_kill(pid: int, selected_signal: signal.Signals) -> None:
        kill_calls.append((pid, selected_signal))
        order.append(("kill", selected_signal))
        if len(kill_calls) == 1:
            selected_latch = holder["latch"]
            selected_latch.offer(  # type: ignore[union-attr]
                "consumer",
                CANARY_BOUNDARIES["consumer"],
                "pre",
            )
            close_count = sum(name == "close" for name, _value in order)
            selected_latch.close()  # type: ignore[union-attr]
            assert sum(name == "close" for name, _value in order) == close_count

    monkeypatch.setattr(durable_ingestion_module.os, "write", partial_write)
    monkeypatch.setattr(durable_ingestion_module.os, "fsync", observed_fsync)
    monkeypatch.setattr(durable_ingestion_module.os, "close", observed_close)
    monkeypatch.setattr(durable_ingestion_module.os, "kill", observed_kill)
    latch = latch_type.from_environment(storage_root=storage_root)
    holder["latch"] = latch

    latch.offer("consumer", CANARY_BOUNDARIES["consumer"], "pre")

    expected = (
        f'{{"boundary":"load_or_create_ingestion_consumer","phase":"consumer",'
        f'"pid":{os.getpid()},"side":"pre"}}\n'
    ).encode()
    assert trace_path.read_bytes() == expected
    assert kill_calls == [(os.getpid(), signal.SIGSTOP)]
    assert all(name == "write" for name, _value in order[:-3])
    assert [name for name, _value in order[-3:]] == ["fsync", "close", "kill"]
    first_order = tuple(order)
    latch.offer("consumer", CANARY_BOUNDARIES["consumer"], "pre")
    latch.close()
    assert tuple(order) == first_order
    assert trace_path.read_bytes() == expected

    unfired_path = _canary_trace(tmp_path / "unfired.log")
    _set_canary_environment(
        monkeypatch,
        agent=None,
        stop="bind.post",
        trace=unfired_path,
    )
    unfired = latch_type.from_environment(storage_root=storage_root)
    close_count = sum(name == "close" for name, _value in order)
    unfired.close()
    unfired.close()
    assert sum(name == "close" for name, _value in order) == close_count + 1
    assert unfired_path.read_bytes() == b""


def test_canary_latch_correlates_only_selected_final_dml_and_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latch_type = _task7_interface("_CanaryLatch")
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        durable_ingestion_module.os,
        "kill",
        lambda pid, selected_signal: kills.append((pid, selected_signal)),
    )
    all_final_labels = set(CANARY_TRANSITIONS.values())

    for phase, final_label in CANARY_TRANSITIONS.items():
        for side in ("pre", "post"):
            trace_path = _canary_trace(tmp_path / f"{phase}-{side}.log")
            if side == "post":
                unrelated_labels = (
                    "meta_revision_epoch_cas",
                    "delivery_work_delete",
                    *sorted(all_final_labels - {final_label}),
                )
                for index, unrelated_label in enumerate(unrelated_labels):
                    probe_path = _canary_trace(
                        tmp_path / f"{phase}-post-unrelated-{index}.log"
                    )
                    _set_canary_environment(
                        monkeypatch,
                        agent=None,
                        stop=f"{phase}.post",
                        trace=probe_path,
                    )
                    probe = latch_type.from_environment(storage_root=storage_root)
                    probe.transition(unrelated_label)
                    probe.transition("before_commit")
                    probe.transition("commit")
                    assert probe_path.read_bytes() == b""
                    probe.close()
            _set_canary_environment(
                monkeypatch,
                agent=None,
                stop=f"{phase}.{side}",
                trace=trace_path,
            )
            latch = latch_type.from_environment(storage_root=storage_root)
            ignored = ["commit", "before_commit"]
            if side == "pre":
                ignored.extend(
                    (
                        "meta_revision_epoch_cas",
                        "delivery_work_delete",
                        *sorted(all_final_labels - {final_label}),
                    )
                )
            for label in ignored:
                latch.transition(label)
            assert trace_path.read_bytes() == b""
            before_kills = len(kills)

            latch.transition(final_label)
            if side == "post":
                assert trace_path.read_bytes() == b""
                for unrelated_label in (
                    "meta_revision_epoch_cas",
                    "delivery_work_delete",
                    *sorted(all_final_labels - {final_label}),
                ):
                    latch.transition(unrelated_label)
                latch.transition("before_commit")
                assert trace_path.read_bytes() == b""
                latch.transition("commit")

            assert json.loads(trace_path.read_bytes()) == {
                "boundary": CANARY_BOUNDARIES[phase],
                "phase": phase,
                "pid": os.getpid(),
                "side": side,
            }
            assert kills[before_kills:] == [(os.getpid(), signal.SIGSTOP)]
            frozen = trace_path.read_bytes()
            latch.transition("commit")
            latch.transition(final_label)
            latch.close()
            assert trace_path.read_bytes() == frozen


@pytest.mark.asyncio
async def test_latched_admission_brackets_only_the_real_committed_view(
    tmp_path: Path,
) -> None:
    latched_type = _task7_interface("_LatchedAdmission")
    store = EventJournalStore.open_sqlite(tmp_path / "latched-admission.db")
    try:
        await _bound_principal(store)
        inside_transaction = threading.Event()
        release_transaction = threading.Event()

        def hold_receipt() -> None:
            inside_transaction.set()
            assert release_transaction.wait(20), "latched admission never released"

        principal = EventJournalStore(
            backend=_ObservedBackend(
                store.backend,
                lambda transaction: _ObservedTransaction(
                    transaction,
                    statement_matches=lambda sql: (
                        "INSERT INTO matrix_ingestion_receipts" in sql
                    ),
                    after_statement=hold_receipt,
                ),
            )
        ).principal(ACCOUNT_ID)
        recorder = _LatchRecorder()
        view = latched_type(principal, recorder)
        admitting = asyncio.create_task(
            view.admit_ingestion_batch(_expected_admission())
        )
        try:
            assert await asyncio.to_thread(inside_transaction.wait, 20)
            assert recorder.offers == [
                ("admission", CANARY_BOUNDARIES["admission"], "pre")
            ]
            assert not admitting.done()
            assert await _graph(store) == _old_graph()
        finally:
            release_transaction.set()
            with suppress(BaseException):
                await _task7_bounded(admitting)

        assert admitting.result() is AdmissionResult.ADMITTED
        assert recorder.offers == [
            ("admission", CANARY_BOUNDARIES["admission"], "pre"),
            ("admission", CANARY_BOUNDARIES["admission"], "post"),
        ]
        assert await _graph(store) == _fresh_graph()

        duplicate_recorder = _LatchRecorder()
        duplicate = latched_type(
            _AdapterAdmission(result=AdmissionResult.DUPLICATE),
            duplicate_recorder,
        )
        duplicate_result = await duplicate.admit_ingestion_batch(_expected_admission())
        assert duplicate_result is AdmissionResult.DUPLICATE
        assert duplicate_recorder.offers == [
            ("admission", CANARY_BOUNDARIES["admission"], "pre"),
            ("admission", CANARY_BOUNDARIES["admission"], "post"),
        ]

        error = _RunnerFailure("admission failed")
        failing_recorder = _LatchRecorder()
        failing = latched_type(_AdapterAdmission(error=error), failing_recorder)
        with pytest.raises(_RunnerFailure) as raised:
            await failing.admit_ingestion_batch(_expected_admission())
        assert raised.value is error
        assert failing_recorder.offers == [
            ("admission", CANARY_BOUNDARIES["admission"], "pre")
        ]

        entered, release = asyncio.Event(), asyncio.Event()
        cancelled_recorder = _LatchRecorder()
        cancellable = latched_type(
            _AdapterAdmission(entered=entered, release=release),
            cancelled_recorder,
        )
        cancelling = asyncio.create_task(
            cancellable.admit_ingestion_batch(_expected_admission())
        )
        await _task7_bounded(entered.wait())
        cancelling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _task7_bounded(cancelling)
        assert cancelled_recorder.offers == [
            ("admission", CANARY_BOUNDARIES["admission"], "pre")
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_durable_runner_uses_exact_handshake_and_one_record_task6_pump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _task7_interface("run_durable_ingestion")
    latch_type = _task7_interface("_CanaryLatch")
    session_block = asyncio.Event()
    pump_error = _RunnerFailure("focused pump complete")

    async def run_body() -> None:
        await session_block.wait()

    admitted_batch, duplicate_batch = _batch(), _batch()
    harness = _install_runner_harness(
        monkeypatch,
        tmp_path / "storage",
        run_body=run_body,
        batches=[None, admitted_batch, duplicate_batch],
        batch_error=pump_error,
        admission_results=[AdmissionResult.ADMITTED, AdmissionResult.DUPLICATE],
    )
    monkeypatch.setenv(
        "MINDROOM_INGESTION_DATABASE",
        str(tmp_path / "ignored-database.db"),
    )

    restart_root = tmp_path / "restart-storage"
    restart_parent = restart_root / "tracking" / "nio_ingestion"
    restart_parent.mkdir(parents=True)
    restart_error = _RunnerFailure("restart reached top-level open")

    async def restart_unused_run() -> None:
        raise AssertionError("restart session must not be entered")

    with monkeypatch.context() as restart_patch:
        restart_harness = _install_runner_harness(
            restart_patch,
            restart_root,
            run_body=restart_unused_run,
            loaded_stream_id=STREAM_ID,
            open_error=restart_error,
        )
        with pytest.raises(_RunnerFailure) as restart_raised:
            await _task7_bounded(runner(restart_harness.bot))  # type: ignore[arg-type]
    assert restart_raised.value is restart_error
    assert restart_parent.is_dir()
    assert restart_harness.trace == [
        "journal-principal",
        ("load-consumer", CANARY_REQUESTED_GENERATION),
        "open-store",
        ("bind-stream", CANARY_GENERATION, STREAM_ID),
        "open-ingestion",
        "bootstrap-close",
    ]
    assert restart_harness.bootstrap.close_calls == 1
    assert (
        restart_harness.session.enter_calls == restart_harness.session.exit_calls == 0
    )
    assert len(restart_harness.store_calls) == len(restart_harness.ingestion_calls) == 1
    restart_store_path, restart_store_kwargs = restart_harness.store_calls[0]
    restart_client, restart_bootstrap, restart_ingestion_kwargs = (
        restart_harness.ingestion_calls[0]
    )
    assert restart_store_path == restart_parent
    assert restart_store_kwargs["consumer_generation"] == CANARY_GENERATION
    assert restart_client is restart_harness.bot.client
    assert restart_bootstrap is restart_harness.bootstrap
    assert restart_ingestion_kwargs["consumer_generation"] == CANARY_GENERATION
    assert restart_ingestion_kwargs["stream_id"] == STREAM_ID
    assert restart_ingestion_kwargs["room_id"] == CANARY_ROOM_ID

    consume_calls: list[tuple[object, object, str, str]] = []
    latch_offers: list[tuple[str, str, str]] = []
    latch_instances: list[object] = []
    call_count = 0
    real_consume = consume_one_ingestion_batch
    real_offer = latch_type.offer

    def observed_offer(
        latch: object,
        phase: str,
        boundary: str,
        side: str,
    ) -> None:
        latch_instances.append(latch)
        latch_offers.append((phase, boundary, side))
        harness.trace.append(("latch-offer", phase, boundary, side))
        real_offer(latch, phase, boundary, side)

    async def consume(
        session: object,
        admission: object,
        *,
        account_id: str,
        device_id: str,
    ) -> AdmissionResult | None:
        nonlocal call_count
        call_count += 1
        consume_calls.append((session, admission, account_id, device_id))
        harness.trace.append(f"task6-call-{call_count}")
        result = await real_consume(
            session,  # type: ignore[arg-type]
            admission,  # type: ignore[arg-type]
            account_id=account_id,
            device_id=device_id,
        )
        harness.trace.append(("task6-return", call_count, result))
        return result

    real_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def observed_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        harness.trace.append(("sleep", delay))
        await real_sleep(0)

    monkeypatch.setattr(
        durable_ingestion_module,
        "consume_one_ingestion_batch",
        consume,
    )
    monkeypatch.setattr(latch_type, "offer", observed_offer)
    monkeypatch.setattr(durable_ingestion_module.asyncio, "sleep", observed_sleep)

    with pytest.raises(ExceptionGroup) as raised:
        await _task7_bounded(runner(harness.bot))  # type: ignore[arg-type]

    assert _exception_group_contains(raised.value, pump_error)
    consumer_pre = (
        "latch-offer",
        "consumer",
        CANARY_BOUNDARIES["consumer"],
        "pre",
    )
    consumer_post = (*consumer_pre[:-1], "post")
    bootstrap_pre = (
        "latch-offer",
        "bootstrap",
        CANARY_BOUNDARIES["bootstrap"],
        "pre",
    )
    bootstrap_post = (*bootstrap_pre[:-1], "post")
    bind_pre = ("latch-offer", "bind", CANARY_BOUNDARIES["bind"], "pre")
    bind_post = (*bind_pre[:-1], "post")
    idle_post = ("latch-offer", "idle", CANARY_BOUNDARIES["idle"], "post")
    assert (
        _trace_position(harness.trace, "journal-principal")
        < _trace_position(harness.trace, consumer_pre)
        < _trace_position(harness.trace, ("load-consumer", CANARY_REQUESTED_GENERATION))
        < _trace_position(harness.trace, consumer_post)
    )
    assert (
        _trace_position(harness.trace, bootstrap_pre)
        < _trace_position(harness.trace, "open-store")
        < _trace_position(harness.trace, bootstrap_post)
    )
    assert (
        _trace_position(harness.trace, bind_pre)
        < _trace_position(harness.trace, ("bind-stream", CANARY_GENERATION, STREAM_ID))
        < _trace_position(harness.trace, bind_post)
        < _trace_position(harness.trace, "open-ingestion")
    )
    assert harness.session.enter_calls == harness.session.exit_calls == 1
    assert harness.bootstrap.close_calls == 1
    assert len(harness.store_calls) == len(harness.ingestion_calls) == 1
    store_path, store_kwargs = harness.store_calls[0]
    client, bootstrap, ingestion_kwargs = harness.ingestion_calls[0]
    expected_parent = tmp_path / "storage" / "tracking" / "nio_ingestion"
    assert store_path == expected_parent
    assert expected_parent.is_dir()
    assert set(store_kwargs) == {
        "account_id",
        "device_id",
        "consumer_generation",
        "source",
        "database_name",
        "transition_statement_hook",
    }
    assert store_kwargs["account_id"] == ACCOUNT_ID
    assert store_kwargs["device_id"] == DEVICE_ID
    assert store_kwargs["consumer_generation"] == CANARY_GENERATION
    assert type(store_kwargs["source"]) is ClassicSourceConfig
    assert store_kwargs["source"].filter_json == CANARY_FILTER  # type: ignore[union-attr]
    assert store_kwargs["database_name"] == f"{CANARY_AGENT_NAME}.db"
    transition_hook = store_kwargs["transition_statement_hook"]
    assert callable(transition_hook)
    assert transition_hook.__self__ is latch_instances[0]  # type: ignore[union-attr]
    assert transition_hook.__func__ is latch_type.transition  # type: ignore[union-attr]
    assert client is harness.bot.client
    assert bootstrap is harness.bootstrap
    assert set(ingestion_kwargs) == {
        "config",
        "consumer_generation",
        "stream_id",
        "room_id",
    }
    assert type(ingestion_kwargs["config"]) is IngestionConfig
    assert ingestion_kwargs["config"].source is store_kwargs["source"]  # type: ignore[union-attr]
    assert ingestion_kwargs["consumer_generation"] == CANARY_GENERATION
    assert ingestion_kwargs["stream_id"] == STREAM_ID
    assert ingestion_kwargs["room_id"] == CANARY_ROOM_ID
    assert len(consume_calls) == 4
    assert all(call[0] is harness.session for call in consume_calls)
    assert all(
        type(call[1]) is durable_ingestion_module._LatchedAdmission
        for call in consume_calls
    )
    assert all(call[2:] == (ACCOUNT_ID, DEVICE_ID) for call in consume_calls)
    assert harness.session.next_calls == [{"max_records": 1}] * 4
    assert harness.session.ack_attempts == [
        admitted_batch.ref,
        duplicate_batch.ref,
    ]
    assert sleep_calls == [0.05]
    assert harness.bot._journal_dispatcher.wake_calls == 2
    assert harness.principal.admissions == [
        _expected_admission(),
        _expected_admission(),
    ]
    assert latch_instances and all(
        latch is latch_instances[0] for latch in latch_instances
    )
    assert latch_offers == [
        ("consumer", CANARY_BOUNDARIES["consumer"], "pre"),
        ("consumer", CANARY_BOUNDARIES["consumer"], "post"),
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "pre"),
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "post"),
        ("bind", CANARY_BOUNDARIES["bind"], "pre"),
        ("bind", CANARY_BOUNDARIES["bind"], "post"),
        ("idle", CANARY_BOUNDARIES["idle"], "post"),
        ("admission", CANARY_BOUNDARIES["admission"], "pre"),
        ("admission", CANARY_BOUNDARIES["admission"], "post"),
        ("admission", CANARY_BOUNDARIES["admission"], "pre"),
        ("admission", CANARY_BOUNDARIES["admission"], "post"),
    ]
    assert (
        _trace_position(harness.trace, ("task6-return", 1, None))
        < _trace_position(harness.trace, idle_post)
        < _trace_position(harness.trace, ("sleep", 0.05))
    )
    for index, result in enumerate(
        (AdmissionResult.ADMITTED, AdmissionResult.DUPLICATE),
        start=1,
    ):
        call = index + 1
        assert _trace_position(
            harness.trace,
            ("session-ack", index, harness.session.ack_attempts[index - 1]),
        ) < _trace_position(harness.trace, ("task6-return", call, result))
        assert _trace_position(
            harness.trace, ("task6-return", call, result)
        ) < _trace_position(harness.trace, ("dispatcher-wake", index))
    assert "session-run-cancelled" in harness.trace


@pytest.mark.asyncio
async def test_durable_runner_closes_session_when_either_taskgroup_child_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _task7_interface("run_durable_ingestion")
    real_open, real_close = os.open, os.close

    for failing_child in ("session", "pump"):
        with monkeypatch.context() as selected:
            case_root = tmp_path / failing_child
            storage_root = case_root / "storage"
            trace_path = _canary_trace(case_root / "trace.log")
            run_started, pump_started = asyncio.Event(), asyncio.Event()
            error = _RunnerFailure(f"{failing_child} failed")

            async def run_body() -> None:
                run_started.set()
                if failing_child == "session":
                    await pump_started.wait()
                    raise error
                await asyncio.Future()

            harness = _install_runner_harness(
                selected,
                storage_root,
                run_body=run_body,
                stop="idle.post",
                trace_path=trace_path,
            )
            opened: list[int] = []
            closed: list[int] = []
            kill_calls: list[tuple[int, signal.Signals]] = []

            def observed_open(path: object, flags: int, mode: int = 0o777) -> int:
                fd = real_open(path, flags, mode)
                opened.append(fd)
                return fd

            def observed_close(fd: int) -> None:
                closed.append(fd)
                harness.trace.append("latch-close")
                real_close(fd)

            async def consume(
                _session: object,
                _admission: object,
                *,
                account_id: str,
                device_id: str,
            ) -> None:
                assert (account_id, device_id) == (ACCOUNT_ID, DEVICE_ID)
                pump_started.set()
                if failing_child == "pump":
                    await run_started.wait()
                    raise error
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    harness.trace.append("pump-cancelled")
                    raise

            selected.setattr(durable_ingestion_module.os, "open", observed_open)
            selected.setattr(durable_ingestion_module.os, "close", observed_close)
            selected.setattr(
                durable_ingestion_module.os,
                "kill",
                lambda pid, selected_signal: kill_calls.append((pid, selected_signal)),
            )
            selected.setattr(
                durable_ingestion_module,
                "consume_one_ingestion_batch",
                consume,
            )

            with pytest.raises(ExceptionGroup) as raised:
                await _task7_bounded(runner(harness.bot))  # type: ignore[arg-type]

            assert _exception_group_contains(raised.value, error)
            assert run_started.is_set() and pump_started.is_set()
            assert harness.session.enter_calls == harness.session.exit_calls == 1
            assert harness.bootstrap.close_calls == 1
            assert opened == closed and len(opened) == 1
            assert kill_calls == []
            assert trace_path.read_bytes() == b""
            assert _trace_position(harness.trace, "bootstrap-close") < _trace_position(
                harness.trace, "latch-close"
            )
            assert harness.bot._journal_dispatcher.wake_calls == 0
            cancelled = (
                "pump-cancelled"
                if failing_child == "session"
                else "session-run-cancelled"
            )
            assert cancelled in harness.trace


@pytest.mark.asyncio
async def test_durable_runner_closes_bootstrap_and_latch_when_bind_or_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _task7_interface("run_durable_ingestion")
    latch_type = _task7_interface("_CanaryLatch")
    real_offer = latch_type.offer
    real_open, real_write, real_close = os.open, os.write, os.close
    consumer_pair = [
        ("consumer", CANARY_BOUNDARIES["consumer"], "pre"),
        ("consumer", CANARY_BOUNDARIES["consumer"], "post"),
    ]
    bootstrap_pair = [
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "pre"),
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "post"),
    ]
    bind_pair = [
        ("bind", CANARY_BOUNDARIES["bind"], "pre"),
        ("bind", CANARY_BOUNDARIES["bind"], "post"),
    ]
    failure_stops = {
        "mkdir": "consumer.pre",
        "principal": "consumer.pre",
        "load": "consumer.post",
        "store": "bootstrap.post",
        "bind": "bind.post",
        "open": "idle.post",
    }

    for failure_point in (
        "mkdir",
        "principal",
        "load",
        "store",
        "bind",
        "open",
    ):
        with monkeypatch.context() as selected:
            case_root = tmp_path / failure_point
            trace_path = _canary_trace(case_root / "trace.log")
            error = _RunnerFailure(f"{failure_point} failed")
            storage_root = case_root / "storage"
            expected_parent = storage_root / "tracking" / "nio_ingestion"
            if failure_point == "mkdir":
                storage_root.mkdir()
                (storage_root / "tracking").write_bytes(b"not-a-directory")
            elif failure_point == "principal":
                expected_parent.mkdir(parents=True)

            async def unused_run() -> None:
                raise AssertionError("session must not be entered")

            harness = _install_runner_harness(
                selected,
                storage_root,
                run_body=unused_run,
                principal_error=error if failure_point == "principal" else None,
                load_error=error if failure_point == "load" else None,
                store_error=error if failure_point == "store" else None,
                bind_error=error if failure_point == "bind" else None,
                open_error=error if failure_point == "open" else None,
                stop=failure_stops[failure_point],
                trace_path=trace_path,
            )
            opened: list[int] = []
            closed: list[int] = []
            kill_calls: list[tuple[int, signal.Signals]] = []
            offers: list[tuple[str, str, str]] = []
            write_calls: list[tuple[int, bytes]] = []

            def observed_open(path: object, flags: int, mode: int = 0o777) -> int:
                fd = real_open(path, flags, mode)
                opened.append(fd)
                return fd

            def observed_close(fd: int) -> None:
                closed.append(fd)
                harness.trace.append("latch-close")
                real_close(fd)

            def observed_write(fd: int, value: bytes) -> int:
                write_calls.append((fd, value))
                return real_write(fd, value)

            def observed_offer(
                latch: object,
                phase: str,
                boundary: str,
                side: str,
            ) -> None:
                offers.append((phase, boundary, side))
                real_offer(latch, phase, boundary, side)

            selected.setattr(durable_ingestion_module.os, "open", observed_open)
            selected.setattr(durable_ingestion_module.os, "close", observed_close)
            selected.setattr(durable_ingestion_module.os, "write", observed_write)
            selected.setattr(latch_type, "offer", observed_offer)
            selected.setattr(
                durable_ingestion_module.os,
                "kill",
                lambda pid, selected_signal: kill_calls.append((pid, selected_signal)),
            )

            if failure_point == "mkdir":
                with pytest.raises(NotADirectoryError) as raised:
                    await _task7_bounded(runner(harness.bot))  # type: ignore[arg-type]
                assert Path(raised.value.filename) == expected_parent
            else:
                with pytest.raises(_RunnerFailure) as raised:
                    await _task7_bounded(runner(harness.bot))  # type: ignore[arg-type]
                assert raised.value is error
            bootstrap_owned = failure_point in ("bind", "open")
            assert harness.bootstrap.close_calls == int(bootstrap_owned)
            assert harness.session.enter_calls == harness.session.exit_calls == 0
            assert opened == closed and len(opened) == 1
            assert kill_calls == []
            assert write_calls == []
            assert trace_path.read_bytes() == b""
            if bootstrap_owned:
                assert _trace_position(
                    harness.trace, "bootstrap-close"
                ) < _trace_position(harness.trace, "latch-close")
            else:
                assert "bootstrap-close" not in harness.trace
                assert "latch-close" in harness.trace
            assert len(harness.store_calls) == int(
                failure_point not in ("mkdir", "principal", "load")
            )
            assert len(harness.ingestion_calls) == int(failure_point == "open")
            assert harness.principal.admissions == []
            assert harness.bot._journal_dispatcher.wake_calls == 0
            expected_offers = {
                "mkdir": [],
                "principal": [],
                "load": [consumer_pair[0]],
                "store": [*consumer_pair, bootstrap_pair[0]],
                "bind": [*consumer_pair, *bootstrap_pair, bind_pair[0]],
                "open": [*consumer_pair, *bootstrap_pair, *bind_pair],
            }
            assert offers == expected_offers[failure_point]
            expected_traces = {
                "mkdir": ["latch-close"],
                "principal": ["journal-principal", "latch-close"],
                "load": [
                    "journal-principal",
                    ("load-consumer", CANARY_REQUESTED_GENERATION),
                    "latch-close",
                ],
                "store": [
                    "journal-principal",
                    ("load-consumer", CANARY_REQUESTED_GENERATION),
                    "open-store",
                    "latch-close",
                ],
                "bind": [
                    "journal-principal",
                    ("load-consumer", CANARY_REQUESTED_GENERATION),
                    "open-store",
                    ("bind-stream", CANARY_GENERATION, STREAM_ID),
                    "bootstrap-close",
                    "latch-close",
                ],
                "open": [
                    "journal-principal",
                    ("load-consumer", CANARY_REQUESTED_GENERATION),
                    "open-store",
                    ("bind-stream", CANARY_GENERATION, STREAM_ID),
                    "open-ingestion",
                    "bootstrap-close",
                    "latch-close",
                ],
            }
            assert harness.trace == expected_traces[failure_point]


@pytest.mark.asyncio
async def test_durable_runner_closes_bootstrap_and_latch_when_setup_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _task7_interface("run_durable_ingestion")
    latch_type = _task7_interface("_CanaryLatch")
    real_offer = latch_type.offer
    bind_entered, bind_release = asyncio.Event(), asyncio.Event()
    trace_path = _canary_trace(tmp_path / "trace.log")

    async def unused_run() -> None:
        raise AssertionError("session must not be entered")

    harness = _install_runner_harness(
        monkeypatch,
        tmp_path / "storage",
        run_body=unused_run,
        bind_entered=bind_entered,
        bind_release=bind_release,
        stop="bind.post",
        trace_path=trace_path,
    )
    real_open, real_close = os.open, os.close
    opened: list[int] = []
    closed: list[int] = []
    kill_calls: list[tuple[int, signal.Signals]] = []
    offers: list[tuple[str, str, str]] = []

    def observed_open(path: object, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        opened.append(fd)
        return fd

    def observed_close(fd: int) -> None:
        closed.append(fd)
        harness.trace.append("latch-close")
        real_close(fd)

    def observed_offer(
        latch: object,
        phase: str,
        boundary: str,
        side: str,
    ) -> None:
        offers.append((phase, boundary, side))
        real_offer(latch, phase, boundary, side)

    monkeypatch.setattr(durable_ingestion_module.os, "open", observed_open)
    monkeypatch.setattr(durable_ingestion_module.os, "close", observed_close)
    monkeypatch.setattr(latch_type, "offer", observed_offer)
    monkeypatch.setattr(
        durable_ingestion_module.os,
        "kill",
        lambda pid, selected_signal: kill_calls.append((pid, selected_signal)),
    )
    running = asyncio.create_task(runner(harness.bot))  # type: ignore[arg-type]
    await _task7_bounded(bind_entered.wait())
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await _task7_bounded(running)
    assert harness.bootstrap.close_calls == 1
    assert harness.session.enter_calls == harness.session.exit_calls == 0
    assert harness.ingestion_calls == []
    assert "bind-cancelled" in harness.trace
    assert opened == closed and len(opened) == 1
    assert kill_calls == []
    assert offers == [
        ("consumer", CANARY_BOUNDARIES["consumer"], "pre"),
        ("consumer", CANARY_BOUNDARIES["consumer"], "post"),
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "pre"),
        ("bootstrap", CANARY_BOUNDARIES["bootstrap"], "post"),
        ("bind", CANARY_BOUNDARIES["bind"], "pre"),
    ]
    assert trace_path.read_bytes() == b""
    assert _trace_position(harness.trace, "bootstrap-close") < _trace_position(
        harness.trace, "latch-close"
    )
