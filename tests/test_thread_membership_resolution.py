"""Transitive thread membership resolution and thread-root proofs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadResolutionState,
    ThreadRootProof,
    conversation_relation_thread_membership_access,
    resolve_event_thread_membership,
    resolve_related_event_thread_id_best_effort,
    resolve_related_event_thread_membership,
    room_scan_thread_membership_access,
    thread_messages_thread_membership_access,
)
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos
from tests.conftest import (
    drain_coalescing,
)
from tests.event_cache_test_support import replace_thread_unconditionally
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
    _matrix_room,
    _wait_for_room_cache_idle,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

    @pytest.mark.asyncio
    async def test_padded_reply_target_does_not_enter_thread_membership_resolution(self) -> None:
        """A malformed padded reply ID cannot be normalized into another event's identity."""
        room_id = "!test:localhost"
        event_id = "$reply:localhost"
        event_source = {
            "event_id": event_id,
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {
                "body": "Malformed reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "  $victim:localhost  "},
                },
            },
        }
        fail_if_called = AsyncMock(side_effect=AssertionError("invalid relation target must be ignored"))

        resolution = await resolve_event_thread_membership(
            room_id,
            EventInfo.from_event(event_source),
            event_id=event_id,
            event_source=event_source,
            access=ThreadMembershipAccess(
                lookup_thread_id=fail_if_called,
                fetch_event_info=fail_if_called,
                prove_thread_root=fail_if_called,
            ),
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL
        assert resolution.thread_id is None

    @pytest.mark.asyncio
    async def test_live_plain_reply_to_threaded_event_persists_event_thread_membership(
        self,
        bot: AgentBot,
    ) -> None:
        """Plain replies to threaded events should keep a durable event-to-thread mapping."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"

        real_event_cache = SqliteEventCache(bot.storage_path / "plain-reply-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await real_event_cache.store_event(
                thread_reply_id,
                room_id,
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567894,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                plain_reply_event,
                event_info=EventInfo.from_event(plain_reply_event.source),
            )
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            assert await real_event_cache.get_thread_id_for_event(room_id, plain_reply_id) == thread_root_id
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_live_plain_reply_chain_persists_thread_membership_transitively(
        self,
        bot: AgentBot,
    ) -> None:
        """A plain-reply chain should persist thread membership transitively once it reaches a thread."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        second_plain_reply_id = "$second_plain_reply:localhost"

        real_event_cache = SqliteEventCache(bot.storage_path / "plain-reply-second-hop-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await replace_thread_unconditionally(
                real_event_cache,
                room_id,
                thread_root_id,
                [
                    {
                        "content": {
                            "body": "Thread reply",
                            "msgtype": "m.text",
                            "m.relates_to": {
                                "rel_type": "m.thread",
                                "event_id": thread_root_id,
                            },
                        },
                        "event_id": thread_reply_id,
                        "sender": "@mindroom_general:localhost",
                        "origin_server_ts": 1234567894,
                        "room_id": room_id,
                        "type": "m.room.message",
                    },
                    {
                        "content": {
                            "body": "first bridge reply",
                            "msgtype": "m.text",
                            "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                        },
                        "event_id": plain_reply_id,
                        "sender": "@user:localhost",
                        "origin_server_ts": 1234567895,
                        "room_id": room_id,
                        "type": "m.room.message",
                    },
                ],
            )

            second_plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "second bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_id}},
                    },
                    "event_id": second_plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                second_plain_reply_event,
                event_info=EventInfo.from_event(second_plain_reply_event.source),
            )
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            assert await real_event_cache.get_thread_id_for_event(room_id, second_plain_reply_id) == thread_root_id
        finally:
            await real_event_cache.close()

    @pytest.mark.asyncio
    async def test_media_ingress_does_not_guess_past_unproven_rich_reply_root(
        self,
        bot: AgentBot,
    ) -> None:
        """Cold-start media ingress should not guess past an unproven rich-reply root."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        audio_event_id = "$audio_reply:localhost"
        room = _matrix_room(room_id)
        real_event_cache = SqliteEventCache(bot.storage_path / "media-ingress-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        audio_event = nio.RoomMessageAudio.from_dict(
            {
                "content": {
                    "body": "voice-note.ogg",
                    "msgtype": "m.audio",
                    "url": "mxc://localhost/voice-note",
                    "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_id}},
                },
                "event_id": audio_event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1234567896,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        prechecked_event = MagicMock(event=audio_event, requester_user_id="@user:localhost")
        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=True)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()

        def room_get_event_response(event_id: str, content: dict[str, object]) -> nio.RoomGetEventResponse:
            return nio.RoomGetEventResponse.from_dict(
                {
                    "content": content,
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567890,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

        async def fetch_related_event(fetch_room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            assert fetch_room_id == room_id
            if event_id == plain_reply_id:
                return room_get_event_response(
                    plain_reply_id,
                    {
                        "body": "bridge reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                )
            if event_id == thread_reply_id:
                return room_get_event_response(
                    thread_reply_id,
                    {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                )
            msg = f"unexpected event lookup: {event_id}"
            raise AssertionError(msg)

        bot.client.room_get_event = AsyncMock(side_effect=fetch_related_event)

        try:
            await bot._turn_controller.handle_media_event(room, audio_event)
            await drain_coalescing(bot)
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            assert await real_event_cache.get_thread_id_for_event(room_id, audio_event_id) is None
        finally:
            await real_event_cache.close()

    @pytest.mark.parametrize("invalidity", ["state", "wrong-room", "malformed", "unsupported-type"])
    @pytest.mark.asyncio
    async def test_invalid_current_source_cannot_supply_explicit_thread_membership(
        self,
        invalidity: str,
    ) -> None:
        """Explicit thread metadata is proof only on valid current-room timeline events."""
        room_id = "!room:localhost"
        source: dict[str, object] = {
            "event_id": "$child:localhost",
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {
                "body": "child",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$foreign-root:localhost",
                },
            },
        }
        if invalidity == "state":
            source["state_key"] = ""
        elif invalidity == "wrong-room":
            source["room_id"] = "!other:localhost"
        elif invalidity == "unsupported-type":
            source["type"] = "m.sticker"
            source["content"] = {
                "body": "sticker",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$foreign-root:localhost",
                },
                "url": "mxc://localhost/sticker",
            }
        else:
            content = source["content"]
            assert isinstance(content, dict)
            del content["msgtype"]
        access = ThreadMembershipAccess(
            lookup_thread_id=AsyncMock(return_value=None),
            fetch_event_info=AsyncMock(return_value=None),
            prove_thread_root=AsyncMock(return_value=ThreadRootProof.not_a_thread_root()),
        )

        resolution = await resolve_event_thread_membership(
            room_id,
            EventInfo.from_event(source),
            event_id="$child:localhost",
            event_source=source,
            access=access,
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL

    @pytest.mark.asyncio
    async def test_transitive_thread_membership_handles_long_reply_chains(
        self,
    ) -> None:
        """The shared transitive resolver should handle reply chains longer than the old 32-hop ceiling."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        threaded_event_id = "$thread_reply:localhost"
        last_event_id = "$plain_reply_33:localhost"
        event_infos: dict[str, EventInfo] = {
            threaded_event_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": threaded_event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }
        for index in range(1, 34):
            event_id = f"$plain_reply_{index}:localhost"
            reply_target_id = threaded_event_id if index == 1 else f"$plain_reply_{index - 1}:localhost"
            event_infos[event_id] = EventInfo.from_event(
                {
                    "content": {
                        "body": f"plain reply {index}",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": reply_target_id}},
                    },
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": index + 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return event_infos.get(event_id)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolution = await resolve_event_thread_membership(
            room_id,
            event_infos[last_event_id],
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_resolve_thread_ids_for_event_infos_reaches_fixpoint_across_transitive_chain(
        self,
    ) -> None:
        """Map-backed resolution should derive thread IDs even when children are visited before parents."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_1_id = "$plain_reply_1:localhost"
        plain_reply_2_id = "$plain_reply_2:localhost"
        event_infos = {
            thread_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_1_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply 1",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_1_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 2,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_2_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply 2",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": plain_reply_1_id}},
                    },
                    "event_id": plain_reply_2_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 3,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }

        resolved_thread_ids = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            ordered_event_ids=[
                plain_reply_2_id,
                plain_reply_1_id,
                thread_reply_id,
            ],
        )

        assert resolved_thread_ids == {
            thread_reply_id: thread_root_id,
            plain_reply_1_id: thread_root_id,
            plain_reply_2_id: thread_root_id,
        }

    @pytest.mark.parametrize("invalid_position", ["root", "child"])
    @pytest.mark.parametrize("invalidity", ["state", "wrong-room", "malformed"])
    @pytest.mark.asyncio
    async def test_batch_resolution_rejects_invalid_current_and_root_proof_sources(
        self,
        invalidity: str,
        invalid_position: str,
    ) -> None:
        """Batch projection cannot resolve or prove membership from an invalid supplied source."""
        room_id = "!test:localhost"
        root_id = "$root:localhost"
        child_id = "$child:localhost"
        event_sources: dict[str, dict[str, object]] = {
            root_id: {
                "content": {"body": "root", "msgtype": "m.text"},
                "event_id": root_id,
                "origin_server_ts": 1,
                "room_id": room_id,
                "sender": "@user:localhost",
                "type": "m.room.message",
            },
            child_id: {
                "content": {
                    "body": "child",
                    "msgtype": "m.text",
                    "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
                },
                "event_id": child_id,
                "origin_server_ts": 2,
                "room_id": room_id,
                "sender": "@user:localhost",
                "type": "m.room.message",
            },
        }
        invalid_source = event_sources[root_id if invalid_position == "root" else child_id]
        if invalidity == "state":
            invalid_source["state_key"] = ""
        elif invalidity == "wrong-room":
            invalid_source["room_id"] = "!other:localhost"
        else:
            content = invalid_source["content"]
            assert isinstance(content, dict)
            del content["msgtype"]
        event_infos = {event_id: EventInfo.from_event(event_source) for event_id, event_source in event_sources.items()}

        resolved = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            event_sources_by_event_id=event_sources,
            ordered_event_ids=[root_id, child_id],
        )

        assert resolved == ({child_id: root_id} if invalid_position == "root" else {})

    @pytest.mark.parametrize("invalidity", ["state", "wrong-room", "malformed"])
    @pytest.mark.asyncio
    async def test_batch_resolution_rejects_invalid_related_ancestor_source(self, invalidity: str) -> None:
        """A valid reply cannot inherit membership through an invalid supplied ancestor."""
        room_id = "!test:localhost"
        root_id = "$root:localhost"
        ancestor_id = "$ancestor:localhost"
        child_id = "$child:localhost"
        event_sources: dict[str, dict[str, object]] = {
            root_id: {
                "content": {"body": "root", "msgtype": "m.text"},
                "event_id": root_id,
                "origin_server_ts": 1,
                "room_id": room_id,
                "sender": "@user:localhost",
                "type": "m.room.message",
            },
            ancestor_id: {
                "content": {
                    "body": "ancestor",
                    "msgtype": "m.text",
                    "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
                },
                "event_id": ancestor_id,
                "origin_server_ts": 2,
                "room_id": room_id,
                "sender": "@user:localhost",
                "type": "m.room.message",
            },
            child_id: {
                "content": {
                    "body": "child",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": ancestor_id}},
                },
                "event_id": child_id,
                "origin_server_ts": 3,
                "room_id": room_id,
                "sender": "@user:localhost",
                "type": "m.room.message",
            },
        }
        invalid_source = event_sources[ancestor_id]
        if invalidity == "state":
            invalid_source["state_key"] = ""
        elif invalidity == "wrong-room":
            invalid_source["room_id"] = "!other:localhost"
        else:
            content = invalid_source["content"]
            assert isinstance(content, dict)
            del content["msgtype"]
        event_infos = {event_id: EventInfo.from_event(event_source) for event_id, event_source in event_sources.items()}

        resolved = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            event_sources_by_event_id=event_sources,
            ordered_event_ids=[root_id, ancestor_id, child_id],
        )

        assert resolved == {}

    @pytest.mark.asyncio
    async def test_batch_resolution_promotes_rich_reply_with_explicit_thread_child(self) -> None:
        """Local child proof makes a rich reply its own root before parent ancestry wins."""
        room_id = "!test:localhost"
        parent_id = "$parent:localhost"
        rich_reply_id = "$rich_reply:localhost"
        child_id = "$child:localhost"
        old_thread_id = "$old_thread:localhost"
        event_infos = {
            parent_id: EventInfo.from_event(
                {
                    "type": "m.room.message",
                    "content": {
                        "body": "Parent",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": old_thread_id},
                    },
                },
            ),
            rich_reply_id: EventInfo.from_event(
                {
                    "type": "m.room.message",
                    "content": {
                        "body": "Rich reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": parent_id}},
                    },
                },
            ),
            child_id: EventInfo.from_event(
                {
                    "type": "m.room.message",
                    "content": {
                        "body": "Child",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": rich_reply_id},
                    },
                },
            ),
        }

        resolved = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            ordered_event_ids=[parent_id, rich_reply_id, child_id],
        )

        assert resolved == {
            parent_id: old_thread_id,
            rich_reply_id: rich_reply_id,
            child_id: rich_reply_id,
        }

    @pytest.mark.asyncio
    async def test_local_graph_edit_inherits_original_membership_not_replacement_content(self) -> None:
        """A replacement row follows its original even when new content claims another thread."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        edit_id = "$thread_reply_edit:localhost"
        event_sources = {
            thread_reply_id: {
                "event_id": thread_reply_id,
                "room_id": room_id,
                "sender": "@alice:localhost",
                "origin_server_ts": 1,
                "type": "m.room.message",
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
            edit_id: {
                "event_id": edit_id,
                "room_id": room_id,
                "sender": "@alice:localhost",
                "origin_server_ts": 2,
                "type": "m.room.message",
                "content": {
                    "body": "* forged relation",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "forged relation",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$other_thread:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": thread_reply_id},
                },
            },
        }
        event_infos = {event_id: EventInfo.from_event(event_source) for event_id, event_source in event_sources.items()}

        resolved_thread_ids = await resolve_thread_ids_for_event_infos(
            room_id,
            event_infos=event_infos,
            event_sources_by_event_id=event_sources,
            ordered_event_ids=[edit_id, thread_reply_id],
        )

        assert resolved_thread_ids == {
            thread_reply_id: thread_root_id,
            edit_id: thread_root_id,
        }

    @pytest.mark.asyncio
    async def test_edit_membership_ignores_thread_claim_in_new_content(self) -> None:
        """Canonical membership follows the edited event, never copied replacement relations."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        original_id = "$thread_reply:localhost"
        original_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
        )
        edit_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "* forged relation",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "forged relation",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$forged:localhost"},
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
                },
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return original_info if event_id == original_id else None

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            msg = "explicit original membership must not need root proof"
            raise AssertionError(msg)

        resolution = await resolve_event_thread_membership(
            room_id,
            edit_info,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_related_edit_membership_ignores_legacy_cached_edit_index(self) -> None:
        """A related replacement follows its original instead of a stale edit index."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        original_id = "$thread_reply:localhost"
        edit_id = "$thread_reply_edit:localhost"
        edit_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "* edited",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "edited", "msgtype": "m.text"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
                },
            },
        )
        original_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Thread reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
        )

        async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
            return "$forged:localhost" if event_id == edit_id else None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return {edit_id: edit_info, original_id: original_info}.get(event_id)

        resolution = await resolve_related_event_thread_membership(
            room_id,
            edit_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=AsyncMock(side_effect=AssertionError("edits cannot be roots")),
                fetch_event_source=AsyncMock(
                    side_effect=lambda _room_id, event_id: {
                        edit_id: {
                            "event_id": edit_id,
                            "room_id": room_id,
                            "sender": "@alice:localhost",
                            "origin_server_ts": 2,
                            "type": "m.room.message",
                            "content": {
                                "body": "* edited",
                                "msgtype": "m.text",
                                "m.new_content": {"body": "edited", "msgtype": "m.text"},
                                "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
                            },
                        },
                        original_id: {
                            "event_id": original_id,
                            "room_id": room_id,
                            "sender": "@alice:localhost",
                            "origin_server_ts": 1,
                            "type": "m.room.message",
                            "content": {
                                "body": "Thread reply",
                                "msgtype": "m.text",
                                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                            },
                        },
                    }.get(event_id),
                ),
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_rich_reply_root_proof_precedes_its_reply_ancestry(self) -> None:
        """A proven rich-reply root owns its thread instead of inheriting its parent's."""
        room_id = "!test:localhost"
        rich_reply_id = "$rich_reply:localhost"
        rich_reply_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Rich reply root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$parent:localhost"}},
                },
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            raise AssertionError(event_id)

        async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
            assert event_id == rich_reply_id
            return ThreadRootProof.proven()

        resolution = await resolve_event_thread_membership(
            room_id,
            rich_reply_info,
            event_id=rich_reply_id,
            allow_current_root=True,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == rich_reply_id

    @pytest.mark.asyncio
    async def test_related_rich_reply_root_proof_precedes_its_reply_ancestry(self) -> None:
        """A proven rich-reply root reached through a relation owns its thread."""
        room_id = "!test:localhost"
        rich_reply_id = "$rich_reply:localhost"
        parent_id = "$parent:localhost"
        parent_thread_id = "$parent_thread:localhost"
        rich_reply_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Rich reply root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": parent_id}},
                },
            },
        )

        async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
            return parent_thread_id if event_id == parent_id else None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            assert event_id == rich_reply_id
            return rich_reply_info

        async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
            assert event_id == rich_reply_id
            return ThreadRootProof.proven()

        resolution = await resolve_related_event_thread_membership(
            room_id,
            rich_reply_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == rich_reply_id

    @pytest.mark.asyncio
    async def test_negative_candidate_root_proof_preserves_history(self) -> None:
        """A definitive negative proof keeps its strict history for replay safety."""
        room_id = "!test:localhost"
        candidate_root_id = "$candidate_root:localhost"
        strict_history: list[object] = []
        candidate_root_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Candidate root",
                    "msgtype": "m.text",
                },
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return candidate_root_info if event_id == candidate_root_id else None

        async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
            assert event_id == candidate_root_id
            return ThreadRootProof.not_a_thread_root(thread_history=strict_history)

        resolution = await resolve_related_event_thread_membership(
            room_id,
            candidate_root_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL
        assert resolution.thread_history is strict_history

    @pytest.mark.asyncio
    async def test_negative_candidate_root_proof_overrides_stale_cached_index(self) -> None:
        """A definitive non-root proof must beat advisory inherited membership."""
        room_id = "!test:localhost"
        candidate_root_id = "$candidate_root:localhost"
        candidate_root_source = {
            "event_id": candidate_root_id,
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {
                "body": "Candidate root",
                "msgtype": "m.text",
            },
        }
        candidate_root_info = EventInfo.from_event(candidate_root_source)

        async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
            return "$stale_thread:localhost" if event_id == candidate_root_id else None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return candidate_root_info if event_id == candidate_root_id else None

        async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
            assert event_id == candidate_root_id
            return ThreadRootProof.not_a_thread_root()

        async def fetch_event_source(_room_id: str, event_id: str) -> dict[str, object] | None:
            return candidate_root_source if event_id == candidate_root_id else None

        resolution = await resolve_related_event_thread_membership(
            room_id,
            candidate_root_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
                fetch_event_source=fetch_event_source,
            ),
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL
        assert resolution.thread_id is None

    @pytest.mark.asyncio
    async def test_malformed_self_thread_relation_overrides_stale_cached_index(self) -> None:
        """A valid source with an unusable self-thread relation must beat its stale index."""
        room_id = "!test:localhost"
        event_id = "$self_thread:localhost"
        event_source = {
            "event_id": event_id,
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {
                "body": "Malformed self-thread",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": event_id},
            },
        }
        event_info = EventInfo.from_event(event_source)

        resolution = await resolve_related_event_thread_membership(
            room_id,
            event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=AsyncMock(return_value="$stale_thread:localhost"),
                fetch_event_info=AsyncMock(return_value=event_info),
                prove_thread_root=AsyncMock(side_effect=AssertionError("self-thread cannot be a root")),
                fetch_event_source=AsyncMock(return_value=event_source),
            ),
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL
        assert resolution.thread_id is None

    @pytest.mark.asyncio
    async def test_related_rich_reply_root_proof_precedes_stale_cached_parent_membership(self) -> None:
        """A cached inherited membership cannot hide later proof that the reply is a root."""
        room_id = "!test:localhost"
        rich_reply_id = "$rich_reply:localhost"
        parent_thread_id = "$parent_thread:localhost"
        rich_reply_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Rich reply root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$parent:localhost"}},
                },
            },
        )
        fetch_event_info = AsyncMock(return_value=rich_reply_info)

        async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
            return parent_thread_id if event_id == rich_reply_id else None

        async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
            assert event_id == rich_reply_id
            return ThreadRootProof.proven()

        resolution = await resolve_related_event_thread_membership(
            room_id,
            rich_reply_id,
            access=conversation_relation_thread_membership_access(
                ThreadMembershipAccess(
                    lookup_thread_id=lookup_thread_id,
                    fetch_event_info=fetch_event_info,
                    prove_thread_root=prove_thread_root,
                ),
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == rich_reply_id
        fetch_event_info.assert_awaited_with(room_id, rich_reply_id)

    @pytest.mark.asyncio
    async def test_stale_cached_parent_membership_fails_closed_when_root_proof_is_unavailable(self) -> None:
        """An inherited index is unsafe while current rich-reply root status is unknown."""
        room_id = "!test:localhost"
        rich_reply_id = "$rich_reply:localhost"
        rich_reply_info = EventInfo.from_event(
            {
                "type": "m.room.message",
                "content": {
                    "body": "Rich reply root",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$parent:localhost"}},
                },
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return "$parent_thread:localhost"

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            return rich_reply_info

        async def prove_thread_root(_room_id: str, _event_id: str) -> ThreadRootProof:
            return ThreadRootProof.proof_unavailable(RuntimeError("room scan unavailable"))

        resolution = await resolve_related_event_thread_membership(
            room_id,
            rich_reply_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert resolution.candidate_thread_root_id == rich_reply_id

    @pytest.mark.asyncio
    async def test_resolve_event_thread_membership_follows_reaction_target_transitively(
        self,
    ) -> None:
        """The shared entrypoint should inherit thread membership across reaction targets too."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        reaction_event = EventInfo.from_event(
            {
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": plain_reply_id,
                        "key": "👍",
                    },
                },
                "event_id": "$reaction:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 4,
                "room_id": room_id,
                "type": "m.reaction",
            },
        )
        event_infos: dict[str, EventInfo] = {
            thread_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
            plain_reply_id: EventInfo.from_event(
                {
                    "content": {
                        "body": "plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 2,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            ),
        }

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return event_infos.get(event_id)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolution = await resolve_event_thread_membership(
            room_id,
            reaction_event,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_room_scan_thread_membership_access_treats_root_with_children_as_threaded(
        self,
    ) -> None:
        """Room-scan-backed access should apply one shared root-children rule."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        plain_reply_id = "$plain_reply:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        plain_reply_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": thread_root_id}},
                },
                "event_id": plain_reply_id,
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            if event_id == thread_root_id:
                return root_event_info
            if event_id == plain_reply_id:
                return plain_reply_event_info
            return None

        async def fetch_thread_event_sources(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> tuple[list[dict[str, object]], bool]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                {
                    "content": {"body": "root", "msgtype": "m.text"},
                    "event_id": thread_root_id,
                    "type": "m.room.message",
                },
                {
                    "content": {
                        "body": "child",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "event_id": thread_root_id,
                            "rel_type": "m.thread",
                        },
                    },
                    "event_id": "$child:localhost",
                    "type": "m.room.message",
                },
            ], True

        resolution = await resolve_event_thread_membership(
            room_id,
            plain_reply_event_info,
            event_id=plain_reply_id,
            access=room_scan_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_event_sources=fetch_thread_event_sources,
            ),
        )

        assert resolution.state is ThreadResolutionState.THREADED
        assert resolution.thread_id == thread_root_id

    @pytest.mark.asyncio
    async def test_room_scan_thread_membership_access_does_not_treat_root_edit_as_child_proof(
        self,
    ) -> None:
        """A root edit alone should not prove that plain replies to the root belong to a thread."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        plain_reply_id = "$plain_reply:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        plain_reply_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": thread_root_id}},
                },
                "event_id": plain_reply_id,
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            if event_id == thread_root_id:
                return root_event_info
            if event_id == plain_reply_id:
                return plain_reply_event_info
            return None

        async def fetch_thread_event_sources(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> tuple[list[dict[str, object]], bool]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                {
                    "event_id": thread_root_id,
                    "type": "m.room.message",
                    "content": {
                        "body": "root",
                        "msgtype": "m.text",
                    },
                },
                {
                    "event_id": "$root_edit:localhost",
                    "type": "m.room.message",
                    "content": {
                        "body": "* root edited",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "root edited",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {
                            "rel_type": "m.replace",
                            "event_id": thread_root_id,
                        },
                    },
                },
            ], True

        resolution = await resolve_event_thread_membership(
            room_id,
            plain_reply_event_info,
            event_id=plain_reply_id,
            access=room_scan_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_event_sources=fetch_thread_event_sources,
            ),
        )

        assert resolution.state is ThreadResolutionState.ROOM_LEVEL
        assert resolution.thread_id is None

    @pytest.mark.asyncio
    async def test_related_thread_resolution_marks_event_lookup_failure_indeterminate(
        self,
    ) -> None:
        """Membership resolution should preserve lookup failures as indeterminate candidates."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolution = await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert resolution.candidate_thread_root_id == related_event_id
        assert isinstance(resolution.error, RuntimeError)
        assert str(resolution.error) == "lookup unavailable"

    @pytest.mark.asyncio
    async def test_thread_messages_thread_membership_access_treats_root_with_children_as_threaded(
        self,
    ) -> None:
        """Thread-message-backed access should apply the same root-children contract."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        @dataclass(frozen=True)
        class SnapshotMessage:
            event_id: str

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_messages(
            lookup_room_id: str,
            requested_thread_root_id: str,
        ) -> list[SnapshotMessage]:
            assert lookup_room_id == room_id
            assert requested_thread_root_id == thread_root_id
            return [
                SnapshotMessage(event_id=thread_root_id),
                SnapshotMessage(event_id="$child:localhost"),
            ]

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
    async def test_thread_messages_thread_membership_access_strict_resolution_propagates_event_lookup_failure(
        self,
    ) -> None:
        """Strict resolution should surface unavailable related-event lookups."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"
        plain_reply_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": related_event_id}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def fetch_thread_messages(_room_id: str, _thread_root_id: str) -> list[object]:
            msg = "root proof should not run when event lookup fails"
            raise AssertionError(msg)

        resolution = await resolve_event_thread_membership(
            room_id,
            plain_reply_event_info,
            access=thread_messages_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_messages=fetch_thread_messages,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert isinstance(resolution.error, RuntimeError)
        assert str(resolution.error) == "lookup unavailable"

    @pytest.mark.asyncio
    async def test_thread_messages_thread_membership_access_strict_resolution_propagates_root_proof_failure(
        self,
    ) -> None:
        """Strict resolution should surface unavailable thread-root proof."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        plain_reply_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "plain reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": thread_root_id}},
                },
                "event_id": "$plain_reply:localhost",
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_messages(_room_id: str, _thread_root_id: str) -> list[object]:
            msg = "snapshot unavailable"
            raise RuntimeError(msg)

        resolution = await resolve_event_thread_membership(
            room_id,
            plain_reply_event_info,
            access=thread_messages_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_messages=fetch_thread_messages,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert isinstance(resolution.error, RuntimeError)
        assert str(resolution.error) == "snapshot unavailable"

    @pytest.mark.asyncio
    async def test_best_effort_related_thread_resolution_degrades_when_event_lookup_fails(
        self,
    ) -> None:
        """Best-effort resolution should degrade when related-event lookup is unavailable."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolved_thread_id = await resolve_related_event_thread_id_best_effort(
            room_id,
            related_event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_related_thread_resolution_preserves_candidate_when_event_lookup_fails(
        self,
    ) -> None:
        """Lookup failures should preserve the related event as an indeterminate candidate."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            msg = "lookup unavailable"
            raise RuntimeError(msg)

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolution = await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert resolution.candidate_thread_root_id == related_event_id

    @pytest.mark.asyncio
    async def test_related_thread_resolution_preserves_candidate_when_event_lookup_returns_none(
        self,
    ) -> None:
        """Missing related events should still preserve the related event as a candidate."""
        room_id = "!test:localhost"
        related_event_id = "$related:localhost"

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
            return None

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadRootProof:
            return ThreadRootProof.not_a_thread_root()

        resolution = await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=ThreadMembershipAccess(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
            ),
        )

        assert resolution.state is ThreadResolutionState.INDETERMINATE
        assert resolution.candidate_thread_root_id == related_event_id

    @pytest.mark.asyncio
    async def test_best_effort_related_thread_resolution_degrades_when_root_proof_fails(
        self,
    ) -> None:
        """Best-effort callers should treat proof failures as unknown instead of raising."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        root_event_info = EventInfo.from_event(
            {
                "content": {
                    "body": "root",
                    "msgtype": "m.text",
                },
                "event_id": thread_root_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": room_id,
                "type": "m.room.message",
            },
        )

        async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
            return None

        async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
            return root_event_info if event_id == thread_root_id else None

        async def fetch_thread_messages(_room_id: str, _thread_root_id: str) -> list[object]:
            msg = "thread history unavailable"
            raise RuntimeError(msg)

        resolved_thread_id = await resolve_related_event_thread_id_best_effort(
            room_id,
            thread_root_id,
            access=thread_messages_thread_membership_access(
                lookup_thread_id=lookup_thread_id,
                fetch_event_info=fetch_event_info,
                fetch_thread_messages=fetch_thread_messages,
            ),
        )

        assert resolved_thread_id is None

    @pytest.mark.asyncio
    async def test_live_edit_of_promoted_plain_reply_persists_event_thread_membership(
        self,
        bot: AgentBot,
    ) -> None:
        """Edits of promoted plain replies should keep the same durable thread membership."""
        room_id = "!test:localhost"
        thread_root_id = "$thread_root:localhost"
        thread_reply_id = "$thread_reply:localhost"
        plain_reply_id = "$plain_reply:localhost"
        plain_reply_edit_id = "$plain_reply_edit:localhost"

        real_event_cache = SqliteEventCache(bot.storage_path / "plain-reply-edit-thread-membership.db")
        await real_event_cache.initialize()
        bot.event_cache = real_event_cache
        bot.event_cache_write_coordinator = EventCacheWriteCoordinator(
            logger=MagicMock(),
            background_task_owner=bot._runtime_view,
        )
        try:
            await real_event_cache.store_event(
                thread_reply_id,
                room_id,
                {
                    "content": {
                        "body": "Thread reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": thread_root_id,
                        },
                    },
                    "event_id": thread_reply_id,
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567894,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            plain_reply_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "bridged plain reply",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": thread_reply_id}},
                    },
                    "event_id": plain_reply_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567895,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )
            await bot._conversation_cache.append_live_event(
                room_id,
                plain_reply_event,
                event_info=EventInfo.from_event(plain_reply_event.source),
            )
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            edit_event = nio.RoomMessageText.from_dict(
                {
                    "content": {
                        "body": "* updated bridged plain reply",
                        "msgtype": "m.text",
                        "m.new_content": {
                            "body": "updated bridged plain reply",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {"rel_type": "m.replace", "event_id": plain_reply_id},
                    },
                    "event_id": plain_reply_edit_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1234567896,
                    "room_id": room_id,
                    "type": "m.room.message",
                },
            )

            await bot._conversation_cache.append_live_event(
                room_id,
                edit_event,
                event_info=EventInfo.from_event(edit_event.source),
            )
            await _wait_for_room_cache_idle(bot.event_cache_write_coordinator)

            assert await real_event_cache.get_thread_id_for_event(room_id, plain_reply_edit_id) == thread_root_id
        finally:
            await real_event_cache.close()
