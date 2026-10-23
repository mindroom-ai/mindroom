"""Behavioral coverage for per-thread auto-approve windows that skip tool approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import nio
import pytest
from agno.models.response import ToolExecution

from mindroom.approval import auto_approve_windows
from mindroom.approval.auto_approve_windows import (
    auto_approve_expires_at,
    end_auto_approve,
    room_auto_approve_windows,
    start_auto_approve,
)
from mindroom.approval.manager import ApprovalManager
from mindroom.approval.response import plan_approval_calls
from mindroom.approval.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.commands import auto_approve_commands
from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import CommandType, command_parser, get_command_help
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalDecision, StoredApprovalCard, UnreadableApprovalCard
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import make_conversation_reader_mock, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread"
HUMAN = "@human:localhost"


def _start(runtime_paths: RuntimePaths, **overrides: object) -> datetime:
    scope = {"room_id": ROOM_ID, "thread_id": THREAD_ID, "requester_id": HUMAN, "duration": timedelta(minutes=30)}
    return start_auto_approve(runtime_paths, **{**scope, **overrides})  # type: ignore[arg-type]


def test_auto_approve_window_is_scoped_to_room_thread_and_requester(tmp_path: Path) -> None:
    """A window covers only the conversation and the person that started it, until it ends."""
    runtime_paths = test_runtime_paths(tmp_path)
    expires_at = _start(runtime_paths)

    assert timedelta(minutes=29) < expires_at - datetime.now(UTC) <= timedelta(minutes=30)
    assert (
        auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) == expires_at
    )
    for room_id, thread_id, requester_id in (
        ("!other:localhost", THREAD_ID, HUMAN),
        (ROOM_ID, "$other", HUMAN),
        (ROOM_ID, None, HUMAN),
        (ROOM_ID, THREAD_ID, "@other:localhost"),
        (None, THREAD_ID, HUMAN),
        (ROOM_ID, THREAD_ID, None),
    ):
        assert (
            auto_approve_expires_at(runtime_paths, room_id=room_id, thread_id=thread_id, requester_id=requester_id)
            is None
        )

    other_thread = _start(runtime_paths, thread_id="$other")
    _start(runtime_paths, room_id="!elsewhere:localhost")
    assert [
        (window.thread_id, window.requester_id, window.expires_at)
        for window in room_auto_approve_windows(runtime_paths, ROOM_ID)
    ] == [
        ("$other", HUMAN, other_thread),
        (THREAD_ID, HUMAN, expires_at),
    ]
    assert end_auto_approve(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is True
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None
    assert end_auto_approve(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is False


def test_auto_approve_window_expires_and_survives_unreadable_records(tmp_path: Path) -> None:
    """An elapsed window no longer applies, and corrupt records never read as active."""
    runtime_paths = test_runtime_paths(tmp_path)
    _start(runtime_paths, duration=timedelta(seconds=-1))
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None

    path = auto_approve_windows._store_path(runtime_paths)
    path.write_text('{"x": {"expires_at": "soon", "set_at": "now"}, "y": {"expires_at": "2999-01-01T00:00:00"}}')
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None
    path.write_text("not json")
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None
    path.write_text('{"not a scope": {"expires_at": "2999-01-01T00:00:00+00:00", "set_at": "now"}}')
    assert room_auto_approve_windows(runtime_paths, ROOM_ID) == []
    _start(runtime_paths)
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is not None


async def _plan(runtime_paths: RuntimePaths, tool: ToolExecution, *, requester_id: str, thread_id: str) -> object:
    plan = await plan_approval_calls(
        ((tool, "call-1", tool.tool_name or "", "code"),),
        config=Config(tool_approval=ToolApprovalConfig(default="require_approval")),
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        toolkit_owners={("code", tool.tool_name or ""): "test_toolkit"},
        room_id=ROOM_ID,
        thread_id=thread_id,
    )
    [call] = plan.calls
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_type", [POLICY_CONFIRMATION_APPROVAL_TYPE, None])
async def test_auto_approve_window_approves_every_gated_call_of_its_requester(
    tmp_path: Path,
    approval_type: str | None,
) -> None:
    """Policy gates and a tool's own confirmation both run unasked inside the requester's window."""
    runtime_paths = test_runtime_paths(tmp_path)
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="shell",
        tool_args={"command": "true"},
        requires_confirmation=True,
        approval_type=approval_type,
    )
    assert (await _plan(runtime_paths, tool, requester_id=HUMAN, thread_id=THREAD_ID)).decision is None

    _start(runtime_paths)
    call = await _plan(runtime_paths, tool, requester_id=HUMAN, thread_id=THREAD_ID)
    assert call.decision is ApprovalDecision.APPROVED
    assert call.human_approval_required is True
    assert (await _plan(runtime_paths, tool, requester_id="@other:localhost", thread_id=THREAD_ID)).decision is None
    assert (await _plan(runtime_paths, tool, requester_id=HUMAN, thread_id="$other")).decision is None


@pytest.mark.asyncio
async def test_auto_approve_window_that_ends_during_the_policy_check_approves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window turned off while an approval script runs leaves that call asking."""
    runtime_paths = test_runtime_paths(tmp_path)
    _start(runtime_paths)

    async def evaluate(*_args: object) -> tuple[bool, float]:
        end_auto_approve(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN)
        return True, 60.0

    monkeypatch.setattr("mindroom.approval.response.evaluate_tool_approval", evaluate)
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="shell",
        tool_args={},
        approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
    )

    assert (await _plan(runtime_paths, tool, requester_id=HUMAN, thread_id=THREAD_ID)).decision is None


