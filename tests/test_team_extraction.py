"""Tests for team response extraction functions."""

from __future__ import annotations

from unittest.mock import MagicMock

from agno.models.message import Message
from agno.run.response import RunResponse
from agno.run.team import TeamRunResponse

from mindroom.teams import _get_response_content, format_team_response


class TestExtractContent:
    """Tests for _get_response_content function."""

    def test_get_response_content_from_response_with_content(self) -> None:
        """Test extracting content when response.content is present."""
        response = MagicMock()
        response.content = "Direct content"
        response.messages = []

        result = _get_response_content(response)
        assert result == "Direct content"

    def test_get_response_content_from_messages(self) -> None:
        """Test extracting content from messages when no direct content."""
        msg1 = MagicMock(spec=Message)
        msg1.role = "assistant"
        msg1.content = "First message"

        msg2 = MagicMock(spec=Message)
        msg2.role = "user"  # Should be ignored
        msg2.content = "User message"

        msg3 = MagicMock(spec=Message)
        msg3.role = "assistant"
        msg3.content = " Second message"

        response = MagicMock()
        response.content = None
        response.messages = [msg1, msg2, msg3]

        result = _get_response_content(response)
        assert result == "First message\n\n Second message"

    def test_get_response_content_empty(self) -> None:
        """Test extracting content when no content available."""
        response = MagicMock()
        response.content = None
        response.messages = []

        result = _get_response_content(response)
        assert result == ""

    def test_get_response_content_prefers_direct_content(self) -> None:
        """Test that direct content is preferred over messages."""
        msg = MagicMock(spec=Message)
        msg.role = "assistant"
        msg.content = "Message content"

        response = MagicMock()
        response.content = "Direct content"
        response.messages = [msg]

        result = _get_response_content(response)
        assert result == "Direct content"


