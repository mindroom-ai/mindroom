"""Webex tool configuration and native toolkit."""

from __future__ import annotations

import json

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata


class _WebexTools(Toolkit):
    """List Webex rooms and send messages via the Webex SDK."""

    def __init__(
        self,
        enable_send_message: bool = True,
        enable_list_rooms: bool = True,
        all: bool = False,  # noqa: A002
        access_token: str | None = None,
    ) -> None:
        try:
            from webexpythonsdk import WebexAPI
            from webexpythonsdk.exceptions import ApiError
        except ImportError as exc:
            msg = "Webex tools require `webexpythonsdk`. Install with `pip install webexpythonsdk`."
            raise ImportError(msg) from exc

        if not access_token:
            msg = "Webex access token is not set. Please set the WEBEX_ACCESS_TOKEN environment variable."
            raise ValueError(msg)

        self._api_error = ApiError
        self.client = WebexAPI(access_token=access_token)
        self._allow_read = bool(all or enable_list_rooms)
        self._allow_send = bool(all or enable_send_message)

        super().__init__(name="webex")
        if not self._allow_read:
            self.functions.pop("webex_read", None)
        if not self._allow_send:
            self.functions.pop("webex_send", None)

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, self._api_error):
            return json.dumps({"error": f"Webex API error ({exc.status_code})"})
        return json.dumps({"error": f"Webex request failed: {type(exc).__name__}"})

    @tool
    def webex_read(self) -> str:
        """List Webex rooms with IDs and visibility."""
        if not self._allow_read:
            return json.dumps({"error": "Listing Webex rooms is disabled."})
        try:
            rooms = [
                {
                    "id": room.id,
                    "title": room.title,
                    "type": room.type,
                    "isPublic": room.isPublic,
                    "isReadOnly": room.isReadOnly,
                }
                for room in self.client.rooms.list()
            ]
            return json.dumps({"rooms": rooms}, indent=2)
        except Exception as exc:
            return self._safe_error(exc)

    @tool
    def webex_send(self, room_id: str, text: str) -> str:
        """Send a message to a Webex room.

        Args:
            room_id: Target Webex room ID.
            text: Message text to send.

        """
        if not self._allow_send:
            return json.dumps({"error": "send_message is disabled."})
        if not room_id.strip():
            return json.dumps({"error": "`room_id` must be non-empty."})
        try:
            response = self.client.messages.create(roomId=room_id, text=text)
            return json.dumps(response.json_data)
        except Exception as exc:
            return self._safe_error(exc)


@register_tool_with_metadata(
    name="webex",
    file_access=ToolFileAccess.NONE,
    display_name="Webex",
    description="Video conferencing and messaging platform for teams",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiWebex",
    icon_color="text-blue-600",
    config_fields=[
        bool_config_field("enable_send_message", "Enable Send Message"),
        bool_config_field("enable_list_rooms", "Enable List Rooms"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="access_token",
            env=("WEBEX_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["webexpythonsdk"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#webex",
    function_names=("webex_read", "webex_send"),
)
def webex_tools() -> type[_WebexTools]:
    """Return Webex tools for video conferencing and messaging."""
    return _WebexTools