@pytest.mark.asyncio
async def test_auto_approve_window_never_approves_for_a_requester_who_cannot_approve(tmp_path: Path) -> None:
    """A call whose requester is no person stays denied even when a window names that ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    _start(runtime_paths, requester_id="not-a-matrix-id")
    tool = ToolExecution(tool_call_id="call-1", tool_name="shell", tool_args={}, requires_confirmation=True)

    call = await _plan(runtime_paths, tool, requester_id="not-a-matrix-id", thread_id=THREAD_ID)

    assert call.decision is ApprovalDecision.DENIED


def test_auto_approve_command_parsing_and_help() -> None:
    """`!auto_approve` parses with and without arguments, and help explains it."""
    for message, args_text in (
        ("!auto_approve", ""),
        ("!Auto-Approve 30m", "30m"),
        ("!auto_approve 45", "45"),
        ("!auto_approve  off ", "off"),
    ):
        command = command_parser.parse(message)
        assert command is not None
        assert command.type is CommandType.AUTO_APPROVE
        assert command.args == {"args_text": args_text}
    assert "**Auto-approve Command**" in get_command_help("auto_approve")
    assert "!auto_approve" in get_command_help()


@dataclass(frozen=True)
class _Event:
    sender: str
    body: str
    event_id: str = "$event"

    @property
    def source(self) -> dict[str, object]:
        return {"content": {"body": self.body}}


async def _send(
    tmp_path: Path,
    body: str,
    *,
    requester_user_id: str = HUMAN,
    acts_for_requester: bool = False,
    config: Config | None = None,
    room_mode: bool = False,
    client: AsyncMock | None = None,
) -> tuple[str, RuntimePaths]:
    runtime_paths = test_runtime_paths(tmp_path)
    context = make_test_command_handler_context(
        client=client or AsyncMock(),
        config=config or Config(),
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, THREAD_ID, "$event", room_mode=room_mode),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
    )
    command = command_parser.parse(body)
    assert command is not None
    await handle_command(
        context=context,
        room=SimpleNamespace(room_id=ROOM_ID),
        event=_Event(requester_user_id, body),
        command=command,
        requester_user_id=requester_user_id,
        acts_for_requester=acts_for_requester,
    )
    return context.send_response.await_args.args[0], runtime_paths


@pytest.mark.asyncio
async def test_auto_approve_command_starts_shows_and_ends_the_thread_window(tmp_path: Path) -> None:
    """The command starts a window for its sender's thread, reports it, and ends it."""
    reply, runtime_paths = await _send(tmp_path, "!auto_approve 2h")
    expires_at = auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN)
    assert expires_at is not None
    assert timedelta(minutes=119) < expires_at - datetime.now(UTC) <= timedelta(hours=2)
    assert reply.startswith(f"⚠️ Auto-approve is on in this thread until {expires_at:%Y-%m-%d %H:%M} UTC")

    reply, _ = await _send(tmp_path, "!auto_approve")
    assert reply.startswith("⚠️ Auto-approve is on in this thread until")

    reply, _ = await _send(tmp_path, "!auto_approve off")
    assert reply == "✅ Auto-approve is off. Tool calls in this thread ask for approval again."
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None

    reply, _ = await _send(tmp_path, "!auto_approve")
    assert reply.startswith("Auto-approve is off in this thread.")
    reply, _ = await _send(tmp_path, "!auto_approve off")
    assert reply == "Auto-approve is not on in this thread."


