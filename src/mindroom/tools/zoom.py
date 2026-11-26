"""Zoom tool configuration and native toolkit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

_ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
_ZOOM_API_BASE = "https://api.zoom.us/v2"
_ZoomReadAction = Literal["get_meeting", "list_meetings", "get_upcoming_meetings", "get_meeting_recordings"]
_ZoomManageAction = Literal["schedule_meeting", "delete_meeting"]
_ZoomMeetingType = Literal["scheduled", "live", "upcoming", "upcoming_meetings", "previous_meetings"]


class _ZoomTools(Toolkit):
    """Read, schedule, and delete Zoom meetings using Server-to-Server OAuth."""

    def __init__(
        self,
        account_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.account_id = account_id or None
        self.client_id = client_id or None
        self.__client_secret = client_secret or None
        self.timeout = timeout
        self.__access_token: str | None = None
        self.__token_expiry: datetime | None = None
        super().__init__(name="zoom")

    def _scrub(self, text: str) -> str:
        for secret in (self.__access_token, self.__client_secret):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _get_access_token(self) -> str | None:
        """Mint or reuse the private S2S OAuth bearer token (never exposed to the model)."""
        now = datetime.now(UTC)
        if self.__access_token and self.__token_expiry and now < self.__token_expiry:
            return self.__access_token
        if not self.account_id or not self.client_id or not self.__client_secret:
            return None

        try:
            response = httpx.post(
                _ZOOM_OAUTH_URL,
                auth=(self.client_id, self.__client_secret),
                data={"grant_type": "account_credentials", "account_id": self.account_id},
                timeout=self.timeout,
            )
            response.raise_for_status()
            token_data = response.json()
            access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
            if not isinstance(access_token, str) or not access_token:
                return None
            raw_expires_in = token_data.get("expires_in")
            expires_in = int(3600 if raw_expires_in is None else raw_expires_in)
        except (httpx.HTTPError, ValueError):
            self.__access_token = None
            self.__token_expiry = None
            return None

        self.__access_token = access_token
        self.__token_expiry = now + timedelta(seconds=max(0, expires_in - 60))
        return self.__access_token

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        token = self._get_access_token()
        if not token:
            msg = "Failed to obtain access token"
            raise RuntimeError(msg)
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = httpx.request(
            method,
            f"{_ZOOM_API_BASE}{endpoint}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return response.status_code, {}
        payload = response.json()
        return response.status_code, payload if isinstance(payload, dict) else {}

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, RuntimeError):
            return json.dumps({"error": str(exc)})
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            detail = ""
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                detail = self._scrub(body["message"].strip())
            return json.dumps(
                {"error": f"Zoom API error ({status}): {detail}" if detail else f"Zoom API error ({status})"},
            )
        return json.dumps({"error": f"Zoom request failed: {type(exc).__name__}"})

    @tool
    def zoom_read(
        self,
        action: _ZoomReadAction,
        meeting_id: str | None = None,
        user_id: str = "me",
        meeting_type: _ZoomMeetingType = "scheduled",
    ) -> str:
        """Read Zoom meetings or meeting recordings.

        - `get_meeting`: `meeting_id`
        - `list_meetings`: no required arguments.
        - `get_upcoming_meetings`: no required arguments.
        - `get_meeting_recordings`: `meeting_id`

        Args:
            action: Read operation.
            meeting_id: Zoom meeting ID or UUID for `get_meeting` or `get_meeting_recordings`.
            user_id: Zoom user ID or `"me"` for `list_meetings` or `get_upcoming_meetings`.
            meeting_type: Meeting filter for `list_meetings`.

        """
        try:
            if action == "get_meeting":
                if not meeting_id or not meeting_id.strip():
                    return json.dumps({"error": "`meeting_id` is required for action 'get_meeting'."})
                _, info = self._request("GET", f"/meetings/{quote(meeting_id.strip(), safe='')}")
                result: dict[str, Any] = {
                    "message": "Meeting details retrieved successfully",
                    "meeting_id": str(info.get("id", "")),
                    "topic": info.get("topic", ""),
                    "type": info.get("type", ""),
                    "start_time": info.get("start_time", ""),
                    "duration": info.get("duration", 0),
                    "timezone": info.get("timezone", ""),
                    "created_at": info.get("created_at", ""),
                    "join_url": info.get("join_url", ""),
                    "settings": info.get("settings", {}),
                }
            elif action == "list_meetings":
                _, data = self._request(
                    "GET",
                    f"/users/{quote(user_id.strip() or 'me', safe='')}/meetings",
                    params={"type": meeting_type},
                )
                result = {
                    "message": "Meetings retrieved successfully",
                    "page_count": data.get("page_count", 0),
                    "page_number": data.get("page_number", 1),
                    "page_size": data.get("page_size", 30),
                    "total_records": data.get("total_records", 0),
                    "meetings": data.get("meetings", []),
                }
            elif action == "get_upcoming_meetings":
                _, data = self._request(
                    "GET",
                    f"/users/{quote(user_id.strip() or 'me', safe='')}/meetings",
                    params={"type": "upcoming", "page_size": "30"},
                )
                result = {
                    "message": "Upcoming meetings retrieved successfully",
                    "meetings": data.get("meetings", []),
                }
            else:
                if not meeting_id or not meeting_id.strip():
                    return json.dumps({"error": "`meeting_id` is required for action 'get_meeting_recordings'."})
                _, rec = self._request(
                    "GET",
                    f"/meetings/{quote(meeting_id.strip(), safe='')}/recordings",
                )
                result = {
                    "message": "Meeting recordings retrieved successfully",
                    "meeting_id": str(rec.get("id", "")),
                    "uuid": rec.get("uuid", ""),
                    "host_id": rec.get("host_id", ""),
                    "topic": rec.get("topic", ""),
                    "start_time": rec.get("start_time", ""),
                    "duration": rec.get("duration", 0),
                    "total_size": rec.get("total_size", 0),
                    "recording_count": rec.get("recording_count", 0),
                    "recording_files": rec.get("recording_files", []),
                }
            return json.dumps(result, indent=2)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return self._safe_error(exc)

    @tool
    def zoom_manage(
        self,
        action: _ZoomManageAction,
        topic: str | None = None,
        start_time: str | None = None,
        duration: int | None = None,
        timezone: str = "UTC",
        meeting_id: str | None = None,
        schedule_for_reminder: bool = True,
    ) -> str:
        """Schedule or delete a Zoom meeting.

        - `schedule_meeting`: `topic`, `start_time`, `duration`
        - `delete_meeting`: `meeting_id`

        Args:
            action: Meeting operation.
            topic: Meeting title for `schedule_meeting`.
            start_time: ISO 8601 start time for `schedule_meeting`.
            duration: Duration in minutes for `schedule_meeting`.
            timezone: IANA timezone name for `schedule_meeting`.
            meeting_id: Target Zoom meeting ID for `delete_meeting`.
            schedule_for_reminder: Whether to email cancellation notices on `delete_meeting`.

        """
        try:
            if action == "schedule_meeting":
                if not topic or not topic.strip() or not start_time or not start_time.strip() or duration is None:
                    return json.dumps(
                        {"error": "`topic`, `start_time`, and `duration` are required for action 'schedule_meeting'."},
                    )
                if duration <= 0:
                    return json.dumps({"error": "`duration` must be a positive integer."})
                payload = {
                    "topic": topic,
                    "type": 2,
                    "start_time": start_time,
                    "duration": duration,
                    "timezone": timezone,
                    "settings": {
                        "host_video": True,
                        "participant_video": True,
                        "join_before_host": False,
                        "mute_upon_entry": False,
                        "watermark": True,
                        "audio": "voip",
                        "auto_recording": "none",
                    },
                }
                _, info = self._request("POST", "/users/me/meetings", json_body=payload)
                result: dict[str, Any] = {
                    "message": "Meeting scheduled successfully!",
                    "meeting_id": info["id"],
                    "topic": info["topic"],
                    "start_time": info["start_time"],
                    "duration": info["duration"],
                    "join_url": info["join_url"],
                }
            else:
                if not meeting_id or not meeting_id.strip():
                    return json.dumps({"error": "`meeting_id` is required for action 'delete_meeting'."})
                status, data = self._request(
                    "DELETE",
                    f"/meetings/{quote(meeting_id.strip(), safe='')}",
                    params={"schedule_for_reminder": schedule_for_reminder},
                )
                result = (
                    {"message": "Meeting deleted successfully!", "meeting_id": meeting_id} if status == 204 else data
                )
            return json.dumps(result, indent=2)
        except (httpx.HTTPError, KeyError, RuntimeError, ValueError) as exc:
            return self._safe_error(exc)


@register_tool_with_metadata(
    name="zoom",
    file_access=ToolFileAccess.NONE,
    display_name="Zoom",
    description="Video conferencing platform for scheduling and managing meetings",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    icon="SiZoom",
    icon_color="text-blue-500",  # Zoom blue
    config_fields=[
        # Authentication parameters
        ConfigField(
            name="account_id",
            env=("ZOOM_ACCOUNT_ID",),
            label="Account ID",
            type="text",
            required=False,
            placeholder="your_account_id",
            description="Zoom account ID from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="client_id",
            env=("ZOOM_CLIENT_ID",),
            label="Client ID",
            type="text",
            required=False,
            placeholder="your_client_id",
            description="Client ID from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="client_secret",
            env=("ZOOM_CLIENT_SECRET",),
            label="Client Secret",
            type="password",
            required=False,
            placeholder="your_client_secret",
            description="Client secret from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            description="Per-request HTTP timeout in seconds",
        ),
    ],
    dependencies=[],  # httpx already included in main dependencies
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#zoom",
    function_names=("zoom_manage", "zoom_read"),
    action_scoped_grants=("zoom_manage", "zoom_read"),
)
def zoom_tools() -> type[_ZoomTools]:
    """Return Zoom tools for video conferencing and meeting management."""
    return _ZoomTools
