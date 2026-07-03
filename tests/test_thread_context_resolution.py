"""Conversation thread context resolution: extract_context and dispatch-context thread inheritance, demotion, and proofs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest

import mindroom.matrix.message_content as message_content_module
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_reads import ThreadReadMode
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    is_thread_history_degraded,
)
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    resolve_related_event_thread_id_best_effort,
    thread_messages_thread_membership_access,
)
from mindroom.response_runner import ResponseRequest
from mindroom.turn_policy import _DispatchPlan
from tests.conftest import (
    request_envelope,
    unwrap_extracted_collaborator,
)
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _matrix_room,
    _message,
    thread_history_result,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


def test_plain_reply_event_info_has_no_thread_routing_root() -> None:
    """Plain replies should not populate any synthetic routing root."""
    event_info = EventInfo.from_event(
        {
            "content": {
                "body": "plain reply",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$target:localhost"}},
            },
            "event_id": "$reply:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": "!test:localhost",
            "type": "m.room.message",
        },
    )

    assert event_info.is_reply is True
    assert event_info.reply_to_event_id == "$target:localhost"
    assert event_info.relates_to_event_id is None


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_extract_context_edit_uses_thread_from_new_content(self, bot: AgentBot) -> None:
        """Edit events should resolve thread context from m.new_content thread relation."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567894,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Original"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_resolves_thread_from_original_event(self, bot: AgentBot) -> None:
        """Edits without nested thread metadata should still resolve to the edited message thread."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_msg:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567895,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Thread message",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$thread_msg:localhost",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567893,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Thread message"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_msg:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_plain_root_message_stays_room_level(self, bot: AgentBot) -> None:
        """Edits of plain room-root messages should not be promoted into thread context."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_message:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_message:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(
                return_value=thread_history_result(
                    [
                        _message(event_id="$room_message:localhost", body="Room message"),
                    ],
                    is_full_history=True,
                ),
            ),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$room_message:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to explicit thread messages should stay in that thread."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "follow-up from bridge",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root"),
            _message(event_id="$thread_msg:localhost", body="Thread message"),
        ]
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_lookup.assert_awaited_once_with(room.room_id, "$thread_msg:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_thread_root_inherits_existing_thread(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to the explicit thread root should stay in that thread."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "follow-up from bridge",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message"),
            _message(event_id="$thread_reply:localhost", body="Thread reply"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_fetch.assert_awaited_once_with(room.room_id, "$thread_root:localhost", caller_label="message_context")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_chain_stays_threaded_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain reply chain should stay threaded when it eventually reaches a threaded ancestor."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "second bridge reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain_reply_1:localhost"}},
                },
                "event_id": "$plain_reply_2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "first bridge reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain_reply_1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567896,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$thread_root:localhost",
                            },
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(
                    return_value=thread_history_result(
                        [
                            _message(event_id="$thread_root:localhost", body="Root message"),
                            _message(event_id="$thread_msg:localhost", body="Thread reply"),
                            _message(event_id="$plain_reply_1:localhost", body="first bridge reply"),
                        ],
                        is_full_history=True,
                    ),
                ),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [message.event_id for message in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain_reply_1:localhost",
        ]
        assert mock_lookup.await_args_list == [
            call(room.room_id, "$plain_reply_1:localhost"),
            call(room.room_id, "$thread_msg:localhost"),
        ]
        assert bot.client.room_get_event.await_args_list == [
            call(room.room_id, "$plain_reply_1:localhost"),
            call(room.room_id, "$thread_msg:localhost"),
        ]
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_promoted_plain_reply_stays_threaded(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain reply should inherit thread membership transitively through a promoted plain reply."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "second bridge reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain_reply_1:localhost"}},
                },
                "event_id": "$plain_reply_2:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "first bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                    },
                    "event_id": "$plain_reply_1:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(
                    return_value=thread_history_result(
                        [_message(event_id="$thread_root:localhost", body="root")],
                        is_full_history=True,
                    ),
                ),
            ) as mock_fetch,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == [_message(event_id="$thread_root:localhost", body="root")]
        mock_lookup.assert_awaited_once_with(room.room_id, "$plain_reply_1:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")
        bot.client.room_get_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_thread_root_uses_cached_root_mapping(self, bot: AgentBot) -> None:
        """Edits of a thread root should stay threaded once any child reply taught the cache that thread."""
        room = _matrix_room(name="Test Room")

        real_event_cache = SqliteEventCache(bot.storage_path / "root-edit-thread-cache.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache

        reply_event_source = {
            "content": {
                "body": "Reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
            },
            "event_id": "$reply:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567896,
            "room_id": "!test:localhost",
            "type": "m.room.message",
        }
        try:
            await bot.event_cache.store_events_batch(
                [("$reply:localhost", room.room_id, reply_event_source)],
            )

            event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* updated root",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "updated root",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root:localhost"},
                    },
                    "event_id": "$edit_event:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567897,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            )

            bot.client.room_get_event = AsyncMock(
                return_value=nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Root message",
                            "msgtype": "m.text",
                        },
                        "event_id": "$thread_root:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            )

            expected_history = [
                _message(event_id="$thread_root:localhost", body="Root message"),
                _message(event_id="$reply:localhost", body="Reply"),
            ]
            with patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
            ) as mock_fetch:
                context = await bot._conversation_resolver.extract_message_context(room, event)

            assert context.is_thread is True
            assert context.thread_id == "$thread_root:localhost"
            assert context.thread_history == expected_history
            bot.client.room_get_event.assert_not_awaited()
            mock_fetch.assert_awaited_once()
            assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_thread_root_refetches_when_thread_lookup_cache_is_cold(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of thread roots should stay threaded when authoritative history proves child replies exist."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated root",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated root",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Root message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$thread_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message"),
            _message(event_id="$reply:localhost", body="Reply"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_history:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_history.assert_awaited_once_with(room.room_id, "$thread_root:localhost", caller_label="message_context")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_promoted_plain_reply_refetches_thread_when_lookup_cache_is_cold(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of promoted plain replies should stay threaded without a warmed event-thread mapping."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* edited bridged reply",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "edited bridged reply",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$plain-reply:localhost"},
                },
                "event_id": "$edit-event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Bridged plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
                        },
                        "event_id": "$plain-reply:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": "$thread-root:localhost",
                            },
                        },
                        "event_id": "$thread-reply:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": room.room_id,
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        expected_history = [
            _message(event_id="$thread-root:localhost", body="Root"),
            _message(event_id="$thread-reply:localhost", body="Thread reply"),
            _message(event_id="$plain-reply:localhost", body="Bridged plain reply"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread-root:localhost"
        assert context.thread_history == expected_history
        assert bot.client.room_get_event.await_args_list[0].args == (room.room_id, "$plain-reply:localhost")
        assert bot.client.room_get_event.await_args_list[1].args == (room.room_id, "$thread-reply:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread-root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_plain_root_message_stays_room_level_when_history_has_only_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Root-edit fallback should require child events before treating a message as threaded."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated room message",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated room message",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_root:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room root",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_root:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(return_value=None)

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(
                return_value=ThreadHistoryResult(
                    [_message(event_id="$room_root:localhost", body="Room root")],
                    is_full_history=True,
                ),
            ),
        ) as mock_history:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_root:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$room_root:localhost")
        mock_history.assert_awaited_once()
        assert mock_history.await_args.args == (room.room_id, "$room_root:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_edit_of_plain_root_message_degrades_when_thread_lookup_fails(
        self,
        bot: AgentBot,
    ) -> None:
        """Advisory thread-id lookup failures should not break plain edit context resolution."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$room_message:localhost"},
                },
                "event_id": "$edit_event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567897,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Room message",
                        "msgtype": "m.text",
                    },
                    "event_id": "$room_message:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )
        bot.event_cache.get_thread_id_for_event = AsyncMock(side_effect=RuntimeError("sqlite boom"))

        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(
                return_value=thread_history_result(
                    [
                        _message(
                            event_id="$room_message:localhost",
                            body="Room message",
                        ),
                    ],
                    is_full_history=True,
                ),
            ),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        bot.event_cache.get_thread_id_for_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$room_message:localhost")

    @pytest.mark.asyncio
    async def test_extract_context_plain_reply_to_threaded_message_stays_threaded_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies should inherit thread context transitively from earlier threaded messages."""
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply from non-thread client",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain2:localhost"}},
                },
                "event_id": "$plain3:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567895,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.room_get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Second plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                        },
                        "event_id": "$plain2:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "First plain reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                        },
                        "event_id": "$plain1:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567893,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "Earlier threaded message",
                            "msgtype": "m.text",
                            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                        },
                        "event_id": "$thread_msg:localhost",
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567892,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Thread root"),
            _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
            _message(event_id="$plain1:localhost", body="First plain reply"),
            _message(event_id="$plain2:localhost", body="Second plain reply"),
        ]
        with patch.object(
            bot._conversation_cache,
            "get_thread_history",
            AsyncMock(return_value=thread_history_result(expected_history, is_full_history=True)),
        ) as mock_fetch:
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args == (room.room_id, "$thread_root:localhost")

    @pytest.mark.asyncio
    async def test_explicit_thread_id_returns_none_for_cyclic_edit_chain(self, bot: AgentBot) -> None:
        """Cyclic edit chains should fail closed instead of raising from the shared resolver."""
        bot._conversation_resolver.deps.conversation_cache.get_thread_id_for_event = AsyncMock(return_value=None)
        bot._conversation_resolver.deps.conversation_cache.get_event = AsyncMock(
            side_effect=[
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "* a",
                            "msgtype": "m.text",
                            "m.new_content": {"body": "a", "msgtype": "m.text"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-b:localhost"},
                        },
                        "event_id": "$edit-a:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 1,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
                nio.RoomGetEventResponse.from_dict(
                    {
                        "content": {
                            "body": "* b",
                            "msgtype": "m.text",
                            "m.new_content": {"body": "b", "msgtype": "m.text"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-a:localhost"},
                        },
                        "event_id": "$edit-b:localhost",
                        "sender": "@user:localhost",
                        "origin_server_ts": 2,
                        "room_id": "!test:localhost",
                        "type": "m.room.message",
                    },
                ),
            ],
        )
        event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "* incoming",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "incoming", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$edit-a:localhost"},
                },
                "event_id": "$incoming-edit:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 3,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        thread_lookup = await bot._conversation_resolver._explicit_thread_id_for_event(
            "!test:localhost",
            "$incoming-edit:localhost",
            event_info,
            mode=ThreadReadMode.ADVISORY_FULL,
            caller_label="threading_error_test",
        )

        assert thread_lookup.thread_id is None

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_plain_reply_inherits_thread_with_bounded_full_history(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch policy context should inherit an existing explicit thread across plain replies."""
        message_content_module._mxc_cache.clear()
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "Newest plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                },
                "event_id": "$incoming:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": "!test:localhost",
                "type": "m.room.message",
            },
        )

        bot.client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(
                    {
                        "msgtype": "m.text",
                        "body": "Hydrated plain reply from sidecar",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                    },
                ).encode("utf-8"),
            ),
        )
        bot.client.room_get_event = AsyncMock()

        dispatch_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$thread_msg:localhost", body="Earlier threaded message"),
                _message(event_id="$plain1:localhost", body="Plain reply"),
            ],
            is_full_history=True,
        )
        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                new=AsyncMock(return_value=dispatch_history),
            ) as mock_history,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(),
            ) as mock_fetch,
        ):
            preview_context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            preview_context = preview_context_result.context

            assert preview_context.is_thread is True
            assert preview_context.thread_id == "$thread_root:localhost"
            assert [message.event_id for message in preview_context.thread_history] == [
                "$thread_root:localhost",
                "$thread_msg:localhost",
                "$plain1:localhost",
            ]
            assert preview_context.requires_model_history_refresh is False
            bot.client.download.assert_not_awaited()
            bot.client.room_get_event.assert_not_awaited()
            mock_lookup.assert_awaited_once_with(room.room_id, "$plain1:localhost")
            mock_history.assert_awaited_once()
            assert mock_history.await_args.args == (room.room_id, "$thread_root:localhost")
            mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_routes_bounded_full_reads_through_single_cache_entrypoint(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch resolution should select the bounded full read through one cache helper."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        dispatch_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$plain1:localhost", body="Plain reply"),
            ],
            is_full_history=True,
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value="$thread_root:localhost"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=dispatch_history),
            ) as mock_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.requires_model_history_refresh is False
        mock_read.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            caller_label="dispatch_context",
        )

    @pytest.mark.asyncio
    async def test_dispatch_room_demotion_clears_source_and_resolved_thread_ids(
        self,
        bot: AgentBot,
    ) -> None:
        """Degraded root proof should demote an indeterminate plain-reply candidate to room-level dispatch."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        root_response = nio.RoomGetEventResponse.from_dict(
            {
                "content": {"body": "root", "msgtype": "m.text"},
                "event_id": "$thread_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567880,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        degraded_history = ThreadHistoryResult(
            [],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "cache_coordinator_timeout",
            },
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(return_value=root_response),
            ) as mock_get_event,
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=degraded_history),
            ) as mock_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$thread_root:localhost"
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id is None
        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context_result.thread_context.thread_history == []
        assert context_result.thread_context.replay_guard_history is degraded_history
        assert context.requires_model_history_refresh is False
        assert context.planning_thread_history == ()
        mock_lookup.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")
        mock_read.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            caller_label="dispatch_context",
        )

    @pytest.mark.asyncio
    async def test_dispatch_candidate_without_proof_history_demotes_without_retry(
        self,
        bot: AgentBot,
    ) -> None:
        """Proof-unavailable candidates without reusable history must demote without repeating the failed read."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to maybe-root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$maybe_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        root_response = nio.RoomGetEventResponse.from_dict(
            {
                "content": {"body": "maybe root", "msgtype": "m.text"},
                "event_id": "$maybe_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567880,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(bot._conversation_cache, "get_event", AsyncMock(return_value=root_response)),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(side_effect=TimeoutError("dispatch read timed out")),
            ) as mock_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$maybe_root:localhost"
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.thread_context.replay_guard_history == []
        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False
        mock_read.assert_awaited_once_with(
            room.room_id,
            "$maybe_root:localhost",
            caller_label="dispatch_context",
        )

    @pytest.mark.asyncio
    async def test_dispatch_related_lookup_failure_keeps_candidate_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Related-event lookup failures should demote while keeping the candidate root for dispatch."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to maybe-root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$maybe_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(bot._conversation_cache, "get_event", AsyncMock(side_effect=RuntimeError("lookup failed"))),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock()) as mock_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$maybe_root:localhost"
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id is None
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.context.is_thread is False
        assert context_result.context.thread_id is None
        mock_read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_related_not_found_keeps_candidate_root(
        self,
        bot: AgentBot,
    ) -> None:
        """M_NOT_FOUND related-event lookups should demote while keeping the candidate root for dispatch."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to missing root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$missing_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(return_value=nio.RoomGetEventError("missing", status_code="M_NOT_FOUND")),
            ),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock()) as mock_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$missing_root:localhost"
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id is None
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.context.is_thread is False
        assert context_result.context.thread_id is None
        mock_read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advisory_context_missing_related_reply_demotes_room_level(
        self,
        bot: AgentBot,
    ) -> None:
        """Advisory context extraction should not fail closed for missing/redacted related events."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to redacted root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$redacted_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(return_value=nio.RoomGetEventError("missing", status_code="M_NOT_FOUND")),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock()) as mock_read,
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False
        mock_read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_new_root_target_does_not_become_existing_thread_context(
        self,
        bot: AgentBot,
    ) -> None:
        """A room-level inbound message may start a delivery thread without existing thread context."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "@mindroom_general start here",
                    "msgtype": "m.text",
                    "m.mentions": {"user_ids": ["@mindroom_general:localhost"]},
                },
                "event_id": "$new_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )

        context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
        context = context_result.context

        assert context_result.thread_context is not None
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id == "$new_root:localhost"
        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False
        assert context.planning_thread_history == ()

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_plain_reply_to_plain_message_stays_room_level_with_empty_history(
        self,
        bot: AgentBot,
    ) -> None:
        """Empty bounded history should not promote plain replies to threads."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        plain_response = nio.RoomGetEventResponse.from_dict(
            {
                "content": {"body": "not a thread root", "msgtype": "m.text"},
                "event_id": "$plain:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567880,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        empty_history = ThreadHistoryResult(
            [],
            is_full_history=True,
            diagnostics={THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_CACHE},
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ),
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(return_value=plain_response),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=empty_history),
            ),
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
            context = context_result.context

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False

    @pytest.mark.asyncio
    async def test_degraded_dispatch_candidate_does_not_call_strict_proof_before_planning(
        self,
        bot: AgentBot,
    ) -> None:
        """Degraded dispatch candidates must be demoted before policy without strict proof."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "follow-up",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        degraded_history = ThreadHistoryResult(
            [],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "cache_coordinator_timeout",
            },
        )
        root_response = nio.RoomGetEventResponse.from_dict(
            {
                "content": {"body": "root", "msgtype": "m.text"},
                "event_id": "$thread_root:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567880,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        observed_targets = []

        async def fake_plan(_room: object, _event: object, dispatch: object, **_kwargs: object) -> _DispatchPlan:
            observed_targets.append(dispatch.target)
            assert dispatch.context.is_thread is False
            assert dispatch.context.thread_id is None
            assert dispatch.context.planning_thread_history == ()
            return _DispatchPlan(kind="ignore")

        bot.event_cache.get_recent_room_events = AsyncMock(return_value=[])
        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(bot._conversation_cache, "get_event", AsyncMock(return_value=root_response)),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=degraded_history),
            ),
            patch.object(
                bot._conversation_cache,
                "get_strict_thread_history",
                AsyncMock(side_effect=AssertionError("dispatch finalization must remain bounded")),
            ) as mock_strict_history,
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock(side_effect=fake_plan)) as mock_plan,
        ):
            await bot._turn_controller._dispatch_text_message(room, event, "@user:localhost")

        mock_strict_history.assert_not_awaited()
        mock_plan.assert_awaited_once()
        assert observed_targets
        assert observed_targets[0].source_thread_id is None
        assert observed_targets[0].resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_degraded_dispatch_history_uses_strict_history_before_policy(
        self,
        bot: AgentBot,
    ) -> None:
        """Degraded proven-thread dispatch history must be refreshed before policy."""
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "thread follow-up",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        degraded_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$reply:localhost", body="Reply"),
            ],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "cache_coordinator_timeout",
            },
        )
        full_history = thread_history_result(list(degraded_history), is_full_history=True)
        resolver = unwrap_extracted_collaborator(bot._conversation_resolver)
        observed_policy_targets = []

        async def fake_plan(_room: object, _event: object, dispatch: object, **_kwargs: object) -> _DispatchPlan:
            observed_policy_targets.append(dispatch.target)
            assert dispatch.context.is_thread is True
            assert dispatch.context.thread_id == "$thread_root:localhost"
            assert dispatch.context.thread_history == full_history
            assert dispatch.context.planning_thread_history == tuple(full_history)
            assert dispatch.context.planning_thread_history_unavailable is False
            assert dispatch.context.requires_model_history_refresh is False
            return _DispatchPlan(kind="ignore")

        with (
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=degraded_history),
            ) as mock_dispatch_history,
            patch.object(
                bot._conversation_cache,
                "get_strict_thread_history",
                AsyncMock(return_value=full_history),
            ) as mock_strict_history,
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock(side_effect=fake_plan)) as mock_plan,
        ):
            await bot._turn_controller._dispatch_text_message(room, event, "@user:localhost")

        mock_dispatch_history.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            caller_label="dispatch_context",
        )
        mock_strict_history.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            caller_label="dispatch_context_strict_thread_fallback",
        )
        mock_plan.assert_awaited_once()
        assert observed_policy_targets[0].resolved_thread_id == "$thread_root:localhost"

        with patch.object(
            resolver,
            "fetch_thread_history",
            AsyncMock(return_value=full_history),
        ) as mock_fetch_thread_history:
            request = await bot._response_runner._refresh_model_history_after_lock(
                ResponseRequest(
                    thread_history=degraded_history,
                    prompt="thread follow-up",
                    response_envelope=request_envelope(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id="$thread_root:localhost",
                        prompt="thread follow-up",
                    ),
                    requires_model_history_refresh=True,
                ),
            )

        mock_fetch_thread_history.assert_awaited_once_with(
            room.room_id,
            "$thread_root:localhost",
            caller_label="dispatch_post_lock_refresh",
        )
        assert request.thread_history == full_history

    def test_thread_history_degraded_helper_honors_explicit_diagnostic_flag(
        self,
    ) -> None:
        """Stale fallback history is degraded for planning even when its source is stale_cache."""
        stale_degraded_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$reply:localhost", body="Reply"),
            ],
            is_full_history=True,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "homeserver unavailable",
            },
        )

        assert is_thread_history_degraded(stale_degraded_history) is True

    @pytest.mark.asyncio
    async def test_thread_root_proof_accepts_stale_cache_fallback_with_children(
        self,
    ) -> None:
        """Stale fallback history is degraded but still usable proof when it has children."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_history = ThreadHistoryResult(
            [
                _message(event_id=thread_root_id, body="Root"),
                _message(event_id="$reply:localhost", body="Reply"),
            ],
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                THREAD_HISTORY_ERROR_DIAGNOSTIC: "homeserver unavailable",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            return EventInfo.from_event(
                {
                    "content": {"body": "Root", "msgtype": "m.text"},
                    "event_id": thread_root_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567880,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

        async def fetch_thread_messages(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
            return thread_history

        resolved_thread_id = await resolve_related_event_thread_id_best_effort(
            room_id,
            thread_root_id,
            access=thread_messages_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_messages=fetch_thread_messages,
            ),
        )

        assert resolved_thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_coalescing_thread_id_labels_thread_membership_reads(self, bot: AgentBot) -> None:
        """Ingress coalescing should reject indeterminate thread proof refreshes it triggers."""
        room = _matrix_room()
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$plain1:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        access = MagicMock()
        resolver = unwrap_extracted_collaborator(bot._conversation_resolver)

        with (
            patch.object(
                resolver,
                "thread_membership_access",
                MagicMock(return_value=access),
            ) as mock_access,
            patch(
                "mindroom.conversation_resolver.resolve_event_thread_membership",
                new=AsyncMock(
                    return_value=ThreadResolution.indeterminate(
                        RuntimeError("proof unavailable"),
                        candidate_thread_root_id="$thread_root:localhost",
                    ),
                ),
            ) as mock_resolve,
            pytest.raises(RuntimeError, match="Could not resolve canonical coalescing thread"),
        ):
            await resolver.coalescing_thread_id(room, event)

        mock_access.assert_called_once_with(
            mode=ThreadReadMode.DISPATCH_SNAPSHOT,
            caller_label="coalescing_thread_id",
        )
        mock_resolve.assert_awaited_once_with(
            room.room_id,
            EventInfo.from_event(event.source),
            event_id=event.event_id,
            access=access,
        )

    @pytest.mark.asyncio
    async def test_coalescing_thread_id_rejects_lookup_failure_candidate(self, bot: AgentBot) -> None:
        """Lookup-failed plain replies should not be admitted under a guessed coalescing key."""
        room = _matrix_room()
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$maybe_root:localhost"}},
                },
                "event_id": "$event:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
            },
        )
        resolver = unwrap_extracted_collaborator(bot._conversation_resolver)

        with (
            patch.object(bot._conversation_cache, "get_thread_id_for_event", AsyncMock(return_value=None)),
            patch.object(bot._conversation_cache, "get_event", AsyncMock(side_effect=RuntimeError("lookup failed"))),
            pytest.raises(RuntimeError, match="Could not resolve canonical coalescing thread"),
        ):
            await resolver.coalescing_thread_id(room, event)

    @pytest.mark.asyncio
    async def test_full_history_thread_resolution_uses_full_history_to_prove_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Full-history resolution should use full history, not partial snapshots, to prove a root thread exists."""
        room_id = "!test:localhost"
        incoming_event_id = "$incoming:localhost"
        event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "Newest plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root:localhost"}},
                },
                "event_id": incoming_event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 3,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        thread_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$thread_reply:localhost", body="Thread reply"),
            ],
            is_full_history=True,
        )

        with (
            patch.object(
                bot._conversation_cache,
                "get_thread_id_for_event",
                AsyncMock(return_value=None),
            ) as mock_lookup,
            patch.object(
                bot._conversation_cache,
                "get_event",
                AsyncMock(
                    return_value=nio.RoomGetEventResponse.from_dict(
                        {
                            "content": {
                                "body": "Root",
                                "msgtype": "m.text",
                            },
                            "event_id": "$thread_root:localhost",
                            "sender": "@user:localhost",
                            "origin_server_ts": 1,
                            "room_id": room_id,
                            "type": "m.room.message",
                        },
                    ),
                ),
            ) as mock_get_event,
            patch.object(
                bot._conversation_cache,
                "get_thread_history",
                AsyncMock(return_value=thread_history_result(thread_history, is_full_history=True)),
            ) as mock_history,
        ):
            thread_context = await bot._conversation_resolver._resolve_thread_context(
                room_id,
                incoming_event_id,
                event_info,
                mode=ThreadReadMode.ADVISORY_FULL,
                caller_label="threading_error_test",
            )

        assert thread_context.is_thread is True
        assert thread_context.thread_id == "$thread_root:localhost"
        assert [message.event_id for message in thread_context.thread_history] == [
            "$thread_root:localhost",
            "$thread_reply:localhost",
        ]
        assert thread_context.requires_model_history_refresh is False
        mock_lookup.assert_awaited_once_with(room_id, "$thread_root:localhost")
        mock_get_event.assert_awaited_once_with(room_id, "$thread_root:localhost")
        mock_history.assert_awaited_once_with(room_id, "$thread_root:localhost", caller_label="threading_error_test")
