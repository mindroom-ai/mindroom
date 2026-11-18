"""What the `matrix_user` toolkit writes as the person: a message or a file, reactions, edits, redactions, and membership.

Each write sends at most one message event or redaction, so none ends with only a part of it sent.
Each message, file, reaction, edit, and redaction names the agent as its author in the content the send helpers are
given, so the uploaded full text of a long message, which history reads instead of the event, says so too; the client
marks each of them again on the wire. A join, leave, invitation, or new room carries no marker.
Nothing is sent into an encrypted room, or one whose encryption could not be read: the client has no device, so the
event would go out in plain text.
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from http import HTTPStatus
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.attachments.storage import filter_attachments_for_context, load_attachment
from mindroom.constants import AGENT_AUTHORED_KEY
from mindroom.custom_tools.attachments import resolve_context_attachment_record
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.matrix_user_reads import (
    MATRIX_USER_ID,
    REQUEST_FAILURES,
    MatrixRequestError,
    json_objects,
    matrix_json,
    optional_json,
    read_event,
    request_failure,
    room_encrypted,
    room_needed,
)
from mindroom.custom_tools.react import reaction_key_error
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    MatrixDeliveryFailureKind,
    build_edit_event_content,
    send_file_message_outcome,
    send_message_outcome,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    import nio

    from mindroom.attachments.storage import AttachmentRecord
    from mindroom.custom_tools.matrix_user_authority import MatrixUserActor
    from mindroom.matrix.client_delivery import MatrixSendOutcome
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = ["MatrixUserWrite", "write_as_person"]

logger = get_logger(__name__)

_MAX_WRITES = 12
_WRITES_WINDOW_SECONDS = 30.0
_RATE_LIMIT_LOCK = Lock()
# Recent writes by agent, requester, and room: the room written to, else the person messaged, else none for a new room.
_RECENT_WRITES: defaultdict[tuple[str, str, str], deque[float]] = defaultdict(deque)
# One call at a time finds or makes a DM for a person: two at once would each make one with the same someone, or
# each write the person's `m.direct` whole and drop the DM the other added.
_DIRECT_ROOM_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_ONE_TARGET = "send needs exactly one of room_id and user_id."
_NOTHING_TO_SEND = "send needs message or attachment."
_ONE_EVENT = "send takes message or attachment, not both: send the text and the file as two calls."
_REPLY_NEEDS_TEXT = "A reply (event_id) needs message."
_NOT_A_USER_ID = "user_id is a full Matrix ID such as @bob:example.com."
_NOT_THE_OWNER = "user_id is whom to message or invite, never your owner."
# A bridge or the homeserver runs such a message as its sender's command, whatever marks it as the agent's.
_COMMAND_LIKE = 'A message sent as your owner cannot start with "!" or "\\": it would read as a command.'
_ENCRYPTED = "The room is encrypted, and nothing is sent as your owner into an encrypted room."
_ENCRYPTION_UNKNOWN = "Nothing was sent: whether the room is encrypted could not be read"
_UNCONFIRMED = "The homeserver did not confirm the message. It may have been sent: read the room before sending again."
# The send failures that prove nothing was sent: each is known before the event is requested, or, for one too large,
# may also be the homeserver's refusal of it.
_PROVES_NOTHING_SENT = frozenset(
    {
        MatrixDeliveryFailureKind.ENCRYPTION_GUARD,
        MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE,
        MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE,
    },
)


def _user_problem(user_id: str | None) -> str | None:
    return None if user_id is None or MATRIX_USER_ID.fullmatch(user_id) else _NOT_A_USER_ID


def _first_visible(line: str) -> str:
    """Return the first character of ``line`` that is neither whitespace nor invisible, or "" when it has none."""
    # A recipient may drop leading whitespace and invisible format characters (Unicode category Cf, such as U+FEFF and
    # U+200B) before it looks for a command.
    return next(
        (character for character in line if not character.isspace() and unicodedata.category(character) != "Cf"),
        "",
    )


def _command_problem(text: str | None) -> str | None:
    # A bridge drops a reply's quote, its leading `>` lines and the blank lines between and after them, before it
    # looks for a command, so the first line that is neither decides. The repository's reply-fallback stripper takes
    # only the exact fallback a client writes ("> " lines, then one blank line), which a model need not.
    first = next(
        (first for line in (text or "").splitlines() if (first := _first_visible(line)) not in {"", ">"}),
        "",
    )
    return _COMMAND_LIKE if first in {"!", "\\"} else None


@dataclass(frozen=True, slots=True)
class MatrixUserWrite:
    """One write the model asked for, by its arguments; one it left blank is None."""

    action: str
    room_id: str | None = None
    # Whom to message or invite; never whom to act as.
    user_id: str | None = None
    # The text of a send or an edit, or the reason of a redaction.
    message: str | None = None
    event_id: str | None = None
    thread_id: str | None = None
    emoji: str | None = None
    # The ID of the file a send sends in place of a message.
    attachment: str | None = None
    name: str | None = None

    def problem(self) -> str | None:  # noqa: PLR0911 - one return per action's own check
        """Return what the arguments lack, or why they are refused, without asking the homeserver anything."""
        if self.action == "send":
            return self._send_problem()
        if self.action == "create_room":
            return _user_problem(self.user_id) if self.name is not None else "create_room needs name."
        if self.room_id is None:
            return room_needed(self.action)
        needed = {
            "react": {"event_id": self.event_id, "emoji": self.emoji},
            "edit": {"event_id": self.event_id, "message": self.message},
            "redact": {"event_id": self.event_id},
            "invite": {"user_id": self.user_id},
        }.get(self.action, {})
        missing = next((name for name, value in needed.items() if value is None), None)
        if missing is not None:
            return f"{self.action} needs {missing}."
        if self.action == "react":
            # The `react` tool's rule: a reaction acknowledges a message, so it is never a sentence.
            return reaction_key_error(str(self.emoji).strip())
        if self.action == "edit":
            return _command_problem(self.message)
        return _user_problem(self.user_id) if self.action == "invite" else None

    def _send_problem(self) -> str | None:
        if (self.room_id is None) == (self.user_id is None):
            return _ONE_TARGET
        if (problem := _user_problem(self.user_id)) is not None:
            return problem
        if self.message is None and self.attachment is None:
            return _NOTHING_TO_SEND
        if self.message is not None and self.attachment is not None:
            return _ONE_EVENT
        if self.event_id is not None and self.message is None:
            return _REPLY_NEEDS_TEXT
        return _command_problem(self.message)


@dataclass
class _Written:
    """What a write has done so far, for its result and its audit line."""

    room_id: str | None
    event_id: str | None = None
    # The content of its event, unless it is a file's, which the send helper builds; only its keys and size are logged.
    content: dict[str, Any] | None = None
    # Whom it invited: into a room, into a new room, or into a DM made for a send.
    invited: str | None = None
    # Why the DM made for a send could not be added to the person's `m.direct`, when it could not.
    dm_not_listed: str | None = None


async def write_as_person(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
) -> dict[str, object]:
    """Make ``write`` as the person ``client`` acts as, marked as ``actor``'s agent's, and log one audit line for it.

    ``write`` has no ``problem``. Raises ``MatrixRequestError`` with what the model reads for a refusal or a failed
    request; a transport failure propagates as aiohttp raised it. However the write ends, its line is logged: one that
    was cancelled, or failed as no request does, may have sent something, so it is logged as interrupted, with the
    failure's type alone, and raised again.
    """
    written = _Written(room_id=write.room_id)
    try:
        fields = await _write(client, actor, turn, write, written)
    except REQUEST_FAILURES as error:
        _audit(actor, write, written, "error", request_failure(error))
        raise
    except BaseException as error:
        _audit(actor, write, written, "interrupted", type(error).__name__)
        raise
    _audit(actor, write, written, "ok")
    return fields


def _audit(
    actor: MatrixUserActor,
    write: MatrixUserWrite,
    written: _Written,
    status: str,
    error: str | None = None,
) -> None:
    content = written.content or {}
    logger.warning(
        "matrix_user_write_audit",
        agent=actor.agent_name,
        person=actor.person_user_id,
        room_id=written.room_id,
        action=write.action,
        status=status,
        event_id=written.event_id,
        content_keys=sorted(content),
        content_bytes=len(json.dumps(content).encode()) if content else 0,
        attachments=int(write.attachment is not None),
        **({} if written.invited is None else {"invited": written.invited}),
        **({} if written.dm_not_listed is None else {"dm_not_listed": written.dm_not_listed}),
        **({} if error is None else {"error": error}),
    )


async def _write(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
    written: _Written,
) -> dict[str, object]:
    if write.user_id == actor.person_user_id:
        raise MatrixRequestError(_NOT_THE_OWNER)
    file = None if write.attachment is None else _turn_file(turn, write.attachment)
    limited = check_rate_limit(
        lock=_RATE_LIMIT_LOCK,
        recent_actions=_RECENT_WRITES,
        window_seconds=_WRITES_WINDOW_SECONDS,
        max_actions=_MAX_WRITES,
        tool_name="matrix_user",
        context=turn,
        room_id=write.room_id or write.user_id or "",
        limit_label="matrix_user writes",
    )
    if limited is not None:
        raise MatrixRequestError(limited)
    marker = {AGENT_AUTHORED_KEY: actor.agent_user_id}
    if write.action == "send":
        return await _send(client, actor, turn, write, written, file, marker)
    if write.action in {"react", "edit", "redact"}:
        return await _event_write(client, actor, turn, write, written, marker)
    if write.action == "create_room":
        written.invited = write.user_id
        # A plain request: the homeserver's default power levels, as a room the person makes in their own client has.
        invite = {"invite": [write.user_id]} if write.user_id is not None else {}
        body = {"name": write.name, "preset": "private_chat", **invite}
        written.room_id = _created_room(await matrix_json(client, "POST", "/createRoom", body))
        return {"room_id": written.room_id}
    room_id, room = str(write.room_id), quote(str(write.room_id), safe="")
    if write.action == "join":
        joined = await matrix_json(client, "POST", f"/join/{room}", {})
        written.room_id = joined["room_id"] if isinstance(joined.get("room_id"), str) else room_id
        return {"room_id": written.room_id}
    if write.action == "leave":
        await matrix_json(client, "POST", f"/rooms/{room}/leave", {})
        return {"room_id": room_id}
    # The only action left: invite.
    written.invited = write.user_id
    await matrix_json(client, "POST", f"/rooms/{room}/invite", {"user_id": write.user_id})
    return {"room_id": room_id}


def _conversation_file(turn: ToolRuntimeContext, attachment_id: str) -> AttachmentRecord | None:
    """Return the file stored under ``attachment_id`` for the turn's own room and thread, if it is still on disk.

    A confirmed write runs in a context built anew, which holds the attachments of the owner's message and none that an
    earlier call of the run added, such as a download, which stores its file for exactly this conversation.
    """
    if turn.storage_path is None:
        return None
    stored = load_attachment(turn.storage_path, attachment_id)
    if stored is None or not stored.local_path.is_file():
        return None
    ours, _ = filter_attachments_for_context([stored], room_id=turn.room_id, thread_id=turn.resolved_thread_id)
    return stored if ours else None


def _turn_file(turn: ToolRuntimeContext, attachment_id: str) -> AttachmentRecord:
    """Return the stored file that ``attachment_id`` names, refusing one the turn and its conversation do not hold.

    A file goes out with its name as its text, which whoever sent the file chose, so a name that would read as a
    command is refused too.
    """
    record = resolve_context_attachment_record(turn, attachment_id)
    if isinstance(record, str):
        record = _conversation_file(turn, attachment_id) or record
    if isinstance(record, str):
        raise MatrixRequestError(record)
    if (problem := _command_problem(record.filename)) is not None:
        msg = f"Attachment {attachment_id}'s file name is sent as its text. {problem}"
        raise MatrixRequestError(msg)
    return record


def _created_room(answer: dict[str, Any]) -> str:
    room_id = answer.get("room_id")
    if not isinstance(room_id, str):
        msg = "The homeserver's answer named no room."
        raise MatrixRequestError(msg)
    return room_id


async def _require_unencrypted(client: nio.AsyncClient, room_id: str) -> None:
    """Refuse a room that is encrypted, or whose encryption could not be read, before anything is sent into it."""
    try:
        encrypted = await room_encrypted(client, room_id)
    except REQUEST_FAILURES as error:
        msg = f"{_ENCRYPTION_UNKNOWN} ({request_failure(error).rstrip('.')})."
        raise MatrixRequestError(msg) from None
    if encrypted:
        raise MatrixRequestError(_ENCRYPTED)


def _delivered(outcome: MatrixSendOutcome) -> str:
    """Return the ID of the event a send delivered, saying that nothing was sent only when its failure proves it.

    A failure before the request, and the homeserver's own refusal, which has a 4xx status, prove it. After any other
    failure the request may have reached the homeserver, and a retry is a new transaction: a second message.
    """
    if isinstance(outcome, DeliveredMatrixEvent):
        return outcome.event_id
    refused = outcome.status is not None and HTTPStatus.BAD_REQUEST <= outcome.status < HTTPStatus.INTERNAL_SERVER_ERROR
    if outcome.kind not in _PROVES_NOTHING_SENT and not refused:
        raise MatrixRequestError(_UNCONFIRMED)
    msg = f"Nothing was sent: {outcome.detail.rstrip('.')}."
    raise MatrixRequestError(msg)


async def _send(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
    written: _Written,
    file: AttachmentRecord | None,
    marker: dict[str, str],
) -> dict[str, object]:
    """Send one event into the room, or into the person's DM with whom ``write`` names, made now when they have none."""
    if write.room_id is not None:
        return await _send_into(client, turn, write, written, file, marker, write.room_id)
    them = str(write.user_id)
    room_id, made = await _direct_room(client, actor.person_user_id, them, written)
    try:
        fields = await _send_into(client, turn, write, written, file, marker, room_id)
    except MatrixRequestError as failed:
        if not made:
            raise
        # The DM made for this send stays, with them invited, so the failure names it.
        msg = f"{_made_dm(room_id, them, written.dm_not_listed)} {failed}"
        raise MatrixRequestError(msg) from None
    if written.dm_not_listed is not None:
        fields["note"] = _made_dm(room_id, them, written.dm_not_listed)
    return fields


