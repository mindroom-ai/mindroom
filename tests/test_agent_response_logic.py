"""Tests for agent response decision logic.

This module comprehensively tests all agent response rules:
1. Mentioned agents always respond
2. Single agent continues conversation
3. Multiple agents need explicit mentions
4. Smart routing for new threads
5. Invited agents behave like native agents

These tests ensure no regressions in the core response logic.
"""

from mindroom.thread_utils import should_agent_respond


class TestAgentResponseLogic:
    """Test the should_agent_respond logic."""

    def test_mentioned_agent_always_responds(self):
        """If an agent is mentioned, it should always respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is True

    def test_only_agent_in_thread_continues(self):
        """If agent is the only one in thread, it continues."""
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is True

    def test_invited_agent_behaves_like_native_agent(self):
        """Invited agents should follow the same rules as native agents."""
        # Test 1: Invited agent with no agents in thread - should use router
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

        # Test 2: Invited agent as only agent in thread - should continue
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
            mentioned_agents=[],
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
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_no_agents_in_thread_uses_router(self):
        """If no agents have participated, use router."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_multiple_agents_nobody_responds(self):
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
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_not_in_thread_uses_router(self):
        """If not in a thread, use router to determine response."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_agent_not_in_room_no_response(self):
        """If agent is not in room (native or invited), don't respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=["!other_room:localhost"],  # Different room
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_mentioned_outside_thread_responds(self):
        """Agents respond when mentioned in room (will create thread)."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is True

    def test_agent_mentioned_in_thread_history(self):
        """When any agent is mentioned in thread, only mentioned agents respond."""
        # Thread history with agent mentions
        thread_history = [
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
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_router_selection_scenarios(self):
        """Test various scenarios where router should be used."""
        # Scenario 1: Empty thread, native agent
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
            mentioned_agents=[],
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
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_room_message_no_access_no_response(self):
        """Agent without room access doesn't respond to room messages."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room_id="!room:localhost",
            configured_rooms=[],  # No access to this room
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_edge_case_empty_configured_rooms(self):
        """Test agent with no configured rooms but invited to thread."""
        # Should behave same as native agent when invited
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # No native rooms
            thread_history=[],
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_mixed_agent_and_user_messages(self):
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
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
            mentioned_agents=[],
        )
        assert should_respond is False

    def test_router_disabled_when_any_agent_mentioned(self):
        """Test that router is disabled when any agent is mentioned, not just the current one."""
        # Room message scenario - agent1 is NOT mentioned but agent2 IS mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room_id="!test:example.org",
            configured_rooms=["!test:example.org"],
            thread_history=[],
            mentioned_agents=["agent2"],  # Another agent is mentioned
        )
        # Agent1 should not respond and should NOT use router
        assert not should_respond

        # Now test when no agents are mentioned - router should be used
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room_id="!test:example.org",
            configured_rooms=["!test:example.org"],
            thread_history=[],
            mentioned_agents=[],  # No agents mentioned
        )
        # Agent1 should not respond but SHOULD use router
        assert not should_respond

        # Test when current agent is mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=True,
            room_id="!test:example.org",
            configured_rooms=["!test:example.org"],
            thread_history=[],
            mentioned_agents=["agent1"],  # Current agent is mentioned
        )
        # Agent1 SHOULD respond and should NOT use router
        assert should_respond
