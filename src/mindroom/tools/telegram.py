"""Telegram tool configuration and native toolkit."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Literal

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools._comms import restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

_TelegramReadAction = Literal["get_chat", "get_file"]
_TelegramSendAction = Literal["message", "photo", "document", "video", "audio", "animation", "sticker"]
_TelegramManageAction = Literal["edit", "delete", "react", "pin"]

_ALL_READ_ACTIONS: tuple[_TelegramReadAction, ...] = ("get_chat", "get_file")
_ALL_SEND_ACTIONS: tuple[_TelegramSendAction, ...] = (
    "message",
    "photo",
    "document",
    "video",
    "audio",
    "animation",
    "sticker",
)
_ALL_MANAGE_ACTIONS: tuple[_TelegramManageAction, ...] = ("edit", "delete", "react", "pin")


_SEND_FIELDS = {
    "message": "message",
    "photo": "media_base64 caption",
    "document": "media_base64 filename caption",
    "video": "media_base64 caption",
    "audio": "media_base64 caption title",
    "animation": "media_base64 caption",
    "sticker": "media_base64",
}


def _save_download(output_directory: Path, remote_path: str, content: bytes) -> Path:
    """Save under the operator's directory, without trusting the remote name or replacing files."""
    output_directory.mkdir(parents=True, exist_ok=True)
    suffix = PurePosixPath(remote_path).suffix
    suffix = suffix if suffix.removeprefix(".").isascii() and suffix.removeprefix(".").isalnum() else ""
    with NamedTemporaryFile(dir=output_directory, prefix="telegram-", suffix=suffix, delete=False) as handle:
        path = Path(handle.name)
        try:
            handle.write(content)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    return path


def _decode_media(media_base64: str | None, *, action: str) -> bytes | str:
    """Decode base64 media bytes or return an error message."""
    if not media_base64 or not media_base64.strip():
        return f"`media_base64` is required for action '{action}'."
    try:
        return base64.b64decode(media_base64, validate=True)
    except (binascii.Error, ValueError):
        return "Invalid base64 media content."


class _MissingChatIDError(ValueError):
    """A chat operation is missing the configured destination."""


