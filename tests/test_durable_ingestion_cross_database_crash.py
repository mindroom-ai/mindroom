"""Cross-repository crash and transport parity proofs for durable ingestion."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import nio
import pytest
from nio.client.async_client import AsyncClientConfig
from nio.crypto import OlmAccount
from nio.ingest import coordinator
from nio.ingest.classic import ClassicSource
from nio.ingest.config import ClassicSourceConfig, IngestionConfig, SlidingSourceConfig
from nio.ingest.hydration import normalize_hydration_response
from nio.ingest.model import EventRecord, RecordKind, TimelineEventProvenance
from nio.ingest.ports import NetworkRequest, NetworkResult, StagedSourceResponse
from nio.ingest.serialization import batch_from_records
from nio.ingest.sliding import RESERVED_ALL_ROOMS_LIST, SlidingSource
from nio.ingest.source import canonical_json
from nio.ingest.state import SourceState, StagedFrame
from nio.store import SqliteStore, sync_journal
from nio.store._sync_journal_values import MaterializerLimits, MaterializeStatus

from mindroom.event_journal import EventKind, IngestionBatchIntegrityError
from mindroom.matrix.durable_ingestion import (
    consume_one_ingestion_batch,
    validate_ingestion_batch,
)
from mindroom.pending_event_worker import PendingEventWorker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, JournalEvent, PrincipalStore


ACCOUNT = "@alice:example.org"
DEVICE = "ALICE"
BOB = "@bob:example.org"
ROOM = "!durable:example.org"
PICKLE_KEY = "task7-secret"
DATABASE_NAME = "task7-owned.db"
GLOBAL_EVENT_TYPE = "org.example.task7"
CLASSIC_SOURCE = ClassicSourceConfig(30_000, b"{}")
SLIDING_SOURCE = SlidingSourceConfig(30_000, "task7", b"{}", b"{}", b"{}")
_ACK_CRASH_MESSAGE = "crash after MindRoom admission before nio ack"
_DUPLICATE_CALLBACK_MESSAGE = "receipt replay invoked a second application callback"
_UNREACHABLE_MESSAGE = "unreachable"


class _AckCrashError(RuntimeError):
    pass


def _config(transport: str) -> IngestionConfig:
    return IngestionConfig(CLASSIC_SOURCE if transport == "classic" else SLIDING_SOURCE)


def _member_event() -> dict[str, object]:
    return {
        "content": {"displayname": "Alice", "membership": "join"},
        "event_id": "$task7-member",
        "origin_server_ts": 1,
        "sender": ACCOUNT,
        "state_key": ACCOUNT,
        "type": "m.room.member",
    }


def _message_event() -> dict[str, object]:
    return {
        "content": {"body": "task7 durable event", "msgtype": "m.text"},
        "event_id": "$task7-message",
        "origin_server_ts": 2,
        "sender": BOB,
        "type": "m.room.message",
    }


def _response_body(
    transport: str,
    request_body: bytes | None,
    *,
    empty: bool,
    one_time_key_count: int,
) -> dict[str, object]:
    key_counts = {"signed_curve25519": one_time_key_count}
    if transport == "classic":
        rooms: dict[str, object] = {}
        if not empty:
            rooms = {
                "join": {
                    ROOM: {
                        "state": {"events": [_member_event()]},
                        "timeline": {
                            "events": [_message_event()],
                            "limited": False,
                        },
                    },
                },
            }
        return {
            "account_data": {"events": []},
            "device_lists": {"changed": [], "left": []},
            "device_one_time_keys_count": key_counts,
            "device_unused_fallback_key_types": [],
            "next_batch": "s1" if empty else "s2",
            "presence": {"events": []},
            "rooms": rooms,
            "to_device": {"events": []},
        }

    assert transport == "sliding"
    assert request_body is not None
    transaction_id = json.loads(request_body)["txn_id"]
    sliding_rooms: dict[str, object] = {}
    if not empty:
        sliding_rooms = {
            ROOM: {
                "limited": False,
                "membership": "join",
                "num_live": 1,
                "required_state": [_member_event()],
                "timeline": [_message_event()],
            },
        }
    return {
        "extensions": {
            "account_data": {"global": [], "rooms": {}},
            "e2ee": {
                "device_lists": {"changed": [], "left": []},
                "device_one_time_keys_count": key_counts,
                "device_unused_fallback_key_types": [],
            },
            "presence": {"events": []},
            "to_device": {"events": [], "next_batch": "td1" if empty else "td2"},
            "typing": {"rooms": {}},
        },
        "lists": {RESERVED_ALL_ROOMS_LIST: {"count": 0 if empty else 1}},
        "pos": "p1" if empty else "p2",
        "rooms": sliding_rooms,
        "txn_id": transaction_id,
    }


def _open_bootstrap(
    store_path: Path,
    generation: UUID,
    config: IngestionConfig,
) -> sync_journal.StoreBootstrap:
    store_path.mkdir(parents=True)
    seed = SqliteStore(
        ACCOUNT,
        DEVICE,
        str(store_path),
        pickle_key=PICKLE_KEY,
        database_name=DATABASE_NAME,
    )
    seed.save_account(OlmAccount())
    seed.database.close()
    return sync_journal._open_configured_ingestion_store(
        store_path,
        source_store_class=SqliteStore,
        owned_store_class=SqliteStore,
        account_id=ACCOUNT,
        device_id=DEVICE,
        consumer_generation=generation,
        source=config.source,
        pickle_key=PICKLE_KEY,
        database_name=DATABASE_NAME,
    )


def _reopen_bootstrap(
    store_path: Path,
    generation: UUID,
    config: IngestionConfig,
) -> sync_journal.StoreBootstrap:
    return sync_journal._open_configured_ingestion_store(
        store_path,
        source_store_class=SqliteStore,
        owned_store_class=SqliteStore,
        account_id=ACCOUNT,
        device_id=DEVICE,
        consumer_generation=generation,
        source=config.source,
        pickle_key=PICKLE_KEY,
        database_name=DATABASE_NAME,
    )


def _client(store_path: Path) -> nio.AsyncClient:
    client = nio.AsyncClient(
        "https://example.org",
        ACCOUNT,
        DEVICE,
    )
    client.restore_login(ACCOUNT, DEVICE, "task7-token")
    client.store_path = str(store_path)
    client.config = AsyncClientConfig(
        store=SqliteStore,
        pickle_key=PICKLE_KEY,
        store_name=DATABASE_NAME,
    )
    return client


def _open_session(
    client: nio.AsyncClient,
    bootstrap: sync_journal.StoreBootstrap,
    generation: UUID,
    config: IngestionConfig,
) -> coordinator._OwnedIngestionSession:
    return coordinator._open_owned_ingestion(
        client,
        bootstrap,
        config=config,
        consumer_generation=generation,
        stream_id=bootstrap.stream_id,
    )


def _stage_response(
    session: object,
    config: IngestionConfig,
    transport: str,
    *,
    empty: bool,
) -> StagedFrame:
    journal = session._journal
    owner = journal.load_owner()
    prior = journal.load_source()
    adapter = (
        ClassicSource(owner.stream_id, config.source, owner.account_id)
        if transport == "classic"
        else SlidingSource(owner.stream_id, config.source, owner.account_id)
    )
    request = adapter.plan_request(prior, prior.next_request_id)
    assert request is not None
    olm = session._client.olm
    assert olm is not None
    body = _response_body(
        transport,
        request.body,
        empty=empty,
        one_time_key_count=olm.account.max_one_time_keys,
    )
    normalized = adapter.normalize(
        request,
        NetworkResult(
            request.stream_id,
            request.transport,
            request.source_epoch,
            request.request_id,
            200,
            canonical_json(body),
            None,
            None,
        ),
    )
    assert normalized.frame is not None
    frame = StagedFrame(
        normalized.frame.frame_id,
        StagedSourceResponse(
            request,
            normalized.response_body,
            normalized.frame.source_sha256,
        ),
    )
    committed = journal.stage_source_response(
        source=SourceState(
            prior.source_epoch,
            prior.transport_kind,
            normalized.frame.candidate_cursor_json,
            request.request_id + 1,
            prior.active,
        ),
        frame=frame,
    )
    return replace(frame, staged_revision=committed.revision)


async def _prepare_target(
    session: object,
    config: IngestionConfig,
    transport: str,
) -> StagedFrame:
    olm = session._client.olm
    assert olm is not None
    olm.account.shared = True
    olm.uploaded_key_count = olm.account.max_one_time_keys
    olm.save_account()

    warm = _stage_response(session, config, transport, empty=True)
    assert session._materialize_oldest_frame(limits=MaterializerLimits()).status is MaterializeStatus.MATERIALIZED
    assert await session._advance_blocked_frame(warm.frame_id)
    assert session._journal.load_frame(warm.frame_id) is None

    target = _stage_response(session, config, transport, empty=False)
    assert session._materialize_oldest_frame(limits=MaterializerLimits()).status is MaterializeStatus.MATERIALIZED
    pending = session._journal.load_pending_hydrations(limit=2)
    assert len(pending) == 1
    hydrated = normalize_hydration_response(
        pending[0],
        own_user_id=ACCOUNT,
        response_body=canonical_json([_member_event()]),
    )
    assert session._journal.apply_hydration_result(result=hydrated) is not None
    return target


async def _bind_principal(
    store: EventJournalStore,
    generation: UUID,
    stream_id: UUID,
) -> PrincipalStore:
    principal = store.principal(ACCOUNT)
    await principal.load_or_create_ingestion_consumer(new_generation=generation)
    await principal.bind_ingestion_stream(generation=generation, stream_id=stream_id)
    return principal


async def _semantic_signature(principal: PrincipalStore) -> tuple[object, ...]:
    page = await principal.pending()
    assert len(page) == 1
    event = page[0]
    return (
        event.event_id,
        event.room_id,
        event.kind,
        event.sender,
        event.origin_server_ts,
        event.source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_real_owned_batch_replays_after_admission_before_nio_ack(  # noqa: PLR0915
    tmp_path: Path,
    journal_database: Callable[[], EventJournalStore],
    transport: str,
) -> None:
    """A committed MindRoom receipt survives a failed nio ack on both stores."""
    generation = uuid4()
    config = _config(transport)
    nio_path = tmp_path / transport
    bootstrap = _open_bootstrap(nio_path, generation, config)
    client = _client(nio_path)
    session = _open_session(client, bootstrap, generation, config)
    mindroom_store = journal_database()
    principal = await _bind_principal(
        mindroom_store,
        generation,
        bootstrap.stream_id,
    )
    callback_events: list[str] = []

    async def on_event(room: nio.MatrixRoom, event: nio.Event) -> None:
        assert room is client.rooms[ROOM]
        assert session._journal._owner.database.connection().in_transaction is False
        assert len(await principal.pending()) == 1
        callback_events.append(event.event_id)

    client.add_event_callback(on_event, nio.Event)
    target = await _prepare_target(session, config, transport)
    while True:
        batch = session.next_batch(max_records=1)
        assert batch is not None
        record = batch.records[0]
        if type(record) is EventRecord and record.kind is RecordKind.TIMELINE:
            assert record.event_id == "$task7-message"
            assert record.provenance is TimelineEventProvenance.LIVE
            break
        prefix = await consume_one_ingestion_batch(
            session,
            principal,
            account_id=ACCOUNT,
            device_id=DEVICE,
        )
        assert prefix is not None
        assert prefix.receipt_new is True
        assert prefix.semantic_event_new is False
        assert callback_events == []
    assert session.next_batch(max_records=1) == batch
    assert session._journal.load_frame(target.frame_id) is None
    retained = session._journal._load_authenticated_frame_headers(session._journal.load_owner())
    assert tuple(header.frame_id for header in retained) == (target.frame_id,)

    def crash_first_ack(label: str) -> None:
        if label == "before_commit":
            raise _AckCrashError(_ACK_CRASH_MESSAGE)

    session._journal.set_transition_statement_hook(crash_first_ack)
    try:
        with pytest.raises(_AckCrashError, match="after MindRoom admission"):
            await consume_one_ingestion_batch(
                session,
                principal,
                account_id=ACCOUNT,
                device_id=DEVICE,
            )
        assert callback_events == ["$task7-message"]
        assert await _semantic_signature(principal) == (
            "$task7-message",
            ROOM,
            EventKind.MESSAGE,
            BOB,
            2,
            _message_event(),
        )
        original = batch.records[0]
        assert type(original) is EventRecord
        conflicting_source = _message_event()
        conflicting_source["content"] = {
            "body": "conflicting durable event",
            "msgtype": "m.text",
        }
        competing = batch_from_records(
            account_id=batch.account_id,
            device_id=batch.device_id,
            consumer_generation=batch.consumer_generation,
            stream_id=batch.ref.stream_id,
            sequence=batch.ref.sequence,
            created_revision=batch.created_revision,
            records=(
                replace(
                    original,
                    source_json=canonical_json(conflicting_source),
                ),
            ),
        )
        assert competing.ref.sha256 != batch.ref.sha256
        with pytest.raises(IngestionBatchIntegrityError):
            await principal.admit_ingestion_batch(
                validate_ingestion_batch(
                    competing,
                    account_id=ACCOUNT,
                    device_id=DEVICE,
                ),
            )
        assert await _semantic_signature(principal) == (
            "$task7-message",
            ROOM,
            EventKind.MESSAGE,
            BOB,
            2,
            _message_event(),
        )
        assert session.next_batch(max_records=1) == batch
    finally:
        session._journal.set_transition_statement_hook(None)
        await session.close()

    reopened_bootstrap = _reopen_bootstrap(nio_path, generation, config)
    reopened_client = _client(nio_path)
    reopened_session = _open_session(
        reopened_client,
        reopened_bootstrap,
        generation,
        config,
    )

    async def forbid_duplicate_callback(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        raise AssertionError(_DUPLICATE_CALLBACK_MESSAGE)

    reopened_client.add_event_callback(forbid_duplicate_callback, nio.Event)
    try:
        replay = await consume_one_ingestion_batch(
            reopened_session,
            principal,
            account_id=ACCOUNT,
            device_id=DEVICE,
        )
        assert replay is not None
        assert (replay.receipt_new, replay.semantic_event_new) == (False, False)
        assert reopened_session.next_batch(max_records=1) is None
        assert await _semantic_signature(principal) == (
            "$task7-message",
            ROOM,
            EventKind.MESSAGE,
            BOB,
            2,
            _message_event(),
        )
        headers = reopened_session._journal._load_authenticated_frame_headers(reopened_session._journal.load_owner())
        assert tuple(header.frame_id for header in headers) == (target.frame_id,)
        dispatched: list[tuple[object, ...]] = []

        async def dispatch(event: JournalEvent) -> bool:
            dispatched.append(
                (
                    event.event_id,
                    event.room_id,
                    event.kind,
                    event.sender,
                    event.origin_server_ts,
                    event.source,
                ),
            )
            return True

        worker = PendingEventWorker(store=principal, handle=dispatch)
        assert await worker.drain_once() == 1
        assert dispatched == [
            (
                "$task7-message",
                ROOM,
                EventKind.MESSAGE,
                BOB,
                2,
                _message_event(),
            ),
        ]
        assert await worker.drain_once() == 0
        assert await principal.pending() == ()
    finally:
        await reopened_session.close()


@pytest.mark.asyncio
async def test_sliding_unknown_position_after_admission_before_nio_ack(  # noqa: PLR0915
    tmp_path: Path,
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """A positioned reset follows receipt replay without duplicating effects."""
    generation = uuid4()
    config = _config("sliding")
    nio_path = tmp_path / "sliding-reset"
    bootstrap = _open_bootstrap(nio_path, generation, config)
    client = _client(nio_path)
    session = _open_session(client, bootstrap, generation, config)
    mindroom_store = journal_database()
    principal = await _bind_principal(
        mindroom_store,
        generation,
        bootstrap.stream_id,
    )
    callback_events: list[str] = []

    async def on_event(_room: nio.MatrixRoom, event: nio.Event) -> None:
        callback_events.append(event.event_id)

    client.add_event_callback(on_event, nio.Event)
    target = await _prepare_target(session, config, "sliding")
    while True:
        batch = session.next_batch(max_records=1)
        assert batch is not None
        record = batch.records[0]
        if type(record) is EventRecord and record.kind is RecordKind.TIMELINE:
            break
        facts = await consume_one_ingestion_batch(
            session,
            principal,
            account_id=ACCOUNT,
            device_id=DEVICE,
        )
        assert facts is not None
        assert (facts.receipt_new, facts.semantic_event_new) == (True, False)

    def crash_first_ack(label: str) -> None:
        if label == "before_commit":
            raise _AckCrashError(_ACK_CRASH_MESSAGE)

    session._journal.set_transition_statement_hook(crash_first_ack)
    try:
        with pytest.raises(_AckCrashError, match="after MindRoom admission"):
            await consume_one_ingestion_batch(
                session,
                principal,
                account_id=ACCOUNT,
                device_id=DEVICE,
            )
    finally:
        session._journal.set_transition_statement_hook(None)
    assert callback_events == ["$task7-message"]
    assert session.next_batch(max_records=1) == batch

    replay = await consume_one_ingestion_batch(
        session,
        principal,
        account_id=ACCOUNT,
        device_id=DEVICE,
    )
    assert replay is not None
    assert (replay.receipt_new, replay.semantic_event_new) == (False, False)
    assert callback_events == ["$task7-message"]
    assert session.next_batch(max_records=1) is None

    journal = session._journal
    owner_before = journal.load_owner()
    source_before = journal.load_source()
    positioned_cursor = json.loads(source_before.cursor_json)
    assert positioned_cursor["pos"] == "p2"
    assert positioned_cursor["to_device_since"] == "td2"
    adapter = SlidingSource(
        owner_before.stream_id,
        config.source,
        owner_before.account_id,
    )
    positioned_request = adapter.plan_request(
        source_before,
        source_before.next_request_id,
    )
    assert positioned_request is not None
    requests: list[NetworkRequest] = []
    replanned = asyncio.Event()

    async def request(
        source_request: NetworkRequest,
        **_kwargs: object,
    ) -> NetworkResult:
        requests.append(source_request)
        if len(requests) == 1:
            assert source_request == positioned_request
            return session._network_result(
                source_request,
                400,
                b'{"errcode":"M_UNKNOWN_POS"}',
            )
        replanned.set()
        await asyncio.Event().wait()
        raise AssertionError(_UNREACHABLE_MESSAGE)

    session._request = request
    run_task = asyncio.create_task(session.run())
    try:
        async with asyncio.timeout(5):
            await replanned.wait()
        assert len(requests) == 2
        committed_owner = journal.load_owner()
        committed_source = journal.load_source()
        reset_cursor = json.loads(committed_source.cursor_json)
        assert committed_owner.next_source_epoch == owner_before.next_source_epoch + 1
        assert committed_source.source_epoch == owner_before.next_source_epoch
        assert committed_source.next_request_id == 0
        assert reset_cursor["pos"] is None
        assert reset_cursor["to_device_since"] == "td2"
        expected = adapter.plan_request(
            committed_source,
            committed_source.next_request_id,
        )
        assert requests[1] == expected
        assert journal._load_authenticated_frame_headers(committed_owner) == ()
        assert await _semantic_signature(principal) == (
            "$task7-message",
            ROOM,
            EventKind.MESSAGE,
            BOB,
            2,
            _message_event(),
        )
    finally:
        await session.close()
        await asyncio.gather(
            run_task,
            return_exceptions=True,
        )

    reopened_bootstrap = _reopen_bootstrap(nio_path, generation, config)
    reopened_client = _client(nio_path)
    reopened_session = _open_session(
        reopened_client,
        reopened_bootstrap,
        generation,
        config,
    )
    try:
        reopened_source = reopened_session._journal.load_source()
        reopened_cursor = json.loads(reopened_source.cursor_json)
        assert reopened_cursor["pos"] is None
        assert reopened_cursor["to_device_since"] == "td2"
        assert reopened_session.next_batch(max_records=1) is None
        assert reopened_session._journal._load_authenticated_frame_headers(reopened_session._journal.load_owner()) == ()
        assert callback_events == ["$task7-message"]
        assert target.frame_id not in tuple(
            header.frame_id
            for header in reopened_session._journal._load_authenticated_frame_headers(
                reopened_session._journal.load_owner(),
            )
        )
    finally:
        await reopened_session.close()
