"""Keyed Matrix sends bound shared storage and stalled network operations."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import nio
import pytest

from mindroom.custom_tools import matrix_message_idempotency as durable
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.file_locks import file_lock_is_held
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_matrix_message_idempotency import context as context  # noqa: PLC0414
from tests.test_matrix_message_idempotency import transport as transport  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import AsyncMock

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from tests.test_matrix_message_idempotency import MatrixTransport

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]


def _state_path(context: ToolRuntimeContext) -> Path:
    root = context.runtime_paths.control_state_root
    assert root is not None
    return next((root / "matrix_message_sends").glob("*.json"))


async def test_stalled_transport_times_out_and_releases_scope(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost response cannot monopolize the scope or lose its frozen transaction."""
    monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 0.05, raising=False)
    accepted = asyncio.Event()
    never = asyncio.Event()

    async def stall_after_acceptance(**kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
        response = await transport.room_send(**kwargs)
        accepted.set()
        await never.wait()
        return response

    cast("AsyncMock", context.client.room_send).side_effect = stall_after_acceptance
    with tool_runtime_context(context):
        first_task = asyncio.create_task(
            MatrixMessageTools().matrix_message(message="first", idempotency_key="stalled"),
        )
        async with asyncio.timeout(1):
            await accepted.wait()
            first = json.loads(await first_task)
        assert first["status"] == "error"
        assert first["message"] == "Idempotent Matrix send timed out; retry with the same idempotency_key."
        row = next(iter(json.loads(_state_path(context).read_bytes())["sends"].values()))
        assert row["event_id"] is row["completed_at"] is None
        assert row["payload"]["body"] == "first"
        monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 60)
        cast("AsyncMock", context.client.room_send).side_effect = transport.room_send
        other = json.loads(await MatrixMessageTools().matrix_message(message="other", idempotency_key="other"))
        retry = json.loads(await MatrixMessageTools().matrix_message(message="changed", idempotency_key="stalled"))
    assert other["status"] == retry["status"] == "ok"
    assert len(transport.events) == 2
    assert transport.attempts[0] == transport.attempts[-1]
    assert retry["event_id"] == f"${row['transaction_id']}"


@pytest.mark.parametrize("pending", [False, True])
async def test_full_scope_rejects_new_keys_but_recovers_existing(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,
) -> None:
    """Count limits gate only admission, retaining every existing send identity."""
    monkeypatch.setattr(durable, "_MAX_RETAINED_RECORDS", 1, raising=False)
    monkeypatch.setattr(durable, "_MAX_PENDING_RECORDS", 1, raising=False)
    transport.lose_response = pending
    with tool_runtime_context(context):
        first = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="existing"))
        full = json.loads(await MatrixMessageTools().matrix_message(message="new", idempotency_key="new"))
        assert full["status"] == "error"
        assert "capacity" in full["message"]
        assert len(transport.attempts) == 1
        existing = json.loads(await MatrixMessageTools().matrix_message(message="changed", idempotency_key="existing"))
    assert existing["status"] == "ok"
    assert len(transport.events) == 1
    if not pending:
        assert first == existing


async def test_pending_capacity_reopens_after_completion(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settling an uncertain send frees pending capacity without evicting its receipt."""
    monkeypatch.setattr(durable, "_MAX_PENDING_RECORDS", 1, raising=False)
    transport.lose_response = True
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="pending")
        full = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="new"))
        assert full["status"] == "error"
        assert len(transport.events) == 1
        await MatrixMessageTools().matrix_message(message="retry", idempotency_key="pending")
        admitted = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="new"))
    assert admitted["status"] == "ok"
    assert len(transport.events) == 2


async def test_expired_receipt_pruning_frees_admission(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed retention can free a full scope while pending sends remain retained."""
    monkeypatch.setattr(durable, "_MAX_RETAINED_RECORDS", 1, raising=False)
    now = time.time()
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="expired")
        full = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="new"))
        assert full["status"] == "error"
        monkeypatch.setattr(durable.time, "time", lambda: now + 9 * 86400)
        admitted = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="new"))
    assert admitted["status"] == "ok"
    assert len(transport.events) == 2
    assert len(json.loads(_state_path(context).read_bytes())["sends"]) == 1


