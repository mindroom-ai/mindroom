"""Durable Matrix sends retain one event across retries and process restarts."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import nio
import pytest

from mindroom.custom_tools import matrix_message_idempotency as durable
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.matrix.state import MatrixState
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import make_latest_thread_event_id_mock
from tests.test_matrix_agent_discovery import context as context  # noqa: PLC0414

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from unittest.mock import AsyncMock

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]


class MatrixTransport:
    """Record actual transport events, deduplicating Matrix transaction IDs."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.lose_response = False

    async def room_send(self, **kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
        """Accept a transport event before optionally losing its response."""
        self.attempts.append(json.loads(json.dumps(kwargs)))
        transaction = kwargs.get("tx_id") or f"ordinary-{len(self.attempts)}"
        self.events.setdefault(transaction, json.loads(json.dumps(kwargs)))
        if self.lose_response:
            self.lose_response = False
            msg = "response lost after acceptance"
            raise TimeoutError(msg)
        return nio.RoomSendResponse(f"${transaction}", kwargs["room_id"])


@pytest.fixture
def transport(context: ToolRuntimeContext) -> MatrixTransport:
    """Use real Matrix preparation and delivery with an in-memory homeserver."""
    MatrixMessageTools._recent_actions.clear()
    context.client.device_id = "DEVICE"
    context.client.olm = None
    context.client.user_id = "@actual_general:localhost"
    cast(
        "AsyncMock",
        context.conversation_reader.latest_thread_event_id,
    ).side_effect = make_latest_thread_event_id_mock().side_effect
    server = MatrixTransport()
    cast("AsyncMock", context.client.room_send).side_effect = server.room_send
    return server


async def test_repeated_key_replays_first_event(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
) -> None:
    """Changed text and a fresh tool instance must return the first receipt."""
    with tool_runtime_context(context):
        first = json.loads(
            await MatrixMessageTools().matrix_message(
                message="first",
                recipient="general",
                new_thread=True,
                idempotency_key="event-1",
            ),
        )
        second = json.loads(
            await MatrixMessageTools().matrix_message(
                message="changed",
                recipient="code",
                idempotency_key="event-1",
            ),
        )
    assert first["status"] == "ok"
    assert second == first
    assert first["thread_id"] == first["event_id"]
    assert len(transport.events) == len(transport.attempts) == 1
    assert next(iter(transport.events.values()))["content"]["body"].endswith("first")


@pytest.mark.parametrize("failure", ["transport", "receipt", "receipt_fsync"])
async def test_retry_after_uncertain_delivery_preserves_payload(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Lost acknowledgement or receipt cannot create a second transport event."""
    original_write = durable.write_json_file_durable

    def fail_receipt(path: Path, payload: object, **kwargs: Any) -> None:  # noqa: ANN401
        completed = any(row["event_id"] is not None for row in cast("dict", payload)["sends"].values())
        if not completed or failure == "receipt_fsync":
            original_write(path, payload, **kwargs)
        if completed:
            msg = "receipt storage unavailable"
            raise OSError(msg)

    if failure == "transport":
        transport.lose_response = True
    else:
        monkeypatch.setattr(durable, "write_json_file_durable", fail_receipt)
    with tool_runtime_context(context):
        first = json.loads(
            await MatrixMessageTools().matrix_message(
                message="original @code",
                recipient="general",
                new_thread=True,
                idempotency_key="retry",
            ),
        )
        assert first["status"] == "error"
        assert len(transport.events) == 1
        if failure != "transport":
            still_unavailable = json.loads(
                await MatrixMessageTools().matrix_message(message="changed", idempotency_key="retry"),
            )
            assert still_unavailable["status"] == "error"
        monkeypatch.setattr(durable, "write_json_file_durable", original_write)
        second = json.loads(
            await MatrixMessageTools().matrix_message(
                message="changed",
                recipient="code",
                thread_id="room",
                idempotency_key="retry",
            ),
        )
    assert second["status"] == "ok"
    assert second["thread_id"] == second["event_id"]
    assert len(transport.events) == 1
    assert all(attempt == transport.attempts[0] for attempt in transport.attempts)
    assert transport.attempts[0]["content"]["m.mentions"] == {"user_ids": ["@actual_general:localhost"]}
    assert "m.relates_to" not in transport.attempts[0]["content"]


async def test_concurrent_retries_have_one_transport_event(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
) -> None:
    """Independent tool instances serialize receipt preparation and delivery."""
    with tool_runtime_context(context):
        results = await asyncio.gather(
            *(
                MatrixMessageTools().matrix_message(message=f"text {index}", idempotency_key="concurrent")
                for index in range(6)
            ),
        )
    assert all(json.loads(result)["status"] == "ok" for result in results)
    assert len(set(results)) == len(transport.events) == len(transport.attempts) == 1


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("field", ["user_id", "device_id"])
async def test_changed_matrix_identity_fails_closed(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    pending: bool,
    field: str,
) -> None:
    """A different Matrix transaction scope must never acknowledge or resend a claim."""
    transport.lose_response = pending
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="identity")
        setattr(context.client, field, "changed")
        result = json.loads(await MatrixMessageTools().matrix_message(message="retry", idempotency_key="identity"))
    assert result["status"] == "error"
    assert "sender or device" in result["message"]
    assert len(transport.attempts) == 1


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("revoke", ["actor", "recipient"])
async def test_revoked_authority_is_rechecked_for_stored_target(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    pending: bool,
    revoke: str,
) -> None:
    """Neither pending payloads nor completed receipts bypass current permissions."""
    transport.lose_response = pending
    config = context.config.model_copy(deep=True)
    live_context = replace(context, config_provider=lambda: config)
    with tool_runtime_context(live_context):
        await MatrixMessageTools().matrix_message(message="first", recipient="code", idempotency_key="revoked")
        access = config.agents["general" if revoke == "actor" else "code"].access
        assert access is not None
        access.users = []
        result = json.loads(await MatrixMessageTools().matrix_message(message="retry", idempotency_key="revoked"))
    assert result["status"] == "error"
    assert len(transport.attempts) == 1


async def test_requester_aliases_share_receipts(context: ToolRuntimeContext, transport: MatrixTransport) -> None:
    """Canonical requester identity scopes receipts even across bridge aliases."""
    context.config.authorization.aliases = {"@alice:localhost": ["@bridge_alice:localhost"]}
    with tool_runtime_context(replace(context, requester_id="@bridge_alice:localhost")):
        first = await MatrixMessageTools().matrix_message(message="first", idempotency_key="alias")
    with tool_runtime_context(context):
        second = await MatrixMessageTools().matrix_message(message="second", idempotency_key="alias")
    assert first == second
    assert json.loads(first)["status"] == "ok"
    assert len(transport.events) == 1


@pytest.mark.parametrize("pending", [False, True])
async def test_completed_receipts_expire_but_pending_sends_do_not(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,
) -> None:
    """Eight days bounds completed deduplication without expiring uncertain sends."""
    now = time.time()
    transport.lose_response = pending
    with tool_runtime_context(context):
        first = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="retention"))
        monkeypatch.setattr(durable.time, "time", lambda: now + 9 * 86400)
        second = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="retention"))
    assert second["status"] == "ok"
    assert len(transport.events) == (1 if pending else 2)
    if not pending:
        assert first["event_id"] != second["event_id"]
    assert transport.attempts[-1]["content"]["body"] == ("first" if pending else "second")
    root = context.runtime_paths.control_state_root
    assert root is not None
    stored = json.loads(next((root / "matrix_message_sends").glob("*.json")).read_text())
    assert len(stored["sends"]) == 1
    assert next(iter(stored["sends"].values()))["payload"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"idempotency_key": ""},
        {"idempotency_key": " "},
        {"idempotency_key": "x" * 257},
        {"idempotency_key": 7},
        {"action": "read", "idempotency_key": "key"},
        {"action": "edit", "event_id": "$event", "idempotency_key": "key"},
        {"action": "react", "event_id": "$event", "idempotency_key": "key"},
        {"attachments": ["att_file"], "idempotency_key": "key"},
        {"message": " ", "idempotency_key": "key"},
    ],
)
async def test_invalid_keyed_sends_never_send(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    arguments: dict[str, Any],
) -> None:
    """Only valid text sends may create durable send state or transport events."""
    with tool_runtime_context(context):
        result = json.loads(await MatrixMessageTools().matrix_message(**{"message": "text", **arguments}))
    assert result["status"] == "error"
    assert not transport.events


async def test_unkeyed_repeats_still_send_each_time(context: ToolRuntimeContext, transport: MatrixTransport) -> None:
    """Ordinary sends retain their existing non-idempotent behavior."""
    with tool_runtime_context(context):
        first = json.loads(await MatrixMessageTools().matrix_message(message="same"))
        second = json.loads(await MatrixMessageTools().matrix_message(message="same"))
    assert first["status"] == second["status"] == "ok"
    assert first["event_id"] != second["event_id"]
    assert len(transport.events) == 2


async def test_cancellation_after_acceptance_preserves_pending_send(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
) -> None:
    """Cancellation releases the lock but keeps the accepted event's transaction."""
    accepted = asyncio.Event()
    never = asyncio.Event()

    async def pause_response(**kwargs: Any) -> nio.RoomSendResponse:  # noqa: ANN401
        response = await transport.room_send(**kwargs)
        accepted.set()
        await never.wait()
        return response

    cast("AsyncMock", context.client.room_send).side_effect = pause_response
    with tool_runtime_context(context):
        pending = asyncio.create_task(MatrixMessageTools().matrix_message(message="first", idempotency_key="cancel"))
        await accepted.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        cast("AsyncMock", context.client.room_send).side_effect = transport.room_send
        receipt = json.loads(await MatrixMessageTools().matrix_message(message="changed", idempotency_key="cancel"))
    assert receipt["status"] == "ok"
    assert len(transport.events) == 1
    assert len(transport.attempts) == 2
    assert transport.attempts[0] == transport.attempts[1]


async def test_room_alias_reuses_receipt(context: ToolRuntimeContext, transport: MatrixTransport) -> None:
    """Room names and resolved IDs must address the same receipt scope."""
    state = MatrixState()
    state.add_room("lobby", room_id=context.room_id, alias="#lobby:localhost", name="Lobby")
    state.save(runtime_paths=context.runtime_paths)
    with tool_runtime_context(context):
        first = await MatrixMessageTools().matrix_message(message="first", room_id="lobby", idempotency_key="room")
        second = await MatrixMessageTools().matrix_message(
            message="second",
            room_id=context.room_id,
            idempotency_key="room",
        )
    assert first == second
    assert json.loads(first)["status"] == "ok"
    assert len(transport.attempts) == 1


@pytest.mark.parametrize("scope", ["requester", "agent", "room"])
async def test_distinct_scopes_do_not_share_receipts(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    scope: str,
) -> None:
    """The same opaque key must not conflate independent sending principals or rooms."""
    with tool_runtime_context(context):
        first = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="scoped"))
    changed = context
    room_id = context.room_id
    if scope == "requester":
        access = context.config.agents["general"].access
        assert access is not None
        access.users.append("@bob:localhost")
        changed = replace(context, requester_id="@bob:localhost")
    elif scope == "agent":
        changed = replace(context, agent_name="code")
    else:
        room_id = "!other:localhost"
        context.client.rooms[room_id] = nio.MatrixRoom(room_id, context.client.user_id)
    with tool_runtime_context(changed):
        second = json.loads(
            await MatrixMessageTools().matrix_message(message="second", room_id=room_id, idempotency_key="scoped"),
        )
    assert first["status"] == second["status"] == "ok"
    assert first["event_id"] != second["event_id"]
    assert len(transport.events) == 2


