"""What the `matrix_user` toolkit reads as the person: their rooms, messages, files, and people.

Each read takes a client that acts as the person and returns its result's fields, with rooms, messages, and people as
compact lines. Every request goes through ``matrix_json``, which reads an answer up to 2 MiB and retries a 429 at most
twice, waiting at most 5 seconds each time, as matrix-mcp does; only a file's download, which is not JSON, goes through
the capped media download instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote, urlencode
from uuid import uuid4

import aiohttp

from mindroom.attachments.storage import register_bytes_attachment
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.constants import RETAINED_MEDIA_MAX_BYTES
from mindroom.matrix.media import (
    MATRIX_MEDIA_EVENT_TYPES,
    download_media_bytes,
    media_mime_type,
    parse_matrix_media_event_source,
)
from mindroom.matrix.visible_body import strip_matrix_rich_reply_fallback

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import nio

__all__ = [
    "MatrixRequestError",
    "download_file",
    "mark_read",
    "matrix_json",
    "optional_json",
    "read_event",
    "read_messages",
    "read_people",
    "read_room",
    "read_rooms",
    "room_encrypted",
    "search_messages",
    "transport_failure",
]

_MAX_ANSWER_BYTES = 2 * 1024 * 1024
_RATE_LIMIT_RETRIES = 2
_MAX_RATE_LIMIT_WAIT_SECONDS = 5.0
# The wait after a 429 that names none.
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 1.0
_ROOMS_LIMIT, _MAX_ROOMS_LIMIT = 50, 200
_MEMBERS_LIMIT, _MAX_MEMBERS_LIMIT = 50, 200
_PEOPLE_LIMIT, _MAX_PEOPLE_LIMIT = 10, 50
_MESSAGES_LIMIT, _MAX_MESSAGES_LIMIT = 30, 100
_SEARCH_LIMIT, _MAX_SEARCH_LIMIT = 10, 50
_MAX_TEXT_CHARACTERS = 2000
# For the other fields a room member writes, such as a file's name or a reaction's key.
_MAX_NAME_CHARACTERS = 200
# What a page of messages asks for: the messages, and the reactions to count on them. Some homeservers filter by type
# after taking the newest events, so a page can be shorter than asked while older messages remain; only the absence of
# the homeserver's token ends the history.
_MESSAGES_FILTER = json.dumps({"types": ["m.room.message", "m.room.encrypted", "m.reaction"]})
type _MediaKind = Literal["audio", "file", "image", "video"]
_MEDIA_KINDS: dict[str, _MediaKind] = {"m.image": "image", "m.file": "file", "m.video": "video", "m.audio": "audio"}
# An event about another one, rather than a message of its own.
_EDIT, _REACTION = "m.replace", "m.annotation"
_THREAD = "m.thread"
# The first millisecond of the year 10000, which no date reaches.
_END_OF_DATES_MS = 253_402_300_800_000
_MATRIX_USER_ID = re.compile(r"@[^:\s]+:\S+")
# The room list's groups, in the order it lists them.
_UNREAD, _INVITED, _OTHER = range(3)
# Every event field the room list reads. The homeserver trims each event of the joined rooms to these, so a long last
# message costs a few bytes; `m.direct`'s content, keyed by people, is account data, which it does not trim.
_ROOM_LIST_EVENT_FIELDS = ["type", "state_key", "sender", "content.name", "content.is_direct", "content.unread"]
_TOO_MANY_ROOMS = (
    'This account has too many rooms to list at once. Ask the person for the room\'s name or ID and use action "room".'
)
_NO_FILE = "That message has no file."
_ENCRYPTED = "That message is encrypted and cannot be read."
_NOT_FETCHED = (
    "The file could not be fetched (refused, unreachable, or larger than "
    f"{RETAINED_MEDIA_MAX_BYTES // (1024 * 1024)} MiB)."
)
_OTHER_ROOM = "That event is in another room."
_NOTHING_TO_MARK = "The room has no events to mark read."
_UNREACHABLE = "The Matrix homeserver could not be reached."
_TIMED_OUT = "The homeserver took too long to answer."


class MatrixRequestError(Exception):
    """A request the homeserver refused or answered unreadably; the message is what the model reads."""

    def __init__(self, message: str, *, status: int | None = None, errcode: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.errcode = errcode


class _AnswerTooLargeError(MatrixRequestError):
    """An answer over 2 MiB."""


async def matrix_json(
    client: nio.AsyncClient,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
    *,
    version: str = "v3",
) -> dict[str, Any]:
    """Return the JSON object the homeserver answers to one client-server API request, ``path`` following its version.

    Raises ``MatrixRequestError`` for an error answer, one over 2 MiB, or one that is not a JSON object; a transport
    failure propagates as aiohttp raised it.
    """
    headers = {"Authorization": f"Bearer {client.access_token}", "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body)
    retries = 0
    while True:
        response = await client.send(method, f"/_matrix/client/{version}{path}", data, headers)
        try:
            content = await collect_bounded_bytes(response.content.iter_chunked(65536), max_bytes=_MAX_ANSWER_BYTES)
        except ByteLimitExceededError:
            msg = "The homeserver's answer was larger than 2 MiB."
            raise _AnswerTooLargeError(msg) from None
        finally:
            response.release()
        answer = _json_object(content)
        if response.status == HTTPStatus.TOO_MANY_REQUESTS and retries < _RATE_LIMIT_RETRIES:
            wait = _rate_limit_wait(response.headers.get("Retry-After"), answer)
            if wait <= _MAX_RATE_LIMIT_WAIT_SECONDS:
                retries += 1
                await asyncio.sleep(wait)
                continue
        if response.status >= HTTPStatus.BAD_REQUEST:
            raise _refusal(response.status, answer or {})
        if answer is None:
            msg = "The homeserver's answer was not a JSON object."
            raise MatrixRequestError(msg, status=response.status)
        return answer


def _json_object(content: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _rate_limit_wait(retry_after: str | None, answer: dict[str, Any] | None) -> float:
    """Return the seconds a 429 asks to wait: its `Retry-After` header, else its `retry_after_ms`."""
    if retry_after is not None and retry_after.strip().isdigit():
        return float(retry_after)
    milliseconds = (answer or {}).get("retry_after_ms")
    if isinstance(milliseconds, int) and not isinstance(milliseconds, bool):
        return max(milliseconds, 0) / 1000
    return _DEFAULT_RATE_LIMIT_WAIT_SECONDS


def _refusal(status: int, answer: dict[str, Any]) -> MatrixRequestError:
    errcode = answer.get("errcode") if isinstance(answer.get("errcode"), str) else None
    error = answer.get("error") if isinstance(answer.get("error"), str) else None
    message = f"Matrix answered {status}" + (f" {errcode}" if errcode else "") + (f": {error}" if error else "")
    return MatrixRequestError(message, status=status, errcode=errcode)


def transport_failure(failed: TimeoutError | aiohttp.ClientError) -> str:
    """Return what the model reads of a request that got no answer.

    Never the transport's own text, which names the host or URL it requested. Some of aiohttp's timeouts are
    ClientErrors too, so the timeout is checked first.
    """
    return _TIMED_OUT if isinstance(failed, TimeoutError) else _UNREACHABLE


async def optional_json(client: nio.AsyncClient, path: str) -> dict[str, Any] | None:
    """Return the answer to a GET of something that may not exist, None only when the homeserver says it does not."""
    try:
        return await matrix_json(client, "GET", path)
    except MatrixRequestError as error:
        if error.status == HTTPStatus.NOT_FOUND and error.errcode == "M_NOT_FOUND":
            return None
        raise


async def room_encrypted(client: nio.AsyncClient, room_id: str) -> bool:
    """Return whether a room is encrypted: it is not only when the homeserver says the room has no encryption state.

    Any other answer that is not the state raises, as ``matrix_json`` does.
    """
    return await optional_json(client, f"/rooms/{quote(room_id, safe='')}/state/m.room.encryption") is not None


def _clamped(limit: int | None, default: int, maximum: int) -> int:
    return default if limit is None else max(1, min(limit, maximum))


def _mapping(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _objects(answer: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the JSON objects listed under ``key``, such as a page's events."""
    listed = answer.get(key)
    return [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []


def _events(section: object) -> list[dict[str, Any]]:
    return _objects(_mapping(section), "events")


def _state_event(events: list[dict[str, Any]], event_type: str, state_key: str = "") -> dict[str, Any]:
    """Return the last ``event_type`` state event with ``state_key`` in ``events``, or an empty one."""
    return next(
        (
            event
            for event in reversed(events)
            if event.get("type") == event_type and event.get("state_key") == state_key
        ),
        {},
    )


def _content(event: dict[str, Any]) -> dict[str, Any]:
    return _mapping(event.get("content"))


def _account_data(section: object, event_type: str) -> dict[str, Any]:
    """Return the content of the ``event_type`` account data in ``section``, or an empty one."""
    return next((_content(event) for event in _events(section) if event.get("type") == event_type), {})


def _text(value: object) -> str | None:
    """Return ``value`` as one line of text, so a name cannot start a line of its own; None when it is not text."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _direct_partners(direct: dict[str, Any]) -> dict[str, str]:
    """Return the other person of each direct chat that the person's `m.direct` lists."""
    partners: dict[str, str] = {}
    for user_id, room_ids in sorted(direct.items()):
        for room_id in room_ids if isinstance(room_ids, list) else ():
            if isinstance(room_id, str):
                partners.setdefault(room_id, user_id)
    return partners


def _line(*columns: str | None) -> str:
    """Return one result line of ``columns``, leaving out None ones.

    A ``|`` in a column, such as a room name that anyone inviting the person sets, becomes ``¦``, so it cannot fake
    another column.
    """
    return " | ".join(column.replace("|", "¦") for column in columns if column is not None)


def _heroes(room: dict[str, Any]) -> str | None:
    """Return the members the homeserver picked to name a room without a name, comma-separated, if it sent any."""
    heroes = _mapping(room.get("summary")).get("m.heroes")
    names = [hero for hero in heroes if isinstance(hero, str)] if isinstance(heroes, list) else []
    return ", ".join(names) or None


@dataclass(frozen=True, slots=True)
class _ListedRoom:
    """One room of the person's room list."""

    group: int
    room_id: str
    # The room's name, or for a room without one the other person of a direct chat or the homeserver's heroes.
    name: str | None
    # Its unread counts or its invitation; None for a room with neither.
    status: str | None

    def order(self) -> tuple[int, bool, str, str]:
        """Order by group, then by name, rooms without one last."""
        return self.group, self.name is None, (self.name or "").casefold(), self.room_id

    def line(self) -> str:
        return _line(self.room_id, self.name or "(no name)", self.status)


def _rooms_filter() -> dict[str, object]:
    """Return the room list's sync filter, unique to this call.

    Synapse answers a sync request identical to a recent one from a cache, with stale counts and invitations;
    excluding a presence type nobody sends changes the request and not its answer.
    """
    return {
        "event_fields": _ROOM_LIST_EVENT_FIELDS,
        "presence": {"types": [], "not_types": [f"mindroom.uncached.{uuid4().hex}"]},
        "account_data": {"types": ["m.direct"]},
        "room": {
            # Only the name: an invitation's stripped state, which says who sent it, is not subject to this filter.
            # Synapse sends a room's summary, whose heroes name a room without a name, only when members are lazy-loaded.
            "state": {"types": ["m.room.name"], "lazy_load_members": True},
            # A name changed among the newest events is in the timeline, not in the state before it.
            "timeline": {"limit": 1},
            "ephemeral": {"types": []},
            "account_data": {"types": ["m.marked_unread"]},
        },
    }


def _joined_room(room_id: str, room: dict[str, Any], partners: dict[str, str]) -> _ListedRoom:
    events = [*_events(room.get("state")), *_events(room.get("timeline"))]
    counts = _mapping(room.get("unread_notifications"))
    unread, mentions = _count(counts.get("notification_count")), _count(counts.get("highlight_count"))
    marked = _account_data(room.get("account_data"), "m.marked_unread").get("unread") is True
    # Only what applies, so a quiet room costs no zero counts.
    attention = [
        *([f"unread {unread}"] if unread else []),
        *([f"mentions {mentions}"] if mentions else []),
        *(["marked unread"] if marked else []),
    ]
    return _ListedRoom(
        group=_UNREAD if attention else _OTHER,
        room_id=room_id,
        name=_text(_content(_state_event(events, "m.room.name")).get("name")) or partners.get(room_id) or _heroes(room),
        status=", ".join(attention) or None,
    )


def _invited_room(room_id: str, room: dict[str, Any], person: str) -> _ListedRoom:
    """Read an invitation from the room's stripped state, which holds the person's invitation itself."""
    events = _events(room.get("invite_state"))
    invitation = _state_event(events, "m.room.member", person)
    inviter = invitation.get("sender") if isinstance(invitation.get("sender"), str) else None
    direct = _content(invitation).get("is_direct") is True
    return _ListedRoom(
        group=_INVITED,
        room_id=room_id,
        name=_text(_content(_state_event(events, "m.room.name")).get("name")) or (inviter if direct else None),
        status=f"invited by {inviter}" if inviter else "invited",
    )


async def read_rooms(client: nio.AsyncClient, *, query: str | None, limit: int | None) -> dict[str, object]:
    """Return the person's rooms: unread first, then invitations, then the rest, each group by name.

    A room without a name is named by the other person of a direct chat, else by the members the homeserver picks.
    ``query`` keeps the rooms whose name contains it, whatever its case; ``limit`` (default 50, at most 200) bounds the
    lines, and ``left_out`` counts the others.
    """
    parameters = {"timeout": 0, "set_presence": "offline", "filter": json.dumps(_rooms_filter())}
    try:
        sync = await matrix_json(client, "GET", f"/sync?{urlencode(parameters)}")
    except _AnswerTooLargeError:
        raise MatrixRequestError(_TOO_MANY_ROOMS) from None
    partners = _direct_partners(_account_data(sync.get("account_data"), "m.direct"))
    rooms = _mapping(sync.get("rooms"))
    listed = [
        *(_joined_room(room_id, _mapping(room), partners) for room_id, room in _mapping(rooms.get("join")).items()),
        *(
            _invited_room(room_id, _mapping(room), client.user_id)
            for room_id, room in _mapping(rooms.get("invite")).items()
        ),
    ]
    if query and (needle := query.strip().casefold()):
        listed = [room for room in listed if room.name is not None and needle in room.name.casefold()]
    listed.sort(key=_ListedRoom.order)
    shown = listed[: _clamped(limit, _ROOMS_LIMIT, _MAX_ROOMS_LIMIT)]
    return {"rooms": [room.line() for room in shown], "left_out": len(listed) - len(shown)}


async def read_room(client: nio.AsyncClient, room_id: str, *, limit: int | None) -> dict[str, object]:
    """Return a room's name, topic, encryption, whether it is a direct chat, and up to ``limit`` joined members.

    Encryption counts as off only when the homeserver says the room has no encryption state.
    """
    room = quote(room_id, safe="")
    joined = _mapping((await matrix_json(client, "GET", f"/rooms/{room}/joined_members")).get("joined"))
    name = _mapping(await optional_json(client, f"/rooms/{room}/state/m.room.name")).get("name")
    topic = _mapping(await optional_json(client, f"/rooms/{room}/state/m.room.topic")).get("topic")
    encrypted = await room_encrypted(client, room_id)
    person = quote(client.user_id, safe="")
    direct = _mapping(await optional_json(client, f"/user/{person}/account_data/m.direct"))
    members = [
        _line(user_id, _text(_mapping(member).get("display_name"))) for user_id, member in sorted(joined.items())
    ]
    return {
        "room_id": room_id,
        "name": _text(name),
        "topic": topic if isinstance(topic, str) else None,
        "encrypted": encrypted,
        "dm": room_id in _direct_partners(direct),
        "joined_members": len(joined),
        "members": members[: _clamped(limit, _MEMBERS_LIMIT, _MAX_MEMBERS_LIMIT)],
    }


async def read_people(client: nio.AsyncClient, query: str, *, limit: int | None) -> dict[str, object]:
    """Return the profile of a full Matrix ID, or else the people the user directory finds for ``query``.

    ``limit`` (default 10, at most 50) bounds a search, and ``limited`` says whether the directory had more.
    """
    query = query.strip()
    if _MATRIX_USER_ID.fullmatch(query):
        profile = await matrix_json(client, "GET", f"/profile/{quote(query, safe='')}")
        return {"people": [_line(query, _text(profile.get("displayname")))]}
    count = _clamped(limit, _PEOPLE_LIMIT, _MAX_PEOPLE_LIMIT)
    found = await matrix_json(client, "POST", "/user_directory/search", {"search_term": query, "limit": count})
    results = found.get("results")
    people = [
        _line(result["user_id"], _text(result.get("display_name")))
        for result in (results if isinstance(results, list) else [])
        if isinstance(result, dict) and isinstance(result.get("user_id"), str)
    ]
    return {"people": people[:count], "limited": found.get("limited") is True}


def _relation(event: dict[str, Any]) -> dict[str, Any]:
    return _mapping(_content(event).get("m.relates_to"))


def _bundled(event: dict[str, Any], rel_type: str) -> dict[str, Any]:
    """Return what the homeserver bundled with ``event`` of the ``rel_type`` relations to it."""
    return _mapping(_mapping(_mapping(event.get("unsigned")).get("m.relations")).get(rel_type))


def _redacted(event: dict[str, Any]) -> bool:
    return "redacted_because" in _mapping(event.get("unsigned"))


def _is_message(event: dict[str, Any]) -> bool:
    """Whether ``event`` is a message of its own, not an edit or a reaction, which are about another one."""
    return (
        event.get("type") in {"m.room.message", "m.room.encrypted"}
        and isinstance(event.get("event_id"), str)
        # A tuple, as a sender's `rel_type` need not be hashable.
        and _relation(event).get("rel_type") not in (_EDIT, _REACTION)
    )


def _replaces(edit: dict[str, Any], message: dict[str, Any]) -> bool:
    """Whether ``edit`` may replace ``message``: new content for it, sent by its sender as an event of its type."""
    relation = _relation(edit)
    return (
        relation.get("rel_type") == _EDIT
        and relation.get("event_id") == message.get("event_id")
        and edit.get("sender") == message.get("sender")
        and edit.get("type") == message.get("type")
        and isinstance(edit.get("event_id"), str)
        and isinstance(_content(edit).get("m.new_content"), dict)
        and not _redacted(edit)
    )


def _sent_order(event: dict[str, Any]) -> tuple[int, str]:
    return _count(event.get("origin_server_ts")), str(event.get("event_id"))


def _minute(timestamp: object) -> str:
    """Return a UTC time to the minute, such as ``2026-11-16T12:00Z``, or ``…`` for a timestamp that is no date."""
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not 0 <= timestamp < _END_OF_DATES_MS:
        return "…"
    return datetime.fromtimestamp(timestamp / 1000, UTC).strftime("%Y-%m-%dT%H:%MZ")


def _cut(text: str | None, limit: int = _MAX_TEXT_CHARACTERS) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]} […]"


