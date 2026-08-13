"""Tests for Matrix-backed tool approval state."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

import mindroom.tool_approval as approval_module
from mindroom import approval_transport
from mindroom.approval_events import parse_approval_datetime
from mindroom.approval_manager import (
    ApprovalStartupSweep,
    PendingApproval,
    SentApprovalEvent,
    ToolApprovalTransportError,
    _ApprovalManager,
    _build_event_arguments_preview,
    _build_full_event_arguments,
    get_approval_store,
    initialize_approval_store,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry, mindroom_user_id
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.tool_approval import (
    ToolApprovalScriptError,
    _shutdown_approval_store,
    evaluate_tool_approval,
    resolve_tool_approval_approver,
    tool_requires_approval_for_openai_compat,
)
from mindroom.tools import approved_egress as _approved_egress  # noqa: F401 - registers the approval exemption
from tests.approval_test_support import (
    CLAIMING_DEVICE_ID,
    FakeApprovalCards,
    transaction_id_for,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping
    from pathlib import Path


def _recording_point_lookup(
    cards: FakeApprovalCards,
    seen: list[tuple[str, str]],
) -> Callable[..., Awaitable[dict[str, Any] | None]]:
    """Wrap the point lookup so a test can prove a scan was not used instead."""
    original = cards.pending_approval_card

    async def lookup(*, room_id: str, card_event_id: str) -> dict[str, Any] | None:
        seen.append((room_id, card_event_id))
        return await original(room_id=room_id, card_event_id=card_event_id)

    return lookup


def _recording_scan(
    cards: FakeApprovalCards,
    seen: list[str],
) -> Callable[..., Awaitable[tuple[dict[str, Any], ...]]]:
    original = cards.pending_approval_cards

    async def scan(*, room_id: str, limit: int = 256) -> tuple[dict[str, Any], ...]:
        seen.append(room_id)
        return await original(room_id=room_id, limit=limit)

    return scan


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    asyncio.run(_shutdown_approval_store())
    yield
    asyncio.run(_shutdown_approval_store())


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router", "code": "mindroom_code"})
    return config


def test_tool_approval_config_coerces_numeric_timeout_strings() -> None:
    """Pydantic should own normal numeric coercion for approval timeouts."""
    config = Config.model_validate(
        {
            "tool_approval": {
                "timeout_days": "7",
                "rules": [{"match": "read_*", "action": "require_approval", "timeout_days": "3"}],
            },
        },
    )

    assert config.tool_approval.timeout_days == 7.0
    assert config.tool_approval.rules[0].timeout_days == 3.0


@pytest.mark.parametrize(
    ("tool_approval", "expected_location"),
    [
        ({"timeout_days": True}, ("tool_approval", "timeout_days")),
        (
            {"rules": [{"match": "read_*", "action": "require_approval", "timeout_days": False}]},
            ("tool_approval", "rules", 0, "timeout_days"),
        ),
    ],
)
def test_tool_approval_config_rejects_boolean_timeout_days_with_nested_location(
    tool_approval: dict[str, object],
    expected_location: tuple[object, ...],
) -> None:
    """Only the bool edge case needs custom validation around Pydantic numeric fields."""
    with pytest.raises(ValidationError) as exc_info:
        Config.model_validate({"tool_approval": tool_approval})

    assert expected_location in {tuple(error["loc"]) for error in exc_info.value.errors(include_context=False)}


def _approval_card(
    *,
    approval_id: str = "approval-1",
    event_id: str = "$approval",
    room_id: str = "!room:localhost",
    sender: str = "@mindroom_router:localhost",
    requester: str = "@requester:localhost",
    approver: str = "@user:localhost",
    status: str = "pending",
    origin_server_ts: int | None = None,
    arguments_truncated: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    content: dict[str, Any] = {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approval required: read_file",
        "tool_name": "read_file",
        "tool_call_id": approval_id,
        "approval_id": approval_id,
        "arguments": {"path": "notes.txt"},
        "status": status,
        "requester_id": requester,
        "approver_user_id": approver,
        "agent_name": "code",
        "thread_id": "$thread",
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    if arguments_truncated:
        content["arguments_truncated"] = True
    return {
        "event_id": event_id,
        "room_id": room_id,
        "sender": sender,
        "type": "io.mindroom.tool_approval",
        "origin_server_ts": origin_server_ts or int(now.timestamp() * 1000),
        "content": content,
    }


def _approval_edit(
    card: dict[str, Any],
    *,
    event_id: str = "$approval-edit",
    sender: str | None = None,
    status: str = "approved",
) -> dict[str, Any]:
    content = {**card["content"], "status": status}
    return {
        "event_id": event_id,
        "room_id": card["room_id"],
        "sender": sender or card["sender"],
        "type": "io.mindroom.tool_approval",
        "origin_server_ts": int(card["origin_server_ts"]) + 1,
        "content": {
            **content,
            "m.new_content": content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": card["event_id"]},
        },
    }


async def _await_claim(cards: FakeApprovalCards, *, count: int = 1) -> None:
    """Wait until this many claim rows exist, without waiting for their sends."""
    async with asyncio.timeout(5):
        while len(cards.rows) < count:  # noqa: ASYNC110 - the store double has nothing to signal on
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_detached_card_displays_the_durable_winning_decision(tmp_path: Path) -> None:
    """A human click racing expiry must not overwrite the decision that released the continuation."""
    cards = FakeApprovalCards()
    editor = AsyncMock(return_value=True)
    decision_handler = AsyncMock(return_value=("expired", "Tool approval request timed out."))
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=decision_handler,
    )
    await store.create_detached_approval(
        approval_id="approval-1",
        continuation_id="continuation-1",
        tool_call_id="call-1",
        tool_name="dangerous",
        arguments={},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        timeout_seconds=30,
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.resolved is True
    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request timed out."
    assert editor.await_args.args[2]["resolved_by"] is None


@pytest.mark.asyncio
async def test_detached_transport_refusal_forgets_the_unsent_card_row(tmp_path: Path) -> None:
    """A fail-closed transport refusal must not block every later startup sweep."""
    cards = FakeApprovalCards()
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(side_effect=ToolApprovalTransportError("router does not manage this room")),
        editor=AsyncMock(return_value=True),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    with pytest.raises(ToolApprovalTransportError, match="router does not manage"):
        await store.create_detached_approval(
            approval_id="approval-refused",
            continuation_id="continuation-refused",
            tool_call_id="call-refused",
            tool_name="dangerous",
            arguments={},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        )

    assert cards.rows == {}


@pytest.mark.asyncio
async def test_missing_detached_card_notifies_continuation_owner(tmp_path: Path) -> None:
    """A membership-fenced card must fail its continuation instead of silently ending expiry."""
    cards = FakeApprovalCards()
    missing = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(),
        editor=AsyncMock(),
        cards=cards,
        detached_card_missing=missing,
    )

    settled = await store.expire_detached_card(
        room_id="!room:localhost",
        card_event_id="$departed-card",
    )

    assert settled is True
    missing.assert_awaited_once_with("$departed-card")


@pytest.mark.asyncio
async def test_detached_approval_expiry_resolves_continuation_without_waiter(tmp_path: Path) -> None:
    """Expiry must wake the durable continuation even though no response coroutine is waiting."""
    cards = FakeApprovalCards()
    decision_handler = AsyncMock(return_value=("expired", "Tool approval request timed out."))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=decision_handler,
    )

    await store.create_detached_approval(
        approval_id="approval-1",
        continuation_id="continuation-1",
        tool_call_id="call-1",
        tool_name="dangerous",
        arguments={},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        timeout_seconds=0,
    )
    for _attempt in range(20):
        if decision_handler.await_count:
            break
        await asyncio.sleep(0.01)

    decision_handler.assert_awaited_once_with(
        "continuation-1",
        "call-1",
        "expired",
        "Tool approval request timed out.",
    )
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_startup_reclaims_expiry_for_unresolved_continuation_card(tmp_path: Path) -> None:
    """The card coordinator alone must restore expiry ownership after restart."""
    cards = FakeApprovalCards()
    card = _approval_card()
    card["content"]["continuation_id"] = "continuation-1"
    card["content"]["tool_call_id"] = "call-1"
    await cards.store_card("$approval", "!room:localhost", card)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=AsyncMock(return_value=True),
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep.failed == 0
    assert store._detached_expiry_sweep_task is not None
    await store.shutdown(reason="test complete")


@pytest.mark.asyncio
async def test_startup_terminalizes_malformed_continuation_card_without_call_id(tmp_path: Path) -> None:
    """A malformed current-format card must fail closed instead of retrying forever."""
    cards = FakeApprovalCards()
    card = _approval_card()
    card["content"]["continuation_id"] = "continuation-1"
    card["content"].pop("tool_call_id")
    await cards.store_card("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep.complete is True
    assert cards.rows == {}
    assert store._detached_expiry_sweep_task is None
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_detached_approval_retries_recorded_expiry_until_card_edit_lands(tmp_path: Path) -> None:
    """A transient Matrix edit failure must not retire the only in-process expiry owner."""
    cards = FakeApprovalCards()
    editor = AsyncMock(side_effect=[False, True])
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=AsyncMock(return_value=("expired", "Tool approval request timed out.")),
        detached_decision_ready=AsyncMock(),
    )

    await store.create_detached_approval(
        approval_id="approval-retry",
        continuation_id="continuation-retry",
        tool_call_id="call-retry",
        tool_name="dangerous",
        arguments={},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        timeout_seconds=0,
    )
    for _attempt in range(100):
        if editor.await_count == 1:
            break
        await asyncio.sleep(0.01)
    await store._sweep_detached_expiries()

    assert editor.await_count == 2
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_detached_decision_retries_terminal_edit_without_waiting_for_deadline(tmp_path: Path) -> None:
    """A recorded human decision must keep retry ownership when its first Matrix edit fails."""
    cards = FakeApprovalCards()
    editor = AsyncMock(side_effect=[False, True])
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=AsyncMock(return_value=("approved", None)),
        detached_decision_ready=AsyncMock(),
    )
    await store.create_detached_approval(
        approval_id="approval-edit-retry",
        continuation_id="continuation-edit-retry",
        tool_call_id="call-edit-retry",
        tool_name="dangerous",
        arguments={},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        timeout_seconds=30,
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )
    await store._sweep_detached_expiries()

    assert result.resolved is False
    assert editor.await_count == 2
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_cancelled_detached_card_bind_expires_after_recovery(tmp_path: Path) -> None:
    """Cancellation after send must bind and immediately terminalize the delivered card."""
    cards = FakeApprovalCards()
    first_bind_started = asyncio.Event()
    release_first_bind = asyncio.Event()
    real_acknowledge = cards.acknowledge_approval_card
    bind_attempts = 0

    async def flaky_acknowledge(*args: object, **kwargs: object) -> object:
        nonlocal bind_attempts
        bind_attempts += 1
        if bind_attempts == 1:
            first_bind_started.set()
            await release_first_bind.wait()
            msg = "journal unavailable"
            raise RuntimeError(msg)
        return await real_acknowledge(*args, **kwargs)

    cards.acknowledge_approval_card = flaky_acknowledge  # type: ignore[method-assign]
    card_ready = AsyncMock(return_value=True)
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=AsyncMock(return_value=("expired", "expired")),
        detached_card_ready=card_ready,
    )
    create = asyncio.create_task(
        store.create_detached_approval(
            approval_id="approval-bind",
            continuation_id="continuation-bind",
            tool_call_id="call-bind",
            tool_name="dangerous",
            arguments={},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await first_bind_started.wait()

    create.cancel()
    release_first_bind.set()
    with pytest.raises(asyncio.CancelledError):
        await create
    for _attempt in range(100):
        if not cards.rows and editor.await_count:
            break
        await asyncio.sleep(0.01)

    assert bind_attempts >= 2
    card_ready.assert_awaited_with("continuation-bind", "call-bind", "$approval")
    assert cards.rows == {}
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_cancelled_detached_send_hands_delivered_event_to_recovery(tmp_path: Path) -> None:
    """Cancellation during send must terminally expire a card whose event id arrives afterward."""
    cards = FakeApprovalCards()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def sender(*_args: object) -> SentApprovalEvent:
        send_started.set()
        await release_send.wait()
        return SentApprovalEvent("$approval")

    card_ready = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=AsyncMock(return_value=True),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=AsyncMock(return_value=("expired", "expired")),
        detached_card_ready=card_ready,
    )
    create = asyncio.create_task(
        store.create_detached_approval(
            approval_id="approval-send",
            continuation_id="continuation-send",
            tool_call_id="call-send",
            tool_name="dangerous",
            arguments={},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await send_started.wait()

    create.cancel()
    await asyncio.sleep(0)
    release_send.set()
    with pytest.raises(asyncio.CancelledError):
        await create
    for _attempt in range(100):
        if card_ready.await_count and not cards.rows:
            break
        await asyncio.sleep(0.01)

    card_ready.assert_awaited_with("continuation-send", "call-send", "$approval")
    assert cards.rows == {}


def test_resolution_after_deadline_is_forced_to_expired() -> None:
    """A click queued before the expiry task runs must not authorize an already-expired request."""
    event = _approval_card()
    content = event["content"]
    assert isinstance(content, dict)
    content["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    pending = PendingApproval.from_card_event(event, room_id="!room:localhost")

    status, reason, truncated = _ApprovalManager._normalized_resolution_request(
        pending,
        status="approved",
        reason=None,
    )

    assert status == "expired"
    assert reason == "Tool approval request timed out."
    assert truncated is False


@pytest.mark.asyncio
async def test_detached_expiry_registration_cannot_escape_shutdown(tmp_path: Path) -> None:
    """Shutdown racing a successful send must still leave no detached expiry task behind."""
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def sender(*_args: object) -> SentApprovalEvent:
        send_started.set()
        await release_send.wait()
        return SentApprovalEvent("$approval")

    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=AsyncMock(return_value=True),
        cards=FakeApprovalCards(),
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        detached_decision_handler=AsyncMock(return_value=("expired", "shutdown")),
    )
    create = asyncio.create_task(
        store.create_detached_approval(
            approval_id="approval-1",
            continuation_id="continuation-1",
            tool_call_id="call-1",
            tool_name="dangerous",
            arguments={},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await send_started.wait()
    shutdown = asyncio.create_task(store.shutdown(reason="shutdown"))
    await asyncio.sleep(0)
    release_send.set()

    await create
    await shutdown
    try:
        assert store._detached_expiry_sweep_task is None
    finally:
        await store.shutdown(reason="test cleanup")


async def _wait_for_room_send_approval_id(client: MagicMock) -> str:
    async with asyncio.timeout(1):
        while True:
            if client.room_send.await_args is not None:
                return str(client.room_send.await_args.kwargs["content"]["approval_id"])
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_approval_transport_returns_event_after_successful_send_without_sender_user_id(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
    orchestrator._capture_runtime_loop()

    client = MagicMock()
    client.user_id = None
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$approval", room_id="!room:localhost"))
    bot = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    orchestrator.agent_bots = {"router": bot}
    orchestrator._approval_transport.cache_approval_event_now = AsyncMock()

    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "pending",
        },
        "txn-1",
    )

    assert sent == SentApprovalEvent(
        event_id="$approval",
        sent_content={
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "pending",
        },
    )


def _approval_transport_orchestrator(tmp_path: Path) -> tuple[_MultiAgentOrchestrator, MagicMock]:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
    orchestrator._capture_runtime_loop()

    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$approval", room_id="!room:localhost"))
    bot = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    orchestrator.agent_bots = {"router": bot}
    orchestrator._approval_transport.cache_approval_event_now = AsyncMock()
    return orchestrator, client


@pytest.mark.asyncio
async def test_approval_transport_keeps_small_full_arguments_inline(tmp_path: Path) -> None:
    orchestrator, client = _approval_transport_orchestrator(tmp_path)
    client.upload = AsyncMock()

    content = {
        "approval_id": "approval-1",
        "tool_name": "write_file",
        "arguments": {"content": "preview"},
        "arguments_truncated": True,
        "full_arguments": {"content": "x" * 2_000},
        "status": "pending",
    }
    sent = await orchestrator._approval_transport.send_approval_event_now("!room:localhost", None, content, "txn-1")

    assert sent is not None
    assert sent.sent_content == content
    client.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_transport_offloads_oversized_full_arguments_to_sidecar(tmp_path: Path) -> None:
    orchestrator, client = _approval_transport_orchestrator(tmp_path)
    client.upload = AsyncMock(return_value=(nio.UploadResponse("mxc://localhost/full-args"), None))

    full_arguments = {"content": "word " * 20_000}
    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "write_file",
            "arguments": {"content": "preview"},
            "arguments_truncated": True,
            "full_arguments": full_arguments,
            "status": "pending",
        },
        "txn-1",
    )

    assert sent is not None
    sent_content = client.room_send.await_args.kwargs["content"]
    assert "full_arguments" not in sent_content
    assert sent_content["full_arguments_url"] == "mxc://localhost/full-args"
    assert sent_content["full_arguments_info"]["mimetype"] == "application/json"
    assert sent.sent_content == sent_content

    uploaded_bytes = client.upload.await_args.kwargs["data_provider"](None, None).read()
    assert json.loads(uploaded_bytes) == full_arguments


@pytest.mark.asyncio
async def test_approval_transport_offloads_encrypted_full_arguments_to_file_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, client = _approval_transport_orchestrator(tmp_path)
    client.rooms["!room:localhost"].encrypted = True
    mxc_uri = "mxc://localhost/encrypted-full-args"
    file_info = {
        "url": mxc_uri,
        "key": {"alg": "A256CTR", "k": "secret", "key_ops": ["encrypt", "decrypt"], "kty": "oct"},
        "iv": "iv-value",
        "hashes": {"sha256": "sha256-value"},
        "v": "v2",
        "size": 100_014,
        "mimetype": "application/json",
    }
    upload_sidecar = AsyncMock(return_value=(mxc_uri, file_info))
    monkeypatch.setattr("mindroom.approval_transport.upload_json_sidecar", upload_sidecar)

    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "write_file",
            "arguments": {"content": "preview"},
            "arguments_truncated": True,
            "full_arguments": {"content": "word " * 20_000},
            "status": "pending",
        },
        "txn-1",
    )

    assert sent is not None
    sent_content = client.room_send.await_args.kwargs["content"]
    assert sent_content["full_arguments_file"] == file_info
    assert "full_arguments" not in sent_content
    assert "full_arguments_url" not in sent_content
    assert "full_arguments_info" not in sent_content
    assert sent.sent_content == sent_content


@pytest.mark.asyncio
async def test_approval_sidecar_uses_remote_encryption_state_during_cache_rebuild() -> None:
    """A Classic cache reset cannot downgrade complete approval arguments to plaintext."""
    client = MagicMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.olm = MagicMock()
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            {"algorithm": "m.megolm.v1.aes-sha2"},
            "m.room.encryption",
            "",
            "!room:localhost",
        ),
    )
    client.upload = AsyncMock(return_value=(nio.UploadResponse("mxc://localhost/full-args"), None))
    full_arguments = {"content": "secret " * 20_000}

    offloaded = await approval_transport._offload_oversized_full_arguments(
        client,
        "!room:localhost",
        {
            "approval_id": "approval-1",
            "full_arguments": full_arguments,
            "approvable": True,
        },
    )

    client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.encryption")
    assert "full_arguments_url" not in offloaded
    assert offloaded["full_arguments_file"]["url"] == "mxc://localhost/full-args"
    upload = client.upload.await_args.kwargs
    assert upload["content_type"] == "application/octet-stream"
    assert json.dumps(full_arguments).encode() not in upload["data_provider"](None, None).read()


@pytest.mark.asyncio
async def test_approval_transport_marks_card_non_approvable_when_sidecar_upload_fails(tmp_path: Path) -> None:
    orchestrator, client = _approval_transport_orchestrator(tmp_path)
    client.upload = AsyncMock(return_value=(nio.UploadError("boom"), None))

    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "write_file",
            "arguments": {"content": "preview"},
            "arguments_truncated": True,
            "full_arguments": {"content": "word " * 20_000},
            "status": "pending",
        },
        "txn-1",
    )

    assert sent is not None
    sent_content = client.room_send.await_args.kwargs["content"]
    assert "full_arguments" not in sent_content
    assert "full_arguments_url" not in sent_content
    assert sent_content["approvable"] is False
    assert sent.sent_content == sent_content


@pytest.mark.asyncio
async def test_approval_notice_replies_to_room_mode_card(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
    orchestrator._capture_runtime_loop()

    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$notice", room_id="!room:localhost"))
    bot = MagicMock(
        agent_name="router",
        running=True,
        client=client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    orchestrator.agent_bots = {"router": bot}

    sent = await orchestrator._approval_transport.send_notice(
        room_id="!room:localhost",
        approval_event_id="$approval",
        thread_id=None,
        reason="Cannot approve: the displayed arguments are truncated.",
    )

    assert sent is True
    assert client.room_send.await_args.kwargs["content"]["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "$approval"},
    }


@pytest.mark.asyncio
async def test_approval_thread_relation_uses_requesting_agent_cache(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
    orchestrator._capture_runtime_loop()
    sent_contents: list[dict[str, Any]] = []

    async def room_send(
        *,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        ignore_unverified_devices: bool = False,
        tx_id: str | None = None,
    ) -> nio.RoomSendResponse:
        assert room_id == "!room:localhost"
        assert message_type == "io.mindroom.tool_approval"
        assert ignore_unverified_devices is True
        is_edit = "m.new_content" in content
        # The card's own send carries the caller's transaction, which is what
        # lets a repeat converge; the edit that resolves it does not need one.
        assert tx_id == (None if is_edit else "txn-1")
        sent_contents.append(content)
        return nio.RoomSendResponse(event_id="$approval-edit" if is_edit else "$approval", room_id=room_id)

    router_client = MagicMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    router_client.room_send = AsyncMock(side_effect=room_send)
    router_bot = MagicMock(
        agent_name="router",
        running=True,
        client=router_client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    router_bot.latest_thread_event_id_if_needed = AsyncMock(return_value="$router-latest")

    code_bot = MagicMock(agent_name="code", running=True)
    code_bot.latest_thread_event_id_if_needed = AsyncMock(return_value="$code-latest")

    orchestrator.agent_bots = {"router": router_bot, "code": code_bot}
    orchestrator._approval_transport.cache_approval_event_now = AsyncMock()

    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        "$thread",
        {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "pending",
            "agent_name": "code",
        },
        "txn-1",
    )
    edited = await orchestrator._approval_transport.edit_approval_event_now(
        "!room:localhost",
        "$approval",
        {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "expired",
            "agent_name": "code",
            "thread_id": "$thread",
        },
    )

    assert sent is not None
    assert sent.event_id == "$approval"
    assert sent.sent_content == sent_contents[0]
    assert edited is True
    assert sent_contents[0]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$code-latest"
    assert "m.relates_to" not in sent_contents[1]["m.new_content"]
    code_bot.latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!room:localhost",
        "$thread",
    )
    router_bot.latest_thread_event_id_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_transport_refuses_encrypted_room_without_e2ee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
    orchestrator._capture_runtime_loop()
    monkeypatch.setattr("mindroom.matrix.client_delivery.crypto.ENCRYPTION_ENABLED", False)

    room = nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost", encrypted=True)
    router_client = MagicMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.rooms = {"!room:localhost": room}
    router_client.room_send = AsyncMock()
    router_bot = MagicMock(
        agent_name="router",
        running=True,
        client=router_client,
        approval_room_ids=frozenset({"!room:localhost"}),
    )
    orchestrator.agent_bots = {"router": router_bot}

    sent = await orchestrator._approval_transport.send_approval_event_now(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "pending",
        },
        "txn-1",
    )
    edited = await orchestrator._approval_transport.edit_approval_event_now(
        "!room:localhost",
        "$approval",
        {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "expired",
        },
    )

    assert sent is None
    assert edited is False
    router_client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_approval_store_clears_script_cache_when_manager_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_module._SCRIPT_CACHE[("approval.py", 1)] = MagicMock()
    original_shutdown = approval_module.approval_manager.shutdown_approval_manager

    async def fail_shutdown(*, reason: str) -> None:
        del reason
        message = "shutdown failed"
        raise RuntimeError(message)

    monkeypatch.setattr(approval_module.approval_manager, "shutdown_approval_manager", fail_shutdown)

    try:
        with pytest.raises(RuntimeError, match="shutdown failed"):
            await _shutdown_approval_store()
    finally:
        monkeypatch.setattr(approval_module.approval_manager, "shutdown_approval_manager", original_shutdown)

    assert approval_module._SCRIPT_CACHE == {}


def _claimed_card_body(approval_id: str) -> dict[str, Any]:
    """One card as it is recorded before its send: everything but the event id."""
    return {
        "sender": "@mindroom_router:localhost",
        "type": "io.mindroom.tool_approval",
        "origin_server_ts": 1_000,
        "content": {
            "msgtype": "io.mindroom.tool_approval",
            "tool_name": "read_file",
            "approval_id": approval_id,
            "tool_call_id": approval_id,
            "status": "pending",
            "approver_user_id": "@user:localhost",
            "arguments": {"path": "notes.txt"},
            "thread_id": "$thread",
        },
    }


@pytest.mark.asyncio
async def test_a_restart_retires_a_card_whose_send_never_came_back(tmp_path: Path) -> None:
    """The window between claiming a card and learning what it became.

    The row is written first, so a process that dies around the send leaves a
    claim with no event id rather than a card with no row. That is a knowable
    state: presenting the same transaction again either collapses onto the
    event the homeserver already accepted or posts the card now, and either way
    startup ends up holding an event it can expire.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card("txn-stranded", "!room:localhost", _claimed_card_body("stranded-approval"))
    # The homeserver already has this card; the repeat resolves to that event.
    sender = AsyncMock(return_value=SentApprovalEvent("$stranded"))
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    # The repeat carries the stored transaction, which is the only reason it
    # can converge on the card already in the room instead of adding a second.
    assert sender.await_args.args == ("!room:localhost", "$thread", ANY, "txn-stranded")
    assert editor.await_args.args[:2] == ("!room:localhost", "$stranded")
    assert editor.await_args.args[2]["status"] == "expired"
    assert cards.acknowledged == [("txn-stranded", "$stranded")]
    # Retired for good: the row is gone, so the next startup has nothing to do.
    assert await cards.pending_approval_cards(room_id="!room:localhost") == ()


