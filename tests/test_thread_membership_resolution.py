"""Transitive thread membership resolution and thread-root proofs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import thread_root
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadResolution,
    ThreadResolutionState,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
    thread_messages_thread_membership_access,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROOM_ID = "!test:localhost"


def _message(content: dict[str, object], *, event_type: str = "m.room.message") -> EventInfo:
    return EventInfo.from_event({"type": event_type, "content": content})


def _reply(body: str, *, to: str) -> EventInfo:
    return _message({"body": body, "msgtype": "m.text", "m.relates_to": {"m.in_reply_to": {"event_id": to}}})


def _edit_claiming_a_thread(*, replaces: str) -> EventInfo:
    """Return one edit whose ``m.new_content`` names a thread the edit's author chose."""
    return _message(
        {
            "body": "* updated",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "updated",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$claimed:localhost"},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": replaces},
        },
    )


async def _no_thread(_room_id: str, _event_id: str) -> str | None:
    return None


async def _no_event(_room_id: str, _event_id: str) -> EventInfo | None:
    return None


async def _failing_event_lookup(_room_id: str, _event_id: str) -> EventInfo | None:
    msg = "lookup unavailable"
    raise RuntimeError(msg)


async def _not_a_thread_root(_room_id: str, _thread_root_id: str) -> ThreadResolution:
    return ThreadResolution._room_level()


def _access(
    *,
    lookup_thread_id: Callable[[str, str], Awaitable[str | None]] = _no_thread,
    fetch_event_info: Callable[[str, str], Awaitable[EventInfo | None]] = _no_event,
    prove_thread_root: Callable[[str, str], Awaitable[ThreadResolution]] = _not_a_thread_root,
) -> ThreadMembershipAccess:
    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        prove_thread_root=prove_thread_root,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_thread_root", ["", 7])
async def test_malformed_thread_relation_resolves_room_level_like_the_journal(malformed_thread_root: object) -> None:
    """A thread relation naming no event must resolve room level, as the journal admitted it.

    ``event_journal.projection.thread_root`` keeps only a non-empty string, so an
    ``m.relates_to`` carrying ``""`` or a non-string lands in the journal as a
    room-level row. Resolving the same event as ``THREADED`` here would split the
    live turn away from the durable projection of the very event that admitted it.
    """
    content = {
        "body": "malformed thread relation",
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.thread", "event_id": malformed_thread_root},
    }
    assert thread_root(content) is None

    async def prove_thread_root(_room_id: str, thread_root_id: str) -> ThreadResolution:
        return ThreadResolution._threaded(thread_root_id)

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _message(content),
        access=_access(prove_thread_root=prove_thread_root),
        event_id="$malformed:localhost",
    )

    assert resolution.state is ThreadResolutionState.ROOM_LEVEL
    assert resolution.thread_id is None


@pytest.mark.asyncio
async def test_transitive_thread_membership_handles_long_reply_chains() -> None:
    """The shared transitive resolver should handle reply chains longer than the old 32-hop ceiling."""
    thread_root_id = "$thread_root:localhost"
    threaded_event_id = "$thread_reply:localhost"
    event_infos: dict[str, EventInfo] = {
        threaded_event_id: _message(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
            },
        ),
    }
    for index in range(1, 34):
        reply_target_id = threaded_event_id if index == 1 else f"$plain_reply_{index - 1}:localhost"
        event_infos[f"$plain_reply_{index}:localhost"] = _reply(f"plain reply {index}", to=reply_target_id)

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return event_infos.get(event_id)

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        event_infos["$plain_reply_33:localhost"],
        access=_access(fetch_event_info=fetch_event_info),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == thread_root_id


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_follows_reaction_target_transitively() -> None:
    """The shared entrypoint should inherit thread membership across reaction targets too."""
    thread_root_id = "$thread_root:localhost"
    thread_reply_id = "$thread_reply:localhost"
    plain_reply_id = "$plain_reply:localhost"
    reaction_event = _message(
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": plain_reply_id, "key": "👍"}},
        event_type="m.reaction",
    )
    event_infos: dict[str, EventInfo] = {
        thread_reply_id: _message(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
            },
        ),
        plain_reply_id: _reply("plain reply", to=thread_reply_id),
    }

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return event_infos.get(event_id)

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        reaction_event,
        access=_access(fetch_event_info=fetch_event_info),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == thread_root_id


@pytest.mark.asyncio
async def test_edit_resolves_through_its_original_not_the_thread_it_names() -> None:
    """The thread inside an edit's ``m.new_content`` loses to the thread of the event it edits.

    Matrix applies ``m.new_content`` by keeping the original event's relation and ignoring
    every ``m.relates_to`` written there, so that value is a claim its author chose. Answering
    from it would let anyone who can edit a message declare which conversation the edit joins.
    """
    original_id = "$original:localhost"
    original_event_info = _message(
        {
            "body": "original",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$real_thread:localhost"},
        },
    )

    async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
        return "$real_thread:localhost" if event_id == original_id else None

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return original_event_info if event_id == original_id else None

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _edit_claiming_a_thread(replaces=original_id),
        access=_access(lookup_thread_id=lookup_thread_id, fetch_event_info=fetch_event_info),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == "$real_thread:localhost"


