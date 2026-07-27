"""Which replacement events are legitimate.

A bounded or collapsed read picks the newest replacement candidate for each message and says
nothing about whether that candidate had any right to replace it. These tests pin the rules that
decide the winner.

Invariant: a Matrix replacement is applied only when it preserves one immutable event identity,
comes from the sender of the event it replaces, and - among equally-timestamped candidates from
that sender - is the one with the lexicographically greatest event ID.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    ThreadEditCandidates,
    apply_latest_edits_to_messages,
)
from mindroom.matrix.event_info import EventInfo
from tests.threading_helpers import _text_event

if TYPE_CHECKING:
    import nio

_AUTHOR = "@author:localhost"
_IMPOSTOR = "@impostor:localhost"
_ORIGINAL_ID = "$original"


def _original_message(*, sender: str = _AUTHOR, body: str = "original") -> ResolvedVisibleMessage:
    """Return the resolved message a replacement will be matched against."""
    return ResolvedVisibleMessage(
        sender=sender,
        body=body,
        timestamp=1_000,
        event_id=_ORIGINAL_ID,
        content={"body": body, "msgtype": "m.text"},
        thread_id=None,
        latest_event_id=_ORIGINAL_ID,
    )


def _record(candidates: ThreadEditCandidates, event: nio.RoomMessageText) -> None:
    candidates.record(event, event_info=EventInfo.from_event(event.source))


async def _apply(
    candidates: ThreadEditCandidates,
    messages: dict[str, ResolvedVisibleMessage],
) -> None:
    await apply_latest_edits_to_messages(
        AsyncMock(),
        messages_by_event_id=messages,
        edit_candidates=candidates,
        trusted_sender_ids=(_AUTHOR, _IMPOSTOR),
    )


class TestReplacementSenderRule:
    """A replacement is only an edit when it comes from the original's sender."""

    @pytest.mark.asyncio
    async def test_foreign_replacement_does_not_rewrite_another_members_message(self) -> None:
        """A room member cannot rewrite someone else's message as the agent reads it."""
        candidates = ThreadEditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$forged",
                body="* forged",
                new_body="forged",
                sender=_IMPOSTOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "original"
        assert messages[_ORIGINAL_ID].latest_event_id == _ORIGINAL_ID

    @pytest.mark.asyncio
    async def test_newer_foreign_replacement_does_not_hide_the_authors_own_edit(self) -> None:
        """A foreign candidate must not shadow the newest legitimate one.

        Keeping a single global newest candidate would drop the author's edit on the floor the
        moment anyone else sent a later replacement for the same event.
        """
        candidates = ThreadEditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$authored",
                body="* authored",
                new_body="authored",
                sender=_AUTHOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        _record(
            candidates,
            _text_event(
                event_id="$forged",
                body="* forged",
                new_body="forged",
                sender=_IMPOSTOR,
                server_timestamp=9_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "authored"
        assert messages[_ORIGINAL_ID].latest_event_id == "$authored"

    @pytest.mark.asyncio
    async def test_synthesized_missing_original_keeps_the_editors_own_sender(self) -> None:
        """An unseen original cannot be impersonated: the synthesized message is the editor's."""
        candidates = ThreadEditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$orphan-edit",
                body="* orphaned",
                new_body="orphaned",
                sender=_IMPOSTOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages: dict[str, ResolvedVisibleMessage] = {}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].sender == _IMPOSTOR
        assert messages[_ORIGINAL_ID].body == "orphaned"


class TestReplacementWinnerSelection:
    """Which of several legitimate candidates wins."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reversed_arrival", [False, True])
    async def test_equal_timestamps_break_on_greatest_event_id(self, reversed_arrival: bool) -> None:
        """Equally-timestamped replacements resolve by lexicographically greatest event ID."""
        events = [
            _text_event(
                event_id="$edit-aaa",
                body="* first",
                new_body="first",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
            _text_event(
                event_id="$edit-zzz",
                body="* second",
                new_body="second",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
        ]
        candidates = ThreadEditCandidates()
        for event in reversed(events) if reversed_arrival else events:
            _record(candidates, event)
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].latest_event_id == "$edit-zzz"
        assert messages[_ORIGINAL_ID].body == "second"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reversed_arrival", [False, True])
    async def test_duplicate_observations_of_one_edit_are_idempotent(self, reversed_arrival: bool) -> None:
        """The same replacement seen twice resolves identically in either arrival order."""
        duplicate = _text_event(
            event_id="$edit",
            body="* edited",
            new_body="edited",
            sender=_AUTHOR,
            server_timestamp=5_000,
            replacement_of=_ORIGINAL_ID,
        )
        older = _text_event(
            event_id="$edit-older",
            body="* older",
            new_body="older",
            sender=_AUTHOR,
            server_timestamp=4_000,
            replacement_of=_ORIGINAL_ID,
        )
        candidates = ThreadEditCandidates()
        for event in [duplicate, older, duplicate] if reversed_arrival else [older, duplicate, duplicate]:
            _record(candidates, event)
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].latest_event_id == "$edit"
        assert messages[_ORIGINAL_ID].body == "edited"

    @pytest.mark.asyncio
    async def test_contradictory_payloads_for_one_edit_id_resolve_deterministically(self) -> None:
        """Two payloads claiming one edit event ID must not depend on arrival order."""
        first_payload = _text_event(
            event_id="$edit",
            body="* alpha",
            new_body="alpha",
            sender=_AUTHOR,
            server_timestamp=5_000,
            replacement_of=_ORIGINAL_ID,
        )
        second_payload = _text_event(
            event_id="$edit",
            body="* beta",
            new_body="beta",
            sender=_AUTHOR,
            server_timestamp=5_000,
            replacement_of=_ORIGINAL_ID,
        )

        forward: dict[str, ResolvedVisibleMessage] = {_ORIGINAL_ID: _original_message()}
        forward_candidates = ThreadEditCandidates()
        _record(forward_candidates, first_payload)
        _record(forward_candidates, second_payload)
        await _apply(forward_candidates, forward)

        backward: dict[str, ResolvedVisibleMessage] = {_ORIGINAL_ID: _original_message()}
        backward_candidates = ThreadEditCandidates()
        _record(backward_candidates, second_payload)
        _record(backward_candidates, first_payload)
        await _apply(backward_candidates, backward)

        assert forward[_ORIGINAL_ID].body == backward[_ORIGINAL_ID].body
        assert forward[_ORIGINAL_ID].latest_event_id == "$edit"

    @pytest.mark.asyncio
    async def test_malformed_newest_replacement_leaves_the_original_intact(self) -> None:
        """A newest candidate with no usable new content must not blank the message."""
        malformed = _text_event(
            event_id="$edit-malformed",
            body="* malformed",
            sender=_AUTHOR,
            server_timestamp=9_000,
            replacement_of=_ORIGINAL_ID,
        )
        malformed.source["content"].pop("m.new_content")
        candidates = ThreadEditCandidates()
        _record(candidates, malformed)
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "original"
        assert messages[_ORIGINAL_ID].latest_event_id == _ORIGINAL_ID