@pytest.mark.asyncio
async def test_a_restart_does_not_resend_a_card_it_already_has_an_event_for(tmp_path: Path) -> None:
    """An acknowledged card is expired where it stands.

    Resending one would present a transaction the homeserver has already
    answered for no reason, and on a device whose transaction namespace has
    since changed it would put a second card in the room.
    """
    cards = FakeApprovalCards()
    await cards.store_card(
        "$recorded",
        "!room:localhost",
        {**_claimed_card_body("recorded-approval"), "event_id": "$recorded"},
    )
    sender = AsyncMock()
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    assert editor.await_count == 1
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_restart_keeps_the_claim_when_the_repeat_send_fails(tmp_path: Path) -> None:
    """A repeat that fails leaves the card claimed, not abandoned.

    The send failing says the outcome is still unknown. Dropping the row on
    that would strand whatever did reach the room -- exactly the state the
    claim exists to prevent -- so the row survives for the next startup to
    try again.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card("txn-stranded", "!room:localhost", _claimed_card_body("stranded-approval"))
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(return_value=None),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 0
    editor.assert_not_awaited()
    remaining = await cards.pending_approval_cards(room_id="!room:localhost")
    assert [card.transaction_id for card in remaining] == ["txn-stranded"]
    assert remaining[0].card_event_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restarted_device",
    [
        pytest.param("ADIFFERENTDEVICE", id="relogged-in-under-a-new-device"),
        pytest.param(None, id="device-not-yet-known"),
    ],
)
async def test_a_restart_expires_an_unsent_card_it_cannot_prove_the_device_for(
    tmp_path: Path,
    restarted_device: str | None,
) -> None:
    """A transaction belongs to a device, so a repeat from another is a new card.

    The homeserver deduplicates a transaction ID only against the device that
    used it. Presenting a claimed card again from a device that cannot be
    matched would therefore not converge on the card already in the room; it
    would add a second one, and a duplicated prompt for a human decision is
    worse than a stale one -- answering the copy resolves nothing.

    So the card dies here. The row goes with it, because the room has said it
    holds no such card, and keeping it would only re-ask the same unanswerable
    question on the next startup.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card("txn-stranded", "!room:localhost", _claimed_card_body("stranded-approval"))
    sender = AsyncMock(return_value=SentApprovalEvent("$second-card"))
    editor = AsyncMock(return_value=True)
    # The room's own answer: nothing this approval id names is in it.
    locate_card = AsyncMock(return_value=None)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: restarted_device,
        locate_card=locate_card,
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 0
    # The whole point: no second card, and nothing edited, because there is no
    # event id this process is entitled to claim.
    sender.assert_not_awaited()
    editor.assert_not_awaited()
    # Expired for good rather than left for the next startup to retry, which
    # would be a retry that can never succeed.
    assert await cards.pending_approval_cards(room_id="!room:localhost") == ()


