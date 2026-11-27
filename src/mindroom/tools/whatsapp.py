"""WhatsApp tool configuration and native toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

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
from mindroom.tools.native_helpers import error_json, format_http_error, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

_WHATSAPP_API_BASE = "https://graph.facebook.com"
_WhatsAppSendAction = Literal[
    "text",
    "template",
    "reply_buttons",
    "list",
    "image",
    "document",
    "location",
    "reaction",
]


_SEND_FIELDS = {
    "text": "text preview_url",
    "template": "template_name language_code components",
    "reply_buttons": "text buttons header footer",
    "list": "text button_text sections header footer",
    "image": "media_url media_id caption",
    "document": "media_url media_id filename caption",
    "location": "latitude longitude name address",
    "reaction": "message_id emoji",
}


class ReplyButton(BaseModel):
    """A quick-reply button."""

    id: str = Field(..., description="Unique button identifier (e.g. 'yes', 'no').")
    title: str = Field(..., description="Button display text, max 20 characters.")


class ListRow(BaseModel):
    """A selectable row inside a list section."""

    id: str = Field(..., description="Unique row identifier.")
    title: str = Field(..., description="Row title text.")
    description: str | None = Field(None, description="Optional row description.")


class ListSection(BaseModel):
    """A titled section containing selectable rows."""

    title: str = Field(..., description="Section heading.")
    rows: list[ListRow] = Field(..., description="Selectable rows in this section.")


def _interactive_envelope(
    interactive_type: str,
    body_text: str,
    action_payload: dict[str, Any],
    *,
    header: str | None,
    footer: str | None,
) -> dict[str, Any]:
    interactive: dict[str, Any] = {
        "type": interactive_type,
        "body": {"text": body_text},
        "action": action_payload,
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return interactive


def _build_text_payload(to: str, *, text: str | None, preview_url: bool) -> dict[str, Any] | str:
    if text is None:
        return "`text` is required for action 'text'."
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": preview_url, "body": text},
    }


def _build_template_payload(
    to: str,
    *,
    template_name: str | None,
    language_code: str,
    components: list[dict[str, Any]] | None,
) -> dict[str, Any] | str:
    if not template_name or not template_name.strip():
        return "`template_name` is required for action 'template'."
    template: dict[str, Any] = {"name": template_name, "language": {"code": language_code}}
    if components:
        template["components"] = components
    return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}


def _build_reply_buttons_payload(
    to: str,
    *,
    text: str | None,
    buttons: list[ReplyButton] | None,
    header: str | None,
    footer: str | None,
) -> dict[str, Any] | str:
    if not text:
        return "`text` is required for action 'reply_buttons'."
    if not buttons or len(buttons) > 3:
        return "WhatsApp requires 1-3 reply buttons"
    action_buttons = [{"type": "reply", "reply": {"id": btn.id, "title": btn.title[:20]}} for btn in buttons]
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": _interactive_envelope(
            "button",
            text,
            {"buttons": action_buttons},
            header=header,
            footer=footer,
        ),
    }


def _build_list_payload(
    to: str,
    *,
    text: str | None,
    button_text: str | None,
    sections: list[ListSection] | None,
    header: str | None,
    footer: str | None,
) -> dict[str, Any] | str:
    if not text or not button_text:
        return "`text` and `button_text` are required for action 'list'."
    if not sections or len(sections) > 10:
        return "WhatsApp requires 1-10 sections"
    total_rows = sum(len(section.rows) for section in sections)
    if total_rows > 10:
        return f"WhatsApp allows a maximum of 10 rows total across all sections (got {total_rows})."
    sections_payload = [
        {
            "title": section.title,
            "rows": [
                {
                    "id": row.id,
                    "title": row.title[:24],
                    **({"description": row.description[:72]} if row.description else {}),
                }
                for row in section.rows
            ],
        }
        for section in sections
    ]
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": _interactive_envelope(
            "list",
            text,
            {"button": button_text[:20], "sections": sections_payload},
            header=header,
            footer=footer,
        ),
    }


def _build_media_payload(
    kind: Literal["image", "document"],
    to: str,
    *,
    media_url: str | None,
    media_id: str | None,
    filename: str | None,
    caption: str | None,
) -> dict[str, Any] | str:
    if not media_url and not media_id:
        return "Either `media_url` or `media_id` must be provided"
    media: dict[str, Any] = {"id": media_id} if media_id else {"link": media_url}
    if kind == "document" and filename:
        media["filename"] = filename
    if caption:
        media["caption"] = caption
    return {"messaging_product": "whatsapp", "to": to, "type": kind, kind: media}


def _build_location_payload(
    to: str,
    *,
    latitude: float | str | None,
    longitude: float | str | None,
    name: str | None,
    address: str | None,
) -> dict[str, Any] | str:
    if latitude is None or longitude is None:
        return "`latitude` and `longitude` are required for action 'location'."
    location: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = name
    if address:
        location["address"] = address
    return {"messaging_product": "whatsapp", "to": to, "type": "location", "location": location}


def _build_reaction_payload(to: str, *, message_id: str | None, emoji: str | None) -> dict[str, Any] | str:
    if not message_id or emoji is None:
        return "`message_id` and `emoji` are required for action 'reaction'."
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    }


class _WhatsAppTools(Toolkit):
    """Send messages via the WhatsApp Business Cloud API."""

    def __init__(
        self,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        version: str | None = None,
        recipient_waid: str | None = None,
        enable_send_text_message: bool = True,
        enable_send_template_message: bool = True,
        enable_send_reply_buttons: bool = False,
        enable_send_list_message: bool = False,
        enable_send_image: bool = False,
        enable_send_document: bool = False,
        enable_send_location: bool = False,
        enable_send_reaction: bool = False,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        if not access_token:
            msg = "WHATSAPP_ACCESS_TOKEN is not set. Set the environment variable or pass access_token."
            raise ValueError(msg)

        if not phone_number_id:
            msg = "WHATSAPP_PHONE_NUMBER_ID is not set. Set the environment variable or pass phone_number_id."
            raise ValueError(msg)

        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.default_recipient = recipient_waid or None
        self.version = version or "v22.0"
        self.base_url = _WHATSAPP_API_BASE
        self.timeout = timeout

        self._enabled_actions: tuple[_WhatsAppSendAction, ...] = tuple(
            action
            for action, enabled in (
                ("text", all or enable_send_text_message),
                ("template", all or enable_send_template_message),
                ("reply_buttons", all or enable_send_reply_buttons),
                ("list", all or enable_send_list_message),
                ("image", all or enable_send_image),
                ("document", all or enable_send_document),
                ("location", all or enable_send_location),
                ("reaction", all or enable_send_reaction),
            )
            if enabled
        )

        super().__init__(name="whatsapp")
        if not self._enabled_actions:
            self.functions.pop("whatsapp_send", None)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``whatsapp_send``'s action schema to enabled send actions."""
        if function.name == "whatsapp_send":
            return restrict_actions(
                function,
                self._enabled_actions,
                action_fields=_SEND_FIELDS,
                common_fields=("recipient",),
            )
        return function

    def _safe_error(self, exc: Exception) -> str:
        return error_json(
            format_http_error("WhatsApp", exc, secrets=(self.access_token,), detail_path=("error", "message")),
        )

    def _send_message(self, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{quote(self.version, safe='')}/{quote(self.phone_number_id, safe='')}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        response = httpx.post(url, headers=headers, json=data, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @tool
    def whatsapp_send(
        self,
        action: _WhatsAppSendAction,
        recipient: str | None = None,
        text: str | None = None,
        preview_url: bool = False,
        template_name: str | None = None,
        language_code: str = "en_US",
        components: list[dict[str, Any]] | None = None,
        buttons: list[ReplyButton] | None = None,
        button_text: str | None = None,
        sections: list[ListSection] | None = None,
        header: str | None = None,
        footer: str | None = None,
        media_url: str | None = None,
        media_id: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
        latitude: float | str | None = None,
        longitude: float | str | None = None,
        name: str | None = None,
        address: str | None = None,
        message_id: str | None = None,
        emoji: str | None = None,
    ) -> str:
        """Send a WhatsApp Business API message.

        Requires `recipient` unless configured.
        - `text`: `text`
        - `template`: `template_name`
        - `reply_buttons`: `text`, `buttons`
        - `list`: `text`, `button_text`, `sections`
        - `image`: `media_url` or `media_id`
        - `document`: `media_url` or `media_id`
        - `location`: `latitude`, `longitude`
        - `reaction`: `message_id`, `emoji`

        Args:
            action: Message type to send.
            recipient: Recipient WhatsApp ID or phone number; defaults to configured `recipient_waid`.
            text: Message text.
            preview_url: Show link previews (`text`).
            template_name: Approved template name (`template`).
            language_code: Template locale, e.g. `en_US`.
            components: Optional template components (`template`).
            buttons: 1-3 quick-reply buttons (`reply_buttons`).
            button_text: List button label, max 20 chars (`list`).
            sections: 1-10 list sections with <=10 total rows (`list`).
            header: Optional interactive header text (`reply_buttons`, `list`).
            footer: Optional interactive footer text (`reply_buttons`, `list`).
            media_url: Public URL for `image` or `document`.
            media_id: WhatsApp media ID for `image` or `document`.
            filename: Display filename for `document`.
            caption: Optional caption for `image` or `document`.
            latitude: Location latitude (`location`).
            longitude: Location longitude (`location`).
            name: Optional location name (`location`).
            address: Optional location address (`location`).
            message_id: Target WhatsApp message ID (`reaction`).
            emoji: Reaction emoji; an empty string removes the reaction.

        """
        if action not in self._enabled_actions:
            return json.dumps({"error": f"Action '{action}' is disabled."})
        to = recipient or self.default_recipient
        if not to:
            return json.dumps({"error": "No recipient provided and no default recipient set"})

        builders = {
            "text": lambda: _build_text_payload(to, text=text, preview_url=preview_url),
            "template": lambda: _build_template_payload(
                to,
                template_name=template_name,
                language_code=language_code,
                components=components,
            ),
            "reply_buttons": lambda: _build_reply_buttons_payload(
                to,
                text=text,
                buttons=buttons,
                header=header,
                footer=footer,
            ),
            "list": lambda: _build_list_payload(
                to,
                text=text,
                button_text=button_text,
                sections=sections,
                header=header,
                footer=footer,
            ),
            "image": lambda: _build_media_payload(
                "image",
                to,
                media_url=media_url,
                media_id=media_id,
                filename=None,
                caption=caption,
            ),
            "document": lambda: _build_media_payload(
                "document",
                to,
                media_url=media_url,
                media_id=media_id,
                filename=filename,
                caption=caption,
            ),
            "location": lambda: _build_location_payload(
                to,
                latitude=latitude,
                longitude=longitude,
                name=name,
                address=address,
            ),
            "reaction": lambda: _build_reaction_payload(to, message_id=message_id, emoji=emoji),
        }
        payload = builders[action]()
        if isinstance(payload, str):
            return json.dumps({"error": payload})

        try:
            response = self._send_message(payload)
        except (httpx.HTTPError, ValueError) as exc:
            return self._safe_error(exc)
        messages = response.get("messages")
        first = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
        return json.dumps({"ok": True, "message_id": first.get("id", "unknown")})


@register_tool_with_metadata(
    name="whatsapp",
    file_access=ToolFileAccess.NONE,
    display_name="WhatsApp Business",
    description="Send text and template messages via WhatsApp Business API",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiWhatsapp",
    icon_color="text-green-500",
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="access_token",
            env=("WHATSAPP_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=False,
            placeholder="EAAxxxxxxx...",
            description="WhatsApp Business API access token",
        ),
        ConfigField(
            name="phone_number_id",
            env=("WHATSAPP_PHONE_NUMBER_ID",),
            label="Phone Number ID",
            type="text",
            required=False,
            placeholder="1234567890123456",
            description="WhatsApp Business Account phone number ID",
        ),
        ConfigField(
            name="version",
            env=("WHATSAPP_VERSION",),
            label="API Version",
            type="text",
            required=False,
            default="v22.0",
            placeholder="v22.0",
            description="WhatsApp API version to use",
        ),
        ConfigField(
            name="recipient_waid",
            env=("WHATSAPP_RECIPIENT_WAID",),
            label="Default Recipient WhatsApp ID",
            type="text",
            required=False,
            default=None,
            placeholder="+1234567890",
            description="Default recipient WhatsApp ID or phone number (optional)",
        ),
        bool_config_field(
            "enable_send_text_message",
            "Enable Send Text Message",
            description="Enable plain text WhatsApp messages",
        ),
        bool_config_field(
            "enable_send_template_message",
            "Enable Send Template Message",
            description="Enable WhatsApp template messages",
        ),
        bool_config_field(
            "enable_send_reply_buttons",
            "Enable Send Reply Buttons",
            default=False,
            description="Enable interactive reply button messages",
        ),
        bool_config_field(
            "enable_send_list_message",
            "Enable Send List Message",
            default=False,
            description="Enable interactive list messages",
        ),
        bool_config_field("enable_send_image", "Enable Send Image", default=False, description="Enable image messages"),
        bool_config_field(
            "enable_send_document",
            "Enable Send Document",
            default=False,
            description="Enable document messages",
        ),
        bool_config_field(
            "enable_send_location",
            "Enable Send Location",
            default=False,
            description="Enable location messages",
        ),
        bool_config_field(
            "enable_send_reaction",
            "Enable Send Reaction",
            default=False,
            description="Enable reaction messages",
        ),
        bool_config_field("all", "All", default=False, description="Enable all WhatsApp tools"),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            description="Per-request HTTP timeout in seconds",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#whatsapp",
    function_names=("whatsapp_send",),
    action_scoped_grants=("whatsapp_send",),
)
def whatsapp_tools() -> type[_WhatsAppTools]:
    """Return WhatsApp Business API tools for messaging."""
    return _WhatsAppTools
