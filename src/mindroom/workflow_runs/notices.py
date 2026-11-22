"""What a workflow run posts in its thread: a notice that carries a card, posted once and edited from then on.

The one way a run's card and its questions reach the room. Every notice is content a homeserver signs, so it holds
no float, and it is measured as the edit it is or will get, which carries its content twice.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.matrix.client_delivery import build_edit_event_content, edit_message_result, send_message_result
from mindroom.matrix.large_messages import fits_one_event
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    import nio

    from mindroom.workflow_runs.models import WorkflowRun

# How long a send that failed waits before each further try: a homeserver that restarts, or a rate limit.
RETRY_SECONDS = (0.5, 2.0)
# What stands for a notice's event while one that was not posted yet is measured as the edit it will get.
_NO_EVENT = "$" + "0" * 43
_CUT = "... [truncated]"


def clip(text: str, chars: int) -> str:
    """Return *text* in at most *chars* characters, with a mark at the end of one that was cut."""
    return text if len(text) <= chars else text[: chars - len(_CUT)] + _CUT


def _signable(value: object) -> object:
    """Return *value* as an event a homeserver signs may hold it: a float as a decimal string of six places at most."""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return {name: _signable(part) for name, part in value.items()}
    return [_signable(part) for part in value] if isinstance(value, list | tuple) else value


def notice(run: WorkflowRun, text: str, key: str, content: dict[str, Any]) -> dict[str, Any]:
    """Return a notice in the run's thread that says *text* and carries *content* under *key*."""
    return build_message_content(
        text,
        thread_event_id=run.thread_root_event_id,
        latest_thread_event_id=run.thread_root_event_id,
        extra_content={"msgtype": "m.notice", key: _signable(content)},
    )


def notice_fits(client: nio.AsyncClient, run: WorkflowRun, message: dict[str, Any], event_id: str | None) -> bool:
    """Return whether *message* is one event in the run's room as an edit of the notice at *event_id*."""
    room = client.rooms.get(run.room_id)
    edit = build_edit_event_content(event_id=event_id or _NO_EVENT, new_content=message, new_text=message["body"])
    return fits_one_event(client, run.room_id, edit, room_encrypted=room is None or bool(room.encrypted))


async def send_notice(
    client: nio.AsyncClient,
    run: WorkflowRun,
    message: dict[str, Any],
    *,
    event_id: str | None = None,
    transaction_id: str | None = None,
    retries: tuple[float, ...] = RETRY_SECONDS,
) -> str | None:
    """Post *message*, or edit the notice at *event_id* to say it, and return the notice's event.

    A post is sent under *transaction_id*, so one that is sent again is the same event. A send that fails is tried
    again after each of *retries*; None says that it could not be sent now, and the caller's next look sends it.
    """
    for wait in (*retries, None):
        if event_id is None:
            send = partial(send_message_result, client, run.room_id, message, operation="send_workflow_notice")
            sent = await send(transaction_id=transaction_id)
        else:
            sent = await edit_message_result(client, run.room_id, event_id, message, message["body"])
        if sent is not None:
            return event_id or sent.event_id
        if wait is not None:
            await asyncio.sleep(wait)
    return None