@pytest.mark.asyncio
async def test_a_restart_adopts_and_expires_the_card_a_previous_device_left(tmp_path: Path) -> None:
    """The other half of a device change: the card really did reach the room.

    A row can be attempted, unacknowledged, and answered by the homeserver all
    at once -- that is what a crash between the send and the acknowledgement
    leaves. Forgetting it would retire the only thing that could ever expire
    the card or honour a click on it, so the room is read first, the card found
    there is adopted, and it is expired where it stands.

    Still no resend, which is the rule this does not touch: the card is
    addressed by the event id the room gave up, not by a transaction this
    device cannot present.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card(
        "txn-stranded",
        "!room:localhost",
        _claimed_card_body("stranded-approval"),
        sending_device_id="ANOTHERDEVICE",
    )
    sender = AsyncMock(return_value=SentApprovalEvent("$second-card"))
    editor = AsyncMock(return_value=True)
    locate_card = AsyncMock(return_value="$stranded")
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        locate_card=locate_card,
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    # Located by the approval id, which is device-independent, and never by the
    # transaction, which is not.
    assert locate_card.await_args.args == ("!room:localhost", "@mindroom_router:localhost", "stranded-approval")
    sender.assert_not_awaited()
    assert editor.await_args.args[:2] == ("!room:localhost", "$stranded")
    assert editor.await_args.args[2]["status"] == "expired"
    assert cards.acknowledged == [("txn-stranded", "$stranded")]
    # And only now is the row safe to drop: nothing clickable is left behind it.
    assert await cards.pending_approval_cards(room_id="!room:localhost") == ()


@pytest.mark.asyncio
async def test_a_restart_keeps_a_card_whose_room_lookup_could_not_run(tmp_path: Path) -> None:
    """A question that could not be put is not an answer of "no card".

    Failing to reach the homeserver says nothing about what is in the room, and
    a row dropped on that guess takes a clickable card's only owner with it. So
    it stays, and it is reported owed so the sweep's retry owner comes back.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card(
        "txn-stranded",
        "!room:localhost",
        _claimed_card_body("stranded-approval"),
        sending_device_id="ANOTHERDEVICE",
    )
    sender = AsyncMock(return_value=SentApprovalEvent("$second-card"))
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        locate_card=AsyncMock(side_effect=RuntimeError("the homeserver is unreachable")),
    )

    sweep = await restarted.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=0, failed=1)
    assert sweep.complete is False
    sender.assert_not_awaited()
    editor.assert_not_awaited()
    remaining = await cards.pending_approval_cards(room_id="!room:localhost")
    assert [card.transaction_id for card in remaining] == ["txn-stranded"]


