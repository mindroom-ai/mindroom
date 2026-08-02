"""Reaction handling, interactive selections, and tool-approval flows on AgentBot."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
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
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_obligations import DispatchCallbackKind, DispatchSemanticConsumer
from mindroom.handled_turns import TurnRecord, with_user_stop
from mindroom.hooks import (
    EVENT_REACTION_RECEIVED,
    HookRegistry,
    ReactionReceivedContext,
    hook,
)
from mindroom.message_target import MessageTarget
from mindroom.tool_approval import ApprovalActionResult, MatrixApprovalAction, _shutdown_approval_store
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_plugin,
    _install_runtime_cache_support,
    _start_live_approval,
    make_mock_agent_user,
)
from tests.bot_helpers import (
    dispatch_reaction_durably as _dispatch_reaction,
)
from tests.conftest import (
    make_matrix_client_mock,
    replace_reaction_dispatcher_deps,
    runtime_paths_for,
    unwrap_extracted_collaborator,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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


async def _cancel_dispatch_retry(bot: AgentBot) -> None:
    retry_task = bot._dispatch_obligation_runner._retry_task
    assert retry_task is not None
    retry_task.cancel()
    with suppress(asyncio.CancelledError):
        await retry_task


def _approval_reply_event(event_id: str = "$approval-reply") -> nio.RoomMessageText:
    event = nio.Event.parse_event(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": "Deny it.",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$approval"}},
            },
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _reaction_event(key: str, event_id: str) -> nio.ReactionEvent:
    event = nio.Event.parse_event(
        {
            "type": "m.reaction",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval" if key == "✅" else "$response",
                    "key": key,
                },
            },
        },
    )
    assert isinstance(event, nio.ReactionEvent)
    return event


def _install_reaction_recorder(bot: AgentBot) -> list[str]:
    """Install a real reaction hook and return its observed event IDs."""
    seen: list[str] = []

    @hook(EVENT_REACTION_RECEIVED)
    async def record_reaction(ctx: ReactionReceivedContext) -> None:
        seen.append(ctx.event_id)

    bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
    return seen


def _install_text_dispatch_mock(
    monkeypatch: pytest.MonkeyPatch,
    bot: AgentBot,
) -> AsyncMock:
    """Replace the unwrapped text-dispatch collaborator through an auto-restored seam."""
    handle_text_event = AsyncMock(return_value=TurnDispatchOutcome.DEFERRED)
    monkeypatch.setattr(
        unwrap_extracted_collaborator(bot._turn_controller),
        "handle_text_event",
        handle_text_event,
    )
    return handle_text_event


async def _dispatch_message(bot: AgentBot, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
    """Exercise one message through its durable production entrypoint."""
    source = dict(event.source)
    source.setdefault("event_id", event.event_id)
    source.setdefault("sender", event.sender)
    source.setdefault("origin_server_ts", 1)
    source.setdefault("type", "m.room.message")
    event.source = source
    event.decrypted = False
    await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)


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

        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=None)):
            await _dispatch_reaction(bot, room, event)

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
        replace_reaction_dispatcher_deps(bot, handle_interactive_selection=AsyncMock())

        with patch(
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
        ):
            await _dispatch_reaction(bot, room, event)

        assert seen == []

    @pytest.mark.asyncio
    async def test_recovered_interactive_reaction_keeps_its_durable_consumer(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An interactive consumer claim must not replay through generic reaction hooks."""
        seen: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append(ctx.event_id)

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock(room_id="!test:localhost")
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.key = "✅"

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._dispatch_obligation_runner),
                "semantic_consumer",
                new=MagicMock(return_value=DispatchSemanticConsumer.INTERACTIVE_REACTION),
            ),
            patch(
                "mindroom.bot.interactive.handle_reaction",
                new=AsyncMock(return_value=None),
            ) as interactive_handler,
        ):
            await _dispatch_reaction(bot, room, event)

        assert seen == []
        interactive_handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recovered_config_reaction_keeps_its_visible_consumer(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A config consumer claim must not replay through generic reaction hooks."""
        seen: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append(ctx.event_id)

        config = self._config_for_storage(tmp_path)
        router_user = replace(
            mock_agent_user,
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="RouterAgent",
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock(room_id="!test:localhost")
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.key = "✅"

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._dispatch_obligation_runner),
                "semantic_consumer",
                new=MagicMock(return_value=DispatchSemanticConsumer.CONFIG_CONFIRMATION),
            ),
            patch(
                "mindroom.bot.config_confirmation.resolve_reaction_pending_change",
                new=AsyncMock(return_value=None),
            ) as resolve_pending,
        ):
            await _dispatch_reaction(bot, room, event)

        assert seen == []
        resolve_pending.assert_awaited_once_with(bot.client, room.room_id, event, enabled=True)

    @pytest.mark.asyncio
    async def test_interactive_reaction_failure_restores_question_for_durable_retry(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A failed selection handoff must leave its question answerable on exact retry."""
        interactive._cleanup()
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = MagicMock()
        room.room_id = "!test:localhost"
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        interactive._active_questions["$question"] = interactive._InteractiveQuestion(
            room_id=room.room_id,
            thread_id=None,
            options={"👍": "approve"},
            creator_agent=bot.agent_name,
        )

        try:
            with (
                patch.object(
                    bot._turn_controller,
                    "_execute_interactive_selection",
                    new=AsyncMock(side_effect=OSError("pending write failed")),
                ),
                pytest.raises(OSError, match="pending write failed"),
            ):
                await _dispatch_reaction(bot, room, event)

            assert "$question" in interactive._active_questions
        finally:
            interactive._cleanup()

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

        replace_reaction_dispatcher_deps(bot, handle_interactive_selection=handle_selection)
        with patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=selection)):
            await _dispatch_reaction(bot, room, event)

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

        replace_reaction_dispatcher_deps(bot, handle_interactive_selection=AsyncMock())
        with (
            patch("mindroom.reaction_dispatch.handle_tool_approval_action", side_effect=delayed_approval),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=selection)),
        ):
            reaction_task = asyncio.create_task(_dispatch_reaction(bot, room, event))
            await asyncio.wait_for(approval_started.wait(), timeout=0.5)
            try:
                reaction_slots = bot._coalescing_gate.lanes.unsettled_slots()
                assert reaction_slots
                later_owner = bot._turn_controller.reserve_prompt_ingress_order(room, "@user:localhost")
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
            patch("mindroom.reaction_dispatch.handle_tool_approval_action", approval_handler),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock()) as interactive_handler,
        ):
            await _dispatch_reaction(bot, room, event)

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
            before_consume=None,
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-approver approval replies should fall through to normal text handling."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_awaited_once()
            assert handle_text_event.await_args.args == (room, event)
            assert isinstance(handle_text_event.await_args.kwargs["receipt_time"], float)
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary rich replies should fall through when their target is not an approval card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_awaited_once()
            assert handle_text_event.await_args.args == (room, event)
            assert isinstance(handle_text_event.await_args.kwargs["receipt_time"], float)
            event_cache.get_event.assert_awaited_once_with("!test:localhost", "$ordinary-message")
            assert store is get_approval_store()
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_reply_to_detached_pending_approval_is_consumed_and_expires_card(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detached approval replies should expire their card instead of entering conversation input."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_not_awaited()
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Thread fallback metadata must not turn ordinary text into an approval response."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_awaited_once()
            event_cache.get_event.assert_not_awaited()
            editor.assert_not_awaited()
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_plain_thread_reply_with_approval_store_does_not_require_room_alias(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary replies should not run approval authorization before matching an in-memory card."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_awaited_once()
            assert handle_text_event.await_args.args == (room, event)
            assert isinstance(handle_text_event.await_args.kwargs["receipt_time"], float)
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_duplicate_live_approval_reply_is_consumed_without_falling_through(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Duplicate approver replies should be consumed while the first resolution is in flight."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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
            await _dispatch_message(bot, room, event)

            handle_text_event.assert_not_awaited()
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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Follow-up text on a terminal approval card should remain a normal message."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
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

            await _dispatch_message(bot, room, event)

            handle_text_event.assert_awaited_once()
            assert handle_text_event.await_args.args == (room, event)
            assert isinstance(handle_text_event.await_args.kwargs["receipt_time"], float)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_interrupted_approval_reply_replay_cannot_become_ai_input(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reply claimed by approval handling must retain that owner after a crash."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _approval_reply_event()

        async def consume_then_crash(**kwargs: object) -> bool:
            before_consume = cast("Callable[[], Awaitable[None]]", kwargs["before_consume"])
            await before_consume()
            message = "crash after approval reply side effect"
            raise RuntimeError(message)

        with (
            patch("mindroom.bot.maybe_handle_tool_approval_reply", side_effect=consume_then_crash),
            pytest.raises(RuntimeError, match="crash after approval reply side effect"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
        await _cancel_dispatch_retry(bot)
        assert bot._dispatch_obligation_store.pending()[0].semantic_consumer is DispatchSemanticConsumer.APPROVAL_REPLY

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, restarted)
        with patch(
            "mindroom.bot.maybe_handle_tool_approval_reply",
            new=AsyncMock(return_value=False),
        ) as approval_reply:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=True)

        approval_reply.assert_awaited_once()
        assert approval_reply.await_args.kwargs["before_consume"] is None
        assert approval_reply.await_args.kwargs["authorization_prevalidated"] is True
        handle_text_event.assert_not_awaited()
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_interrupted_approval_reaction_replay_cannot_become_hook_input(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A reaction claimed by approval handling must never fall through to hooks."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("✅", "$approval-reaction")
        failure = RuntimeError("crash after approval reaction side effect")

        async def fail_after_claim(
            _action: MatrixApprovalAction,
            *,
            before_consume: Callable[[], Awaitable[None]] | None = None,
        ) -> ApprovalActionResult:
            assert before_consume is not None
            await before_consume()
            raise failure

        with (
            patch(
                "mindroom.approval_inbound.handle_matrix_approval_action",
                new=AsyncMock(side_effect=fail_after_claim),
            ),
            pytest.raises(RuntimeError, match="crash after approval reaction side effect"),
        ):
            await _dispatch_reaction(bot, room, event)
        await _cancel_dispatch_retry(bot)
        assert (
            bot._dispatch_obligation_store.pending()[0].semantic_consumer
            is DispatchSemanticConsumer.TOOL_APPROVAL_REACTION
        )

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=False, resolved=False)),
        ):
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        assert unexpected_hooks == []
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_interrupted_stop_reaction_replay_cannot_become_hook_input(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A reaction claimed by stop handling must never fall through to hooks."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        pending_turn = TurnRecord.create(
            ["$source"],
            response_event_id="$response",
            completed=False,
            response_owner=bot.agent_name,
            requester_id="@user:localhost",
            conversation_target=target,
        )
        bot._turn_store.record_pending_turn(pending_turn)
        failure = RuntimeError("crash after stop reaction side effect")
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch.object(
                bot._user_stop_reconciler,
                "finalize",
                new=AsyncMock(side_effect=failure),
            ),
            pytest.raises(RuntimeError, match="crash after stop reaction side effect"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)
        pending = bot._dispatch_obligation_store.pending()
        assert pending[0].semantic_consumer is DispatchSemanticConsumer.STOP_REACTION
        stop_receipt_order = bot._dispatch_obligation_store.receipt_order(pending[0].key)

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)

        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        assert unexpected_hooks == []
        assert restarted._turn_store.is_durably_handled("$source") is True
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_interrupted_stop_claim_suppresses_preceding_edit_after_restart(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A STOP claimed before cancellation must durably cover earlier edits."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        bot._turn_store.record_turn(
            TurnRecord.create(
                ["$source"],
                response_event_id="$response",
                response_owner=bot.agent_name,
                requester_id="@user:localhost",
                conversation_target=target,
            ),
        )
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch.object(bot.stop_manager, "can_handle_stop_reaction", new=MagicMock(return_value=True)),
            patch.object(
                bot._user_stop_reconciler,
                "finalize",
                new=AsyncMock(side_effect=RuntimeError("crash after stop claim")),
            ),
            pytest.raises(RuntimeError, match="crash after stop claim"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)
        pending = bot._dispatch_obligation_store.pending()
        stop_receipt_order = bot._dispatch_obligation_store.receipt_order(pending[0].key)

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert unexpected_hooks == []
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_stop_replay_preserves_visible_partial_finalized_by_live_cancellation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A live cancellation's partial terminal body must not be replaced on replay."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        pending_turn = TurnRecord.create(
            ["$source"],
            response_event_id="$response",
            completed=False,
            response_owner=bot.agent_name,
            requester_id="@user:localhost",
            conversation_target=target,
        )
        bot._turn_store.record_pending_turn(pending_turn)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch.object(
                bot._user_stop_reconciler,
                "finalize",
                new=AsyncMock(side_effect=RuntimeError("crash after stop claim")),
            ),
            pytest.raises(RuntimeError, match="crash after stop claim"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)
        pending = bot._dispatch_obligation_store.pending()
        stop_receipt_order = bot._dispatch_obligation_store.receipt_order(pending[0].key)
        bot._turn_store.record_turn_durably(
            with_user_stop(
                pending_turn,
                "$response",
                stop_receipt_order,
                delivery_settled=True,
            ),
        )

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        finalize_stopped_response.assert_not_awaited()
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order is not None
        assert stopped_record.user_stop_settled_receipt_order == stopped_record.user_stop_receipt_order
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_failed_stop_delivery_suppresses_model_recovery_and_retries_after_restart(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Durable STOP truth must precede its retryable visible terminal edit."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        bot._turn_store.record_pending_turn(
            TurnRecord.create(
                ["$source"],
                response_event_id="$response",
                completed=False,
                response_owner=bot.agent_name,
                requester_id="@user:localhost",
                conversation_target=target,
            ),
        )
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(RuntimeError, match="Failed to finalize user-stopped response"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)

        pending = bot._dispatch_obligation_store.pending()
        stop_receipt_order = bot._dispatch_obligation_store.receipt_order(pending[0].key)
        stopped_record = bot._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.completed is True
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert stopped_record.user_stop_settled_receipt_order is None
        assert bot._turn_store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$source",),
            terminal_source_event_ids=("$source",),
        )
        assert len(pending) == 1
        assert pending[0].semantic_consumer is DispatchSemanticConsumer.STOP_REACTION

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        assert unexpected_hooks == []
        finalized_record = restarted._turn_store.get_turn_record("$source")
        assert finalized_record is not None
        assert finalized_record.user_stop_settled_receipt_order == stop_receipt_order
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_older_stop_callback_preserves_later_edit_and_its_stop_button(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A delayed older STOP cannot cancel or clean up a later edit's live controls."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        bot._turn_store.record_pending_turn(
            TurnRecord.create(
                ["$source"],
                response_event_id="$response",
                completed=False,
                response_owner=bot.agent_name,
                requester_id="@user:localhost",
                conversation_target=target,
            ),
        )
        assert not bot._turn_store.prepare_edit_response_source(
            target=target,
            source_event_ids=("$source",),
            response_event_id="$response",
            edit_receipt_order=3,
        )
        later_edit_task = asyncio.create_task(asyncio.Event().wait())
        bot.stop_manager.set_current(
            "$response",
            target,
            later_edit_task,
            reaction_event_id="$later-edit-stop-button",
        )
        response_runner = unwrap_extracted_collaborator(bot._response_runner)
        lifecycle_lock = response_runner._lifecycle_coordinator._response_lifecycle_lock(target)
        await lifecycle_lock.acquire()
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        original_lookup = turn_store.get_turn_record
        cancellation_check_started = threading.Event()

        def tracked_lookup(source_event_id: str) -> TurnRecord | None:
            cancellation_check_started.set()
            return original_lookup(source_event_id)

        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stale-stop-reaction")
        with (
            patch.object(
                unwrap_extracted_collaborator(bot._dispatch_obligation_runner),
                "receipt_order",
                new=AsyncMock(return_value=2),
            ),
            patch.object(turn_store, "get_turn_record", side_effect=tracked_lookup) as source_lookup,
        ):
            replay_task = asyncio.create_task(
                bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION),
            )
            assert await asyncio.to_thread(cancellation_check_started.wait, 2)
            assert later_edit_task.done() is False
            lifecycle_lock.release()
            await replay_task

        source_lookup.assert_called_with("$source")
        tracked = bot.stop_manager.tracked_messages["$response"]
        assert tracked.task is later_edit_task
        assert tracked.reaction_event_id == "$later-edit-stop-button"
        bot.client.room_redact.assert_not_awaited()
        later_edit_task.cancel()
        await asyncio.gather(later_edit_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_stop_guard_uses_post_reconciliation_source_identity(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """STOP cancellation must follow the turn after a redacted alias is reassigned."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$second")
        bot._turn_store.record_pending_turn(
            TurnRecord.create(
                ["$first", "$second"],
                redacted_source_event_ids=["$first"],
                response_event_id="$response-a",
                completed=False,
                response_owner=bot.agent_name,
                requester_id="@user:localhost",
                conversation_target=target,
            ),
        )
        live_task = asyncio.create_task(asyncio.Event().wait())
        bot.stop_manager.set_current("$response-a", target, live_task)
        reconciler = bot._user_stop_reconciler
        turn_store = unwrap_extracted_collaborator(bot._turn_store)
        original_record_user_stop = reconciler._record
        alias_claimed = False

        async def record_after_alias_claim(
            response_event_id: str,
            stop_receipt_order: int,
            *,
            delivery_settled: bool = False,
        ) -> TurnRecord:
            nonlocal alias_claimed
            if not alias_claimed:
                alias_claimed = True
                turn_store.record_turn(
                    TurnRecord.create(
                        ["$first", "$other"],
                        response_event_id="$response-b",
                        latest_edit_receipt_order=3,
                    ),
                )
            return await original_record_user_stop(
                response_event_id,
                stop_receipt_order,
                delivery_settled=delivery_settled,
            )

        on_current_stop_finalized = AsyncMock()
        try:
            with (
                patch.object(reconciler, "_record", side_effect=record_after_alias_claim),
                patch(
                    "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
                    new=AsyncMock(return_value=True),
                ),
            ):
                assert await reconciler.finalize(
                    "$response-a",
                    2,
                    on_current_stop_finalized,
                )
            await asyncio.sleep(0)

            stopped = turn_store.turn_record_for_response_event_id("$response-a")
            assert stopped is not None
            assert stopped.source_event_ids == ("$second",)
            assert turn_store.get_turn_record("$first").response_event_id == "$response-b"
            assert live_task.cancelled()
            on_current_stop_finalized.assert_awaited_once()
        finally:
            live_task.cancel()
            await asyncio.gather(live_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_interrupted_config_reaction_replays_only_its_durable_consumer(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A config reaction claimed before a crash must not reach another consumer."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        router_user = replace(
            mock_agent_user,
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="RouterAgent",
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("✅", "$config-reaction")
        pending_change = MagicMock(decision_event_id="$decision")
        failure = RuntimeError("crash after config reaction claim")

        with (
            patch(
                "mindroom.bot.config_confirmation.resolve_reaction_pending_change",
                new=AsyncMock(return_value=pending_change),
            ),
            patch(
                "mindroom.bot.config_confirmation.resume_committed_confirmation",
                new=AsyncMock(side_effect=failure),
            ),
            pytest.raises(RuntimeError, match="crash after config reaction claim"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)
        assert (
            bot._dispatch_obligation_store.pending()[0].semantic_consumer
            is DispatchSemanticConsumer.CONFIG_CONFIRMATION
        )

        restarted = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.bot.config_confirmation.resolve_reaction_pending_change",
            new=AsyncMock(return_value=None),
        ) as resolve_pending:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        resolve_pending.assert_awaited_once()
        assert unexpected_hooks == []
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_interrupted_interactive_reaction_replays_only_its_durable_consumer(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An interactive claim must survive a crash before turn handoff completes."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("👍", "$interactive-reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose",
            selection_key="👍",
            selected_label="Chosen",
            selected_value="chosen",
            thread_id=None,
        )
        failure = RuntimeError("crash after interactive reaction claim")
        replace_reaction_dispatcher_deps(
            bot,
            handle_interactive_selection=AsyncMock(side_effect=failure),
        )

        with (
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=selection)),
            pytest.raises(RuntimeError, match="crash after interactive reaction claim"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        await _cancel_dispatch_retry(bot)
        assert (
            bot._dispatch_obligation_store.pending()[0].semantic_consumer
            is DispatchSemanticConsumer.INTERACTIVE_REACTION
        )

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.bot.interactive.handle_reaction",
            new=AsyncMock(return_value=None),
        ) as interactive_handler:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        interactive_handler.assert_awaited_once()
        assert unexpected_hooks == []
        assert restarted._dispatch_obligation_store.pending() == ()

    @pytest.mark.asyncio
    async def test_interrupted_hook_reaction_replays_hooks_without_reentering_builtins(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A claimed generic hook keeps at-least-once delivery without reclassification."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("👍", "$hook-reaction")
        emissions: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def emit_then_crash(ctx: ReactionReceivedContext) -> None:
            emissions.append(ctx.event_id)
            message = "cancel after reaction hook side effect"
            raise asyncio.CancelledError(message)

        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [emit_then_crash])])
        with (
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=None)),
            pytest.raises(asyncio.CancelledError, match="cancel after reaction hook side effect"),
        ):
            await bot._dispatch_obligation_runner.dispatch(room, event, DispatchCallbackKind.REACTION)
        assert bot._dispatch_obligation_store.pending()[0].semantic_consumer is DispatchSemanticConsumer.REACTION_HOOKS

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()

        @hook(EVENT_REACTION_RECEIVED)
        async def emit_replay(ctx: ReactionReceivedContext) -> None:
            emissions.append(ctx.event_id)

        restarted.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [emit_replay])])
        with patch(
            "mindroom.bot.interactive.handle_reaction",
            new=AsyncMock(return_value=None),
        ) as interactive_handler:
            await restarted._dispatch_obligation_runner.recover_pending(turn_backed=False)

        assert emissions == [event.event_id, event.event_id]
        interactive_handler.assert_not_awaited()
        assert restarted._dispatch_obligation_store.pending() == ()

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

        async def consume(
            _action: MatrixApprovalAction,
            *,
            before_consume: Callable[[], Awaitable[None]] | None = None,
        ) -> ApprovalActionResult:
            assert before_consume is not None
            await before_consume()
            return ApprovalActionResult(consumed=True, resolved=True)

        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(side_effect=consume),
        ) as handle_matrix_approval_action:
            await _dispatch_reaction(bot, room, event)

        action = handle_matrix_approval_action.await_args.args[0]
        assert action == MatrixApprovalAction(
            room_id="!test:localhost",
            sender_id="@user:localhost",
            card_event_id="$approval",
            approval_id=None,
            status="approved",
            reason=None,
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
        get_thread_id_for_event = AsyncMock(
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

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._conversation_cache),
                "get_thread_id_for_event",
                get_thread_id_for_event,
            ),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=None)),
        ):
            await _dispatch_reaction(bot, room, event)

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
        resolve_related_event_thread_id = AsyncMock(
            return_value="$thread-root",
        )
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock()
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        event.reacts_to = "$plain-reply"

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._conversation_resolver),
                "resolve_related_event_thread_id_dispatch_snapshot_best_effort",
                resolve_related_event_thread_id,
            ),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=None)),
        ):
            await _dispatch_reaction(bot, room, event)

        resolve_related_event_thread_id.assert_awaited_once_with(
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
        get_thread_id_for_event = AsyncMock(
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

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._conversation_cache),
                "get_thread_id_for_event",
                get_thread_id_for_event,
            ),
            patch("mindroom.bot.interactive.handle_reaction", new=AsyncMock(return_value=None)),
        ):
            await _dispatch_reaction(bot, room, event)

        assert seen == [("$plain-reply-2", "$thread-root")]