async def _send_into(
    client: nio.AsyncClient,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
    written: _Written,
    file: AttachmentRecord | None,
    marker: dict[str, str],
    room_id: str,
) -> dict[str, object]:
    """Send one event into ``room_id``: the file, or the text formatted as the agent's own messages are."""
    await _require_unencrypted(client, room_id)
    latest = None if write.thread_id is None else await _newest_in_thread(client, room_id, write.thread_id)
    if file is not None:
        outcome = await send_file_message_outcome(
            client,
            room_id,
            file.local_path,
            thread_id=write.thread_id,
            latest_thread_event_id=latest,
            filename=file.filename,
            mimetype=file.mime_type,
            extra_content=marker,
        )
        if outcome is None:
            msg = f"Nothing was sent: attachment {file.attachment_id} could not be uploaded."
            raise MatrixRequestError(msg)
        written.event_id = _delivered(outcome)
        return {"room_id": room_id, "event_id": written.event_id}
    content = format_message_with_mentions(
        turn.config,
        turn.runtime_paths,
        str(write.message).strip(),
        thread_event_id=write.thread_id,
        reply_to_event_id=write.event_id if write.thread_id is not None else None,
        latest_thread_event_id=latest,
        extra_content=marker,
    )
    if write.event_id is not None and write.thread_id is None:
        # A reply that every client shows as one, not MindRoom's own unquoted kind.
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": write.event_id}}
    written.content = content
    # A message too long for one event goes out whole, with its full text uploaded, or not at all.
    outcome = await send_message_outcome(
        client,
        room_id,
        content,
        operation="matrix_user_send",
        sidecar_required=True,
    )
    written.event_id = _delivered(outcome)
    return {"room_id": room_id, "event_id": written.event_id}


