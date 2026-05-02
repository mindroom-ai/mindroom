"""Tests for Matrix-backed tool approval state."""
# ruff: noqa: D101,D102,D103

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from pydantic import ValidationError

from mindroom.approval_inbound import handle_tool_approval_action
from mindroom.approval_manager import (
    ApprovalManager,
    PendingApproval,
    SentApprovalEvent,
    _build_event_arguments_preview,
    get_approval_store,
    initialize_approval_store,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.logging_config import get_logger
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.tool_approval import (
    MatrixApprovalAction,
    ToolApprovalCall,
    ToolApprovalScriptError,
    evaluate_tool_approval,
    handle_matrix_approval_action,
    is_process_approval_card,
    request_tool_approval_for_call,
    resolve_tool_approval_approver,
    shutdown_approval_store,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


class FakeEventCache:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        return self.events.get((room_id, event_id))

    async def get_latest_edit(
        self,
        room_id: str,
        original_event_id: str,
        *,
        sender: str | None = None,
    ) -> dict[str, Any] | None:
        edits: list[dict[str, Any]] = []
        for (event_room_id, _), event in self.events.items():
            if event_room_id != room_id or (sender is not None and event.get("sender") != sender):
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            relates_to = content.get("m.relates_to")
            if not isinstance(relates_to, dict):
                continue
            if relates_to.get("rel_type") == "m.replace" and relates_to.get("event_id") == original_event_id:
                edits.append(event)
        if not edits:
            return None
        return max(edits, key=lambda event: int(event.get("origin_server_ts", 0)))

    async def get_recent_room_events(
        self,
        room_id: str,
        *,
        event_type: str,
        since_ts_ms: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        events = [
            event
            for (event_room_id, _), event in self.events.items()
            if event_room_id == room_id
            and event.get("type") == event_type
            and int(event.get("origin_server_ts", 0)) >= since_ts_ms
        ]
        return sorted(events, key=lambda event: int(event["origin_server_ts"]), reverse=True)[:limit]

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        self.events[(room_id, event_id)] = event_data


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    asyncio.run(shutdown_approval_store())
    yield
    asyncio.run(shutdown_approval_store())


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        test_runtime_paths(tmp_path),
    )


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
        "requester_id": approver,
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
    store: ApprovalManager,
    sender: AsyncMock,
    *,
    room_id: str = "!room:localhost",
) -> PendingApproval:
    async with asyncio.timeout(5):
        while True:
            if sender.await_args is not None:
                approval_id = sender.await_args.args[2]["approval_id"]
                if store._live_card_event_id_for_approval(approval_id) is not None:
                    return await _wait_for_pending_by_id(store, room_id, approval_id)
            await asyncio.sleep(0)


async def _wait_for_pending_by_id(store: ApprovalManager, room_id: str, approval_id: str) -> PendingApproval:
    async with asyncio.timeout(5):
        while True:
            pending = await store.get_pending_approval(room_id, approval_id)
            if pending is not None:
                return pending
            await asyncio.sleep(0)


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
    pending = await _wait_for_pending(store, sender)

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
async def test_live_card_response_ignores_cached_terminal_edit_from_different_sender(tmp_path: Path) -> None:
    cache = FakeEventCache()
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=sender,
        editor=editor,
        event_cache=cache,
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
    pending = await _wait_for_pending(store, sender)
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
    await cache.store_event("$fake-edit", "!room:localhost", fake_edit)

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
    pending = await _wait_for_pending(store, sender)

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

    await store.resolve_approval(
        card_event_id=pending.card_event_id,
        room_id=pending.room_id,
        status="denied",
        reason="Denied by approver.",
        resolved_by="@user:localhost",
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
    pending = await _wait_for_pending(store, sender, room_id="!room-a:localhost")

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

    await store.resolve_approval(
        card_event_id=pending.card_event_id,
        room_id=pending.room_id,
        status="denied",
        reason="cleanup",
        resolved_by="@user:localhost",
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
    pending = await _wait_for_pending(store, sender, room_id="!room-a:localhost")

    result = await store.handle_live_approval_id_response(
        room_id="!room-a:localhost",
        sender_id="@user:localhost",
        approval_id=pending.approval_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert result.resolved is True
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
    pending = await _wait_for_pending(store, sender, room_id="!room-a:localhost")

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

    await store.resolve_approval(
        card_event_id=pending.card_event_id,
        room_id=pending.room_id,
        status="denied",
        reason="cleanup",
        resolved_by="@user:localhost",
    )
    await task


@pytest.mark.asyncio
async def test_handle_card_response_orphan_approval_falls_through_until_startup_cleanup(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
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

    assert await store.discard_pending_on_startup() == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "expired"


@pytest.mark.asyncio
async def test_request_approval_truncated_approval_fails_closed(tmp_path: Path) -> None:
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
    pending = await _wait_for_pending(store, sender)

    await store.resolve_approval(
        card_event_id=pending.card_event_id,
        room_id=pending.room_id,
        status="approved",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert decision.status == "denied"
    assert "displayed arguments are truncated" in (decision.reason or "")
    assert editor.await_args.args[2]["status"] == "denied"


@pytest.mark.asyncio
async def test_truncated_approval_action_sends_denial_notice(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="write_file",
            arguments={"content": "x" * 10_000},
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            timeout_seconds=30,
        ),
    )
    pending = await _wait_for_pending(store, sender)
    room = MagicMock(room_id="!room:localhost", canonical_alias=None)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            authorization=AuthorizationConfig(global_users=["@user:localhost"]),
        ),
        runtime_paths,
    )
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
    pending = await _wait_for_pending(store, sender)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    assert await store.get_pending_approval("!room:localhost", pending.approval_id) is None


@pytest.mark.asyncio
async def test_request_approval_cancel_after_event_id_before_sender_return_emits_expired_edit(tmp_path: Path) -> None:
    event_committed = asyncio.Event()
    release_sender = asyncio.Event()
    edit_seen = asyncio.Event()
    sent_content: dict[str, Any] = {}

    async def sender(_room_id: str, _thread_id: str | None, content: dict[str, Any]) -> SentApprovalEvent:
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

    async def sender(_room_id: str, _thread_id: str | None, content: dict[str, Any]) -> SentApprovalEvent:
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

    async def sender(_room_id: str, _thread_id: str | None, content: dict[str, Any]) -> SentApprovalEvent:
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

    async def sender(_room_id: str, _thread_id: str | None, _content: dict[str, Any]) -> SentApprovalEvent:
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

    shutdown_task = asyncio.create_task(shutdown_approval_store())
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
    monkeypatch.setattr("mindroom.approval_manager._POST_CANCEL_CLEANUP_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    send_started = asyncio.Event()
    never_release_sender = asyncio.Event()

    async def sender(_room_id: str, _thread_id: str | None, _content: dict[str, Any]) -> SentApprovalEvent:
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

    await asyncio.wait_for(shutdown_approval_store(), timeout=1)

    assert not store._post_cancel_cleanup_tasks


@pytest.mark.asyncio
async def test_request_approval_cleans_up_when_cache_write_is_cancelled_after_room_send(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    cache_started = asyncio.Event()
    release_cache = asyncio.Event()

    async def cache_after_send(*_args: object, **_kwargs: object) -> None:
        cache_started.set()
        await release_cache.wait()

    orchestrator._approval_transport.cache_approval_event_now = AsyncMock(side_effect=cache_after_send)
    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$approval", room_id="!room:localhost"))
    bot = MagicMock(agent_name="router", running=True, client=client)
    orchestrator.agent_bots = {"router": bot}
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(
        runtime_paths,
        sender=orchestrator._approval_transport.send_approval_event,
        editor=editor,
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
    await asyncio.wait_for(cache_started.wait(), timeout=1)
    approval_id = client.room_send.await_args.kwargs["content"]["approval_id"]
    assert await _wait_for_pending_by_id(store, "!room:localhost", approval_id) is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    cache_task = next(iter(orchestrator._approval_transport._cache_write_tasks))
    release_cache.set()
    await asyncio.wait_for(cache_task, timeout=1)
    assert not orchestrator._approval_transport._cache_write_tasks


@pytest.mark.asyncio
async def test_approval_thread_relation_uses_requesting_agent_cache(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    sent_contents: list[dict[str, Any]] = []

    async def room_send(
        *,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> nio.RoomSendResponse:
        assert room_id == "!room:localhost"
        assert message_type == "io.mindroom.tool_approval"
        sent_contents.append(content)
        event_id = "$approval-edit" if "m.new_content" in content else "$approval"
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

    router_client = MagicMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    router_client.room_send = AsyncMock(side_effect=room_send)
    router_bot = MagicMock(agent_name="router", running=True, client=router_client)
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

    assert sent == SentApprovalEvent(event_id="$approval")
    assert edited is True
    assert sent_contents[0]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$code-latest"
    assert sent_contents[1]["m.new_content"]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$code-latest"
    assert code_bot.latest_thread_event_id_if_needed.await_count == 2
    router_bot.latest_thread_event_id_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_transport_refuses_encrypted_room_without_e2ee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    monkeypatch.setattr("mindroom.matrix.client_delivery.crypto.ENCRYPTION_ENABLED", False)

    room = nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost", encrypted=True)
    router_client = MagicMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.rooms = {"!room:localhost": room}
    router_client.room_send = AsyncMock()
    router_bot = MagicMock(agent_name="router", running=True, client=router_client)
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

    async def sender(_room_id: str, _thread_id: str | None, _content: dict[str, Any]) -> SentApprovalEvent:
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

    shutdown_task = asyncio.create_task(shutdown_approval_store())
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
    pending = await _wait_for_pending(store, sender)
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
    pending = await _wait_for_pending(store, sender)
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
    pending = await _wait_for_pending(store, sender)
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
async def test_get_pending_approval_returns_none_for_resolved_card(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    await cache.store_event(
        "$edit",
        "!room:localhost",
        {
            "event_id": "$edit",
            "sender": "@mindroom_router:localhost",
            "type": "io.mindroom.tool_approval",
            "origin_server_ts": card["origin_server_ts"] + 1,
            "content": {
                **card["content"],
                "status": "approved",
                "m.new_content": {**card["content"], "status": "approved"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
            },
        },
    )
    store = ApprovalManager(test_runtime_paths(tmp_path), event_cache=cache)

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None


@pytest.mark.asyncio
async def test_card_response_for_cached_approval_is_not_consumed_without_live_waiter(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
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
async def test_get_pending_approval_ignores_cached_card_after_live_waiter_is_gone(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        event_cache=cache,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None


@pytest.mark.asyncio
async def test_startup_discard_ignores_cached_terminal_edit_from_different_sender(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card(sender="@mindroom_router:localhost")
    fake_edit = _approval_edit(card, sender="@attacker:localhost", status="approved")
    await cache.store_event("$approval", "!room:localhost", card)
    await cache.store_event("$fake-edit", "!room:localhost", fake_edit)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_startup_discard_uses_trusted_cached_terminal_edit_despite_newer_untrusted_edit(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    card = _approval_card(sender="@mindroom_router:localhost")
    trusted_edit = _approval_edit(card, event_id="$trusted-edit", status="approved")
    fake_edit = _approval_edit(card, event_id="$fake-edit", sender="@attacker:localhost", status="denied")
    fake_edit["origin_server_ts"] = int(trusted_edit["origin_server_ts"]) + 1
    await cache.store_event("$approval", "!room:localhost", card)
    await cache.store_event("$trusted-edit", "!room:localhost", trusted_edit)
    await cache.store_event("$fake-edit", "!room:localhost", fake_edit)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_pending_approval_does_not_scan_history_when_event_missing(
    tmp_path: Path,
) -> None:
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None


@pytest.mark.asyncio
async def test_get_pending_approval_returns_none_for_cross_router_cached_pending_without_live_waiter(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    await cache.store_event(
        "$approval",
        "!room:localhost",
        _approval_card(sender="@other_router:localhost"),
    )
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        event_cache=cache,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None


@pytest.mark.asyncio
async def test_response_for_unknown_card_does_not_emit_terminal_edit(tmp_path: Path) -> None:
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
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
async def test_response_for_unknown_card_does_not_read_cache(tmp_path: Path) -> None:
    cache = MagicMock()
    cache.get_event = AsyncMock(side_effect=RuntimeError("cache should not run"))
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
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
    cache.get_event.assert_not_awaited()
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_response_ignores_same_router_cached_pending_without_history_scan(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    result = await store.handle_card_response(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="denied",
        reason="No.",
    )

    assert result.consumed is False
    assert result.resolved is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_response_ignores_cross_router_matrix_only_card(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card(sender="@router_a:localhost"))
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
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
async def test_concurrent_cached_response_events_fall_through_without_terminal_edits(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    edit_count = 0

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
        return True

    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        event_cache=cache,
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

    assert first_result.consumed is False
    assert second_result.consumed is False
    assert first_result.resolved is False
    assert second_result.resolved is False
    assert edit_count == 0


@pytest.mark.asyncio
async def test_failed_terminal_edit_keeps_card_terminal_in_process(tmp_path: Path) -> None:
    cache = FakeEventCache()

    async def sender(room_id: str, _thread_id: str | None, content: dict[str, Any]) -> SentApprovalEvent:
        await cache.store_event(
            "$approval",
            room_id,
            {
                "event_id": "$approval",
                "room_id": room_id,
                "sender": "@mindroom_router:localhost",
                "type": "io.mindroom.tool_approval",
                "origin_server_ts": int(datetime.now(UTC).timestamp() * 1000),
                "content": content,
            },
        )
        return SentApprovalEvent("$approval")

    sender_mock = AsyncMock(side_effect=sender)
    editor = AsyncMock(side_effect=[False, True])
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        sender=sender_mock,
        editor=editor,
        event_cache=cache,
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
    pending = await _wait_for_pending(store, sender_mock)

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

    assert first_result.resolved is False
    assert decision.status == "denied"
    assert decision.reason == "Tool approval request could not be delivered to Matrix."
    assert second_result.resolved is False
    assert editor.await_count == 1


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
    pending = await _wait_for_pending(store, sender)

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
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())

    async def editor(room_id: str, event_id: str, content: dict[str, Any]) -> bool:
        await cache.store_event(
            "$edit",
            room_id,
            {
                "event_id": "$edit",
                "sender": "@mindroom_router:localhost",
                "type": "io.mindroom.tool_approval",
                "origin_server_ts": int(datetime.now(UTC).timestamp() * 1000),
                "content": {
                    **content,
                    "m.new_content": content,
                    "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
                },
            },
        )
        return True

    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 1
    assert await store.discard_pending_on_startup() == 0
    latest_edit = await cache.get_latest_edit("!room:localhost", "$approval")
    assert latest_edit is not None
    assert latest_edit["content"]["m.new_content"]["status"] == "expired"
    assert latest_edit["content"]["m.new_content"]["resolution_reason"] == (
        "Bot restarted before approval — original request was cancelled."
    )


@pytest.mark.asyncio
async def test_discard_pending_on_startup_uses_cached_cards_without_history_scan(tmp_path: Path) -> None:
    cache = FakeEventCache()
    cached_card = _approval_card(approval_id="cached-approval", event_id="$cached-approval")
    await cache.store_event("$cached-approval", "!room:localhost", cached_card)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 1
    assert {call.args[1] for call in editor.await_args_list} == {"$cached-approval"}


@pytest.mark.asyncio
async def test_discard_pending_on_startup_scans_more_than_500_cached_cards(tmp_path: Path) -> None:
    cache = FakeEventCache()
    for index in range(501):
        event_id = f"$approval-{index}"
        await cache.store_event(
            event_id,
            "!room:localhost",
            _approval_card(
                approval_id=f"approval-{index}",
                event_id=event_id,
                origin_server_ts=int(datetime.now(UTC).timestamp() * 1000) + index,
            ),
        )
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 501
    assert editor.await_count == 501


@pytest.mark.asyncio
async def test_discard_pending_on_startup_expires_same_router_cached_cards(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    replacement = editor.await_args.args[2]
    assert replacement["status"] == "expired"
    assert replacement["resolution_reason"] == "Bot restarted before approval — original request was cancelled."


@pytest.mark.asyncio
async def test_discard_pending_on_startup_preserves_same_router_cache_hit(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 1
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_discard_pending_on_startup_skips_cross_router_cached_cards(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card(sender="@other_router:localhost"))
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_pending_on_startup_skips_same_router_cached_terminal_edit(
    tmp_path: Path,
) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    await cache.store_event("$approval-edit", "!room:localhost", _approval_edit(card, status="approved"))
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_pending_on_startup_skips_other_routers_cards(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card(sender="@other_router:localhost"))
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.discard_pending_on_startup() == 0
    editor.assert_not_awaited()


def test_pending_approval_from_card_event_requires_approver_user_id() -> None:
    card = _approval_card()
    card["content"].pop("approver_user_id")

    with pytest.raises(ValueError, match="missing required approval fields"):
        PendingApproval.from_card_event(card, room_id="!room:localhost")


def test_approval_arguments_preview_marks_sanitizer_truncation() -> None:
    arguments = {f"k{index}": index for index in range(30)}
    preview, truncated = _build_event_arguments_preview(arguments)

    assert preview["__truncated__"] == "5 more items"
    assert truncated is True

    card = ApprovalManager._pending_event_content(
        approval_id="approval-1",
        tool_name="read_file",
        arguments=preview,
        arguments_truncated=truncated,
        agent_name="code",
        room_id="!room:localhost",
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
    pending = await _wait_for_pending(store, sender)

    with pytest.raises(RuntimeError, match="Cannot reinitialize approval store"):
        initialize_approval_store(second_runtime_paths)

    result = await store.resolve_approval(
        card_event_id=pending.card_event_id,
        room_id=pending.room_id,
        status="approved",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert result.resolved is True
    assert decision.status == "approved"


def test_resolve_tool_approval_approver_rejects_internal_users(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(tmp_path)
    internal_user_id = config.get_mindroom_user_id(runtime_paths)

    assert resolve_tool_approval_approver(config, runtime_paths, None) is None
    assert resolve_tool_approval_approver(config, runtime_paths, "@agent:localhost") == "@agent:localhost"
    assert resolve_tool_approval_approver(config, runtime_paths, internal_user_id) is None


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
