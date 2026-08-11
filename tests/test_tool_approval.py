"""Tests for Matrix-backed tool approval state."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

import mindroom.tool_approval as approval_module
from mindroom import approval_transport
from mindroom.approval_events import parse_approval_datetime
from mindroom.approval_inbound import handle_tool_approval_action
from mindroom.approval_manager import (
    _MAX_REMEMBERED_TERMINAL_CARD_IDS,
    DEFAULT_SHUTDOWN_REASON,
    ApprovalDecision,
    ApprovalStartupSweep,
    PendingApproval,
    SentApprovalEvent,
    _approval_transaction_id,
    _ApprovalManager,
    _build_event_arguments_preview,
    _build_full_event_arguments,
    _LiveApprovalWaiter,
    get_approval_store,
    initialize_approval_store,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry, mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.tool_approval import (
    MatrixApprovalAction,
    ToolApprovalCall,
    ToolApprovalScriptError,
    _shutdown_approval_store,
    evaluate_tool_approval,
    handle_matrix_approval_action,
    is_process_approval_card,
    request_tool_approval_for_call,
    resolve_tool_approval_approver,
    tool_requires_approval_for_openai_compat,
)
from mindroom.tools import approved_egress as _approved_egress  # noqa: F401 - registers the approval exemption
from tests.approval_test_support import (
    CLAIMING_DEVICE_ID,
    FakeApprovalCards,
    UnclaimableApprovalCards,
    UnwritableApprovalCards,
    transaction_id_for,
)
from tests.approval_test_support import resolve_pending_approval as _resolve_pending_approval
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


async def _wait_for_pending(
    store: _ApprovalManager,
    *,
    room_id: str = "!room:localhost",
    approval_id: str | None = None,
    sender: AsyncMock | None = None,
    call_index: int | None = None,
) -> PendingApproval:
    async with asyncio.timeout(5):
        while True:
            resolved_approval_id = approval_id
            if resolved_approval_id is None and sender is not None:
                if call_index is None and sender.await_args is not None:
                    resolved_approval_id = sender.await_args.args[2]["approval_id"]
                elif call_index is not None and len(sender.await_args_list) > call_index:
                    resolved_approval_id = sender.await_args_list[call_index].args[2]["approval_id"]
            if resolved_approval_id is not None:
                pending = await _live_pending_approval(store, room_id=room_id, approval_id=resolved_approval_id)
                if pending is not None:
                    return pending
            await asyncio.sleep(0)


async def _live_pending_approval(
    store: _ApprovalManager,
    *,
    room_id: str,
    approval_id: str,
) -> PendingApproval | None:
    card_event_id = store._live_card_event_id_for_approval(approval_id)
    if card_event_id is None:
        return None
    return store._pending_approval_for_card(room_id=room_id, card_event_id=card_event_id)


@pytest.mark.asyncio
async def test_request_approval_approves_and_edits_matrix_event(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    assert sender.await_args.args[2]["approver_user_id"] == "@user:localhost"
    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert result.resolved is True
    assert decision.status == "approved"
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "approved"
    assert editor.await_args.args[2]["approver_user_id"] == "@user:localhost"


@pytest.mark.asyncio
async def test_request_approval_carries_workflow_provenance_through_resolution(tmp_path: Path) -> None:
    """Dynamic Workflow participant cards must name the workflow and participant, pending and resolved."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "ls"},
            agent_name="general",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
            workflow_id="competitor-research-report",
            participant_id="writer",
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    card_content = sender.await_args.args[2]
    assert card_content["workflow_id"] == "competitor-research-report"
    assert card_content["participant_id"] == "writer"
    assert card_content["body"] == (
        "🔒 Approval required: run_shell_command — Dynamic Workflow 'competitor-research-report' participant 'writer'"
    )
    assert pending.workflow_id == "competitor-research-report"
    assert pending.participant_id == "writer"

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert result.resolved is True
    assert decision.status == "approved"
    edited_content = editor.await_args.args[2]
    assert edited_content["workflow_id"] == "competitor-research-report"
    assert edited_content["participant_id"] == "writer"
    assert edited_content["body"] == (
        "Approved: run_shell_command — Dynamic Workflow 'competitor-research-report' participant 'writer'"
    )