async def _event_write(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
    written: _Written,
    marker: dict[str, str],
) -> dict[str, object]:
    """React to an event, edit one of the person's own messages, or redact one of their own messages or reactions."""
    room_id, event_id = str(write.room_id), str(write.event_id)
    if write.action == "redact":
        # A redaction carries no text that encryption would protect.
        await _require_changeable(client, room_id, event_id, actor.person_user_id, edit=False)
        reason = (write.message or "").strip()
        written.content = {**({"reason": reason} if reason else {}), **marker}
        path = f"/rooms/{quote(room_id, safe='')}/redact/{quote(event_id, safe='')}/{uuid4().hex}"
        redaction = await matrix_json(client, "PUT", path, written.content)
        if isinstance(redaction.get("event_id"), str):
            written.event_id = redaction["event_id"]
        return {"room_id": room_id, "event_id": redaction.get("event_id")}
    await _require_unencrypted(client, room_id)
    if write.action == "react":
        # The homeserver takes a reaction to an event the room does not hold, such as one of another room, and nobody
        # sees it, so the event is read in the room first, as the `react` tool reads it.
        await read_event(client, room_id, event_id)
        written.content = {**build_reaction_content(event_id, str(write.emoji).strip()), **marker}
        outcome = await send_message_outcome(
            client,
            room_id,
            written.content,
            message_type="m.reaction",
            operation="matrix_user_react",
        )
    else:
        named = await _require_changeable(client, room_id, event_id, actor.person_user_id, edit=True)
        replaced = _replaced_event_id(named)
        if replaced is not None:
            # An earlier edit's result names its replacement event. Clients ignore a replacement of a replacement, so
            # the message it replaced is the one to edit, if it too may be.
            event_id = replaced
            await _require_changeable(client, room_id, event_id, actor.person_user_id, edit=True)
        text = str(write.message).strip()
        new_content = format_message_with_mentions(turn.config, turn.runtime_paths, text)
        # ``extra_content`` puts the marker on both layers, as history keeps the edit's replacement content.
        written.content = build_edit_event_content(
            event_id=event_id,
            new_content=new_content,
            new_text=text,
            extra_content=marker,
        )
        outcome = await send_message_outcome(
            client,
            room_id,
            written.content,
            operation="matrix_user_edit",
            sidecar_required=True,
        )
    written.event_id = _delivered(outcome)
    return {"room_id": room_id, "event_id": written.event_id}