class _TelegramTools(Toolkit):
    """Send messages/media, manage messages, and read chat or file details via a Telegram bot."""

    def __init__(
        self,
        chat_id: str | None = None,
        token: str | None = None,
        output_directory: str | None = None,
        save_downloads: bool = False,
        enable_send_message: bool = True,
        enable_send_photo: bool = False,
        enable_send_document: bool = False,
        enable_send_video: bool = False,
        enable_send_audio: bool = False,
        enable_send_animation: bool = False,
        enable_send_sticker: bool = False,
        enable_edit_message: bool = False,
        enable_delete_message: bool = False,
        enable_react_with_emoji: bool = False,
        enable_pin_message: bool = False,
        enable_get_chat: bool = False,
        enable_get_file: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        try:
            from telebot import TeleBot
            from telebot.apihelper import ApiTelegramException
            from telebot.types import ReactionTypeEmoji
        except ImportError as exc:
            msg = "`pyTelegramBotAPI` not installed. Install with `pip install pytelegrambotapi`."
            raise ImportError(msg) from exc

        if not token:
            msg = "TELEGRAM_TOKEN not set. Please set the TELEGRAM_TOKEN environment variable."
            raise ValueError(msg)

        self.token = token
        self.chat_id = chat_id or None
        self.bot = TeleBot(self.token)
        self._api_error = ApiTelegramException
        self._reaction_emoji_cls = ReactionTypeEmoji

        clean_dir = output_directory.strip() if isinstance(output_directory, str) and output_directory.strip() else None
        self.save_downloads = bool(save_downloads or clean_dir is not None)
        self.output_directory: Path | None = (
            (Path(clean_dir).resolve() if clean_dir is not None else Path.cwd().resolve())
            if self.save_downloads
            else None
        )

        self._read_actions: tuple[_TelegramReadAction, ...] = tuple(
            action
            for action, enabled in (
                ("get_chat", all or enable_get_chat),
                ("get_file", all or enable_get_file),
            )
            if enabled
        )
        self._send_actions: tuple[_TelegramSendAction, ...] = tuple(
            action
            for action, enabled in (
                ("message", all or enable_send_message),
                ("photo", all or enable_send_photo),
                ("document", all or enable_send_document),
                ("video", all or enable_send_video),
                ("audio", all or enable_send_audio),
                ("animation", all or enable_send_animation),
                ("sticker", all or enable_send_sticker),
            )
            if enabled
        )
        self._manage_actions: tuple[_TelegramManageAction, ...] = tuple(
            action
            for action, enabled in (
                ("edit", all or enable_edit_message),
                ("delete", all or enable_delete_message),
                ("react", all or enable_react_with_emoji),
                ("pin", all or enable_pin_message),
            )
            if enabled
        )

        super().__init__(name="telegram")
        if not self._read_actions:
            self.functions.pop("telegram_read", None)
        if not self._send_actions:
            self.functions.pop("telegram_send", None)
        if not self._manage_actions:
            self.functions.pop("telegram_manage", None)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict action enums on offered Telegram functions to enabled actions."""
        if function.name == "telegram_read" and len(self._read_actions) < len(_ALL_READ_ACTIONS):
            return restrict_actions(function, self._read_actions, {"get_chat": "", "get_file": "file_id"})
        if function.name == "telegram_send" and len(self._send_actions) < len(_ALL_SEND_ACTIONS):
            return restrict_actions(function, self._send_actions, _SEND_FIELDS)
        if function.name == "telegram_manage" and len(self._manage_actions) < len(_ALL_MANAGE_ACTIONS):
            return restrict_actions(
                function,
                self._manage_actions,
                {"edit": "text", "delete": "", "react": "emoji", "pin": "disable_notification"},
                common=("message_id",),
            )
        return function

    @property
    def _chat_id(self) -> str:
        if not self.chat_id:
            msg = "chat_id is required. Set it in the constructor or set the TELEGRAM_CHAT_ID environment variable."
            raise _MissingChatIDError(msg)
        return self.chat_id

    def _error_json(self, message: str) -> str:
        return json.dumps({"status": "error", "message": message.replace(self.token, "[REDACTED]")})

    def _exception_json(self, exc: Exception) -> str:
        if isinstance(exc, self._api_error):
            return self._error_json(f"Telegram API error ({exc.error_code}): {exc.description}")
        return self._error_json(f"Telegram request failed: {type(exc).__name__}")

    def _get_file(self, file_id: str) -> str:
        file_info = self.bot.get_file(file_id)
        remote_path = file_info.file_path
        if not isinstance(remote_path, str) or not remote_path:
            return self._error_json("File path not available")
        file_content = self.bot.download_file(remote_path)
        result: dict[str, Any] = {
            "status": "success",
            "file_id": file_info.file_id,
            "file_size": file_info.file_size,
        }
        if self.save_downloads and self.output_directory is not None:
            local_path = _save_download(self.output_directory, remote_path, file_content)
            result["saved_filename"] = local_path.name
            result["local_path"] = str(local_path)
        else:
            result["content_base64"] = base64.b64encode(file_content).decode("utf-8")
        return json.dumps(result)

    @tool
    def telegram_read(
        self,
        action: _TelegramReadAction,
        file_id: str | None = None,
    ) -> str:
        """Read Telegram chat metadata or fetch a file (base64 or an operator-configured download).

        - `get_chat`: none
        - `get_file`: `file_id`

        Args:
            action: Read operation.
            file_id: Telegram file ID for `get_file`.

        """
        if action not in self._read_actions:
            return self._error_json(f"Action '{action}' is disabled.")
        try:
            if action == "get_chat":
                chat = self.bot.get_chat(self._chat_id)
                return json.dumps(
                    {
                        "status": "success",
                        "id": chat.id,
                        "type": chat.type,
                        "title": chat.title,
                        "username": chat.username,
                        "first_name": chat.first_name,
                        "last_name": chat.last_name,
                        "description": chat.description,
                    },
                )
            if not file_id or not file_id.strip():
                return self._error_json("`file_id` is required for action 'get_file'.")
            return self._get_file(file_id.strip())
        except _MissingChatIDError:
            raise
        except Exception as exc:
            return self._exception_json(exc)

    def _dispatch_send(
        self,
        action: _TelegramSendAction,
        *,
        message: str | None,
        media_base64: str | None,
        filename: str | None,
        caption: str | None,
        title: str | None,
    ) -> dict[str, Any] | str:
        if action == "message":
            if message is None:
                return "`message` is required for action 'message'."
            result = self.bot.send_message(self._chat_id, message)
            return {"status": "success", "message_id": result.message_id}

        media = _decode_media(media_base64, action=action)
        if isinstance(media, str):
            return media
        if action == "document":
            safe_name = PurePosixPath((filename or "").replace("\\", "/")).name.strip()
            if not safe_name:
                return "`filename` is required for action 'document'."
            result = self.bot.send_document(self._chat_id, (safe_name, media), caption=caption)
        elif action == "photo":
            result = self.bot.send_photo(self._chat_id, media, caption=caption)
        elif action == "video":
            result = self.bot.send_video(self._chat_id, media, caption=caption)
        elif action == "audio":
            result = self.bot.send_audio(self._chat_id, media, caption=caption, title=title)
        elif action == "animation":
            result = self.bot.send_animation(self._chat_id, media, caption=caption)
        else:
            result = self.bot.send_sticker(self._chat_id, media)
        return {"status": "success", "message_id": result.message_id}

    @tool
    def telegram_send(
        self,
        action: _TelegramSendAction,
        message: str | None = None,
        media_base64: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
        title: str | None = None,
    ) -> str:
        """Send to the configured Telegram chat.

        - `message`: `message`
        - `photo`: `media_base64`
        - `video`: `media_base64`
        - `animation`: `media_base64`
        - `document`: `media_base64`, `filename`
        - `audio`: `media_base64`
        - `sticker`: `media_base64`

        Args:
            action: Send operation.
            message: Text content for `message`.
            media_base64: Base64-encoded media bytes for non-text actions (local paths are not supported).
            filename: Display filename for `document`.
            caption: Optional caption for `photo`, `document`, `video`, `audio`, or `animation`.
            title: Optional track title for `audio`.

        """
        if action not in self._send_actions:
            return self._error_json(f"Action '{action}' is disabled.")
        try:
            outcome = self._dispatch_send(
                action,
                message=message,
                media_base64=media_base64,
                filename=filename,
                caption=caption,
                title=title,
            )
            return self._error_json(outcome) if isinstance(outcome, str) else json.dumps(outcome)
        except _MissingChatIDError:
            raise
        except Exception as exc:
            return self._exception_json(exc)

    def _dispatch_manage(
        self,
        action: _TelegramManageAction,
        message_id: int,
        *,
        text: str | None,
        emoji: str | None,
        disable_notification: bool,
    ) -> dict[str, Any] | str:
        if action == "edit":
            if text is None:
                return "`text` is required for action 'edit'."
            self.bot.edit_message_text(text, chat_id=self._chat_id, message_id=message_id)
            return {"status": "success", "message_id": message_id}
        if action == "delete":
            self.bot.delete_message(self._chat_id, message_id)
            return {"status": "success", "deleted": True}
        if action == "react":
            if not emoji or not emoji.strip():
                return "`emoji` is required for action 'react'."
            self.bot.set_message_reaction(
                chat_id=self._chat_id,
                message_id=message_id,
                reaction=[self._reaction_emoji_cls(emoji=emoji)],
            )
            return {"status": "success", "message_id": message_id, "emoji": emoji}
        self.bot.pin_chat_message(self._chat_id, message_id, disable_notification=disable_notification)
        return {"status": "success", "pinned": True, "message_id": message_id}

    @tool
    def telegram_manage(
        self,
        action: _TelegramManageAction,
        message_id: int,
        text: str | None = None,
        emoji: str | None = None,
        disable_notification: bool = False,
    ) -> str:
        """Edit, delete, react to, or pin a message in the configured Telegram chat.

        - `edit`: `message_id`, `text`
        - `delete`: `message_id`
        - `react`: `message_id`, `emoji`
        - `pin`: `message_id`

        Args:
            action: Management operation.
            message_id: Target Telegram message ID.
            text: Replacement text for `edit`.
            emoji: Reaction emoji for `react`.
            disable_notification: Suppress chat notification on `pin`.

        """
        if action not in self._manage_actions:
            return self._error_json(f"Action '{action}' is disabled.")
        try:
            outcome = self._dispatch_manage(
                action,
                message_id,
                text=text,
                emoji=emoji,
                disable_notification=disable_notification,
            )
            return self._error_json(outcome) if isinstance(outcome, str) else json.dumps(outcome)
        except _MissingChatIDError:
            raise
        except Exception as exc:
            return self._exception_json(exc)


@register_tool_with_metadata(
    name="telegram",
    file_access=ToolFileAccess.NONE,
    display_name="Telegram",
    description="Send messages and media, manage messages, and read chat details via a Telegram bot",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTelegram",
    icon_color="text-blue-500",  # Telegram blue
    config_fields=[
        ConfigField(
            name="chat_id",
            env=("TELEGRAM_CHAT_ID",),
            label="Chat ID",
            type="text",
            required=True,
        ),
        ConfigField(
            name="token",
            env=("TELEGRAM_TOKEN",),
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="output_directory",
            label="Output Directory",
            type="text",
            required=False,
            default=None,
            description="Directory where downloaded Telegram files are saved when save_downloads is enabled",
        ),
        ConfigField(
            name="save_downloads",
            label="Save Downloads",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_message",
            label="Enable Send Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_send_photo",
            label="Enable Send Photo",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_document",
            label="Enable Send Document",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_video",
            label="Enable Send Video",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_audio",
            label="Enable Send Audio",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_animation",
            label="Enable Send Animation",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_sticker",
            label="Enable Send Sticker",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_edit_message",
            label="Enable Edit Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_delete_message",
            label="Enable Delete Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_react_with_emoji",
            label="Enable React With Emoji",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_pin_message",
            label="Enable Pin Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_chat",
            label="Enable Get Chat",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_file",
            label="Enable Get File",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["pytelegrambotapi"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#telegram",
    function_names=("telegram_manage", "telegram_read", "telegram_send"),
    action_scoped_grants=("telegram_manage", "telegram_read", "telegram_send"),
)
def telegram_tools() -> type[_TelegramTools]:
    """Return Telegram tools for sending messages."""
    return _TelegramTools
