"""What the `matrix_user` toolkit reads as the person: their rooms, one room, and people.

Each read takes a client that acts as the person and returns its result's fields, with rooms and people as compact
lines. Every request goes through ``matrix_json``, which reads an answer up to 2 MiB and retries a 429 at most twice,
waiting at most 5 seconds each time, as matrix-mcp does.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlencode
from uuid import uuid4

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

__all__ = ["MatrixRequestError", "matrix_json", "read_people", "read_room", "read_rooms"]

_CLIENT_API = "/_matrix/client/v3"
_MAX_ANSWER_BYTES = 2 * 1024 * 1024
_RATE_LIMIT_RETRIES = 2
_MAX_RATE_LIMIT_WAIT_SECONDS = 5.0
# The wait after a 429 that names none.
_DEFAULT_RATE_LIMIT_WAIT_SECONDS = 1.0
_ROOMS_LIMIT, _MAX_ROOMS_LIMIT = 50, 200
_MEMBERS_LIMIT, _MAX_MEMBERS_LIMIT = 50, 200
_PEOPLE_LIMIT, _MAX_PEOPLE_LIMIT = 10, 50
_MATRIX_USER_ID = re.compile(r"@[^:\s]+:\S+")
# The room list's groups, in the order it lists them.
_UNREAD, _INVITED, _OTHER = range(3)
# Every event field the room list reads. The homeserver trims each event of the joined rooms to these, so a long last
# message costs a few bytes; `m.direct`'s content, keyed by people, is account data, which it does not trim.
_ROOM_LIST_EVENT_FIELDS = ["type", "state_key", "sender", "content.name", "content.is_direct", "content.unread"]
_TOO_MANY_ROOMS = (
    'This account has too many rooms to list at once. Ask the person for the room\'s name or ID and use action "room".'
)


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
) -> dict[str, Any]:
    """Return the JSON object the homeserver answers to one client-server API request, ``path`` following ``/v3``.

    Raises ``MatrixRequestError`` for an error answer, one over 2 MiB, or one that is not a JSON object; a transport
    failure propagates as aiohttp raised it.
    """
    headers = {"Authorization": f"Bearer {client.access_token}", "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body)
    retries = 0
    while True:
        response = await client.send(method, _CLIENT_API + path, data, headers)
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


async def _optional_json(client: nio.AsyncClient, path: str) -> dict[str, Any] | None:
    """Return the answer to a GET of something that may not exist, None only when the homeserver says it does not."""
    try:
        return await matrix_json(client, "GET", path)
    except MatrixRequestError as error:
        if error.status == HTTPStatus.NOT_FOUND and error.errcode == "M_NOT_FOUND":
            return None
        raise


def _clamped(limit: int | None, default: int, maximum: int) -> int:
    return default if limit is None else max(1, min(limit, maximum))


def _mapping(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _events(section: object) -> list[dict[str, Any]]:
    events = _mapping(section).get("events")
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


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
    name = _mapping(await _optional_json(client, f"/rooms/{room}/state/m.room.name")).get("name")
    topic = _mapping(await _optional_json(client, f"/rooms/{room}/state/m.room.topic")).get("topic")
    encryption = await _optional_json(client, f"/rooms/{room}/state/m.room.encryption")
    person = quote(client.user_id, safe="")
    direct = _mapping(await _optional_json(client, f"/user/{person}/account_data/m.direct"))
    members = [
        _line(user_id, _text(_mapping(member).get("display_name"))) for user_id, member in sorted(joined.items())
    ]
    return {
        "room_id": room_id,
        "name": _text(name),
        "topic": topic if isinstance(topic, str) else None,
        "encrypted": encryption is not None,
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
