"""Configured byte budgets govern the real durable Matrix input stream."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING
from uuid import uuid4

import nio
import pytest
from nio.durable import DurableSyncConfig, open_durable_sync
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.matrix.sync_loop import bot_ingestion_config

if TYPE_CHECKING:
    from pathlib import Path

    from aioresponses import aioresponses


@pytest.mark.parametrize("field", ["max_response_bytes", "max_pending_bytes"])
@pytest.mark.parametrize("value", [0, -1])
def test_sync_byte_limits_require_positive_values(field: str, value: int) -> None:
    """Reject invalid byte budgets during configuration loading."""
    with pytest.raises(ValidationError) as error:
        Config.model_validate({"matrix_sync": {field: value}})
    assert error.value.errors()[0]["type"] == "greater_than_equal"
    assert error.value.errors()[0]["loc"] == ("matrix_sync", field)


def test_omitted_sync_byte_limits_preserve_durable_defaults() -> None:
    """Existing configurations retain the library's input and pending budgets."""
    configured = MatrixSyncConfig()
    defaults = DurableSyncConfig()
    assert configured.max_response_bytes == defaults.max_response_bytes
    assert configured.max_pending_bytes == defaults.max_pending_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["classic", "sliding"])
@pytest.mark.parametrize(
    ("limits", "large_response", "error_message"),
    [
        ({}, True, "HTTP response exceeds the durable input bound"),
        ({"max_response_bytes": 32 * 1024 * 1024}, True, None),
        ({"max_pending_bytes": 1024}, False, "prepared output exceeds the durable pending bound"),
        ({"max_pending_bytes": 32 * 1024}, False, None),
    ],
    ids=["default-response-bound", "raised-response-bound", "small-pending-bound", "sufficient-pending-bound"],
)
async def test_sync_session_enforces_configured_byte_limits(
    tmp_path: Path,
    aioresponse: aioresponses,
    mode: str,
    limits: dict[str, int],
    large_response: bool,
    error_message: str | None,
) -> None:
    """YAML settings control HTTP reads and durable event admission in both modes."""
    config = Config.model_validate({"matrix_sync": {"mode": mode, **limits}})
    ingestion = bot_ingestion_config(config, agent_name="general", room_ids=[], timeout_ms=0, sync_filter={})
    client = nio.AsyncClient("https://example.org", "@bot:example.org", device_id="DEVICE")
    client.restore_login("@bot:example.org", "DEVICE", "token")
    session = open_durable_sync(client, consumer_id=uuid4(), store_path=tmp_path / "sync", config=ingestion)
    event = {
        "type": "m.room.message",
        "event_id": "$message",
        "sender": "@alice:example.org",
        "origin_server_ts": 1,
        "content": {"msgtype": "m.text", "body": "x" * 8192},
    }
    frame = (
        {
            "pos": "next",
            "rooms": {
                "!room:example.org": {
                    "initial": True,
                    "membership": "join",
                    "required_state": [],
                    "timeline": [event],
                },
            },
        }
        if mode == "sliding"
        else {
            "next_batch": "next",
            "rooms": {
                "join": {
                    "!room:example.org": {
                        "state": {"events": []},
                        "timeline": {"limited": False, "events": [event]},
                    },
                },
            },
        }
    )
    # Exercise a response larger than the default without an oversized event.
    if large_response:
        frame["padding"] = "x" * (16 * 1024 * 1024)
    aioresponse.add(
        re.compile(r"https://example\.org/_matrix/client/.*sync.*"),
        method="POST" if mode == "sliding" else "GET",
        body=json.dumps(frame),
    )
    runner = asyncio.create_task(session.run())
    try:
        async with asyncio.timeout(5):
            if error_message is not None:
                with pytest.raises(nio.LocalProtocolError, match=error_message):
                    await runner
            else:
                while True:
                    batch = await session.next_batch()
                    if batch is not None:
                        if any(record.source == event for record in batch.records):
                            break
                        assert not batch.completes_sync
                        await session.ack(batch)
                        continue
                    if runner.done():
                        await runner
                        pytest.fail("Sync runner stopped before publishing the event")
                    await session.wait_for_work()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await session.close()
        await client.close()