@pytest.mark.asyncio
async def test_live_card_response_ignores_cached_terminal_edit_from_different_sender(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    fake_edit = _approval_edit(
        _approval_card(
            event_id=pending.card_event_id,
            room_id=pending.room_id,
            sender=pending.card_sender_id,
            approver=pending.approver_user_id,
        ),
        sender="@attacker:localhost",
        status="approved",
    )
    await cards.store_card("$fake-edit", "!room:localhost", fake_edit)

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = None
    if result.resolved:
        decision = await asyncio.wait_for(task, timeout=1)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert result.resolved is True
    assert result.consumed is True
    assert decision is not None
    assert decision.status == "approved"
    editor.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_card_response_wins_when_approval_card_is_cached(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    await cards.store_card(
        pending.card_event_id,
        pending.room_id,
        _approval_card(
            approval_id=pending.approval_id,
            event_id=pending.card_event_id,
            room_id=pending.room_id,
            sender=pending.card_sender_id,
            approver=pending.approver_user_id,
        ),
    )

    result = await store.handle_card_response(
        room_id=pending.room_id,
        sender_id=pending.approver_user_id,
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert result.resolved is True
    assert decision.status == "approved"
    assert editor.await_args.args[2]["status"] == "approved"


@pytest.mark.asyncio
async def test_handle_card_response_wrong_clicker_noops(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@other:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    assert result.resolved is False
    assert result.consumed is False
    editor.assert_not_awaited()

    await _resolve_pending_approval(
        store,
        pending,
        status="denied",
        reason="Denied by approver.",
    )
    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "Denied by approver."


@pytest.mark.asyncio
async def test_public_tool_approval_facade_resolves_live_matrix_action(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_file", "action": "require_approval"}]},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router", "code": "mindroom_code"})
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    approval_task = asyncio.create_task(
        request_tool_approval_for_call(
            ToolApprovalCall(
                config=config,
                runtime_paths=runtime_paths,
                tool_name="read_file",
                arguments={"path": "notes.txt"},
                agent_name="code",
                room_id="!room:localhost",
                thread_id="$thread",
                requester_id="@user:localhost",
            ),
        ),
    )
    for _ in range(20):
        if is_process_approval_card("$approval"):
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("approval card was not registered")

    action_result = await handle_matrix_approval_action(
        MatrixApprovalAction(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            approval_id=None,
            status="approved",
            reason=None,
        ),
    )
    decision = await asyncio.wait_for(approval_task, timeout=1)

    assert action_result.consumed is True
    assert action_result.resolved is True
    assert decision is not None
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_public_tool_approval_facade_falls_back_to_live_id_after_terminal_card_match(
    tmp_path: Path,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(
        side_effect=[
            SentApprovalEvent("$first-approval"),
            SentApprovalEvent("$second-approval"),
        ],
    )
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    first_task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "first.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    first_pending = await _wait_for_pending(store, sender=sender)
    first_result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=first_pending.card_event_id,
        status="approved",
        reason=None,
    )
    first_decision = await first_task
    assert first_result.resolved is True
    assert first_decision.status == "approved"

    second_task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "second.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    second_pending = await _wait_for_pending(store, sender=sender)

    action_result = await handle_matrix_approval_action(
        MatrixApprovalAction(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id=first_pending.card_event_id,
            approval_id=second_pending.approval_id,
            status="denied",
            reason="Wrong current tool.",
        ),
    )
    second_decision = await asyncio.wait_for(second_task, timeout=1)

    assert action_result.consumed is True
    assert action_result.resolved is True
    assert action_result.card_event_id == second_pending.card_event_id
    assert second_decision.status == "denied"
    assert second_decision.reason == "Wrong current tool."


@pytest.mark.asyncio
async def test_public_tool_approval_facade_uses_approval_id_over_active_unrelated_card(
    tmp_path: Path,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(
        side_effect=[
            SentApprovalEvent("$first-approval"),
            SentApprovalEvent("$second-approval"),
        ],
    )
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    first_task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "first.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    first_pending = await _wait_for_pending(store, sender=sender, call_index=0)
    second_task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "second.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    second_pending = await _wait_for_pending(store, sender=sender, call_index=1)

    try:
        action_result = await handle_matrix_approval_action(
            MatrixApprovalAction(
                room_id="!room:localhost",
                sender_id="@user:localhost",
                card_event_id=first_pending.card_event_id,
                approval_id=second_pending.approval_id,
                status="denied",
                reason="Wrong current tool.",
            ),
        )
        assert action_result.card_event_id == second_pending.card_event_id
        second_decision = await asyncio.wait_for(second_task, timeout=1)

        assert action_result.consumed is True
        assert action_result.resolved is True
        assert second_decision.status == "denied"
        assert second_decision.reason == "Wrong current tool."
        assert not first_task.done()
    finally:
        if not first_task.done():
            await _resolve_pending_approval(
                store,
                first_pending,
                status="denied",
                reason="cleanup",
            )
            await asyncio.wait_for(first_task, timeout=1)
        if not second_task.done():
            await _resolve_pending_approval(
                store,
                second_pending,
                status="denied",
                reason="cleanup",
            )
            await asyncio.wait_for(second_task, timeout=1)


@pytest.mark.asyncio
async def test_public_tool_approval_facade_missing_runtime_decision_uses_datetime(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval={"rules": [{"match": "read_file", "action": "require_approval"}]},
        ),
        runtime_paths,
    )

    decision = await request_tool_approval_for_call(
        ToolApprovalCall(
            config=config,
            runtime_paths=runtime_paths,
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
        ),
    )

    assert decision is not None
    assert decision.status == "expired"
    assert isinstance(decision.resolved_at, datetime)


@pytest.mark.asyncio
async def test_handle_card_response_rejects_live_card_from_wrong_room(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room-a:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender, room_id="!room-a:localhost")

    result = await store.handle_card_response(
        room_id="!room-b:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    assert not task.done()
    editor.assert_not_awaited()

    await _resolve_pending_approval(
        store,
        pending,
        status="denied",
        reason="cleanup",
    )
    await task


@pytest.mark.asyncio
async def test_handle_live_approval_id_response_resolves_same_room_waiter(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room-a:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender, room_id="!room-a:localhost")

    before_consume = AsyncMock()
    wrong_room_result = await store.handle_live_approval_id_response(
        room_id="!room-b:localhost",
        sender_id="@user:localhost",
        approval_id=pending.approval_id,
        status="approved",
        reason=None,
        before_consume=before_consume,
    )
    assert wrong_room_result.consumed is False
    before_consume.assert_not_awaited()

    result = await store.handle_live_approval_id_response(
        room_id="!room-a:localhost",
        sender_id="@user:localhost",
        approval_id=pending.approval_id,
        status="approved",
        reason=None,
        before_consume=before_consume,
    )
    decision = await task

    assert result.resolved is True
    before_consume.assert_awaited_once_with()
    assert decision.status == "approved"
    assert editor.await_args.args[:2] == ("!room-a:localhost", "$approval")


@pytest.mark.asyncio
async def test_handle_live_approval_id_response_rejects_waiter_from_wrong_room(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room-a:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender, room_id="!room-a:localhost")

    result = await store.handle_live_approval_id_response(
        room_id="!room-b:localhost",
        sender_id="@user:localhost",
        approval_id=pending.approval_id,
        status="approved",
        reason=None,
    )

    assert result.consumed is False
    assert result.resolved is False
    assert not task.done()
    editor.assert_not_awaited()

    await _resolve_pending_approval(
        store,
        pending,
        status="denied",
        reason="cleanup",
    )
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("response_status", ["approved", "denied"])
async def test_public_matrix_action_expires_trusted_pending_orphan_without_approving(
    tmp_path: Path,
    response_status: Literal["approved", "denied"],
) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await handle_matrix_approval_action(
        MatrixApprovalAction(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            approval_id="approval-1",
            status=response_status,
            reason=None,
        ),
    )

    assert result.consumed is True
    assert result.resolved is True
    assert store.has_live_work() is False
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    replacement = editor.await_args.args[2]
    assert replacement["status"] == "expired"
    assert replacement["resolution_reason"] == "Original tool request is no longer active."
    assert replacement["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_request_approval_truncated_approval_fails_closed(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 3_000_000},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    card_content = sender.await_args.args[2]
    assert card_content["arguments_truncated"] is True
    assert card_content["approvable"] is False
    assert "full_arguments" not in card_content

    await _resolve_pending_approval(
        store,
        pending,
        status="approved",
    )
    decision = await task

    assert decision.status == "denied"
    assert "too large to show in full" in (decision.reason or "")
    assert editor.await_args.args[2]["status"] == "denied"


@pytest.mark.asyncio
async def test_request_approval_truncated_preview_with_full_arguments_can_be_approved(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 10_000},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    card_content = sender.await_args.args[2]
    assert card_content["arguments_truncated"] is True
    assert card_content["full_arguments"] == {"content": "x" * 10_000}
    assert "approvable" not in card_content

    await _resolve_pending_approval(
        store,
        pending,
        status="approved",
    )
    decision = await task

    assert decision.status == "approved"
    assert editor.await_args.args[2]["status"] == "approved"


@pytest.mark.asyncio
async def test_request_approval_propagates_full_arguments_build_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_unexpected(_arguments: dict[str, Any]) -> dict[str, Any] | None:
        msg = "unexpected redaction failure"
        raise ValueError(msg)

    monkeypatch.setattr("mindroom.approval_manager._build_full_event_arguments", raise_unexpected)
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    with pytest.raises(ValueError, match="unexpected redaction failure"):
        await store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 10_000},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        )

    sender.assert_not_awaited()
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_honors_transport_stripped_full_arguments(tmp_path: Path) -> None:
    """When the transport strips full arguments (failed sidecar upload), approve must fail closed."""
    runtime_paths = test_runtime_paths(tmp_path)

    async def send_without_full_arguments(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        sent_content = {key: value for key, value in content.items() if key != "full_arguments"}
        sent_content["approvable"] = False
        return SentApprovalEvent(event_id="$approval", sent_content=sent_content)

    sender = AsyncMock(side_effect=send_without_full_arguments)
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 10_000},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    assert pending.full_arguments_available is False

    await _resolve_pending_approval(
        store,
        pending,
        status="approved",
    )
    decision = await task

    assert decision.status == "denied"
    assert "too large to show in full" in (decision.reason or "")


@pytest.mark.asyncio
async def test_request_approval_honors_transport_non_approvable_flag_with_stale_full_arguments(
    tmp_path: Path,
) -> None:
    """An explicit transport veto must win over stale full-argument delivery metadata."""
    runtime_paths = test_runtime_paths(tmp_path)

    async def send_non_approvable(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        return SentApprovalEvent(
            event_id="$approval",
            sent_content={**content, "approvable": False},
        )

    sender = AsyncMock(side_effect=send_non_approvable)
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 10_000},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    assert pending.approvable is False
    assert pending.full_arguments_available is True

    await _resolve_pending_approval(
        store,
        pending,
        status="approved",
    )
    decision = await task

    assert decision.status == "denied"
    assert "too large to show in full" in (decision.reason or "")


@pytest.mark.asyncio
async def test_truncated_approval_action_sends_denial_notice(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 3_000_000},
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    room = MagicMock(room_id="!room:localhost", canonical_alias=None)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            authorization=AuthorizationConfig(global_users=["@user:localhost"]),
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router", "code": "mindroom_code"})
    orchestrator = MagicMock()
    orchestrator.send_approval_notice = AsyncMock(return_value=True)

    handled = await handle_tool_approval_action(
        room=room,
        sender_id="@user:localhost",
        config=config,
        runtime_paths=runtime_paths,
        orchestrator=orchestrator,
        logger=get_logger(__name__),
        approval_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )

    decision = await task
    assert handled is True
    assert decision.status == "denied"
    assert editor.await_args.args[2]["status"] == "denied"
    orchestrator.send_approval_notice.assert_awaited_once()
    assert orchestrator.send_approval_notice.await_args.kwargs == {
        "room_id": "!room:localhost",
        "approval_event_id": pending.card_event_id,
        "thread_id": "$thread",
        "reason": editor.await_args.args[2]["resolution_reason"],
    }


@pytest.mark.asyncio
async def test_request_approval_cleans_up_on_cancellation_after_send(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    assert await _live_pending_approval(store, room_id="!room:localhost", approval_id=pending.approval_id) is None


@pytest.mark.asyncio
async def test_a_cancellation_while_acknowledging_the_send_still_expires_the_card(tmp_path: Path) -> None:
    """A cancelled request must not leave a clickable card nobody settles.

    The card is already in the room by this point, and the row that accounts
    for it was written before the send. What is still owed is the expiry edit,
    and the caller's cancellation must not swallow it: without it the card
    stays clickable until the next startup goes looking.
    """
    cards = FakeApprovalCards()
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    write_started = asyncio.Event()
    release_write = asyncio.Event()
    real_acknowledge = cards.acknowledge_approval_card
    first_call = True

    async def gated_acknowledge(*args: object, **kwargs: object) -> object:
        nonlocal first_call
        if first_call:
            first_call = False
            write_started.set()
            await release_write.wait()
        return await real_acknowledge(*args, **kwargs)

    cards.acknowledge_approval_card = gated_acknowledge  # type: ignore[method-assign]

    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(write_started.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release_write.set()

    for _ in range(200):
        if editor.await_args is not None:
            break
        await asyncio.sleep(0.01)

    # Recorded, then expired -- so it is no longer pending, which is the whole
    # point: a restart has nothing left to recover because the card is settled.
    assert editor.await_args is not None, "the orphaned card was never taken back"
    assert editor.await_args.args[2]["status"] == "expired"
    assert await cards.pending_approval_cards(room_id="!room:localhost") == ()


@pytest.mark.asyncio
async def test_request_approval_cancel_after_event_id_before_sender_return_emits_expired_edit(tmp_path: Path) -> None:
    event_committed = asyncio.Event()
    release_sender = asyncio.Event()
    edit_seen = asyncio.Event()
    sent_content: dict[str, Any] = {}

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        sent_content.update(content)
        event_committed.set()
        await release_sender.wait()
        return SentApprovalEvent("$approval")

    async def edit_side_effect(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        edit_seen.set()
        return True

    editor = AsyncMock(side_effect=edit_side_effect)
    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(event_committed.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    release_sender.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(edit_seen.wait(), timeout=1)

    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    replacement = editor.await_args.args[2]
    assert replacement["status"] == "expired"
    assert replacement["resolution_reason"] == "Tool approval request was cancelled."
    assert store._live_card_event_id_for_approval(sent_content["approval_id"]) is None


@pytest.mark.asyncio
async def test_request_approval_cancelled_send_returns_before_event_id_and_cleans_up_later(tmp_path: Path) -> None:
    event_committed = asyncio.Event()
    release_sender = asyncio.Event()
    edit_seen = asyncio.Event()
    sent_content: dict[str, Any] = {}
    edits: list[tuple[str, str, dict[str, Any]]] = []

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        sent_content.update(content)
        event_committed.set()
        await release_sender.wait()
        return SentApprovalEvent("$approval")

    async def editor(room_id: str, event_id: str, content: dict[str, Any]) -> bool:
        edits.append((room_id, event_id, content))
        edit_seen.set()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(event_committed.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert edits == []
    assert store._post_cancel_cleanup_tasks

    release_sender.set()
    await asyncio.wait_for(edit_seen.wait(), timeout=1)

    assert edits[0][:2] == ("!room:localhost", "$approval")
    replacement = edits[0][2]
    assert replacement["status"] == "expired"
    assert replacement["resolution_reason"] == "Tool approval request was cancelled."
    assert store._live_card_event_id_for_approval(sent_content["approval_id"]) is None
    await asyncio.sleep(0)
    assert not store._post_cancel_cleanup_tasks


@pytest.mark.asyncio
async def test_request_approval_cancelled_slow_send_background_cleanup_removes_waiter(tmp_path: Path) -> None:
    send_started = asyncio.Event()
    release_sender = asyncio.Event()
    edit_seen = asyncio.Event()
    sent_content: dict[str, Any] = {}
    edits: list[dict[str, Any]] = []

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        sent_content.update(content)
        send_started.set()
        await release_sender.wait()
        return SentApprovalEvent("$approval")

    async def editor(_room_id: str, _event_id: str, content: dict[str, Any]) -> bool:
        edits.append(content)
        edit_seen.set()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert edits == []

    release_sender.set()
    await asyncio.wait_for(edit_seen.wait(), timeout=1)

    assert edits[0]["status"] == "expired"
    assert edits[0]["resolution_reason"] == "Tool approval request was cancelled."
    assert store._live_card_event_id_for_approval(sent_content["approval_id"]) is None


@pytest.mark.asyncio
async def test_shutdown_waits_for_cancelled_send_background_cleanup(tmp_path: Path) -> None:
    event_committed = asyncio.Event()
    release_sender = asyncio.Event()
    edit_seen = asyncio.Event()
    edits: list[dict[str, Any]] = []

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        _content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        event_committed.set()
        await release_sender.wait()
        return SentApprovalEvent("$approval")

    async def editor(_room_id: str, _event_id: str, content: dict[str, Any]) -> bool:
        edits.append(content)
        edit_seen.set()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(event_committed.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert store._post_cancel_cleanup_tasks

    shutdown_task = asyncio.create_task(_shutdown_approval_store())
    await asyncio.sleep(0)
    assert not shutdown_task.done()

    release_sender.set()
    await asyncio.wait_for(edit_seen.wait(), timeout=1)
    await asyncio.wait_for(shutdown_task, timeout=1)

    assert edits[0]["status"] == "expired"
    assert edits[0]["resolution_reason"] == "Tool approval request was cancelled."
    assert not store._post_cancel_cleanup_tasks


@pytest.mark.asyncio
async def test_shutdown_bounds_cancelled_send_cleanup_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mindroom.approval_manager._SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.01)
    send_started = asyncio.Event()
    never_release_sender = asyncio.Event()

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        _content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        send_started.set()
        await never_release_sender.wait()
        return SentApprovalEvent("$approval")

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=AsyncMock())
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert store._post_cancel_cleanup_tasks

    await asyncio.wait_for(_shutdown_approval_store(), timeout=1)

    assert not store._post_cancel_cleanup_tasks


@pytest.mark.asyncio
async def test_request_approval_cancelled_after_a_real_transport_send_leaves_no_card(tmp_path: Path) -> None:
    """Cancelling a sent approval expires it in the room and clears its debt.

    Runs the real Matrix transport rather than a mock sender, because the card
    is recorded on the transport's own send path: a card left behind here is
    one a later startup would expire a second time.
    """
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
    cards = FakeApprovalCards()
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=orchestrator._approval_transport.send_approval_event,
        editor=editor,
        cards=cards,
    )

    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    approval_id = await _wait_for_room_send_approval_id(client)
    pending = await _wait_for_pending(store, room_id="!room:localhost", approval_id=approval_id)
    assert pending.card_event_id == "$approval"
    assert await cards.pending_approval_card(room_id="!room:localhost", card_event_id="$approval") is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    assert cards.rows == {}


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
async def test_shutdown_expires_approval_send_that_finishes_after_shutdown_starts(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        _content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        send_started.set()
        await release_send.wait()
        return SentApprovalEvent("$approval")

    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)

    shutdown_task = asyncio.create_task(_shutdown_approval_store())
    await asyncio.sleep(0)
    assert shutdown_task.done() is False

    release_send.set()
    await asyncio.wait_for(shutdown_task, timeout=1)
    decision = await asyncio.wait_for(task, timeout=1)

    assert decision.status == "expired"
    assert decision.reason == "MindRoom shut down before approval completed."
    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "MindRoom shut down before approval completed."
    assert get_approval_store() is None


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


@pytest.mark.asyncio
async def test_request_approval_cancel_during_click_resolution_leaves_expired_terminal_edit(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    edit_count = 0
    edits: list[dict[str, Any]] = []

    async def editor(_room_id: str, _event_id: str, content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
        edits.append(content)
        if edit_count == 1:
            edit_started.set()
            await release_edit.wait()
        return True

    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    click_task = asyncio.create_task(
        store.handle_card_response(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id=pending.card_event_id,
            status="approved",
            reason=None,
        ),
    )
    await asyncio.wait_for(edit_started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    release_edit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    click_result = await click_task

    assert click_result.resolved is True
    assert edit_count == 2
    assert edits[-1]["status"] == "expired"
    assert edits[-1]["resolution_reason"] == "Tool approval request was cancelled."


@pytest.mark.asyncio
async def test_request_approval_cancel_during_click_resolution_emits_expired_not_approved(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    release_edit = asyncio.Event()
    edits: list[dict[str, Any]] = []

    async def editor(_room_id: str, _event_id: str, content: dict[str, Any]) -> bool:
        edits.append(content)
        await release_edit.wait()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    click_task = asyncio.create_task(
        store.handle_card_response(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id=pending.card_event_id,
            status="approved",
            reason=None,
        ),
    )
    async with asyncio.timeout(1):
        while True:
            with store._live_lock:
                resolving = pending.card_event_id in store._resolving_card_event_ids
            if resolving:
                break
            await asyncio.sleep(0)

    task.cancel()
    await asyncio.sleep(0)
    release_edit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    click_result = await click_task

    assert click_result.resolved is True
    assert len(edits) == 1
    assert edits[0]["status"] == "expired"
    assert edits[0]["resolution_reason"] == "Tool approval request was cancelled."


@pytest.mark.asyncio
async def test_duplicate_live_response_from_approver_is_consumed_while_resolution_in_progress(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    edit_count = 0

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
        edit_started.set()
        await release_edit.wait()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    first = asyncio.create_task(
        store.handle_card_response(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id=pending.card_event_id,
            status="approved",
            reason=None,
        ),
    )
    await asyncio.wait_for(edit_started.wait(), timeout=1)

    second_result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="denied",
        reason="Clicked twice.",
    )

    release_edit.set()
    first_result = await first
    decision = await task

    assert second_result.consumed is True
    assert second_result.resolved is False
    assert first_result.resolved is True
    assert decision.status == "approved"
    assert edit_count == 1


@pytest.mark.asyncio
async def test_a_sent_card_survives_the_process_that_sent_it(tmp_path: Path) -> None:
    """The card has to be recorded when it is sent, not when it is answered.

    A restart destroys the live waiter, so the durable card is the only thing
    that lets the next process recognise a click on it -- or expire it. If the
    send does not record one, every approval outstanding at a restart becomes
    a button that answers nobody.
    """
    cards = FakeApprovalCards()
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    store = initialize_approval_store(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=AsyncMock(return_value=True),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    stored = await cards.pending_approval_card(room_id="!room:localhost", card_event_id=pending.card_event_id)
    assert stored is not None
    assert stored.resolution is None
    assert stored.card["content"]["approval_id"] == pending.approval_id
    assert stored.card["sender"] == "@mindroom_router:localhost"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_restart_can_answer_a_card_the_previous_process_sent(tmp_path: Path) -> None:
    """What the sending process recorded is what the next one recovers."""
    cards = FakeApprovalCards()
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        # Nothing this process writes to Matrix lands, which is what leaves a
        # clickable card behind for the next one to deal with.
        editor=AsyncMock(side_effect=RuntimeError("process died mid-approval")),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _shutdown_approval_store()

    # A new process, with nothing in memory and the same durable cards.
    editor = AsyncMock(return_value=True)
    restarted = initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[:2] == ("!room:localhost", pending.card_event_id)


@pytest.mark.asyncio
async def test_the_row_exists_before_the_card_reaches_matrix(tmp_path: Path) -> None:
    """The ordering itself, observed from inside the send.

    Everything downstream depends on it: a card the homeserver has accepted
    while no row accounts for it cannot be expired by a restart, and a click on
    it finds neither a live waiter nor a stored card. Recording afterwards
    leaves that window open however small it is, so the check is not "a row
    exists at the end" but "a row existed before the send was made".
    """
    cards = FakeApprovalCards()
    claimed_when_sent: list[tuple[str, str | None]] = []

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        _content: dict[str, Any],
        transaction_id: str,
    ) -> SentApprovalEvent:
        rows = await cards.pending_approval_cards(room_id="!room:localhost")
        claimed_when_sent.extend((row.transaction_id, row.card_event_id) for row in rows)
        # The transaction the row was claimed under is the one being sent, or a
        # repeat could never converge on this event.
        assert transaction_id in {claimed for claimed, _ in claimed_when_sent}
        return SentApprovalEvent("$approval")

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(side_effect=sender),
        editor=AsyncMock(return_value=True),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=store._send_event)  # type: ignore[arg-type] - the AsyncMock above

    # Claimed with no event id, because the homeserver had not answered yet.
    assert claimed_when_sent == [(_approval_transaction_id(pending.approval_id), None)]
    # And pointed at the event once it had.
    assert cards.acknowledged == [(_approval_transaction_id(pending.approval_id), "$approval")]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
async def test_the_sending_device_is_recorded_before_the_card_goes_out(tmp_path: Path) -> None:
    """The device is committed before the send, and never before that.

    Recording it afterwards would leave exactly the rows that matter -- the
    ones a crash interrupted around the send -- with no device on them, and a
    row whose device is unknown is one recovery has to reconcile against the
    room rather than present again.

    Recording it at claim time is the other way to get it wrong: a re-login
    between the claim and the send would leave this device's name against a
    transaction the homeserver never saw from it, and recovery would read that
    as licence to present the transaction again.
    """
    rows_when_claimed: list[tuple[bool, str | None]] = []
    rows_when_sent: list[tuple[bool, str | None]] = []

    class _WatchedCards(FakeApprovalCards):
        async def claim_approval_card(
            self,
            *,
            room_id: str,
            transaction_id: str,
            card: Mapping[str, Any],
        ) -> None:
            await super().claim_approval_card(room_id=room_id, transaction_id=transaction_id, card=card)
            rows_when_claimed.extend((row.attempted, row.sending_device_id) for row in self.rows.values())

    cards = _WatchedCards()

    async def sender(
        _room_id: str,
        _thread_id: str | None,
        _content: dict[str, Any],
        _transaction_id: str,
    ) -> SentApprovalEvent:
        rows_when_sent.extend((row.attempted, row.sending_device_id) for row in cards.rows.values())
        return SentApprovalEvent("$approval")

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=AsyncMock(side_effect=sender),
        editor=AsyncMock(return_value=True),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await _wait_for_pending(store, sender=store._send_event)  # type: ignore[arg-type] - the AsyncMock above

    assert rows_when_claimed == [(False, None)]
    assert rows_when_sent == [(True, CLAIMING_DEVICE_ID)]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_sweep_landing_inside_the_attempt_write_leaves_the_send_alone(tmp_path: Path) -> None:
    """The attempt is committed inside the registered send, never ahead of it.

    Marking the row and registering the send as in flight cannot be two awaited
    steps. A sweep suspended into the gap between them finds an attempted row
    this device could present again with nothing saying it is spoken for, and
    presents it -- a second prompt in the room while the first send is still
    on its way, then expired out from under the request waiting on it.

    Driven by running the sweep from inside the store write itself, which is
    the innermost point the ordering has to hold at.
    """
    sweeps: list[ApprovalStartupSweep] = []

    class _SweepingCards(FakeApprovalCards):
        async def mark_approval_card_attempted(
            self,
            *,
            transaction_id: str,
            sending_device_id: str | None,
        ) -> bool:
            marked = await super().mark_approval_card_attempted(
                transaction_id=transaction_id,
                sending_device_id=sending_device_id,
            )
            sweeps.append(await store.discard_pending_on_startup())
            return marked

    cards = _SweepingCards()
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
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    # The sweep saw the row and left it alone: one card sent, none expired, and
    # the request still waiting on an answer nobody has given.
    assert sweeps == [ApprovalStartupSweep(discarded=0, failed=0)]
    assert sender.await_count == 1
    editor.assert_not_awaited()
    assert pending.card_event_id == "$approval"
    assert set(cards.rows) == {_approval_transaction_id(pending.approval_id)}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_card_response_for_resolved_card_is_not_consumed_without_live_waiter(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    await cards.store_card("$approval", "!room:localhost", _approval_card())
    # The decision landed in the room, which is what drops the card. A user
    # clicking the answered card afterwards must not resolve it a second time.
    await cards.forget_approval_card(transaction_id=transaction_id_for("$approval"))
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
    editor.assert_not_awaited()


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
async def test_live_pending_lookup_ignores_cached_card_after_live_waiter_is_gone(tmp_path: Path) -> None:
    cards = FakeApprovalCards()
    card = _approval_card()
    await cards.store_card("$approval", "!room:localhost", card)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await _live_pending_approval(store, room_id="!room:localhost", approval_id="approval-1") is None


@pytest.mark.asyncio
async def test_live_pending_lookup_does_not_scan_history_when_event_missing(
    tmp_path: Path,
) -> None:
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await _live_pending_approval(store, room_id="!room:localhost", approval_id="approval-1") is None


@pytest.mark.asyncio
async def test_live_pending_lookup_returns_none_for_cross_router_cached_pending_without_live_waiter(
    tmp_path: Path,
) -> None:
    cards = FakeApprovalCards()
    await cards.store_card(
        "$approval",
        "!room:localhost",
        _approval_card(sender="@other_router:localhost"),
    )
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await _live_pending_approval(store, room_id="!room:localhost", approval_id="approval-1") is None


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
async def test_a_decision_that_cannot_be_recorded_is_never_shown(tmp_path: Path) -> None:
    """A store that failed leaves a row still reading as unanswered.

    Showing the decision anyway would release the tool and let the next
    startup expire a card whose tool has already run, so the edit is not even
    attempted and the card stays clickable.
    """
    cards = UnwritableApprovalCards()

    sender_mock = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender_mock)

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    editor.assert_not_awaited()
    assert result.resolved is False
    assert decision.status == "expired"
    assert decision.reason == "Tool approval request could not be delivered to Matrix."
    assert cards.resolutions == {}
    assert cards.stored_event_ids() == {"$approval"}


@pytest.mark.asyncio
async def test_a_decision_no_row_takes_is_never_shown_or_acted_on(tmp_path: Path) -> None:
    """A guarded update that matches nothing is a failure to record, not a commit.

    The row is what makes a decision accountable: it is what a later startup
    reads to redeliver an answer the room may never have been shown. Once it
    is gone, the write updates nothing and raises nothing, and reading that
    silence as a commit would run the tool on a decision no durable record
    agrees with and leave nobody able to repair it.
    """
    cards = FakeApprovalCards()
    sender_mock = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender_mock)
    assert cards.stored_event_ids() == {"$approval"}

    # The row goes away between the card being sent and the human answering.
    await cards.forget_approval_card(transaction_id=_approval_transaction_id(pending.approval_id))

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    editor.assert_not_awaited()
    # The click is still this bot's to swallow; what it must not do is resolve.
    assert result.consumed is True
    assert result.resolved is False
    assert decision.status == "expired"
    assert cards.resolutions == {}


@pytest.mark.asyncio
async def test_a_decision_the_row_already_holds_is_not_replaced_or_reshown(tmp_path: Path) -> None:
    """The first committed decision is the one the room and the tool both get.

    A row takes one decision and refuses the next, silently. If the refusal
    read as a commit, the second answer would be shown in the room and acted
    on while the row still held the first, and the next startup would restore
    the decision that did not happen over the one that did.
    """
    cards = FakeApprovalCards()
    sender_mock = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender_mock)

    # Something else committed a decision to this card first.
    committed = await cards.resolve_approval_card(
        card_event_id="$approval",
        resolution={"status": "approved", "resolution_reason": "Looks fine."},
    )
    assert committed.recorded is True

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="denied",
        reason="Changed my mind.",
    )
    decision = await task

    editor.assert_not_awaited()
    assert result.consumed is True
    assert result.resolved is False
    assert decision.status == "expired"
    assert cards.resolutions["$approval"]["status"] == "approved"


@pytest.mark.asyncio
async def test_a_card_no_row_can_back_is_never_sent(tmp_path: Path) -> None:
    """A card the store will not take must not reach the room at all.

    Nothing could expire it, nothing could redeliver a decision made on it, and
    a click on it would sit exactly one step from releasing a tool nothing
    durable agreed to. Because the claim comes first, that whole class is
    settled by not sending: the request fails closed at the point the record
    failed, and the room never learns an approval was contemplated.
    """
    cards = UnclaimableApprovalCards()
    sender_mock = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    # Short, so a card that stayed answerable would show up as a timeout here
    # rather than as a hang.
    decision = await store.request_approval(
        tool_name="read_file",
        arguments={"path": "notes.txt"},
        room_id="!room:localhost",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        timeout_seconds=0.05,
    )

    assert decision.status == "expired"
    assert decision.reason == "Tool approval request could not be recorded durably, so it cannot be answered."
    # Nothing was sent, so there is nothing to take back either.
    sender_mock.assert_not_awaited()
    editor.assert_not_awaited()

    clicked = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    # No waiter, no row: the click resolves nothing and releases nothing.
    assert clicked.consumed is False
    assert clicked.resolved is False
    assert cards.resolutions == {}
    assert editor.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_edit_does_not_give_one_decision_two_meanings(tmp_path: Path) -> None:
    """The tool gets what was written down, because the room will get it too.

    Composed on purpose: a live half that ends in denial and a restart half
    that shows approval each look correct alone, and only disagree when the
    same card crosses both. Once ``resolution_json`` commits, the decision is
    settled -- the edit failing means the room has not been told yet, not that
    the user decided something else.
    """
    cards = FakeApprovalCards()

    sender_mock = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(side_effect=[False, True])
    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender_mock)

    first_result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task
    second_result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )

    # The edit failed, so the room does not show it yet.
    assert first_result.resolved is False
    # But the decision is committed, and it is the one the user made.
    assert decision.status == "approved"
    assert cards.resolutions["$approval"]["status"] == "approved"
    # A second click cannot replace a decision already recorded, and does not
    # spend a second edit trying.
    assert second_result.resolved is False
    assert editor.await_count == 1

    # A later process finds the recorded decision and shows the same thing the
    # live tool acted on -- not the opposite of it.
    restarted = _ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert (await restarted.discard_pending_on_startup()).discarded == 1
    assert editor.await_args.args[2]["status"] == "approved"
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_wrong_clicker_response_is_not_consumed_and_leaves_card_pending(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@other:localhost",
        card_event_id=pending.card_event_id,
        status="denied",
        reason="Wrong user.",
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()

    approver_result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert approver_result.resolved is True
    assert decision.status == "approved"


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


@pytest.mark.asyncio
async def test_a_sweep_leaves_alone_a_card_whose_send_has_not_come_back(tmp_path: Path) -> None:
    """The row is durable before the send, and the waiter only exists after it.

    In between, the row looks exactly like one a dead process abandoned:
    claimed, no event id, claimed by a device this process can still present
    from. Nothing else marks it as spoken for, because the live waiter that
    would is created out of the send's own return value.

    A sweep landing in that window presents the transaction again -- which
    posts a second card whenever the homeserver has not yet seen the first --
    and then expires it. The request that is still inside its send goes on to
    bind a waiter to a card the room already shows as expired, and blocks
    until its own timeout for an answer no one can now give.

    This used to be a startup-only window. It stopped being one when the sweep
    gained a retry that runs during ordinary operation.
    """
    cards = FakeApprovalCards()
    editor = AsyncMock(return_value=True)
    sending = asyncio.Event()
    finish_send = asyncio.Event()
    sends: list[str] = []

    async def sender(
        room_id: str,  # noqa: ARG001 - matches the transport signature
        thread_id: str | None,  # noqa: ARG001 - matches the transport signature
        content: dict[str, Any],  # noqa: ARG001 - matches the transport signature
        transaction_id: str,
    ) -> SentApprovalEvent:
        sends.append(transaction_id)
        if len(sends) == 1:
            sending.set()
            await finish_send.wait()
        # What the homeserver does with a repeat of a transaction it has
        # already accepted, which is the kindest case for the sweep.
        return SentApprovalEvent("$card")

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    request = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    async with asyncio.timeout(5):
        await sending.wait()

    sweep = await store.discard_pending_on_startup()

    # Not this sweep's row: it belongs to a request that has not finished
    # sending it. Not a failure either -- nothing is owed, so counting it
    # would keep the sweep coming back for a card that is doing fine.
    assert sweep == ApprovalStartupSweep(discarded=0, failed=0)
    assert sends == [transaction_id_for_approval(cards)]
    editor.assert_not_awaited()

    finish_send.set()
    pending = await _wait_for_pending(store, approval_id=_only_claimed_approval_id(cards))
    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await asyncio.wait_for(request, timeout=5)

    assert result.resolved is True
    assert decision.status == "approved"


def _only_claimed_approval_id(cards: FakeApprovalCards) -> str:
    """Return the approval id of the single card the store is holding."""
    (row,) = cards.rows.values()
    return str(row.card["content"]["approval_id"])


def transaction_id_for_approval(cards: FakeApprovalCards) -> str:
    """Return the transaction the single claimed card was sent under."""
    (transaction_id,) = cards.rows
    return transaction_id


@pytest.mark.asyncio
async def test_a_sweep_leaves_alone_a_card_a_cancelled_request_is_still_sending(tmp_path: Path) -> None:
    """Cancelling the requester does not end the send; it hands it to another owner.

    The caller is gone, so the request-side registration goes with it, but the
    send is shielded and still open behind the cleanup that will bind a waiter
    to whatever it returns and expire the card properly. From the sweep the row
    is once again indistinguishable from one a dead process left: claimed, no
    event id, and attempted by a device this process can still present from.

    Acting on it presents the transaction a second time, expires the card, and
    deletes the row -- and because that expiry belongs to no waiter, the
    cleanup then binds one to a card already recorded as decided and waits
    forever for a decision that was made before it existed.
    """
    cards = FakeApprovalCards()
    editor = AsyncMock(return_value=True)
    sending = asyncio.Event()
    finish_send = asyncio.Event()
    sends: list[str] = []

    async def sender(
        room_id: str,  # noqa: ARG001 - matches the transport signature
        thread_id: str | None,  # noqa: ARG001 - matches the transport signature
        content: dict[str, Any],  # noqa: ARG001 - matches the transport signature
        transaction_id: str,
    ) -> SentApprovalEvent:
        sends.append(transaction_id)
        if len(sends) == 1:
            sending.set()
            await finish_send.wait()
        return SentApprovalEvent("$card")

    store = _ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender,
        editor=editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: CLAIMING_DEVICE_ID,
    )

    request = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    async with asyncio.timeout(5):
        await sending.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await request
    # The owner the cancelled request handed the still-open send to.
    (cleanup,) = store._post_cancel_cleanup_tasks

    sweep = await store.discard_pending_on_startup()

    # Still not this sweep's row. The request that owned it is gone, but the
    # send it started is not, and the cleanup standing in for it is the one
    # thing that can expire the card exactly once.
    assert sweep == ApprovalStartupSweep(discarded=0, failed=0)
    assert sends == [transaction_id_for_approval(cards)]
    editor.assert_not_awaited()

    finish_send.set()
    await asyncio.wait_for(asyncio.wrap_future(cleanup.cleanup_future), timeout=5)
    assert not store.has_live_work()

    # One expiry, by the cleanup, against the card the one send produced -- and
    # the row goes with it rather than outliving the card it accounts for.
    assert [call.args[1] for call in editor.await_args_list] == ["$card"]
    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    assert not cards.rows


@pytest.mark.asyncio
async def test_shutdown_gives_up_on_a_decision_a_hung_edit_is_still_holding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown waits for the owner of a claimed resolution, and owners can stall.

    A click claims the card and only completes the waiter once its edit comes
    back. Shutdown, finding the claim taken, waits for that decision rather
    than racing it -- correctly, because it must not hand the caller an answer
    the owner is about to contradict.

    What it must not do is wait without end. The homeserver is the one thing in
    this path that may simply never answer during a teardown, which is why the
    drains below this wait are all bounded -- and every one of them is queued
    behind it, as is ``orchestrator.stop()``, which has no bound of its own.
    """
    monkeypatch.setattr("mindroom.approval_manager._SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.05)
    edit_started = asyncio.Event()
    never_finishes = asyncio.Event()
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        edit_started.set()
        await never_finishes.wait()
        return True

    store = initialize_approval_store(test_runtime_paths(tmp_path), sender=sender, editor=editor)
    request = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)
    click = asyncio.create_task(_resolve_pending_approval(store, pending, status="approved"))
    async with asyncio.timeout(5):
        await edit_started.wait()

    try:
        # Bounded by the test as well, because an unbounded stand-down is the
        # defect: a test that hangs on it reports nothing.
        await asyncio.wait_for(store.shutdown(reason=DEFAULT_SHUTDOWN_REASON), timeout=5)

        # The claim is still held, so shutdown stood down rather than inventing
        # a second decision for a card whose first one is still being written.
        assert not click.done()
        assert not request.done()
    finally:
        never_finishes.set()

    assert (await asyncio.wait_for(click, timeout=5)).resolved is True
    assert (await asyncio.wait_for(request, timeout=5)).status == "approved"


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


@pytest.mark.asyncio
async def test_initialize_approval_store_rejects_storage_root_change_with_pending_waiter(tmp_path: Path) -> None:
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    first_runtime_paths = test_runtime_paths(tmp_path / "first")
    second_runtime_paths = test_runtime_paths(tmp_path / "second")
    store = initialize_approval_store(first_runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender=sender)

    with pytest.raises(RuntimeError, match="Cannot reinitialize approval store"):
        initialize_approval_store(second_runtime_paths)

    result = await _resolve_pending_approval(
        store,
        pending,
        status="approved",
    )
    decision = await task

    assert result.resolved is True
    assert decision.status == "approved"


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


def test_terminal_approval_card_ids_are_bounded(tmp_path: Path) -> None:
    store = _ApprovalManager(test_runtime_paths(tmp_path))

    for index in range(_MAX_REMEMBERED_TERMINAL_CARD_IDS + 1):
        store._remember_resolved_card_event_id(f"$approval-{index}")

    assert store.knows_in_memory_approval_card("$approval-0") is False
    assert store.knows_in_memory_approval_card("$approval-1") is True
    assert store.knows_in_memory_approval_card(f"$approval-{_MAX_REMEMBERED_TERMINAL_CARD_IDS}") is True


def test_terminal_approval_card_ids_drop_discarded_entries(tmp_path: Path) -> None:
    store = _ApprovalManager(test_runtime_paths(tmp_path))

    for index in range(_MAX_REMEMBERED_TERMINAL_CARD_IDS + 1):
        card_event_id = f"$approval-{index}"
        store._remember_cancelled_card_event_id(card_event_id)
        store._forget_cancelled_card_event_id(card_event_id)

    assert len(store._cancelled_card_event_ids) == 0


@pytest.mark.asyncio
async def test_cancelled_fast_path_moves_card_to_resolved_memory(tmp_path: Path) -> None:
    store = _ApprovalManager(test_runtime_paths(tmp_path), editor=AsyncMock())
    waiter = _LiveApprovalWaiter(
        approval_id="approval-1",
        transaction_id="txn-approval-1",
        card_event_id="$approval",
        room_id="!room:localhost",
        card_event=_approval_card(),
        future=asyncio.get_running_loop().create_future(),
    )
    waiter.future.set_result(
        ApprovalDecision(
            status="expired",
            reason="Tool approval request was cancelled.",
            resolved_by=None,
            resolved_at=datetime.now(UTC),
        ),
    )
    store._remember_cancelled_card_event_id(waiter.card_event_id)

    await store._settle_bound_waiter_as_cancelled(waiter)

    assert store._cancelled_card_event_ids_contains("$approval") is False
    assert store.knows_in_memory_approval_card("$approval") is True


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


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_cancelled_cards_recovery(tmp_path: Path) -> None:
    """Shutdown must not return while a card's record-then-expire is in flight.

    That recovery needs the journal store and the Matrix client, and bot
    shutdown closes both immediately after. Returning early lets the record
    fail against a closed store and the expiry edit against a closed client,
    leaving the clickable card with no durable row that detaching the recovery
    was meant to prevent.
    """
    cards = FakeApprovalCards()
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    write_started = asyncio.Event()
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    real_acknowledge = cards.acknowledge_approval_card
    calls = 0

    async def gated_acknowledge(*args: object, **kwargs: object) -> object:
        # First call is the caller's, and is cancelled out from under it.
        # Second is the detached recovery -- the one shutdown must wait for.
        nonlocal calls
        calls += 1
        if calls == 1:
            write_started.set()
            await asyncio.Event().wait()
        recovery_started.set()
        await release_recovery.wait()
        return await real_acknowledge(*args, **kwargs)

    cards.acknowledge_approval_card = gated_acknowledge  # type: ignore[method-assign]

    task = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(write_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(recovery_started.wait(), timeout=5)

    shutdown = asyncio.create_task(store.shutdown(reason="test shutdown"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not shutdown.done(), "shutdown returned while the recovery was still blocked"

    release_recovery.set()
    await asyncio.wait_for(shutdown, timeout=5)

    # Shutdown waited, so the card reached a terminal state while the store and
    # client were still open.
    assert editor.await_args is not None
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_shutdown_bounds_a_cancelled_cards_stalled_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown leaves a stalled terminal edit for the next startup sweep."""
    monkeypatch.setattr("mindroom.approval_manager._SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.01)
    cards = FakeApprovalCards()
    runtime_paths = test_runtime_paths(tmp_path)
    write_started = asyncio.Event()
    detached_edit_started = asyncio.Event()
    release_detached_edit = asyncio.Event()
    real_acknowledge = cards.acknowledge_approval_card
    acknowledge_calls = 0

    async def gated_acknowledge(*args: object, **kwargs: object) -> object:
        nonlocal acknowledge_calls
        acknowledge_calls += 1
        if acknowledge_calls == 1:
            write_started.set()
            await asyncio.Event().wait()
        return await real_acknowledge(*args, **kwargs)

    async def stalled_editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        detached_edit_started.set()
        await release_detached_edit.wait()
        return True

    cards.acknowledge_approval_card = gated_acknowledge  # type: ignore[method-assign]
    store = initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=stalled_editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    request = asyncio.create_task(
        store.request_approval(
            tool_name="read_file",
            arguments={"path": "notes.txt"},
            room_id="!room:localhost",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    await asyncio.wait_for(write_started.wait(), timeout=5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(detached_edit_started.wait(), timeout=5)

    shutdown = asyncio.create_task(_shutdown_approval_store())
    done, _pending = await asyncio.wait({shutdown}, timeout=1)
    try:
        assert shutdown in done, "shutdown waited forever for a Matrix edit that startup can retry"
    finally:
        release_detached_edit.set()
        await asyncio.wait_for(shutdown, timeout=5)

    (retained,) = cards.rows.values()
    assert retained.card_event_id == "$approval"
    assert retained.resolution is not None
    assert retained.resolution["status"] == "expired"

    recovered_edits: list[dict[str, Any]] = []

    async def recovery_editor(_room_id: str, _event_id: str, content: dict[str, Any]) -> bool:
        recovered_edits.append(content)
        return True

    recovered = initialize_approval_store(
        runtime_paths,
        editor=recovery_editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    sweep = await recovered.discard_pending_on_startup()

    assert sweep == ApprovalStartupSweep(discarded=1, failed=0)
    assert recovered_edits[0]["status"] == "expired"
    assert cards.rows == {}


@pytest.mark.asyncio
async def test_shutdown_cancels_detached_card_recovery_on_its_owner_loop(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown cannot leave foreign-loop recovery alive after teardown."""
    monkeypatch.setattr("mindroom.approval_manager._SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.02)
    cards = FakeApprovalCards()
    runtime_paths = test_runtime_paths(tmp_path)
    first_acknowledgement_started = threading.Event()
    detached_edit_started = threading.Event()
    detached_edit_finished = threading.Event()
    real_acknowledge = cards.acknowledge_approval_card
    acknowledge_calls = 0

    async def gated_acknowledge(*args: object, **kwargs: object) -> object:
        nonlocal acknowledge_calls
        acknowledge_calls += 1
        if acknowledge_calls == 1:
            first_acknowledgement_started.set()
            await asyncio.Future()
        return await real_acknowledge(*args, **kwargs)

    async def stalled_editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        detached_edit_started.set()
        try:
            await asyncio.Future()
        finally:
            detached_edit_finished.set()

    cards.acknowledge_approval_card = gated_acknowledge  # type: ignore[method-assign]
    store = initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
        editor=stalled_editor,
        cards=cards,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )
    worker_loop = asyncio.new_event_loop()
    worker_thread = threading.Thread(target=worker_loop.run_forever, daemon=True)
    request_tasks: list[asyncio.Task[ApprovalDecision]] = []
    detached_tasks: list[asyncio.Task[Any]] = []
    request_created = threading.Event()
    request_finished = threading.Event()

    def start_request() -> None:
        request = worker_loop.create_task(
            store.request_approval(
                tool_name="read_file",
                arguments={"path": "notes.txt"},
                room_id="!room:localhost",
                requester_id="@user:localhost",
                approver_user_id="@user:localhost",
                timeout_seconds=30,
            ),
        )
        request_tasks.append(request)
        request.add_done_callback(lambda _task: request_finished.set())
        request_created.set()

    worker_thread.start()
    worker_loop.call_soon_threadsafe(start_request)
    try:
        assert await asyncio.to_thread(request_created.wait, 5)
        assert await asyncio.to_thread(first_acknowledgement_started.wait, 5)
        worker_loop.call_soon_threadsafe(request_tasks[0].cancel)
        assert await asyncio.to_thread(request_finished.wait, 5)
        assert await asyncio.to_thread(detached_edit_started.wait, 5)

        snapshot_taken = threading.Event()

        def snapshot_tasks() -> None:
            detached_tasks.extend(task for task in asyncio.all_tasks(worker_loop) if not task.done())
            snapshot_taken.set()

        worker_loop.call_soon_threadsafe(snapshot_tasks)
        assert await asyncio.to_thread(snapshot_taken.wait, 5)
        assert len(detached_tasks) == 1

        await asyncio.wait_for(_shutdown_approval_store(), timeout=5)

        assert detached_tasks[0].done(), "foreign-loop recovery was still running after shutdown returned"
        assert detached_edit_finished.is_set()
    finally:
        for task in [*request_tasks, *detached_tasks]:
            if not task.done():
                worker_loop.call_soon_threadsafe(task.cancel)
        if detached_edit_started.is_set():
            await asyncio.to_thread(detached_edit_finished.wait, 5)
        worker_loop.call_soon_threadsafe(worker_loop.stop)
        await asyncio.to_thread(worker_thread.join, 5)
        worker_loop.close()