@pytest.mark.asyncio
async def test_a_restart_drops_a_claim_whose_send_was_never_attempted(tmp_path: Path) -> None:
    """An unattempted row is the one case that needs no evidence at all.

    The claim is committed before the send is reached, so a process that died
    in between leaves a row that provably put nothing in the room. Nothing to
    resend, nothing to reconcile, and no reason to spend a room scan proving
    what the row already says.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card(
        "txn-unattempted",
        "!room:localhost",
        _claimed_card_body("unattempted-approval"),
        sending_device_id=None,
        attempted=False,
    )
    sender = AsyncMock(return_value=SentApprovalEvent("$second-card"))
    editor = AsyncMock(return_value=True)
    locate_card = AsyncMock(return_value="$never-happened")
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        locate_card=locate_card,
    )

    sweep = await restarted.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=0, failed=0)
    sender.assert_not_awaited()
    editor.assert_not_awaited()
    locate_card.assert_not_awaited()
    assert await cards.pending_approval_cards(room_id="!room:localhost") == ()


@pytest.mark.asyncio
async def test_a_restart_still_expires_an_acknowledged_card_from_another_device(tmp_path: Path) -> None:
    """The device only gates the resend, never the edit.

    A card whose event id is already recorded needs no transaction to be
    addressed, and a second ``m.replace`` carrying the same terminal content
    resolves to the same visible message. Refusing to expire it because the
    device changed would strand an answerable card for no gain.
    """
    cards = FakeApprovalCards()
    await cards.store_card(
        "$recorded",
        "!room:localhost",
        {**_claimed_card_body("recorded-approval"), "event_id": "$recorded"},
    )
    sender = AsyncMock()
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: "ADIFFERENTDEVICE",
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    sender.assert_not_awaited()
    assert editor.await_args.args[:2] == ("!room:localhost", "$recorded")
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_a_store_failure_while_binding_owes_one_row_not_the_page(tmp_path: Path) -> None:
    """A store that fails mid-recovery must cost one row, not the rest of the scan.

    Recovery writes the event id it established back to the row. That write
    used to be unguarded, so a store failing there raised out of the whole
    sweep: the tally went with it, and every row behind this one in the page
    -- and every room after it -- was never looked at. With the sweep retrying
    on a timer, each pass aborted at the same row.
    """

    class UnbindableCards(FakeApprovalCards):
        """A card store that refuses to record what a recovered card became."""

        async def acknowledge_approval_card(
            self,
            *,
            transaction_id: str,  # noqa: ARG002 - signature is the store protocol's
            card_event_id: str,  # noqa: ARG002
            card: Mapping[str, Any],  # noqa: ARG002
        ) -> None:
            msg = "acknowledge is unavailable"
            raise RuntimeError(msg)

    cards = UnbindableCards()
    # Claimed and attempted from this device but never acknowledged, which is
    # the row recovery presents the frozen transaction for.
    await cards.store_unsent_card("txn-unacknowledged", "!room:localhost", _approval_card())
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        locate_card=AsyncMock(return_value=None),
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=0, failed=1)
    assert cards.rows, "the row is owed, so it must survive for the next pass"


@pytest.mark.asyncio
@pytest.mark.parametrize("card_status", ["approved", "denied", "expired"])
async def test_card_response_for_terminal_original_card_is_untouched(
    tmp_path: Path,
    card_status: Literal["approved", "denied", "expired"],
) -> None:
    cards = FakeApprovalCards()
    card = _approval_card()
    card["content"]["status"] = card_status
    await cards.store_card("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("card_status", [None, "invalid"])
async def test_card_response_for_malformed_original_status_is_untouched(
    tmp_path: Path,
    card_status: str | None,
) -> None:
    cards = FakeApprovalCards()
    card = _approval_card()
    if card_status is None:
        card["content"].pop("status")
    else:
        card["content"]["status"] = card_status
    await cards.store_card("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


def test_pending_approval_ignores_malformed_edit_status() -> None:
    card = _approval_card()
    card["content"]["status"] = "approved"
    pending = PendingApproval.from_card_event(card, room_id="!room:localhost")

    assert pending.latest_status({"content": None}) == "approved"
    assert pending.latest_status({"content": {"status": "invalid"}}) == "approved"


@pytest.mark.asyncio
async def test_card_response_for_cached_orphan_rejects_non_approver(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    card = _approval_card()
    await cards.store_card("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@other:localhost",
        card_event_id="$approval",
        status="denied",
        reason="Too late.",
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_for_unknown_card_does_not_emit_terminal_edit(tmp_path: Path) -> None:
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_for_unknown_card_uses_bounded_point_lookup(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    lookups: list[tuple[str, str]] = []
    scans: list[str] = []
    cards.pending_approval_card = _recording_point_lookup(cards, lookups)  # type: ignore[method-assign]
    cards.pending_approval_cards = _recording_scan(cards, scans)  # type: ignore[method-assign]
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="denied",
        reason="Too late.",
    )

    assert result.consumed is False
    assert result.resolved is False
    assert lookups == [("!room:localhost", "$approval")]
    assert scans == []
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_response_expires_same_router_cached_pending_with_point_lookup(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="denied",
        reason="No.",
    )

    assert result.consumed is True
    assert result.resolved is True
    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Original tool request is no longer active."


@pytest.mark.asyncio
async def test_detached_card_response_ignores_untrusted_terminal_edit(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    card = _approval_card()
    await cards.store_card("$approval", "!room:localhost", card)
    await cards.store_card(
        "$fake-edit",
        "!room:localhost",
        _approval_edit(
            card,
            event_id="$fake-edit",
            sender="@attacker:localhost",
            status="approved",
        ),
    )
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="denied",
        reason=None,
    )

    assert result.consumed is True
    assert result.resolved is True
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_card_response_ignores_cross_router_matrix_only_card(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card(sender="@router_a:localhost"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@router_b:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    assert result.thread_id is None
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_response_ignores_cached_card_from_different_room(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    foreign_card = _approval_card(room_id="!other:localhost")
    await cards.store_card("$approval", "!room:localhost", foreign_card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_cached_response_events_emit_one_expired_edit(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    edit_count = 0

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
        return True

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    first = asyncio.create_task(
        store.handle_card_response(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            status="approved",
            reason=None,
        ),
    )
    second = asyncio.create_task(
        store.handle_card_response(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            status="denied",
            reason="Clicked elsewhere.",
        ),
    )
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.consumed is True
    assert second_result.consumed is True
    assert first_result.resolved is True
    assert second_result.resolved is False
    assert edit_count == 1


@pytest.mark.asyncio
async def test_discard_pending_on_startup_emits_replace_for_each_unresolved_card(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    edits: list[tuple[str, dict[str, Any]]] = []

    async def editor(room_id: str, event_id: str, content: dict[str, Any]) -> bool:
        del room_id
        edits.append((event_id, content))
        return True

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    # The delivered edit dropped the card, so a second startup owes nothing.
    assert (await store.discard_pending_on_startup()).discarded == 0
    assert [event_id for event_id, _ in edits] == ["$approval"]
    assert edits[0][1]["status"] == "expired"
    assert edits[0][1]["resolution_reason"] == ("Bot restarted before approval — original request was cancelled.")
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_startup_discovers_pending_rooms_no_longer_in_runtime_config(tmp_path: Path) -> None:
    """Durable cards must re-arm cleanup even when their room left the configured set."""
    cards = FakeApprovalCards()
    await cards.store_card(
        "$approval",
        "!former-room:localhost",
        _approval_card(room_id="!former-room:localhost"),
    )
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: set(),
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep.discarded == 1
    editor.assert_awaited_once()
    assert editor.await_args.args[:2] == ("!former-room:localhost", "$approval")


@pytest.mark.asyncio
async def test_discard_pending_on_startup_uses_cached_cards_without_history_scan(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    cached_card = _approval_card(approval_id="cached-approval", event_id="$cached-approval")
    await cards.store_card("$cached-approval", "!room:localhost", cached_card)
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert {call.args[1] for call in editor.await_args_list} == {"$cached-approval"}


@pytest.mark.asyncio
async def test_discard_pending_on_startup_expires_card_older_than_approval_timeout(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    old_timestamp = int((datetime.now(UTC) - timedelta(days=30)).timestamp() * 1000)
    await cards.store_card(
        "$old-approval",
        "!room:localhost",
        _approval_card(event_id="$old-approval", origin_server_ts=old_timestamp),
    )
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[1] == "$old-approval"
    assert editor.await_args.args[2]["status"] == "expired"


def _only_claimed_approval_id(cards: FakeApprovalCards) -> str:
    """Return the approval id of the single card the store is holding."""
    (row,) = cards.rows.values()
    return str(row.card["content"]["approval_id"])


def transaction_id_for_approval(cards: FakeApprovalCards) -> str:
    """Return the transaction the single claimed card was sent under."""
    (transaction_id,) = cards.rows
    return transaction_id


@pytest.mark.asyncio
async def test_a_page_of_undeliverable_cards_does_not_starve_the_ones_behind_it(tmp_path: Path) -> None:
    """A card whose edit failed keeps its row, so the scan has to advance past it.

    The row stays on purpose -- the decision may not be in the room yet -- which
    means it is still in the window the next read of this room returns. A scan
    that always starts at the beginning would hand back the same failures
    forever and never reach the cards queued behind them.
    """
    cards = FakeApprovalCards()
    for index in range(3):
        event_id = f"$approval-{index}"
        await cards.store_card(event_id, "!room:localhost", _approval_card(event_id=event_id))

    async def editor(room_id: str, event_id: str, content: dict[str, Any]) -> bool:
        del room_id, content
        return event_id == "$approval-2"

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    with patch("mindroom.approval_manager._STARTUP_DISCARD_SCAN_PAGE", 2):
        sweep = await store.discard_pending_on_startup()

    assert sweep.discarded == 1
    assert sweep.failed == 2
    assert sweep.complete is False
    # The two that failed keep their rows; the one behind them was reached.
    assert set(cards.rows) == {transaction_id_for("$approval-0"), transaction_id_for("$approval-1")}


@pytest.mark.asyncio
async def test_a_card_left_unsettled_is_reported_as_still_owed(tmp_path: Path) -> None:
    """A sweep that settled nothing must not look like a sweep with nothing to do."""
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=AsyncMock(return_value=False),
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=0, failed=1)
    assert sweep.complete is False


@pytest.mark.asyncio
async def test_a_card_no_device_can_resend_is_not_reported_as_owed(tmp_path: Path) -> None:
    """Dropping a claim the room disowns finishes it, so the sweep must not keep asking.

    The card is expired deliberately rather than presented again from a device
    the homeserver would not deduplicate against. Counting that as owed would
    make every later sweep come back for a row that is already gone.
    """
    cards = FakeApprovalCards()
    await cards.store_unsent_card(
        "txn-stranded",
        "!room:localhost",
        _approval_card(),
        sending_device_id="ANOTHERDEVICE",
    )
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=AsyncMock(return_value=True),
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
        locate_card=AsyncMock(return_value=None),
    )

    sweep = await store.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=0, failed=0)
    assert sweep.complete is True


@pytest.mark.asyncio
async def test_discard_pending_on_startup_scans_more_than_500_cached_cards(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    for index in range(501):
        event_id = f"$approval-{index}"
        await cards.store_card(
            event_id,
            "!room:localhost",
            _approval_card(
                approval_id=f"approval-{index}",
                event_id=event_id,
                origin_server_ts=int(datetime.now(UTC).timestamp() * 1000) + index,
            ),
        )
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 501
    assert editor.await_count == 501


@pytest.mark.asyncio
async def test_discard_pending_on_startup_expires_same_router_cached_cards(
    tmp_path: Path,
) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    replacement = editor.await_args.args[2]
    assert replacement["status"] == "expired"
    assert replacement["resolution_reason"] == "Bot restarted before approval — original request was cancelled."


@pytest.mark.asyncio
async def test_discard_pending_on_startup_preserves_same_router_cache_hit(
    tmp_path: Path,
) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_discard_pending_on_startup_skips_cross_router_cached_cards(
    tmp_path: Path,
) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card(sender="@other_router:localhost"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_restart_redelivers_a_decision_instead_of_expiring_it(tmp_path: Path) -> None:
    """A card whose decision was recorded is answered, even if the edit was lost.

    The decision is written before the edit is attempted, so a crash between
    the two leaves the row behind. Expiring it would overwrite an approval the
    room may already show -- and whose tool may already have run -- with
    "expired". The recorded decision is redelivered instead.
    """
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    await cards.resolve_approval_card(
        card_event_id="$approval",
        resolution={"status": "approved", "resolution_reason": "Looks fine."},
    )
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "approved"
    assert editor.await_args.args[2]["resolution_reason"] == "Looks fine."
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_a_click_on_an_already_decided_card_does_not_re_resolve_it(tmp_path: Path) -> None:
    """A recorded decision closes the card to further answers.

    Its live waiter is gone with the process that made the decision, so the
    click arrives at the recovery path. Treating it as a fresh resolution would
    replace a decision whose tool may already have run.
    """
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    await cards.resolve_approval_card(card_event_id="$approval", resolution={"status": "approved"})
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="denied",
        reason="Changed my mind.",
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_decision_is_recorded_before_the_edit_is_attempted(tmp_path: Path) -> None:
    """Ordering is the whole point: recorded first, shown second.

    If the edit were attempted first, a crash in between would leave a card
    that looks unanswered, and the next startup would expire a decision the
    room already shows.
    """
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    recorded_when_edited: list[dict[str, Any] | None] = []

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        recorded_when_edited.append(cards.resolutions.get("$approval"))
        return True

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 1
    assert len(recorded_when_edited) == 1
    assert recorded_when_edited[0] is not None, "the edit went out before the decision was durable"
    assert recorded_when_edited[0]["status"] == "expired"


@pytest.mark.asyncio
async def test_startup_discard_that_never_reached_matrix_stays_recoverable(
    tmp_path: Path,
) -> None:
    """A card is only dropped once the room shows the decision.

    The edit is what makes the card unclickable. If it never landed, the room
    still shows something a user can answer, and the row is the only thing
    that brings the next startup back to it.
    """
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=False)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 0
    assert cards.stored_event_ids() == {"$approval"}

    editor.return_value = True
    assert (await store.discard_pending_on_startup()).discarded == 1
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_discard_pending_on_startup_skips_other_routers_cards(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card(sender="@other_router:localhost"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await store.discard_pending_on_startup()).discarded == 0
    editor.assert_not_awaited()


def test_pending_approval_from_card_event_requires_approver_user_id() -> None:
    card = _approval_card()
    card["content"].pop("approver_user_id")

    with pytest.raises(ValueError, match="missing required approval fields"):
        PendingApproval.from_card_event(card, room_id="!room:localhost")


def test_pending_approval_preserves_distinct_requester_and_approver() -> None:
    card = _approval_card(requester="@requester:localhost", approver="@approver:localhost")

    pending = PendingApproval.from_card_event(card, room_id="!room:localhost")

    assert pending.requester_id == "@requester:localhost"
    assert pending.approver_user_id == "@approver:localhost"


def test_parse_approval_datetime_preserves_approval_timestamp_contract() -> None:
    assert parse_approval_datetime(None) is None
    assert parse_approval_datetime("2030-01-01T10:00:00+02:00") == datetime.fromisoformat(
        "2030-01-01T10:00:00+02:00",
    )
    assert parse_approval_datetime("2030-01-01T10:00:00") == datetime(2030, 1, 1, 10, tzinfo=UTC)

    with pytest.raises(ValueError, match="Invalid isoformat string"):
        parse_approval_datetime("not-a-datetime")


def test_approval_arguments_preview_marks_sanitizer_truncation() -> None:
    arguments = {f"k{index}": index for index in range(30)}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview["__truncated__"] == "5 more items"
    assert truncated is True

    card = _ApprovalManager._pending_event_content(
        approval_id="approval-1",
        tool_name="read_file",
        arguments=preview,
        arguments_truncated=truncated,
        agent_name="code",
        thread_id=None,
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        requested_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="pending",
    )

    assert card["arguments_truncated"] is True


def test_approval_arguments_preview_marks_nested_sanitizer_truncation() -> None:
    arguments = {"items": list(range(30))}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview["items"][-1] == "... [truncated]"
    assert truncated is True


def test_approval_arguments_preview_does_not_mark_literal_truncation_marker() -> None:
    arguments = {"note": "literal marker ... [truncated]"}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview == arguments
    assert truncated is False


def test_full_event_arguments_returns_complete_payload() -> None:
    arguments = {"content": "x" * 10_000, "path": "notes.txt"}

    assert _build_full_event_arguments(arguments) == arguments


def test_full_event_arguments_redacts_secrets_without_bypassing_truncation_checks() -> None:
    arguments = {"api_key": "sk-live-1234567890abcdef", "content": "x" * 5_000}

    full_arguments = _build_full_event_arguments(arguments)

    assert full_arguments is not None
    assert full_arguments["content"] == "x" * 5_000
    assert "sk-live-1234567890abcdef" not in json.dumps(full_arguments)


def test_full_event_arguments_rejects_payload_over_completeness_cap() -> None:
    assert _build_full_event_arguments({"content": "x" * 3_000_000}) is None


def test_full_event_arguments_accepts_sidecar_sized_payload() -> None:
    payload = {"content": "x" * 100_000}

    assert _build_full_event_arguments(payload) == payload


def test_full_event_arguments_budgets_utf8_bytes_not_characters() -> None:
    # 800k CJK chars stay under a character-based cap but encode to ~2.4MB, over the byte cap.
    assert _build_full_event_arguments({"content": "汉" * 800_000}) is None
    assert _build_full_event_arguments({"content": "汉" * 8_000}) == {"content": "汉" * 8_000}


def test_full_event_arguments_accepts_structurally_complex_payload_below_byte_cap() -> None:
    nested: object = "value"
    for _ in range(20):
        nested = {"nested": nested}
    arguments = {"items": list(range(60_000)), "nested": nested}

    assert _build_full_event_arguments(arguments) == arguments


def test_pending_approval_parses_full_arguments_availability() -> None:
    card = _approval_card(arguments_truncated=True)
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is False

    card["content"]["full_arguments"] = {}
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is False

    card["content"]["full_arguments"] = {"content": "x" * 10_000}
    assert PendingApproval.from_card_event(card, room_id="!room:localhost").full_arguments_available is True


def test_pending_approval_parses_sidecar_full_arguments_availability() -> None:
    url_card = _approval_card(arguments_truncated=True)
    url_card["content"]["full_arguments_url"] = "mxc://localhost/full-args"
    assert PendingApproval.from_card_event(url_card, room_id="!room:localhost").full_arguments_available is False

    url_card["content"]["full_arguments_info"] = {"size": 10_000, "mimetype": "application/json"}
    assert PendingApproval.from_card_event(url_card, room_id="!room:localhost").full_arguments_available is True

    file_card = _approval_card(arguments_truncated=True)
    file_card["content"]["full_arguments_file"] = {}
    assert PendingApproval.from_card_event(file_card, room_id="!room:localhost").full_arguments_available is False

    file_card["content"]["full_arguments_file"] = {
        "url": "mxc://localhost/full-args",
        "key": {"alg": "A256CTR", "k": "secret", "key_ops": ["encrypt", "decrypt"], "kty": "oct"},
        "iv": "iv-value",
        "hashes": {"sha256": "sha256-value"},
        "v": "v2",
        "size": 10_000,
        "mimetype": "application/json",
    }
    assert PendingApproval.from_card_event(file_card, room_id="!room:localhost").full_arguments_available is True


def test_pending_approval_defaults_missing_approvable_flag_to_true() -> None:
    card = _approval_card(arguments_truncated=True)

    assert PendingApproval.from_card_event(card, room_id="!room:localhost").approvable is True


@pytest.mark.parametrize(("value", "expected"), [(False, False), (True, True), (None, False), ("false", False)])
def test_pending_approval_parses_explicit_approvable_flag(value: object, expected: bool) -> None:
    card = _approval_card(arguments_truncated=True)
    card["content"]["approvable"] = value

    assert PendingApproval.from_card_event(card, room_id="!room:localhost").approvable is expected


def test_resolve_tool_approval_approver_rejects_internal_users(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            bot_accounts=["@bridge_bot:localhost"],
            mindroom_user=MindRoomUserConfig(),
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "code": "actual_code"})
    internal_user_id = mindroom_user_id(config, runtime_paths)
    assert internal_user_id is not None
    agent_user_id = entity_identity_registry(config, runtime_paths).current_id("code").full_id

    assert resolve_tool_approval_approver(config, runtime_paths, None) is None
    assert resolve_tool_approval_approver(config, runtime_paths, agent_user_id) is None
    assert resolve_tool_approval_approver(config, runtime_paths, internal_user_id) is None
    assert resolve_tool_approval_approver(config, runtime_paths, "@bridge_bot:localhost") is None
    assert resolve_tool_approval_approver(config, runtime_paths, "@user:localhost") == "@user:localhost"


@pytest.mark.asyncio
async def test_evaluate_tool_approval_rule_action_requires_approval(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_*", "action": "require_approval"}]},
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is True
    assert timeout_seconds > 0


@pytest.mark.parametrize(
    ("hostnames", "expected"),
    [
        (["docs.example.com"], False),
        (["docs.example.com", "api.example.com"], False),
        (["docs.example.com", "docs.other.test"], True),
        (["docs.other.test"], True),
        ([123], True),
        (["https://docs.example.com"], True),
        ("docs.example.com", True),
        ([], True),
    ],
)
@pytest.mark.asyncio
async def test_evaluate_tool_approval_honors_tool_approval_exemption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hostnames: object,
    expected: bool,
) -> None:
    """request_network_access calls where every hostname is statically allowlisted need no approval."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", ".example.com")
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "request_network_access", "action": "require_approval"}]},
        ),
        runtime_paths,
    )

    requires_approval, _ = await evaluate_tool_approval(
        config,
        runtime_paths,
        "request_network_access",
        {"hostnames": hostnames, "ttl_minutes": 5, "reason": "Need docs."},
        "code",
    )

    assert requires_approval is expected


