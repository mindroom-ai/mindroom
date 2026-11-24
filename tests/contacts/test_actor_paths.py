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

from mindroom.contacts.parties import remote_party
from mindroom.hooks import (
    EVENT_MESSAGE_RECEIVED,
    EVENT_REACTION_RECEIVED,
    HookRegistry,
    MessageReceivedContext,
    ReactionReceivedContext,
    hook,
)
from tests.bot_helpers import _hook_plugin, _room_file_event, dispatch_reaction_durably
from tests.conftest import drain_coalescing, install_generate_response_mock
from tests.contacts.test_admission import ADMIN, CAROL, CONTACT_ROOM, _agent, _contact, _event, _setup
from tests.contacts.test_matrix_message_policy import (  # noqa: F401 - _reset_rate_limit is an autouse pytest fixture
    _call,
    _context,
    _remove,
    _reset_rate_limit,
    _sent,
)
from tests.contacts.test_turns import _bot, _contact_room
from tests.test_generated_agents import CO_OWNER, OWNER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

RUN, REFUSED, IGNORED = "run", "refused", "ignored"
PATHS = ("edit", "message hook post", "media", "reaction hook post")
# The hook each path runs when it is admitted; an edit runs none and reaches the edit regenerator instead.
HOOKS = {"message hook post": "message", "media": "message", "reaction hook post": "reaction"}
# What each actor's event does in Bob's agent's contact room, by path. "run" reaches the edit regenerator, runs the
# message hooks on a text message or a file, or runs the reaction hooks; "refused" logs contact_room_sender_refused and
# runs nothing; "ignored" runs nothing, as for the agent's own messages and another agent's edits in any room.
# The agent's own reactions run its reaction hooks as in any room, and the room refuses their posts.
# fmt: off
ACTIVE = {
    #                      edit     message hook media    reaction hook
    "owner":              (RUN,     RUN,         RUN,     RUN),
    "counterparty owner": (RUN,     RUN,         RUN,     RUN),
    "counterparty agent": (IGNORED, RUN,         RUN,     RUN),
    "administrator":      (REFUSED, REFUSED,     REFUSED, REFUSED),
    "stranger":           (REFUSED, REFUSED,     REFUSED, REFUSED),
    "agent itself":       (IGNORED, IGNORED,     IGNORED, RUN),
}
# fmt: on
# In a contact with another deployment's agent, that agent is the whole other side: nobody here can vouch for its
# owner, who is refused like a stranger. Its edit reaches the edit regenerator like a person's, which regenerates no
# contact turn.
# fmt: off
REMOTE_ACTIVE = {
    #                      edit     message hook media    reaction hook
    "owner":              (RUN,     RUN,         RUN,     RUN),
    "remote agent":       (RUN,     RUN,         RUN,     RUN),
    "remote owner":       (REFUSED, REFUSED,     REFUSED, REFUSED),
    "administrator":      (REFUSED, REFUSED,     REFUSED, REFUSED),
    "stranger":           (REFUSED, REFUSED,     REFUSED, REFUSED),
    "agent itself":       (IGNORED, IGNORED,     IGNORED, RUN),
}
# fmt: on


