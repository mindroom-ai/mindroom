"""Which bot runs a scheduled task, decided once from the room's full state when the task is saved."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import nio

from mindroom.constants import ROUTER_AGENT_NAME, SCHEDULED_TASK_EVENT_TYPE
from mindroom.entity_resolution import current_entity_id
from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.executor import scheduled_trigger_message_type

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.constants import RuntimePaths
    from mindroom.scheduling.tasks import ScheduledWorkflow


def room_from_state(room_id: str, own_user_id: str, events: Iterable[dict[str, object]]) -> nio.MatrixRoom:
    """Build the membership and power view of a room from its full state, as Matrix sync would."""
    room = nio.MatrixRoom(room_id, own_user_id)
    for source in events:
        event = nio.Event.parse_event(dict(source))
        if isinstance(event, nio.RoomMemberEvent):
            room.handle_membership(event)
        elif isinstance(event, nio.Event):
            room.handle_event(event)
    return room


def task_owner(room: nio.MatrixRoom, workflow: ScheduledWorkflow, *, creator_id: str, router_id: str) -> str:
    """Return the Matrix ID of the bot that runs a task: the router when it can serve the room, else its creator.

    The rule reads only room state, never which bots run, so the owner does
    not depend on which bot happens to be restarting.
    """
    if _may_send_trigger(room, router_id, workflow) and room.power_levels.can_user_send_state(
        router_id,
        SCHEDULED_TASK_EVENT_TYPE,
    ):
        return router_id
    return creator_id


async def decide_task_owner(
    client: nio.AsyncClient,
    room_id: str,
    workflow: ScheduledWorkflow,
    runtime_paths: RuntimePaths,
) -> str:
    """Decide a new task's owner from the room's full state, with the saving bot as its creator.

    A synced room view can lack members a lazy-loading sync never sent, so
    only the full state gives every bot that later reads the task the same answer.
    """
    failure = f"Could not read the state of room {room_id} to choose which bot runs the task"
    try:
        response = await client.room_get_state(room_id)
    except aiohttp.ClientError as exc:
        # nio raises a response that broke off instead of answering it.
        msg = f"{failure}: {type(exc).__name__}: {exc}"
        raise SchedulingError(msg) from exc
    if not isinstance(response, nio.RoomGetStateResponse):
        msg = f"{failure}: {response}"
        raise SchedulingError(msg)
    room = room_from_state(room_id, client.user_id, response.events)
    router_id = current_entity_id(ROUTER_AGENT_NAME, runtime_paths).full_id
    return task_owner(room, workflow, creator_id=client.user_id, router_id=router_id)


def _may_send_trigger(room: nio.MatrixRoom, user_id: str, workflow: ScheduledWorkflow) -> bool:
    if user_id not in room.users or user_id in room.invited_users:
        return False
    trigger_type = "m.room.encrypted" if room.encrypted else scheduled_trigger_message_type(workflow)
    return room.power_levels.can_user_send_message(user_id, trigger_type)