class TestExtractTeamMemberContributions:
    """Tests for format_team_response function."""

    def test_single_agent_response(self) -> None:
        """Test extraction from a single agent response."""
        response = MagicMock(spec=RunResponse)
        response.agent_name = "test_agent"
        response.content = "Agent response"
        response.messages = []

        result = format_team_response(response)
        assert result == ["**test_agent**: Agent response"]

    def test_single_agent_no_name(self) -> None:
        """Test extraction from agent without name."""
        response = MagicMock(spec=RunResponse)
        response.agent_name = None
        response.content = "Agent response"
        response.messages = []

        result = format_team_response(response)
        assert result == ["**Agent**: Agent response"]

    def test_simple_team_response(self) -> None:
        """Test extraction from a simple team with two agents."""
        agent1 = MagicMock(spec=RunResponse)
        agent1.agent_name = "analyzer"
        agent1.content = "Analysis complete"
        agent1.messages = []

        agent2 = MagicMock(spec=RunResponse)
        agent2.agent_name = "writer"
        agent2.content = "Report written"
        agent2.messages = []

        team = MagicMock(spec=TeamRunResponse)
        team.team_name = "Research Team"
        team.content = "Final team output"
        team.member_responses = [agent1, agent2]
        team.messages = []

        result = format_team_response(team)
        expected = [
            "**analyzer**: Analysis complete",
            "**writer**: Report written",
            "\n**Team Consensus**:",
            "Final team output",
        ]
        assert result == expected

    def test_team_without_consensus(self) -> None:
        """Test team that only has member responses, no consensus."""
        agent1 = MagicMock(spec=RunResponse)
        agent1.agent_name = "agent1"
        agent1.content = "Response 1"
        agent1.messages = []

        team = MagicMock(spec=TeamRunResponse)
        team.team_name = "Team"
        team.content = None  # No consensus
        team.member_responses = [agent1]
        team.messages = []

        result = format_team_response(team)
        assert result == ["**agent1**: Response 1", "\n*No team consensus - showing individual responses only*"]

    def test_team_with_only_consensus(self) -> None:
        """Test team with consensus but no member responses."""
        team = MagicMock(spec=TeamRunResponse)
        team.team_name = "Team"
        team.content = "Team consensus only"
        team.member_responses = []
        team.messages = []

        result = format_team_response(team)
        assert result == ["\n**Team Consensus**:", "Team consensus only"]

    def test_nested_teams(self) -> None:
        """Test extraction from nested teams."""
        # Inner team members
        inner_agent1 = MagicMock(spec=RunResponse)
        inner_agent1.agent_name = "researcher"
        inner_agent1.content = "Research done"
        inner_agent1.messages = []

        inner_agent2 = MagicMock(spec=RunResponse)
        inner_agent2.agent_name = "analyst"
        inner_agent2.content = "Analysis done"
        inner_agent2.messages = []

        # Inner team
        inner_team = MagicMock(spec=TeamRunResponse)
        inner_team.team_name = "Research Team"
        inner_team.content = "Research complete"  # This should NOT appear (nested team consensus)
        inner_team.member_responses = [inner_agent1, inner_agent2]
        inner_team.messages = []

        # Outer team member
        outer_agent = MagicMock(spec=RunResponse)
        outer_agent.agent_name = "writer"
        outer_agent.content = "Final report"
        outer_agent.messages = []

        # Outer team
        outer_team = MagicMock(spec=TeamRunResponse)
        outer_team.team_name = "Main Team"
        outer_team.content = "Project complete"
        outer_team.member_responses = [inner_team, outer_agent]
        outer_team.messages = []

        result = format_team_response(outer_team)
        expected = [
            "**Research Team** (Team):",
            "  **researcher**: Research done",
            "  **analyst**: Analysis done",
            "**writer**: Final report",
            "\n**Team Consensus**:",
            "Project complete",
        ]
        assert result == expected

    def test_deeply_nested_teams(self) -> None:
        """Test extraction from deeply nested teams (3 levels)."""
        # Level 3 agent
        deep_agent = MagicMock(spec=RunResponse)
        deep_agent.agent_name = "deep_agent"
        deep_agent.content = "Deep work"
        deep_agent.messages = []

        # Level 3 team
        deep_team = MagicMock(spec=TeamRunResponse)
        deep_team.team_name = "Deep Team"
        deep_team.content = "Deep consensus"
        deep_team.member_responses = [deep_agent]
        deep_team.messages = []

        # Level 2 agent
        mid_agent = MagicMock(spec=RunResponse)
        mid_agent.agent_name = "mid_agent"
        mid_agent.content = "Mid work"
        mid_agent.messages = []

        # Level 2 team
        mid_team = MagicMock(spec=TeamRunResponse)
        mid_team.team_name = "Mid Team"
        mid_team.content = "Mid consensus"
        mid_team.member_responses = [deep_team, mid_agent]
        mid_team.messages = []

        # Level 1 team
        top_team = MagicMock(spec=TeamRunResponse)
        top_team.team_name = "Top Team"
        top_team.content = "Top consensus"
        top_team.member_responses = [mid_team]
        top_team.messages = []

        result = format_team_response(top_team)
        expected = [
            "**Mid Team** (Team):",
            "  **Deep Team** (Team):",
            "    **deep_agent**: Deep work",
            "  **mid_agent**: Mid work",
            "\n**Team Consensus**:",
            "Top consensus",
        ]
        assert result == expected

    def test_team_with_no_name(self) -> None:
        """Test team without a name falls back to default."""
        agent = MagicMock(spec=RunResponse)
        agent.agent_name = "agent"
        agent.content = "Content"
        agent.messages = []

        inner_team = MagicMock(spec=TeamRunResponse)
        inner_team.team_name = None  # No name
        inner_team.content = "Inner consensus"
        inner_team.member_responses = [agent]
        inner_team.messages = []

        outer_team = MagicMock(spec=TeamRunResponse)
        outer_team.team_name = "Main"
        outer_team.content = "Outer consensus"
        outer_team.member_responses = [inner_team]
        outer_team.messages = []

        result = format_team_response(outer_team)
        expected = [
            "**Nested Team** (Team):",  # Default name
            "  **agent**: Content",
            "\n**Team Consensus**:",
            "Outer consensus",
        ]
        assert result == expected

    def test_empty_response(self) -> None:
        """Test handling of completely empty responses."""
        response = MagicMock(spec=RunResponse)
        response.agent_name = "empty_agent"
        response.content = None
        response.messages = []

        result = format_team_response(response)
        assert result == []

    def test_mixed_content_sources(self) -> None:
        """Test team with agents using different content sources."""
        # Agent with direct content
        agent1 = MagicMock(spec=RunResponse)
        agent1.agent_name = "direct_agent"
        agent1.content = "Direct content"
        agent1.messages = []

        # Agent with message content
        msg = MagicMock(spec=Message)
        msg.role = "assistant"
        msg.content = "Message content"

        agent2 = MagicMock(spec=RunResponse)
        agent2.agent_name = "message_agent"
        agent2.content = None
        agent2.messages = [msg]

        team = MagicMock(spec=TeamRunResponse)
        team.team_name = "Mixed Team"
        team.content = "Team output"
        team.member_responses = [agent1, agent2]
        team.messages = []

        result = format_team_response(team)
        expected = [
            "**direct_agent**: Direct content",
            "**message_agent**: Message content",
            "\n**Team Consensus**:",
            "Team output",
        ]
        assert result == expected