@pytest.mark.asyncio
async def test_walking_onto_an_edit_keeps_walking_to_the_message_it_replaces() -> None:
    """Reaching an edit mid-walk resolves the edited message, not the thread the edit names.

    Implied membership makes this reachable from any plain reply or reaction: the walk lands on
    a replacement, and the replacement's ``m.new_content`` is the one relation on the path that
    Matrix throws away. The hop to the original is what the walk exists for.
    """
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    edit_event_info = _edit_claiming_a_thread(replaces=original_id)

    async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
        return "$real_thread:localhost" if event_id == original_id else None

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return edit_event_info if event_id == edit_id else None

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _reply("a reply", to=edit_id),
        access=_access(lookup_thread_id=lookup_thread_id, fetch_event_info=fetch_event_info),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == "$real_thread:localhost"


@pytest.mark.asyncio
async def test_edit_of_an_unreadable_original_is_indeterminate_not_the_thread_it_names() -> None:
    """An edit whose original nobody can read is unplaced, not placed where the edit says.

    This is the case the claim used to answer, and answering it is exactly what an outsider
    wants: the original is the only authority, and it is unavailable. Fail closed on the
    candidate so dispatch coalesces and retries rather than acting on an attacker's choice.
    """
    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _edit_claiming_a_thread(replaces="$original:localhost"),
        access=_access(),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.thread_id is None


@pytest.mark.asyncio
async def test_related_thread_resolution_marks_event_lookup_failure_indeterminate() -> None:
    """Membership resolution should preserve lookup failures as indeterminate candidates."""
    resolution = await resolve_related_event_thread_membership(
        ROOM_ID,
        "$related:localhost",
        access=_access(fetch_event_info=_failing_event_lookup),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.thread_id is None
    assert isinstance(resolution.error, RuntimeError)
    assert str(resolution.error) == "lookup unavailable"


@pytest.mark.asyncio
async def test_related_thread_resolution_preserves_candidate_when_event_lookup_returns_none() -> None:
    """Missing related events should still preserve the related event as a candidate."""
    resolution = await resolve_related_event_thread_membership(ROOM_ID, "$related:localhost", access=_access())

    assert resolution.state is ThreadResolutionState.INDETERMINATE


@pytest.mark.asyncio
async def test_thread_messages_thread_membership_access_treats_root_with_children_as_threaded() -> None:
    """Thread-message-backed access should apply the same root-children contract."""
    thread_root_id = "$thread_root:localhost"
    root_event_info = _message({"body": "root", "msgtype": "m.text"})

    @dataclass(frozen=True)
    class SnapshotMessage:
        event_id: str

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return root_event_info if event_id == thread_root_id else None

    async def fetch_thread_messages(
        lookup_room_id: str,
        requested_thread_root_id: str,
    ) -> list[SnapshotMessage]:
        assert lookup_room_id == ROOM_ID
        assert requested_thread_root_id == thread_root_id
        return [
            SnapshotMessage(event_id=thread_root_id),
            SnapshotMessage(event_id="$child:localhost"),
        ]

    resolution = await resolve_related_event_thread_membership(
        ROOM_ID,
        thread_root_id,
        access=thread_messages_thread_membership_access(
            lookup_thread_id=_no_thread,
            fetch_event_info=fetch_event_info,
            fetch_thread_messages=fetch_thread_messages,
            absence_needs_full_history=True,
        ),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == thread_root_id


@pytest.mark.asyncio
async def test_thread_messages_thread_membership_access_strict_resolution_propagates_event_lookup_failure() -> None:
    """Strict resolution should surface unavailable related-event lookups."""

    async def fetch_thread_messages(_room_id: str, _thread_root_id: str) -> list[object]:
        msg = "root proof should not run when event lookup fails"
        raise AssertionError(msg)

    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _reply("plain reply", to="$related:localhost"),
        access=thread_messages_thread_membership_access(
            lookup_thread_id=_no_thread,
            fetch_event_info=_failing_event_lookup,
            fetch_thread_messages=fetch_thread_messages,
            absence_needs_full_history=True,
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert isinstance(resolution.error, RuntimeError)
    assert str(resolution.error) == "lookup unavailable"


def _access_whose_root_proof_raises(thread_root_id: str) -> ThreadMembershipAccess:
    """Return access serving one relation-free root whose thread read raises."""
    root_event_info = _message({"body": "root", "msgtype": "m.text"})

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return root_event_info if event_id == thread_root_id else None

    async def fetch_thread_messages(_room_id: str, _thread_root_id: str) -> list[object]:
        msg = "snapshot unavailable"
        raise RuntimeError(msg)

    return thread_messages_thread_membership_access(
        lookup_thread_id=_no_thread,
        fetch_event_info=fetch_event_info,
        fetch_thread_messages=fetch_thread_messages,
        absence_needs_full_history=True,
    )


@pytest.mark.asyncio
async def test_thread_messages_thread_membership_access_strict_resolution_propagates_root_proof_failure() -> None:
    """A thread read that raises leaves the proof unavailable on the candidate root, never room level."""
    resolution = await resolve_event_thread_membership(
        ROOM_ID,
        _reply("plain reply", to="$thread_root:localhost"),
        access=_access_whose_root_proof_raises("$thread_root:localhost"),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.thread_id is None
    assert isinstance(resolution.error, RuntimeError)
    assert str(resolution.error) == "snapshot unavailable"


@pytest.mark.asyncio
async def test_related_root_whose_thread_read_fails_is_indeterminate() -> None:
    """A related event that is itself the candidate root stays unplaced when its thread cannot be read."""
    resolution = await resolve_related_event_thread_membership(
        ROOM_ID,
        "$thread_root:localhost",
        access=_access_whose_root_proof_raises("$thread_root:localhost"),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.thread_id is None