async def _require_changeable(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    person: str,
    *,
    edit: bool,
) -> dict[str, Any]:
    """Return the event, refusing one that is not the person's own message or reaction, or, for an edit, their message.

    The events that made a room and set its name, topic, and members are state, many of them the person's own; the
    toolkit offers to change none of them.
    """
    event = await read_event(client, room_id, event_id)
    sender, event_type = event.get("sender"), event.get("type")
    if sender != person:
        msg = f"Only your owner's own events can be changed, and {event_id} was sent by {sender}."
        raise MatrixRequestError(msg)
    if "state_key" in event:
        msg = f"Only messages and reactions can be changed this way, and {event_id} is room state of type {event_type}."
        raise MatrixRequestError(msg)
    if edit and event_type != "m.room.message":
        msg = f"Only messages can be edited, and {event_id} is of type {event_type}."
        raise MatrixRequestError(msg)
    return event


def _replaced_event_id(event: dict[str, Any]) -> str | None:
    """Return the ID of the message that ``event`` replaces, when it is an edit's replacement event."""
    content = event.get("content")
    relation = content.get("m.relates_to") if isinstance(content, dict) else None
    if not isinstance(relation, dict) or relation.get("rel_type") != "m.replace":
        return None
    replaced = relation.get("event_id")
    return replaced if isinstance(replaced, str) else None


