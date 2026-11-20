"""Which MindRoom account acts for a room: the steward for shared invisible work, the poster where no agent answers.

The steward rule also picks the account that observes a room's members for reply access.

Both answers come from the live bots and each bot's own room cache at the moment of the call; nothing is saved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    import nio

    from mindroom.bot import AgentBot


def _pick_steward(candidates: Iterable[tuple[str, float]]) -> str | None:
    """Return the Matrix ID with the highest power among ``(matrix_user_id, power)`` pairs, ties to the lowest ID."""
    steward = min(candidates, key=lambda candidate: (-candidate[1], candidate[0]), default=None)
    return None if steward is None else steward[0]


def _joined(bots: Mapping[str, AgentBot], room_id: str) -> Iterator[tuple[str, AgentBot, nio.MatrixRoom]]:
    """Yield each running bot whose own client lists the room, with its Matrix ID and its own cache of the room."""
    for bot in bots.values():
        client = bot.client
        if bot.running and client is not None and room_id in client.rooms:
            yield client.user_id, bot, client.rooms[room_id]


def joined_bot(bots: Mapping[str, AgentBot], room_id: str, user_id: str) -> AgentBot | None:
    """Return the running bot of that Matrix ID whose own client lists the room, None when there is none."""
    return next((bot for bot_id, bot, _room in _joined(bots, room_id) if bot_id == user_id), None)


def room_steward(bots: Mapping[str, AgentBot], room_id: str) -> AgentBot | None:
    """Return the room's steward: the running, joined bot with the highest power there, None when there is none.

    Every entity counts, the owner account too, and each bot's power is the one its own cache of the room gives it,
    which is above every power level for a creator of a room whose version gives creators unlimited power.
    """
    joined = list(_joined(bots, room_id))
    steward_id = _pick_steward((user_id, room.power_levels.get_user_level(user_id)) for user_id, _bot, room in joined)
    return next((bot for user_id, bot, _room in joined if user_id == steward_id), None)


def membership_observers(
    bots: Mapping[str, AgentBot],
) -> tuple[list[nio.AsyncClient], Callable[[str, Sequence[str]], str | None], bool]:
    """Return the clients that may observe room members for reply access, and the steward rule among their accounts.

    A bot observes only while its own sync is live: from the first settled frame of a receive loop until that loop
    ends, however it ends.
    The rule ranks the accounts it is given for one room by the power each one's own cache of that room gives it.
    The last value says whether every running bot observes: only then do the clients' room lists name every room.
    """
    clients = {
        bot.client.user_id: bot.client for bot in bots.values() if bot.client is not None and bot.observes_room_members
    }

    def pick_observer(room_id: str, user_ids: Sequence[str]) -> str | None:
        return _pick_steward((user_id, _own_power(clients[user_id], room_id)) for user_id in user_ids)

    return list(clients.values()), pick_observer, all(bot.observes_room_members for bot in bots.values() if bot.running)


def _own_power(client: nio.AsyncClient, room_id: str) -> float:
    """Return the power the client's own cache of a room gives its account, 0 before that cache holds the room."""
    room = client.rooms.get(room_id)
    return 0 if room is None else room.power_levels.get_user_level(client.user_id)


def room_poster(
    bots: Mapping[str, AgentBot],
    room_id: str,
    sender: tuple[str | None, str] | None = None,
) -> AgentBot | None:
    """Return the bot for the few posts that have no answering agent, None when no running bot is in the room.

    It is the owner account where that is running and joined, which reads as a neutral voice in the rooms it
    administers, else the lowest Matrix ID among the running, joined agents and teams.
    A retry names the Matrix ID and device of its first attempt and gets that account or none.
    A retry saved before the Matrix ID was pinned names only a device, which is the owner's.
    """
    owner = bots.get(ROUTER_AGENT_NAME)

    joined = (
        (bot is not owner, user_id, bot)
        for user_id, bot, _room in _joined(bots, room_id)
        if sender is None
        or (
            (bot is owner if sender[0] is None else user_id == sender[0])
            and bot.client is not None
            and bot.client.device_id == sender[1]
        )
    )
    poster = min(joined, key=lambda candidate: candidate[:2], default=None)
    return None if poster is None else poster[2]
