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


def create_mock_room(
    room_id: str = "#test:example.org",
    agents: list[str] | None = None,
    config: Config | None = None,
) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        # Use the domain from config if provided, otherwise default to localhost
        domain = config.domain if config else "localhost"
        room.users = {f"@mindroom_{agent}:{domain}": None for agent in agents}
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
                "research": AgentConfig(display_name="Research", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )
        # Helper for generating agent IDs with correct domain
        self.domain = self.config.domain
        self.sender = f"@user:{self.domain}"

    def agent_id(self, agent_name: str) -> str:
        """Generate agent Matrix ID with correct domain."""
        return f"@mindroom_{agent_name}:{self.domain}"

    def test_mentioned_agent_always_responds(self) -> None:
        """If an agent is mentioned, it should always respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_mentioned_agent_blocked_by_reply_permissions(self) -> None:
        """Per-agent reply allowlist should block disallowed senders even when mentioned."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"@alice:{self.domain}"],
        }
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=f"@bob:{self.domain}",
        )
        assert should_respond is False

    def test_mentioned_agent_reply_permissions_honor_aliases(self) -> None:
        """Bridge aliases should inherit per-agent reply permissions."""
        canonical_user = f"@alice:{self.domain}"
        alias_user = f"@telegram_111:{self.domain}"
        self.config.authorization.agent_reply_permissions = {
            "calculator": [canonical_user],
        }
        self.config.authorization.aliases = {canonical_user: [alias_user]}
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=alias_user,
        )
        assert should_respond is True

    def test_mentioned_agent_reply_permissions_support_domain_pattern(self) -> None:
        """Per-agent reply patterns should allow domain-scoped sender matching."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"*:{self.domain}"],
        }
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=f"@bob:{self.domain}",
        )
        assert should_respond is True

    def test_only_agent_in_thread_continues(self) -> None:
        """If agent is the only one in thread, it continues."""
        thread_history = [
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_invited_agent_behaves_like_native_agent(self) -> None:
        """Invited agents should follow the same rules as native agents."""
        # Test 1: Invited agent with no agents in thread - router decides (multiple agents)
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # Test 2: Invited agent as only agent in thread - should continue
        thread_history = [
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True

        # Test 3: Invited agent with multiple agents - nobody responds
        thread_history = [
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": self.agent_id("general"), "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_only_agent_with_access_responds_when_no_history(self) -> None:
        """When no agents have spoken yet, router decides who responds if multiple agents available."""
        # Multiple agents with access - router should decide
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # Agent in room but not configured - should not respond when multiple agents available
        # (router decides) but CAN respond if mentioned
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # But if mentioned, agent in room can respond even if not configured
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True  # Can respond when mentioned even if not configured

    def test_no_agents_in_thread_uses_router(self) -> None:
        """If no agents have participated, router decides who responds (multiple agents available)."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

    def test_multiple_agents_nobody_responds(self) -> None:
        """If multiple agents in thread, nobody responds unless mentioned."""
        thread_history = [
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": self.agent_id("general"), "body": "Let me help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_only_permitted_agent_in_thread_continues(self) -> None:
        """A permitted agent should continue when other thread participants are disallowed."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"@alice:{self.domain}"],
            "general": [f"@bob:{self.domain}"],
        }
        thread_history = [
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": self.agent_id("general"), "body": "I'll help too"},
            {"sender": f"@alice:{self.domain}", "body": "What about 3+3?"},
        ]

        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=f"@alice:{self.domain}",
        )
        assert should_respond is True

    def test_not_in_thread_uses_router(self) -> None:
        """If not in a thread, use router to determine response."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_agent_not_in_room_no_response(self) -> None:
        """If agent is not in room (native or invited), don't respond."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_mentioned_outside_thread_responds(self) -> None:
        """Agents respond when mentioned in room (will create thread)."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_agent_mentioned_in_thread_history(self) -> None:
        """When any agent is mentioned in thread, only mentioned agents respond."""
        # Thread history with agent mentions
        thread_history: list[dict[str, Any]] = [
            {
                "sender": "@user:localhost",
                "body": "@mindroom_calculator help",
                "content": {"m.mentions": {"user_ids": [self.agent_id("calculator")]}},
            },
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": "@user:localhost", "body": "what about 3+3?"},
        ]

        # Non-mentioned agent should not respond
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_router_selection_scenarios(self) -> None:
        """Test various scenarios where router should be used."""
        # Scenario 1: Empty thread, native agent
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
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
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_room_message_no_access_no_response(self) -> None:
        """Agent without room access doesn't respond to room messages."""
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_edge_case_empty_configured_rooms(self) -> None:
        """Test agent with no configured rooms but invited to thread."""
        # Should behave same as native agent when invited
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_mixed_agent_and_user_messages(self) -> None:
        """Test thread with interleaved agent and user messages."""
        thread_history = [
            {"sender": "@user:localhost", "body": "Help with math"},
            {"sender": self.agent_id("calculator"), "body": "I can help!"},
            {"sender": "@user:localhost", "body": "Great, what's 2+2?"},
            {"sender": self.agent_id("calculator"), "body": "2+2=4"},
            {"sender": self.agent_id("general"), "body": "I can also help"},
            {"sender": "@user:localhost", "body": "What about 3+3?"},
        ]

        # Multiple agents present, nobody should respond without mention
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_router_disabled_when_any_agent_mentioned(self) -> None:
        """Test that router is disabled when any agent is mentioned, not just the current one."""
        # Room message scenario - agent1 is NOT mentioned but agent2 IS mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        # Agent1 should not respond and should NOT use router
        assert not should_respond

        # Now test when no agents are mentioned - router should be used
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
            # No agents mentioned
        )
        # Agent1 should not respond but SHOULD use router
        assert not should_respond

        # Test when current agent is mentioned
        should_respond = should_agent_respond(
            agent_name="agent1",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            sender_id=self.sender,
        )
        # Agent1 SHOULD respond and should NOT use router
        assert should_respond

    def test_single_agent_takes_ownership_of_empty_thread(self) -> None:
        """When there's only one agent with access to an empty thread, it takes ownership."""
        # Only one agent in the room
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator"], self.config),  # Only calculator in room
            thread_history=[],  # Empty thread
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True  # Single agent takes ownership

        # Thread with only user messages - single agent should also take ownership
        thread_history = [
            {"sender": "@user:localhost", "body": "I need help"},
            {"sender": "@user:localhost", "body": "Anyone there?"},
        ]
        should_respond = should_agent_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator"], self.config),  # Only calculator
            thread_history=thread_history,
            config=self.config,
            sender_id=self.sender,
        )
        assert should_respond is True  # Single agent takes ownership when only users have spoken

    def test_multiple_non_agent_users_in_thread_require_mentions(self) -> None:
        """Require mention when multiple humans posted in a thread, but allow thread continuity."""
        room = create_mock_room("!room:localhost", ["calculator"], self.config)

        # Thread with two different human senders and no agent yet → require mention
        multi_human_thread = [
            {"sender": "@alice:localhost", "body": "Can someone help?"},
            {"sender": "@bob:localhost", "body": "I also need help"},
        ]
        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=multi_human_thread,
                config=self.config,
                sender_id=self.sender,
            )
            is False
        )

        # Same thread but agent is explicitly mentioned → respond
        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=True,
                is_thread=True,
                room=room,
                thread_history=multi_human_thread,
                config=self.config,
                sender_id=self.sender,
            )
            is True
        )

        # Thread with only one human sender → auto-respond (single agent room)
        single_human_thread = [
            {"sender": "@alice:localhost", "body": "Can someone help?"},
        ]
        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=single_human_thread,
                config=self.config,
                sender_id=self.sender,
            )
            is True
        )

        # Agent already participating in multi-human thread → still require mention
        owned_thread_history = [
            {"sender": "@alice:localhost", "body": "help"},
            {"sender": "@bob:localhost", "body": "me too"},
            {"sender": self.agent_id("calculator"), "body": "Sure, I can help."},
            {"sender": "@alice:localhost", "body": "Can you continue?"},
        ]
        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=owned_thread_history,
                config=self.config,
                sender_id=self.sender,
            )
            is False
        )

    def test_non_agent_mention_suppresses_auto_response(self) -> None:
        """Agent should not auto-respond when a non-agent user is explicitly mentioned."""
        room = create_mock_room("!room:localhost", ["calculator"], self.config)

        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=False,
                room=room,
                thread_history=[],
                config=self.config,
                has_non_agent_mentions=True,
                sender_id=self.sender,
            )
            is False
        )

    def test_multi_human_room_non_thread_auto_responds(self) -> None:
        """Non-thread messages in multi-human rooms auto-respond (single agent)."""
        room = create_mock_room("!room:localhost", ["calculator"], self.config)
        room.users["@alice:localhost"] = None
        room.users["@bob:localhost"] = None

        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=False,
                room=room,
                thread_history=[],
                config=self.config,
                sender_id=self.sender,
            )
            is True
        )

    def test_bot_account_excluded_from_multi_human_thread(self) -> None:
        """A bot_account posting in a thread should not count as a second human."""
        config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            bot_accounts=["@telegram:localhost"],
        )
        room = create_mock_room("!room:localhost", ["calculator"], config)

        thread_with_bot = [
            {"sender": "@alice:localhost", "body": "hello"},
            {"sender": "@telegram:localhost", "body": "relayed message"},
        ]
        # Only one real human — agent should auto-respond
        assert (
            should_agent_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=thread_with_bot,
                config=config,
                sender_id="@alice:localhost",
            )
            is True
        )

    def test_agent_stops_when_user_mentions_other_agent(self) -> None:
        """Test that an agent stops responding when user mentions a different agent.

        This tests the specific bug where GeneralAgent continued responding
        after the user explicitly mentioned ResearchAgent.
        """
        # Thread history: GeneralAgent was initially mentioned by router and responded
        thread_history = [
            {"sender": "@user:localhost", "body": "hi"},
            {"sender": self.agent_id("router"), "body": "@general could you help with this?"},
            {"sender": self.agent_id("general"), "body": "Hello! How can I help?"},
        ]

        # GeneralAgent should NOT respond because ResearchAgent is mentioned
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,  # GeneralAgent is NOT mentioned
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            mentioned_agents=[self.config.ids["research"]],  # ResearchAgent is mentioned
            sender_id=self.sender,
        )
        assert should_respond is False  # Should NOT respond when another agent is mentioned

        # But if no agents are mentioned, general should continue the conversation
        should_respond = should_agent_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            mentioned_agents=[],  # No agents mentioned
            sender_id=self.sender,
        )
        assert should_respond is True  # Should continue when no one is mentioned
