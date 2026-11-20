"""One turn loads its thread from the journal once, and again only when the thread changed since.

A turn reads its thread when it is dispatched and again once it holds the reply lock. The second read must see what
arrived since the first, so it checks the journal's row index and loads the thread again only when that shows a change.
The summary pass after the answer always loads the thread, and is left out here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.constants import FORK_CONTENT_KEY
from mindroom.event_journal import EventClass, EventKind, InboundEvent, ProjectedEvent, reads
from mindroom.event_journal.views import ConversationReadView
from mindroom.matrix import conversation_reads
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import complete_thread_history, thread_is_unchanged
from mindroom.matrix.thread_history_result import thread_history_result
from tests.conftest import drain_coalescing
from tests.journal_helpers import record_room_history_recovery
from tests.response_runner_helpers import streamed_answer
from tests.threading_helpers import ThreadingBehaviorTestBase, _matrix_room, _message, seed_thread_history

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.bot import AgentBot

_ROOM = "!test:localhost"
_ROOT = "$thread_root:localhost"
_USER = "@user:localhost"
_BOT = "@mindroom_general:localhost"
_REPLY = "$response:localhost"
_FOLLOW_UP = "@mindroom_general follow-up"
_SEEDED = ["Root", "one", "two", "three"]


async def _admit(
    bot: AgentBot,
    event_id: str,
    body: str,
    *,
    ts: int,
    sender: str = _USER,
    replaces: str | None = None,
    redacts: str | None = None,
    thread_id: str = _ROOT,
) -> None:
    """Land one event of a thread in the bot's journal, as sync does: a message, an edit or a redaction."""
    content: dict[str, object] = {"msgtype": "m.text", "body": body}
    if replaces is not None:
        content = {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": replaces},
        }
    await bot._journal_store.principal(bot._journal_principal_id).admit(
        InboundEvent(
            event_id=event_id,
            room_id=_ROOM,
            thread_id=thread_id,
            kind=EventKind.MESSAGE if redacts is None else EventKind.REDACTION,
            event_class=EventClass.CONTEXT_ONLY,
            sender=sender,
            origin_server_ts=ts,
            source={"event_id": event_id, "content": content},
        ),
        ProjectedEvent(
            event_id=event_id,
            room_id=_ROOM,
            thread_id=thread_id,
            sender=sender,
            origin_server_ts=ts,
            content=content,
            replaces_event_id=replaces,
            redacts_event_id=redacts,
        ),
    )


async def _refetch_restores_two(bot: AgentBot, body: str) -> None:
    """Edit ``$two``, delete the edit, and settle the refetch it owes with ``body`` under the original revision.

    The row ends as it began in everything but its text: the same revision and time, and no refetch owed.
    """
    two = "$two:localhost"
    await _admit(bot, "$edit:localhost", "edited", ts=101, replaces=two)
    await _admit(bot, "$redact:localhost", "", ts=102, redacts="$edit:localhost")
    store = bot._journal_store.principal(bot._journal_principal_id)
    page = await store.read_conversation(room_id=_ROOM, thread_id=_ROOT, limit=50)
    (request,) = page.refresh_pending
    assert await store.install_refetched_revision(
        request,
        revision_event_id=two,
        revision_ts=2,
        revision_sender=_USER,
        content={"body": body},
    )
    page = await store.read_conversation(room_id=_ROOM, thread_id=_ROOT, limit=50)
    assert not page.refresh_pending
    (restored,) = [m for m in page.messages if m.logical_event_id == two]
    assert (restored.revision_event_id, restored.revision_ts, restored.content["body"]) == (two, 2, body)


@dataclass
class _Turn:
    """How often one turn loaded its thread before answering, and the history its model was given."""

    loads: int = 0
    model_history: list[str] = field(default_factory=list)


@asynccontextmanager
async def _thread(bot: AgentBot) -> AsyncIterator[None]:
    """Seed a hydrated four-message thread whose replies the homeserver accepts."""
    seeded = [_message(event_id=_ROOT, body=_SEEDED[0], thread_id=_ROOT)] + [
        _message(event_id=f"${body}:localhost", body=body, thread_id=_ROOT) for body in _SEEDED[1:]
    ]
    await seed_thread_history(bot, room_id=_ROOM, thread_id=_ROOT, messages=seeded)
    bot.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse.from_dict({"event_id": _REPLY}, room_id=_ROOM),
    )
    yield