async def _newest_in_thread(client: nio.AsyncClient, room_id: str, thread_id: str) -> str:
    """Return the newest event of a thread, which a reply that answers none of them follows; its root if it has none."""
    path = f"/rooms/{quote(room_id, safe='')}/relations/{quote(thread_id, safe='')}/m.thread"
    page = await matrix_json(client, "GET", f"{path}?{urlencode({'dir': 'b', 'limit': 1})}", version="v1")
    newest = next(iter(json_objects(page, "chunk")), {}).get("event_id")
    return newest if isinstance(newest, str) else thread_id


def _made_dm(room_id: str, them: str, not_listed: str | None) -> str:
    """Return what a result says of the DM made for its send, and of why it is not in the person's DMs, if it is not."""
    made = f"Made the DM {room_id} with {them}"
    if not_listed is None:
        return f"{made}."
    why = not_listed.rstrip(".")
    return f"{made}, but could not list it as a DM ({why}). To send there again, use room_id {room_id}."


async def _direct_room(client: nio.AsyncClient, person: str, them: str, written: _Written) -> tuple[str, bool]:
    """Return the person's DM with ``them``, and whether it was made now, as one is when they have none.

    Their DM is the newest room the person's `m.direct` lists for ``them`` whose members, joined or invited, are only the
    two of them. A room made now is added there, and is their DM for this send even when that fails.
    Calls of this runtime for one person take turns, so a second one finds the DM the first made and recorded.
    ``written`` names the room, whom a room made now invited, and why it could not be added, if it could not.
    """
    async with _DIRECT_ROOM_LOCKS.setdefault(person, asyncio.Lock()):
        direct_path = f"/user/{quote(person, safe='')}/account_data/m.direct"
        direct = await optional_json(client, direct_path) or {}
        listed = direct.get(them)
        for room_id in reversed(listed if isinstance(listed, list) else []):
            if isinstance(room_id, str) and await _only_the_two(client, room_id, person, them):
                written.room_id = room_id
                return room_id, False
        created = await matrix_json(
            client,
            "POST",
            "/createRoom",
            {"preset": "trusted_private_chat", "is_direct": True, "invite": [them]},
        )
        room_id = _created_room(created)
        written.room_id, written.invited = room_id, them
        written.dm_not_listed = await _record_direct_room(client, direct_path, them, room_id)
        return room_id, True


