"""Replay missed user messages before the long-lived Matrix sync loop starts."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeGuard, cast

import nio

from mindroom.commands.parsing import command_parser
from mindroom.matrix.identity import is_agent_id
from mindroom.matrix.sync_tokens import load_sync_token, save_sync_token

if TYPE_CHECKING:
    from .bot import AgentBot

type StartupCatchUpMediaEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
    | nio.RoomMessageAudio
    | nio.RoomEncryptedAudio
)

type StartupCatchUpEvent = nio.RoomMessageText | StartupCatchUpMediaEvent
type _BotMediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)


STARTUP_CATCH_UP_MEDIA_EVENT_TYPES = (
    nio.RoomMessageImage,
    nio.RoomEncryptedImage,
    nio.RoomMessageFile,
    nio.RoomEncryptedFile,
    nio.RoomMessageVideo,
    nio.RoomEncryptedVideo,
    nio.RoomMessageAudio,
    nio.RoomEncryptedAudio,
)


def _is_startup_catch_up_event(event: object) -> TypeGuard[StartupCatchUpEvent]:
    return isinstance(event, (nio.RoomMessageText, *STARTUP_CATCH_UP_MEDIA_EVENT_TYPES))


def _should_catch_up_message(bot: AgentBot, event: object) -> bool:
    if not _is_startup_catch_up_event(event):
        return False

    if is_agent_id(event.sender, bot.config, bot.runtime_paths):
        return False

    if bot._turn_store.is_handled(event.event_id):
        return False

    if not isinstance(event, nio.RoomMessageText):
        return True

    content = event.source.get("content") if isinstance(event.source, dict) else None
    relates_to = content.get("m.relates_to") if isinstance(content, dict) else None
    if isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.replace":
        return False

    return command_parser.parse(event.body) is None


def _require_catch_up_sync_response(bot: AgentBot, response: object) -> nio.SyncResponse:
    if isinstance(response, nio.SyncResponse):
        return response
    if isinstance(response, nio.SyncError):
        bot.logger.warning("startup_catch_up_sync_failed", status_code=response.status_code)
        msg = f"Startup catch-up sync failed for {bot.agent_name}"
        raise RuntimeError(msg)  # noqa: TRY004
    msg = f"Unexpected startup catch-up response for {bot.agent_name}: {type(response).__name__}"
    raise TypeError(msg)


async def _dispatch_catch_up_event(
    bot: AgentBot,
    room: nio.MatrixRoom,
    room_id: str,
    event: StartupCatchUpEvent,
) -> None:
    try:
        if isinstance(event, nio.RoomMessageText):
            await bot._on_message(room, event)
        else:
            await bot._on_media_message(room, cast("_BotMediaDispatchEvent", event))
    except Exception:
        bot.logger.exception(
            "startup_catch_up_dispatch_failed",
            room_id=room_id,
            event_id=event.event_id,
        )


async def catch_up_missed_user_messages(bot: AgentBot) -> None:
    """Replay missed startup text and media events before live callbacks register."""
    client = bot.client
    if client is None:
        return

    try:
        token = load_sync_token(bot.storage_path, bot.agent_name)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        bot.logger.warning("matrix_sync_token_load_failed", error=str(exc))
        return

    if token is None:
        return

    response = _require_catch_up_sync_response(
        bot,
        await client.sync(timeout=0, since=token, full_state=False),
    )

    for room_id, room_info in response.rooms.join.items():
        room = client.rooms.get(room_id) or nio.MatrixRoom(room_id, client.user_id or bot.agent_user.user_id or "")
        for event in room_info.timeline.events:
            if not _should_catch_up_message(bot, event):
                continue
            await _dispatch_catch_up_event(bot, room, room_id, event)

    client.next_batch = response.next_batch
    save_sync_token(bot.storage_path, bot.agent_name, response.next_batch)