def _removed(active: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """A removed contact's room starts nothing for anyone else, its agent's owner included."""
    return {actor: active["agent itself"] if actor == "agent itself" else (REFUSED,) * len(PATHS) for actor in active}


REMOTE = "@mindroom_mind_dana:other.org"
# Each actor's Matrix ID, or the agent whose account it writes from.
SENDERS = {
    "owner": CO_OWNER,
    "counterparty owner": OWNER,
    "counterparty agent": "mind_alice",
    "remote agent": REMOTE,
    "remote owner": "@dana:other.org",
    "administrator": ADMIN,
    "stranger": CAROL,
    "agent itself": "mind_bob",
}
TABLES = {
    ("local", "active"): ACTIVE,
    ("local", "removed"): _removed(ACTIVE),
    ("remote", "active"): REMOTE_ACTIVE,
    ("remote", "removed"): _removed(REMOTE_ACTIVE),
}
CASES = [
    pytest.param(other, status, actor, path, outcome, id=f"{other}-{status}-{actor}-{path}".replace(" ", "_"))
    for (other, status), table in TABLES.items()
    for actor, outcomes in table.items()
    for path, outcome in zip(PATHS, outcomes, strict=True)
]


async def _message(config: Config, paths: RuntimePaths, actor: str) -> dict[str, Any]:
    """Return what the actor writes: Alice's agent writes what its matrix_message sends to Bob's agent as recipient.

    Recipient discovery must offer Bob's agent there, though its access never admits Alice, whom that send is for.
    """
    if actor != "counterparty agent":
        return {}
    context = _context(config, paths)
    sent = await _call(context, message="Are you free on Friday?", recipient="mind_bob")
    assert sent["status"] == "ok", sent
    [content] = _sent(context)
    return content


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
    await bot._turn_ingress.handle_text_event(room, _event(sender, **content))


async def _drained(bot: AgentBot, handled: Awaitable[object]) -> None:
    """Await an event's ingress callback, then the turn it coalesced into."""
    await handled
    await drain_coalescing(bot)


def _hooks(bot: AgentBot, ran: list[str], posted: list[str | None]) -> None:
    """Install hooks on every message and reaction that note they ran and post into the room."""

    async def note(kind: str, ctx: MessageReceivedContext | ReactionReceivedContext) -> None:
        ran.append(kind)
        posted.append(await ctx.send_message(CONTACT_ROOM, "Bob is free on Friday."))

    @hook(EVENT_MESSAGE_RECEIVED)
    async def on_message(ctx: MessageReceivedContext) -> None:
        await note("message", ctx)
        # A message that runs the hooks starts no reply then, so the grid checks only that they ran.
        ctx.suppress = True

    @hook(EVENT_REACTION_RECEIVED)
    async def on_reaction(ctx: ReactionReceivedContext) -> None:
        await note("reaction", ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_hook_plugin("observer", [on_message, on_reaction])],
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(("other", "status", "actor", "path", "expected"), CASES)
async def test_each_actor_on_each_path_is_run_refused_or_ignored_as_the_table_says(
    tmp_path: Path,
    other: str,
    status: str,
    actor: str,
    path: str,
    expected: str,
) -> None:
    """No path reaches the room: a hook's post into it is refused, and a refused or ignored event starts no reply.

    The other side is Alice's agent here, or an agent of another deployment.
    """
    config, paths = _setup(tmp_path)
    if other == "local":
        _contact(paths)
    else:
        _contact(paths, requester=remote_party(REMOTE))
    sender = SENDERS[actor] if SENDERS[actor].startswith("@") else _agent(paths, SENDERS[actor])
    # Only the message path reads the content, so a recipient-discovery regression fails that path alone.
    content = await _message(config, paths, actor) if path == "message hook post" else {}
    if status == "removed":
        # Whatever was written while the contact was active, as an event still in flight at its removal is.
        _remove(paths)
    bot = _bot(config, paths)
    generate = AsyncMock()
    install_generate_response_mock(bot, generate)
    ran: list[str] = []
    posted: list[str | None] = []
    _hooks(bot, ran, posted)
    room = _contact_room(paths)
    ingress = bot._turn_ingress
    upload = _room_file_event(sender=sender, event_id="$file", room_id=CONTACT_ROOM)
    steps: dict[str, Callable[[], Awaitable[None]]] = {
        "edit": lambda: _edit(bot, room, sender),
        "message hook post": lambda: _drained(bot, ingress.handle_text_event(room, _event(sender, **content))),
        "media": lambda: _drained(bot, ingress.handle_media_event(room, upload)),
        "reaction hook post": lambda: _react(bot, room, sender),
    }

    with capture_logs() as logs:
        await steps[path]()
    events = {log["event"] for log in logs}
    # An edit that reaches the edit regenerator logs edit_not_regenerated, as no turn of the edited message is recorded.
    handled = bool(ran) or "edit_not_regenerated" in events
    outcome = REFUSED if "contact_room_sender_refused" in events else RUN if handled else IGNORED

    assert outcome == expected
    assert not (outcome == REFUSED and handled)
    assert ran == ([HOOKS[path]] if outcome == RUN and path in HOOKS else [])
    assert posted == [None] * len(ran)
    assert [call for call in bot.client.room_send.await_args_list if call.kwargs["room_id"] == CONTACT_ROOM] == []
    generate.assert_not_awaited()
