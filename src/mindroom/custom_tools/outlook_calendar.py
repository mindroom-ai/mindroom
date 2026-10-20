"""Outlook calendar tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    GraphSession,
    MicrosoftGraphError,
    graph_error_payload,
    graph_session,
    graph_tool_result,
    path_segment,
)
from mindroom.oauth.microsoft import outlook_calendar_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_WRITE_FUNCTIONS = frozenset(
    {
        "outlook_calendar_create_event",
        "outlook_calendar_update_event",
        "outlook_calendar_delete_event",
        "outlook_calendar_respond_to_event",
    },
)
_MAX_EVENTS = 50
_MAX_CALENDARS = 100
_MAX_WINDOW = timedelta(days=366)
# Event descriptions come back into the conversation, so one read stays small.
_MAX_BODY_CHARACTERS = 20_000
# Exchange allows 500; an agent inviting more than this is almost certainly a mistake.
_MAX_ATTENDEES = 100
_TOOL_NAME = "outlook_calendar"
_UTC = 'outlook.timezone="UTC"'
_UTC_TEXT = f'{_UTC}, outlook.body-content-type="text"'
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CALENDAR_SELECT = "id,name,owner,canEdit,isDefaultCalendar"
_EVENT_SELECT = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,isOnlineMeeting,onlineMeeting,showAs,webLink,"
    "bodyPreview,isOrganizer,responseStatus,isCancelled,type"
)
# Windows zone names ("W. Europe Standard Time", "UTC-11") and IANA names ("America/Port-au-Prince").
_TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9 ._+/-]{0,63}")
_EMAIL = re.compile(r'[^@\s<>,;"]+@[^@\s<>,;"]+\.[^@\s<>,;"]+')
_RESPONSE_ACTIONS = {"accept": "accept", "tentative": "tentativelyAccept", "decline": "decline"}


@dataclass(frozen=True)
class _GraphRequest:
    method: str
    path: str
    params: dict[str, str] | None = None
    body: dict[str, Any] | None = None
    prefer: str = _UTC

    def send(self, session: GraphSession) -> dict[str, Any]:
        return session.json(
            self.method,
            self.path,
            params=self.params,
            json=self.body,
            headers={"Prefer": self.prefer},
        )


def _invalid(message: str) -> MicrosoftGraphError:
    return MicrosoftGraphError(code="invalid_argument", message=message)


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_EVENTS:
        msg = f"limit must be an integer from 1 to {_MAX_EVENTS}."
        raise _invalid(msg)
    return value


def _parse_time(value: object, field_name: str) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            pass
    msg = f"{field_name} must be an ISO 8601 date and time, such as 2026-10-21T09:00:00."
    raise _invalid(msg)


def _utc(value: object, field_name: str) -> datetime:
    """Return a calendar-view bound in UTC; a value without an offset is UTC, as Graph reads it."""
    moment = _parse_time(value, field_name)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _window(start: object, end: object) -> tuple[str, str]:
    begins, ends = _utc(start, "start"), _utc(end, "end")
    if ends <= begins:
        msg = "end must be after start."
        raise _invalid(msg)
    if ends - begins > _MAX_WINDOW:
        msg = f"The window may span at most {_MAX_WINDOW.days} days."
        raise _invalid(msg)
    return begins.strftime(_UTC_FORMAT), ends.strftime(_UTC_FORMAT)


def _local(value: object, field_name: str) -> datetime:
    """Return an event time in its own time zone; Graph takes the zone separately, so an offset is refused."""
    moment = _parse_time(value, field_name)
    if moment.tzinfo is not None:
        msg = (
            f"{field_name} must be a local date and time without an offset, such as 2026-10-21T14:00:00; "
            "put the zone in time_zone."
        )
        raise _invalid(msg)
    return moment


def _time_zone(value: object) -> str:
    if not isinstance(value, str) or not _TIME_ZONE.fullmatch(value) or value != value.strip():
        msg = 'time_zone must be a Windows or IANA time zone name, such as "Pacific Standard Time" or "Europe/Berlin".'
        raise _invalid(msg)
    return value


def _zoned(moment: datetime, zone: str) -> dict[str, str]:
    return {"dateTime": moment.isoformat(timespec="seconds"), "timeZone": zone}


def _times(start: object, end: object, zone: str) -> dict[str, dict[str, str]]:
    """Return the start and end fields to send, checking their order when both are given."""
    begins = None if start is None else _local(start, "start")
    ends = None if end is None else _local(end, "end")
    if begins is not None and ends is not None and ends <= begins:
        msg = "end must be after start."
        raise _invalid(msg)
    fields = {"start": begins, "end": ends}
    return {name: _zoned(moment, zone) for name, moment in fields.items() if moment is not None}


def _subject(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = "subject must not be empty."
        raise _invalid(msg)
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} must be text."
        raise _invalid(msg)
    return value


def _attendees(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    addresses = [address.strip() for address in value if isinstance(address, str)] if isinstance(value, list) else []
    if (
        not isinstance(value, list)
        or len(addresses) != len(value)
        or any(len(address) > 254 or not _EMAIL.fullmatch(address) for address in addresses)
    ):
        msg = "attendees must be a list of email addresses, such as alex@contoso.com."
        raise _invalid(msg)
    if len(addresses) > _MAX_ATTENDEES:
        msg = f"attendees may list at most {_MAX_ATTENDEES} addresses."
        raise _invalid(msg)
    return [{"emailAddress": {"address": address}, "type": "required"} for address in addresses]


def _calendar_path(calendar: object) -> str:
    return "/me/calendar" if calendar is None else f"/me/calendars/{path_segment(calendar, 'calendar')}"


def _event_path(event_id: object) -> str:
    return f"/me/events/{path_segment(event_id, 'event_id')}"


def _dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _person(recipient: object) -> dict[str, Any]:
    address = _dict(_dict(recipient).get("emailAddress"))
    return {"name": address.get("name"), "email": address.get("address")}


def _time(value: object) -> str | None:
    """Return a Graph dateTimeTimeZone as ISO 8601; with the UTC preference that is always `...Z`."""
    moment = _dict(value).get("dateTime")
    if not isinstance(moment, str):
        return None
    zone = _dict(value).get("timeZone")
    moment = moment.split(".", 1)[0]
    return f"{moment}Z" if zone in {None, "UTC"} else f"{moment} ({zone})"


def _event(event: Mapping[str, Any]) -> dict[str, Any]:
    attendees = [attendee for attendee in event.get("attendees") or [] if isinstance(attendee, dict)]
    return {
        "id": event.get("id"),
        "subject": event.get("subject"),
        "start": _time(event.get("start")),
        "end": _time(event.get("end")),
        "is_all_day": event.get("isAllDay"),
        "location": _dict(event.get("location")).get("displayName") or None,
        "organizer": _person(event.get("organizer")),
        "attendees": [
            {**_person(attendee), "response": _dict(attendee.get("status")).get("response")}
            for attendee in attendees[:_MAX_ATTENDEES]
        ],
        "attendee_count": len(attendees),
        "is_online_meeting": event.get("isOnlineMeeting"),
        "join_url": _dict(event.get("onlineMeeting")).get("joinUrl"),
        "show_as": event.get("showAs"),
        "web_link": event.get("webLink"),
        "body_preview": event.get("bodyPreview"),
        "is_organizer": event.get("isOrganizer"),
        "my_response": _dict(event.get("responseStatus")).get("response"),
        "is_cancelled": event.get("isCancelled"),
        "event_type": event.get("type"),
    }


def _event_with_body(event: Mapping[str, Any]) -> dict[str, Any]:
    content = _dict(event.get("body")).get("content")
    text = content if isinstance(content, str) else ""
    return {**_event(event), "body": text[:_MAX_BODY_CHARACTERS], "truncated": len(text) > _MAX_BODY_CHARACTERS}


def _calendar(calendar: Mapping[str, Any]) -> dict[str, Any]:
    owner = _dict(calendar.get("owner"))
    return {
        "id": calendar.get("id"),
        "name": calendar.get("name"),
        "owner": {"name": owner.get("name"), "email": owner.get("address")},
        "can_edit": calendar.get("canEdit"),
        "is_default": calendar.get("isDefaultCalendar"),
    }


def _records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [record for record in payload.get("value") or [] if isinstance(record, dict)]


def _refuse_online_meeting_body(event_id: str) -> Callable[[GraphSession], None]:
    """Graph keeps an online meeting's join details in its body, so replacing the body would remove them."""

    def check(session: GraphSession) -> None:
        event = session.json("GET", _event_path(event_id), params={"$select": "isOnlineMeeting"})
        if event.get("isOnlineMeeting") is True:
            msg = (
                "Replacing an online meeting's description would remove its join link; "
                "change the other fields, or edit the description in Outlook."
            )
            raise MicrosoftGraphError(code="online_meeting_body", message=msg)

    return check