async def _record_direct_room(client: nio.AsyncClient, direct_path: str, them: str, room_id: str) -> str | None:
    """Add ``room_id`` to the person's `m.direct` list for ``them``, as it is when this writes; return why it failed.

    `m.direct` is written as a whole, and the person's own client may have changed it since it was first read, so it is
    read again just before the write and that copy, not the early one, is written back. The caller holds the person's
    lock, so no other call of this runtime writes it between that read and the write.
    """
    try:
        fresh = await optional_json(client, direct_path) or {}
        listed = fresh.get(them)
        listed = listed if isinstance(listed, list) else []
        if room_id not in listed:
            await matrix_json(client, "PUT", direct_path, {**fresh, them: [*listed, room_id]})
    except REQUEST_FAILURES as error:
        return request_failure(error)
    return None


async def _only_the_two(client: nio.AsyncClient, room_id: str, person: str, them: str) -> bool:
    """Return whether the person is in ``room_id`` and its only other member, joined or invited, is ``them``.

    A room the homeserver does not show the person is not their DM.
    """
    try:
        answer = await matrix_json(client, "GET", f"/rooms/{quote(room_id, safe='')}/members")
    except MatrixRequestError as error:
        if error.status in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}:
            return False
        raise
    memberships = {
        event.get("state_key"): content.get("membership")
        for event in json_objects(answer, "chunk")
        if isinstance(content := event.get("content"), dict)
    }
    present = {user_id for user_id, membership in memberships.items() if membership in {"join", "invite"}}
    return memberships.get(person) == "join" and present == {person, them}
