"""Live-path (no restart) convergence pins for user-stop reconciliation.

One 🛑 reaction on an in-flight response must produce exactly one coherent
outcome on both sides: the visible Matrix response ends as the committed
cancellation note, and the durable TurnStore record for the source turn lands
terminal with the same response event id and the stop's receipt order.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import STREAM_STATUS_CANCELLED, STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.streaming import _CANCELLED_RESPONSE_NOTE
from tests.bot_helpers import (
    AgentBotTestBase,
    _install_runtime_cache_support,
    _room_send_response,
    dispatch_reaction_durably,
    make_mock_agent_user,
)
from tests.conftest import (
    drain_coalescing,
    make_matrix_client_mock,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.matrix.users import AgentMatrixUser

_ROOM_ID = "!test:localhost"
_USER_ID = "@user:localhost"
_THREAD_ROOT = "$thread-root"
_SOURCE_EVENT_ID = "$source"
_RESPONSE_EVENT_ID = "$response"
_PLACEHOLDER_BODY = "Thinking..."
_FINAL_STREAM_BODY = "Streaming final answer"
_DISPATCH_TIMEOUT_SECONDS = 10.0


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


def _thread_message_event(mention_id: str) -> nio.RoomMessageText:
    """Build one real thread text event that mentions the bot."""
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": _SOURCE_EVENT_ID,
            "sender": _USER_ID,
            "origin_server_ts": 1234567890,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": f"{mention_id}: please compute",
                "m.mentions": {"user_ids": [mention_id]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT},
            },
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    event.decrypted = False
    return event


def _stop_reaction_event(target_event_id: str = _RESPONSE_EVENT_ID) -> nio.ReactionEvent:
    """Build one real 🛑 reaction event aimed at the visible response."""
    event = nio.Event.parse_event(
        {
            "type": "m.reaction",
            "event_id": "$stop-reaction",
            "sender": _USER_ID,
            "origin_server_ts": 1,
            "room_id": _ROOM_ID,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target_event_id,
                    "key": "🛑",
                },
            },
        },
    )
    assert isinstance(event, nio.ReactionEvent)
    return event


@dataclass(frozen=True)
class _VisibleSend:
    """One captured client.room_send payload."""

    message_type: str
    content: dict[str, Any]

    @property
    def edit_target_event_id(self) -> str | None:
        relates_to = self.content.get("m.relates_to")
        if not isinstance(relates_to, dict) or relates_to.get("rel_type") != "m.replace":
            return None
        target = relates_to.get("event_id")
        return target if isinstance(target, str) else None


class _VisibleSendRecorder:
    """Capture every room_send payload while vending deterministic event ids."""

    def __init__(
        self,
        *,
        placeholder_started: asyncio.Event | None = None,
        release_placeholder: asyncio.Event | None = None,
    ) -> None:
        self.sends: list[_VisibleSend] = []
        self._placeholder_sent = False
        self._placeholder_started = placeholder_started
        self._release_placeholder = release_placeholder

    async def __call__(
        self,
        *,
        room_id: str,  # noqa: ARG002
        message_type: str,
        content: dict[str, Any],
        ignore_unverified_devices: bool,  # noqa: ARG002
    ) -> MagicMock:
        self.sends.append(_VisibleSend(message_type=message_type, content=content))
        if not self._placeholder_sent and message_type == "m.room.message" and content.get("body") == _PLACEHOLDER_BODY:
            self._placeholder_sent = True
            if self._placeholder_started is not None:
                self._placeholder_started.set()
            if self._release_placeholder is not None:
                await self._release_placeholder.wait()
            return _room_send_response(_RESPONSE_EVENT_ID)
        return _room_send_response(f"$send-{len(self.sends)}")

    def edits_targeting(self, event_id: str) -> list[dict[str, Any]]:
        """Return the full edit payloads aimed at one event, in send order."""
        return [send.content for send in self.sends if send.edit_target_event_id == event_id]

    def edit_bodies_targeting(self, event_id: str) -> list[Any]:
        """Return the m.new_content bodies of every edit aimed at one event."""
        bodies: list[Any] = []
        for edit_content in self.edits_targeting(event_id):
            new_content = edit_content["m.new_content"]
            assert isinstance(new_content, dict)
            bodies.append(new_content["body"])
        return bodies

    def non_edit_message_sends(self) -> list[_VisibleSend]:
        """Return every m.room.message send that is a fresh message, not an edit."""
        return [
            send for send in self.sends if send.message_type == "m.room.message" and send.edit_target_event_id is None
        ]


@contextmanager
def _streaming_response_patches(
    bot: AgentBot,
    stream: Callable[..., AsyncGenerator[str, None]],
) -> Iterator[None]:
    """Run the real response lifecycle against a scripted streaming model source."""
    with (
        patch("mindroom.response_runner.stream_agent_response", new=stream),
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.conversation_resolver.ConversationResolver.fetch_thread_history",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=True)),
        ),
        patch(
            "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value=_THREAD_ROOT),
        ),
        patch.object(
            bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=ThreadHistoryResult([], is_full_history=False)),
        ),
        patch.object(
            bot._conversation_cache,
            "get_dispatch_thread_history",
            new=AsyncMock(return_value=ThreadHistoryResult([], is_full_history=True)),
        ),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new=AsyncMock(return_value=False)),
    ):
        yield


def _spy_on_stop_finalize(
    monkeypatch: pytest.MonkeyPatch,
    bot: AgentBot,
) -> list[tuple[str, int]]:
    """Record UserStopReconciler.finalize calls while keeping the real implementation."""
    finalize_calls: list[tuple[str, int]] = []
    original_finalize = bot._user_stop_reconciler.finalize

    async def spy_finalize(
        response_event_id: str,
        stop_receipt_order: int,
        on_current_stop_finalized: Callable[[], Awaitable[None]],
    ) -> bool:
        finalize_calls.append((response_event_id, stop_receipt_order))
        return await original_finalize(
            response_event_id,
            stop_receipt_order,
            on_current_stop_finalized,
        )

    monkeypatch.setattr(bot._user_stop_reconciler, "finalize", spy_finalize)
    return finalize_calls


async def _cancel_stop_manager_cleanup(bot: AgentBot) -> None:
    """Cancel delayed stop-manager cleanup tasks instead of outliving their delay."""
    for cleanup_task in list(bot.stop_manager.cleanup_tasks):
        cleanup_task.cancel()
    if bot.stop_manager.cleanup_tasks:
        await asyncio.gather(*bot.stop_manager.cleanup_tasks, return_exceptions=True)


class TestUserStopConvergence(AgentBotTestBase):
    """Live-path pins: one stop converges visible cancellation and durable terminal truth."""

    def _make_streaming_bot(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> AgentBot:
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(
            mock_agent_user,
            tmp_path,
            rooms=[_ROOM_ID],
            enable_streaming=True,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        _install_runtime_cache_support(bot)
        bot.client = make_matrix_client_mock(user_id=mock_agent_user.user_id)
        return bot

    @staticmethod
    def _room(own_user_id: str) -> nio.MatrixRoom:
        room = nio.MatrixRoom(_ROOM_ID, own_user_id)
        room.add_member(_USER_ID, None, None)
        return room

    @pytest.mark.asyncio
    async def test_live_stop_during_streaming_response_converges_visible_and_durable(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mid-flight stop lands one cancelled-note edit and one matching terminal record."""
        bot = self._make_streaming_bot(mock_agent_user, tmp_path)
        sends = _VisibleSendRecorder()
        bot.client.room_send = sends
        room = self._room(mock_agent_user.user_id)
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()
        stream_calls = 0

        async def gated_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            nonlocal stream_calls
            stream_calls += 1
            generation_started.set()
            await release_generation.wait()
            yield _FINAL_STREAM_BODY

        finalize_calls = _spy_on_stop_finalize(monkeypatch, bot)

        with _streaming_response_patches(bot, gated_stream):
            message_task = asyncio.create_task(bot._on_message(room, _thread_message_event(mock_agent_user.user_id)))
            try:
                async with asyncio.timeout(_DISPATCH_TIMEOUT_SECONDS):
                    await generation_started.wait()
                await asyncio.wait_for(
                    dispatch_reaction_durably(bot, room, _stop_reaction_event()),
                    timeout=_DISPATCH_TIMEOUT_SECONDS,
                )
            finally:
                release_generation.set()
            await asyncio.wait_for(message_task, timeout=_DISPATCH_TIMEOUT_SECONDS)
            await drain_coalescing(bot)

        # Visible side: the placeholder event ends as the committed cancellation
        # note, with no duplicate final message and no later completed body.
        edits = sends.edits_targeting(_RESPONSE_EVENT_ID)
        assert sends.edit_bodies_targeting(_RESPONSE_EVENT_ID) == [_CANCELLED_RESPONSE_NOTE]
        assert edits[-1].get(STREAM_STATUS_KEY) == STREAM_STATUS_CANCELLED
        non_edit_messages = sends.non_edit_message_sends()
        assert len(non_edit_messages) == 1
        assert non_edit_messages[0].content.get("body") == _PLACEHOLDER_BODY
        assert all(_FINAL_STREAM_BODY not in str(send.content) for send in sends.sends)

        # Exactly one stop finalize reached the reconciler.
        assert stream_calls == 1
        assert len(finalize_calls) == 1
        stopped_event_id, stop_receipt_order = finalize_calls[0]
        assert stopped_event_id == _RESPONSE_EVENT_ID

        # Durable side: the source turn is terminal and agrees with the visible event.
        record = bot._turn_store.get_turn_record(_SOURCE_EVENT_ID)
        assert record is not None
        assert record.completed is True
        assert record.response_event_id == _RESPONSE_EVENT_ID
        assert record.user_stop_receipt_order == stop_receipt_order
        assert record.user_stop_settled_receipt_order == stop_receipt_order
        assert bot._turn_store.is_durably_handled(_SOURCE_EVENT_ID) is True

        # The stop reaction's dispatch obligation is tombstoned once terminal truth lands.
        assert bot._dispatch_obligation_store.pending() == ()

        await _cancel_stop_manager_cleanup(bot)

    @pytest.mark.asyncio
    async def test_live_stop_before_placeholder_leaves_one_coherent_completed_outcome(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stop landing before any visible response is a no-op, not a stray outcome."""
        bot = self._make_streaming_bot(mock_agent_user, tmp_path)
        placeholder_started = asyncio.Event()
        release_placeholder = asyncio.Event()
        sends = _VisibleSendRecorder(
            placeholder_started=placeholder_started,
            release_placeholder=release_placeholder,
        )
        bot.client.room_send = sends
        room = self._room(mock_agent_user.user_id)

        async def quick_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield _FINAL_STREAM_BODY

        finalize_calls = _spy_on_stop_finalize(monkeypatch, bot)

        with (
            _streaming_response_patches(bot, quick_stream),
            patch("mindroom.interactive.handle_reaction", new=AsyncMock(return_value=None)),
        ):
            message_task = asyncio.create_task(bot._on_message(room, _thread_message_event(mock_agent_user.user_id)))
            try:
                async with asyncio.timeout(_DISPATCH_TIMEOUT_SECONDS):
                    await placeholder_started.wait()
                # The placeholder send is still blocked: nothing visible or
                # tracked exists for the stop reaction to claim.
                assert sends.edit_bodies_targeting(_RESPONSE_EVENT_ID) == []
                await asyncio.wait_for(
                    dispatch_reaction_durably(bot, room, _stop_reaction_event()),
                    timeout=_DISPATCH_TIMEOUT_SECONDS,
                )
            finally:
                release_placeholder.set()
            await asyncio.wait_for(message_task, timeout=_DISPATCH_TIMEOUT_SECONDS)
            await drain_coalescing(bot)

        # The stop never reached the reconciler and no cancellation note appeared.
        assert finalize_calls == []
        assert all(_CANCELLED_RESPONSE_NOTE not in str(send.content) for send in sends.sends)

        # The response then completes exactly once: one placeholder plus only
        # completed-body edits to the same event, nothing stray.
        assert len(sends.non_edit_message_sends()) == 1
        edits = sends.edits_targeting(_RESPONSE_EVENT_ID)
        assert len(edits) >= 1
        assert sends.edit_bodies_targeting(_RESPONSE_EVENT_ID) == [_FINAL_STREAM_BODY] * len(edits)
        assert edits[-1].get(STREAM_STATUS_KEY) == STREAM_STATUS_COMPLETED

        record = bot._turn_store.get_turn_record(_SOURCE_EVENT_ID)
        assert record is not None
        assert record.completed is True
        assert record.response_event_id == _RESPONSE_EVENT_ID
        assert record.user_stop_receipt_order is None
        assert record.user_stop_settled_receipt_order is None

        assert bot._dispatch_obligation_store.pending() == ()

        await _cancel_stop_manager_cleanup(bot)