class OutlookCalendarTools(Toolkit):
    """Read and change the requester's Outlook calendar events."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        write: bool = True,
    ) -> None:
        if credentials_manager is None:
            msg = "OutlookCalendarTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = outlook_calendar_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        functions: dict[str, Callable[..., str]] = {
            "outlook_calendar_list_calendars": self.outlook_calendar_list_calendars,
            "outlook_calendar_list_events": self.outlook_calendar_list_events,
            "outlook_calendar_get_event": self.outlook_calendar_get_event,
            "outlook_calendar_create_event": self.outlook_calendar_create_event,
            "outlook_calendar_update_event": self.outlook_calendar_update_event,
            "outlook_calendar_delete_event": self.outlook_calendar_delete_event,
            "outlook_calendar_respond_to_event": self.outlook_calendar_respond_to_event,
        }
        super().__init__(
            name=_TOOL_NAME,
            tools=[function for name, function in functions.items() if write or name not in _WRITE_FUNCTIONS],
        )

    def _send(
        self,
        build: Callable[[], _GraphRequest],
        render: Callable[[dict[str, Any]], Mapping[str, object]],
        *,
        check: Callable[[GraphSession], None] | None = None,
    ) -> str:
        """Refuse malformed arguments before any request, then send the one request and render its answer."""
        try:
            request = build()
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)

        def run() -> Mapping[str, object]:
            with graph_session(
                self._provider,
                self._runtime_paths,
                self._credentials_manager,
                self._worker_target,
                self._config,
            ) as session:
                if check is not None:
                    check(session)
                return render(request.send(session))

        return graph_tool_result(_TOOL_NAME, run)

    def outlook_calendar_list_calendars(self) -> str:
        """List the requester's Outlook calendars.

        Pass a calendar's id as `calendar` to outlook_calendar_list_events or outlook_calendar_create_event;
        can_edit tells whether events can be created there.
        """
        return self._send(
            lambda: _GraphRequest(
                "GET",
                "/me/calendars",
                params={"$select": _CALENDAR_SELECT, "$top": str(_MAX_CALENDARS)},
            ),
            lambda payload: {
                "calendars": [_calendar(calendar) for calendar in _records(payload)],
                "more_available": "@odata.nextLink" in payload,
            },
        )

    def outlook_calendar_list_events(
        self,
        start: str,
        end: str,
        calendar: str | None = None,
        limit: int = 50,
    ) -> str:
        """List the events between two moments in the requester's default Outlook calendar or another calendar.

        Times in the result are UTC (ending in Z); convert them to the requester's time zone before showing them.
        Events come in start order, and each occurrence of a recurring meeting is its own entry with its own id.
        more_available means more events fall in the window than were returned; narrow the window to see them.

        Args:
            start: Window start in ISO 8601, such as 2026-10-21T00:00:00+02:00; without an offset it is UTC.
            end: Window end in ISO 8601, after start and at most 366 days later.
            calendar: A calendar id from outlook_calendar_list_calendars; omit for the default calendar.
            limit: Maximum number of events to return, from 1 to 50.

        """

        def build() -> _GraphRequest:
            window_start, window_end = _window(start, end)
            return _GraphRequest(
                "GET",
                f"{_calendar_path(calendar)}/calendarView",
                params={
                    "startDateTime": window_start,
                    "endDateTime": window_end,
                    "$select": _EVENT_SELECT,
                    "$orderby": "start/dateTime",
                    "$top": str(_limit(limit)),
                },
            )

        return self._send(
            build,
            lambda payload: {
                "events": [_event(event) for event in _records(payload)],
                "more_available": "@odata.nextLink" in payload,
            },
        )

    def outlook_calendar_get_event(self, event_id: str) -> str:
        """Get one event with its full description as plain text, up to 20,000 characters.

        Times in the result are UTC (ending in Z). truncated is true when the description was cut.

        Args:
            event_id: An event id from outlook_calendar_list_events.

        """
        return self._send(
            lambda: _GraphRequest(
                "GET",
                _event_path(event_id),
                params={"$select": f"{_EVENT_SELECT},body"},
                prefer=_UTC_TEXT,
            ),
            lambda payload: {"event": _event_with_body(payload)},
        )

    def outlook_calendar_create_event(
        self,
        subject: str,
        start: str,
        end: str,
        calendar: str | None = None,
        time_zone: str = "UTC",
        attendees: list[str] | None = None,
        location: str | None = None,
        body: str | None = None,
        is_online_meeting: bool = False,
    ) -> str:
        """Create an event in the requester's default Outlook calendar or another calendar they can edit.

        Before calling, tell the requester the subject, the start and end in their time zone, the calendar, and
        every attendee, because an approval prompt may show only IDs. Outlook emails an invitation to every attendee
        as soon as the event exists.

        Args:
            subject: The event title.
            start: Local start date and time without an offset, such as 2026-10-21T14:00:00, in time_zone.
            end: Local end date and time, after start, in time_zone.
            calendar: A calendar id from outlook_calendar_list_calendars; omit for the default calendar.
            time_zone: The zone of start and end: a Windows name such as "W. Europe Standard Time" or
                "Pacific Standard Time", an IANA name Outlook accepts such as "Europe/Berlin" or "America/New_York",
                or "UTC".
            attendees: Up to 100 email addresses to invite as required attendees.
            location: The location shown on the event.
            body: A plain-text description.
            is_online_meeting: Add an online meeting, such as Microsoft Teams, with the calendar's default provider.

        """

        def build() -> _GraphRequest:
            if start is None or end is None:
                msg = "start and end are required."
                raise _invalid(msg)
            payload: dict[str, Any] = {"subject": _subject(subject), **_times(start, end, _time_zone(time_zone))}
            if invitees := _attendees(attendees):
                payload["attendees"] = invitees
            if location is not None:
                payload["location"] = {"displayName": _text(location, "location")}
            if body is not None:
                payload["body"] = {"contentType": "text", "content": _text(body, "body")}
            if not isinstance(is_online_meeting, bool):
                msg = "is_online_meeting must be true or false."
                raise _invalid(msg)
            if is_online_meeting:
                payload["isOnlineMeeting"] = True
            return _GraphRequest("POST", f"{_calendar_path(calendar)}/events", body=payload)

        return self._send(build, lambda payload: {"event": _event(payload)})

    def outlook_calendar_update_event(
        self,
        event_id: str,
        subject: str | None = None,
        start: str | None = None,
        end: str | None = None,
        time_zone: str = "UTC",
        location: str | None = None,
        body: str | None = None,
    ) -> str:
        """Change the subject, time, location, or description of one event; fields left out keep their values.

        Before calling, tell the requester the event's subject and current time and what will change, because an
        approval prompt may show only IDs. When the requester organizes a meeting with attendees, Outlook sends
        every attendee the update. An occurrence id from outlook_calendar_list_events changes only that occurrence.
        An online meeting's description cannot be replaced, because that would remove its join link.

        Args:
            event_id: An event id from outlook_calendar_list_events.
            subject: The new title.
            start: The new local start date and time without an offset, in time_zone.
            end: The new local end date and time without an offset, in time_zone.
            time_zone: The zone of start and end, as for outlook_calendar_create_event.
            location: The new location; an empty string clears it.
            body: The new plain-text description, replacing the old one.

        """

        def build() -> _GraphRequest:
            changes: dict[str, Any] = {} if subject is None else {"subject": _subject(subject)}
            changes |= _times(start, end, _time_zone(time_zone))
            if location is not None:
                changes["location"] = {"displayName": _text(location, "location")}
            if body is not None:
                changes["body"] = {"contentType": "text", "content": _text(body, "body")}
            if not changes:
                msg = "Give at least one of subject, start, end, location, or body to change."
                raise _invalid(msg)
            return _GraphRequest("PATCH", _event_path(event_id), body=changes)

        return self._send(
            build,
            lambda payload: {"event": _event(payload)},
            check=None if body is None else _refuse_online_meeting_body(event_id),
        )

    def outlook_calendar_delete_event(self, event_id: str) -> str:
        """Delete one event from the requester's calendar.

        Before calling, tell the requester the event's subject and time, and whether attendees will be told, because
        an approval prompt may show only IDs. When the requester organizes the meeting (is_organizer), Outlook sends
        every attendee a cancellation. When the requester is only an attendee, the event leaves their calendar
        without telling the organizer; use outlook_calendar_respond_to_event with decline to tell them instead.
        An occurrence id removes only that occurrence of a recurring meeting.

        Args:
            event_id: An event id from outlook_calendar_list_events.

        """
        return self._send(
            lambda: _GraphRequest("DELETE", _event_path(event_id)),
            lambda _payload: {"deleted": True, "event_id": event_id},
        )

    def outlook_calendar_respond_to_event(
        self,
        event_id: str,
        response: Literal["accept", "tentative", "decline"],
        comment: str | None = None,
        send_response: bool = True,
    ) -> str:
        """Accept, tentatively accept, or decline a meeting the requester was invited to.

        Before calling, tell the requester the meeting's subject, time, and organizer, the response, and any comment,
        because an approval prompt may show only IDs. With send_response, the organizer receives the response and
        comment by email.

        Args:
            event_id: An event id from outlook_calendar_list_events.
            response: accept, tentative, or decline.
            comment: A note to the organizer, sent with the response.
            send_response: Whether to email the response to the organizer; a comment needs it.

        """

        def build() -> _GraphRequest:
            action = _RESPONSE_ACTIONS.get(response) if isinstance(response, str) else None
            if action is None:
                msg = "response must be accept, tentative, or decline."
                raise _invalid(msg)
            if not isinstance(send_response, bool):
                msg = "send_response must be true or false."
                raise _invalid(msg)
            payload: dict[str, Any] = {"sendResponse": send_response}
            if comment is not None:
                if not send_response:
                    msg = "A comment reaches the organizer only with send_response true; drop it or send."
                    raise _invalid(msg)
                payload["comment"] = _text(comment, "comment")
            return _GraphRequest("POST", f"{_event_path(event_id)}/{action}", body=payload)

        return self._send(
            build,
            lambda _payload: {"event_id": event_id, "response": response, "response_sent": send_response},
        )
