"""Conversation thread context resolution: extract_context and dispatch-context thread inheritance, demotion, and proofs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest

from mindroom.event_journal import ConversationPage, VisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import ThreadReadMode
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    is_thread_history_degraded,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    resolve_related_event_thread_id_best_effort,
    thread_messages_thread_membership_access,
)
from mindroom.response_runner import ResponseRequest
from mindroom.turn_policy import _DispatchPlan
from tests.conftest import (
    install_relation_lookup,
    request_envelope,
    unwrap_extracted_collaborator,
)
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _matrix_room,
    _message,
    seed_thread_history,
    seed_unhydrated_room_event,
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


def _room_get_event_by_id(*responses: nio.RoomGetEventResponse) -> AsyncMock:
    """Return a point-lookup mock keyed by event ID rather than call order.

    A positional `side_effect` list encodes how many times each hop is fetched,
    which is an implementation detail of whoever is walking the chain. Keying by
    ID lets the walk change shape without the mock handing back the wrong event.
    """
    by_id = {response.event.event_id: response for response in responses}

    async def _lookup(_room_id: str, event_id: str) -> object:
        return by_id.get(event_id, nio.RoomGetEventError("missing", status_code="M_NOT_FOUND"))

    return AsyncMock(side_effect=_lookup)


def _visible_message(room_id: str, event_id: str, *, thread_id: str | None, body: str) -> VisibleMessage:
    """Return one projected message as a hydrated conversation page would carry it."""
    return VisibleMessage(
        logical_event_id=event_id,
        room_id=room_id,
        thread_id=thread_id,
        sender="@user:localhost",
        created_ts=1234567880,
        revision_event_id=event_id,
        revision_ts=1234567880,
        content={"msgtype": "m.text", "body": body},
    )


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_extract_context_edit_ignores_the_thread_its_new_content_names(self, bot: AgentBot) -> None:
        """An edit is placed by the message it edits, not by the thread it names inside itself.

        Matrix applies ``m.new_content`` by keeping the original event's relation and ignoring
        every ``m.relates_to`` written there, so the thread named inside an edit is a claim its
        author chose. Reading it would let anyone who can edit a message move the conversation it
        belongs to - here into a thread the edited message was never part of.
        """
        room = _matrix_room(name="Test Room")

        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* updated",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "updated",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed_thread:localhost"},
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
            _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
            _message(event_id="$thread_msg:localhost", body="Original", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history

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
            _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
            _message(event_id="$thread_msg:localhost", body="Thread message", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_msg:localhost")

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

        root_only = [
            _message(
                event_id="$room_message:localhost",
                body="Room message",
                thread_id="$room_message:localhost",
            ),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$room_message:localhost",
            messages=root_only,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_message:localhost")

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
            _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
            _message(event_id="$thread_msg:localhost", body="Thread message", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history

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

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message", thread_id="$thread_root:localhost"),
            _message(event_id="$thread_reply:localhost", body="Thread reply", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")

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

        bot.client.room_get_event = _room_get_event_by_id(
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
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message", thread_id="$thread_root:localhost"),
            _message(event_id="$thread_msg:localhost", body="Thread reply", thread_id="$thread_root:localhost"),
            _message(
                event_id="$plain_reply_1:localhost",
                body="first bridge reply",
                thread_id="$thread_root:localhost",
            ),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        relations = install_relation_lookup(bot)
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert [message.event_id for message in context.thread_history] == [
            "$thread_root:localhost",
            "$thread_msg:localhost",
            "$plain_reply_1:localhost",
        ]
        assert relations.asked == [
            (room.room_id, "$plain_reply_1:localhost"),
            (room.room_id, "$thread_msg:localhost"),
        ]
        # Which events were fetched, not how many times: the resolver used to
        # ask a cached index and then the server, and now asks the journal and
        # then the server, so a hop can cost a different number of point reads.
        # The per-event cost is pinned against the real lookup in
        # `tests/test_relation_lookup.py`.
        assert {c.args[1] for c in bot.client.room_get_event.await_args_list} == {
            "$plain_reply_1:localhost",
            "$thread_msg:localhost",
        }

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

        expected_history = [
            _message(event_id="$thread_root:localhost", body="root", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        # The journal already admitted the promoted plain reply into the thread,
        # which is what the cached index used to be told.
        install_relation_lookup(bot, threads={"$plain_reply_1:localhost": "$thread_root:localhost"})
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        assert {c.args[1] for c in bot.client.room_get_event.await_args_list} == {"$plain_reply_1:localhost"}

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

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Root message", thread_id="$thread_root:localhost"),
            _message(event_id="$reply:localhost", body="Reply", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$thread_root:localhost")

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

        bot.client.room_get_event = _room_get_event_by_id(
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
        )

        expected_history = [
            _message(event_id="$thread-root:localhost", body="Root", thread_id="$thread-root:localhost"),
            _message(event_id="$thread-reply:localhost", body="Thread reply", thread_id="$thread-root:localhost"),
            _message(event_id="$plain-reply:localhost", body="Bridged plain reply", thread_id="$thread-root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread-root:localhost",
            messages=expected_history,
        )
        # A cold index is now an empty journal. Without this the journal would
        # already know these events' thread from having admitted them, and the
        # walk this test is about would never happen.
        install_relation_lookup(bot)
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread-root:localhost"
        assert context.thread_history == expected_history
        # Which events were fetched, not how many times: the resolver used to
        # ask a cached index and then the server, and now asks the journal and
        # then the server, so a hop can cost a different number of point reads.
        # The per-event cost is pinned against the real lookup in
        # `tests/test_relation_lookup.py`.
        assert {c.args[1] for c in bot.client.room_get_event.await_args_list} == {
            "$plain-reply:localhost",
            "$thread-reply:localhost",
        }

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

        root_only = [
            _message(event_id="$room_root:localhost", body="Room root", thread_id="$room_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$room_root:localhost",
            messages=root_only,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$room_root:localhost")

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
        install_relation_lookup(bot, failure=RuntimeError("sqlite boom"))

        root_only = [
            _message(
                event_id="$room_message:localhost",
                body="Room message",
                thread_id="$room_message:localhost",
            ),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$room_message:localhost",
            messages=root_only,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        # Twice: the edit's target, and then the thread lookup that the failed
        # journal read degraded onto the homeserver rather than giving up.
        assert bot.client.room_get_event.await_args_list == [
            call(room.room_id, "$room_message:localhost"),
            call(room.room_id, "$room_message:localhost"),
        ]

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

        bot.client.room_get_event = _room_get_event_by_id(
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
        )

        expected_history = [
            _message(event_id="$thread_root:localhost", body="Thread root", thread_id="$thread_root:localhost"),
            _message(
                event_id="$thread_msg:localhost",
                body="Earlier threaded message",
                thread_id="$thread_root:localhost",
            ),
            _message(event_id="$plain1:localhost", body="First plain reply", thread_id="$thread_root:localhost"),
            _message(event_id="$plain2:localhost", body="Second plain reply", thread_id="$thread_root:localhost"),
        ]
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=expected_history,
        )
        context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.thread_history == expected_history

    @pytest.mark.asyncio
    async def test_explicit_thread_id_returns_none_for_cyclic_edit_chain(self, bot: AgentBot) -> None:
        """Cyclic edit chains should fail closed instead of raising from the shared resolver."""
        cycle = {
            "$edit-a:localhost": ("a", "$edit-b:localhost", 1),
            "$edit-b:localhost": ("b", "$edit-a:localhost", 2),
        }

        async def fetch_cyclic_edit(_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            body, replaces, timestamp = cycle[event_id]
            return nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": f"* {body}",
                        "msgtype": "m.text",
                        "m.new_content": {"body": body, "msgtype": "m.text"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": replaces},
                    },
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": timestamp,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            )

        bot.client.room_get_event = AsyncMock(side_effect=fetch_cyclic_edit)
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
            mode=ThreadReadMode.STRICT,
        )

        assert thread_lookup.thread_id is None

    @pytest.mark.asyncio
    async def test_extract_dispatch_context_plain_reply_inherits_thread_with_bounded_full_history(
        self,
        bot: AgentBot,
    ) -> None:
        """Dispatch policy context should inherit an existing explicit thread across plain replies."""
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
        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {
                        "body": "Plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg:localhost"}},
                    },
                    "event_id": "$plain1:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": "!test:localhost",
                    "type": "m.room.message",
                },
            ),
        )

        dispatch_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
                _message(
                    event_id="$thread_msg:localhost",
                    body="Earlier threaded message",
                    thread_id="$thread_root:localhost",
                ),
                _message(event_id="$plain1:localhost", body="Plain reply", thread_id="$thread_root:localhost"),
            ],
            is_full_history=True,
        )
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=list(dispatch_history),
        )
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
        bot.client.room_get_event.assert_awaited_once_with(room.room_id, "$plain1:localhost")

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
                _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
                _message(event_id="$plain1:localhost", body="Plain reply", thread_id="$thread_root:localhost"),
            ],
            is_full_history=True,
        )
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=list(dispatch_history),
        )

        context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
        context = context_result.context

        assert context.is_thread is True
        assert context.thread_id == "$thread_root:localhost"
        assert context.requires_model_history_refresh is False

    @pytest.mark.asyncio
    async def test_dispatch_room_demotion_clears_source_and_resolved_thread_ids(
        self,
        bot: AgentBot,
    ) -> None:
        """Strict root proof should demote an indeterminate plain-reply candidate to room-level dispatch."""
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
        # The room holds an admitted event this conversation has never
        # hydrated, so local absence cannot prove the thread is empty and the
        # dispatch read comes back degraded.
        await seed_unhydrated_room_event(
            bot,
            room_id=room.room_id,
            event_id="$thread_root:localhost",
            body="root",
        )
        empty_strict_page = ConversationPage(messages=(), refresh_pending=(), next_cursor=None)

        relations = install_relation_lookup(bot)
        with (
            patch.object(
                bot.client,
                "room_get_event",
                AsyncMock(return_value=root_response),
            ) as mock_get_event,
            patch(
                "mindroom.matrix.conversation_reads.ConversationReader.read_strict",
                new=AsyncMock(return_value=empty_strict_page),
            ) as mock_strict_read,
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
        # The strict read answered, so the guard history is that complete page
        # rather than the degraded dispatch page that preceded it.
        assert context_result.thread_context.replay_guard_history == []
        assert context_result.thread_context.replay_guard_history.is_full_history is True
        assert context_result.thread_context.replay_guard_degraded is False
        assert mock_strict_read.await_count >= 1
        assert context.requires_model_history_refresh is False
        assert context.planning_thread_history == ()
        assert relations.asked == [
            (room.room_id, "$thread_root:localhost"),
            (room.room_id, "$thread_root:localhost"),
        ]
        mock_get_event.assert_has_awaits(
            [
                call(room.room_id, "$thread_root:localhost"),
                call(room.room_id, "$thread_root:localhost"),
            ],
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
        await seed_unhydrated_room_event(
            bot,
            room_id=room.room_id,
            event_id="$maybe_root:localhost",
            body="maybe root",
        )

        with (
            patch.object(bot.client, "room_get_event", AsyncMock(return_value=root_response)),
            patch(
                "mindroom.matrix.conversation_reads.ConversationReader.read_strict",
                new=AsyncMock(side_effect=TimeoutError("dispatch read timed out")),
            ),
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(
                room,
                event,
                mode=ThreadReadMode.NONBLOCKING,
            )
            context = context_result.context

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$maybe_root:localhost"
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.thread_context.replay_guard_history == []
        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False

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
            patch.object(bot.client, "room_get_event", AsyncMock(side_effect=RuntimeError("lookup failed"))),
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$maybe_root:localhost"
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id is None
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.context.is_thread is False
        assert context_result.context.thread_id is None

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
            patch.object(
                bot.client,
                "room_get_event",
                AsyncMock(return_value=nio.RoomGetEventError("missing", status_code="M_NOT_FOUND")),
            ),
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context_result.thread_context is not None
        assert context_result.thread_context.candidate_thread_root_id == "$missing_root:localhost"
        assert context_result.thread_context.stable_target.source_thread_id is None
        assert context_result.thread_context.stable_target.resolved_thread_id is None
        assert context_result.thread_context.replay_guard_degraded is True
        assert context_result.context.is_thread is False
        assert context_result.context.thread_id is None

    @pytest.mark.asyncio
    async def test_dispatch_first_journal_event_in_an_existing_room_keeps_the_thread(
        self,
        bot: AgentBot,
    ) -> None:
        """A conversation nobody hydrated cannot report itself complete on its room's first event.

        The cutover shape: a room full of Matrix history whose journal is
        empty, so the very first admitted event is the only row in it. Nothing
        local knows the reply target is a thread root, and a dispatch-safe read
        that calls that absence "complete" turns the room's real thread into
        room-level traffic for exactly one turn -- the turn a user is waiting on.
        """
        room = _matrix_room(name="Test Room")
        event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "plain reply to a thread root the journal never saw",
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
        # The inbound event is the only row the journal holds for this room,
        # which is what makes "is there another event here" answer "no" about a
        # room that predates the journal entirely.
        await seed_unhydrated_room_event(
            bot,
            room_id=room.room_id,
            event_id="$event:localhost",
            body="plain reply to a thread root the journal never saw",
        )
        # What hydration would install: the thread the homeserver has had all
        # along, reachable only once a read admits it does not already know.
        hydrated_thread = ConversationPage(
            messages=(
                _visible_message(room.room_id, "$thread_root:localhost", thread_id=None, body="root"),
                _visible_message(room.room_id, "$reply:localhost", thread_id="$thread_root:localhost", body="reply"),
            ),
            refresh_pending=(),
            next_cursor=None,
        )

        with (
            patch.object(bot.client, "room_get_event", AsyncMock(return_value=root_response)),
            patch(
                "mindroom.matrix.conversation_reads.ConversationReader.read_strict",
                new=AsyncMock(return_value=hydrated_thread),
            ) as mock_strict_read,
        ):
            context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)

        assert context_result.context.is_thread is True
        assert context_result.context.thread_id == "$thread_root:localhost"
        assert context_result.thread_context is not None
        assert context_result.thread_context.stable_target.source_thread_id == "$thread_root:localhost"
        assert mock_strict_read.await_count >= 1

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
            patch.object(
                bot.client,
                "room_get_event",
                AsyncMock(return_value=nio.RoomGetEventError("missing", status_code="M_NOT_FOUND")),
            ),
        ):
            context = await bot._conversation_resolver.extract_message_context(room, event)

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False

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
        bot.client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "content": {"body": "not a thread root", "msgtype": "m.text"},
                    "event_id": "$plain:localhost",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567880,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                },
            ),
        )

        context_result = await bot._conversation_resolver.extract_dispatch_context(room, event)
        context = context_result.context

        assert context.is_thread is False
        assert context.thread_id is None
        assert context.thread_history == []
        assert context.requires_model_history_refresh is False

    @pytest.mark.asyncio
    async def test_degraded_dispatch_candidate_calls_strict_proof_before_planning(
        self,
        bot: AgentBot,
    ) -> None:
        """Degraded dispatch candidates must receive strict proof before room-level planning."""
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

        with (
            patch.object(bot.client, "room_get_event", AsyncMock(return_value=root_response)),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock(side_effect=fake_plan)),
        ):
            await bot._turn_controller._dispatch_text_message(room, event, "@user:localhost")

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
        seeded = [
            _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
            _message(event_id="$reply:localhost", body="Reply", thread_id="$thread_root:localhost"),
        ]
        # Seed the thread's content but leave the conversation unhydrated, so
        # the dispatch read really is degraded rather than being told it is.
        await seed_thread_history(
            bot,
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            messages=seeded,
            hydrated=False,
        )
        degraded_history = thread_history_result(
            seeded,
            is_full_history=False,
            diagnostics={
                THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
                THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
            },
        )
        full_history = thread_history_result(seeded, is_full_history=True)
        strict_page = await bot._journal_store.principal(bot._journal_principal_id).read_conversation(
            room_id=room.room_id,
            thread_id="$thread_root:localhost",
            limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
        )
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
            patch(
                "mindroom.matrix.conversation_reads.ConversationReader.read_strict",
                new=AsyncMock(return_value=strict_page),
            ),
            patch("mindroom.turn_policy.TurnPolicy.plan_turn", new=AsyncMock(side_effect=fake_plan)),
        ):
            await bot._turn_controller._dispatch_text_message(room, event, "@user:localhost")

        assert observed_policy_targets[0].resolved_thread_id == "$thread_root:localhost"

        with patch.object(
            resolver,
            "fetch_thread_history",
            AsyncMock(return_value=full_history),
        ):
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

        assert request.thread_history == full_history

    def test_thread_history_degraded_helper_honors_explicit_diagnostic_flag(
        self,
    ) -> None:
        """The explicit flag alone marks a read degraded, whatever its content says."""
        flagged_history = ThreadHistoryResult(
            [
                _message(event_id="$thread_root:localhost", body="Root"),
                _message(event_id="$reply:localhost", body="Reply"),
            ],
            is_full_history=True,
            diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True},
        )

        assert is_thread_history_degraded(flagged_history) is True

    @pytest.mark.asyncio
    async def test_thread_root_proof_accepts_partial_history_with_children(
        self,
    ) -> None:
        """A page that is not the whole thread still proves the root has children."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_history = ThreadHistoryResult(
            [
                _message(event_id=thread_root_id, body="Root"),
                _message(event_id="$reply:localhost", body="Reply"),
            ],
            is_full_history=False,
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
        """Ingress coalescing repairs an unproven candidate once, then rejects it.

        Two labelled reads, in this order. The dispatch-safe one is what every
        turn pays; the strict one runs only for a candidate root the first read
        could not prove, and here the stub refuses to prove it either, so the
        repair is exhausted and the scope stays unresolved.
        """
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
                "_thread_membership_access",
                MagicMock(return_value=access),
            ) as mock_access,
            patch(
                "mindroom.conversation_resolver.resolve_event_thread_membership",
                new=AsyncMock(
                    return_value=ThreadResolution._indeterminate(
                        RuntimeError("proof unavailable"),
                        candidate_thread_root_id="$thread_root:localhost",
                    ),
                ),
            ),
            pytest.raises(RuntimeError, match="Could not resolve canonical coalescing thread"),
        ):
            await resolver.coalescing_thread_id(room, event)

        assert mock_access.call_args_list == [
            call(
                mode=ThreadReadMode.NONBLOCKING,
                requires_complete_history=True,
            ),
            call(
                mode=ThreadReadMode.STRICT,
                requires_complete_history=True,
            ),
        ]

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
            patch.object(bot.client, "room_get_event", AsyncMock(side_effect=RuntimeError("lookup failed"))),
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
                _message(event_id="$thread_root:localhost", body="Root", thread_id="$thread_root:localhost"),
                _message(event_id="$thread_reply:localhost", body="Thread reply", thread_id="$thread_root:localhost"),
            ],
            is_full_history=True,
        )
        await seed_thread_history(
            bot,
            room_id=room_id,
            thread_id="$thread_root:localhost",
            messages=list(thread_history),
        )

        thread_context = await bot._conversation_resolver._resolve_thread_context(
            room_id,
            incoming_event_id,
            event_info,
            mode=ThreadReadMode.STRICT,
        )

        assert thread_context.is_thread is True
        assert thread_context.thread_id == "$thread_root:localhost"
        assert [message.event_id for message in thread_context.thread_history] == [
            "$thread_root:localhost",
            "$thread_reply:localhost",
        ]
        assert thread_context.requires_model_history_refresh is False
