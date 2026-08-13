"""Strict validation for one durable nio ingestion batch."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4, uuid5

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
from nio.ingest.serialization import batch_from_records

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
    IngestionConsumerBindingError,
    ProjectedEvent,
)
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
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence

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