@pytest.mark.asyncio
async def test_tool_approval_rule_matching_uses_first_matching_action_for_both_callers(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": "auto_approve",
                "rules": [
                    {"match": "read_*", "action": "auto_approve", "timeout_days": 2},
                    {"match": "read_file", "action": "require_approval", "timeout_days": 9},
                ],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is False
    assert timeout_seconds == 2 * 24 * 60 * 60
    assert tool_requires_approval_for_openai_compat(config, "read_file") is False


@pytest.mark.asyncio
async def test_tool_approval_script_rule_listing_requires_approval_but_evaluation_runs_script(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    return arguments['requires_approval']\n",
        encoding="utf-8",
    )
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": "auto_approve",
                "timeout_days": 4,
                "rules": [{"match": "write_*", "script": str(script_path), "timeout_days": 1}],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "write_file",
        {"requires_approval": False},
        "code",
    )

    assert requires_approval is False
    assert timeout_seconds == 24 * 60 * 60
    assert tool_requires_approval_for_openai_compat(config, "write_file") is True


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        ("auto_approve", False),
        ("require_approval", True),
    ],
)
@pytest.mark.asyncio
async def test_tool_approval_rule_matching_falls_back_to_default_for_both_callers(
    tmp_path: Path,
    default: str,
    expected: bool,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={
                "default": default,
                "rules": [{"match": "write_*", "action": "require_approval"}],
            },
        ),
        runtime_paths,
    )

    requires_approval, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is expected
    assert timeout_seconds == 7 * 24 * 60 * 60
    assert tool_requires_approval_for_openai_compat(config, "read_file") is expected


@pytest.mark.asyncio
async def test_evaluate_tool_approval_script_error_is_sanitized(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    raise ValueError('boom')\n",
        encoding="utf-8",
    )
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_file", "script": str(script_path)}]},
        ),
        runtime_paths,
    )

    with pytest.raises(ToolApprovalScriptError, match="failed with ValueError"):
        await evaluate_tool_approval(config, runtime_paths, "read_file", {"path": "notes.txt"}, "code")


def test_get_approval_store_returns_initialized_store(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)

    store = initialize_approval_store(runtime_paths)

    assert get_approval_store() is store
