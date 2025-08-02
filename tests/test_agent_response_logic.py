"""Tests for agent response decision logic."""

from mindroom.utils import should_agent_respond


class TestAgentResponseLogic:
    """Test the should_agent_respond logic."""

    def test_mentioned_agent_always_responds(self):
        """If an agent is mentioned, it should always respond."""
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
        )
        assert should_respond is True
        assert use_router is False

    def test_only_agent_in_thread_continues(self):
        """If agent is the only one in thread, it continues."""
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
        )
        assert should_respond is True
        assert use_router is False

    def test_invited_agent_behaves_like_native_agent(self):
        """Invited agents should follow the same rules as native agents."""
        # Test 1: Invited agent with no agents in thread - should use router
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=[],
        )
        assert should_respond is False
        assert use_router is True

        # Test 2: Invited agent as only agent in thread - should continue
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
        )
        assert should_respond is True
        assert use_router is False

        # Test 3: Invited agent with multiple agents - nobody responds
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@mindroom_general:localhost", "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=True,
            room_id="!room:localhost",
            configured_rooms=[],  # Not native to room
            thread_history=thread_history,
        )
        assert should_respond is False
        assert use_router is False

    def test_no_agents_in_thread_uses_router(self):
        """If no agents have participated, use router."""
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
        )
        assert should_respond is False
        assert use_router is True

    def test_multiple_agents_nobody_responds(self):
        """If multiple agents in thread, nobody responds unless mentioned."""
        thread_history = [
            {"sender": "@mindroom_calculator:localhost", "body": "2+2=4"},
            {"sender": "@mindroom_general:localhost", "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=thread_history,
        )
        assert should_respond is False
        assert use_router is False

    def test_not_in_thread_no_response(self):
        """If not in a thread, don't respond."""
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!room:localhost"],
            thread_history=[],
        )
        assert should_respond is False
        assert use_router is False

    def test_agent_not_in_room_no_response(self):
        """If agent is not in room (native or invited), don't respond."""
        should_respond, use_router = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            is_invited_to_thread=False,
            room_id="!room:localhost",
            configured_rooms=["!other_room:localhost"],  # Different room
            thread_history=[],
        )
        assert should_respond is False
        assert use_router is False