@pytest.mark.asyncio
async def test_auto_approve_command_publishes_the_rooms_open_windows_for_clients(tmp_path: Path) -> None:
    """Starting and ending a window rewrite the router's room state from the stored windows."""
    client = AsyncMock(user_id="@mindroom_router:localhost")
    client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id=ROOM_ID)

    await _send(tmp_path, "!auto_approve 30", client=client)

    runtime_paths = test_runtime_paths(tmp_path)
    expires_at = auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN)
    assert expires_at is not None
    client.room_put_state.assert_awaited_once_with(
        room_id=ROOM_ID,
        event_type="io.mindroom.auto_approve",
        content={"windows": [{"thread_id": THREAD_ID, "requester_id": HUMAN, "expires_at": expires_at.isoformat()}]},
        state_key="@mindroom_router:localhost",
    )
    client.room_put_state.reset_mock()
    await _send(tmp_path, "!auto_approve off", client=client)
    assert client.room_put_state.await_args.kwargs["content"] == {"windows": []}


@pytest.mark.asyncio
async def test_auto_approve_command_replies_when_publishing_its_state_fails(tmp_path: Path) -> None:
    """A refused or failed state write leaves the window on and the confirmation sent."""
    client = AsyncMock(user_id="@mindroom_router:localhost")
    client.room_put_state.side_effect = nio.exceptions.RemoteProtocolError("connection reset")

    reply, runtime_paths = await _send(tmp_path, "!auto_approve 30", client=client)

    assert reply.startswith("⚠️ Auto-approve is on in this thread until")
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is not None


@pytest.mark.asyncio
async def test_auto_approve_command_names_the_whole_room_in_a_room_without_threads_and_uses_local_time(
    tmp_path: Path,
) -> None:
    """A room-mode window covers the room, and its end shows in the configured timezone."""
    reply, runtime_paths = await _send(
        tmp_path,
        "!auto_approve 30",
        config=Config(timezone="Europe/Amsterdam"),
        room_mode=True,
    )

    expires_at = auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=None, requester_id=HUMAN)
    assert expires_at is not None
    local = expires_at.astimezone(ZoneInfo("Europe/Amsterdam"))
    assert reply.startswith(f"⚠️ Auto-approve is on in this room until {local:%Y-%m-%d %H:%M %Z}: ")
    reply, _ = await _send(tmp_path, "!auto_approve off", room_mode=True)
    assert reply == "✅ Auto-approve is off. Tool calls in this room ask for approval again."


@pytest.mark.asyncio
@pytest.mark.parametrize(("duration", "expected"), [("45", timedelta(minutes=45)), ("1440", timedelta(hours=24))])
async def test_auto_approve_command_reads_a_bare_number_as_minutes(
    tmp_path: Path,
    duration: str,
    expected: timedelta,
) -> None:
    """`!auto_approve 45` starts a 45-minute window."""
    _reply, runtime_paths = await _send(tmp_path, f"!auto_approve {duration}")

    expires_at = auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN)
    assert expires_at is not None
    assert expected - timedelta(minutes=1) < expires_at - datetime.now(UTC) <= expected


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["0", "0m", "1441", "25h", "99999999999999999999h", "forever", "30s"])
async def test_auto_approve_command_rejects_durations_outside_one_minute_to_a_day(
    tmp_path: Path,
    duration: str,
) -> None:
    """Durations must name minutes or hours between one minute and 24 hours."""
    reply, runtime_paths = await _send(tmp_path, f"!auto_approve {duration}")

    assert reply.startswith("❌ Choose a duration from 1 minute to 24 hours")
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None


