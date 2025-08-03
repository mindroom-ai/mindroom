"""Thread-specific agent invitation management using Matrix state events."""

from datetime import datetime, timedelta

import nio

from .logging_config import get_logger

logger = get_logger(__name__)

THREAD_INVITE_EVENT_TYPE = "com.mindroom.thread.invite"
DEFAULT_TIMEOUT_HOURS = 24


class ThreadInviteManager:
    def __init__(self, client: nio.AsyncClient):
        self.client = client

    def _get_state_key(self, thread_id: str, agent_name: str) -> str:
        return f"{thread_id}:{agent_name}"

    async def add_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
        invited_by: str,
    ) -> None:
        now = datetime.now().isoformat()
        await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={
                "invited_by": invited_by,
                "invited_at": now,
                "last_activity": now,
            },
            state_key=self._get_state_key(thread_id, agent_name),
        )

    async def get_thread_agents(self, thread_id: str, room_id: str) -> list[str]:
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            return []

        return [
            event.get("state_key", "").split(":", 1)[1]
            for event in response.events
            if event.get("type") == THREAD_INVITE_EVENT_TYPE and event.get("state_key", "").startswith(f"{thread_id}:")
        ]

    async def is_agent_invited_to_thread(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key=self._get_state_key(thread_id, agent_name),
        )
        return isinstance(response, nio.RoomGetStateEventResponse)

    async def get_agent_threads(self, room_id: str, agent_name: str) -> list[str]:
        response = await self.client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            return []

        return [
            event.get("state_key", "").rsplit(":", 1)[0]
            for event in response.events
            if event.get("type") == THREAD_INVITE_EVENT_TYPE and event.get("state_key", "").endswith(f":{agent_name}")
        ]

    async def remove_invite(
        self,
        thread_id: str,
        room_id: str,
        agent_name: str,
    ) -> bool:
        if not await self.is_agent_invited_to_thread(thread_id, room_id, agent_name):
            return False

        response = await self.client.room_put_state(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            content={},
            state_key=self._get_state_key(thread_id, agent_name),
        )
        return isinstance(response, nio.RoomPutStateResponse)

    async def get_invite_state(self, thread_id: str, room_id: str, agent_name: str) -> dict | None:
        """Get the current invitation state for an agent in a thread."""
        response = await self.client.room_get_state_event(
            room_id=room_id,
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key=self._get_state_key(thread_id, agent_name),
        )
        if isinstance(response, nio.RoomGetStateEventResponse):
            return response.content  # type: ignore[no-any-return]
        return None

    async def update_agent_activity(self, thread_id: str, room_id: str, agent_name: str) -> None:
        """Update the last activity timestamp for an agent in a thread."""
        current_state = await self.get_invite_state(thread_id, room_id, agent_name)
        if current_state:
            current_state["last_activity"] = datetime.now().isoformat()
            await self.client.room_put_state(
                room_id=room_id,
                event_type=THREAD_INVITE_EVENT_TYPE,
                content=current_state,
                state_key=self._get_state_key(thread_id, agent_name),
            )

    async def cleanup_inactive_agents(self, room_id: str, timeout_hours: int = DEFAULT_TIMEOUT_HOURS) -> int:
        """Remove agents who haven't responded in the room for timeout_hours."""
        state_response = await self.client.room_get_state(room_id)
        if not isinstance(state_response, nio.RoomGetStateResponse):
            return 0

        # Get all thread invitations and their activity
        now = datetime.now()
        threshold = timedelta(hours=timeout_hours)
        agents_to_remove = []

        for event in state_response.events:
            if event.get("type") == THREAD_INVITE_EVENT_TYPE:
                state_key = event.get("state_key", "")
                if ":" in state_key:
                    thread_id, agent_name = state_key.split(":", 1)
                    content = event.get("content", {})

                    # Get last activity timestamp (use invited_at if no activity tracked)
                    last_activity_str = content.get("last_activity", content.get("invited_at"))
                    if last_activity_str:
                        try:
                            last_activity = datetime.fromisoformat(last_activity_str)
                            if now - last_activity > threshold:
                                agents_to_remove.append((state_key, agent_name))
                        except (ValueError, TypeError):
                            pass

        if not agents_to_remove:
            return 0

        # Remove inactive agents
        removed_count = 0
        for state_key, agent_name in agents_to_remove:
            # Try to kick the agent from the room
            kick_response = await self.client.room_kick(
                room_id, f"@{agent_name}:mindroom.space", f"Inactive for {timeout_hours} hours"
            )
            if isinstance(kick_response, nio.RoomKickResponse):
                # Successfully kicked, now remove the invitation
                await self.client.room_put_state(room_id, THREAD_INVITE_EVENT_TYPE, {}, state_key)
                removed_count += 1
                logger.info(f"Removed inactive agent {agent_name} from room {room_id}")

        return removed_count
