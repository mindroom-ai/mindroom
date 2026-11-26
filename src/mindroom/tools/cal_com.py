"""Cal.com scheduling and booking management toolkit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    UnsetType,
    error_json,
    require_non_empty,
    resolve_toggle,
    safe_path_segment,
)

_ReadAction = Literal["available_slots", "upcoming_bookings"]
_WriteAction = Literal["create_booking", "reschedule_booking", "cancel_booking"]
_CALCOM_API_BASE = "https://api.cal.com/v2"
_CALCOM_API_VERSION = "2024-08-13"


class _CalComTools(Toolkit):
    """Query availability and manage bookings via the Cal.com v2 API."""

    def __init__(
        self,
        api_key: str | None = None,
        event_type_id: int | None = None,
        user_timezone: str | None = None,
        timeout: int = 30,
        enable_get_available_slots: bool | None | UnsetType = UNSET,
        enable_create_booking: bool | None | UnsetType = UNSET,
        enable_get_upcoming_bookings: bool | None | UnsetType = UNSET,
        enable_reschedule_booking: bool | None | UnsetType = UNSET,
        enable_cancel_booking: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
    ) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.event_type_id = int(event_type_id) if event_type_id is not None and str(event_type_id).strip() else 0
        self.user_timezone = (
            user_timezone.strip() if isinstance(user_timezone, str) and user_timezone.strip() else "America/New_York"
        )
        self.timeout = timeout

        enable_all = resolve_toggle(all, default=False)
        self._allow_slots = resolve_toggle(enable_get_available_slots, default=True, enable_all=enable_all)
        self._allow_upcoming = resolve_toggle(enable_get_upcoming_bookings, default=True, enable_all=enable_all)
        self._allow_create = resolve_toggle(enable_create_booking, default=True, enable_all=enable_all)
        self._allow_reschedule = resolve_toggle(enable_reschedule_booking, default=True, enable_all=enable_all)
        self._allow_cancel = resolve_toggle(enable_cancel_booking, default=True, enable_all=enable_all)

        tools: list[Any] = []
        if self._allow_slots or self._allow_upcoming:
            tools.append(self.cal_com_read)
        if self._allow_create or self._allow_reschedule or self._allow_cancel:
            tools.append(self.cal_com_write)
        super().__init__(name="cal_com", tools=tools)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            msg = "Cal.com API key is required."
            raise ValueError(msg)
        return {
            "Authorization": f"Bearer {self.api_key}",
            "cal-api-version": _CALCOM_API_VERSION,
            "Content-Type": "application/json",
        }

    def _require_event_type_id(self) -> int:
        if self.event_type_id <= 0:
            msg = "Cal.com event_type_id is required."
            raise ValueError(msg)
        return self.event_type_id

    def _format_user_time(self, utc_time: str) -> str:
        utc_dt = datetime.fromisoformat(utc_time)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=UTC)
        return utc_dt.astimezone(ZoneInfo(self.user_timezone)).strftime("%Y-%m-%d %H:%M %Z")

    @staticmethod
    def _to_utc_iso(value: str) -> str:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _get_available_slots(self, headers: dict[str, str], start_date: str | None, end_date: str | None) -> str:
        if not self._allow_slots:
            return error_json("Checking available slots is disabled.")
        params = {
            "startTime": f"{require_non_empty(start_date, 'start_date')}T00:00:00Z",
            "endTime": f"{require_non_empty(end_date, 'end_date')}T23:59:59Z",
            "eventTypeId": str(self._require_event_type_id()),
        }
        response = httpx.get(
            f"{_CALCOM_API_BASE}/slots/available",
            headers=headers,
            params=params,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            return f"Failed to fetch slots: {response.text}"
        slots = response.json().get("data", {}).get("slots", {})
        formatted = [
            self._format_user_time(slot["time"])
            for times in slots.values()
            for slot in times
            if isinstance(slot, dict) and "time" in slot
        ]
        return f"Available slots: {', '.join(formatted)}"

    def _get_upcoming_bookings(self, headers: dict[str, str], email: str | None) -> str:
        if not self._allow_upcoming:
            return error_json("Listing upcoming bookings is disabled.")
        params = {"status": "upcoming"}
        if email:
            params["attendeeEmail"] = email
        response = httpx.get(f"{_CALCOM_API_BASE}/bookings", headers=headers, params=params, timeout=self.timeout)
        if response.status_code != 200:
            return f"Failed to fetch bookings: {response.text}"
        bookings = response.json().get("data", [])
        if not bookings:
            return "No upcoming bookings found."
        lines = [
            f"uid: {b['uid']}, Title: {b['title']}, Time: {self._format_user_time(b['start'])}, Status: {b['status']}"
            for b in bookings
        ]
        return "Upcoming bookings:\n" + "\n".join(lines)

    def cal_com_read(
        self,
        action: _ReadAction,
        start_date: str | None = None,
        end_date: str | None = None,
        email: str | None = None,
    ) -> str:
        """Read Cal.com availability and upcoming bookings.

        Actions:
        - `available_slots`: Fetch open slots between `start_date` and `end_date` (`YYYY-MM-DD`).
        - `upcoming_bookings`: List upcoming bookings (optional `email` attendee filter).
        """
        try:
            headers = self._headers()
            if action == "available_slots":
                return self._get_available_slots(headers, start_date, end_date)
            return self._get_upcoming_bookings(headers, email)
        except Exception as exc:
            return f"Error: {exc}"

    def _create_booking(
        self,
        headers: dict[str, str],
        start_time: str | None,
        name: str | None,
        email: str | None,
    ) -> str:
        if not self._allow_create:
            return error_json("Creating bookings is disabled.")
        payload = {
            "start": self._to_utc_iso(require_non_empty(start_time, "start_time")),
            "eventTypeId": self._require_event_type_id(),
            "attendee": {
                "name": require_non_empty(name, "name"),
                "email": require_non_empty(email, "email"),
                "timeZone": self.user_timezone,
            },
        }
        response = httpx.post(f"{_CALCOM_API_BASE}/bookings", json=payload, headers=headers, timeout=self.timeout)
        if response.status_code == 201:
            data = response.json()["data"]
            return (
                f"Booking created successfully for {self._format_user_time(data['start'])}. Booking uid: {data['uid']}"
            )
        return f"Failed to create booking: {response.text}"

    def _reschedule_booking(
        self,
        headers: dict[str, str],
        booking_uid: str | None,
        start_time: str | None,
        reason: str | None,
    ) -> str:
        if not self._allow_reschedule:
            return error_json("Rescheduling bookings is disabled.")
        safe_uid = safe_path_segment(require_non_empty(booking_uid, "booking_uid"), "booking_uid")
        payload = {
            "start": self._to_utc_iso(require_non_empty(start_time, "start_time")),
            "reschedulingReason": require_non_empty(reason, "reason"),
        }
        response = httpx.post(
            f"{_CALCOM_API_BASE}/bookings/{safe_uid}/reschedule",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code == 201:
            data = response.json()["data"]
            return f"Booking rescheduled to {self._format_user_time(data['start'])}. New booking uid: {data['uid']}"
        return f"Failed to reschedule booking: {response.text}"

    def _cancel_booking(self, headers: dict[str, str], booking_uid: str | None, reason: str | None) -> str:
        if not self._allow_cancel:
            return error_json("Cancelling bookings is disabled.")
        safe_uid = safe_path_segment(require_non_empty(booking_uid, "booking_uid"), "booking_uid")
        payload = {"cancellationReason": require_non_empty(reason, "reason")}
        response = httpx.post(
            f"{_CALCOM_API_BASE}/bookings/{safe_uid}/cancel",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code == 200:
            return "Booking cancelled successfully."
        return f"Failed to cancel booking: {response.text}"

    def cal_com_write(
        self,
        action: _WriteAction,
        start_time: str | None = None,
        name: str | None = None,
        email: str | None = None,
        booking_uid: str | None = None,
        reason: str | None = None,
    ) -> str:
        """Create, reschedule, or cancel Cal.com bookings.

        Actions:
        - `create_booking`: Book `start_time` (ISO-8601) for attendee `name` and `email`.
        - `reschedule_booking`: Move `booking_uid` to `start_time` with `reason`.
        - `cancel_booking`: Cancel `booking_uid` with `reason`.
        """
        try:
            headers = self._headers()
            if action == "create_booking":
                return self._create_booking(headers, start_time, name, email)
            if action == "reschedule_booking":
                return self._reschedule_booking(headers, booking_uid, start_time, reason)
            return self._cancel_booking(headers, booking_uid, reason)
        except Exception as exc:
            return f"Error: {exc}"


@register_tool_with_metadata(
    name="cal_com",
    file_access=ToolFileAccess.NONE,
    display_name="Cal.com",
    description="Calendar scheduling and booking management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaCalendarAlt",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("CALCOM_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="event_type_id",
            env=("CALCOM_EVENT_TYPE_ID",),
            label="Event Type ID",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_timezone",
            label="User Timezone",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_get_available_slots",
            label="Enable Get Available Slots",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_booking",
            label="Enable Create Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_upcoming_bookings",
            label="Enable Get Upcoming Bookings",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_reschedule_booking",
            label="Enable Reschedule Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_cancel_booking",
            label="Enable Cancel Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/calendar-and-scheduling/#cal_com",
    function_names=("cal_com_read", "cal_com_write"),
    action_scoped_grants=("cal_com_read", "cal_com_write"),
)
def cal_com_tools() -> type[Toolkit]:
    """Return Cal.com tools for calendar scheduling and booking management."""
    return _CalComTools