@pytest.mark.asyncio
async def test_auto_approve_command_needs_the_person_in_person(tmp_path: Path) -> None:
    """An agent cannot start a window for its human, and non-person requesters cannot start one."""
    reply, runtime_paths = await _send(tmp_path, "!auto_approve 30m", acts_for_requester=True)
    assert reply == "❌ Agents cannot run this command for you. Send it yourself."
    assert auto_approve_expires_at(runtime_paths, room_id=ROOM_ID, thread_id=THREAD_ID, requester_id=HUMAN) is None

    reply, _ = await _send(tmp_path, "!auto_approve 30m", requester_user_id="not-a-matrix-id")
    assert reply == "❌ Only a person can turn on auto-approve."


@pytest.mark.asyncio
async def test_auto_approve_command_approves_cards_already_waiting_in_the_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning the window on approves the sender's pending cards in that thread through the normal action path."""
    manager = MagicMock(spec=ApprovalManager)
    manager.pending_card_event_ids = AsyncMock(return_value=("$card-1", "$card-2"))
    approve = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(auto_approve_commands, "get_approval_store", lambda: manager)
    monkeypatch.setattr(auto_approve_commands, "handle_tool_approval_action", approve)

    reply, _ = await _send(tmp_path, "!auto_approve 30m")

    assert "Approved 1 waiting tool call." in reply
    manager.pending_card_event_ids.assert_awaited_once_with(
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        approver_user_id=HUMAN,
    )
    assert [call.kwargs["approval_event_id"] for call in approve.await_args_list] == ["$card-1", "$card-2"]
    assert {(call.kwargs["sender_id"], call.kwargs["status"]) for call in approve.await_args_list} == {
        (HUMAN, "approved"),
    }


def _stored(
    card_event_id: str | None,
    *,
    created_at_ns: int,
    thread_id: str = THREAD_ID,
    approver: str = HUMAN,
    resolution: dict[str, object] | None = None,
    target_kind: str = "continuation",
    approvable: bool = True,
    expires_in: timedelta = timedelta(hours=1),
) -> StoredApprovalCard:
    content = {
        "thread_id": thread_id,
        "approver_user_id": approver,
        "approvable": approvable,
        "expires_at": (datetime.now(UTC) + expires_in).isoformat(),
    }
    return StoredApprovalCard(
        card={"content": content},
        resolution=resolution,
        delivery_id=f"delivery-{created_at_ns}",
        card_event_id=card_event_id,
        created_at_ns=created_at_ns,
        continuation_id="continuation",
        continuation_generation=1,
        tool_call_id=f"call-{created_at_ns}",
        continuation_entity_name="code",
        target_kind=target_kind,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_pending_card_listing_keeps_only_the_approvers_open_cards_in_one_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only unexpired, unanswered, approvable, published cards of the approver in the thread are listed, across pages."""
    monkeypatch.setattr("mindroom.approval.manager._STARTUP_RECOVERY_SCAN_PAGE", 3)
    pages = [
        (
            _stored("$reply-card", created_at_ns=1),
            _stored("$other-thread", created_at_ns=2, thread_id="$other"),
            _stored("$other-approver", created_at_ns=3, approver="@other:localhost"),
        ),
        (
            _stored("$answered", created_at_ns=4, resolution={"status": "approved"}),
            _stored(None, created_at_ns=5),
            _stored("$scheduled", created_at_ns=6, target_kind="scheduled_call"),
        ),
        (
            _stored("$too-large", created_at_ns=7, approvable=False),
            UnreadableApprovalCard(delivery_id="unreadable", created_at_ns=8, continuation_id="continuation"),
            _stored("$job-card", created_at_ns=9, target_kind="background_script"),
        ),
        (_stored("$past-deadline", created_at_ns=10, expires_in=timedelta(seconds=-1)),),
    ]
    cards = MagicMock()
    cards.pending_approval_cards = AsyncMock(side_effect=pages)
    manager = ApprovalManager(test_runtime_paths(tmp_path), send_delivery=AsyncMock(), cards=cards)

    try:
        event_ids = await manager.pending_card_event_ids(room_id=ROOM_ID, thread_id=THREAD_ID, approver_user_id=HUMAN)
    finally:
        await manager.shutdown()

    assert event_ids == ("$reply-card", "$job-card")
    assert [call.kwargs["after"] for call in cards.pending_approval_cards.await_args_list] == [
        None,
        (3, "delivery-3"),
        (6, "delivery-6"),
        (9, "delivery-9"),
    ]
