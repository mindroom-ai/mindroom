"""Twilio toolkit."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

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
from mindroom.tools.native_helpers import restrict_actions

if TYPE_CHECKING:
    from twilio.rest import Client

    from mindroom.tool_system.tool_function import ToolFunction

_ReadAction = Literal["get_call_details", "list_messages"]
_PHONE = re.compile(r"\+[1-9]\d{1,14}", re.ASCII)


class _TwilioTools(Toolkit):
    """Send SMS and read calls or messages via Twilio."""

    name = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        region: str | None = None,
        edge: str | None = None,
        debug: bool = False,
        enable_send_sms: bool = True,
        enable_get_call_details: bool = True,
        enable_list_messages: bool = True,
        all: bool = False,  # noqa: A002 - public config field
    ) -> None:
        self._account_sid = account_sid or None
        self._auth_token = auth_token or None
        self._api_key = api_key or None
        self._api_secret = api_secret or None
        self._region = region or None
        self._edge = edge or None
        self._debug = debug
        self._read_actions: dict[_ReadAction, bool] = {
            "get_call_details": all or enable_get_call_details,
            "list_messages": all or enable_list_messages,
        }
        super().__init__()
        if not (all or enable_send_sms):
            self.functions.pop("send_sms")
        if not any(self._read_actions.values()):
            self.functions.pop("twilio_read")

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Advertise only the enabled read actions."""
        if function.name != "twilio_read":
            return function
        return restrict_actions(function, tuple(action for action, enabled in self._read_actions.items() if enabled))

    def _client(self) -> Client:
        from twilio.rest import Client

        if not self._account_sid:
            msg = "Configure a Twilio account SID."
            raise ValueError(msg)
        if self._api_key and self._api_secret:
            username, password = self._api_key, self._api_secret
        elif self._auth_token:
            username, password = self._account_sid, self._auth_token
        else:
            msg = "Configure a Twilio auth token or API key and secret."
            raise ValueError(msg)
        client = Client(username, password, self._account_sid, region=self._region, edge=self._edge)
        if self._debug:
            client.http_client.logger.setLevel(logging.INFO)
        return client

    @tool
    def send_sms(self, to: str, from_: str, body: str) -> str:
        """Send an SMS via Twilio.

        Args:
            to: Recipient number in E.164 format.
            from_: Twilio sender number in E.164 format.
            body: Non-empty message text.

        """
        if not (_PHONE.fullmatch(to) and _PHONE.fullmatch(from_)):
            return "Use E.164 format for both phone numbers."
        if not body.strip():
            return "Provide a non-empty message."
        from twilio.base.exceptions import TwilioRestException

        try:
            message = self._client().messages.create(to=to, from_=from_, body=body)
        except ValueError:
            return "Twilio credentials or settings are invalid."
        except TwilioRestException as exc:
            return f"Twilio rejected the SMS (HTTP {exc.status})."
        return f"SMS sent. SID: {message.sid}"

    @tool
    def twilio_read(
        self,
        action: _ReadAction,
        call_sid: str | None = None,
        limit: int = 20,
    ) -> dict[str, object] | list[dict[str, object]]:
        """Read Twilio calls or recent SMS.

        - `get_call_details`: `call_sid`
        - `list_messages`: no required arguments.

        Args:
            action: Read operation.
            call_sid: Call SID.
            limit: Maximum SMS records; positive.

        """
        if not self._read_actions.get(action):
            return {"error": "Twilio read action is disabled."}
        if invalid := (
            "Provide call_sid."
            if action == "get_call_details" and not call_sid
            else "Use a positive message limit."
            if action == "list_messages" and limit < 1
            else None
        ):
            return {"error": invalid}
        from twilio.base.exceptions import TwilioRestException

        try:
            client = self._client()
            if action == "get_call_details":
                assert call_sid
                call = client.calls(call_sid).fetch()
                return {
                    "to": call.to,
                    "from": call._from,
                    "status": call.status,
                    "duration": call.duration,
                    "direction": call.direction,
                    "price": call.price,
                    "start_time": str(call.start_time),
                    "end_time": str(call.end_time),
                }
            return [
                {
                    "sid": message.sid,
                    "to": message.to,
                    "from": message.from_,
                    "body": message.body,
                    "status": message.status,
                    "date_sent": str(message.date_sent),
                }
                for message in client.messages.list(limit=limit)
            ]
        except ValueError:
            return {"error": "Twilio credentials or settings are invalid."}
        except TwilioRestException as exc:
            return {"error": f"Twilio read failed (HTTP {exc.status})."}


@register_tool_with_metadata(
    name="twilio",
    file_access=ToolFileAccess.NONE,
    display_name="Twilio",
    description="SMS messaging and voice communication platform",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="TbBrandTwilio",
    icon_color="text-red-600",  # Twilio red
    config_fields=[
        ConfigField(
            name="account_sid",
            env=("TWILIO_ACCOUNT_SID",),
            label="Account Sid",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="auth_token",
            env=("TWILIO_AUTH_TOKEN",),
            label="Auth Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_key",
            env=("TWILIO_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_secret",
            env=("TWILIO_API_SECRET",),
            label="API Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="region",
            env=("TWILIO_REGION",),
            label="Region",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="edge",
            env=("TWILIO_EDGE",),
            label="Edge",
            type="text",
            required=False,
            default=None,
        ),
        bool_config_field("debug", "Debug", default=False),
        bool_config_field("enable_send_sms", "Enable Send Sms"),
        bool_config_field("enable_get_call_details", "Enable Get Call Details"),
        bool_config_field("enable_list_messages", "Enable List Messages"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["twilio"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#twilio",
    function_names=("send_sms", "twilio_read"),
    action_scoped_grants=("twilio_read",),
)
def twilio_tools() -> type[_TwilioTools]:
    """Return the native twilio toolkit."""
    return _TwilioTools
