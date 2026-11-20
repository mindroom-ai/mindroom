"""Which bot runs a scheduled task: the bot that saved it, or the heir of a task the router owned.

A new task stores the Matrix ID of the bot that saves it as its owner, so every bot that restores a room agrees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.scheduling.executor import scheduled_trigger_message_type

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.scheduling.tasks import ScheduledWorkflow


def room_from_state(room_id: str, own_user_id: str, events: Iterable[dict[str, object]]) -> nio.MatrixRoom:
    """Build the membership and power view of a room from its full state, as Matrix sync would."""
    room = nio.MatrixRoom(room_id, own_user_id)
    for source in events:
        # nio's room view acts only on `m.` events, so MindRoom's own state, such as every finished task, is skipped.
        if not str(source.get("type")).startswith("m."):
            continue
        event = nio.Event.parse_event(dict(source))
        if isinstance(event, nio.RoomMemberEvent):
            room.handle_membership(event)
        elif isinstance(event, nio.Event):
            room.handle_event(event)
    return room


def legacy_router_task_owner(
    room: nio.MatrixRoom,
    workflow: ScheduledWorkflow,
    *,
    router_id: str,
    agent_ids: Sequence[str],
) -> str | None:
    """Return the agent or team that runs a stored task the router owned, None where none can.

    ``agent_ids`` names the candidates in order of preference: those the task's message mentions, then every agent
    and team by Matrix ID.
    The first one that is joined and may post the task's trigger wins, read from the room's full state, never from
    which bots run, so every bot that restores the room agrees.
    """
    return next(
        (agent_id for agent_id in agent_ids if agent_id != router_id and _may_send_trigger(room, agent_id, workflow)),
        None,
    )


def _may_send_trigger(room: nio.MatrixRoom, user_id: str, workflow: ScheduledWorkflow) -> bool:
    if user_id not in room.users or user_id in room.invited_users:
        return False
    trigger_type = "m.room.encrypted" if room.encrypted else scheduled_trigger_message_type(workflow)
    return room.power_levels.can_user_send_message(user_id, trigger_type)
