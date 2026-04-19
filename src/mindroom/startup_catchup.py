"""Replay missed user messages before the long-lived Matrix sync loop starts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TypeGuard, assert_never

import nio

from mindroom.commands.parsing import command_parser
from mindroom.matrix.identity import is_agent_id
from mindroom.matrix.sync_tokens import load_sync_token, save_sync_token

if TYPE_CHECKING:
    from .bot import AgentBot
    from .config.main import Config
    from .constants import RuntimePaths

type StartupCatchUpNonAudioMediaEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)
type StartupCatchUpAudioEvent = nio.RoomMessageAudio | nio.RoomEncryptedAudio
type StartupCatchUpMediaEvent = StartupCatchUpNonAudioMediaEvent | StartupCatchUpAudioEvent

type StartupCatchUpEvent = nio.RoomMessageText | StartupCatchUpMediaEvent
type _BotAudioDispatchEvent = StartupCatchUpAudioEvent
type _IsHandled = Callable[[str], bool]
type _TextCatchUpHandler = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[object]]
type _AudioCatchUpHandler = Callable[[nio.MatrixRoom, _BotAudioDispatchEvent], Awaitable[object]]
type _NonAudioMediaCatchUpHandler = Callable[
    [nio.MatrixRoom, StartupCatchUpNonAudioMediaEvent],
    Awaitable[object],
]


class _StartupCatchUpLogger(Protocol):
    def warning(self, event: str, /, **kw: object) -> object: ...
    def exception(self, event: str, /, **kw: object) -> object: ...


STARTUP_CATCH_UP_NON_AUDIO_MEDIA_EVENT_TYPES = (
    nio.RoomMessageImage,
    nio.RoomEncryptedImage,
    nio.RoomMessageFile,
    nio.RoomEncryptedFile,
    nio.RoomMessageVideo,
    nio.RoomEncryptedVideo,
)
STARTUP_CATCH_UP_AUDIO_EVENT_TYPES = (
    nio.RoomMessageAudio,
    nio.RoomEncryptedAudio,
)
STARTUP_CATCH_UP_MEDIA_EVENT_TYPES = (
    *STARTUP_CATCH_UP_NON_AUDIO_MEDIA_EVENT_TYPES,
    *STARTUP_CATCH_UP_AUDIO_EVENT_TYPES,
)


def _is_startup_catch_up_event(event: object) -> TypeGuard[StartupCatchUpEvent]:
    return isinstance(event, (nio.RoomMessageText, *STARTUP_CATCH_UP_MEDIA_EVENT_TYPES))


def _is_startup_audio_event(event: StartupCatchUpMediaEvent) -> TypeGuard[_BotAudioDispatchEvent]:
    return isinstance(event, STARTUP_CATCH_UP_AUDIO_EVENT_TYPES)


def _is_startup_non_audio_media_event(
    event: StartupCatchUpMediaEvent,
) -> TypeGuard[StartupCatchUpNonAudioMediaEvent]:
    return isinstance(event, STARTUP_CATCH_UP_NON_AUDIO_MEDIA_EVENT_TYPES)


def _should_catch_up_message(
    event: object,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    is_handled: _IsHandled,
) -> bool:
    if not _is_startup_catch_up_event(event):
        return False

    if is_agent_id(event.sender, config, runtime_paths):
        return False

    if is_handled(event.event_id):
        return False

    if not isinstance(event, nio.RoomMessageText):
        return True

    content = event.source.get("content") if isinstance(event.source, dict) else None
    relates_to = content.get("m.relates_to") if isinstance(content, dict) else None
    if isinstance(relates_to, dict) and relates_to.get("rel_type") == "m.replace":
        return False

    return command_parser.parse(event.body) is None


def _require_catch_up_sync_response(
    response: nio.SyncResponse | nio.SyncError,
    *,
    agent_name: str,
    logger: _StartupCatchUpLogger,
) -> nio.SyncResponse:
    if isinstance(response, nio.SyncResponse):
        return response
    if isinstance(response, nio.SyncError):
        logger.warning("startup_catch_up_sync_failed", status_code=response.status_code)
        msg = f"Startup catch-up sync failed for {agent_name}"
        raise RuntimeError(msg)  # noqa: TRY004
    assert_never(response)


async def _dispatch_catch_up_event(
    room: nio.MatrixRoom,
    room_id: str,
    event: StartupCatchUpEvent,
    *,
    on_message: _TextCatchUpHandler,
    on_audio_message: _AudioCatchUpHandler,
    on_media_message: _NonAudioMediaCatchUpHandler,
    logger: _StartupCatchUpLogger,
) -> None:
    try:
        if isinstance(event, nio.RoomMessageText):
            await on_message(room, event)
        elif _is_startup_audio_event(event):
            await on_audio_message(room, event)
        elif _is_startup_non_audio_media_event(event):
            await on_media_message(room, event)
    except Exception:
        logger.exception(
            "startup_catch_up_dispatch_failed",
            room_id=room_id,
            event_id=event.event_id,
        )
        raise


async def catch_up_missed_user_messages(bot: AgentBot) -> None:
    """Replay missed startup text and media events before live callbacks register."""
    client = bot.client
    if client is None:
        return
    config = bot.config
    runtime_paths = bot.runtime_paths
    is_handled = bot._turn_store.is_handled
    on_message = bot._on_message
    on_audio_message = bot._on_audio_message
    on_media_message = bot._on_media_message
    logger = bot.logger

    try:
        token = load_sync_token(bot.storage_path, bot.agent_name)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("matrix_sync_token_load_failed", error=str(exc))
        return

    if token is None:
        return

    try:
        response = _require_catch_up_sync_response(
            await client.sync(timeout=0, since=token, full_state=False),
            agent_name=bot.agent_name,
            logger=logger,
        )

        for room_id, room_info in response.rooms.join.items():
            room = client.rooms.get(room_id) or nio.MatrixRoom(room_id, client.user_id or bot.agent_user.user_id or "")
            for event in room_info.timeline.events:
                if not _should_catch_up_message(
                    event,
                    config=config,
                    runtime_paths=runtime_paths,
                    is_handled=is_handled,
                ):
                    continue
                await _dispatch_catch_up_event(
                    room,
                    room_id,
                    event,
                    on_message=on_message,
                    on_audio_message=on_audio_message,
                    on_media_message=on_media_message,
                    logger=logger,
                )
    except Exception:
        client.next_batch = token
        raise

    client.next_batch = response.next_batch
    save_sync_token(bot.storage_path, bot.agent_name, response.next_batch)
