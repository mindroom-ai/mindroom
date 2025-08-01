"""Tests for alien agent activity tracking."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from mindroom.thread_activity import AlienAgentActivityTracker


class TestAlienAgentActivityTracker:
    """Test alien agent activity tracking."""

    @pytest.fixture
    def temp_config(self):
        """Create a temporary config file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({}, f)
            yield Path(f.name)
        Path(f.name).unlink()

    @pytest.fixture
    def tracker(self, temp_config):
        """Create a tracker with temporary config."""
        return AlienAgentActivityTracker(temp_config)

    @pytest.mark.asyncio
    async def test_update_agent_activity(self, tracker):
        """Test updating agent activity."""
        # Update activity for an agent
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread456",
        )

        # Check activity was recorded
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert activity is not None
        assert activity.agent_name == "calculator"
        assert activity.room_id == "!room123:localhost"
        assert "$thread456" in activity.active_threads
        assert (datetime.now() - activity.last_active).total_seconds() < 5

    @pytest.mark.asyncio
    async def test_update_activity_multiple_threads(self, tracker):
        """Test updating activity with multiple threads."""
        # Add first thread
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread1",
        )

        # Add second thread
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread2",
        )

        # Check both threads are tracked
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert len(activity.active_threads) == 2
        assert "$thread1" in activity.active_threads
        assert "$thread2" in activity.active_threads

    @pytest.mark.asyncio
    async def test_remove_thread_from_agent(self, tracker):
        """Test removing a thread from agent tracking."""
        # Add activity with threads
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread1",
        )
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread2",
        )

        # Remove one thread
        await tracker.remove_thread_from_agent(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread1",
        )

        # Check only one thread remains
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert len(activity.active_threads) == 1
        assert "$thread2" in activity.active_threads
        assert "$thread1" not in activity.active_threads

    @pytest.mark.asyncio
    async def test_get_inactive_agents(self, tracker, temp_config):
        """Test getting inactive agents."""
        # Manually create old activity
        config = {
            "alien_agent_activity": {
                "calculator:!room123:localhost": {
                    "agent_name": "calculator",
                    "room_id": "!room123:localhost",
                    "last_active": (datetime.now() - timedelta(hours=25)).isoformat(),
                    "active_threads": ["$thread1"],
                },
                "code:!room456:localhost": {
                    "agent_name": "code",
                    "room_id": "!room456:localhost",
                    "last_active": datetime.now().isoformat(),  # Recent activity
                    "active_threads": ["$thread2"],
                },
            }
        }
        with open(temp_config, "w") as f:
            yaml.dump(config, f)

        # Get inactive agents (older than 24 hours)
        inactive = await tracker.get_inactive_agents(hours=24)
        assert len(inactive) == 1
        assert inactive[0].agent_name == "calculator"
        assert inactive[0].room_id == "!room123:localhost"

    @pytest.mark.asyncio
    async def test_get_inactive_agents_by_room(self, tracker, temp_config):
        """Test getting inactive agents filtered by room."""
        # Create activities in different rooms
        config = {
            "alien_agent_activity": {
                "calculator:!room123:localhost": {
                    "agent_name": "calculator",
                    "room_id": "!room123:localhost",
                    "last_active": (datetime.now() - timedelta(hours=25)).isoformat(),
                    "active_threads": [],
                },
                "code:!room456:localhost": {
                    "agent_name": "code",
                    "room_id": "!room456:localhost",
                    "last_active": (datetime.now() - timedelta(hours=25)).isoformat(),
                    "active_threads": [],
                },
            }
        }
        with open(temp_config, "w") as f:
            yaml.dump(config, f)

        # Get inactive agents for specific room
        inactive = await tracker.get_inactive_agents(room_id="!room123:localhost", hours=24)
        assert len(inactive) == 1
        assert inactive[0].agent_name == "calculator"
        assert inactive[0].room_id == "!room123:localhost"

    @pytest.mark.asyncio
    async def test_cleanup_agent_activity(self, tracker):
        """Test cleaning up agent activity."""
        # Add activity
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread1",
        )

        # Verify it exists
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert activity is not None

        # Clean it up
        await tracker.cleanup_agent_activity("calculator", "!room123:localhost")

        # Verify it's gone
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert activity is None

    @pytest.mark.asyncio
    async def test_activity_without_thread(self, tracker):
        """Test updating activity without specifying a thread."""
        # Update activity without thread
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
        )

        # Check activity was recorded
        activity = await tracker.get_agent_activity("calculator", "!room123:localhost")
        assert activity is not None
        assert activity.agent_name == "calculator"
        assert activity.room_id == "!room123:localhost"
        assert len(activity.active_threads) == 0

    @pytest.mark.asyncio
    async def test_persistence(self, tracker, temp_config):
        """Test that data persists between tracker instances."""
        # Add activity
        await tracker.update_agent_activity(
            agent_name="calculator",
            room_id="!room123:localhost",
            thread_id="$thread1",
        )

        # Create new tracker instance
        new_tracker = AlienAgentActivityTracker(temp_config)

        # Check activity persisted
        activity = await new_tracker.get_agent_activity("calculator", "!room123:localhost")
        assert activity is not None
        assert "$thread1" in activity.active_threads