async def test_invalid_store_never_resends(context: ToolRuntimeContext, transport: MatrixTransport) -> None:
    """Unreadable persisted state cannot silently discard a prior identity."""
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(message="first", idempotency_key="corrupt")
        root = context.runtime_paths.control_state_root
        assert root is not None
        next((root / "matrix_message_sends").glob("*.json")).write_text("not json")
        receipt = json.loads(await MatrixMessageTools().matrix_message(message="second", idempotency_key="corrupt"))
    assert receipt["status"] == "error"
    assert len(transport.attempts) == 1


async def test_recipient_mode_change_preserves_prepared_thread(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
) -> None:
    """Current recipient availability must not rewrite the first prepared conversation."""
    transport.lose_response = True
    with tool_runtime_context(context):
        await MatrixMessageTools().matrix_message(
            message="first",
            recipient="general",
            new_thread=True,
            idempotency_key="thread-mode",
        )
        context.config.agents["general"].thread_mode = "room"
        receipt = json.loads(
            await MatrixMessageTools().matrix_message(message="changed", idempotency_key="thread-mode"),
        )
    assert receipt["status"] == "ok"
    assert receipt["thread_id"] == receipt["event_id"]
    assert len(transport.events) == 1
    assert transport.attempts[0] == transport.attempts[1]


