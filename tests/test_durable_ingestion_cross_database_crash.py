"""A crash between application commit and nio ack safely redelivers a batch."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import nio
import pytest
from nio.durable import DurableSync, SyncBatch, open_durable_sync

from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import (
        EventJournalStore,
    )

ACCOUNT = "@bot:example.org"
ROOM = "!room:example.org"


@pytest.mark.asyncio
async def test_real_owned_batch_replays_after_admission_before_nio_ack(  # noqa: PLR0915 - one crash lifecycle  # noqa: PLR0915
    tmp_path: Path,
    journal_database: Callable[[], EventJournalStore],
) -> None:
    journal = journal_database()
    principal = journal.principal(ACCOUNT)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    body = json.dumps(
        {
            "next_batch": "cursor-one",
            "rooms": {
                "join": {
                    ROOM: {
                        "state": {"events": []},
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "event_id": "$durable",
                                    "sender": "@alice:example.org",
                                    "origin_server_ts": 100,
                                    "content": {"msgtype": "m.text", "body": "retained"},
                                },
                            ],
                            "limited": False,
                            "prev_batch": "previous",
                        },
                    },
                },
            },
        },
    ).encode()

    def open_session() -> tuple[nio.AsyncClient, DurableSync]:
        client = nio.AsyncClient("https://example.org", ACCOUNT, device_id="DEVICE")
        client.restore_login(ACCOUNT, "DEVICE", "token")
        session = open_durable_sync(client, consumer_id=consumer.generation, store_path=tmp_path / "crypto")
        return client, session

    client, session = open_session()
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=session.stream_id)
    delivered = False

    async def request(*_args: object, **_kwargs: object) -> bytes:
        nonlocal delivered
        if delivered:
            await asyncio.Event().wait()
        delivered = True
        return body

    session._transport.request = request
    session._maintain_crypto = AsyncMock()
    runner = asyncio.create_task(session.run())
    real_ack = session.ack
    crashed_batch = None

    async def crash_before_ack(batch: SyncBatch) -> None:
        nonlocal crashed_batch
        if any(record.source.get("event_id") == "$durable" for record in batch.records):
            crashed_batch = batch
            message = "crash before ack"
            raise RuntimeError(message)
        await real_ack(batch)

    session.ack = crash_before_ack
    try:
        with pytest.raises(RuntimeError, match="crash before ack"):  # noqa: PT012 - drive source to failure
            async with asyncio.timeout(3):
                while True:
                    result = await consume_one_ingestion_batch(session, principal, account_id=ACCOUNT)
                    if result is None:
                        await session.wait_for_work()
        assert await principal.load_event("$durable") is not None
        assert crashed_batch is not None
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        await session.close()
        await client.close()

    client, reopened = open_session()
    try:
        batch = await reopened.next_batch()
        assert batch == crashed_batch
        callbacks = []
        client.add_event_callback(lambda _room, event: callbacks.append(event), nio.RoomMessageText)
        result = await consume_one_ingestion_batch(reopened, principal, account_id=ACCOUNT)
        assert result is not None
        assert not result.receipt_new
        assert not result.semantic_event_new
        assert callbacks == []
        assert (await principal.load_event("$durable")).event_id == "$durable"
    finally:
        await reopened.close()
        await client.close()
