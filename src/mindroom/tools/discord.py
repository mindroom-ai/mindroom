"""Discord tool configuration and native toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

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
from mindroom.tools.native_helpers import format_http_error, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DiscordReadAction = Literal["list_channels", "get_channel_info", "get_channel_messages"]


class _DiscordTools(Toolkit):
    """Interact with Discord guilds, channels, and messages via the Discord REST API."""

    def __init__(
        self,
        bot_token: str | None = None,
        enable_send_message: bool = True,
        enable_get_channel_messages: bool = True,
        enable_get_channel_info: bool = True,
        enable_list_channels: bool = True,
        enable_delete_message: bool = True,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        if not bot_token:
            msg = "Discord bot token is required"
            raise ValueError(msg)

        self.bot_token = bot_token
        self.base_url = _DISCORD_API_BASE
        self.timeout = timeout
        self._allow_send = bool(all or enable_send_message)
        self._allow_delete = bool(all or enable_delete_message)
        self._read_actions: tuple[_DiscordReadAction, ...] = tuple(
            action
            for action, enabled in (
                ("list_channels", all or enable_list_channels),
                ("get_channel_info", all or enable_get_channel_info),
                ("get_channel_messages", all or enable_get_channel_messages),
            )
            if enabled
        )

        super().__init__(name="discord")
        if not self._read_actions:
            self.functions.pop("discord_read", None)
        if not self._allow_send:
            self.functions.pop("discord_send", None)
        if not self._allow_delete:
            self.functions.pop("discord_manage", None)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``discord_read``'s action schema to configured read actions."""
        if function.name == "discord_read":
            return restrict_actions(function, self._read_actions)
        return function

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
        }
        response = httpx.request(
            method,
            f"{self.base_url}{endpoint}",
            headers=headers,
            json=json_body,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    @tool
    def discord_read(
        self,
        action: _DiscordReadAction,
        guild_id: str | None = None,
        channel_id: str | None = None,
        limit: int = 100,
    ) -> str:
        """Read Discord channels or channel messages.

        - `list_channels`: `guild_id`
        - `get_channel_info`: `channel_id`
        - `get_channel_messages`: `channel_id` (`limit` 1-100)
        """
        if action not in self._read_actions:
            return f"Error: action '{action}' is disabled."
        if action == "list_channels":
            if not guild_id or not guild_id.strip():
                return "Provide guild_id."
            endpoint = f"/guilds/{quote(guild_id.strip(), safe='')}/channels"
        elif not channel_id or not channel_id.strip():
            return "Provide channel_id."
        else:
            endpoint = f"/channels/{quote(channel_id.strip(), safe='')}"
        params = None
        if action == "get_channel_messages":
            if not 1 <= limit <= 100:
                return "Use a message limit between 1 and 100."
            endpoint += "/messages"
            params = {"limit": limit}
        try:
            data = self._request("GET", endpoint, params=params)
            return json.dumps(data, indent=2)
        except (httpx.HTTPError, ValueError) as exc:
            return format_http_error("Discord", exc, secrets=(self.bot_token,))

    @tool
    def discord_send(self, channel_id: str, message: str) -> str:
        """Send `message` to Discord `channel_id`."""
        if not self._allow_send:
            return "Error: send_message is disabled."
        if not channel_id.strip():
            return "Error: `channel_id` must be non-empty."
        try:
            self._request(
                "POST",
                f"/channels/{quote(channel_id.strip(), safe='')}/messages",
                json_body={"content": message},
            )
        except (httpx.HTTPError, ValueError) as exc:
            return format_http_error("Discord", exc, secrets=(self.bot_token,))
        return f"Message sent successfully to channel {channel_id}"

    @tool
    def discord_manage(self, channel_id: str, message_id: str) -> str:
        """Delete a message from a Discord channel."""
        if not self._allow_delete:
            return "Discord message deletion is disabled."
        if not channel_id.strip() or not message_id.strip():
            return "Provide channel_id and message_id."
        try:
            self._request(
                "DELETE",
                f"/channels/{quote(channel_id.strip(), safe='')}/messages/{quote(message_id.strip(), safe='')}",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return format_http_error("Discord", exc, secrets=(self.bot_token,))
        return f"Message {message_id} deleted successfully from channel {channel_id}"


@register_tool_with_metadata(
    name="discord",
    file_access=ToolFileAccess.NONE,
    display_name="Discord",
    description="Tool for interacting with Discord channels and servers",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiDiscord",
    icon_color="text-indigo-500",  # Discord brand color
    config_fields=[
        ConfigField(
            name="bot_token",
            env=("DISCORD_BOT_TOKEN",),
            label="Bot Token",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_send_message", "Enable Send Message"),
        bool_config_field("enable_get_channel_messages", "Enable Get Channel Messages"),
        bool_config_field("enable_get_channel_info", "Enable Get Channel Info"),
        bool_config_field("enable_list_channels", "Enable List Channels"),
        bool_config_field("enable_delete_message", "Enable Delete Message"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],  # httpx already included in main dependencies
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#discord",
    function_names=(
        "discord_manage",
        "discord_read",
        "discord_send",
    ),
    action_scoped_grants=("discord_read",),
)
def discord_tools() -> type[_DiscordTools]:
    """Return Discord tools for interacting with Discord channels and servers."""
    return _DiscordTools
