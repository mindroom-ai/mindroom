"""Tests for message coalescing — skip older messages when newer unresponded ones exist."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, _MessageContext, _PreparedTextEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="TestAgent", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )


def _make_bot(tmp_path: Path) -> AgentBot:
    config = _make_config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        password=TEST_PASSWORD,
        display_name="TestAgent",
        user_id="@mindroom_test_agent:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = AsyncMock(spec=nio.AsyncClient)
    return bot


def _make_event(
    *,
    sender: str = "@user:localhost",
    event_id: str = "$evt1",
    server_timestamp: int = 1000,
) -> MagicMock:
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = sender
    event.event_id = event_id
    event.server_timestamp = server_timestamp
    event.body = "hello"
    event.source = {"content": {"body": "hello"}}
    return event


def _make_context(thread_history: list[dict] | None = None) -> _MessageContext:
    return _MessageContext(
        am_i_mentioned=True,
        is_thread=bool(thread_history),
        thread_id="$thread_root" if thread_history else None,
        thread_history=thread_history or [],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )


class TestHasNewerUnrespondedInScope:
    """Tests for AgentBot._has_newer_unresponded_in_scope."""

    def test_skip_older_when_newer_unresponded_exists(self, tmp_path: Path) -> None:
        """Older message should be skipped when a newer unresponded message exists from same sender."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is True

    def test_latest_message_not_skipped(self, tmp_path: Path) -> None:
        """The newest message should never be skipped."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m2", server_timestamp=2000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_different_senders_never_coalesced(self, tmp_path: Path) -> None:
        """Messages from different senders should never cause each other to skip."""
        bot = _make_bot(tmp_path)
        event = _make_event(sender="@alice:localhost", event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@alice:localhost", "event_id": "$m1", "timestamp": 1000, "body": "alice msg"},
                {"sender": "@bob:localhost", "event_id": "$m2", "timestamp": 2000, "body": "bob msg"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_already_responded_newer_message_no_skip(self, tmp_path: Path) -> None:
        """If the newer message was already responded to, the older one should proceed."""
        bot = _make_bot(tmp_path)
        bot.response_tracker.mark_responded("$m2", "$resp2")
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_empty_thread_history_no_skip(self, tmp_path: Path) -> None:
        """Empty thread_history (room mode) should never skip."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context([])
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_single_message_not_skipped(self, tmp_path: Path) -> None:
        """A single message on restart should process normally."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "only msg"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_multiple_messages_only_latest_not_skipped(self, tmp_path: Path) -> None:
        """With 3 messages from same sender, only the latest should not be skipped."""
        bot = _make_bot(tmp_path)
        history = [
            {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
            {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            {"sender": "@user:localhost", "event_id": "$m3", "timestamp": 3000, "body": "third"},
        ]
        context = _make_context(history)

        e1 = _make_event(event_id="$m1", server_timestamp=1000)
        assert bot._has_newer_unresponded_in_scope(e1, context) is True

        e2 = _make_event(event_id="$m2", server_timestamp=2000)
        assert bot._has_newer_unresponded_in_scope(e2, context) is True

        e3 = _make_event(event_id="$m3", server_timestamp=3000)
        assert bot._has_newer_unresponded_in_scope(e3, context) is False

    def test_room_mode_no_thread_returns_false(self, tmp_path: Path) -> None:
        """Room mode (no thread_id, empty thread_history) returns False."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_newer_command_does_not_coalesce(self, tmp_path: Path) -> None:
        """If the newer message is a command (!help), the older message should NOT be coalesced."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "question"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "!help"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_newer_command_with_whitespace_does_not_coalesce(self, tmp_path: Path) -> None:
        """Commands with leading whitespace should still be excluded from coalescing."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "question"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "  !schedule 5m"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_scheduled_event_not_coalesced(self, tmp_path: Path) -> None:
        """Scheduled automation should bypass coalescing even with newer bot activity."""
        bot = _make_bot(tmp_path)
        event = _make_event(sender="@mindroom_router:localhost", event_id="$m1", server_timestamp=1000)
        event.source["content"]["com.mindroom.source_kind"] = "scheduled"
        context = _make_context(
            [
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m1",
                    "timestamp": 1000,
                    "body": "scheduled task fire",
                },
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m2",
                    "timestamp": 2000,
                    "body": "newer bot activity",
                },
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_hook_event_not_coalesced(self, tmp_path: Path) -> None:
        """Hook-originated automation should bypass coalescing even with newer bot activity."""
        bot = _make_bot(tmp_path)
        event = _make_event(sender="@mindroom_router:localhost", event_id="$m1", server_timestamp=1000)
        event.source["content"]["com.mindroom.source_kind"] = "hook"
        context = _make_context(
            [
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m1",
                    "timestamp": 1000,
                    "body": "hook fire",
                },
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m2",
                    "timestamp": 2000,
                    "body": "newer bot activity",
                },
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_multiple_scheduled_fires_not_coalesced(self, tmp_path: Path) -> None:
        """Repeated scheduled fires in one thread should all dispatch independently."""
        bot = _make_bot(tmp_path)
        context = _make_context(
            [
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m1",
                    "timestamp": 1000,
                    "body": "scheduled fire one",
                },
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m2",
                    "timestamp": 2000,
                    "body": "scheduled fire two",
                },
            ],
        )

        first_event = _make_event(sender="@mindroom_router:localhost", event_id="$m1", server_timestamp=1000)
        first_event.source["content"]["com.mindroom.source_kind"] = "scheduled"
        second_event = _make_event(sender="@mindroom_router:localhost", event_id="$m2", server_timestamp=2000)
        second_event.source["content"]["com.mindroom.source_kind"] = "scheduled"

        assert bot._has_newer_unresponded_in_scope(first_event, context) is False
        assert bot._has_newer_unresponded_in_scope(second_event, context) is False

    def test_command_older_normal_newer_still_coalesces(self, tmp_path: Path) -> None:
        """A command followed by a normal message: the command should still be coalesced by the normal one."""
        bot = _make_bot(tmp_path)
        # Older message is normal text, newer is also normal → coalesce
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first question"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "!help"},
                {"sender": "@user:localhost", "event_id": "$m3", "timestamp": 3000, "body": "second question"},
            ],
        )
        # $m1 should be coalesced because $m3 is a newer normal message
        assert bot._has_newer_unresponded_in_scope(event, context) is True

    def test_normal_message_still_coalesces_regression(self, tmp_path: Path) -> None:
        """Human messages without an exempt source kind should still coalesce."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1", server_timestamp=1000)
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is True

    def test_bot_relay_without_source_kind_still_coalesces(self, tmp_path: Path) -> None:
        """Relayed bot-authored messages still coalesce without an exempt source kind."""
        bot = _make_bot(tmp_path)
        event = _make_event(sender="@mindroom_router:localhost", event_id="$m1", server_timestamp=1000)
        event.source["content"][ORIGINAL_SENDER_KEY] = "@user:localhost"
        context = _make_context(
            [
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m1",
                    "timestamp": 1000,
                    "body": "relay one",
                },
                {
                    "sender": "@mindroom_router:localhost",
                    "event_id": "$m2",
                    "timestamp": 2000,
                    "body": "relay two",
                },
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is True

    def test_synthetic_event_no_server_timestamp(self, tmp_path: Path) -> None:
        """Events without server_timestamp (e.g. prepared voice text events) should not skip."""
        bot = _make_bot(tmp_path)

        class _SyntheticEventWithoutTimestampAccess(_PreparedTextEvent):
            def __getattribute__(self, name: str) -> object:
                if name == "server_timestamp":
                    msg = "Synthetic events should not access server_timestamp."
                    raise AssertionError(msg)
                return super().__getattribute__(name)

        event = _SyntheticEventWithoutTimestampAccess(
            sender="@user:localhost",
            event_id="$m1",
            body="hello",
            source={"content": {"body": "hello"}},
            is_synthetic=True,
        )
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False

    def test_non_numeric_server_timestamp_no_skip(self, tmp_path: Path) -> None:
        """Events with non-numeric timestamps should degrade to the pre-coalescing behavior."""
        bot = _make_bot(tmp_path)
        event = _make_event(event_id="$m1")
        event.server_timestamp = MagicMock()
        context = _make_context(
            [
                {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
                {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
            ],
        )
        assert bot._has_newer_unresponded_in_scope(event, context) is False


class TestCoalescingInDispatch:
    """Integration tests verifying coalescing is wired into _dispatch_text_message."""

    @pytest.mark.asyncio
    async def test_dispatch_skips_older_message(self, tmp_path: Path) -> None:
        """_dispatch_text_message should mark_responded and return early for older messages."""
        bot = _make_bot(tmp_path)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = _make_event(event_id="$m1", server_timestamp=1000)

        thread_history = [
            {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
            {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
        ]
        context = _make_context(thread_history)
        dispatch = MagicMock()
        dispatch.context = context

        with (
            patch.object(bot, "_extract_message_context", return_value=context),
            patch.object(bot, "_emit_message_received_hooks", return_value=False),
            patch.object(bot, "_prepare_dispatch", return_value=dispatch),
            patch.object(bot, "_resolve_dispatch_action") as mock_resolve,
        ):
            await bot._dispatch_text_message(room, event, "@user:localhost")

        # Should have marked the older event as responded
        assert bot.response_tracker.has_responded("$m1")
        # Should NOT have called _resolve_dispatch_action (skipped before reaching it)
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_proceeds_for_latest_message(self, tmp_path: Path) -> None:
        """_dispatch_text_message should proceed normally for the latest message."""
        bot = _make_bot(tmp_path)
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        event = _make_event(event_id="$m2", server_timestamp=2000)

        thread_history = [
            {"sender": "@user:localhost", "event_id": "$m1", "timestamp": 1000, "body": "first"},
            {"sender": "@user:localhost", "event_id": "$m2", "timestamp": 2000, "body": "second"},
        ]
        context = _make_context(thread_history)
        dispatch = MagicMock()
        dispatch.context = context
        dispatch.requester_user_id = "@user:localhost"

        action = MagicMock()

        with (
            patch.object(bot, "_extract_message_context", return_value=context),
            patch.object(bot, "_emit_message_received_hooks", return_value=False),
            patch.object(bot, "_prepare_dispatch", return_value=dispatch),
            patch.object(bot, "_resolve_dispatch_action", return_value=action),
            patch.object(bot, "_build_dispatch_payload_with_attachments", return_value=MagicMock()),
            patch.object(bot, "_execute_dispatch_action") as mock_execute,
        ):
            await bot._dispatch_text_message(room, event, "@user:localhost")

        mock_execute.assert_called_once()
