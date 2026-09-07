"""Late Nio decryption refines application work without blocking later input."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import nio
import pytest
from nio.crypto import InboundGroupSession, OlmAccount, OutboundGroupSession
from nio.durable import DurableSyncConfig, RecordKind, SlidingSyncConfig, SyncBatch, SyncRecord, open_durable_sync
from nio.durable.model import CryptoEvidence

from mindroom.event_journal import EventKind, IngestionBatchIntegrityError
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from tests.journal_membership_helpers import admit_room_membership
from tests.test_durable_ingestion_admission import Session, principal_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore

ACCOUNT = "@bot:example.org"
SENDER = "@alice:example.org"
ROOM = "!room:example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("provenance", list(nio.TimelineEventProvenance))
async def test_real_nio_late_decryption_preserves_work_and_history_silence(
    journal_store: EventJournalStore,
    tmp_path: Path,
    provenance: nio.TimelineEventProvenance,
) -> None:
    """An unresolved encrypted event must not poison the stream when its key arrives."""
    principal = journal_store.principal(ACCOUNT)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    client = nio.AsyncClient("https://example.org", ACCOUNT, device_id="DEVICE")
    client.restore_login(ACCOUNT, "DEVICE", "token")
    session = open_durable_sync(
        client,
        consumer_id=consumer.generation,
        store_path=tmp_path / "crypto",
        config=DurableSyncConfig(sliding=SlidingSyncConfig()),
    )
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=session.stream_id)
    frames: asyncio.Queue[bytes] = asyncio.Queue()

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/messages?" in path:
            query = parse_qs(urlparse(path).query)
            return json.dumps({"start": query["from"][0], "end": query.get("to", [None])[0], "chunk": []}).encode()
        return await frames.get()

    session._transport.request = request
    session._maintain_crypto = AsyncMock()
    runner = asyncio.create_task(session.run())

    async def send_frame(position: str, room: dict[str, object]) -> None:
        await frames.put(json.dumps({"pos": position, "rooms": {ROOM: room}}).encode())
        async with asyncio.timeout(5):
            while True:
                batch = await session.next_batch()
                if batch is None:
                    await session.wait_for_work()
                    continue
                await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT)
                if batch.completes_sync:
                    return

    try:
        await send_frame(
            "baseline",
            {
                "initial": True,
                "membership": "join",
                "prev_batch": "old",
                "timeline": [],
                "required_state": [
                    {
                        "type": "m.room.member",
                        "event_id": "$join",
                        "sender": ACCOUNT,
                        "state_key": ACCOUNT,
                        "origin_server_ts": 1,
                        "content": {"membership": "join"},
                    },
                ],
            },
        )
        encrypted, inbound = _encrypted_thread_message()
        await send_frame("ciphertext", {"num_live": 1, "timeline": [encrypted]})
        with session._outbound.transaction():
            assert client.olm is not None
            assert client.olm.inbound_group_store.add(inbound)
            client.olm.save_inbound_group_session(inbound)
        await send_frame(
            "decrypted",
            {
                "expanded_timeline": provenance is nio.TimelineEventProvenance.HISTORY,
                "limited": provenance is nio.TimelineEventProvenance.RECOVERED,
                "prev_batch": "new",
                "num_live": int(provenance is nio.TimelineEventProvenance.LIVE),
                "timeline": [encrypted],
            },
        )
        clear = await principal.load_event("$late-key")
        assert clear is not None
        assert clear.kind is EventKind.MESSAGE
        assert clear.thread_id == "$thread"
        assert await principal.is_pending("$late-key") is (provenance is not nio.TimelineEventProvenance.HISTORY)
        await principal.settle("$late-key")
        await send_frame("duplicate", {"num_live": 1, "timeline": [encrypted]})
        assert not await principal.is_pending("$late-key")
        await send_frame(
            "next",
            {
                "num_live": 1,
                "timeline": [
                    {
                        "type": "m.room.message",
                        "event_id": "$next",
                        "sender": SENDER,
                        "origin_server_ts": 11,
                        "content": {"msgtype": "m.text", "body": "next request"},
                    },
                ],
            },
        )
        assert await principal.is_pending("$next")
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await session.close()
        await client.close()


def _encrypted_thread_message() -> tuple[dict[str, object], InboundGroupSession]:
    """Encrypt one threaded message and return its matching inbound key."""
    peer = OlmAccount()
    outbound = OutboundGroupSession()
    inbound = InboundGroupSession(
        outbound.session_key,
        peer.identity_keys["ed25519"],
        peer.identity_keys["curve25519"],
        ROOM,
    )
    outbound.mark_as_shared()
    ciphertext = outbound.encrypt(
        json.dumps(
            {
                "room_id": ROOM,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "late key",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
                },
            },
        ),
    )
    encrypted = {
        "type": "m.room.encrypted",
        "event_id": "$late-key",
        "sender": SENDER,
        "origin_server_ts": 10,
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "sender_key": peer.identity_keys["curve25519"],
            "device_id": "ALICE",
            "session_id": outbound.id,
            "ciphertext": ciphertext,
        },
    }
    return encrypted, inbound


def _ciphertext(provenance: nio.TimelineEventProvenance) -> SyncRecord:
    return SyncRecord(
        RecordKind.TIMELINE,
        ROOM,
        {
            "type": "m.room.encrypted",
            "event_id": "$cipher",
            "sender": SENDER,
            "origin_server_ts": 10,
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "sender_key": "sender-key",
                "device_id": "ALICE",
                "session_id": "session",
                "ciphertext": "AgAAAA",
            },
        },
        provenance=provenance,
        membership_epoch=0,
    )


def _decrypted(record: SyncRecord) -> SyncRecord:
    return replace(
        record,
        clear={
            "type": "m.room.message",
            "event_id": "$cipher",
            "sender": SENDER,
            "origin_server_ts": 10,
            "content": {
                "msgtype": "m.text",
                "body": "old request",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        },
        crypto=CryptoEvidence(False, "sender-key", "session"),
    )


@pytest.mark.asyncio
async def test_historical_ciphertext_cannot_become_a_new_request(journal_store: EventJournalStore) -> None:
    """Historical silence outlives Nio's bounded duplicate-observation window."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    history = _ciphertext(nio.TimelineEventProvenance.HISTORY)
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (history,))), principal, account_id=ACCOUNT)
    clear = _decrypted(replace(history, provenance=nio.TimelineEventProvenance.LIVE))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (clear,))), principal, account_id=ACCOUNT)
    assert not await principal.is_pending("$cipher")
    page = await principal.read_conversation(room_id=ROOM, thread_id="$thread", limit=10)
    assert [event.content["body"] for event in page.messages] == ["old request"]
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 3, (clear,))), principal, account_id=ACCOUNT)
    assert not await principal.is_pending("$cipher")


