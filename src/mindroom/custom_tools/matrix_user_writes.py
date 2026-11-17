"""What the `matrix_user` toolkit writes as the person: messages and files, reactions, edits, redactions, and membership.

Each event's content names the agent as its author before any send helper sees it, so the uploaded full text of a long
message, which history reads instead of the event, says so too; the client marks every event again on the wire.
Nothing is sent into an encrypted room, or one whose encryption could not be read: the client has no device, so the
event would go out in plain text.
"""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http import HTTPStatus
from threading import Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import aiohttp

from mindroom.constants import AGENT_AUTHORED_KEY
from mindroom.custom_tools.attachments import resolve_context_attachment_record
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.matrix_user_reads import (
    MATRIX_USER_ID,
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
    send_file_message,
    send_message_outcome,
)
from mindroom.matrix.guarded_rooms import send_refused
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
# A DM made for the person with someone, by the two of them, that could not be added to the person's `m.direct`: the
# next send to that someone uses it rather than making another.
_UNRECORDED_DIRECT_ROOMS: dict[tuple[str, str], str] = {}
_ONE_TARGET = "send needs exactly one of room_id and user_id."
_NOTHING_TO_SEND = "send needs message, attachments, or both."
_REPLY_NEEDS_TEXT = "A reply (event_id) needs message."
_NOT_A_USER_ID = "user_id is a full Matrix ID such as @bob:example.com."
_NOT_THE_OWNER = "user_id is whom to message or invite, never your owner."
# A bridge or the homeserver runs such a message as its sender's command, whatever marks it as the agent's.
_COMMAND_LIKE = 'A message sent as your owner cannot start with "!" or "\\": it would read as a command.'
_ENCRYPTED = "The room is encrypted, and nothing is sent as your owner into an encrypted room."
_ENCRYPTION_UNKNOWN = "Nothing was sent: whether the room is encrypted could not be read"
_CONTACT_ROOM = "Nothing is written as your owner into a contact room."
_UNCONFIRMED = "The homeserver did not confirm the message. It may have been sent: read the room before sending again."


def _user_problem(user_id: str | None) -> str | None:
    return None if user_id is None or MATRIX_USER_ID.fullmatch(user_id) else _NOT_A_USER_ID


