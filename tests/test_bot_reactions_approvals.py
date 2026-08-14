"""Reaction handling, interactive selections, and tool-approval flows on AgentBot."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.agent import Agent as AgnoAgent
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom import interactive
from mindroom.approval_manager import (
    get_approval_store,
    initialize_approval_store,
)
from mindroom.bot import AgentBot
from mindroom.coalescing import ReadyPendingEvent
from mindroom.coalescing_batch import PendingEvent, PreparedTurn, requester_coalescing_key
from mindroom.config.auth import AgentReplyPermission, AuthorizationConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.event_journal import (
    AdmissionResult,
    ApprovalContinuation,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
    SemanticConsumer,
)
from mindroom.handled_turns import TurnRecord, with_user_stop
from mindroom.hooks import (
    EVENT_REACTION_RECEIVED,
    HookRegistry,
    ReactionReceivedContext,
    hook,
)
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest, ResponseRunner, _DeliveryProgress
from mindroom.response_turn import paused_attempt_from_response
from mindroom.room_thread_modes import set_room_thread_mode_override
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    ApprovalActionResult,
    MatrixApprovalAction,
    SentApprovalEvent,
    shutdown_approval_runtime,
)
from tests.approval_test_support import FakeApprovalCards
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_plugin,
    make_mock_agent_user,
)
from tests.bot_helpers import (
    dispatch_reaction_durably as _dispatch_reaction,
)
from tests.conftest import (
    activate_interactive_prompt,
    install_relation_lookup,
    make_matrix_client_mock,
    replace_interactive_selection_handlers,
    replace_reaction_dispatcher_deps,
    replace_turn_controller_deps,
    request_envelope,
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
            "tool_call_id": "call-1",
            "continuation_id": "continuation-1",
            "continuation_generation": 0,
            "arguments": {"path": "notes.txt"},
            "status": "pending",
            "requester_id": "@user:localhost",
            "approver_user_id": "@user:localhost",
            "requested_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        },
    }


async def _cancel_dispatch_retry(bot: AgentBot) -> None:
    await bot._journal_dispatcher.stop()


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


def _reaction_event(key: str, event_id: str, *, timestamp: int = 1) -> nio.ReactionEvent:
    event = nio.Event.parse_event(
        {
            "type": "m.reaction",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": timestamp,
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


def _message_event(event_id: str) -> nio.RoomMessageText:
    event = nio.Event.parse_event(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 2,
            "content": {"msgtype": "m.text", "body": "Another thread"},
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _claimed_test_selection(
    bot: AgentBot,
    *,
    room_id: str = "!test:localhost",
) -> tuple[interactive.InteractiveSelection, MessageTarget]:
    """Create one interactive selection and its canonical target."""
    selection = interactive.InteractiveSelection(
        question_event_id="$question",
        question_text="Choose one",
        selection_key="👍",
        selected_label="Selected",
        selected_value="Selected",
        thread_id="$thread-a",
    )
    target = bot._conversation_resolver.build_message_target(
        room_id=room_id,
        thread_id=selection.thread_id,
        reply_to_event_id=selection.question_event_id,
    )
    return selection, target


def _mock_interactive_claim(
    bot: AgentBot,
    selection: interactive.InteractiveSelection | None,
) -> AbstractContextManager[AsyncMock]:
    """Replace one bot's journal-owned interactive claim boundary."""
    return patch.object(
        unwrap_extracted_collaborator(bot._journal_dispatcher),
        "claim_interactive_reaction",
        new=AsyncMock(return_value=selection),
    )


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
    await bot._journal_dispatcher.admit_out_of_band(room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    await bot._journal_dispatcher.drain_once()


def _direct_response_request(target: MessageTarget, prompt: str, source_event_id: str) -> ResponseRequest:
    return ResponseRequest(
        prompt=prompt,
        thread_history=[],
        user_id="@user:localhost",
        response_envelope=request_envelope(
            target=target,
            reply_to_event_id=source_event_id,
            prompt=prompt,
        ),
    )


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_recover_approval_final_owns_recovery_client_lifetime(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """The bot that mutates a recovery-only client must own its full lifetime."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))

        with (
            patch.object(bot, "_open_approval_recovery_client", new=AsyncMock()) as open_client,
            patch.object(bot._response_runner, "recover_approval_final", new=AsyncMock(return_value=True)) as recover,
            patch.object(bot, "_close_approval_recovery_client", new=AsyncMock()) as close_client,
        ):
            assert await bot.recover_approval_final("approval-1")

        open_client.assert_awaited_once_with()
        recover.assert_awaited_once_with("approval-1")
        close_client.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_recovery_client_waits_for_post_effects_before_close(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Original-principal recovery must not close Matrix under queued post-effects."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        order: list[str] = []
        client = MagicMock()
        client.close = AsyncMock(side_effect=lambda: order.append("close"))
        bot.client = client

        async def wait_for_effects(*_args: object, **_kwargs: object) -> bool:
            order.append("effects")
            return True

        with patch("mindroom.bot.wait_for_background_tasks", side_effect=wait_for_effects) as wait:
            await bot._close_approval_recovery_client()

        assert order == ["effects", "close"]
        wait.assert_awaited_once_with(timeout=5.0, owner=bot._runtime_view)
        assert bot.client is None

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

        with _mock_interactive_claim(bot, None):
            await _dispatch_reaction(bot, room, event)

        assert seen == [("👍", "$question", "$thread-root")]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("enforce_turn_authorization")
    async def test_reaction_hook_waits_for_reload_and_rechecks_reply_authorization(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A generic hook must not run after a reply-policy reload revokes its sender."""
        sender_id = "@user:localhost"
        config = self._config_for_storage(tmp_path)
        config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                mock_agent_user.agent_name: AgentReplyPermission(users=[sender_id]),
            },
        )
        denied_config = config.model_copy(deep=True)
        denied_config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                mock_agent_user.agent_name: AgentReplyPermission(users=[]),
            },
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = MagicMock()
        seen: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_reaction(ctx: ReactionReceivedContext) -> None:
            seen.append(ctx.event_id)

        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_reaction])])
        room = MagicMock(room_id="!test:localhost", canonical_alias=None)
        event = self._make_handler_event("reaction", sender=sender_id, event_id="$reaction-during-reload")
        event.reacts_to = ""
        claim_started = asyncio.Event()
        release_claim = asyncio.Event()

        async def delayed_interactive_claim() -> None:
            claim_started.set()
            await release_claim.wait()

        gate = bot._runtime_view.response_admission_gate
        dispatcher = unwrap_extracted_collaborator(bot._journal_dispatcher)
        with patch.object(dispatcher, "claim_interactive_reaction", side_effect=delayed_interactive_claim):
            reaction_task = asyncio.create_task(_dispatch_reaction(bot, room, event))
            await asyncio.wait_for(claim_started.wait(), timeout=1)
            assert gate.close_if_idle()
            bot.config = denied_config
            release_claim.set()
            await asyncio.sleep(0)
            try:
                assert not reaction_task.done()
                assert seen == []
            finally:
                gate.reopen()
                await reaction_task

        assert seen == []

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
        replace_interactive_selection_handlers(bot, handle=AsyncMock(return_value=False))
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="1",
            selected_label="Selected",
            selected_value="Selected",
            thread_id=None,
        )

        with _mock_interactive_claim(bot, selection):
            await _dispatch_reaction(bot, room, event)

        await bot._response_runner.drain_inbox_responses()
        assert seen == []
        assert event.event_id not in bot._journal_dispatcher._deferred_reaction_ids
        assert event.event_id not in bot._journal_dispatcher._worker._deferred

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
                unwrap_extracted_collaborator(bot._journal_dispatcher),
                "semantic_consumer",
                new=MagicMock(return_value=SemanticConsumer.INTERACTIVE_REACTION),
            ),
            _mock_interactive_claim(bot, None) as interactive_handler,
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
                unwrap_extracted_collaborator(bot._journal_dispatcher),
                "semantic_consumer",
                new=MagicMock(return_value=SemanticConsumer.CONFIG_CONFIRMATION),
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
    @pytest.mark.usefixtures("enforce_turn_authorization")
    async def test_fresh_config_confirmation_waits_for_reload_and_rechecks_authorization(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An uncommitted config decision must not apply after room access is revoked."""
        sender_id = "@user:localhost"
        config = self._config_for_storage(tmp_path)
        config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                ROUTER_AGENT_NAME: AgentReplyPermission(users=[sender_id]),
            },
        )
        router_user = replace(
            mock_agent_user,
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="RouterAgent",
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("✅", "$fresh-config")
        pending_change = MagicMock(decision_event_id=None)
        resolution_started = asyncio.Event()
        release_resolution = asyncio.Event()

        async def delayed_resolution(*_args: object, **_kwargs: object) -> object:
            resolution_started.set()
            await release_resolution.wait()
            return pending_change

        replacement = config.model_copy(deep=True)
        replacement.authorization = AuthorizationConfig(
            default_room_access=False,
            room_permissions={room.room_id: []},
            agent_reply_permissions={
                ROUTER_AGENT_NAME: AgentReplyPermission(users=[sender_id]),
            },
        )
        handler = AsyncMock()
        gate = bot.admission_gate
        with (
            patch(
                "mindroom.bot.config_confirmation.resolve_reaction_pending_change",
                side_effect=delayed_resolution,
            ),
            patch(
                "mindroom.bot.config_confirmation.handle_confirmation_reaction",
                new=handler,
            ),
        ):
            reaction = asyncio.create_task(_dispatch_reaction(bot, room, event))
            try:
                await asyncio.wait_for(resolution_started.wait(), timeout=1)
                assert gate.close_if_idle()
                bot.config = replacement
                release_resolution.set()
                await asyncio.sleep(0)
                handler.assert_not_awaited()
                gate.reopen()
                await reaction
            finally:
                release_resolution.set()
                gate.reopen()
                if not reaction.done():
                    reaction.cancel()
                await asyncio.gather(reaction, return_exceptions=True)

        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interactive_reaction_failure_replays_the_same_durable_claim(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A failed detached response leaves its source-owned selection replayable."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = MagicMock(room_id="!test:localhost", canonical_alias=None)
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
        store = bot._journal_store.principal(bot._journal_principal_id)
        admission = await activate_interactive_prompt(
            store,
            question_event_id="$question",
            room_id=room.room_id,
            sender=bot.matrix_id.full_id,
            creator_agent=bot.agent_name,
            question_text="Choose one",
            options={"👍": "approve"},
            option_labels={"👍": "Approve"},
        )
        assert admission is AdmissionResult.ADMITTED

        execute = AsyncMock(side_effect=(OSError("pending write failed"), None))
        with patch.object(bot._turn_controller, "_execute_interactive_selection", new=execute):
            await _dispatch_reaction(bot, room, event)
            await bot._response_runner.drain_inbox_responses()
            assert event.event_id in await bot._journal_dispatcher.unsettled_event_ids()

            await bot._journal_dispatcher.drain_once()
            await bot._response_runner.drain_inbox_responses()

        assert event.event_id not in await bot._journal_dispatcher.unsettled_event_ids()
        assert execute.await_count == 2

    @pytest.mark.asyncio
    async def test_departure_consumes_a_failed_interactive_claim(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A departure retires both the pending source and its stored selection."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = MagicMock(room_id="!test:localhost", canonical_alias=None)
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
        store = bot._journal_store.principal(bot._journal_principal_id)
        admission = await activate_interactive_prompt(
            store,
            question_event_id="$question",
            room_id=room.room_id,
            sender=bot.matrix_id.full_id,
            creator_agent=bot.agent_name,
            question_text="Choose one",
            options={"👍": "approve"},
            option_labels={"👍": "Approve"},
        )
        assert admission is AdmissionResult.ADMITTED

        with patch.object(
            bot._turn_controller,
            "_execute_interactive_selection",
            new=AsyncMock(side_effect=OSError("pending write failed")),
        ):
            await _dispatch_reaction(bot, room, event)
            await bot._response_runner.drain_inbox_responses()

        await bot._membership_fence.fence_local_departure(room.room_id)

        assert event.event_id not in await bot._journal_dispatcher.unsettled_event_ids()
        rows = await bot._journal_store.backend.read(
            lambda transaction: transaction.fetchall(
                "SELECT question_event_id FROM interactive_questions WHERE principal_id = ?",
                (bot._journal_principal_id,),
            ),
        )
        assert rows == ()

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

        async def handle_selection(*_args: object, **_kwargs: object) -> bool:
            selection_started.set()
            assert bot._coalescing_gate.lanes.unsettled_slots()
            return False

        replace_interactive_selection_handlers(bot, handle=handle_selection)
        with _mock_interactive_claim(bot, selection):
            await _dispatch_reaction(bot, room, event)

        await asyncio.wait_for(selection_started.wait(), timeout=0.5)
        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_interactive_reaction_enqueues_barrier_before_releasing_prompt_lane(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Selection handoff must enter its FIFO barrier before releasing ingress order."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = MagicMock(room_id="!test:localhost", canonical_alias=None)
        event = self._make_handler_event("reaction", sender="@user:localhost", event_id="$reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-a",
        )
        startup_observed = asyncio.Event()
        release_startup = asyncio.Event()
        reservation_owner = MagicMock()
        reservation_owner.release = AsyncMock()
        release_count_during_startup = -1

        async def blocked_enqueue(*_args: object, **_kwargs: object) -> None:
            nonlocal release_count_during_startup
            release_count_during_startup = reservation_owner.release.await_count
            startup_observed.set()
            await release_startup.wait()

        replace_reaction_dispatcher_deps(
            bot,
            reserve_prompt_ingress_order=MagicMock(return_value=reservation_owner),
            enqueue_interactive_selection=blocked_enqueue,
        )
        with _mock_interactive_claim(bot, selection):
            dispatch = asyncio.create_task(_dispatch_reaction(bot, room, event))
            await startup_observed.wait()
            assert release_count_during_startup == 0
            release_startup.set()
            await dispatch

        reservation_owner.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interactive_reaction_waits_for_earlier_same_thread_ingress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A reaction response must not overtake an earlier queued thread message."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-a",
        )
        reaction = _reaction_event("👍", "$reaction")
        message_dispatch_started = asyncio.Event()
        release_message_dispatch = asyncio.Event()
        execution_order: list[str] = []
        original_dispatch = bot._coalescing_gate._dispatch_turn

        async def dispatch_turn(turn: PreparedTurn) -> None:
            if turn.event.event_id == "$earlier":
                message_dispatch_started.set()
                await release_message_dispatch.wait()
                execution_order.append("message")
                return
            await original_dispatch(turn)

        async def start_selection(
            _response_factory: Callable[[], Awaitable[None]],
            **_kwargs: object,
        ) -> None:
            execution_order.append("reaction")

        monkeypatch.setattr(bot._coalescing_gate, "_dispatch_turn", dispatch_turn)
        replace_interactive_selection_handlers(bot, start=start_selection)
        earlier_owner = bot._turn_controller.reserve_prompt_ingress_order(room, "@user:localhost")
        earlier_event = PreparedIngress(
            sender="@user:localhost",
            event_id="$earlier",
            body="Earlier message",
            source={"content": {"msgtype": "m.text", "body": "Earlier message"}},
            requester_user_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        )
        await earlier_owner.admit(
            requester_coalescing_key(room.room_id, "$thread-a", "@user:localhost"),
            source_event_id=earlier_event.event_id,
            source_kind=MESSAGE_SOURCE_KIND,
            ready_result=ReadyPendingEvent(pending_event=PendingEvent(event=earlier_event, room=room)),
        )

        try:
            await asyncio.wait_for(message_dispatch_started.wait(), timeout=1.0)
            with _mock_interactive_claim(bot, selection):
                await _dispatch_reaction(bot, room, reaction)
            release_message_dispatch.set()
            await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
            assert execution_order == ["message", "reaction"]
        finally:
            release_message_dispatch.set()
            await bot._coalescing_gate.drain_all()

    @pytest.mark.asyncio
    async def test_interactive_reaction_response_does_not_hold_room_journal_lane(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A parked selection response must not delay a different root thread."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        reaction = _reaction_event("👍", "$interactive-reaction")
        message = _message_event("$other-thread-root")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-a",
        )
        selection_started = asyncio.Event()
        release_selection = asyncio.Event()
        message_started = asyncio.Event()

        async def blocked_selection(*_args: object, **_kwargs: object) -> bool:
            selection_started.set()
            await release_selection.wait()
            return False

        async def handle_other_thread(*_args: object, **_kwargs: object) -> TurnDispatchOutcome:
            message_started.set()
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED

        replace_interactive_selection_handlers(bot, handle=blocked_selection)
        monkeypatch.setattr(
            unwrap_extracted_collaborator(bot._turn_controller),
            "handle_text_event",
            handle_other_thread,
        )
        bot._journal_dispatcher.start()
        try:
            with _mock_interactive_claim(bot, selection):
                await bot._journal_dispatcher.admit_out_of_band(
                    room,
                    reaction,
                    EventKind.REACTION,
                    EventClass.ACTIONABLE,
                )
                await asyncio.wait_for(selection_started.wait(), timeout=1.0)
                await bot._journal_dispatcher.admit_out_of_band(
                    room,
                    message,
                    EventKind.MESSAGE,
                    EventClass.ACTIONABLE,
                )

                await asyncio.wait_for(message_started.wait(), timeout=1.0)
                assert bot._journal_dispatcher.callbacks.source_has_live_owner(reaction.event_id)
                assert reaction.event_id in await bot._journal_dispatcher.unsettled_event_ids()
        finally:
            release_selection.set()
            await bot._response_runner.drain_inbox_responses()
            await bot._journal_dispatcher.stop()

    @pytest.mark.asyncio
    async def test_interactive_preparation_waits_for_older_same_thread_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Selection pre-generation work must wait behind its reserved lifecycle lock."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        reaction = _reaction_event("👍", "$interactive-reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-a",
        )
        older_target = MessageTarget.resolve(room.room_id, selection.thread_id, "$older")
        older_entered = asyncio.Event()
        release_older = asyncio.Event()
        preparation_started = asyncio.Event()

        async def generate_locked(
            _self: ResponseRunner,
            _request: ResponseRequest,
            *,
            resolved_target: MessageTarget,
            early_placeholder_state: object,
        ) -> str:
            del resolved_target, early_placeholder_state
            older_entered.set()
            await release_older.wait()
            return "$older-response"

        async def handle_selection(*_args: object, **_kwargs: object) -> bool:
            preparation_started.set()
            return False

        replace_interactive_selection_handlers(bot, handle=handle_selection)
        older: asyncio.Task[str | None] | None = None
        try:
            with (
                patch.object(ResponseRunner, "_generate_response_locked", new=generate_locked),
                _mock_interactive_claim(bot, selection),
            ):
                older = asyncio.create_task(
                    bot._response_runner.generate_response(
                        _direct_response_request(older_target, "older", "$older"),
                    ),
                )
                await older_entered.wait()
                await _dispatch_reaction(bot, room, reaction)

                assert not preparation_started.is_set()
                assert bot._journal_dispatcher.callbacks.source_has_live_owner(reaction.event_id)

                release_older.set()
                assert await older == "$older-response"
                await preparation_started.wait()
                await bot._response_runner.drain_inbox_responses()
        finally:
            release_older.set()
            if older is not None:
                await asyncio.gather(older, return_exceptions=True)
            await bot._response_runner.drain_inbox_responses()

    @pytest.mark.asyncio
    async def test_cancelled_interactive_preparation_releases_same_thread_reservation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Cancelling detached preparation must release its early lifecycle reservation."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        preparation_started = asyncio.Event()

        async def prepare_forever() -> None:
            preparation_started.set()
            await asyncio.Event().wait()

        target = bot._conversation_resolver.build_message_target(
            room_id="!test:localhost",
            thread_id="$thread-a",
            reply_to_event_id="$question",
        )
        await bot._turn_controller._start_interactive_selection(
            prepare_forever,
            response_target=target,
            source_event_id="$reaction",
            user_id="@user:localhost",
            selected_value="Selected",
        )
        await asyncio.wait_for(preparation_started.wait(), timeout=1.0)
        assert bot._response_runner.has_active_response_for_target(target)

        assert await bot._response_runner.drain_inbox_responses(cancel_after_seconds=0.01) is False
        assert not bot._response_runner.has_active_response_for_target(target)

    @pytest.mark.asyncio
    async def test_interactive_postresponse_settlement_failure_does_not_run_prestart_cleanup(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Journal cleanup after a completed response must not restore its selection."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        settlement_error = OSError("simulated journal settlement failure")
        settle_dispatch_sources = AsyncMock(side_effect=settlement_error)
        retry_dispatch_sources = MagicMock()
        controller = replace_turn_controller_deps(
            bot,
            settle_dispatch_sources=settle_dispatch_sources,
            retry_dispatch_sources=retry_dispatch_sources,
        )
        response_completed = asyncio.Event()

        async def response() -> None:
            response_completed.set()

        target = bot._conversation_resolver.build_message_target(
            room_id="!test:localhost",
            thread_id="$thread-a",
            reply_to_event_id="$question",
        )
        await controller._start_interactive_selection(
            response,
            response_target=target,
            source_event_id="$reaction",
            user_id="@user:localhost",
            selected_value="Selected",
        )
        await bot._response_runner.drain_inbox_responses()

        assert response_completed.is_set()
        settle_dispatch_sources.assert_awaited_once_with(("$reaction",))
        # The failed settlement must hand the exact journal source back for retry.
        retry_dispatch_sources.assert_called_once_with(("$reaction",))

    @pytest.mark.asyncio
    async def test_interactive_approval_handoff_skips_fallback_source_settlement(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A suspended reaction response leaves its source to the approval continuation."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        settle_dispatch_sources = AsyncMock()
        controller = replace_turn_controller_deps(
            bot,
            settle_dispatch_sources=settle_dispatch_sources,
        )

        async def hand_off_response() -> bool:
            return True

        target = bot._conversation_resolver.build_message_target(
            room_id="!test:localhost",
            thread_id="$thread-a",
            reply_to_event_id="$question",
        )
        await controller._start_interactive_selection(
            hand_off_response,
            response_target=target,
            source_event_id="$reaction",
            user_id="@user:localhost",
            selected_value="Selected",
        )
        await bot._response_runner.drain_inbox_responses()

        settle_dispatch_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_interactive_selection_preserves_task_cancellation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Durable selection cleanup must not turn cancellation into success."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        source_is_terminal = AsyncMock(return_value=True)
        controller = replace_turn_controller_deps(
            bot,
            dispatch_source_is_terminal=source_is_terminal,
        )
        selection, target = _claimed_test_selection(bot)
        room = nio.MatrixRoom(target.room_id, bot.matrix_id.full_id)

        with (
            patch.object(
                controller,
                "_execute_interactive_selection",
                new=AsyncMock(side_effect=asyncio.CancelledError("restart")),
            ),
            pytest.raises(asyncio.CancelledError, match="restart"),
        ):
            await controller._handle_interactive_selection(
                room,
                selection=selection,
                user_id="@user:localhost",
                source_event_id="$reaction",
                response_target=target,
            )

        source_is_terminal.assert_awaited_once_with("$reaction")

    @pytest.mark.asyncio
    async def test_terminal_interactive_selection_survives_repeated_cancellation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Terminal reconciliation finishes before a second cancellation propagates."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        execution_started = asyncio.Event()
        terminal_probe_started = asyncio.Event()
        release_terminal_probe = asyncio.Event()

        async def execute_selection(*_args: object, **_kwargs: object) -> None:
            execution_started.set()
            await asyncio.Event().wait()

        async def source_is_terminal(_source_event_id: str) -> bool:
            terminal_probe_started.set()
            await release_terminal_probe.wait()
            return True

        controller = replace_turn_controller_deps(bot, dispatch_source_is_terminal=source_is_terminal)
        selection, target = _claimed_test_selection(bot)
        room = nio.MatrixRoom(target.room_id, bot.matrix_id.full_id)

        with patch.object(controller, "_execute_interactive_selection", new=execute_selection):
            response = asyncio.create_task(
                controller._handle_interactive_selection(
                    room,
                    selection=selection,
                    user_id="@user:localhost",
                    source_event_id="$reaction",
                    response_target=target,
                ),
            )
            await execution_started.wait()
            response.cancel("first restart cancellation")
            await terminal_probe_started.wait()
            response.cancel("second restart cancellation")
            await asyncio.sleep(0)
            assert not response.done()

            release_terminal_probe.set()
            with pytest.raises(asyncio.CancelledError):
                await response

    @pytest.mark.asyncio
    async def test_restart_stop_waits_for_live_interactive_claim_owner(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A replacement cannot start while the prior generation still owns a source."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        response_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()
        ordinary_response_started = asyncio.Event()

        async def response_owner() -> None:
            response_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await finish_cleanup.wait()

        async def ordinary_response() -> None:
            ordinary_response_started.set()
            await asyncio.Event().wait()

        response_task = bot._response_runner.track_inbox_response(
            response_owner(),
            name="test_interactive_claim_owner",
            recovery_proof_ready=lambda: False,
            source_event_ids=("$reaction",),
        )
        ordinary_response_task = bot._response_runner.track_inbox_response(
            ordinary_response(),
            name="test_ordinary_response",
            recovery_proof_ready=lambda: False,
        )
        await response_started.wait()
        await ordinary_response_started.wait()
        response_task.cancel()
        await cleanup_started.wait()
        dispatcher_stop = AsyncMock()

        try:
            with (
                patch.object(bot, "prepare_for_sync_shutdown", new=AsyncMock()),
                patch.object(bot, "_emit_agent_lifecycle_event", new=AsyncMock()),
                patch.object(bot._journal_dispatcher, "stop", new=dispatcher_stop),
                patch.object(bot, "_own_journal", None),
            ):
                stop_task = asyncio.create_task(bot.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN))
                await asyncio.sleep(0)

                assert not stop_task.done()
                dispatcher_stop.assert_awaited_once_with()

                finish_cleanup.set()
                await asyncio.wait_for(stop_task, timeout=1.0)

            assert response_task.done()
            assert not ordinary_response_task.done()
        finally:
            finish_cleanup.set()
            ordinary_response_task.cancel()
            await asyncio.gather(response_task, ordinary_response_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_approval_resume_handoff_releases_lane_and_tracks_every_source(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A resumed Agno run belongs to the response runtime, not the room journal lane."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        store = bot.approval_store
        for ordinal, event_id in enumerate(("$source", "$coalesced"), start=1):
            await store.admit(
                InboundEvent(
                    event_id=event_id,
                    room_id="!test:localhost",
                    thread_id="$thread",
                    kind=EventKind.MESSAGE,
                    event_class=EventClass.ACTIONABLE,
                    sender="@user:localhost",
                    origin_server_ts=ordinal,
                    source={"event_id": event_id, "content": {"body": "run it"}},
                ),
                ProjectedEvent(
                    event_id=event_id,
                    room_id="!test:localhost",
                    thread_id="$thread",
                    sender="@user:localhost",
                    origin_server_ts=ordinal,
                    content={"body": "run it"},
                    replaces_event_id=None,
                    redacts_event_id=None,
                ),
            )
        continuation = ApprovalContinuation(
            approval_id="approval-handoff",
            run_id="run-1",
            session_id="session-1",
            entity_kind="agent",
            entity_name=bot.agent_name,
            room_id="!test:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            response_event_id="$waiting",
            source_event_ids=("$source", "$coalesced"),
            calls=(),
            state="ready",
        )
        assert await store.create_approval_continuation(continuation) == continuation
        resume_started = asyncio.Event()
        release_resume = asyncio.Event()

        async def resume(_source_event_id: str) -> bool:
            resume_started.set()
            await release_resume.wait()
            return False

        with patch.object(bot._response_runner, "_resume_approval_source", new=resume):
            handoff = asyncio.create_task(
                bot._journal_dispatcher.callbacks.on_approval_continuation("$source"),
            )
            try:
                await resume_started.wait()
                await asyncio.sleep(0)

                assert handoff.done()
                assert await handoff is False
                assert bot._response_runner.has_live_inbox_response("$source")
                assert bot._response_runner.has_live_inbox_response("$coalesced")
                deferred = await store.load_event("$source")
                assert deferred is not None
                bot._journal_dispatcher._worker._deferred[deferred.event_id] = deferred
                assert bot._journal_dispatcher._deferral_is_live(deferred)
                assert bot._journal_dispatcher._worker._reclaim_lost_deferrals() == {}
            finally:
                release_resume.set()
                await asyncio.gather(handoff, return_exceptions=True)
                await bot._response_runner.wait_for_source_owned_inbox_responses()
                assert "$source" not in bot._journal_dispatcher._worker._deferred

    @pytest.mark.parametrize(
        ("requires_human", "redact_while_waiting"),
        [(False, False), (True, False), (True, True)],
        ids=["automatic", "human", "human-redacted"],
    )
    @pytest.mark.asyncio
    async def test_persisted_pause_resumes_once_in_fresh_bot_runtime(  # noqa: PLR0915 - full restart boundary
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        requires_human: bool,
        redact_while_waiting: bool,
    ) -> None:
        """A fresh bot must execute the persisted tool once after policy or card consent."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        session_db = tmp_path / "persisted-approval-agent.db"
        executed: list[list[str]] = []
        target = MessageTarget.resolve("!test:localhost", None, "$source")

        def run_shell_command(args: list[str]) -> str:
            executed.append(args)
            return "ok"

        def new_agent() -> AgnoAgent:
            return AgnoAgent(
                id=mock_agent_user.agent_name,
                model=SyntheticModel(
                    id="synthetic",
                    seed=1,
                    min_response_chars=20,
                    max_response_chars=20,
                    chars_per_second=0,
                    tool_call_probability=1,
                ),
                tools=[
                    Function(
                        name="run_shell_command",
                        entrypoint=run_shell_command,
                        requires_confirmation=True,
                    ),
                ],
                db=SqliteDb(db_file=str(session_db), session_table="sessions"),
            )

        first_agent = new_agent()
        paused_response = await first_agent.arun(
            "exercise the tool",
            session_id=target.session_id,
            user_id="@user:localhost",
            stream=False,
        )
        assert isinstance(paused_response, RunOutput)
        assert paused_response.status is RunStatus.paused
        paused = paused_attempt_from_response(
            paused_response,
            fallback_session_id=target.session_id,
            fallback_run_id=paused_response.run_id,
        )
        assert paused is not None
        paused.tools[0].approval_type = POLICY_CONFIRMATION_APPROVAL_TYPE

        first = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        first.client = make_matrix_client_mock(user_id=mock_agent_user.user_id)
        first.client.room_send.return_value = nio.RoomSendResponse("$waiting", "!test:localhost")
        router_principal_id = "router@@mindroom_router:localhost"
        if requires_human:
            initialize_approval_store(
                runtime_paths,
                sender=AsyncMock(return_value=SentApprovalEvent("$approval")),
                editor=AsyncMock(return_value=True),
                cards=first._journal_store.principal(router_principal_id),
                approval_room_ids=lambda: {"!test:localhost"},
                transport_sender=lambda: "@mindroom_router:localhost",
                sending_device=lambda: "DEVICE",
            )
        source = InboundEvent(
            event_id="$source",
            room_id="!test:localhost",
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={"event_id": "$source", "content": {"body": "exercise the tool"}},
        )
        projected = ProjectedEvent(
            event_id="$source",
            room_id="!test:localhost",
            thread_id=None,
            sender="@user:localhost",
            origin_server_ts=1,
            content={"body": "exercise the tool"},
            replaces_event_id=None,
            redacts_event_id=None,
        )
        assert await first.approval_store.admit(source, projected)
        request = _direct_response_request(target, "exercise the tool", "$source")
        identity = first._response_runner.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id="@user:localhost",
            agent_name=mock_agent_user.agent_name,
        )

        with patch(
            "mindroom.approval_response.evaluate_tool_approval",
            new=AsyncMock(return_value=(requires_human, 60.0)),
        ):
            suspended = await first._response_runner._suspend_for_approval(
                paused,
                request=request,
                target=target,
                progress=_DeliveryProgress(),
                execution_identity=identity,
                entity_kind="agent",
                history_scope=first._response_runner.deps.state_writer.history_scope(),
            )

        assert suspended.terminal_status == "suspended"
        assert executed == []
        persisted = await first.approval_store.approval_continuation_for_source("$source")
        if requires_human:
            await shutdown_approval_runtime()
        assert persisted is not None
        assert persisted.state == ("waiting" if requires_human else "ready")
        if redact_while_waiting:
            # The durable approval card remains the explicit consent surface even
            # when the requester removes the original room message.
            redaction = InboundEvent(
                event_id="$redaction",
                room_id="!test:localhost",
                thread_id=None,
                kind=EventKind.REDACTION,
                event_class=EventClass.CONTEXT_ONLY,
                sender="@user:localhost",
                origin_server_ts=2,
                source={"event_id": "$redaction", "redacts": "$source", "content": {}},
            )
            projected_redaction = ProjectedEvent(
                event_id="$redaction",
                room_id="!test:localhost",
                thread_id=None,
                sender="@user:localhost",
                origin_server_ts=2,
                content={},
                replaces_event_id=None,
                redacts_event_id="$source",
            )
            assert await first.approval_store.admit(redaction, projected_redaction)
            conversation = await first.approval_store.read_conversation(
                room_id="!test:localhost",
                thread_id=None,
                limit=10,
            )
            assert conversation.messages == ()
            assert await first.approval_store.approval_continuation_for_source("$source") == persisted
        if first_agent.db is not None:
            first_agent.db.close()

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock(user_id=mock_agent_user.user_id)
        restarted.client.room_send.return_value = nio.RoomSendResponse("$final", "!test:localhost")

        async def drain_restarted_runtime() -> None:
            restarted._journal_dispatcher.release_turn_replay()
            await restarted._journal_dispatcher.drain_once()
            await restarted._response_runner.wait_for_source_owned_inbox_responses()
            await restarted._journal_dispatcher.drain_once()

        with patch("mindroom.approval_execution.create_agent", side_effect=lambda *_args, **_kwargs: new_agent()):
            if requires_human:
                manager = initialize_approval_store(
                    runtime_paths,
                    editor=AsyncMock(return_value=True),
                    cards=restarted._journal_store.principal(router_principal_id),
                    approval_room_ids=lambda: {"!test:localhost"},
                    transport_sender=lambda: "@mindroom_router:localhost",
                    sending_device=lambda: "DEVICE",
                    continuation_ready=lambda _entity_name, source_ids: restarted.retry_approval_sources(source_ids),
                )
                restarted._journal_dispatcher.release_turn_replay()
                try:
                    resolved = await manager.handle_card_response(
                        room_id="!test:localhost",
                        sender_id="@user:localhost",
                        card_event_id="$approval",
                        status="approved",
                        reason=None,
                    )
                    assert resolved.consumed is True
                    assert resolved.resolved is True
                    await drain_restarted_runtime()
                finally:
                    await shutdown_approval_runtime()
            else:
                await drain_restarted_runtime()

        remaining = await restarted.approval_store.approval_continuation_for_source("$source")
        assert len(executed) == 1, remaining
        assert remaining is None
        assert not await restarted.approval_store.is_pending("$source")

    @pytest.mark.asyncio
    async def test_restart_stop_waits_for_source_owner_registered_during_dispatcher_stop(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Quiescing journal lanes must precede the final source-owner wait."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        response_started = asyncio.Event()
        finish_response = asyncio.Event()
        response_tasks: list[asyncio.Task[None]] = []

        async def response_owner() -> None:
            response_started.set()
            await finish_response.wait()

        async def stop_dispatcher() -> None:
            response_tasks.append(
                bot._response_runner.track_inbox_response(
                    response_owner(),
                    name="test_late_interactive_claim_owner",
                    recovery_proof_ready=lambda: False,
                    source_event_ids=("$reaction",),
                ),
            )
            await response_started.wait()

        stop_task: asyncio.Task[None] | None = None
        try:
            with (
                patch.object(bot, "prepare_for_sync_shutdown", new=AsyncMock()),
                patch.object(bot, "_emit_agent_lifecycle_event", new=AsyncMock()),
                patch.object(bot._journal_dispatcher, "stop", new=stop_dispatcher),
                patch.object(bot, "_own_journal", None),
            ):
                stop_task = asyncio.create_task(bot.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN))
                await response_started.wait()
                await asyncio.sleep(0)

                assert not stop_task.done()

                finish_response.set()
                await asyncio.wait_for(stop_task, timeout=1.0)

            assert response_tasks[0].done()
        finally:
            finish_response.set()
            cleanup_tasks = [task for task in (stop_task, *response_tasks) if task is not None]
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_stage", ["acquire", "queued_cancel"])
    async def test_interactive_prestart_failure_retries_durable_source(
        self,
        failure_stage: str,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Outer response ownership returns its journal source before preparation starts."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        selection, target = _claimed_test_selection(bot)
        retry_dispatch_sources = MagicMock()
        controller = replace_turn_controller_deps(
            bot,
            retry_dispatch_sources=retry_dispatch_sources,
        )
        response_entered = asyncio.Event()

        async def response() -> None:
            response_entered.set()

        lifecycle_lock = unwrap_extracted_collaborator(
            bot._response_runner,
        )._lifecycle_coordinator._response_lifecycle_lock(target)
        lock_owned_by_test = False
        if failure_stage == "acquire":
            acquisition_error = RuntimeError("simulated lifecycle acquisition failure")

            async def fail_acquire() -> bool:
                raise acquisition_error

            acquire_patch = patch.object(lifecycle_lock, "acquire", new=fail_acquire)
        else:
            await lifecycle_lock.acquire()
            lock_owned_by_test = True
            acquire_patch = nullcontext()

        try:
            with acquire_patch:
                await controller._start_interactive_selection(
                    response,
                    response_target=target,
                    source_event_id="$reaction",
                    user_id="@user:localhost",
                    selected_value=selection.selected_value,
                )
                if failure_stage == "queued_cancel":
                    assert await bot._response_runner.drain_inbox_responses(cancel_after_seconds=0.01) is False
                else:
                    await bot._response_runner.drain_inbox_responses()

            assert not response_entered.is_set()
            retry_dispatch_sources.assert_called_with(("$reaction",))
            if lock_owned_by_test:
                lifecycle_lock.release()
                lock_owned_by_test = False
            assert not bot._response_runner.has_active_response_for_target(target)
        finally:
            if lock_owned_by_test:
                lifecycle_lock.release()
            await bot._response_runner.drain_inbox_responses()

    @pytest.mark.asyncio
    async def test_interactive_reaction_reserves_same_thread_lifecycle_before_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A newer same-thread response must wait behind selection preparation."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        reaction = _reaction_event("👍", "$interactive-reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$thread-a",
        )
        target = MessageTarget.resolve(room.room_id, selection.thread_id, "$question")
        (
            preparation_started,
            release_preparation,
            interactive_entered,
            release_interactive,
            newer_lock_attempted,
            newer_entered,
        ) = (asyncio.Event() for _ in range(6))
        lifecycle_lock = unwrap_extracted_collaborator(
            bot._response_runner,
        )._lifecycle_coordinator._response_lifecycle_lock(target)
        acquire_lifecycle_lock = lifecycle_lock.acquire

        async def tracked_acquire() -> bool:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.get_name() == "newer-same-thread-response":
                newer_lock_attempted.set()
            return await acquire_lifecycle_lock()

        lifecycle_lock.acquire = tracked_acquire  # type: ignore[method-assign]

        async def handle_selection(*_args: object, **_kwargs: object) -> bool:
            preparation_started.set()
            await release_preparation.wait()
            await bot._response_runner.generate_response(
                _direct_response_request(target, "interactive", reaction.event_id),
            )
            return False

        async def generate_locked(
            _self: ResponseRunner,
            request: ResponseRequest,
            *,
            resolved_target: MessageTarget,
            early_placeholder_state: object,
        ) -> str:
            del resolved_target, early_placeholder_state
            if request.prompt == "interactive":
                interactive_entered.set()
                await release_interactive.wait()
                return "$interactive-response"
            newer_entered.set()
            return "$newer-response"

        replace_interactive_selection_handlers(bot, handle=handle_selection)
        newer_task: asyncio.Task[str | None] | None = None
        try:
            with (
                _mock_interactive_claim(bot, selection),
                patch.object(ResponseRunner, "_generate_response_locked", new=generate_locked),
            ):
                await _dispatch_reaction(bot, room, reaction)
                await asyncio.wait_for(preparation_started.wait(), timeout=1.0)

                newer_target = MessageTarget.resolve(room.room_id, selection.thread_id, "$newer")
                newer_task = asyncio.create_task(
                    bot._response_runner.generate_response(
                        _direct_response_request(newer_target, "newer", "$newer"),
                    ),
                    name="newer-same-thread-response",
                )
                await asyncio.wait_for(newer_lock_attempted.wait(), timeout=1.0)
                assert not newer_entered.is_set()

                release_preparation.set()
                await asyncio.wait_for(interactive_entered.wait(), timeout=1.0)
                assert not newer_entered.is_set()

                release_interactive.set()
                await asyncio.wait_for(newer_entered.wait(), timeout=1.0)
                assert await newer_task == "$newer-response"
                await bot._response_runner.drain_inbox_responses()
        finally:
            release_preparation.set()
            release_interactive.set()
            if newer_task is not None:
                await asyncio.gather(newer_task, return_exceptions=True)
            await bot._response_runner.drain_inbox_responses()

    @pytest.mark.asyncio
    async def test_interactive_reaction_keeps_room_mode_target_across_handoff(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reaction reservation and execution must share one configured target."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        room = nio.MatrixRoom("!test:localhost", mock_agent_user.user_id)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        reaction = _reaction_event("👍", "$interactive-reaction")
        selection = interactive.InteractiveSelection(
            question_event_id="$question",
            question_text="Choose one",
            selection_key="👍",
            selected_label="Selected",
            selected_value="Selected",
            thread_id="$original-thread",
        )
        set_room_thread_mode_override(
            runtime_paths,
            room_id=room.room_id,
            mode="room",
            set_by="@admin:localhost",
        )
        resolved_targets: list[MessageTarget] = []

        async def generate_locked(
            _self: ResponseRunner,
            _request: ResponseRequest,
            *,
            resolved_target: MessageTarget,
            early_placeholder_state: object,
        ) -> str:
            del early_placeholder_state
            resolved_targets.append(resolved_target)
            return "$response"

        bot._conversation_resolver.fetch_thread_history = AsyncMock(
            return_value=thread_history_result([], is_full_history=True),
        )
        bot._visible_responses.recovered_response_event_id = AsyncMock(return_value=None)
        bot._visible_responses.deliver_recoverable_text = AsyncMock(return_value="$ack")
        try:
            with (
                _mock_interactive_claim(bot, selection),
                patch.object(ResponseRunner, "_generate_response_locked", new=generate_locked),
            ):
                await _dispatch_reaction(bot, room, reaction)
                await bot._response_runner.drain_inbox_responses()

            assert [target.resolved_thread_id for target in resolved_targets] == [None]
            assert reaction.event_id not in await bot._journal_dispatcher.unsettled_event_ids()
        finally:
            await bot._response_runner.drain_inbox_responses()

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

        replace_interactive_selection_handlers(bot, handle=AsyncMock(return_value=False))
        with (
            patch("mindroom.reaction_dispatch.handle_tool_approval_action", side_effect=delayed_approval),
            _mock_interactive_claim(bot, selection),
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
            _mock_interactive_claim(bot, None) as interactive_handler,
        ):
            await _dispatch_reaction(bot, room, event)

        approval_handler.assert_awaited_once()
        interactive_handler.assert_not_awaited()
        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_unknown_tool_approval_response_without_card_event_is_ignored(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Approval-id-only custom events must not enter the card-anchored approval API."""
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

        handle_matrix_approval_action.assert_not_awaited()

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
        cards = FakeApprovalCards()
        store = initialize_approval_store(
            runtime_paths,
            cards=cards,
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
            assert cards.lookups == [("!test:localhost", "$ordinary-message")]
            assert store is get_approval_store()
        finally:
            await shutdown_approval_runtime()

    @pytest.mark.asyncio
    async def test_reply_to_detached_pending_approval_is_consumed_and_denies_card(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detached approval replies should deny their card instead of entering conversation input."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, bot)
        room = SimpleNamespace(room_id="!test:localhost", canonical_alias=None)
        cards = FakeApprovalCards()
        await cards.store_card("$approval", "!test:localhost", _detached_approval_card())
        editor = AsyncMock(return_value=True)
        initialize_approval_store(
            runtime_paths,
            editor=editor,
            cards=cards,
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
            assert replacement["status"] == "denied"
            assert replacement["resolution_reason"] == "Deny."
        finally:
            await shutdown_approval_runtime()

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
        cards = FakeApprovalCards()
        await cards.store_card("$approval", "!test:localhost", _detached_approval_card())
        editor = AsyncMock(return_value=True)
        initialize_approval_store(
            runtime_paths,
            editor=editor,
            cards=cards,
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
            assert cards.lookups == []
            editor.assert_not_awaited()
        finally:
            await shutdown_approval_runtime()

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
            await shutdown_approval_runtime()

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

        journal = bot._journal_dispatcher.store
        with patch("mindroom.bot.maybe_handle_tool_approval_reply", side_effect=consume_then_crash):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)

        # The crash leaves the event pending, but its consumer claim is durable,
        # so the replay cannot route the reply anywhere else.
        pending = await journal.pending()
        assert pending[0].semantic_consumer is SemanticConsumer.APPROVAL_REPLY

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        handle_text_event = _install_text_dispatch_mock(monkeypatch, restarted)
        with patch(
            "mindroom.bot.maybe_handle_tool_approval_reply",
            new=AsyncMock(return_value=False),
        ) as approval_reply:
            await restarted._journal_dispatcher.drain_once()

        approval_reply.assert_awaited_once()
        assert approval_reply.await_args.kwargs["before_consume"] is None
        assert approval_reply.await_args.kwargs["authorization_prevalidated"] is True
        handle_text_event.assert_not_awaited()
        assert await restarted._journal_dispatcher.store.pending() == ()

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
        ):
            await _dispatch_reaction(bot, room, event)
        await _cancel_dispatch_retry(bot)
        assert (await bot._journal_dispatcher.store.pending())[
            0
        ].semantic_consumer is SemanticConsumer.TOOL_APPROVAL_REACTION

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.approval_inbound.handle_matrix_approval_action",
            new=AsyncMock(return_value=ApprovalActionResult(consumed=False, resolved=False)),
        ):
            await restarted._journal_dispatcher.drain_once()

        assert unexpected_hooks == []
        assert await restarted._journal_dispatcher.store.pending() == ()

    @pytest.mark.ledger_loads_from_disk
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
        await bot._turn_store.warm()
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
        await bot._turn_store.record_pending_turn(pending_turn)
        failure = RuntimeError("crash after stop reaction side effect")
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch.object(
                bot._user_stop_reconciler,
                "finalize",
                new=AsyncMock(side_effect=failure),
            ),
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)
        pending = await bot._journal_dispatcher.store.pending()
        assert pending[0].semantic_consumer is SemanticConsumer.STOP_REACTION
        stop_receipt_order = pending[0].receipt_order

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        await restarted._turn_store.warm()
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)

        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._journal_dispatcher.drain_once()

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        assert unexpected_hooks == []
        assert restarted._turn_store.is_handled("$source") is True
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert await restarted._journal_dispatcher.store.pending() == ()

    @pytest.mark.ledger_loads_from_disk
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
        await bot._turn_store.warm()
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        await bot._turn_store.record_turn(
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
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)
        pending = await bot._journal_dispatcher.store.pending()
        stop_receipt_order = pending[0].receipt_order

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        await restarted._turn_store.warm()
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._journal_dispatcher.drain_once()

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert unexpected_hooks == []
        assert await restarted._journal_dispatcher.store.pending() == ()

    @pytest.mark.ledger_loads_from_disk
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
        await bot._turn_store.warm()
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
        await bot._turn_store.record_pending_turn(pending_turn)
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("🛑", "$stop-reaction")

        with (
            patch.object(
                bot._user_stop_reconciler,
                "finalize",
                new=AsyncMock(side_effect=RuntimeError("crash after stop claim")),
            ),
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)
        pending = await bot._journal_dispatcher.store.pending()
        stop_receipt_order = pending[0].receipt_order
        await bot._turn_store.record_turn(
            with_user_stop(
                pending_turn,
                "$response",
                stop_receipt_order,
                delivery_settled=True,
            ),
        )

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        await restarted._turn_store.warm()
        restarted.client = make_matrix_client_mock()
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._journal_dispatcher.drain_once()

        finalize_stopped_response.assert_not_awaited()
        stopped_record = restarted._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.user_stop_receipt_order is not None
        assert stopped_record.user_stop_settled_receipt_order == stopped_record.user_stop_receipt_order
        assert await restarted._journal_dispatcher.store.pending() == ()

    @pytest.mark.ledger_loads_from_disk
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
        await bot._turn_store.warm()
        bot.client = make_matrix_client_mock()
        target = MessageTarget.resolve("!test:localhost", None, "$source")
        await bot._turn_store.record_pending_turn(
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
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)

        pending = await bot._journal_dispatcher.store.pending()
        stop_receipt_order = pending[0].receipt_order
        stopped_record = bot._turn_store.get_turn_record("$source")
        assert stopped_record is not None
        assert stopped_record.completed is True
        assert stopped_record.user_stop_receipt_order == stop_receipt_order
        assert stopped_record.user_stop_settled_receipt_order is None
        assert await bot._turn_store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$source",),
            terminal_source_event_ids=("$source",),
        )
        assert len(pending) == 1
        assert pending[0].semantic_consumer is SemanticConsumer.STOP_REACTION

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        await restarted._turn_store.warm()
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.delivery_gateway.DeliveryGateway.finalize_user_stopped_response",
            new=AsyncMock(return_value=True),
        ) as finalize_stopped_response:
            await restarted._journal_dispatcher.drain_once()

        finalize_stopped_response.assert_awaited_once_with(target, "$response")
        assert unexpected_hooks == []
        finalized_record = restarted._turn_store.get_turn_record("$source")
        assert finalized_record is not None
        assert finalized_record.user_stop_settled_receipt_order == stop_receipt_order
        assert await restarted._journal_dispatcher.store.pending() == ()

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
        await bot._turn_store.record_pending_turn(
            TurnRecord.create(
                ["$source"],
                response_event_id="$response",
                completed=False,
                response_owner=bot.agent_name,
                requester_id="@user:localhost",
                conversation_target=target,
            ),
        )
        assert not await bot._turn_store.prepare_edit_response_source(
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
                unwrap_extracted_collaborator(bot._journal_dispatcher),
                "receipt_order",
                new=AsyncMock(return_value=2),
            ),
            patch.object(turn_store, "get_turn_record", side_effect=tracked_lookup) as source_lookup,
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            replay_task = asyncio.create_task(bot._journal_dispatcher.drain_once())
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
        await bot._turn_store.record_pending_turn(
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
                await turn_store.record_turn(
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
        ):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)
        assert (await bot._journal_dispatcher.store.pending())[
            0
        ].semantic_consumer is SemanticConsumer.CONFIG_CONFIRMATION

        restarted = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        with patch(
            "mindroom.bot.config_confirmation.resolve_reaction_pending_change",
            new=AsyncMock(return_value=None),
        ) as resolve_pending:
            await restarted._journal_dispatcher.drain_once()

        resolve_pending.assert_awaited_once()
        assert unexpected_hooks == []
        assert await restarted._journal_dispatcher.store.pending() == ()

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
        event = _reaction_event("👍", "$interactive-reaction", timestamp=2_000)
        selection = interactive.InteractiveSelection(
            question_event_id="$response",
            question_text="Choose",
            selection_key="👍",
            selected_label="Chosen",
            selected_value="chosen",
            thread_id=None,
        )
        failure = RuntimeError("crash after interactive reaction claim")
        replace_interactive_selection_handlers(
            bot,
            handle=AsyncMock(side_effect=failure),
        )
        store = bot._journal_store.principal(bot._journal_principal_id)
        admission = await activate_interactive_prompt(
            store,
            question_event_id=selection.question_event_id,
            room_id=room.room_id,
            sender=bot.matrix_id.full_id,
            creator_agent=bot.agent_name,
            thread_id=selection.thread_id,
            question_text=selection.question_text,
            options={selection.selection_key: selection.selected_value},
            option_labels={selection.selection_key: selection.selected_label},
        )
        assert admission is AdmissionResult.ADMITTED

        await bot._journal_dispatcher.admit_out_of_band(
            room,
            event,
            EventKind.REACTION,
            EventClass.ACTIONABLE,
        )
        await bot._journal_dispatcher.drain_once()
        await _cancel_dispatch_retry(bot)
        assert (await bot._journal_dispatcher.store.pending())[
            0
        ].semantic_consumer is SemanticConsumer.INTERACTIVE_REACTION

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()
        unexpected_hooks = _install_reaction_recorder(restarted)
        replace_interactive_selection_handlers(restarted, handle=AsyncMock(return_value=False))
        await restarted._journal_dispatcher.drain_once()
        await restarted._response_runner.drain_inbox_responses()

        assert unexpected_hooks == []
        assert await restarted._journal_dispatcher.store.pending() == ()

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
        with _mock_interactive_claim(bot, None):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            # Cancellation propagates out of the worker: a cancelled turn is
            # not a failed one, so the event stays pending untouched.
            with pytest.raises(asyncio.CancelledError):
                await bot._journal_dispatcher.drain_once()
        pending = await bot._journal_dispatcher.store.pending()
        assert pending[0].semantic_consumer is SemanticConsumer.REACTION_HOOKS

        restarted = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        restarted.client = make_matrix_client_mock()

        @hook(EVENT_REACTION_RECEIVED)
        async def emit_replay(ctx: ReactionReceivedContext) -> None:
            emissions.append(ctx.event_id)

        restarted.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [emit_replay])])
        with _mock_interactive_claim(restarted, None) as interactive_handler:
            await restarted._journal_dispatcher.drain_once()

        assert emissions == [event.event_id, event.event_id]
        interactive_handler.assert_not_awaited()
        assert await restarted._journal_dispatcher.store.pending() == ()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("enforce_turn_authorization")
    async def test_claimed_reaction_hook_replay_rechecks_reply_authorization(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A claimed hook must settle without running after its sender is revoked."""
        sender_id = "@user:localhost"
        config = self._config_for_storage(tmp_path)
        config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                mock_agent_user.agent_name: AgentReplyPermission(users=[sender_id]),
            },
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = make_matrix_client_mock()
        room = nio.MatrixRoom("!test:localhost", bot.matrix_id.full_id)
        event = _reaction_event("👍", "$claimed-hook-reaction")

        @hook(EVENT_REACTION_RECEIVED)
        async def crash_after_claim(_ctx: ReactionReceivedContext) -> None:
            message = "crash after hook claim"
            raise asyncio.CancelledError(message)

        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [crash_after_claim])])
        with _mock_interactive_claim(bot, None):
            await bot._journal_dispatcher.admit_out_of_band(
                room,
                event,
                EventKind.REACTION,
                EventClass.ACTIONABLE,
            )
            with pytest.raises(asyncio.CancelledError):
                await bot._journal_dispatcher.drain_once()
        pending = await bot._journal_dispatcher.store.pending()
        assert pending[0].semantic_consumer is SemanticConsumer.REACTION_HOOKS

        denied_config = config.model_copy(deep=True)
        denied_config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                mock_agent_user.agent_name: AgentReplyPermission(users=[]),
            },
        )
        restarted = AgentBot(
            mock_agent_user,
            tmp_path,
            config=denied_config,
            runtime_paths=runtime_paths,
        )
        restarted.client = make_matrix_client_mock()
        replays: list[str] = []

        @hook(EVENT_REACTION_RECEIVED)
        async def record_replay(ctx: ReactionReceivedContext) -> None:
            replays.append(ctx.event_id)

        restarted.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [record_replay])])
        await restarted._journal_dispatcher.drain_once()

        assert replays == []
        assert await restarted._journal_dispatcher.store.pending() == ()

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
        event.server_timestamp = 1234567890
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

        install_relation_lookup(bot, threads={"$thread-reply": "$thread-root"})
        with _mock_interactive_claim(bot, None):
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
            _mock_interactive_claim(bot, None),
        ):
            await _dispatch_reaction(bot, room, event)

        resolve_related_event_thread_id.assert_awaited_once_with(
            room.room_id,
            "$plain-reply",
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

        with _mock_interactive_claim(bot, None):
            await _dispatch_reaction(bot, room, event)

        assert seen == [("$plain-reply-2", "$thread-root")]
