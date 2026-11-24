"""Which replacement events are legitimate.

A bounded or collapsed read picks the newest replacement candidate for each message and says
nothing about whether that candidate had any right to replace it. These tests pin the rules that
decide the winner.

Invariant: a Matrix replacement is applied only when it preserves one immutable event identity,
comes from the sender of the event it replaces, and - among equally-timestamped candidates from
that sender - is the one with the lexicographically greatest event ID.

Invariant: a message reconstructed from a replacement alone claims only what the replacement
proves. Its thread is not one of those things, so it is reported as unknown rather than as absent.

Invariant: a replacement never places anything. Applying ``m.new_content`` keeps the original
event's relation and ignores every ``m.relates_to`` inside the replacement, so a thread named there
is a claim: it neither moves a message that was read nor places one that was reconstructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.matrix.client_visible_messages import (
    EditCandidates,
    ResolvedVisibleMessage,
    apply_latest_edits_to_messages,
    bundled_replacement_candidates,
    is_visible_room_message,
)
from mindroom.matrix.conversation_reads import ConversationReader, complete_thread_history
from mindroom.matrix.event_info import EventInfo
from tests.test_conversation_hydration import ROOM, FakeClient, admit_all, hydrator, raw
from tests.threading_helpers import _emote_event, _image_event, _text_event

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore
    from mindroom.matrix.message_content import VisibleRoomMessage

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


def _record(candidates: EditCandidates, event: VisibleRoomMessage) -> None:
    # The pool gate is asserted rather than assumed. `record` itself type-checks
    # nothing, so every caller in production decides membership first with
    # `is_visible_room_message`; a fixture that skipped that would be able to
    # rank a replacement no read would ever offer, and these tests would keep
    # passing for a msgtype the reads had dropped.
    assert is_visible_room_message(event), "a replacement a read would not admit cannot be ranked here"
    candidates.record(event, event_info=EventInfo.from_event(event.source))


async def _apply(
    candidates: EditCandidates,
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
        candidates = EditCandidates()
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
        candidates = EditCandidates()
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
        candidates = EditCandidates()
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


class TestSynthesizedMessagePlacement:
    """Where a message reconstructed from a replacement alone is said to live."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("new_thread_id", [None, "$root"], ids=["silent-edit", "edit-naming-a-thread"])
    async def test_an_edit_alone_leaves_the_synthesized_placement_unknown(self, new_thread_id: str | None) -> None:
        """An edit, whether or not it names a thread, must not place the message it reconstructs.

        A replacement inherits the original's ``m.relates_to`` instead of restating it, so a
        threaded reply's edit carries no thread and neither does the window once the original has
        scrolled out of it. Reporting ``thread_id=None`` there tells a reader the reply was posted
        at room level, and the reader answers it in the room rather than in its thread.

        Applying ``m.new_content`` keeps the original event's relation and ignores every
        ``m.relates_to`` written inside the replacement, so a thread named there is a claim no rule
        turns into a fact. Believing it lets anyone who can send an edit file the message it
        reconstructs under a thread of their choosing.
        """
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit",
                body="* final answer",
                new_body="final answer",
                sender=_AUTHOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
                new_thread_id=new_thread_id,
            ),
        )
        messages: dict[str, ResolvedVisibleMessage] = {}

        await _apply(candidates, messages)

        synthesized = messages[_ORIGINAL_ID]
        assert "thread_id" not in synthesized.to_dict()
        assert synthesized.to_dict()["thread_id_unknown"] is True
        assert synthesized.thread_id_known is False
        assert synthesized.thread_id is None

    @pytest.mark.asyncio
    async def test_message_read_with_its_edit_keeps_its_own_thread_and_position(self) -> None:
        """A message that was read is placed by itself, whatever its edit does or does not say."""
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit",
                body="* final answer",
                new_body="final answer",
                sender=_AUTHOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        original = _original_message()
        original.thread_id = "$root"
        messages = {_ORIGINAL_ID: original}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].to_dict()["thread_id"] == "$root"
        assert messages[_ORIGINAL_ID].timestamp == 1_000
        assert messages[_ORIGINAL_ID].edited_timestamp == 2_000

    @pytest.mark.asyncio
    async def test_edit_naming_another_thread_does_not_move_the_message_it_edits(self) -> None:
        """A replacement cannot relocate a message that was actually read.

        The original's own ``m.relates_to`` is the only thing that places it, and an edit inherits
        that relation rather than restating it. An edit that names a different thread is therefore
        either a client bug or an attempt to drag a conversation somewhere its participants cannot
        see it, and both answers are the same: leave the message where its own event put it.
        """
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit",
                body="* final answer",
                new_body="final answer",
                sender=_AUTHOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
                new_thread_id="$attacker-thread",
            ),
        )
        original = _original_message()
        original.thread_id = "$root"
        messages = {_ORIGINAL_ID: original}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].to_dict()["thread_id"] == "$root"
        assert messages[_ORIGINAL_ID].body == "final answer"

    @pytest.mark.asyncio
    async def test_edit_naming_a_thread_does_not_file_a_room_level_message_into_it(self) -> None:
        """A room-level message stays at room level however its edit is decorated."""
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit",
                body="* final answer",
                new_body="final answer",
                sender=_AUTHOR,
                server_timestamp=2_000,
                replacement_of=_ORIGINAL_ID,
                new_thread_id="$attacker-thread",
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].to_dict()["thread_id"] is None
        assert messages[_ORIGINAL_ID].thread_id_known is True
        assert messages[_ORIGINAL_ID].body == "final answer"


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
        candidates = EditCandidates()
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
        candidates = EditCandidates()
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
        forward_candidates = EditCandidates()
        _record(forward_candidates, first_payload)
        _record(forward_candidates, second_payload)
        await _apply(forward_candidates, forward)

        backward: dict[str, ResolvedVisibleMessage] = {_ORIGINAL_ID: _original_message()}
        backward_candidates = EditCandidates()
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
        candidates = EditCandidates()
        _record(candidates, malformed)
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "original"
        assert messages[_ORIGINAL_ID].latest_event_id == _ORIGINAL_ID