def _media_kind(content: dict[str, Any]) -> _MediaKind | None:
    """Return the kind of file a message carries, None for a message without; a `msgtype` that is not text is none."""
    msgtype = content.get("msgtype")
    return _MEDIA_KINDS.get(msgtype) if isinstance(msgtype, str) else None


def _file_name(content: dict[str, Any]) -> str:
    """Return the name of a message's file: its filename, else its body."""
    return _text(content.get("filename")) or _text(content.get("body")) or "unnamed"


def _said(message: dict[str, Any], content: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return what a message says, and the note naming its file when it has one."""
    if message.get("type") == "m.room.encrypted":
        return "[encrypted, not readable]", None
    body = content.get("body")
    if _media_kind(content) is None:
        if isinstance(body, str) and "m.in_reply_to" in _relation(message):
            # A reply's body starts with the message it answers, quoted, which would repeat that message's text.
            body = strip_matrix_rich_reply_fallback(body)
        return _cut(_text(body)), None
    body, name = _text(body), _file_name(content)
    info = _mapping(content.get("info"))
    size = _count(info.get("size"))
    details = [_cut(name, _MAX_NAME_CHARACTERS), _text(info.get("mimetype")), f"{size} bytes" if size else None]
    # A body that is not the file's name is a caption.
    caption = body if body != name else None
    return _cut(caption), f"[file: {', '.join(detail for detail in details if detail)}]"


def _reactions_note(message: dict[str, Any], senders_by_key: dict[str, set[str]]) -> str | None:
    """Return how many people reacted with each key, most first, from the page and the homeserver's bundled counts.

    A bundled count already covers the page's reactions, so a key counts the larger of the two.
    """
    counts = {key: len(senders) for key, senders in senders_by_key.items()}
    for group in _objects(_bundled(message, _REACTION), "chunk"):
        key = group.get("key")
        if isinstance(key, str):
            counts[key] = max(counts.get(key, 0), _count(group.get("count")))
    shown = [
        (name, count) for key, count in counts.items() if count and (name := _cut(_text(key), _MAX_NAME_CHARACTERS))
    ]
    shown.sort(key=lambda reaction: (-reaction[1], reaction[0]))
    return f"[reactions: {' '.join(f'{name}{count}' for name, count in shown)}]" if shown else None


def _thread_note(message: dict[str, Any], *, thread_page: bool) -> str | None:
    """Return the thread a reply in one is in, unless the page is that thread's, or how many replies a root has."""
    relation = _relation(message)
    if relation.get("rel_type") == _THREAD and (root := _cut(_text(relation.get("event_id")), _MAX_NAME_CHARACTERS)):
        return None if thread_page else f"[in thread {root}]"
    replies = _count(_bundled(message, _THREAD).get("count"))
    return f"[thread: {replies} {'reply' if replies == 1 else 'replies'}]" if replies else None


def _message_columns(
    message: dict[str, Any],
    edits: list[dict[str, Any]],
    reactions: dict[str, set[str]],
    *,
    edited: str = "[edited]",
    thread_page: bool = False,
) -> tuple[str, str, str, str]:
    """Return a message's ID, minute, sender, and text, with its latest edit and its reactions folded in.

    The edits are the page's and the one the homeserver bundles; the latest that may replace the message wins, and
    ``edited`` says so. On a ``thread_page``, a page of one thread, a reply does not name its thread.
    """
    latest = max(
        (edit for edit in [*edits, _bundled(message, _EDIT)] if _replaces(edit, message)),
        key=_sent_order,
        default=None,
    )
    content = _content(message) if latest is None else _mapping(_content(latest).get("m.new_content"))
    text, file_note = _said(message, content)
    notes = [
        text,
        edited if latest else None,
        file_note,
        _reactions_note(message, reactions),
        _thread_note(message, thread_page=thread_page),
    ]
    return (
        str(message.get("event_id")),
        _minute(message.get("origin_server_ts")),
        _text(message.get("sender")) or "…",
        " ".join(note for note in notes if note) or "(no text)",
    )


def _message_lines(events: list[dict[str, Any]], *, thread_page: bool = False) -> list[str]:
    """Return a line for each message among ``events``, in their order, with the edits and reactions among them.

    On a ``thread_page``, a page of one thread, a reply does not name its thread.
    """
    events = [event for event in events if not _redacted(event)]
    edits: dict[str, list[dict[str, Any]]] = {}
    reactions: dict[str, dict[str, set[str]]] = {}
    for event in events:
        relation = _relation(event)
        target, key, sender = relation.get("event_id"), relation.get("key"), event.get("sender")
        if not isinstance(target, str):
            continue
        if relation.get("rel_type") == _EDIT:
            edits.setdefault(target, []).append(event)
        elif relation.get("rel_type") == _REACTION and isinstance(key, str) and isinstance(sender, str):
            reactions.setdefault(target, {}).setdefault(key, set()).add(sender)
    return [
        _line(
            *_message_columns(
                event,
                edits.get(event["event_id"], []),
                reactions.get(event["event_id"], {}),
                thread_page=thread_page,
            ),
        )
        for event in events
        if _is_message(event)
    ]


def _messages_page(lines: list[str], next_page_token: object = None) -> dict[str, object]:
    """Return message lines and, when the homeserver has more, the token that continues them."""
    page: dict[str, object] = {"messages": lines}
    if isinstance(next_page_token, str) and next_page_token:
        page["next_page_token"] = next_page_token
    return page


def _page_query(parameters: dict[str, object], page_token: str | None) -> str:
    """Return the query string of ``parameters``, continuing from ``page_token`` when there is one."""
    return urlencode({**parameters, **({"from": page_token} if page_token else {})})


async def read_messages(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_id: str | None,
    thread_id: str | None,
    page_token: str | None,
    limit: int | None,
) -> dict[str, object]:
    """Return a room's messages as lines, oldest first, and the token of the page before them when there is one.

    By default the newest ``limit`` (default 30, at most 100) events, ``page_token`` continuing into older history;
    with ``thread_id``, the thread's root and its newest replies; with ``event_id``, that event and up to half of
    ``limit`` events on each side. The edits and reactions among the events are folded into their messages.
    """
    room = quote(room_id, safe="")
    count = _clamped(limit, _MESSAGES_LIMIT, _MAX_MESSAGES_LIMIT)
    if thread_id is not None:
        return await _thread(client, room, quote(thread_id, safe=""), page_token=page_token, count=count)
    if event_id is not None:
        return await _around(client, room, quote(event_id, safe=""), count=count)
    parameters = _page_query({"dir": "b", "limit": count, "filter": _MESSAGES_FILTER}, page_token)
    page = await matrix_json(client, "GET", f"/rooms/{room}/messages?{parameters}")
    return _messages_page(_message_lines(_objects(page, "chunk")[::-1]), page.get("end"))


async def _thread(
    client: nio.AsyncClient,
    room: str,
    thread: str,
    *,
    page_token: str | None,
    count: int,
) -> dict[str, object]:
    """Return a thread's root, unless continuing, and then its newest ``count`` replies before ``page_token``."""
    root = [] if page_token else [await matrix_json(client, "GET", f"/rooms/{room}/event/{thread}")]
    parameters = _page_query({"dir": "b", "limit": count}, page_token)
    replies = await matrix_json(client, "GET", f"/rooms/{room}/relations/{thread}/m.thread?{parameters}", version="v1")
    lines = _message_lines([*root, *_objects(replies, "chunk")[::-1]], thread_page=True)
    return _messages_page(lines, replies.get("next_batch"))


async def _around(client: nio.AsyncClient, room: str, event: str, *, count: int) -> dict[str, object]:
    """Return an event with up to ``count // 2`` events on each side, even from a homeserver that sends more."""
    side = count // 2
    parameters = urlencode({"limit": side * 2, "filter": _MESSAGES_FILTER})
    context = await matrix_json(client, "GET", f"/rooms/{room}/context/{event}?{parameters}")
    # The events before come newest first.
    before = _objects(context, "events_before")[:side][::-1]
    after = _objects(context, "events_after")[:side]
    return _messages_page(_message_lines([*before, _mapping(context.get("event")), *after]))


async def search_messages(
    client: nio.AsyncClient,
    query: str,
    *,
    room_id: str | None,
    page_token: str | None,
    limit: int | None,
) -> dict[str, object]:
    """Return the messages the homeserver finds for ``query``, newest first, each line starting with its room.

    It searches the person's joined rooms, or only ``room_id``; ``limit`` defaults to 10, at most 50, and
    ``page_token`` continues. An edit found is shown under the ID of the message it edits, as its own sender wrote
    it, unless that message is found too; a deleted message is not shown.
    """
    search_filter: dict[str, object] = {"limit": _clamped(limit, _SEARCH_LIMIT, _MAX_SEARCH_LIMIT)}
    if room_id is not None:
        search_filter["rooms"] = [room_id]
    criteria = {"search_term": query, "order_by": "recent", "keys": ["content.body"], "filter": search_filter}
    path = "/search" + (f"?{urlencode({'next_batch': page_token})}" if page_token else "")
    answer = await matrix_json(client, "POST", path, {"search_categories": {"room_events": criteria}})
    found = _mapping(_mapping(answer.get("search_categories")).get("room_events"))
    lines: dict[str, str] = {}
    for hit in _objects(found, "results"):
        event = _mapping(hit.get("result"))
        room = event.get("room_id")
        if _redacted(event) or not isinstance(room, str):
            continue
        if _relation(event).get("rel_type") != _EDIT:
            if _is_message(event):
                # The message itself, with the homeserver's latest edit of it, replaces the line of an edit found.
                lines[event["event_id"]] = _line(room, *_message_columns(event, [], {}))
            continue
        # The edit's sender need not be the message's author, so the edit is shown as its sender's, not the message.
        original = _relation(event).get("event_id")
        fields = ("type", "sender", "origin_server_ts")
        message = {"event_id": original, **{field: event.get(field) for field in fields}}
        if isinstance(original, str) and original not in lines and _is_message(message) and _replaces(event, message):
            lines[original] = _line(room, *_message_columns(message, [event], {}, edited="[edit]"))
    return _messages_page(list(lines.values()), found.get("next_batch"))


async def read_event(client: nio.AsyncClient, room_id: str, event_id: str) -> dict[str, Any]:
    """Return the event ``event_id`` of the room ``room_id``, refusing one that says it is in another room.

    A homeserver finds an event under a room only when the room holds it; the event's own room is checked too, so a
    homeserver that answers otherwise moves no marker and hands over no file.
    """
    event = await matrix_json(client, "GET", f"/rooms/{quote(room_id, safe='')}/event/{quote(event_id, safe='')}")
    if "room_id" in event and event["room_id"] != room_id:
        raise MatrixRequestError(_OTHER_ROOM)
    return event


def _download_attachment_id(room_id: str, event_id: str, *, for_room_id: str, for_thread_id: str | None) -> str:
    """Return the attachment ID of a message's file fetched for one conversation, the same on each fetch there.

    The attachment store names the file the agent received with a message after the event alone, and that record
    belongs to the conversation the message reached; an ID of its own keeps a download from taking it over.
    """
    scope = json.dumps([for_room_id, for_thread_id, room_id, event_id])
    return f"att_{hashlib.sha256(scope.encode()).hexdigest()[:24]}"


async def download_file(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    storage_path: Path,
    for_room_id: str,
    for_thread_id: str | None,
) -> dict[str, object]:
    """Download a message's file as the person and keep it as an attachment of the conversation it is fetched for.

    The download stops at the attachment store's size cap. Returns the attachment's ID, name, type, and size.
    """
    event = await read_event(client, room_id, event_id)
    if event.get("type") == "m.room.encrypted":
        raise MatrixRequestError(_ENCRYPTED)
    content, media = _content(event), parse_matrix_media_event_source(event)
    kind = _media_kind(content)
    if kind is None or not isinstance(media, MATRIX_MEDIA_EVENT_TYPES):
        raise MatrixRequestError(_NO_FILE)
    # Refused, unreachable, or over the cap: the download says which only in the log.
    data = await download_media_bytes(client, media)
    if data is None:
        raise MatrixRequestError(_NOT_FETCHED)
    name, mime_type = _file_name(content), media_mime_type(media) or "application/octet-stream"
    record = await run_blocking_until_complete(
        partial(
            register_bytes_attachment,
            storage_path,
            data,
            kind=kind,
            mime_type=mime_type,
            attachment_id=_download_attachment_id(
                room_id,
                event_id,
                for_room_id=for_room_id,
                for_thread_id=for_thread_id,
            ),
            filename=name,
            room_id=for_room_id,
            thread_id=for_thread_id,
            sender=media.sender,
        ),
    )
    if record is None:
        raise MatrixRequestError(_NOT_FETCHED)
    return {"attachment_id": record.attachment_id, "name": name, "mime_type": mime_type, "size_bytes": len(data)}


async def mark_read(client: nio.AsyncClient, room_id: str, *, event_id: str | None) -> dict[str, object]:
    """Mark a room read up to ``event_id``, by default its newest event, and clear the person's marked-unread flag.

    It moves the read marker and a private read receipt, which only the person sees. An event the room does not hold
    moves nothing. The result names the event only when the room's newest was taken.
    """
    room = quote(room_id, safe="")
    if event_id is None:
        newest = await matrix_json(client, "GET", f"/rooms/{room}/messages?{urlencode({'dir': 'b', 'limit': 1})}")
        read_up_to = next(iter(_objects(newest, "chunk")), {}).get("event_id")
        if not isinstance(read_up_to, str):
            raise MatrixRequestError(_NOTHING_TO_MARK)
    else:
        await read_event(client, room_id, event_id)
        read_up_to = event_id
    markers = {"m.fully_read": read_up_to, "m.read.private": read_up_to}
    await matrix_json(client, "POST", f"/rooms/{room}/read_markers", markers)
    person = quote(client.user_id, safe="")
    try:
        await matrix_json(client, "PUT", f"/user/{person}/rooms/{room}/account_data/m.marked_unread", {"unread": False})
    except (MatrixRequestError, TimeoutError, aiohttp.ClientError) as error:
        failure = str(error) if isinstance(error, MatrixRequestError) else transport_failure(error)
        msg = f"The room is read up to {read_up_to}, but it is still marked unread: {failure}"
        raise MatrixRequestError(msg) from None
    return {} if event_id is not None else {"read_up_to": read_up_to}
