"""Google Calendar tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import datetime
import json
import uuid
from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, visible_google_function_map
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_CALENDAR_FUNCTION_NAMES = (
    "list_events",
    "get_event",
    "fetch_all_events",
    "find_available_slots",
    "list_calendars",
    "check_availability",
    "get_event_attendees",
    "search_events",
    "create_event",
    "update_event",
    "delete_event",
    "quick_add_event",
    "move_event",
    "respond_to_event",
)
_VALID_RESPONSES = frozenset({"accepted", "declined", "tentative"})
_CALENDAR_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    },
)
_CALENDAR_READ_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.freebusy",
    },
)
_CALENDAR_WRITE_TOOLS = frozenset(
    {
        "create_event",
        "update_event",
        "delete_event",
        "quick_add_event",
        "move_event",
        "respond_to_event",
    },
)
_CALENDAR_READ_TOOLS = frozenset(
    {
        "list_events",
        "get_event",
        "fetch_all_events",
        "find_available_slots",
        "list_calendars",
        "check_availability",
        "get_event_attendees",
        "search_events",
    },
)


def _validate_calendar_scopes(registered_names: Collection[str], scopes: Collection[str]) -> None:
    """Ensure explicit scopes cover every registered Calendar operation."""
    registered = set(registered_names)
    scope_set = set(scopes)
    if registered & _CALENDAR_WRITE_TOOLS and not (_CALENDAR_WRITE_SCOPES & scope_set):
        msg = (
            "A Google Calendar write scope (https://www.googleapis.com/auth/calendar or "
            "https://www.googleapis.com/auth/calendar.events) is required for write operations"
        )
        raise ValueError(msg)
    if registered & _CALENDAR_READ_TOOLS and not (_CALENDAR_READ_SCOPES & scope_set):
        msg = "A Google Calendar read scope is required for read operations"
        raise ValueError(msg)


def _calendar_tips(visible: dict[str, str]) -> list[str]:
    tips: list[str] = []
    if "get_event" in visible and "update_event" in visible:
        tips.append(f"- Use {visible['get_event']} to fetch full details before updating an event")
    if "check_availability" in visible:
        tips.append(f"- Use {visible['check_availability']} to check multiple people's schedules at once")
    if "quick_add_event" in visible:
        tips.append(f"- Use {visible['quick_add_event']} for simple events from natural language descriptions")
    if "search_events" in visible:
        tips.append(f"- Use {visible['search_events']} for full-text search across event fields")
    if "list_events" in visible:
        id_consumers = [visible[name] for name in ("get_event", "update_event", "delete_event") if name in visible]
        if id_consumers:
            tips.append(f"- Event IDs from {visible['list_events']} can be used with {', '.join(id_consumers)}")
    return tips


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
        scopes: Sequence[str] | None = None,
        calendar_id: str = "primary",
        max_results: int = 20,
        expand_recurring: bool = True,
        allow_update: bool = True,
        list_events: bool = True,
        get_event: bool = True,
        fetch_all_events: bool = True,
        find_available_slots: bool = True,
        list_calendars: bool = True,
        check_availability: bool = True,
        get_event_attendees: bool = True,
        search_events: bool = True,
        create_event: bool | None = None,
        update_event: bool | None = None,
        delete_event: bool | None = None,
        quick_add_event: bool | None = None,
        move_event: bool | None = None,
        respond_to_event: bool | None = None,
        instructions: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.calendar_id = calendar_id
        self.max_results = max_results
        self.expand_recurring = expand_recurring
        self.scopes = list(self.default_scopes) if scopes is None else list(scopes)
        self._custom_instructions = instructions

        writes_allowed = allow_update is True
        read_toggles: tuple[tuple[bool, Callable[..., str]], ...] = (
            (list_events, self.list_events),
            (get_event, self.get_event),
            (fetch_all_events, self.fetch_all_events),
            (find_available_slots, self.find_available_slots),
            (list_calendars, self.list_calendars),
            (check_availability, self.check_availability),
            (get_event_attendees, self.get_event_attendees),
            (search_events, self.search_events),
        )
        write_toggles: tuple[tuple[bool | None, Callable[..., str]], ...] = (
            (create_event, self.create_event),
            (update_event, self.update_event),
            (delete_event, self.delete_event),
            (quick_add_event, self.quick_add_event),
            (move_event, self.move_event),
            (respond_to_event, self.respond_to_event),
        )
        tools: list[Callable[..., str]] = [method for enabled, method in read_toggles if enabled]
        tools.extend(method for flag, method in write_toggles if writes_allowed and (flag is None or flag))

        super().__init__(
            name="google_calendar",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )
        _validate_calendar_scopes(self.functions, self.scopes)

    def _service_account_scopes(self) -> tuple[str, ...]:
        """Return Calendar service-account scopes, honoring explicit constructor scopes."""
        return tuple(self.scopes)

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]
        """Return instructions scoped to the currently registered calendar functions."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return Google Calendar guidance naming only the functions offered in ``function_names``."""
        if self._custom_instructions is not None:
            return self._custom_instructions
        visible = visible_google_function_map(function_names, _CALENDAR_FUNCTION_NAMES)
        if not visible:
            return ""

        date_lines = ["- All dates use ISO format: YYYY-MM-DDTHH:MM:SS"]
        if "create_event" in visible or "update_event" in visible:
            date_lines.append("- Always specify timezone when creating events")

        sections = [
            "You have access to Google Calendar tools for managing events and scheduling.",
            "## Date/Time Formats\n" + "\n".join(date_lines),
        ]
        if tips := _calendar_tips(visible):
            sections.append("## Tips\n" + "\n".join(tips))
        return "\n\n".join(sections)

    def list_events(
        self,
        limit: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
        page_token: str | None = None,
    ) -> str:
        """List upcoming events from the user's Google Calendar.

        Args:
            limit: Number of events to return (default: 10).
            start_date: Start date in ISO format (YYYY-MM-DDTHH:MM:SS). Defaults to now.
            end_date: End date in ISO format. Only events starting before this are returned.
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string containing events and nextPageToken if more results exist.

        """
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

    def create_event(
        self,
        start_date: str = "",
        end_date: str = "",
        title: str | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str | None = "UTC",
        attendees: list[str] | None = None,
        add_google_meet_link: bool | None = False,
        notify_attendees: bool | None = False,
    ) -> str:
        """Create a new event in the Google Calendar.

        Args:
            start_date: Start date and time in ISO format (YYYY-MM-DDTHH:MM:SS).
            end_date: End date and time in ISO format (YYYY-MM-DDTHH:MM:SS).
            title: Title/summary of the event.
            description: Detailed description of the event.
            location: Location of the event.
            timezone: Timezone for the event (default: UTC).
            attendees: List of attendee email addresses.
            add_google_meet_link: Whether to add a Google Meet video link.
            notify_attendees: Whether to send email notifications to attendees.

        Returns:
            JSON string containing the created event or error message.

        """
        try:
            start_time = _format_event_datetime(start_date)
            end_time = _format_event_datetime(end_date)
        except ValueError:
            return json.dumps({"error": "Invalid datetime format. Use ISO format (YYYY-MM-DDTHH:MM:SS)."})

        event: dict[str, Any] = {
            "summary": title,
            "location": location,
            "description": description,
            "start": {"dateTime": start_time, "timeZone": timezone},
            "end": {"dateTime": end_time, "timeZone": timezone},
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

    def update_event(
        self,
        event_id: str = "",
        title: str | None = None,
        description: str | None = None,
        location: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        timezone: str | None = None,
        attendees: list[str] | None = None,
        notify_attendees: bool | None = False,
    ) -> str:
        """Update an existing event in the Google Calendar.

        Args:
            event_id: ID of the event to update.
            title: New title/summary.
            description: New description.
            location: New location.
            start_date: New start date/time in ISO format.
            end_date: New end date/time in ISO format.
            timezone: New timezone.
            attendees: Updated list of attendee email addresses.
            notify_attendees: Whether to send email notifications.

        Returns:
            JSON string containing the updated event or error message.

        """
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

    def delete_event(self, event_id: str = "", notify_attendees: bool | None = True) -> str:
        """Delete an event from the Google Calendar.

        Args:
            event_id: ID of the event to delete.
            notify_attendees: Whether to send notifications to attendees (default: True).

        Returns:
            JSON string containing success or error message.

        """
        send_updates = "all" if notify_attendees else "none"
        try:
            service = self._google_api_service()
            service.events().delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates=send_updates).execute()
        except HttpError as exc:
            return _calendar_http_error("delete_event", exc)
        return json.dumps({"success": True, "message": f"Event {event_id} deleted successfully."})

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

    def fetch_all_events(
        self,
        max_results: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Fetch all Google Calendar events in a given date range.

        Paginates through results automatically to retrieve every matching event.

        Args:
            max_results: Number of events to fetch per page (default: 10, max: 100).
            start_date: Start of range in ISO format (YYYY-MM-DDTHH:MM:SS).
            end_date: End of range in ISO format (YYYY-MM-DDTHH:MM:SS).

        Returns:
            JSON string containing all events or error message.

        """
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

    def find_available_slots(
        self,
        start_date: str = "",
        end_date: str = "",
        duration_minutes: int = 30,
    ) -> str:
        """Find available time slots within a date range based on your calendar.

        Checks existing events against working hours to find free windows.
        Dates, working hours and the returned slots are in your Google Calendar timezone, or UTC if that
        timezone cannot be loaded; the result's "timezone" field names the one used.

        Args:
            start_date: Start date in ISO format (YYYY-MM-DD).
            end_date: End date in ISO format (YYYY-MM-DD).
            duration_minutes: Length of the desired slot in minutes (default: 30).

        Returns:
            JSON string containing available time slots or error message.

        """
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

    def list_calendars(self, page_token: str | None = None) -> str:
        """List all available Google Calendars for the authenticated user.

        Args:
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string containing calendars and nextPageToken if more results exist.

        """
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

    def get_event(self, event_id: str = "") -> str:
        """Get full details of a single Google Calendar event by its ID.

        Args:
            event_id: The unique identifier of the event.

        Returns:
            JSON string with full event details.

        """
        try:
            service = self._google_api_service()
            event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            return _calendar_http_error("get_event", exc)
        return json.dumps(event)

    def quick_add_event(self, text: str = "") -> str:
        """Create a Google Calendar event from a natural language description.

        Examples: "Meeting with John tomorrow 3pm", "Lunch at noon on Friday".

        Args:
            text: Natural language description of the event including date, time, and title.

        Returns:
            JSON string containing the created event or error message.

        """
        try:
            service = self._google_api_service()
            event = service.events().quickAdd(calendarId=self.calendar_id, text=text).execute()
        except HttpError as exc:
            return _calendar_http_error("quick_add_event", exc)
        return json.dumps(event)

    def check_availability(
        self,
        start_date: str = "",
        end_date: str = "",
        attendee_emails: list[str] | None = None,
        timezone: str | None = None,
    ) -> str:
        """Check availability of one or more people within a time window.

        Returns busy time ranges for each attendee.
        Use "me" or omit attendee_emails to check the authenticated user's own calendar.

        Args:
            start_date: Start of the time window in ISO format (YYYY-MM-DDTHH:MM:SS).
            end_date: End of the time window in ISO format (YYYY-MM-DDTHH:MM:SS).
            attendee_emails: Email addresses to check availability for. Defaults to the authenticated user's calendar
                if not provided. Use "me" for the authenticated user alongside other emails.
            timezone: Timezone for the query (default: UTC).

        Returns:
            JSON string with busy periods for each attendee.

        """
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

    def search_events(
        self,
        query: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        max_results: int = 10,
        page_token: str | None = None,
    ) -> str:
        """Search Google Calendar events by text across summary, description, location, and attendees.

        Unlike list_events which returns upcoming events, this performs full-text search.

        Args:
            query: Free-text search terms.
            start_date: Start of search window in ISO format.
            end_date: End of search window in ISO format.
            max_results: Maximum number of events to return (default: 10).
            page_token: Token from a previous response to fetch the next page.

        Returns:
            JSON string containing events and nextPageToken if more results exist.

        """
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

    def move_event(
        self,
        event_id: str = "",
        destination_calendar_id: str = "",
        notify_attendees: bool | None = False,
    ) -> str:
        """Move a Google Calendar event to a different calendar.

        Args:
            event_id: ID of the event to move.
            destination_calendar_id: ID of the target calendar.
            notify_attendees: Whether to notify attendees of the move.

        Returns:
            JSON string containing the moved event or error message.

        """
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

    def get_event_attendees(self, event_id: str = "") -> str:
        """Get the attendee list and their RSVP statuses for a Google Calendar event.

        Args:
            event_id: ID of the event.

        Returns:
            JSON string with attendees, their emails, names, and response statuses
            (accepted, declined, tentative, needsAction).

        """
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

    def respond_to_event(self, event_id: str = "", response: str = "") -> str:
        """Set the authenticated user's attendance response for a Google Calendar event.

        Args:
            event_id: ID of the event.
            response: Response status - one of "accepted", "declined", or "tentative".

        Returns:
            JSON string containing the updated event or error message.

        """
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