class TestEveryMsgtypeWithABodyRanksTogether:
    """An `m.emote` or `m.image` replacement is ranked against `m.text` ones, not below them.

    The candidate pool was `(RoomMessageText, RoomMessageNotice)`, then
    `RoomMessageFormatted`, so first an emote and then an image replacement was
    not a candidate at all. That is not a tie broken one way: a replacement
    nobody ranks cannot win, so the collapsed read showed whichever older
    revision happened to be in the pool -- or the unedited original when every
    replacement was outside it. The projection applies `m.replace` off the
    relation alone and never looks at the msgtype, so the two disagreed.

    Widening the pool changes ranking in exactly one direction, and the
    argument is the same for media as it was for emotes. The order is still
    `(server_timestamp, event_id)` and no msgtype carries a privilege in it, so
    the only outcomes that move are the ones where an unrankable replacement
    was being passed over. `_edit_payload_rank` is untouched and fires only on
    equal event IDs, which is one event observed twice and therefore the same
    msgtype in both copies.
    """

    @pytest.mark.asyncio
    async def test_the_newest_replacement_wins_even_when_it_is_an_emote(self) -> None:
        """A newer emote replacement outranks an older text one."""
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit-text",
                body="* superseded",
                new_body="superseded",
                sender=_AUTHOR,
                server_timestamp=4_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        _record(
            candidates,
            _emote_event(
                event_id="$edit-emote",
                body="* waves goodbye",
                new_body="waves goodbye",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "waves goodbye"
        assert messages[_ORIGINAL_ID].latest_event_id == "$edit-emote"

    @pytest.mark.asyncio
    async def test_an_older_emote_replacement_does_not_outrank_a_newer_text_one(self) -> None:
        """Admitting emotes to the pool must not promote them within it.

        The msgtype carries no privilege in the ranking, so an older emote edit
        never displaces a newer text revision.
        """
        candidates = EditCandidates()
        _record(
            candidates,
            _emote_event(
                event_id="$edit-emote",
                body="* waves",
                new_body="waves",
                sender=_AUTHOR,
                server_timestamp=4_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        _record(
            candidates,
            _text_event(
                event_id="$edit-text",
                body="* the final word",
                new_body="the final word",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "the final word"
        assert messages[_ORIGINAL_ID].latest_event_id == "$edit-text"

    @pytest.mark.asyncio
    async def test_the_newest_replacement_wins_even_when_it_is_a_picture(self) -> None:
        """A newer caption replacement outranks an older text one."""
        candidates = EditCandidates()
        _record(
            candidates,
            _text_event(
                event_id="$edit-text",
                body="* superseded",
                new_body="superseded",
                sender=_AUTHOR,
                server_timestamp=4_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        _record(
            candidates,
            _image_event(
                event_id="$edit-image",
                body="* the corrected caption",
                new_body="the corrected caption",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "the corrected caption"
        assert messages[_ORIGINAL_ID].latest_event_id == "$edit-image"
        # The media the caption belongs to comes with it. A collapsed read that
        # kept the new words and dropped `url` would describe a picture nobody
        # can see.
        assert messages[_ORIGINAL_ID].content["url"] == "mxc://localhost/picture"

    @pytest.mark.asyncio
    async def test_an_older_picture_replacement_does_not_outrank_a_newer_text_one(self) -> None:
        """Admitting media to the pool must not promote it within the pool.

        The msgtype carries no privilege in the ranking, so an older caption edit
        never displaces a newer text revision.
        """
        candidates = EditCandidates()
        _record(
            candidates,
            _image_event(
                event_id="$edit-image",
                body="* an early caption",
                new_body="an early caption",
                sender=_AUTHOR,
                server_timestamp=4_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        _record(
            candidates,
            _text_event(
                event_id="$edit-text",
                body="* the final word",
                new_body="the final word",
                sender=_AUTHOR,
                server_timestamp=5_000,
                replacement_of=_ORIGINAL_ID,
            ),
        )
        messages = {_ORIGINAL_ID: _original_message()}

        await _apply(candidates, messages)

        assert messages[_ORIGINAL_ID].body == "the final word"
        assert messages[_ORIGINAL_ID].latest_event_id == "$edit-text"
        assert "url" not in messages[_ORIGINAL_ID].content, "the losing revision's media must not survive it"


class TestBundledReplacementPrecedence:
    """A bundled `m.replace` is read as the candidate that claims to be the newest."""

    @staticmethod
    def _bundled(*, under_unsigned: bool) -> dict[str, object]:
        """Return one source whose bundle carries both keys, disagreeing."""

        def _replacement(event_id: str, body: str) -> dict[str, object]:
            return {
                "event_id": event_id,
                "sender": _AUTHOR,
                "type": "m.room.message",
                "origin_server_ts": 5_000,
                "content": {
                    "msgtype": "m.text",
                    "body": f"* {body}",
                    "m.new_content": {"msgtype": "m.text", "body": body},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": _ORIGINAL_ID},
                },
            }

        relations = {
            "m.replace": {
                "event": _replacement("$stale", "stale"),
                "latest_event": _replacement("$newest", "newest"),
            },
        }
        source: dict[str, object] = {
            "event_id": _ORIGINAL_ID,
            "sender": _AUTHOR,
            "type": "m.room.message",
            "origin_server_ts": 1_000,
            "content": {"msgtype": "m.text", "body": "original"},
        }
        if under_unsigned:
            source["unsigned"] = {"m.relations": relations}
        else:
            source["m.relations"] = relations
        return source

    @pytest.mark.parametrize("under_unsigned", [True, False])
    def test_the_latest_event_wins_wherever_the_bundle_is_carried(self, *, under_unsigned: bool) -> None:
        """`latest_event` is the answer to the question a reader is asking.

        `event` is whichever replacement the server chose to include; only
        `latest_event` claims to be the most recent one. A source can carry
        both, and they can disagree.
        """
        candidates = bundled_replacement_candidates(self._bundled(under_unsigned=under_unsigned))

        assert [candidate["event_id"] for candidate in candidates[:2]] == ["$newest", "$stale"]


class TestTheProjectionShowsWhatTheRoomPublished:
    """A thread read from the conversation projection, the reader tools and turns share."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("admitted_live", [False, True])
    async def test_new_content_on_a_message_that_replaces_nothing_is_not_its_body(
        self,
        journal_store: EventJournalStore,
        *,
        admitted_live: bool,
    ) -> None:
        """`m.new_content` on an event with no `m.replace` relation is not a body.

        Any sender can ship a second body inside a message that revises nothing.
        A read that showed it would hand a model a message disagreeing with the
        body the room displays, whether the thread arrived by sync or by a walk.
        """
        principal = journal_store.principal("agent@general")
        picture = {"url": "mxc://example.org/picture", "info": {"mimetype": "image/png", "w": 8, "h": 8}}
        root = raw("$root", "the question", ts=1_000)
        image = {
            "event_id": "$image",
            "sender": _IMPOSTOR,
            "origin_server_ts": 2_000,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.image",
                "body": "the caption the room shows",
                **picture,
                "m.new_content": {"msgtype": "m.image", "body": "a body nothing published", **picture},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
            },
        }
        if admitted_live:
            await admit_all(principal, [root, image])
        client = FakeClient(events={"$root": root, "$image": image}, relations={"$root": [image]})

        history = await complete_thread_history(
            ConversationReader(store=principal, hydrator=hydrator(principal, client)),
            ROOM,
            "$root",
        )

        picture_message = next(message for message in history if message.event_id == "$image")
        assert picture_message.body == "the caption the room shows"
        assert picture_message.content["body"] == "the caption the room shows"
        assert picture_message.latest_event_id == "$image", "nothing was replaced, so nothing is a later revision"
