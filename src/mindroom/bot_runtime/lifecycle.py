"""Lifecycle and room-management workflows for agent bots."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import nio
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from mindroom.background_tasks import create_background_task
from mindroom.commands.handler import _generate_welcome_message
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.avatar import check_and_set_avatar
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.presence import build_agent_status_message, set_presence_status
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from mindroom.bot import AgentBot


logger = get_logger(__name__)

_SYNC_TIMEOUT_MS = 30000


def _create_task_wrapper(
    callback: Callable[..., Awaitable[None]],
    schedule_background_task: Callable[[Coroutine[object, object, object]], asyncio.Task[object]] = (
        create_background_task
    ),
) -> Callable[..., Awaitable[None]]:
    """Create a wrapper that runs the callback as a background task."""

    async def wrapper(*args: object, **kwargs: object) -> None:
        async def error_handler() -> None:
            try:
                await callback(*args, **kwargs)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error in event callback")

        schedule_background_task(error_handler())

    return wrapper


async def join_configured_rooms(self: AgentBot) -> None:
    """Join all rooms this agent is configured for."""
    assert self.client is not None
    joined_rooms = await self.get_joined_rooms(self.client)
    current_rooms = set(joined_rooms or [])
    current_rooms.update(self.client.rooms)

    for room_id in self.rooms:
        if room_id in current_rooms:
            self.logger.debug("Already joined room", room_id=room_id)
            await self._post_join_room_setup(room_id)
            continue

        if await self.join_room(self.client, room_id):
            current_rooms.add(room_id)
            self.logger.info("Joined room", room_id=room_id)
            await self._post_join_room_setup(room_id)
        else:
            self.logger.warning("Failed to join room", room_id=room_id)


async def _post_join_room_setup(self: AgentBot, room_id: str) -> None:
    """Run room setup that should happen after joins and across restarts."""
    if self.agent_name != ROUTER_AGENT_NAME:
        return

    assert self.client is not None

    restored_tasks = await self.restore_scheduled_tasks(self.client, room_id, self.config)
    if restored_tasks > 0:
        self.logger.info(f"Restored {restored_tasks} scheduled tasks in room {room_id}")

    restored_configs = await self.config_confirmation.restore_pending_changes(self.client, room_id)
    if restored_configs > 0:
        self.logger.info(f"Restored {restored_configs} pending config changes in room {room_id}")

    await self._send_welcome_message_if_empty(room_id)


async def leave_unconfigured_rooms(self: AgentBot) -> None:
    """Leave any rooms this agent is no longer configured for."""
    assert self.client is not None

    joined_rooms = await self.get_joined_rooms(self.client)
    if joined_rooms is None:
        return

    current_rooms = set(joined_rooms)
    configured_rooms = set(self.rooms)
    if self.agent_name == ROUTER_AGENT_NAME:
        root_space_id = self.matrix_state_cls.load().space_room_id
        if root_space_id is not None:
            configured_rooms.add(root_space_id)

    await self.leave_non_dm_rooms(self.client, list(current_rooms - configured_rooms))


async def ensure_user_account(self: AgentBot) -> None:
    """Ensure this agent has a Matrix user account."""
    if self.agent_user.user_id:
        return
    self.agent_user = await self.create_agent_user(
        self.matrix_homeserver,
        self.agent_name,
        self.agent_user.display_name,
    )
    self.logger.info(f"Ensured Matrix user account: {self.agent_user.user_id}")


async def _set_avatar_if_available(self: AgentBot) -> None:
    """Set avatar for the agent if an avatar file exists."""
    if not self.client:
        return

    entity_type = "teams" if self.agent_name in self.config.teams else "agents"
    avatar_path = Path(__file__).parent.parent.parent / "avatars" / entity_type / f"{self.agent_name}.png"

    if avatar_path.exists():
        try:
            success = await check_and_set_avatar(self.client, avatar_path)
            if success:
                self.logger.info(f"Successfully set avatar for {self.agent_name}")
            else:
                self.logger.warning(f"Failed to set avatar for {self.agent_name}")
        except Exception as exc:
            self.logger.warning(f"Failed to set avatar: {exc}")


async def _set_presence_with_model_info(self: AgentBot) -> None:
    """Set presence status with model information."""
    if self.client is None:
        return

    status_msg = build_agent_status_message(self.agent_name, self.config)
    await set_presence_status(self.client, status_msg)


async def ensure_rooms(self: AgentBot) -> None:
    """Ensure the agent is in the correct rooms based on configuration."""
    await self.join_configured_rooms()
    await self.leave_unconfigured_rooms()


async def start(self: AgentBot) -> None:
    """Start the agent bot with user account setup but defer room joins."""
    await self.ensure_user_account()
    self.client = await self.login_agent_user(self.matrix_homeserver, self.agent_user)
    await self._set_avatar_if_available()
    await self._set_presence_with_model_info()

    event_callback = partial(_create_task_wrapper, schedule_background_task=self.create_background_task)
    self.client.add_event_callback(event_callback(self._on_invite), nio.InviteEvent)
    self.client.add_event_callback(event_callback(self._on_message), nio.RoomMessageText)
    self.client.add_event_callback(event_callback(self._on_reaction), nio.ReactionEvent)

    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomMessageImage)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomEncryptedImage)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomMessageFile)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomEncryptedFile)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomMessageVideo)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomEncryptedVideo)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomMessageAudio)
    self.client.add_event_callback(event_callback(self._on_media_message), nio.RoomEncryptedAudio)

    self.running = True

    if self.agent_name == ROUTER_AGENT_NAME:
        try:
            await cleanup_all_orphaned_bots(self.client, self.config)
        except Exception as exc:
            self.logger.warning(f"Could not cleanup orphaned bots (non-critical): {exc}")

    self.logger.info(f"Agent setup complete: {self.agent_user.user_id}")


async def try_start(self: AgentBot) -> bool:
    """Try to start the agent bot with retry logic for transient failures."""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(PermanentMatrixStartupError),
        reraise=True,
    )
    async def _start_with_retry() -> None:
        await self.start()

    try:
        await _start_with_retry()
    except Exception as exc:
        logger.exception(f"Failed to start agent {self.agent_name}")
        if isinstance(exc, PermanentMatrixStartupError):
            raise
        return False
    else:
        return True


async def cleanup(self: AgentBot) -> None:
    """Clean up the agent by leaving all rooms and stopping."""
    assert self.client is not None
    try:
        joined_rooms = await self.get_joined_rooms(self.client)
        if joined_rooms:
            await self.leave_non_dm_rooms(self.client, joined_rooms)
    except Exception:
        self.logger.exception("Error leaving rooms during cleanup")

    await self.stop()


async def stop(self: AgentBot) -> None:
    """Stop the agent bot."""
    self.running = False

    try:
        await self.wait_for_background_tasks(timeout=5.0)
        self.logger.info("Background tasks completed")
    except Exception as exc:
        self.logger.warning(f"Some background tasks did not complete: {exc}")

    if self.agent_name == ROUTER_AGENT_NAME:
        cancelled_tasks = await self.cancel_all_running_scheduled_tasks()
        if cancelled_tasks > 0:
            self.logger.info("Cancelled running scheduled tasks", count=cancelled_tasks)

    if self.client is not None:
        self.logger.warning("Client is not None in stop()")
        await self.client.close()
    self.logger.info("Stopped agent bot")


async def _send_welcome_message_if_empty(self: AgentBot, room_id: str) -> None:
    """Send a welcome message if the room has no messages yet."""
    assert self.client is not None

    response = await self.client.room_messages(
        room_id,
        limit=2,
        message_filter={"types": ["m.room.message"]},
    )

    if not isinstance(response, nio.RoomMessagesResponse):
        self.logger.error("Failed to check room messages", room_id=room_id, error=str(response))
        return

    if not response.chunk:
        self.logger.info("Room is empty, sending welcome message", room_id=room_id)
        welcome_msg = _generate_welcome_message(room_id, self.config)
        await self._send_response(
            room_id=room_id,
            reply_to_event_id=None,
            response_text=welcome_msg,
            thread_id=None,
            skip_mentions=True,
        )
        self.logger.info("Welcome message sent", room_id=room_id)
    elif len(response.chunk) == 1:
        msg = response.chunk[0]
        if (
            isinstance(msg, nio.RoomMessageText)
            and msg.sender == self.agent_user.user_id
            and "Welcome to MindRoom" in msg.body
        ):
            self.logger.debug("Welcome message already sent", room_id=room_id)


async def sync_forever(self: AgentBot) -> None:
    """Run the sync loop for this agent."""
    assert self.client is not None
    await self.client.sync_forever(timeout=_SYNC_TIMEOUT_MS, full_state=True)


async def _on_invite(self: AgentBot, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
    """Handle room invites for the bot account."""
    assert self.client is not None
    self.logger.info("Received invite", room_id=room.room_id, sender=event.sender)
    if await self.join_room(self.client, room.room_id):
        self.logger.info("Joined room", room_id=room.room_id)
        if self.agent_name == ROUTER_AGENT_NAME:
            await self._send_welcome_message_if_empty(room.room_id)
    else:
        self.logger.error("Failed to join room", room_id=room.room_id)
