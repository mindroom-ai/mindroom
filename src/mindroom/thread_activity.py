"""Agent activity tracking across rooms for alien agents."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml


@dataclass
class AgentRoomActivity:
    """Track when an alien agent was last active in a room."""

    agent_name: str
    room_id: str
    last_active: datetime
    active_threads: list[str]  # List of thread IDs where agent is active


class AlienAgentActivityTracker:
    """Tracks alien agent activity globally across rooms."""

    def __init__(self, config_path: Path = Path("matrix_users.yaml")):
        self.config_path = config_path
        self._lock = asyncio.Lock()

    async def load_config(self) -> dict:
        """Load the matrix users config."""
        async with self._lock:
            if self.config_path.exists():
                with open(self.config_path) as f:
                    return yaml.safe_load(f) or {}
            return {}

    async def save_config(self, config: dict) -> None:
        """Save the matrix users config."""
        async with self._lock:
            with open(self.config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    async def update_agent_activity(
        self,
        agent_name: str,
        room_id: str,
        thread_id: str | None = None,
    ) -> None:
        """Update the last activity time for an alien agent in a room."""
        config = await self.load_config()

        # Initialize alien_agent_activity section if not exists
        if "alien_agent_activity" not in config:
            config["alien_agent_activity"] = {}

        # Create key for agent-room combination
        key = f"{agent_name}:{room_id}"

        # Get existing data or create new
        activity = config["alien_agent_activity"].get(
            key, {"agent_name": agent_name, "room_id": room_id, "active_threads": []}
        )

        # Update last active time
        activity["last_active"] = datetime.now().isoformat()

        # Add thread if specified and not already in list
        if thread_id and thread_id not in activity["active_threads"]:
            activity["active_threads"].append(thread_id)

        config["alien_agent_activity"][key] = activity
        await self.save_config(config)

    async def remove_thread_from_agent(
        self,
        agent_name: str,
        room_id: str,
        thread_id: str,
    ) -> None:
        """Remove a thread from an agent's active threads list."""
        config = await self.load_config()
        key = f"{agent_name}:{room_id}"

        if "alien_agent_activity" in config and key in config["alien_agent_activity"]:
            activity = config["alien_agent_activity"][key]
            if thread_id in activity.get("active_threads", []):
                activity["active_threads"].remove(thread_id)

                # If no more active threads, we can consider removing the entry
                if not activity["active_threads"]:
                    # Keep the entry to track last activity time
                    pass

                await self.save_config(config)

    async def get_agent_activity(
        self,
        agent_name: str,
        room_id: str,
    ) -> AgentRoomActivity | None:
        """Get activity info for an agent in a room."""
        config = await self.load_config()
        key = f"{agent_name}:{room_id}"
        activity_data = config.get("alien_agent_activity", {}).get(key)

        if activity_data:
            return AgentRoomActivity(
                agent_name=activity_data["agent_name"],
                room_id=activity_data["room_id"],
                last_active=datetime.fromisoformat(activity_data["last_active"]),
                active_threads=activity_data.get("active_threads", []),
            )
        return None

    async def get_inactive_agents(
        self,
        room_id: str | None = None,
        hours: int = 24,
    ) -> list[AgentRoomActivity]:
        """Get agents that have been inactive for specified hours."""
        config = await self.load_config()
        activities = config.get("alien_agent_activity", {})

        inactive = []
        cutoff = datetime.now() - timedelta(hours=hours)

        for _key, data in activities.items():
            # Filter by room if specified
            if room_id and data["room_id"] != room_id:
                continue

            last_active = datetime.fromisoformat(data["last_active"])
            if last_active < cutoff:
                inactive.append(
                    AgentRoomActivity(
                        agent_name=data["agent_name"],
                        room_id=data["room_id"],
                        last_active=last_active,
                        active_threads=data.get("active_threads", []),
                    )
                )

        return inactive

    async def cleanup_agent_activity(
        self,
        agent_name: str,
        room_id: str,
    ) -> None:
        """Remove an agent's activity record for a room."""
        config = await self.load_config()
        key = f"{agent_name}:{room_id}"

        if "alien_agent_activity" in config and key in config["alien_agent_activity"]:
            del config["alien_agent_activity"][key]
            await self.save_config(config)


# Global instance
alien_activity_tracker = AlienAgentActivityTracker()
