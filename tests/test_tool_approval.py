"""Tests for Matrix-backed tool approval state."""
# ruff: noqa: D101,D102,D103

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.tool_approval import (
    ApprovalManager,
    PendingApproval,
    SentApprovalEvent,
    ToolApprovalScriptError,
    evaluate_tool_approval,
    get_approval_store,
    initialize_approval_store,
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
        self.latest_edits: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        return self.events.get((room_id, event_id))

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        return self.latest_edits.get((room_id, original_event_id))

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
        content = event_data.get("content")
        if isinstance(content, dict):
            relates_to = content.get("m.relates_to")
            if isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.replace":
                original_event_id = relates_to.get("event_id")
                if isinstance(original_event_id, str):
                    self.latest_edits[(room_id, original_event_id)] = event_data


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    asyncio.run(shutdown_approval_store())
    yield
    asyncio.run(shutdown_approval_store())


def _config(tmp_path: Path, *, requester_id: str = "@user:localhost") -> Config:
    del requester_id
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", role="Help with coding", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
        ),
        test_runtime_paths(tmp_path),
    )


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
    status: str = "approved",
) -> dict[str, Any]:
    content = {**card["content"], "status": status}
    return {
        "event_id": event_id,
        "room_id": card["room_id"],
        "sender": card["sender"],
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
    async with asyncio.timeout(1):
        while True:
            if sender.await_args is not None:
                approval_id = sender.await_args.args[2]["approval_id"]
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
    result = await store.handle_response_event(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task

    assert result.handled is True
    assert decision.status == "approved"
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "approved"
    assert editor.await_args.args[2]["approver_user_id"] == "@user:localhost"


@pytest.mark.asyncio
async def test_handle_response_event_wrong_clicker_noops(tmp_path: Path) -> None:
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

    result = await store.handle_response_event(
        room_id="!room:localhost",
        sender_id="@other:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    assert result.handled is False
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
    cache = FakeEventCache()
    card = _approval_card(arguments_truncated=True)
    await cache.store_event("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(test_runtime_paths(tmp_path), event_cache=cache, editor=editor)
    room = MagicMock(room_id="!room:localhost")
    bot = MagicMock()
    bot._sender_can_resolve_tool_approval.return_value = True
    bot._should_send_tool_approval_notice.return_value = True
    bot.orchestrator = MagicMock()
    bot.orchestrator._send_approval_notice = AsyncMock(return_value=True)

    with patch("mindroom.bot.get_approval_store", return_value=store):
        handled = await AgentBot._handle_tool_approval_action(
            bot,
            room=room,
            sender_id="@user:localhost",
            approval_event_id="$approval",
            status="approved",
            reason=None,
        )

    assert handled is True
    assert editor.await_args.args[2]["status"] == "denied"
    notice = bot.orchestrator._send_approval_notice
    notice.assert_awaited_once()
    assert notice.await_args.kwargs == {
        "room_id": "!room:localhost",
        "approval_event_id": "$approval",
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
async def test_request_approval_cleans_up_when_cache_write_is_cancelled_after_room_send(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    cache_started = asyncio.Event()
    release_cache = asyncio.Event()

    async def cache_after_send(*_args: object, **_kwargs: object) -> None:
        cache_started.set()
        await release_cache.wait()

    orchestrator._cache_approval_event_now = AsyncMock(side_effect=cache_after_send)
    client = MagicMock()
    client.user_id = "@mindroom_router:localhost"
    client.rooms = {"!room:localhost": object()}
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$approval", room_id="!room:localhost"))
    bot = MagicMock(agent_name="router", running=True, client=client)
    orchestrator.agent_bots = {"router": bot}
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, sender=orchestrator._send_approval_event, editor=editor)

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
    assert await store.get_pending_approval("!room:localhost", approval_id) is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."
    cache_task = next(iter(orchestrator._approval_cache_write_tasks))
    release_cache.set()
    await asyncio.wait_for(cache_task, timeout=1)
    assert not orchestrator._approval_cache_write_tasks


@pytest.mark.asyncio
async def test_request_approval_cancel_during_click_resolution_emits_one_terminal_edit(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=SentApprovalEvent("$approval"))
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    edit_count = 0

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
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
        store.handle_response_event(
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

    assert click_result.handled is True
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
async def test_get_pending_approval_history_fallback_applies_replacement_edits(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    editor = AsyncMock(return_value=True)
    scanner = AsyncMock(return_value=[_approval_edit(card), card])
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        room_event_scanner=scanner,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None
    assert await store.auto_deny_pending_on_startup() == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_pending_approval_history_scan_without_edit_keeps_cached_terminal_state(tmp_path: Path) -> None:
    cache = FakeEventCache()
    card = _approval_card()
    await cache.store_event("$approval", "!room:localhost", card)
    await cache.store_event("$approval-edit", "!room:localhost", _approval_edit(card))
    scanner = AsyncMock(return_value=[card])
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        event_cache=cache,
        room_event_scanner=scanner,
    )

    assert await store.get_pending_approval("!room:localhost", "approval-1") is None


@pytest.mark.asyncio
async def test_get_pending_approval_cache_miss_falls_back_to_room_get_event(tmp_path: Path) -> None:
    card = _approval_card()
    fetcher = AsyncMock(return_value=card)
    editor = AsyncMock(return_value=True)
    store = ApprovalManager(test_runtime_paths(tmp_path), event_fetcher=fetcher, editor=editor)

    result = await store.handle_response_event(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id="$approval",
        status="approved",
        reason=None,
    )

    assert result.handled is True
    fetcher.assert_awaited_once_with("!room:localhost", "$approval")


@pytest.mark.asyncio
async def test_concurrent_response_events_emit_one_terminal_edit(tmp_path: Path) -> None:
    cache = FakeEventCache()
    await cache.store_event("$approval", "!room:localhost", _approval_card())
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()
    edit_count = 0

    async def editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
        nonlocal edit_count
        edit_count += 1
        edit_started.set()
        await release_edit.wait()
        return True

    store = ApprovalManager(test_runtime_paths(tmp_path), event_cache=cache, editor=editor)
    first = asyncio.create_task(
        store.handle_response_event(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            status="approved",
            reason=None,
        ),
    )
    await asyncio.wait_for(edit_started.wait(), timeout=1)
    second = asyncio.create_task(
        store.handle_response_event(
            room_id="!room:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            status="denied",
            reason="Clicked elsewhere.",
        ),
    )
    await asyncio.sleep(0)
    release_edit.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert [first_result.handled, second_result.handled].count(True) == 1
    assert edit_count == 1


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

    first_result = await store.handle_response_event(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )
    decision = await task
    second_result = await store.handle_response_event(
        room_id="!room:localhost",
        sender_id="@user:localhost",
        card_event_id=pending.card_event_id,
        status="approved",
        reason=None,
    )

    assert first_result.handled is True
    assert decision.status == "denied"
    assert decision.reason == "Tool approval request could not be delivered to Matrix."
    assert second_result.handled is False
    assert editor.await_count == 1


@pytest.mark.asyncio
async def test_get_pending_approval_room_history_scan_when_event_missing(tmp_path: Path) -> None:
    scanner = AsyncMock(return_value=[_approval_card()])
    store = ApprovalManager(test_runtime_paths(tmp_path), room_event_scanner=scanner)

    pending = await store.get_pending_approval("!room:localhost", "approval-1")

    assert pending is not None
    assert pending.approver_user_id == "@user:localhost"
    scanner.assert_awaited()


@pytest.mark.asyncio
async def test_auto_deny_pending_on_startup_emits_replace_for_each_unresolved_card(tmp_path: Path) -> None:
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

    assert await store.auto_deny_pending_on_startup() == 1
    assert await store.auto_deny_pending_on_startup() == 0
    latest_edit = await cache.get_latest_edit("!room:localhost", "$approval")
    assert latest_edit is not None
    assert latest_edit["content"]["m.new_content"]["resolution_reason"] == (
        "Bot restarted before approval — original request was cancelled."
    )


@pytest.mark.asyncio
async def test_auto_deny_pending_on_startup_merges_cached_and_history_cards(tmp_path: Path) -> None:
    cache = FakeEventCache()
    cached_card = _approval_card(approval_id="cached-approval", event_id="$cached-approval")
    history_card = _approval_card(approval_id="history-approval", event_id="$history-approval")
    await cache.store_event("$cached-approval", "!room:localhost", cached_card)
    editor = AsyncMock(return_value=True)
    scanner = AsyncMock(return_value=[history_card])
    store = ApprovalManager(
        test_runtime_paths(tmp_path),
        editor=editor,
        event_cache=cache,
        room_event_scanner=scanner,
        approval_room_ids=lambda: {"!room:localhost"},
        transport_sender=lambda: "@mindroom_router:localhost",
    )

    assert await store.auto_deny_pending_on_startup() == 2
    assert {call.args[1] for call in editor.await_args_list} == {"$cached-approval", "$history-approval"}


@pytest.mark.asyncio
async def test_auto_deny_pending_on_startup_skips_other_routers_cards(tmp_path: Path) -> None:
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

    assert await store.auto_deny_pending_on_startup() == 0
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_json_approval_files_are_purged(tmp_path: Path) -> None:
    runtime_paths = test_runtime_paths(tmp_path)
    legacy_dir = runtime_paths.storage_root / "approvals"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "old.json").write_text("{}", encoding="utf-8")

    ApprovalManager(runtime_paths)

    assert not (legacy_dir / "old.json").exists()


def test_pending_approval_from_card_event_requires_approver_user_id() -> None:
    card = _approval_card()
    card["content"].pop("approver_user_id")

    with pytest.raises(ValueError, match="missing required approval fields"):
        PendingApproval.from_card_event(card, room_id="!room:localhost")


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

    requires_approval, matched_rule, script_path, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is True
    assert matched_rule == "read_*"
    assert script_path is None
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
