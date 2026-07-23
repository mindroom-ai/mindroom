"""Reaction handling, interactive selections, and tool-approval flows on AgentBot."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.approval_inbound import handle_tool_approval_action
from mindroom.approval_manager import (
    get_approval_store,
    initialize_approval_store,
)
from mindroom.bot import AgentBot
from mindroom.hooks import (
    EVENT_REACTION_RECEIVED,
    HookRegistry,
    ReactionReceivedContext,
    hook,
)
from mindroom.tool_approval import ApprovalActionResult, MatrixApprovalAction, _shutdown_approval_store
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_plugin,
    _install_runtime_cache_support,
    _start_live_approval,
    make_mock_agent_user,
)
from tests.conftest import (
    make_matrix_client_mock,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.users import AgentMatrixUser


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


def _detached_approval_card() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "event_id": "$approval",
        "room_id": "!test:localhost",
        "sender": "@mindroom_router:localhost",
        "type": "io.mindroom.tool_approval",
        "origin_server_ts": int(now.timestamp() * 1000),
        "content": {
            "approval_id": "approval-1",
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "status": "pending",
            "requester_id": "@user:localhost",
            "approver_user_id": "@user:localhost",
            "requested_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        },
    }


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_reaction_hooks_run_after_built_in_handlers_decline(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should run only after built-in handlers decline the event."""
        seen: list[tuple[str, str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.reaction_key, ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        _install_runtime_cache_support(bot)
        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Reply in thread",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
                        },
                        "event_id": "$question",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {"body": "Thread root", "msgtype": "m.text"},
                        "event_id": "$thread-root",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$question",
                    "key": "👍",
                },
            },
        }

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        assert seen == [("👍", "$question", "$thread-root")]

    @pytest.mark.asyncio
    async def test_reaction_hooks_do_not_run_when_interactive_handler_claims_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should not run when a built-in handler already consumes the reaction."""
        seen: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append(ctx.reaction_key)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")

        with (
            patch(
                "mindroom.bot.interactive.handle_reaction",
                new=AsyncMock(
                    return_value=interactive.InteractiveSelection(
                        question_event_id="$question",
                        question_text="Choose one",
                        selection_key="1",
                        selected_label="Selected",
                        selected_value="Selected",
                        thread_id=None,
                    ),
                ),
            ),
            patch.object(bot._turn_controller, "handle_interactive_selection", new=AsyncMock()),
        ):
            await bot._on_reaction(room, event)

        assert seen == []

    @pytest.mark.asyncio
    async def test_interactive_reaction_selection_reserves_prompt_order(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reaction selections should occupy receive order while their response runs."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.client.user_id = "@mindroom_test:localhost"
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="1",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-root",
        )
        selection_started = asyncio.Event()

        async def handle_selection(*_args: object, **_kwargs: object) -> None:
            selection_started.set()
            assert bot._coalescing_gate.lanes.unsettled_slots()

        with (
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=selection)),
            patch.object(bot._turn_controller, "handle_interactive_selection", side_effect=handle_selection),
        ):
            await bot._on_reaction(room, event)

        await asyncio.wait_for(selection_started.wait(), timeout=0.5)
        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_checkmark_interactive_reaction_reserves_before_tool_approval_lookup(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A checkmark selection should reserve before the approval fallthrough await."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.client.user_id = "@mindroom_test:localhost"
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.key = "✅"
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Approve?",
            selection_key="✅",
            selected_label="Approved",
            selected_value="Approved",
            thread_id="$thread-root",
        )
        approval_started = asyncio.Event()
        release_approval = asyncio.Event()

        async def delayed_approval(*_args: object, **_kwargs: object) -> bool:
            approval_started.set()
            await release_approval.wait()
            return False

        with (
            patch("mindroom.bot.handle_tool_approval_action", side_effect=delayed_approval),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=selection)),
            patch.object(bot._turn_controller, "handle_interactive_selection", new=AsyncMock()),
        ):
            reaction_task = asyncio.create_task(bot._on_reaction(room, event))
            await asyncio.wait_for(approval_started.wait(), timeout=0.5)
            try:
                reaction_slots = bot._coalescing_gate.lanes.unsettled_slots()
                assert reaction_slots
                later_owner = bot._turn_controller._reserve_prompt_ingress_order(room, "@user:localhost")
                try:
                    assert reaction_slots[0].receipt_time < later_owner.slot.receipt_time
                finally:
                    await later_owner.release()
            finally:
                release_approval.set()
                await reaction_task

        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_checkmark_tool_approval_bypasses_conversation_reply_permission(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval authorization owns approval reactions; reply policy owns chat reactions."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.client.user_id = "@mindroom_test:localhost"
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.key = "✅"
        event.reacts_to = "$approval-card"

        approval_handler = AsyncMock(return_value=True)
        with (
            patch("mindroom.turn_policy.is_sender_allowed_for_agent_reply", return_value=False),
            patch("mindroom.bot.handle_tool_approval_action", approval_handler),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock()) as interactive_handler,
        ):
            await bot._on_reaction(room, event)

        approval_handler.assert_awaited_once()
        interactive_handler.assert_not_awaited()
        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_and_denial_reason_resolves_live_waiter(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Cinny custom approval responses should resolve by approval_id alone."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(runtime_paths)

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {
                        "approval_id": pending.approval_id,
                        "status": "denied",
                        "denial_reason": "Not this time.",
                    },
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert decision.reason == "Not this time."
            assert editor.await_args.args[1] == "$approval"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_and_non_card_reply_resolves_live_waiter(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Custom approval responses should fall back to approval_id when reply metadata is not the card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(runtime_paths)

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {
                        "approval_id": pending.approval_id,
                        "status": "denied",
                        "denial_reason": "Wrong arguments.",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$thread",
                            "m.in_reply_to": {"event_id": "$latest-thread-event"},
                        },
                    },
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert decision.reason == "Wrong arguments."
            assert editor.await_args.args[1] == "$approval"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_with_approval_id_uses_live_id_entrypoint(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval-id-only custom events should use the live-id manager API."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event = nio.UnknownEvent.from_dict(
            {
                "type": "io.mindroom.tool_approval_response",
                "sender": "@user:localhost",
                "event_id": "$response",
                "origin_server_ts": 1,
                "content": {"approval_id": "approval-1", "status": "approved"},
            },
        )
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=True, resolved=True, card_event_id="$approval")),
        ) as handle_matrix_approval_action:
            await bot._on_unknown_event(room, event)

        handle_matrix_approval_action.assert_awaited_once_with(
            MatrixApprovalAction(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=None,
                approval_id="approval-1",
                status="approved",
                reason=None,
            ),
        )

    @pytest.mark.asyncio
    async def test_unknown_truncated_approval_id_response_sends_notice_with_card_event_id(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval-id-only responses should still send truncated-argument denial notices."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        orchestrator = MagicMock()
        orchestrator.send_approval_notice = AsyncMock(return_value=True)
        bot.orchestrator = orchestrator
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            arguments={"content": "x" * 3_000_000},
        )

        try:
            event = SimpleNamespace(
                type="io.mindroom.tool_approval_response",
                source={
                    "sender": "@user:localhost",
                    "content": {"approval_id": pending.approval_id, "status": "approved"},
                },
            )
            await bot._on_unknown_event(room, event)
            decision = await task

            assert decision.status == "denied"
            assert "too large to show in full" in (decision.reason or "")
            replacement = editor.await_args.args[2]
            assert replacement["status"] == "denied"
            assert "too large to show in full" in replacement["resolution_reason"]
            orchestrator.send_approval_notice.assert_awaited_once_with(
                room_id="!test:localhost",
                approval_event_id=pending.card_event_id,
                thread_id=pending.thread_id,
                reason=replacement["resolution_reason"],
            )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_non_router_bot_truncated_approval_race_sends_notice_via_orchestrator(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A non-router bot that wins the approval callback race should still trigger notice delivery."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        agent_bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        agent_bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
        router_bot = MagicMock()
        router_bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator = MagicMock()
        orchestrator.send_approval_notice = AsyncMock(return_value=True)
        agent_bot.orchestrator = orchestrator
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        _store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            arguments={"content": "x" * 3_000_000},
        )

        try:
            handled = await handle_tool_approval_action(
                room=room,
                sender_id="@user:localhost",
                config=agent_bot.config,
                runtime_paths=agent_bot.runtime_paths,
                orchestrator=agent_bot.orchestrator,
                logger=agent_bot.logger,
                approval_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task

            assert handled is True
            assert decision.status == "denied"
            replacement = editor.await_args.args[2]
            assert "too large to show in full" in replacement["resolution_reason"]
            orchestrator.send_approval_notice.assert_awaited_once_with(
                room_id="!test:localhost",
                approval_event_id=pending.card_event_id,
                thread_id=pending.thread_id,
                reason=replacement["resolution_reason"],
            )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_text_from_non_approver_falls_through_to_normal_handler(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Non-approver approval replies should fall through to normal text handling."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            approver_user_id="@approver:localhost",
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$reply"
        event.sender = "@other:localhost"
        event.body = "I should not resolve this."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$reply",
            "sender": "@other:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once()
            assert bot._turn_controller.handle_text_event.await_args.args == (room, event)
            assert isinstance(bot._turn_controller.handle_text_event.await_args.kwargs["receipt_time"], float)
            editor.assert_not_awaited()
            assert task.done() is False

            await store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@approver:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task
            assert decision.status == "approved"
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_plain_rich_reply_falls_through_after_approval_card_point_lookup(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ordinary rich replies should fall through when their target is not an approval card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event_cache = MagicMock()
        event_cache.get_event = AsyncMock(return_value=None)
        store = initialize_approval_store(
            runtime_paths,
            event_cache=event_cache,
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$ordinary-rich-reply"
        event.sender = "@user:localhost"
        event.body = "!help"
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$ordinary-rich-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": "$ordinary-message"}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once()
            assert bot._turn_controller.handle_text_event.await_args.args == (room, event)
            assert isinstance(bot._turn_controller.handle_text_event.await_args.kwargs["receipt_time"], float)
            event_cache.get_event.assert_awaited_once_with("!test:localhost", "$ordinary-message")
            assert store is get_approval_store()
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_to_detached_pending_approval_is_consumed_and_expires_card(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Detached approval replies should expire their card instead of entering conversation input."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event_cache = MagicMock()
        event_cache.get_event = AsyncMock(return_value=_detached_approval_card())
        event_cache.get_latest_edit = AsyncMock(return_value=None)
        editor = AsyncMock(return_value=True)
        initialize_approval_store(
            runtime_paths,
            editor=editor,
            event_cache=event_cache,
            transport_sender=lambda: "@mindroom_router:localhost",
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$reply"
        event.sender = "@user:localhost"
        event.body = "Deny."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$approval"}}},
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_not_awaited()
            assert editor.await_args.args[:2] == ("!test:localhost", "$approval")
            replacement = editor.await_args.args[2]
            assert replacement["status"] == "expired"
            assert replacement["resolution_reason"] == "Original tool request is no longer active."
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_thread_fallback_to_detached_approval_remains_conversation_input(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thread fallback metadata must not turn ordinary text into an approval response."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event_cache = MagicMock()
        event_cache.get_event = AsyncMock(return_value=_detached_approval_card())
        editor = AsyncMock(return_value=True)
        initialize_approval_store(
            runtime_paths,
            editor=editor,
            event_cache=event_cache,
            transport_sender=lambda: "@mindroom_router:localhost",
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$thread-message"
        event.sender = "@user:localhost"
        event.body = "Please continue."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$thread-message",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root",
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": "$approval"},
                },
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once()
            event_cache.get_event.assert_not_awaited()
            editor.assert_not_awaited()
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_plain_thread_reply_with_approval_store_does_not_require_room_alias(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Ordinary replies should not run approval authorization before matching an in-memory card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.matrix_id)
        initialize_approval_store(runtime_paths)
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$ordinary-thread-reply"
        event.sender = "@user:localhost"
        event.body = "ordinary reply"
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$ordinary-thread-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": "$ordinary-message"}},
            },
        }

        try:
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once()
            assert bot._turn_controller.handle_text_event.await_args.args == (room, event)
            assert isinstance(bot._turn_controller.handle_text_event.await_args.kwargs["receipt_time"], float)
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_duplicate_live_approval_reply_is_consumed_without_falling_through(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Duplicate approver replies should be consumed while the first resolution is in flight."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()

        async def slow_editor(_room_id: str, _event_id: str, _content: dict[str, Any]) -> bool:
            edit_started.set()
            await release_edit.wait()
            return True

        store, pending, task, editor = await _start_live_approval(
            runtime_paths,
            editor=AsyncMock(side_effect=slow_editor),
        )
        first_resolution = asyncio.create_task(
            store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            ),
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$duplicate-approval-reply"
        event.sender = "@user:localhost"
        event.body = "No, deny it."
        event.server_timestamp = 1234
        event.source = {
            "event_id": "$duplicate-approval-reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234,
            "content": {
                "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
            },
        }

        try:
            await asyncio.wait_for(edit_started.wait(), timeout=1)
            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_not_awaited()
            release_edit.set()
            first_result = await first_resolution
            decision = await task

            assert first_result.resolved is True
            assert decision.status == "approved"
            assert editor.await_count == 1
        finally:
            release_edit.set()
            if not first_resolution.done():
                first_resolution.cancel()
                with suppress(asyncio.CancelledError):
                    await first_resolution
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_to_resolved_approval_card_falls_through_to_normal_text(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Follow-up text on a terminal approval card should remain a normal message."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot._turn_controller.handle_text_event = AsyncMock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        store, pending, task, _editor = await _start_live_approval(runtime_paths)

        try:
            result = await store.handle_card_response(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id=pending.card_event_id,
                status="approved",
                reason=None,
            )
            decision = await task
            assert result.resolved is True
            assert decision.status == "approved"

            event = MagicMock(spec=nio.RoomMessageText)
            event.event_id = "$follow-up-reply"
            event.sender = "@user:localhost"
            event.body = "Why did this fail?"
            event.server_timestamp = 1234
            event.source = {
                "event_id": "$follow-up-reply",
                "sender": "@user:localhost",
                "origin_server_ts": 1234,
                "content": {
                    "m.relates_to": {"m.in_reply_to": {"event_id": pending.card_event_id}},
                },
            }

            await bot._on_message(room, event)

            bot._turn_controller.handle_text_event.assert_awaited_once()
            assert bot._turn_controller.handle_text_event.await_args.args == (room, event)
            assert isinstance(bot._turn_controller.handle_text_event.await_args.kwargs["receipt_time"], float)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_checkmark_reaction_reaches_approval_manager_with_card_id_and_sender(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Checkmark reactions should dispatch approval actions to the manager."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        event = MagicMock(spec=nio.ReactionEvent)
        event.key = "✅"
        event.reacts_to = "$approval"
        event.sender = "@user:localhost"
        event.event_id = "$reaction"
        event.source = {"content": {}}
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=True, resolved=True)),
        ) as handle_matrix_approval_action:
            await bot._on_reaction(room, event)

        handle_matrix_approval_action.assert_awaited_once_with(
            MatrixApprovalAction(
                room_id="!test:localhost",
                sender_id="@user:localhost",
                card_event_id="$approval",
                approval_id=None,
                status="approved",
                reason=None,
            ),
        )

    @pytest.mark.asyncio
    async def test_reaction_hooks_inherit_thread_for_promoted_plain_reply_target(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should reuse inherited thread membership for promoted plain replies."""
        seen: list[tuple[str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._conversation_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root" if (room_id, event_id) == ("!test:localhost", "$thread-reply") else None
            ),
        )
        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
                    },
                    "event_id": "$plain-reply",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply"
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$plain-reply",
                    "key": "👍",
                },
            },
        }

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        assert seen == [("$plain-reply", "$thread-root")]

    @pytest.mark.asyncio
    async def test_reaction_hooks_label_thread_membership_reads(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should attribute thread proof refreshes."""
        seen: list[tuple[str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        bot._conversation_resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort = AsyncMock(
            return_value="$thread-root",
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply"

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        bot._conversation_resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort.assert_awaited_once_with(
            room.room_id,
            "$plain-reply",
            caller_label="reaction_hook_context",
        )
        assert seen == [("$plain-reply", "$thread-root")]

    @pytest.mark.asyncio
    async def test_reaction_hooks_inherit_thread_transitively_through_plain_reply_chain(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """reaction:received hooks should follow the transitive reply chain to the threaded ancestor."""
        seen: list[tuple[str, str | None]] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append((ctx.target_event_id, ctx.thread_id))

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot._conversation_cache.get_thread_id_for_event = AsyncMock(
            side_effect=lambda room_id, event_id: (
                "$thread-root" if (room_id, event_id) == ("!test:localhost", "$thread-reply") else None
            ),
        )

        def room_get_event_response(event_id: str, content: dict[str, object]) -> nio.RoomGetEventResponse:
            return nio.RoomGetEventResponse.from_dict(
                {
                    "content": content,
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

        async def fetch_related_event(_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            if event_id == "$plain-reply-2":
                return room_get_event_response(
                    "$plain-reply-2",
                    {
                        "body": "second bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply-1"}},
                    },
                )
            if event_id == "$plain-reply-1":
                return room_get_event_response(
                    "$plain-reply-1",
                    {
                        "body": "first bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
                    },
                )
            if event_id == "$thread-reply":
                return room_get_event_response(
                    "$thread-reply",
                    {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "event_id": "$thread-root",
                            "rel_type": "m.thread",
                        },
                    },
                )
            msg = f"unexpected event lookup: {event_id}"
            raise AssertionError(msg)

        bot.client.room_get_event = AsyncMock(side_effect=fetch_related_event)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply-2"
        event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$plain-reply-2",
                    "key": "👍",
                },
            },
        }

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=False)):
            await bot._on_reaction(room, event)

        assert seen == [("$plain-reply-2", "$thread-root")]