async def _run_turn(
    bot: AgentBot,
    *,
    after_placeholder: Callable[[], Awaitable[None]] | None = None,
) -> _Turn:
    """Run one mentioned follow-up through dispatch and the reply.

    ``after_placeholder`` runs once the turn holds the reply lock and sent its placeholder, before it refreshes the
    thread for the model.
    """
    turn = _Turn()
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": _FOLLOW_UP,
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_BOT]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": _ROOT},
            },
            "event_id": "$follow:localhost",
            "sender": _USER,
            "origin_server_ts": 100,
            "room_id": _ROOM,
            "type": "m.room.message",
        },
    )
    load = ConversationReadView.read_conversation
    send = bot.client.room_send

    async def counted_load(self: ConversationReadView, **kwargs: Any) -> Any:  # noqa: ANN401
        if kwargs["thread_id"] == _ROOT and kwargs["limit"] == HYDRATED_PROMPT_WINDOW_MESSAGES:
            turn.loads += 1
        return await load(self, **kwargs)

    async def send_then_land(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        response = await send(*args, **kwargs)
        if send.await_count == 1 and after_placeholder is not None:
            await after_placeholder()
        return response

    async def answer(context: Any, *_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        # The model's copy of a message carries its time in front of its text.
        turn.model_history = [message.body.split("] ", 1)[-1] for message in context.thread_history]
        return "the answer"

    bot.client.room_send = AsyncMock(side_effect=send_then_land)
    # Sync lands a message in the journal before the bot is handed it.
    await _admit(bot, event.event_id, _FOLLOW_UP, ts=100)
    with (
        patch.object(ConversationReadView, "read_conversation", counted_load),
        patch("mindroom.response_runner.stream_agent_response", new=streamed_answer(answer)),
        patch("mindroom.post_response_effects.maybe_generate_thread_summary", new=AsyncMock()),
    ):
        await bot._on_message(_matrix_room(name="Test Room"), event)
        await drain_coalescing(bot)
        assert await wait_for_background_tasks(10)
    return turn


class TestOneTurnLoadsItsThreadOnce(ThreadingBehaviorTestBase):
    """A turn's later reads reuse its first one exactly when the thread did not change in between."""

    @pytest.mark.asyncio
    async def test_an_unchanged_thread_is_loaded_once(self, bot: AgentBot) -> None:
        """Dispatch loads the thread; the turn finds it unchanged once it holds the reply lock and loads nothing."""
        async with _thread(bot):
            turn = await _run_turn(bot)

        assert turn.loads == 1
        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]

    @pytest.mark.asyncio
    async def test_the_echo_of_the_turns_own_reply_is_no_change(self, bot: AgentBot) -> None:
        """The placeholder's echo lands before the lock refresh; the model never reads it, so nothing is loaded again."""
        async with _thread(bot):
            turn = await _run_turn(
                bot,
                after_placeholder=lambda: _admit(bot, _REPLY, "Thinking...", ts=101, sender=_BOT),
            )

        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]
        assert turn.loads == 1

    @pytest.mark.asyncio
    async def test_a_settled_refetch_before_the_lock_refresh_reaches_the_model(self, bot: AgentBot) -> None:
        """A refetch can store other text of the same length under the revision the dispatch read saw."""
        async with _thread(bot):
            turn = await _run_turn(bot, after_placeholder=lambda: _refetch_restores_two(bot, "owt"))

        assert turn.model_history == ["Root", "one", "owt", "three", _FOLLOW_UP]
        assert turn.loads == 2

    @pytest.mark.asyncio
    async def test_a_follow_up_landing_before_the_lock_refresh_reaches_the_model(self, bot: AgentBot) -> None:
        """A message that arrives after dispatch read the thread is in the history the model answers."""
        async with _thread(bot):
            turn = await _run_turn(bot, after_placeholder=lambda: _admit(bot, "$late:localhost", "late", ts=101))

        assert turn.model_history == [*_SEEDED, _FOLLOW_UP, "late"]
        assert turn.loads == 2

    @pytest.mark.asyncio
    async def test_an_edit_landing_before_the_lock_refresh_reaches_the_model(self, bot: AgentBot) -> None:
        """An edit adds no message, so only the changed revision tells the lock refresh to load the thread again."""
        async with _thread(bot):
            turn = await _run_turn(
                bot,
                after_placeholder=lambda: _admit(bot, "$edit:localhost", "TWO", ts=101, replaces="$two:localhost"),
            )

        assert turn.model_history == ["Root", "one", "TWO", "three", _FOLLOW_UP]
        assert turn.loads == 2

    @pytest.mark.asyncio
    async def test_a_redaction_landing_before_the_lock_refresh_reaches_the_model(self, bot: AgentBot) -> None:
        """A message deleted after dispatch read it is not in the history the model answers."""
        async with _thread(bot):
            turn = await _run_turn(
                bot,
                after_placeholder=lambda: _admit(bot, "$redact:localhost", "", ts=101, redacts="$two:localhost"),
            )

        assert turn.model_history == ["Root", "one", "three", _FOLLOW_UP]
        assert turn.loads == 2


