"""A turn a stop or a failure cut short: its own run, marked as the turn's record, and the note its replay ends with.

The run's loop stores the run it ended; the turn's owner marks it (``mark_interrupted``). A marked run is ``COMPLETED``
with ``_MARKER_KEY`` in its metadata, so every release loads and replays it as a finished turn, and this release ends
its replay with a note that it did not finish (``replayed_messages``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.llm.messages import Message
from mindroom.llm.runs import RunStatus

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord

# The run metadata key marking an interrupted turn's record, holding how many of its tool calls had not finished.
# Earlier releases ignore it, so a marked run reads to them as the finished turn its messages show.
_MARKER_KEY = "mindroom_interrupted"
_UNFINISHED_KEY = "unfinished_tool_calls"


def mark_interrupted(record: AnyRunRecord, *, error: str | None) -> None:
    """Make ``record`` the record of its interrupted turn: completed, marked, and holding only calls that finished.

    A call no stored result answers did not finish, and a request answers every call it replays, so the call leaves its
    assistant message, as the tool-call window drops a call, and the marker counts it. Every message keeps its place,
    an emptied one included, since it holds its request's usage. ``error``, when given, is the failure's text.
    """
    answered = {message.tool_call_id for message in record.messages if message.role == "tool"}
    unfinished = 0
    messages: list[Message] = []
    for message in record.messages:
        calls = message.tool_calls or []
        kept = [call for call in calls if call.get("id") in answered]
        unfinished += len(calls) - len(kept)
        messages.append(message if len(kept) == len(calls) else replace(message, tool_calls=kept or None))
    record.messages = messages
    record.status = RunStatus.completed
    record.error = error or record.error
    record.metadata = {**(record.metadata or {}), _MARKER_KEY: {_UNFINISHED_KEY: unfinished}}


def is_interrupted(run: AnyRunRecord) -> bool:
    """Return whether ``run`` is the record of a turn a stop or a failure cut short (``mark_interrupted``)."""
    return isinstance((run.metadata or {}).get(_MARKER_KEY), dict)


def replayed_messages(run: AnyRunRecord) -> list[Message]:
    """Return the messages ``run`` replays: its stored ones, or an interrupted run's (``mark_interrupted``) and its note.

    An interrupted run leaves out the assistant messages it left empty, which hold only their requests' usage. The
    note, which joins the run's final text or follows its last message, tells the model the turn failed, when the run
    holds a failure's text, or stopped, and how many calls did not finish. Changed messages are copies.
    """
    marker = (run.metadata or {}).get(_MARKER_KEY)
    if not isinstance(marker, dict):
        return run.messages
    note = "(turn failed before completion" if run.error else "(turn stopped before completion"
    if unfinished := marker.get(_UNFINISHED_KEY):
        note += f"; {unfinished} tool call(s) had not finished"
    note += ")"
    kept = [message for message in run.messages if message.role != "assistant" or message.content or message.tool_calls]
    last = kept[-1] if kept else None
    if last is not None and last.role == "assistant" and not last.tool_calls and isinstance(last.content, str):
        return [*kept[:-1], replace(last, content=f"{last.content}\n\n{note}")]
    return [*kept, Message(role="assistant", content=note)]


__all__ = ["is_interrupted", "mark_interrupted", "replayed_messages"]
