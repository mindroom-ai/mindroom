"""One turn loads its thread from the journal once, and again only when the thread changed since.

A turn reads its thread when it is dispatched, again once it holds the reply lock, and again when its answer queues
the summary pass. The later two must see what arrived since the first, so they check the journal's row index and load
the thread again only when it shows a change.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.constants import FORK_CONTENT_KEY
from mindroom.event_journal import EventClass, EventKind, InboundEvent, ProjectedEvent
from mindroom.event_journal.views import ConversationReadView
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import complete_thread_history, thread_is_unchanged
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.thread_summary import automatic
from tests.conftest import drain_coalescing
from tests.journal_helpers import record_room_history_recovery
from tests.response_runner_helpers import streamed_answer
from tests.threading_helpers import ThreadingBehaviorTestBase, _matrix_room, _message, seed_thread_history

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

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


@dataclass
class _Turn:
    """What one turn loaded and what its model and its summary pass were given."""

    loads: int = 0
    model_history: list[str] = field(default_factory=list)
    summary_history: list[str] = field(default_factory=list)


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
    while_answering: Callable[[], Awaitable[None]] | None = None,
) -> _Turn:
    """Run one mentioned follow-up through dispatch, the reply and the summary pass.

    ``after_placeholder`` runs once the turn holds the reply lock and sent its placeholder, before it refreshes the
    thread for the model. ``while_answering`` runs while the model answers, before the summary pass.
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
        if while_answering is not None:
            await while_answering()
        return "the answer"

    def summary_state(history: Sequence[ResolvedVisibleMessage], **_kwargs: Any) -> None:  # noqa: ANN401
        turn.summary_history = [message.body for message in history]

    bot.client.room_send = AsyncMock(side_effect=send_then_land)
    # Sync lands a message in the journal before the bot is handed it.
    await _admit(bot, event.event_id, _FOLLOW_UP, ts=100)
    with (
        patch.object(ConversationReadView, "read_conversation", counted_load),
        patch("mindroom.response_runner.stream_agent_response", new=streamed_answer(answer)),
        patch.object(automatic, "summary_state", summary_state),
    ):
        await bot._on_message(_matrix_room(name="Test Room"), event)
        await drain_coalescing(bot)
        assert await wait_for_background_tasks(10)
    return turn


class TestOneTurnLoadsItsThreadOnce(ThreadingBehaviorTestBase):
    """A turn's later reads reuse its first one exactly when the thread did not change in between."""

    @pytest.mark.asyncio
    async def test_an_unchanged_thread_is_loaded_once(self, bot: AgentBot) -> None:
        """Dispatch loads the thread; the reply lock and the summary pass find it unchanged and load nothing."""
        async with _thread(bot):
            turn = await _run_turn(bot)

        assert turn.loads == 1
        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]
        assert turn.summary_history == [*_SEEDED, _FOLLOW_UP, "the answer"]

    @pytest.mark.asyncio
    async def test_the_echo_of_the_turns_own_reply_is_no_change(self, bot: AgentBot) -> None:
        """The placeholder's echo lands before the lock refresh; the model never reads it, so nothing is loaded again."""
        async with _thread(bot):
            turn = await _run_turn(
                bot,
                after_placeholder=lambda: _admit(bot, _REPLY, "Thinking...", ts=101, sender=_BOT),
            )

        assert turn.loads == 1
        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]
        assert turn.summary_history == [*_SEEDED, _FOLLOW_UP, "the answer"]

    @pytest.mark.asyncio
    async def test_a_follow_up_landing_before_the_lock_refresh_reaches_the_model(self, bot: AgentBot) -> None:
        """A message that arrives after dispatch read the thread is in the history the model answers."""
        async with _thread(bot):
            turn = await _run_turn(bot, after_placeholder=lambda: _admit(bot, "$late:localhost", "late", ts=101))

        assert turn.model_history == [*_SEEDED, _FOLLOW_UP, "late"]
        assert turn.summary_history == [*_SEEDED, _FOLLOW_UP, "late", "the answer"]
        # Dispatch and the lock refresh; the summary pass finds the refreshed thread unchanged.
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
        assert turn.summary_history == ["Root", "one", "three", _FOLLOW_UP, "the answer"]
        assert turn.loads == 2

    @pytest.mark.asyncio
    async def test_a_message_landing_while_the_model_answers_reaches_the_summary_pass(self, bot: AgentBot) -> None:
        """The summary pass counts a message the reply never read."""
        async with _thread(bot):
            turn = await _run_turn(bot, while_answering=lambda: _admit(bot, "$during:localhost", "during", ts=102))

        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]
        assert turn.summary_history == [*_SEEDED, _FOLLOW_UP, "during", "the answer"]
        # Dispatch and the summary pass; the lock refresh found the thread unchanged.
        assert turn.loads == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "summarized"),
        [
            ({"replaces": "$two:localhost", "body": "TWO"}, ["Root", "one", "TWO", "three"]),
            ({"redacts": "$two:localhost", "body": ""}, ["Root", "one", "three"]),
        ],
    )
    async def test_an_edit_or_redaction_while_the_model_answers_reaches_the_summary_pass(
        self,
        bot: AgentBot,
        change: dict[str, str],
        summarized: list[str],
    ) -> None:
        """The summary pass reads the thread as it is after the reply, not as the reply read it."""
        async with _thread(bot):
            turn = await _run_turn(bot, while_answering=lambda: _admit(bot, "$change:localhost", ts=102, **change))

        assert turn.model_history == [*_SEEDED, _FOLLOW_UP]
        assert turn.summary_history == [*summarized, _FOLLOW_UP, "the answer"]
        assert turn.loads == 2


class TestAThreadIsUnchangedOnlyWhenAStrictReadWouldRepeat(ThreadingBehaviorTestBase):
    """What lets a caller keep an earlier read: a whole history, a standing complete walk, and the same rows."""

    @pytest.mark.asyncio
    async def test_a_whole_read_of_a_thread_nothing_touched_stands(self, bot: AgentBot) -> None:
        """The read just made is what a read now would return, and so is one missing only the ignored event."""
        async with _thread(bot):
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)
            assert await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held)

            await _admit(bot, _REPLY, "Thinking...", ts=101, sender=_BOT)
            await _admit(bot, "$other:localhost", "other", ts=102)

            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, held, ignoring_event_id=_REPLY)
            held = await complete_thread_history(bot.conversation_reader, _ROOM, _ROOT)
            without_reply = thread_history_result([m for m in held if m.event_id != _REPLY], is_full_history=True)
            assert await thread_is_unchanged(
                bot.conversation_reader,
                _ROOM,
                _ROOT,
                without_reply,
                ignoring_event_id=_REPLY,
            )
            assert not await thread_is_unchanged(bot.conversation_reader, _ROOM, _ROOT, without_reply)

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