async def test_byte_capacity_reserves_receipt_completion(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-full scope reserves room for its pending receipt and rejects another key."""
    monkeypatch.setattr(durable, "_MAX_STORE_BYTES", 3100, raising=False)
    transport.lose_response = True

    async def maximum_receipt(**kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
        await transport.room_send(**kwargs)
        return nio.RoomSendResponse("$" + "\x01" * 254, kwargs["room_id"])

    cast("AsyncMock", context.client.room_send).side_effect = maximum_receipt
    with tool_runtime_context(context):
        first = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="bytes"))
        assert first["status"] == "error"
        assert len(transport.events) == 1
        saved = _state_path(context).read_bytes()
        full = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="new"))
        assert full["status"] == "error"
        assert "capacity" in full["message"]
        assert _state_path(context).read_bytes() == saved
        assert len(transport.attempts) == 1
        completed = json.loads(await MatrixMessageTools().matrix_message(message="retry", idempotency_key="bytes"))
        replay = json.loads(await MatrixMessageTools().matrix_message(message="replay", idempotency_key="bytes"))
    assert completed["status"] == "ok"
    assert replay == completed
    assert len(transport.events) == 1
    assert _state_path(context).stat().st_size <= 3100


async def test_oversized_payload_never_writes_or_sends(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual JSON escaping counts toward the durable byte limit before send."""
    monkeypatch.setattr(durable, "_MAX_STORE_BYTES", 3100, raising=False)
    with tool_runtime_context(context):
        result = json.loads(await MatrixMessageTools().matrix_message(message="\u2603" * 600, idempotency_key="large"))
    assert result["status"] == "error"
    assert "capacity" in result["message"]
    assert not transport.events
    root = context.runtime_paths.control_state_root
    assert root is not None
    assert not list((root / "matrix_message_sends").glob("*.json"))


async def test_oversized_store_fails_before_json_parse(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized on-disk data must fail at the size gate, even if malformed."""
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="existing")
        _state_path(context).write_bytes(b"x" * 501)
        monkeypatch.setattr(durable, "_MAX_STORE_BYTES", 500, raising=False)
        result = json.loads(await MatrixMessageTools().matrix_message(message="retry", idempotency_key="existing"))
    assert result["status"] == "error"
    assert result["message"] == "Matrix message receipt store exceeds the size limit."
    assert len(transport.attempts) == 1


async def test_lock_wait_uses_the_claim_deadline(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting behind another key has the same bounded lifetime as transport."""
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="first")
        monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 0.05)
        async with durable.async_exclusive_file_lock(_state_path(context).with_suffix(".lock")):
            async with asyncio.timeout(1):
                result = json.loads(
                    await MatrixMessageTools().matrix_message(message="second", idempotency_key="second"),
                )
    assert result["status"] == "error"
    assert result["message"] == "Idempotent Matrix send timed out; retry with the same idempotency_key."
    assert len(transport.events) == 1


async def test_deadline_settles_durable_write_before_unlocking(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deadline cancellation cannot release a claim while its writer still owns state."""
    original_write = durable.write_json_file_durable
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def stalled_write(path: Path, payload: object, **kwargs: Any) -> None:  # noqa: ANN401
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), "test did not release durable writer"
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(durable, "write_json_file_durable", stalled_write)
    monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 0.05)
    with tool_runtime_context(context):
        pending = asyncio.create_task(MatrixMessageTools().matrix_message(message="first", idempotency_key="writing"))
        try:
            await entered.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
            assert not pending.done()
            root = context.runtime_paths.control_state_root
            assert root is not None
            assert file_lock_is_held(next((root / "matrix_message_sends").glob("*.lock")))
            assert not transport.events
        finally:
            release.set()
        result = json.loads(await pending)
        assert result["status"] == "error"
        assert "timed out" in result["message"]
        monkeypatch.setattr(durable, "write_json_file_durable", original_write)
        monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 60)
        recovered = json.loads(await MatrixMessageTools().matrix_message(message="changed", idempotency_key="writing"))
    assert recovered["status"] == "ok"
    assert len(transport.events) == 1
    assert transport.attempts[0]["content"]["body"] == "first"


async def test_ordinary_send_does_not_use_keyed_deadline(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an immediately expired keyed deadline must leave ordinary sends unchanged."""
    monkeypatch.setattr(durable, "_CLAIM_TIMEOUT_SECONDS", 0)
    with tool_runtime_context(context):
        result = json.loads(await MatrixMessageTools().matrix_message(message="ordinary"))
    assert result["status"] == "ok"
    assert len(transport.events) == 1


async def test_oversized_matrix_event_id_cannot_consume_reserved_space(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
) -> None:
    """An invalid server receipt must leave the bounded pending intent recoverable."""

    async def oversized_receipt(**kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
        await transport.room_send(**kwargs)
        return nio.RoomSendResponse("$" + "\u2603" * 85, kwargs["room_id"])

    cast("AsyncMock", context.client.room_send).side_effect = oversized_receipt
    with tool_runtime_context(context):
        result = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="invalid-id"))
    assert result["status"] == "error"
    assert "invalid event ID" in result["message"]
    assert len(transport.events) == 1
    row = next(iter(json.loads(_state_path(context).read_bytes())["sends"].values()))
    assert row["event_id"] is None
    assert row["payload"]["body"] == "first"
