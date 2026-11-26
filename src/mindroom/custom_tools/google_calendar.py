"""Google Calendar tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.errors import HttpError

from mindroom.custom_tools.google_service import (
    GoogleApiToolkit,
    filter_google_tools,
    matches_google_function,
    restrict_google_actions,
    visible_google_function_map,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_CALENDAR_FUNCTION_NAMES = ("google_calendar_read", "google_calendar_write")
_ReadAction = Literal[
    "list_events",
    "get_event",
    "fetch_all_events",
    "search_events",
    "get_event_attendees",
    "list_calendars",
    "check_availability",
    "find_available_slots",
]
_WriteAction = Literal[
    "create_event",
    "update_event",
    "delete_event",
    "quick_add_event",
    "move_event",
    "respond_to_event",
]
_ALL_READ_ACTIONS: tuple[str, ...] = (
    "list_events",
    "get_event",
    "fetch_all_events",
    "search_events",
    "get_event_attendees",
    "list_calendars",
    "check_availability",
    "find_available_slots",
)
_ALL_WRITE_ACTIONS: tuple[str, ...] = (
    "create_event",
    "update_event",
    "delete_event",
    "quick_add_event",
    "move_event",
    "respond_to_event",
)
_READ_ACTION_FIELDS: dict[str, str] = {
    "list_events": "limit start_date end_date page_token",
    "get_event": "event_id",
    "fetch_all_events": "limit start_date end_date",
    "search_events": "query limit start_date end_date page_token",
    "get_event_attendees": "event_id",
    "list_calendars": "page_token",
    "check_availability": "start_date end_date attendee_emails timezone",
    "find_available_slots": "start_date end_date duration_minutes",
}
_WRITE_ACTION_FIELDS: dict[str, str] = {
    "create_event": "title start_date end_date description location timezone attendees add_google_meet_link notify_attendees",
    "update_event": "event_id title start_date end_date description location timezone attendees notify_attendees",
    "delete_event": "event_id notify_attendees",
    "quick_add_event": "text",
    "move_event": "event_id destination_calendar_id notify_attendees",
    "respond_to_event": "event_id response",
}
_UNSET = object()
_VALID_RESPONSES = frozenset({"accepted", "declined", "tentative"})
_CALENDAR_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    },
)
_CALENDAR_EVENT_READ_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    },
)
_CALENDAR_LIST_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.calendarlist",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    },
)
_CALENDAR_FREEBUSY_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events.freebusy",
    },
)
_CALENDAR_EVENT_READ_ACTIONS = frozenset(
    {
        "list_events",
        "get_event",
        "fetch_all_events",
        "find_available_slots",
        "get_event_attendees",
        "search_events",
    },
)


def _validate_calendar_scopes(
    registered_names: Collection[str],
    *,
    read_actions: Collection[str],
    write_actions: Collection[str],
    scopes: Collection[str],
) -> None:
    """Ensure explicit scopes cover every enabled Calendar operation."""
    registered = set(registered_names)
    scope_set = set(scopes)
    if "google_calendar_write" in registered and write_actions and not (_CALENDAR_WRITE_SCOPES & scope_set):
        msg = (
            "A Google Calendar write scope (https://www.googleapis.com/auth/calendar or "
            "https://www.googleapis.com/auth/calendar.events) is required for write operations"
        )
        raise ValueError(msg)
    if "google_calendar_read" in registered:
        enabled_reads = set(read_actions)
        if enabled_reads & _CALENDAR_EVENT_READ_ACTIONS and not (_CALENDAR_EVENT_READ_SCOPES & scope_set):
            msg = "A Google Calendar event read scope is required for event read operations"
            raise ValueError(msg)
        if "list_calendars" in enabled_reads and not (_CALENDAR_LIST_SCOPES & scope_set):
            msg = "A Google Calendar list scope is required for list_calendars"
            raise ValueError(msg)
        if "check_availability" in enabled_reads and not (_CALENDAR_FREEBUSY_SCOPES & scope_set):
            msg = "A Google Calendar free/busy scope is required for check_availability"
            raise ValueError(msg)


def _to_local_date(value: str, tz: datetime.tzinfo) -> datetime.date:
    """Return the calendar day ``value`` falls on in ``tz``; a value without an offset is already local."""
    parsed = datetime.datetime.fromisoformat(value)
    return (parsed.astimezone(tz) if parsed.tzinfo else parsed).date()


def _parse_utc_iso(value: str, *, z_suffix: bool = False) -> str:
    """Parse an ISO datetime and normalize it to UTC (attaching UTC if naive)."""
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    elif z_suffix:
        parsed = parsed.astimezone(datetime.UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if z_suffix else parsed.isoformat()


def _parse_utc_iso_or_raw(value: str) -> str:
    """Parse an ISO datetime to UTC ISO format, falling back to the raw string if unparseable."""
    try:
        return _parse_utc_iso(value)
    except ValueError:
        return value


def _format_event_datetime(value: str) -> str:
    """Format an ISO datetime for Google Calendar event start/end fields."""
    parsed = datetime.datetime.fromisoformat(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") if parsed.tzinfo is None else parsed.isoformat()


def _working_hours_for_locale(locale: str) -> tuple[int, int]:
    """Return default (start_hour, end_hour) based on calendar locale."""
    if locale.startswith(("es", "it", "pt", "ja", "ko")):
        return 9, 18
    if locale.startswith(("de", "nl", "dk", "se", "no")):
        return 8, 17
    return 9, 17


def _busy_periods_from_events(events: list[dict[str, Any]]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    busy_periods: list[tuple[datetime.datetime, datetime.datetime]] = []
    for event in events:
        if event.get("transparency") == "transparent":
            continue
        start_info = event.get("start", {})
        end_info = event.get("end", {})
        if "dateTime" not in start_info or "dateTime" not in end_info:
            continue
        try:
            busy_start = datetime.datetime.fromisoformat(start_info["dateTime"])
            busy_end = datetime.datetime.fromisoformat(end_info["dateTime"])
            busy_periods.append((busy_start, busy_end))
        except (ValueError, KeyError):
            continue
    return busy_periods


def _free_slots_in_range(
    *,
    start_day: datetime.date,
    end_day: datetime.date,
    work_start: datetime.time,
    work_end: datetime.time,
    user_tz: datetime.tzinfo,
    duration_minutes: int,
    busy_periods: list[tuple[datetime.datetime, datetime.datetime]],
) -> list[dict[str, str]]:
    available_slots: list[dict[str, str]] = []
    slot_delta = datetime.timedelta(minutes=duration_minutes)
    step_delta = datetime.timedelta(minutes=30)
    day = start_day
    while day <= end_day:
        if day.weekday() < 5:
            # Step in UTC so a DST change inside the window cannot stretch or shrink a slot.
            current = datetime.datetime.combine(day, work_start, tzinfo=user_tz).astimezone(datetime.UTC)
            day_end = datetime.datetime.combine(day, work_end, tzinfo=user_tz).astimezone(datetime.UTC)
            while current < day_end:
                slot_end = current + slot_delta
                if slot_end <= day_end and all(
                    slot_end <= busy_start or current >= busy_end for busy_start, busy_end in busy_periods
                ):
                    available_slots.append(
                        {
                            "start": current.astimezone(user_tz).isoformat(),
                            "end": slot_end.astimezone(user_tz).isoformat(),
                        },
                    )
                current += step_delta
        day += datetime.timedelta(days=1)
    return available_slots


def _apply_event_updates(
    event: dict[str, Any],
    *,
    title: str | None,
    description: str | None,
    location: str | None,
    attendees: list[str] | None,
    formatted_start: str | None,
    formatted_end: str | None,
    timezone: str | None,
) -> None:
    if title is not None:
        event["summary"] = title
    if description is not None:
        event["description"] = description
    if location is not None:
        event["location"] = location
    if attendees is not None:
        event["attendees"] = [{"email": attendee} for attendee in attendees]
    if formatted_start is not None:
        event["start"]["dateTime"] = formatted_start
        if timezone:
            event["start"]["timeZone"] = timezone
    if formatted_end is not None:
        event["end"]["dateTime"] = formatted_end
        if timezone:
            event["end"]["timeZone"] = timezone


def _calendar_http_error(operation: str, exc: HttpError) -> str:
    logger.warning("google_calendar_request_failed", operation=operation, status=exc.resp.status)
    return json.dumps({"error": f"An error occurred: {exc}"})


class GoogleCalendarTools(GoogleApiToolkit):
    """Manage Google Calendar events and availability with MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_oauth_provider("google_calendar")
    _oauth_tool_name = "google_calendar"
    _google_api_name = "calendar"
    _google_api_version = "v3"

    default_scopes: ClassVar[list[str]] = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar",
    ]

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        calendar_id: str = "primary",
        allow_update: bool | None = True,
        scopes: Sequence[str] | None = None,
        max_results: int = 100,
        expand_recurring: bool = True,
        list_events: bool | None = True,
        get_event: bool | None = True,
        fetch_all_events: bool | None = True,
        find_available_slots: bool | None = True,
        list_calendars: bool | None = True,
        check_availability: bool | None = True,
        get_event_attendees: bool | None = True,
        search_events: bool | None = True,
        create_event: object = _UNSET,
        update_event: object = _UNSET,
        delete_event: object = _UNSET,
        quick_add_event: object = _UNSET,
        move_event: object = _UNSET,
        respond_to_event: object = _UNSET,
        include_tools: Sequence[str] | None = None,
        exclude_tools: Sequence[str] | None = None,
        instructions: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.calendar_id = calendar_id
        self.max_results = max_results
        self.expand_recurring = expand_recurring
        self.scopes = list(self.default_scopes) if scopes is None else list(scopes)
        self._custom_instructions = instructions

        read_toggles: tuple[tuple[bool | None, str], ...] = (
            (list_events, "list_events"),
            (get_event, "get_event"),
            (fetch_all_events, "fetch_all_events"),
            (search_events, "search_events"),
            (get_event_attendees, "get_event_attendees"),
            (list_calendars, "list_calendars"),
            (check_availability, "check_availability"),
            (find_available_slots, "find_available_slots"),
        )
        self._read_actions: tuple[str, ...] = tuple(action for enabled, action in read_toggles if enabled)

        writes_allowed = allow_update is True
        write_toggles: tuple[tuple[object, str], ...] = (
            (create_event, "create_event"),
            (update_event, "update_event"),
            (delete_event, "delete_event"),
            (quick_add_event, "quick_add_event"),
            (move_event, "move_event"),
            (respond_to_event, "respond_to_event"),
        )
        self._write_actions: tuple[str, ...] = tuple(
            action for flag, action in write_toggles if writes_allowed and (flag is _UNSET or bool(flag))
        )

        tools: list[Callable[..., str]] = []
        if self._read_actions:
            tools.append(self.google_calendar_read)
        if self._write_actions:
            tools.append(self.google_calendar_write)
        tools = filter_google_tools(tools, include_tools, exclude_tools)

        super().__init__(
            name="google_calendar",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )
        _validate_calendar_scopes(
            self.functions,
            read_actions=self._read_actions,
            write_actions=self._write_actions,
            scopes=self.scopes,
        )

    def _service_account_scopes(self) -> tuple[str, ...]:
        """Return Calendar service-account scopes, honoring explicit constructor scopes."""
        return tuple(self.scopes)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict calendar action enums and parameters to enabled actions."""
        if matches_google_function(function.name, "google_calendar_read") and len(self._read_actions) < len(
            _ALL_READ_ACTIONS,
        ):
            return restrict_google_actions(function, self._read_actions, _READ_ACTION_FIELDS)
        if matches_google_function(function.name, "google_calendar_write") and len(self._write_actions) < len(
            _ALL_WRITE_ACTIONS,
        ):
            return restrict_google_actions(function, self._write_actions, _WRITE_ACTION_FIELDS)
        return function

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]
        """Return instructions scoped to the currently registered calendar functions."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return Google Calendar guidance naming only offered functions."""
        if self._custom_instructions is not None:
            return self._custom_instructions
        visible = visible_google_function_map(function_names, _CALENDAR_FUNCTION_NAMES)
        if not visible:
            return ""
        lines = ["- All dates use ISO format: YYYY-MM-DDTHH:MM:SS"]
        if "google_calendar_write" in visible and (
            "create_event" in self._write_actions or "update_event" in self._write_actions
        ):
            lines.append(
                f"- When calling `{visible['google_calendar_write']}` to create or update events, "
                "specify an IANA `timezone` or include an offset (defaults to UTC)",
            )
        return "\n".join(lines)

    def _list_events(
        self,
        limit: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
        page_token: str | None = None,
    ) -> str:
        if start_date is None:
            resolved_start = datetime.datetime.now(datetime.UTC).isoformat()
        else:
            try:
                resolved_start = _parse_utc_iso(start_date, z_suffix=True)
            except ValueError:
                return json.dumps(
                    {"error": f"Invalid date format: {start_date}. Use ISO format (YYYY-MM-DDTHH:MM:SS)."},
                )

        params: dict[str, Any] = {
            "calendarId": self.calendar_id,
            "timeMin": resolved_start,
            "maxResults": min(limit, self.max_results),
            "singleEvents": self.expand_recurring,
        }
        if self.expand_recurring:
            params["orderBy"] = "startTime"
        if end_date:
            try:
                params["timeMax"] = _parse_utc_iso(end_date)
            except ValueError:
                return json.dumps({"error": f"Invalid end_date format: {end_date}. Use ISO format."})
        if page_token:
            params["pageToken"] = page_token

        try:
            service = self._google_api_service()
            events_result = service.events().list(**params).execute()
        except HttpError as exc:
            return _calendar_http_error("list_events", exc)

        events = events_result.get("items", [])
        result: dict[str, Any] = {"events": events}
        if events_result.get("nextPageToken"):
            result["nextPageToken"] = events_result["nextPageToken"]
        if not events and not page_token:
            result["message"] = "No upcoming events found."
        return json.dumps(result)

    def _fetch_all_events_list(
        self,
        max_results: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        service = self._google_api_service()
        params: dict[str, Any] = {
            "calendarId": self.calendar_id,
            "maxResults": min(max_results, 100),
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if start_date:
            params["timeMin"] = _parse_utc_iso_or_raw(start_date)
        if end_date:
            params["timeMax"] = _parse_utc_iso_or_raw(end_date)

        all_events: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            events_result = service.events().list(**params).execute()
            all_events.extend(events_result.get("items", []))
            page_token = events_result.get("nextPageToken")
            if not page_token:
                break
        return all_events

    def _fetch_all_events(
        self,
        max_results: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        try:
            all_events = self._fetch_all_events_list(
                max_results=max_results,
                start_date=start_date,
                end_date=end_date,
            )
        except HttpError as exc:
            return _calendar_http_error("fetch_all_events", exc)
        if not all_events:
            return json.dumps({"message": "No events found."})
        return json.dumps(all_events)

    def _load_working_hours(self) -> tuple[int, int, str, str]:
        """Return (start_hour, end_hour, timezone_name, locale) from Calendar settings, falling back to UTC 9-17."""
        try:
            service = self._google_api_service()
            settings = service.settings().list().execute().get("items", [])
        except HttpError as exc:
            logger.warning("google_calendar_settings_failed", status=exc.resp.status)
            return 9, 17, "UTC", "en"
        user_prefs = {setting["id"]: setting["value"] for setting in settings}
        tz = user_prefs.get("timezone", "UTC")
        locale = user_prefs.get("locale", "en")
        start_hour, end_hour = _working_hours_for_locale(locale)
        return start_hour, end_hour, tz, locale

    def _find_available_slots(
        self,
        start_date: str = "",
        end_date: str = "",
        duration_minutes: int = 30,
    ) -> str:
        try:
            working_hours_start, working_hours_end, tz, locale = self._load_working_hours()
            user_tz: datetime.tzinfo
            try:
                user_tz = ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("google_calendar_unknown_timezone", timezone=tz)
                user_tz, tz = datetime.UTC, "UTC"

            start_day = _to_local_date(start_date, user_tz)
            end_day = _to_local_date(end_date, user_tz)
            work_start = datetime.time(working_hours_start)
            work_end = datetime.time(working_hours_end)

            # A bare end date becomes 00:00 and timeMax is exclusive, so query the scanned window instead.
            events = self._fetch_all_events_list(
                start_date=datetime.datetime.combine(start_day, work_start, tzinfo=user_tz).isoformat(),
                end_date=datetime.datetime.combine(end_day, work_end, tzinfo=user_tz).isoformat(),
            )
            busy_periods = _busy_periods_from_events(events)
            available_slots = _free_slots_in_range(
                start_day=start_day,
                end_day=end_day,
                work_start=work_start,
                work_end=work_end,
                user_tz=user_tz,
                duration_minutes=duration_minutes,
                busy_periods=busy_periods,
            )
            return json.dumps(
                {
                    "available_slots": available_slots,
                    "duration_minutes": duration_minutes,
                    "working_hours": {"start": f"{working_hours_start:02d}:00", "end": f"{working_hours_end:02d}:00"},
                    "timezone": tz,
                    "locale": locale,
                    "events_analyzed": len(busy_periods),
                },
            )
        except HttpError as exc:
            return _calendar_http_error("find_available_slots", exc)
        except Exception as exc:
            logger.warning("google_calendar_find_slots_failed", error_type=type(exc).__name__)
            return json.dumps({"error": f"An error occurred: {exc}"})

    def _list_calendars(self, page_token: str | None = None) -> str:
        params: dict[str, Any] = {"maxResults": min(self.max_results, 250)}
        if page_token:
            params["pageToken"] = page_token
        try:
            service = self._google_api_service()
            calendar_list = service.calendarList().list(**params).execute()
        except HttpError as exc:
            return _calendar_http_error("list_calendars", exc)

        calendars = [
            {
                "id": calendar.get("id"),
                "name": calendar.get("summary", "Unnamed Calendar"),
                "description": calendar.get("description", ""),
                "primary": calendar.get("primary", False),
                "access_role": calendar.get("accessRole", "unknown"),
                "color": calendar.get("backgroundColor", "#ffffff"),
            }
            for calendar in calendar_list.get("items", [])
        ]
        result: dict[str, Any] = {
            "calendars": calendars,
            "current_default": self.calendar_id,
        }
        if calendar_list.get("nextPageToken"):
            result["nextPageToken"] = calendar_list["nextPageToken"]
        return json.dumps(result)

    def _get_event(self, event_id: str = "") -> str:
        try:
            service = self._google_api_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            return _calendar_http_error("get_event", exc)
        return json.dumps(event)

    def _check_availability(
        self,
        start_date: str = "",
        end_date: str = "",
        attendee_emails: list[str] | None = None,
        timezone: str | None = None,
    ) -> str:
        resolved = ["primary" if email in ("me", "primary") else email for email in (attendee_emails or ["me"])]
        try:
            start_iso = _parse_utc_iso(start_date)
            end_iso = _parse_utc_iso(end_date)
        except ValueError:
            return json.dumps({"error": "Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SS)."})

        body = {
            "timeMin": start_iso,
            "timeMax": end_iso,
            "timeZone": timezone or "UTC",
            "items": [{"id": cal_id} for cal_id in resolved],
        }
        try:
            service = self._google_api_service()
            result = service.freebusy().query(body=body).execute()
        except HttpError as exc:
            return _calendar_http_error("check_availability", exc)

        calendars = result.get("calendars", {})
        availability: dict[str, Any] = {}
        for cal_id in resolved:
            cal_data = calendars.get(cal_id, {})
            busy = cal_data.get("busy", [])
            errors = cal_data.get("errors", [])
            label = "me" if cal_id == "primary" else cal_id
            entry: dict[str, Any] = {
                "busy_periods": busy,
                "is_free": len(busy) == 0 and len(errors) == 0,
            }
            if errors:
                entry["errors"] = errors
            availability[label] = entry

        return json.dumps(
            {
                "availability": availability,
                "time_window": {"start": start_iso, "end": end_iso},
                "timezone": timezone or "UTC",
            },
        )

    def _search_events(
        self,
        query: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "calendarId": self.calendar_id,
            "q": query,
            "maxResults": min(max_results, self.max_results, 100),
            "singleEvents": self.expand_recurring,
        }
        if self.expand_recurring:
            params["orderBy"] = "startTime"
        if start_date:
            params["timeMin"] = _parse_utc_iso_or_raw(start_date)
        if end_date:
            params["timeMax"] = _parse_utc_iso_or_raw(end_date)
        if page_token:
            params["pageToken"] = page_token

        try:
            service = self._google_api_service()
            events_result = service.events().list(**params).execute()
        except HttpError as exc:
            return _calendar_http_error("search_events", exc)

        events = events_result.get("items", [])
        result: dict[str, Any] = {"events": events}
        if events_result.get("nextPageToken"):
            result["nextPageToken"] = events_result["nextPageToken"]
        if not events and not page_token:
            result["message"] = f"No events found matching '{query}'."
        return json.dumps(result)

    def _get_event_attendees(self, event_id: str = "") -> str:
        try:
            service = self._google_api_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            return _calendar_http_error("get_event_attendees", exc)

        attendee_list = [
            {
                "email": attendee.get("email"),
                "name": attendee.get("displayName", ""),
                "response_status": attendee.get("responseStatus", "needsAction"),
                "organizer": attendee.get("organizer", False),
                "optional": attendee.get("optional", False),
            }
            for attendee in event.get("attendees", [])
        ]
        return json.dumps(
            {
                "event_id": event_id,
                "event_summary": event.get("summary", ""),
                "attendees": attendee_list,
                "total": len(attendee_list),
            },
        )

    def _create_event(
        self,
        start_date: str = "",
        end_date: str = "",
        title: str | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str | None = None,
        attendees: list[str] | None = None,
        add_google_meet_link: bool = False,
        notify_attendees: bool | None = None,
    ) -> str:
        try:
            start_time = _format_event_datetime(start_date)
            end_time = _format_event_datetime(end_date)
        except ValueError:
            return json.dumps({"error": "Invalid datetime format. Use ISO format (YYYY-MM-DDTHH:MM:SS)."})

        tz = timezone or "UTC"
        event: dict[str, Any] = {
            "summary": title,
            "location": location,
            "description": description,
            "start": {"dateTime": start_time, "timeZone": tz},
            "end": {"dateTime": end_time, "timeZone": tz},
            "attendees": [{"email": attendee} for attendee in attendees] if attendees else [],
        }
        if add_google_meet_link:
            event["conferenceData"] = {
                "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}},
            }
        event = {key: value for key, value in event.items() if value is not None}
        send_updates = "all" if notify_attendees and attendees else "none"

        try:
            service = self._google_api_service()
            event_result = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event,
                    conferenceDataVersion=1 if add_google_meet_link else 0,
                    sendUpdates=send_updates,
                )
                .execute()
            )
        except HttpError as exc:
            return _calendar_http_error("create_event", exc)
        return json.dumps(event_result)

    def _update_event(
        self,
        event_id: str = "",
        title: str | None = None,
        description: str | None = None,
        location: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        timezone: str | None = None,
        attendees: list[str] | None = None,
        notify_attendees: bool | None = None,
    ) -> str:
        formatted_start: str | None = None
        formatted_end: str | None = None
        if start_date:
            try:
                formatted_start = _format_event_datetime(start_date)
            except ValueError:
                return json.dumps({"error": f"Invalid start datetime format: {start_date}. Use ISO format."})
        if end_date:
            try:
                formatted_end = _format_event_datetime(end_date)
            except ValueError:
                return json.dumps({"error": f"Invalid end datetime format: {end_date}. Use ISO format."})

        try:
            service = self._google_api_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
            _apply_event_updates(
                event,
                title=title,
                description=description,
                location=location,
                attendees=attendees,
                formatted_start=formatted_start,
                formatted_end=formatted_end,
                timezone=timezone,
            )
            send_updates = "all" if notify_attendees and attendees else "none"
            updated_event = (
                service.events()
                .update(calendarId=self.calendar_id, eventId=event_id, body=event, sendUpdates=send_updates)
                .execute()
            )
        except HttpError as exc:
            return _calendar_http_error("update_event", exc)
        return json.dumps(updated_event)

    def _delete_event(self, event_id: str = "", notify_attendees: bool | None = None) -> str:
        should_notify = True if notify_attendees is None else notify_attendees
        send_updates = "all" if should_notify else "none"
        try:
            service = self._google_api_service()
            service.events().delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates=send_updates).execute()
        except HttpError as exc:
            return _calendar_http_error("delete_event", exc)
        return json.dumps({"success": True, "message": f"Event {event_id} deleted successfully."})

    def _quick_add_event(self, text: str = "") -> str:
        try:
            service = self._google_api_service()
            event = service.events().quickAdd(calendarId=self.calendar_id, text=text).execute()
        except HttpError as exc:
            return _calendar_http_error("quick_add_event", exc)
        return json.dumps(event)

    def _move_event(
        self,
        event_id: str = "",
        destination_calendar_id: str = "",
        notify_attendees: bool | None = None,
    ) -> str:
        send_updates = "all" if notify_attendees else "none"
        try:
            service = self._google_api_service()
            moved_event = (
                service.events()
                .move(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    destination=destination_calendar_id,
                    sendUpdates=send_updates,
                )
                .execute()
            )
        except HttpError as exc:
            return _calendar_http_error("move_event", exc)
        return json.dumps(moved_event)

    def _respond_to_event(self, event_id: str = "", response: str = "") -> str:
        if response not in _VALID_RESPONSES:
            valid = ", ".join(sorted(_VALID_RESPONSES))
            return json.dumps({"error": f"Invalid response '{response}'. Must be one of: {valid}"})

        try:
            service = self._google_api_service()
            if not self._user_email:
                cal = service.calendarList().get(calendarId="primary").execute()
                self._user_email = cal.get("id")

            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
            attendees = event.get("attendees", [])
            for attendee in attendees:
                if attendee.get("email") == self._user_email:
                    attendee["responseStatus"] = response
                    break
            else:
                attendees.append({"email": self._user_email, "responseStatus": response})

            updated_event = (
                service.events()
                .patch(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    body={"attendees": attendees},
                    sendUpdates="all",
                )
                .execute()
            )
        except HttpError as exc:
            return _calendar_http_error("respond_to_event", exc)
        return json.dumps(updated_event)

    def google_calendar_read(  # noqa: PLR0911
        self,
        action: _ReadAction,
        event_id: str | None = None,
        query: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        page_token: str | None = None,
        duration_minutes: int = 30,
        attendee_emails: list[str] | None = None,
        timezone: str | None = None,
    ) -> str:
        """Read Google Calendar events, calendars, attendees, and availability.

        - `list_events`: list upcoming events (`limit`, optional `start_date`, `end_date`, `page_token`)
        - `get_event`: get full details for `event_id`
        - `fetch_all_events`: paginate all events in range (`limit` per page, optional `start_date`, `end_date`)
        - `search_events`: full-text search with `query` (`limit`, optional `start_date`, `end_date`, `page_token`)
        - `get_event_attendees`: list attendees and RSVP statuses for `event_id`
        - `list_calendars`: list accessible calendars (optional `page_token`)
        - `check_availability`: check free/busy between `start_date` and `end_date` (optional `attendee_emails`, `timezone`)
        - `find_available_slots`: find free working-hours slots of `duration_minutes` between `start_date` and `end_date`
        """
        if action not in self._read_actions:
            return json.dumps({"error": f"Google Calendar action {action!r} is disabled."})
        if action == "list_events":
            return self._list_events(limit=limit, start_date=start_date, end_date=end_date, page_token=page_token)
        if action == "get_event":
            return self._get_event(event_id=event_id or "")
        if action == "fetch_all_events":
            return self._fetch_all_events(max_results=limit, start_date=start_date, end_date=end_date)
        if action == "search_events":
            return self._search_events(
                query=query or "",
                start_date=start_date,
                end_date=end_date,
                max_results=limit,
                page_token=page_token,
            )
        if action == "get_event_attendees":
            return self._get_event_attendees(event_id=event_id or "")
        if action == "list_calendars":
            return self._list_calendars(page_token=page_token)
        if action == "check_availability":
            return self._check_availability(
                start_date=start_date or "",
                end_date=end_date or "",
                attendee_emails=attendee_emails,
                timezone=timezone,
            )
        if action == "find_available_slots":
            return self._find_available_slots(
                start_date=start_date or "",
                end_date=end_date or "",
                duration_minutes=duration_minutes,
            )
        return json.dumps({"error": f"Unsupported Google Calendar read action {action!r}."})

    def google_calendar_write(  # noqa: PLR0911
        self,
        action: _WriteAction,
        event_id: str | None = None,
        title: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str | None = None,
        attendees: list[str] | None = None,
        add_google_meet_link: bool = False,
        notify_attendees: bool | None = None,
        text: str | None = None,
        destination_calendar_id: str | None = None,
        response: Literal["accepted", "declined", "tentative"] | None = None,
    ) -> str:
        """Create, update, move, RSVP to, or delete Google Calendar events.

        - `create_event`: create event (`start_date`, `end_date`, optional `title`, `description`, `location`, `timezone` default UTC, `attendees`, `add_google_meet_link`, `notify_attendees` default False)
        - `update_event`: update `event_id` (`title`, `start_date`, `end_date`, `description`, `location`, `timezone`, `attendees`, `notify_attendees` default False)
        - `delete_event`: delete `event_id` (optional `notify_attendees` default True)
        - `quick_add_event`: create event from natural-language `text`
        - `move_event`: move `event_id` to `destination_calendar_id` (optional `notify_attendees` default False)
        - `respond_to_event`: set RSVP on `event_id` (`response`: `accepted`, `declined`, or `tentative`)
        """
        if action not in self._write_actions:
            return json.dumps({"error": f"Google Calendar action {action!r} is disabled."})
        if action == "create_event":
            return self._create_event(
                start_date=start_date or "",
                end_date=end_date or "",
                title=title,
                description=description,
                location=location,
                timezone=timezone,
                attendees=attendees,
                add_google_meet_link=add_google_meet_link,
                notify_attendees=notify_attendees,
            )
        if action == "update_event":
            return self._update_event(
                event_id=event_id or "",
                title=title,
                description=description,
                location=location,
                start_date=start_date,
                end_date=end_date,
                timezone=timezone,
                attendees=attendees,
                notify_attendees=notify_attendees,
            )
        if action == "delete_event":
            return self._delete_event(event_id=event_id or "", notify_attendees=notify_attendees)
        if action == "quick_add_event":
            return self._quick_add_event(text=text or "")
        if action == "move_event":
            return self._move_event(
                event_id=event_id or "",
                destination_calendar_id=destination_calendar_id or "",
                notify_attendees=notify_attendees,
            )
        if action == "respond_to_event":
            return self._respond_to_event(event_id=event_id or "", response=response or "")
        return json.dumps({"error": f"Unsupported Google Calendar write action {action!r}."})
