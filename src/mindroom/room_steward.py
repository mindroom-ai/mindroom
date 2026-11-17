"""Which MindRoom account acts for a room: the steward for shared invisible work, the poster where no agent answers.

Both answers come from the live bots and each bot's own room cache at the moment of the call; nothing is saved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    import nio

    from mindroom.bot import AgentBot


def pick_steward(candidates: Iterable[tuple[str, float]]) -> str | None:
    """Return the Matrix ID with the highest power among ``(matrix_user_id, power)`` pairs, ties to the lowest ID."""
    steward = min(candidates, key=lambda candidate: (-candidate[1], candidate[0]), default=None)
    return None if steward is None else steward[0]


def _joined(bots: Mapping[str, AgentBot], room_id: str) -> Iterator[tuple[str, AgentBot, nio.MatrixRoom]]:
    """Yield each running bot whose own client lists the room, with its Matrix ID and its own cache of the room."""
    for bot in bots.values():
        client = bot.client
        if bot.running and client is not None and room_id in client.rooms:
            yield client.user_id, bot, client.rooms[room_id]


def room_steward(bots: Mapping[str, AgentBot], room_id: str) -> AgentBot | None:
    """Return the room's steward: the running, joined bot with the highest power there, None when there is none.

    Every entity counts, the owner account too, and each bot's power is the one its own cache of the room gives it,
    which is above every power level for a creator of a room whose version gives creators unlimited power.
    """
    joined = list(_joined(bots, room_id))
    steward_id = pick_steward((user_id, room.power_levels.get_user_level(user_id)) for user_id, _bot, room in joined)
    return next((bot for user_id, bot, _room in joined if user_id == steward_id), None)


def room_poster(bots: Mapping[str, AgentBot], room_id: str) -> AgentBot | None:
    """Return the bot for the few posts that have no answering agent, None when no running bot is in the room.

    It is the owner account where that is running and joined, which reads as a neutral voice in the rooms it
    administers, else the lowest Matrix ID among the running, joined agents and teams.
    """
    owner = bots.get(ROUTER_AGENT_NAME)
    poster = min(_joined(bots, room_id), key=lambda joined: (joined[1] is not owner, joined[0]), default=None)
    return None if poster is None else poster[1]
