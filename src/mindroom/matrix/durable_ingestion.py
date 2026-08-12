"""Strict conversion of authenticated nio batches into local admissions."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
from hashlib import sha256
from hmac import compare_digest
from json import JSONEncoder, loads
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4, uuid5

import nio
import nio.ingest as ingest  # noqa: PLR0402 - carriers are owned by nio.ingest
import nio.store as nio_store
from nio import IngestionSession
from nio.ingest.config import ClassicSourceConfig, IngestionConfig

import mindroom.event_journal as ej
from mindroom.event_journal.journal import _validate_ingestion_batch_admission
from mindroom.matrix.journal_ingress import ingestion_event_views as event_views

if TYPE_CHECKING:
    from mindroom.bot import AgentBot
    from mindroom.event_journal.views import IngestionBatchAdmissionView

_CANARY_BOUNDARIES = {
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


class _CanaryLatch:
    def __init__(
        self,
        selector: tuple[str, str, str] | None = None,
        fd: int | None = None,
    ) -> None:
        self._selector = selector
        self._fd = fd
        self._armed = False
        self._fired = False

    @classmethod
    def from_environment(cls, *, storage_root: Path) -> _CanaryLatch:
        stop = os.getenv("MINDROOM_INGESTION_CANARY_STOP")
        trace = os.getenv("MINDROOM_INGESTION_CANARY_TRACE")
        if stop is None and trace is None:
            return cls()
        if stop is None or trace is None:
            raise ValueError
        phase, separator, side = stop.rpartition(".")
        boundary = _CANARY_BOUNDARIES.get(phase)
        if (
            not separator
            or boundary is None
            or side not in {"pre", "post"}
            or (phase == "idle" and side != "post")
        ):
            raise ValueError
        path = Path(trace)
        if not path.is_absolute() or path.resolve().is_relative_to(
            storage_root.resolve(),
        ):
            raise ValueError
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            status = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            os.close(fd)
            raise ValueError
        return cls((phase, boundary, side), fd)

    def offer(self, phase: str, boundary: str, side: str) -> None:
        if self._fd is None or self._fired or self._selector != (phase, boundary, side):
            return
        payload = (
            JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            .encode(
                {
                    "phase": phase,
                    "boundary": boundary,
                    "side": side,
                    "pid": os.getpid(),
                },
            )
            .encode()
            + b"\n"
        )
        invalid_write = False
        try:
            while payload:
                written = os.write(self._fd, payload)
                if written <= 0:
                    invalid_write = True
                    break
                payload = payload[written:]
            if not invalid_write:
                os.fsync(self._fd)
        except BaseException:
            self.close()
            raise
        if invalid_write:
            self.close()
            raise ValueError
        self.close()
        self._fired = True
        os.kill(os.getpid(), signal.SIGSTOP)

    def transition(self, label: str) -> None:
        if self._selector is None:
            return
        phase, boundary, side = self._selector
        if label == boundary:
            self._armed = side == "post"
            if side == "pre":
                self.offer(phase, boundary, side)
        elif label == "commit" and self._armed:
            self.offer(phase, boundary, side)

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


class _LatchedAdmission:
    def __init__(
        self,
        admission: IngestionBatchAdmissionView,
        latch: _CanaryLatch,
    ) -> None:
        self._admission = admission
        self._latch = latch

    async def admit_ingestion_batch(
        self,
        admission: ej.IngestionBatchAdmission,
    ) -> ej.AdmissionResult:
        self._latch.offer("admission", _CANARY_BOUNDARIES["admission"], "pre")
        result = await self._admission.admit_ingestion_batch(admission)
        self._latch.offer("admission", _CANARY_BOUNDARIES["admission"], "post")
        return result


async def run_durable_ingestion(bot: AgentBot) -> None:  # noqa: PLR0915
    """Run the exact-agent durable Classic ingestion loop."""
    client = bot.client
    rooms = bot.approval_room_ids
    if (
        os.getenv("MINDROOM_INGESTION_CANARY_AGENT") != bot.agent_name
        or bot.config.matrix_sync.mode != "classic"
        or bot.config.event_journal.backend != "sqlite"
        or not isinstance(client, nio.AsyncClient)
        or len(rooms) != 1
    ):
        raise ValueError
    account_id, device_id, access_token = (
        client.user_id,
        client.device_id,
        client.access_token,
    )
    if (
        type(account_id) is not str
        or type(device_id) is not str
        or type(access_token) is not str
    ):
        raise ValueError
    if not account_id or not device_id or not access_token:
        raise ValueError
    room_id = next(iter(rooms))
    if type(room_id) is not str or not room_id:
        raise ValueError
    latch = _CanaryLatch.from_environment(storage_root=bot.runtime_paths.storage_root)
    try:
        blocked = {"not_types": ["*"]}
        filter_json = (
            JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            .encode(
                {
                    "account_data": blocked,
                    "presence": blocked,
                    "room": {
                        "account_data": blocked,
                        "ephemeral": blocked,
                        "rooms": [room_id],
                        "state": blocked,
                        "timeline": {
                            "limit": 1,
                            "not_senders": [account_id],
                            "types": ["m.room.encrypted", "m.room.message"],
                        },
                    },
                },
            )
            .encode()
        )
        source = ClassicSourceConfig(timeout_ms=30_000, filter_json=filter_json)
        ingestion_config = IngestionConfig(source)
        nio_parent = bot.runtime_paths.storage_root / "tracking" / "nio_ingestion"
        nio_parent.mkdir(parents=True, exist_ok=True)
        principal = bot._journal_principal()
        latch.offer("consumer", _CANARY_BOUNDARIES["consumer"], "pre")
        consumer = await principal.load_or_create_ingestion_consumer(
            new_generation=uuid4(),
        )
        latch.offer("consumer", _CANARY_BOUNDARIES["consumer"], "post")
        latch.offer("bootstrap", _CANARY_BOUNDARIES["bootstrap"], "pre")
        bootstrap = nio_store.open_ingestion_store(
            nio_parent,
            account_id=account_id,
            device_id=device_id,
            consumer_generation=consumer.generation,
            source=source,
            database_name=f"{bot.agent_name}.db",
            transition_statement_hook=latch.transition,
        )
        try:
            latch.offer("bootstrap", _CANARY_BOUNDARIES["bootstrap"], "post")
            latch.offer("bind", _CANARY_BOUNDARIES["bind"], "pre")
            bound = await principal.bind_ingestion_stream(
                generation=consumer.generation,
                stream_id=bootstrap.stream_id,
            )
            latch.offer("bind", _CANARY_BOUNDARIES["bind"], "post")
            session = nio.open_ingestion(
                client,
                bootstrap,
                config=ingestion_config,
                consumer_generation=consumer.generation,
                stream_id=bound.stream_id,
                room_id=room_id,
            )
        except BaseException:
            bootstrap.close()
            raise
        async with session:

            async def pump() -> None:
                while True:
                    result = await consume_one_ingestion_batch(
                        session,
                        _LatchedAdmission(principal, latch),
                        account_id=account_id,
                        device_id=device_id,
                    )
                    if result is None:
                        latch.offer("idle", _CANARY_BOUNDARIES["idle"], "post")
                        await asyncio.sleep(0.05)
                    else:
                        bot._journal_dispatcher.wake()

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(session.run())
                tasks.create_task(pump())
    finally:
        latch.close()
