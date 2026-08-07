"""Transitive thread membership resolution and thread-root proofs."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadResolutionState,
    _ThreadRootProof,
    resolve_event_thread_membership,
    resolve_related_event_thread_id_best_effort,
    resolve_related_event_thread_membership,
    room_scan_thread_membership_access,
    thread_messages_thread_membership_access,
)
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos
from tests.threading_helpers import (
    ThreadingBehaviorTestBase,
)


class TestThreadingBehavior(ThreadingBehaviorTestBase):
    """Threading behavior tests moved verbatim from tests/test_threading_error.py."""

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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

        async def prove_thread_root(_room_id: str, _thread_root_id: str) -> _ThreadRootProof:
            return _ThreadRootProof.not_a_thread_root()

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
