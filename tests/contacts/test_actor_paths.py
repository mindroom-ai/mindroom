"""Who reaches an agent in its contact room, by every ingress path, and that no hook posts into the room.

Bob's agent and Alice's agent share a contact room. Each actor's event arrives on each path Bob's agent takes events
in, and one table says whether it runs what that path runs, is refused as a sender the room does not admit, or is
ignored the way the path ignores it anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.hooks import (
    EVENT_MESSAGE_RECEIVED,
    EVENT_REACTION_RECEIVED,
    HookRegistry,
    MessageReceivedContext,
    ReactionReceivedContext,
    hook,
)
from tests.bot_helpers import _hook_plugin, dispatch_reaction_durably
from tests.conftest import drain_coalescing, install_generate_response_mock
from tests.contacts.test_admission import ADMIN, CAROL, CONTACT_ROOM, _agent, _contact, _setup
from tests.contacts.test_matrix_message_policy import _call, _context, _remove, _sent
from tests.contacts.test_turns import _bot, _contact_room
from tests.test_generated_agents import CO_OWNER, OWNER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

RUN, REFUSED, IGNORED = "run", "refused", "ignored"
PATHS = ("message", "reaction", "edit", "message hook post", "reaction hook post")
# What each actor's event does in Bob's agent's contact room, by path. "run" passes the message precheck, runs the
# reaction hooks, reaches the edit regenerator, or runs the message hooks; "refused" logs contact_room_sender_refused
# and runs nothing; "ignored" runs nothing, as for the agent's own messages and another agent's edits in any room.
# The agent's own reactions run its reaction hooks as in any room, and the room refuses their posts.
# fmt: off
ACTIVE = {
    #                      message  reaction edit     message hook reaction hook
    "owner":              (RUN,     RUN,     RUN,     RUN,         RUN),
    "counterparty owner": (RUN,     RUN,     RUN,     RUN,         RUN),
    "counterparty agent": (RUN,     RUN,     IGNORED, RUN,         RUN),
    "administrator":      (REFUSED, REFUSED, REFUSED, REFUSED,     REFUSED),
    "stranger":           (REFUSED, REFUSED, REFUSED, REFUSED,     REFUSED),
    "agent itself":       (IGNORED, RUN,     IGNORED, IGNORED,     RUN),
}
# fmt: on
# A removed contact's room starts nothing for anyone else, its agent's owner included.
REMOVED = {actor: ACTIVE["agent itself"] if actor == "agent itself" else (REFUSED,) * len(PATHS) for actor in ACTIVE}
# Each actor's Matrix ID, or the agent whose account it writes from.
SENDERS = {
    "owner": CO_OWNER,
    "counterparty owner": OWNER,
    "counterparty agent": "mind_alice",
    "administrator": ADMIN,
    "stranger": CAROL,
    "agent itself": "mind_bob",
}
CASES = [
    pytest.param(status, actor, path, outcome, id=f"{status}-{actor}-{path}")
    for status, table in (("active", ACTIVE), ("removed", REMOVED))
    for actor, outcomes in table.items()
    for path, outcome in zip(PATHS, outcomes, strict=True)
]


async def _message(config: Config, paths: RuntimePaths, actor: str) -> dict[str, Any]:
    """Return what the actor writes: Alice's agent writes what its matrix_message sends to Bob's agent as recipient.

    Recipient discovery must offer Bob's agent there, though its access never admits Alice, whom that send is for.
    """
    if actor != "counterparty agent":
        return {"msgtype": "m.text", "body": "Are you free on Friday?"}
    context = _context(config, paths)
    sent = await _call(context, message="Are you free on Friday?", recipient="mind_bob")
    assert sent["status"] == "ok", sent
    [content] = _sent(context)
    return content


def _text(sender: str, content: dict[str, Any], event_id: str = "$message") -> nio.RoomMessageText:
    source = {"event_id": event_id, "sender": sender, "origin_server_ts": 1, "type": "m.room.message"}
    return nio.RoomMessageText.from_dict(source | {"content": content, "room_id": CONTACT_ROOM})


async def _precheck(bot: AgentBot, room: nio.MatrixRoom, event: nio.RoomMessageText) -> bool:
    validator = bot._ingress_validator
    return await validator.precheck_event(room, event, validator.classify(event, room_id=CONTACT_ROOM))


async def _react(bot: AgentBot, room: nio.MatrixRoom, sender: str) -> None:
    reaction = {"event_id": "$reaction", "sender": sender, "origin_server_ts": 1, "type": "m.reaction"}
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$message", "key": "👍"}}
    await dispatch_reaction_durably(bot, room, nio.ReactionEvent.from_dict(reaction | {"content": content}))


async def _edit(bot: AgentBot, room: nio.MatrixRoom, sender: str) -> None:
    content = {
        "msgtype": "m.text",
        "body": "* Saturday?",
        "m.new_content": {"msgtype": "m.text", "body": "Saturday?"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$message"},
    }
    await bot._turn_ingress.handle_text_event(room, _text(sender, content, "$edit"))


async def _dispatch(bot: AgentBot, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
    await bot._turn_ingress.handle_text_event(room, event)
    await drain_coalescing(bot)


def _hooks(bot: AgentBot, ran: list[str], posted: list[str | None], *, posts: bool) -> None:
    """Install hooks on every message and reaction that note they ran and, when ``posts``, post into the room."""

    async def note(kind: str, ctx: MessageReceivedContext | ReactionReceivedContext) -> None:
        ran.append(kind)
        if posts:
            posted.append(await ctx.send_message(CONTACT_ROOM, "Bob is free on Friday."))

    @hook(EVENT_MESSAGE_RECEIVED)
    async def on_message(ctx: MessageReceivedContext) -> None:
        await note("message", ctx)
        ctx.suppress = True

    @hook(EVENT_REACTION_RECEIVED)
    async def on_reaction(ctx: ReactionReceivedContext) -> None:
        await note("reaction", ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_hook_plugin("observer", [on_message, on_reaction])],
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(("status", "actor", "path", "expected"), CASES)
async def test_each_actor_on_each_path_is_run_refused_or_ignored_as_the_table_says(
    tmp_path: Path,
    status: str,
    actor: str,
    path: str,
    expected: str,
) -> None:
    """Nothing reaches the room from any path: a hook's post into it is refused, and no reply starts there."""
    config, paths = _setup(tmp_path)
    _contact(paths)
    sender = SENDERS[actor] if SENDERS[actor].startswith("@") else _agent(paths, SENDERS[actor])
    content = await _message(config, paths, actor)
    if status == "removed":
        # Whatever was written while the contact was active, as an event still in flight at its removal is.
        _remove(paths)
    bot = _bot(config, paths)
    generate = AsyncMock()
    install_generate_response_mock(bot, generate)
    ran: list[str] = []
    posted: list[str | None] = []
    _hooks(bot, ran, posted, posts=path.endswith("hook post"))
    room = _contact_room(paths)
    steps: dict[str, Callable[[], Awaitable[object]]] = {
        "message": lambda: _precheck(bot, room, _text(sender, content)),
        "reaction": lambda: _react(bot, room, sender),
        "edit": lambda: _edit(bot, room, sender),
        "message hook post": lambda: _dispatch(bot, room, _text(sender, content)),
        "reaction hook post": lambda: _react(bot, room, sender),
    }

    with capture_logs() as logs:
        passed_precheck = await steps[path]()
    events = {log["event"] for log in logs}
    # The edit regenerator reports each edit it handles; none here regenerates, as a contact turn's reply never shows.
    handled = passed_precheck is True or bool(ran) or "edit_not_regenerated" in events
    outcome = REFUSED if "contact_room_sender_refused" in events else RUN if handled else IGNORED

    assert outcome == expected
    assert not (outcome == REFUSED and handled)
    assert posted == ([None] if path.endswith("hook post") and outcome == RUN else [])
    assert [call for call in bot.client.room_send.await_args_list if call.kwargs["room_id"] == CONTACT_ROOM] == []
    generate.assert_not_awaited()