def _command_problem(text: str | None) -> str | None:
    # A recipient may drop leading whitespace and invisible format characters (Unicode category Cf, such as U+FEFF and
    # U+200B) before it looks for a command, so the first character that is neither decides.
    first = next(
        (
            character
            for character in (text or "")
            if not character.isspace() and unicodedata.category(character) != "Cf"
        ),
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
    attachments: tuple[str, ...] = ()
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
        if self.message is None and not self.attachments:
            return _NOTHING_TO_SEND
        if self.event_id is not None and self.message is None:
            return _REPLY_NEEDS_TEXT
        return _command_problem(self.message)


@dataclass
class _Written:
    """What a write has done so far, for its result and its audit line."""

    room_id: str | None
    event_ids: list[str] = field(default_factory=list)
    # The content of its first event; only its keys and size are logged.
    content: dict[str, Any] | None = None
    # Whom it invited: into a room, into a new room, or into a DM made for a send.
    invited: str | None = None


async def write_as_person(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
) -> dict[str, object]:
    """Make ``write`` as the person ``client`` acts as, marked as ``actor``'s agent's, and log one audit line for it.

    ``write`` has no ``problem``. Raises ``MatrixRequestError`` with what the model reads for a refusal or a failed
    request; a transport failure propagates as aiohttp raised it.
    """
    written = _Written(room_id=write.room_id)
    try:
        fields = await _write(client, actor, turn, write, written)
    except (MatrixRequestError, TimeoutError, aiohttp.ClientError) as error:
        _audit(actor, write, written, error=request_failure(error))
        raise
    _audit(actor, write, written)
    return fields


def _audit(actor: MatrixUserActor, write: MatrixUserWrite, written: _Written, *, error: str | None = None) -> None:
    content = written.content or {}
    logger.warning(
        "matrix_user_write_audit",
        agent=actor.agent_name,
        person=actor.person_user_id,
        room_id=written.room_id,
        action=write.action,
        status="ok" if error is None else "error",
        event_id=written.event_ids[0] if written.event_ids else None,
        content_keys=sorted(content),
        content_bytes=len(json.dumps(content).encode()) if content else 0,
        attachments=len(write.attachments),
        **({} if written.invited is None else {"invited": written.invited}),
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
    if write.room_id is not None and send_refused(write.room_id):
        raise MatrixRequestError(_CONTACT_ROOM)
    files = _turn_files(turn, write.attachments)
    limited = check_rate_limit(
        lock=_RATE_LIMIT_LOCK,
        recent_actions=_RECENT_WRITES,
        window_seconds=_WRITES_WINDOW_SECONDS,
        max_actions=_MAX_WRITES,
        tool_name="matrix_user",
        context=turn,
        room_id=write.room_id or write.user_id or "",
        weight=1 + len(files),
        limit_label="matrix_user writes",
    )
    if limited is not None:
        raise MatrixRequestError(limited)
    marker = {AGENT_AUTHORED_KEY: actor.agent_user_id}
    if write.action == "send":
        return await _send(client, actor, turn, write, written, files, marker)
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


def _turn_files(turn: ToolRuntimeContext, attachment_ids: tuple[str, ...]) -> list[AttachmentRecord]:
    """Return the stored files of the turn that ``attachment_ids`` name, refusing any it does not hold.

    A file goes out with its name as its text, which whoever sent the file chose, so a name that would read as a
    command is refused too.
    """
    files: list[AttachmentRecord] = []
    for attachment_id in attachment_ids:
        record = resolve_context_attachment_record(turn, attachment_id)
        if isinstance(record, str):
            raise MatrixRequestError(record)
        if (problem := _command_problem(record.filename)) is not None:
            msg = f"Attachment {attachment_id}'s file name is sent as its text. {problem}"
            raise MatrixRequestError(msg)
        files.append(record)
    return files


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
    except (MatrixRequestError, TimeoutError, aiohttp.ClientError) as error:
        msg = f"{_ENCRYPTION_UNKNOWN} ({request_failure(error).rstrip('.')})."
        raise MatrixRequestError(msg) from None
    if encrypted:
        raise MatrixRequestError(_ENCRYPTED)


def _delivered(outcome: MatrixSendOutcome) -> str:
    if isinstance(outcome, DeliveredMatrixEvent):
        return outcome.event_id
    if outcome.kind is MatrixDeliveryFailureKind.SEND_EXCEPTION:
        # The request may have reached the homeserver, and a retry is a new transaction: a second message.
        raise MatrixRequestError(_UNCONFIRMED)
    msg = f"Nothing was sent: {outcome.detail.rstrip('.')}."
    raise MatrixRequestError(msg)


async def _send(
    client: nio.AsyncClient,
    actor: MatrixUserActor,
    turn: ToolRuntimeContext,
    write: MatrixUserWrite,
    written: _Written,
    files: list[AttachmentRecord],
    marker: dict[str, str],
) -> dict[str, object]:
    """Send the text, formatted as the agent's own messages are, then each file, into the room or the person's DM."""
    if write.room_id is not None:
        room_id, made = write.room_id, False
    else:
        room_id, made = await _direct_room(client, actor.person_user_id, str(write.user_id))
    written.room_id = room_id
    if made:
        written.invited = write.user_id
    try:
        await _require_unencrypted(client, room_id)
    except MatrixRequestError as refused:
        if not made:
            raise
        # The DM made for this send stays, with them invited, so the refusal names it.
        msg = f"{_made_dm(room_id, str(write.user_id))}. {refused}"
        raise MatrixRequestError(msg) from None
    latest = None if write.thread_id is None else await _newest_in_thread(client, room_id, write.thread_id)
    if write.message is not None:
        content = format_message_with_mentions(
            turn.config,
            turn.runtime_paths,
            write.message.strip(),
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
        latest = _delivered(outcome)
        written.event_ids.append(latest)
    for record in files:
        sent = await send_file_message(
            client,
            room_id,
            record.local_path,
            thread_id=write.thread_id,
            latest_thread_event_id=latest,
            filename=record.filename,
            mimetype=record.mime_type,
            extra_content=marker,
        )
        if sent is None:
            already = f"; already sent into {room_id}: {', '.join(written.event_ids)}" if written.event_ids else ""
            msg = f"Attachment {record.attachment_id} could not be sent{already}."
            raise MatrixRequestError(msg)
        written.event_ids.append(sent)
        latest = sent
    fields: dict[str, object] = {"room_id": room_id, "event_id": written.event_ids[0]}
    if len(written.event_ids) > 1:
        fields["event_ids"] = written.event_ids
    return fields


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
            written.event_ids.append(redaction["event_id"])
        return {"room_id": room_id, "event_id": redaction.get("event_id")}
    await _require_unencrypted(client, room_id)
    if write.action == "react":
        written.content = {**build_reaction_content(event_id, str(write.emoji).strip()), **marker}
        outcome = await send_message_outcome(
            client,
            room_id,
            written.content,
            message_type="m.reaction",
            operation="matrix_user_react",
        )
    else:
        await _require_changeable(client, room_id, event_id, actor.person_user_id, edit=True)
        text = str(write.message).strip()
        new_content = format_message_with_mentions(turn.config, turn.runtime_paths, text, extra_content=marker)
        # The marker on both layers: history keeps the edit's replacement content.
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
    written.event_ids.append(_delivered(outcome))
    return {"room_id": room_id, "event_id": written.event_ids[0]}


async def _require_changeable(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    person: str,
    *,
    edit: bool,
) -> None:
    """Refuse an event that is not the person's own message or reaction, or, for an edit, not their own message.

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


async def _newest_in_thread(client: nio.AsyncClient, room_id: str, thread_id: str) -> str:
    """Return the newest event of a thread, which a reply that answers none of them follows; its root if it has none."""
    path = f"/rooms/{quote(room_id, safe='')}/relations/{quote(thread_id, safe='')}/m.thread"
    page = await matrix_json(client, "GET", f"{path}?{urlencode({'dir': 'b', 'limit': 1})}", version="v1")
    newest = next(iter(json_objects(page, "chunk")), {}).get("event_id")
    return newest if isinstance(newest, str) else thread_id


def _made_dm(room_id: str, them: str) -> str:
    return f"Made the DM {room_id} with {them}"


async def _direct_room(client: nio.AsyncClient, person: str, them: str) -> tuple[str, bool]:
    """Return the person's DM with ``them``, and whether it was made now, as one is when they have none.

    Their DM is the newest room the person's `m.direct` lists for ``them`` whose members, joined or invited, are only the
    two of them; one made earlier that could not be added there comes first. A room made now is added there.
    """
    direct_path = f"/user/{quote(person, safe='')}/account_data/m.direct"
    direct = await optional_json(client, direct_path) or {}
    listed = direct.get(them)
    listed = [room_id for room_id in listed if isinstance(room_id, str)] if isinstance(listed, list) else []
    unrecorded = _UNRECORDED_DIRECT_ROOMS.get((person, them))
    candidates = [*([unrecorded] if unrecorded is not None and unrecorded not in listed else []), *reversed(listed)]
    for room_id in candidates:
        if await _only_the_two(client, room_id, person, them):
            if room_id not in listed:
                await _record_direct_room(client, direct_path, {**direct, them: [*listed, room_id]}, person, them)
            return room_id, False
    created = await matrix_json(
        client,
        "POST",
        "/createRoom",
        {"preset": "trusted_private_chat", "is_direct": True, "invite": [them]},
    )
    room_id = _created_room(created)
    _UNRECORDED_DIRECT_ROOMS[(person, them)] = room_id
    await _record_direct_room(client, direct_path, {**direct, them: [*listed, room_id]}, person, them)
    return room_id, True


async def _record_direct_room(
    client: nio.AsyncClient,
    direct_path: str,
    direct: dict[str, Any],
    person: str,
    them: str,
) -> None:
    """Write ``direct`` as the person's `m.direct`, in which the last room listed for ``them`` is their new DM."""
    room_id = direct[them][-1]
    try:
        await matrix_json(client, "PUT", direct_path, direct)
    except (MatrixRequestError, TimeoutError, aiohttp.ClientError) as error:
        msg = (
            f"{_made_dm(room_id, them)}, but could not record it as one ({request_failure(error).rstrip('.')}). "
            f"Nothing was sent; to send there, use room_id {room_id}."
        )
        raise MatrixRequestError(msg) from None
    _UNRECORDED_DIRECT_ROOMS.pop((person, them), None)


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