@pytest.mark.asyncio
async def test_historical_ciphertext_stays_silent_after_restart(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """The old-event identity survives process loss without retaining ciphertext."""
    stream = uuid4()
    store = journal_database()
    principal = await principal_for(store, stream)
    history = _ciphertext(nio.TimelineEventProvenance.HISTORY)
    batch = SyncBatch(stream, 1, (history,))
    await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    await store.close()
    principal = await principal_for(journal_database(), stream)
    await consume_one_ingestion_batch(Session(batch), principal, account_id=ACCOUNT)
    retained = await principal.load_event("$cipher")
    assert retained is not None
    assert retained.kind is EventKind.OPAQUE_HISTORY
    assert retained.source == {}
    clear = _decrypted(replace(history, provenance=nio.TimelineEventProvenance.RECOVERED))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (clear,))), principal, account_id=ACCOUNT)
    assert not await principal.is_pending("$cipher")
    page = await principal.read_conversation(room_id=ROOM, thread_id="$thread", limit=10)
    assert [event.content["body"] for event in page.messages] == ["old request"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("sender", "@mallory:example.org"), ("origin_server_ts", 99)])
@pytest.mark.parametrize("opaque_first", [False, True])
async def test_opaque_duplicate_requires_the_same_envelope(
    journal_store: EventJournalStore,
    field: str,
    value: object,
    opaque_first: bool,
) -> None:
    """Accepting newly readable content must not weaken immutable sender identity."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    opaque = _ciphertext(nio.TimelineEventProvenance.HISTORY)
    clear = _decrypted(replace(opaque, provenance=nio.TimelineEventProvenance.LIVE))
    first, second = (opaque, clear) if opaque_first else (clear, opaque)
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (first,))), principal, account_id=ACCOUNT)
    second = replace(
        second,
        source={**second.source, field: value},
        clear=None if second.clear is None else {**second.clear, field: value},
    )
    session = Session(SyncBatch(stream, 2, (second,)))
    with pytest.raises(IngestionBatchIntegrityError):
        await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT)
    assert session.acked == []
    assert await principal.is_pending("$cipher") is not opaque_first


@pytest.mark.asyncio
async def test_opaque_reobservation_cannot_downgrade_decrypted_work(journal_store: EventJournalStore) -> None:
    """A changed crypto trust decision must not poison or settle an admitted request."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    clear = _decrypted(_ciphertext(nio.TimelineEventProvenance.LIVE))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (clear,))), principal, account_id=ACCOUNT)
    opaque = replace(clear, clear=None, crypto=None, provenance=nio.TimelineEventProvenance.HISTORY)
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (opaque,))), principal, account_id=ACCOUNT)
    retained = await principal.load_event("$cipher")
    assert retained is not None
    assert retained.kind is EventKind.MESSAGE
    assert retained.thread_id == "$thread"
    assert await principal.is_pending("$cipher")


@pytest.mark.asyncio
async def test_late_historical_decryption_respects_departure_and_rejoin(journal_store: EventJournalStore) -> None:
    """A late key cannot repopulate departed context or revive work after rejoin."""
    stream = uuid4()
    principal = await principal_for(journal_store, stream)
    await admit_room_membership(principal, ROOM, "join")
    history = _ciphertext(nio.TimelineEventProvenance.HISTORY)
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 2, (history,))), principal, account_id=ACCOUNT)
    await admit_room_membership(principal, ROOM, "leave")
    clear = _decrypted(replace(history, membership_epoch=1, provenance=nio.TimelineEventProvenance.LIVE))
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 4, (clear,))), principal, account_id=ACCOUNT)
    page = await principal.read_conversation(room_id=ROOM, thread_id="$thread", limit=10)
    assert not page.messages
    await admit_room_membership(principal, ROOM, "join")
    await consume_one_ingestion_batch(Session(SyncBatch(stream, 6, (clear,))), principal, account_id=ACCOUNT)
    assert not await principal.is_pending("$cipher")
    page = await principal.read_conversation(room_id=ROOM, thread_id="$thread", limit=10)
    assert [event.content["body"] for event in page.messages] == ["old request"]