class TestAThreadIsUnchangedOnlyWhenAStrictReadWouldRepeat(ThreadingBehaviorTestBase):
    """What lets a caller keep an earlier read: a whole history, a standing complete walk, and the same rows."""

    @pytest.mark.asyncio
    async def test_a_whole_read_of_a_thread_nothing_touched_stands(self, bot: AgentBot) -> None:
        """The read just made is what a read now would return, and so is one missing only the ignored event."""
        async with _thread(bot):
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)
            assert await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held)

            await _admit(bot, "$other:localhost", "other", ts=100)
            await _admit(bot, _REPLY, "Thinking...", ts=101, sender=_BOT)

            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held, ignoring_event_id=_REPLY)
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)
            without_reply = replace(held, messages=[m for m in held if m.event_id != _REPLY])
            assert await thread_is_unchanged(
                bot.conversation_reader,
                _ROOM,
                _ROOT,
                without_reply,
                ignoring_event_id=_REPLY,
            )
            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, without_reply)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("module", "bound"),
        [
            (conversation_reads, "HYDRATED_PROMPT_WINDOW_MESSAGES"),
            (reads, "PAGE_CONTENT_BUDGET_BYTES"),
            (reads, "_PAGE_DECODED_BUDGET_BYTES"),
        ],
    )
    async def test_an_ignored_echo_that_fills_a_page_bound_makes_the_thread_read_again(
        self,
        bot: AgentBot,
        monkeypatch: pytest.MonkeyPatch,
        module: object,
        bound: str,
    ) -> None:
        """A read counts the echo against its message limit and content budgets before the caller drops it.

        With the bound set to the smallest one the thread alone still fits, the echo makes a read's page a part of
        the thread, so the read the caller holds is not what a read now would return.
        """
        reader = bot.conversation_reader
        async with _thread(bot):
            low, high = 1, 1 << 20
            while low < high:
                monkeypatch.setattr(module, bound, (low + high) // 2)
                if (await complete_thread_history(reader, _ROOM, _ROOT)).is_full_history:
                    high = (low + high) // 2
                else:
                    low = (low + high) // 2 + 1
            monkeypatch.setattr(module, bound, low)
            held = await complete_thread_history(reader, _ROOM, _ROOT)
            assert held.is_full_history
            assert await thread_is_unchanged(reader, _ROOM, _ROOT, held, ignoring_event_id=_REPLY)

            await _admit(bot, _REPLY, "Thinking...", ts=101, sender=_BOT)

            assert not (await complete_thread_history(reader, _ROOM, _ROOT)).is_full_history
            assert not await thread_is_unchanged(reader, _ROOM, _ROOT, held, ignoring_event_id=_REPLY)

    @pytest.mark.asyncio
    async def test_two_bodies_stored_one_after_the_other_get_different_generations(self, bot: AgentBot) -> None:
        """A message deleted and stored again must not repeat its generation, however close the two are."""
        store = bot._journal_store.principal(bot._journal_principal_id)
        await seed_thread_history(bot, room_id=_ROOM, thread_id=_ROOT, messages=[])
        await _admit(bot, _ROOT, "Root", ts=1)
        await _admit(bot, "$one:localhost", "one", ts=2)

        fingerprint = await store.thread_fingerprint(room_id=_ROOM, thread_id=_ROOT)
        assert fingerprint is not None
        assert len({generation for *_, generation in fingerprint[1]}) == 2

    @pytest.mark.asyncio
    async def test_an_edit_takes_a_generation_no_other_body_took(self, bot: AgentBot) -> None:
        """An edit numbers its body from the same count as a new message, so a message stored again repeats none."""
        store = bot._journal_store.principal(bot._journal_principal_id)
        await seed_thread_history(bot, room_id=_ROOM, thread_id=_ROOT, messages=[])
        await _admit(bot, _ROOT, "Root", ts=1)
        await _admit(bot, "$one:localhost", "one", ts=2)
        await _admit(bot, "$edit:localhost", "one, edited", ts=3, replaces="$one:localhost")
        await _admit(bot, "$two:localhost", "two", ts=4)

        fingerprint = await store.thread_fingerprint(room_id=_ROOM, thread_id=_ROOT)
        assert fingerprint is not None
        assert len({generation for *_, generation in fingerprint[1]}) == 3

    @pytest.mark.asyncio
    async def test_a_history_that_is_not_the_whole_thread_never_stands(self, bot: AgentBot) -> None:
        """A page of a longer thread and a plain list prove nothing about the rest of the thread."""
        async with _thread(bot):
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)

            partial = thread_history_result(held, is_full_history=False)
            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, partial)
            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, list(held))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("walk", ["none", "stopped short"])
    async def test_a_thread_no_walk_vouches_for_is_read_again(self, bot: AgentBot, walk: str) -> None:
        """Without a marker a strict read walks the server; after a walk that stopped short it is not whole."""
        seeded = [_message(event_id=_ROOT, body="Root", thread_id=_ROOT)]
        await seed_thread_history(bot, room_id=_ROOM, thread_id=_ROOT, messages=seeded, hydrated=False)
        store = bot._journal_store.principal(bot._journal_principal_id)
        if walk == "stopped short":
            await store.install_hydrated_conversation(
                room_id=_ROOM,
                thread_id=_ROOT,
                events=(),
                complete=False,
                expected_membership_epoch=await store.membership_epoch(_ROOM),
            )
        held = thread_history_result(seeded, is_full_history=True)

        assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repair_stopped_short", [False, True])
    async def test_a_gap_in_the_rooms_history_makes_the_thread_read_again(
        self,
        bot: AgentBot,
        repair_stopped_short: bool,
    ) -> None:
        """A skipped sync interval may hide a message of the thread, awaiting its repair and after one that gave up."""
        async with _thread(bot):
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)
            store = bot._journal_store.principal(bot._journal_principal_id)

            recovery = await record_room_history_recovery(store, _ROOM)
            assert recovery is not None
            if repair_stopped_short:
                await store.settle_room_history_recovery(
                    recovery,
                    exhausted_server=False,
                    unreadable=False,
                    attempted_policy_rank=0,
                    expected_membership_epoch=await store.membership_epoch(_ROOM),
                )
                assert await store.conversation_is_hydrated(room_id=_ROOM, thread_id=_ROOT)

            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held)

    @pytest.mark.asyncio
    async def test_a_fork_is_read_again_because_its_parent_can_change_without_it(self, bot: AgentBot) -> None:
        """A fork's own rows stay the same while the fork point lands in its parent."""
        parent, fork = "$parent:localhost", "$fork:localhost"
        reference = {FORK_CONTENT_KEY: {"thread_id": parent, "event_id": "$point:localhost"}}
        fork_root = ResolvedVisibleMessage.synthetic(
            sender=_USER,
            body="Fork",
            event_id=fork,
            content={"msgtype": "m.text", "body": "Fork", **reference},
            thread_id=fork,
        )
        await seed_thread_history(
            bot,
            room_id=_ROOM,
            thread_id=parent,
            messages=[_message(event_id=parent, body="Parent", thread_id=parent)],
        )
        await seed_thread_history(bot, room_id=_ROOM, thread_id=fork, messages=[fork_root])
        resolver = bot._conversation_resolver
        held = await resolver.fetch_thread_history(_ROOM, fork)
        assert [message.body for message in held] == ["Fork"]
        assert held.is_full_history

        await _admit(bot, "$point:localhost", "the fork point", ts=5, thread_id=parent)

        again = await resolver.fetch_thread_history(_ROOM, fork, held=held)
        assert [message.body for message in again] == ["Parent", "the fork point", "Fork"]
