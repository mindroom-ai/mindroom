"""Tests for agent response decision logic.

This module comprehensively tests all agent response rules:
1. Mentioned agents always respond
2. Single agent continues conversation
3. Multiple agents need explicit mentions
4. Smart routing for new threads
5. Invited agents behave like native agents

These tests ensure no regressions in the core response logic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.thread_utils import should_agent_respond


def create_mock_room(room_id: str = "#test:example.org", agents: list[str] | None = None) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        room.users = {f"@mindroom_{agent}:example.org": None for agent in agents}
    else:
        room.users = {}
    return room


class TestAgentResponseLogic:
    """Test the should_agent_respond logic."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
                "agent1": AgentConfig(display_name="Agent1", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test_mentioned_agent_always_responds(self) -> None:
        """If an agent is mentioned, it should always respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=[],
            config=self.config,
        )
        assert should_respond is True

    def test_only_agent_in_thread_continues(self) -> None:
        """If agent is the only one in thread, it continues."""
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
        )
        assert should_respond is True

    def test_invited_agent_behaves_like_native_agent(self) -> None:
        """Invited agents should follow the same rules as native agents."""
        # Test 1: Invited agent with no agents in thread - should take ownership
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # Not native to room
            thread_history=[],
            config=self.config,
            is_invited_to_thread=True,  # Agent is invited
        )
        assert should_respond is True  # Invited agent takes ownership when no one has spoken

        # Test 2: Invited agent as only agent in thread - should continue
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
            config=self.config,
            is_invited_to_thread=True,  # Agent is invited
        )
        assert should_respond is True

        # Test 3: Invited agent with multiple agents - nobody responds
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@mindroom_general:localhost", "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
            config=self.config,
            is_invited_to_thread=True,  # Agent is invited
        )
        assert should_respond is False

    def test_only_invited_agent_responds_when_no_history(self) -> None:
        """When no agents have spoken yet, only invited agents should respond."""
        # Non-invited agent should not respond
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],  # Native to room
            thread_history=[],  # No one has spoken
            config=self.config,
            is_invited_to_thread=False,  # Not invited
        )
        assert should_respond is False  # Should wait for router or invited agent

        # Invited agent should respond
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # Not native
            thread_history=[],  # No one has spoken
            config=self.config,
            is_invited_to_thread=True,  # Invited
        )
        assert should_respond is True  # Should take ownership

    def test_no_agents_in_thread_uses_router(self) -> None:
        """If no agents have participated, use router."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

    def test_multiple_agents_nobody_responds(self) -> None:
        """If multiple agents in thread, nobody responds unless mentioned."""
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@mindroom_general:localhost", "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
        )
        assert should_respond is False

    def test_not_in_thread_uses_router(self) -> None:
        """If not in a thread, use router to determine response."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

    def test_agent_not_in_room_no_response(self) -> None:
        """If agent is not in room (native or invited), don't respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!other_room:localhost"],  # Different room
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

    def test_mentioned_outside_thread_responds(self) -> None:
        """Agents respond when mentioned in room (will create thread)."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=[],
            config=self.config,
        )
        assert should_respond is True

    def test_agent_mentioned_in_thread_history(self) -> None:
        """When any agent is mentioned in thread, only mentioned agents respond."""
        # Thread history with agent mentions
        thread_history: list[dict[str, Any]] = [
            {
                "sender": "@user:localhost",
                "body": "@mindroom_calculator help",
                "content": {"m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]}},
            },
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "what about 3+3?"},
        ]

        # Non-mentioned agent should not respond
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
        )
        assert should_respond is False

    def test_router_selection_scenarios(self) -> None:
        """Test various scenarios where router should be used."""
        # Scenario 1: Empty thread, native agent
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

        # Scenario 2: Thread with only user messages
        thread_history = [
            {"sender": "@user:localhost", "body": "I need help with math"},
            {"sender": "@user:localhost", "body": "Can someone help?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
        )
        assert should_respond is False

    def test_room_message_no_access_no_response(self) -> None:
        """Agent without room access doesn't respond to room messages."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # No access to this room
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

    def test_edge_case_empty_configured_rooms(self) -> None:
        """Test agent with no configured rooms but invited to thread."""
        # Should behave same as native agent when invited
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=[],  # No native rooms
            thread_history=[],
            config=self.config,
        )
        assert should_respond is False

    def test_mixed_agent_and_user_messages(self) -> None:
        """Test thread with interleaved agent and user messages."""
        thread_history = [
            {"sender": "@user:localhost", "body": "Help with math"},
            {"sender": "@mindroom_calculator:localhost", "body": "I can help!"},
            {"sender": "@user:localhost", "body": "Great, what's 2+2?"},
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@mindroom_general:localhost", "body": "I can also help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        # Multiple agents present, nobody should respond without mention
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
        )
        assert should_respond is False

    def test_router_disabled_when_any_agent_mentioned(self) -> None:
        """Test that router is disabled when any agent is mentioned, not just the current one."""
        # Room message scenario - agent1 is NOT mentioned but agent2 IS mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"]),
            is_dm_room=False,
            configured_rooms=["!test:example.org"],
            thread_history=[],
            config=self.config,
        )
        # Agent1 should not respond and should NOT use router
        assert not should_respond

        # Now test when no agents are mentioned - router should be used
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"]),
            is_dm_room=False,
            configured_rooms=["!test:example.org"],
            thread_history=[],
            config=self.config,
            # No agents mentioned
        )
        # Agent1 should not respond but SHOULD use router
        assert not should_respond

        # Test when current agent is mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"]),
            is_dm_room=False,
            configured_rooms=["!test:example.org"],
            thread_history=[],
            config=self.config,
        )
        # Agent1 SHOULD respond and should NOT use router
        assert should_respond

    def test_agent_stops_when_user_mentions_other_agent(self) -> None:
        """Test that an agent stops responding when user mentions a different agent.

        This tests the specific bug where GeneralAgent continued responding
        after the user explicitly mentioned ResearchAgent.
        """
        # Thread history: GeneralAgent was initially mentioned by router and responded
        thread_history = [
            {"sender": "@user:localhost", "body": "hi"},
            {"sender": "@mindroom_router:localhost", "body": "@general could you help with this?"},
            {"sender": "@mindroom_general:localhost", "body": "Hello! How can I help?"},
        ]

        # GeneralAgent should NOT respond because ResearchAgent is mentioned
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,  # GeneralAgent is NOT mentioned
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
            mentioned_agents=["research"],  # ResearchAgent is mentioned
        )
        assert should_respond is False  # Should NOT respond when another agent is mentioned

        # But if no agents are mentioned, general should continue the conversation
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"]),
            is_dm_room=False,
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            config=self.config,
            mentioned_agents=[],  # No agents mentioned
        )
        assert should_respond is True  # Should continue when no one is mentioned