async def test_alias_revocation_while_waiting_for_lock_fails_closed(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued send must resolve aliases from the authorization snapshot after locking."""
    context.config.authorization.aliases = {"@alice:localhost": ["@bridge_alice:localhost"]}
    original_lock = durable.async_exclusive_file_lock

    @asynccontextmanager
    async def revoke_alias_after_lock(path: Path) -> AsyncIterator[None]:
        async with original_lock(path):
            context.config.authorization.aliases = {}
            yield

    monkeypatch.setattr(durable, "async_exclusive_file_lock", revoke_alias_after_lock)
    with tool_runtime_context(replace(context, requester_id="@bridge_alice:localhost")):
        receipt = json.loads(await MatrixMessageTools().matrix_message(message="first", idempotency_key="queued"))
    assert receipt["status"] == "error"
    assert not transport.events


async def test_pending_write_uncertainty_prevents_retry_transport(
    context: ToolRuntimeContext,
    transport: MatrixTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible pending row must be durably reaffirmed before any retry can send it."""
    original_write = durable.write_json_file_durable
    pending_rows: list[dict[str, Any]] = []

    def fail_durability(path: Path, payload: object, **kwargs: Any) -> None:  # noqa: ANN401
        row = next(iter(cast("dict", payload)["sends"].values()))
        if row["event_id"] is None:
            pending_rows.append(json.loads(json.dumps(row)))
            original_write(path, payload, **kwargs)
        msg = "durable storage unavailable"
        raise OSError(msg)

    monkeypatch.setattr(durable, "write_json_file_durable", fail_durability)
    with tool_runtime_context(context):
        for message in ("first", "changed", "changed again"):
            result = json.loads(
                await MatrixMessageTools().matrix_message(
                    message=message,
                    idempotency_key="pending-fsync",
                ),
            )
            assert result["status"] == "error"
            assert not transport.events
        assert len(pending_rows) == 3
        assert all(row == pending_rows[0] for row in pending_rows)
        monkeypatch.setattr(durable, "write_json_file_durable", original_write)
        result = json.loads(
            await MatrixMessageTools().matrix_message(
                message="recovered",
                idempotency_key="pending-fsync",
            ),
        )
    assert result["status"] == "ok"
    assert len(transport.events) == len(transport.attempts) == 1
    transaction_id = pending_rows[0]["transaction_id"]
    assert transport.attempts[0]["tx_id"] == transaction_id
    assert transport.attempts[0]["content"] == pending_rows[0]["payload"]
    assert result["event_id"] == f"${transaction_id}"
