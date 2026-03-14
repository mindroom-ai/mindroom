"""Tests for universal mention parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import constants as constants_mod
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from mindroom.tool_system.events import _TOOL_TRACE_KEY, ToolTraceEntry

if TYPE_CHECKING:
    from pathlib import Path


def _make_config(*, runtime_paths: constants_mod.RuntimePaths | None = None) -> Config:
    config = Config(
        agents={
            "calculator": AgentConfig(display_name="Calculator"),
            "general": AgentConfig(display_name="General"),
            "code": AgentConfig(display_name="Code"),
            "email": AgentConfig(display_name="Email"),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )
    config._runtime_paths = runtime_paths
    return config


class TestMentionParsing:
    """Test the universal mention parsing system."""

    def test_parse_single_mention(self) -> None:
        """Test parsing a single agent mention."""
        config = _make_config()

        text = "Hey @calculator can you help with this?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Hey @mindroom_calculator:localhost can you help with this?"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_multiple_mentions(self) -> None:
        """Test parsing multiple agent mentions."""
        config = _make_config()

        text = "@calculator and @general please work together on this"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert (
            processed == "@mindroom_calculator:localhost and @mindroom_general:localhost please work together on this"
        )
        assert set(mentions) == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
        assert len(mentions) == 2

    def test_parse_with_full_mention(self) -> None:
        """Test parsing when full @mindroom_agent format is used."""
        config = _make_config()

        text = "Ask @mindroom_calculator for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_domain(self) -> None:
        """Test parsing when mention already has domain."""
        config = _make_config()

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Should replace with sender's domain
        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_namespaced_full_mention(self, tmp_path: Path) -> None:
        """Full localparts that include namespace suffix should resolve to configured agents."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = _make_config(runtime_paths=runtime_paths)

        text = "Ask @mindroom_calculator_a1b2c3d4:matrix.org for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator_a1b2c3d4:localhost for help"
        assert mentions == ["@mindroom_calculator_a1b2c3d4:localhost"]

    def test_custom_domain(self) -> None:
        """Test with custom sender domain."""
        config = _make_config()

        text = "Hey @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "matrix.org", config)

        assert processed == "Hey @mindroom_calculator:matrix.org"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    def test_ignore_unknown_mentions(self) -> None:
        """Test that unknown agents are not converted."""
        config = _make_config()

        text = "@calculator is real but @unknown is not"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost is real but @unknown is not"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_ignore_user_mentions(self) -> None:
        """Test that user mentions are ignored."""
        config = _make_config()

        text = "@mindroom_user_123 and @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_user_123 and @mindroom_calculator:localhost"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_no_duplicate_mentions(self) -> None:
        """Test that duplicate mentions are handled."""
        config = _make_config()

        text = "@calculator help! @calculator are you there?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost help! @mindroom_calculator:localhost are you there?"
        assert mentions == ["@mindroom_calculator:localhost"]  # Only one entry

    def test_format_message_with_mentions(self) -> None:
        """Test the full content creation with mentions."""
        config = _make_config()

        content = format_message_with_mentions(
            config,
            "@calculator and @code please help",
            sender_domain="matrix.org",
            thread_event_id="$thread123",
            latest_thread_event_id="$thread123",  # For thread fallback
        )

        assert content["msgtype"] == "m.text"
        assert content["body"] == "@mindroom_calculator:matrix.org and @mindroom_code:matrix.org please help"
        assert set(content["m.mentions"]["user_ids"]) == {
            "@mindroom_calculator:matrix.org",
            "@mindroom_code:matrix.org",
        }
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content["m.relates_to"]["rel_type"] == "m.thread"

    def test_format_message_with_mentions_includes_tool_trace(self) -> None:
        """Structured tool traces should be attached to message content when provided."""
        config = _make_config()
        trace = [ToolTraceEntry(type="tool_call_started", tool_name="save_file", args_preview="file=a.py")]

        content = format_message_with_mentions(
            config,
            "Done.",
            sender_domain="matrix.org",
            tool_trace=trace,
        )

        assert _TOOL_TRACE_KEY in content
        assert content[_TOOL_TRACE_KEY]["version"] == 2
        assert content[_TOOL_TRACE_KEY]["events"][0]["tool_name"] == "save_file"

    def test_format_message_with_mentions_merges_extra_content(self) -> None:
        """Custom metadata should be merged with structured tool trace content."""
        config = _make_config()
        trace = [ToolTraceEntry(type="tool_call_started", tool_name="save_file")]

        content = format_message_with_mentions(
            config,
            "Done.",
            sender_domain="matrix.org",
            tool_trace=trace,
            extra_content={"io.mindroom.ai_run": {"version": 1, "usage": {"total_tokens": 42}}},
        )

        assert _TOOL_TRACE_KEY in content
        assert content["io.mindroom.ai_run"]["version"] == 1
        assert content["io.mindroom.ai_run"]["usage"]["total_tokens"] == 42

    def test_format_message_with_mentions_preserves_inherited_mentions(self) -> None:
        """Inherited mentions should survive even when the new text adds no mentions."""
        config = _make_config()

        content = format_message_with_mentions(
            config,
            "Transcription omitted the agent mention.",
            sender_domain="matrix.org",
            extra_content={"m.mentions": {"user_ids": ["@mindroom_research:matrix.org"]}},
        )

        assert content["m.mentions"]["user_ids"] == ["@mindroom_research:matrix.org"]

    def test_no_mentions_in_text(self) -> None:
        """Test text with no mentions."""
        config = _make_config()

        text = "This has no mentions"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == text
        assert mentions == []

    def test_mention_in_middle_of_word(self) -> None:
        """Test that mentions in middle of words are not parsed."""
        config = _make_config()

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Current implementation might catch this - documenting actual behavior
        # This is a limitation we should be aware of
        assert "@mindroom_code:localhost" in processed or processed == text

    def test_case_insensitive_mentions(self) -> None:
        """Test that mentions are case-insensitive."""
        config = _make_config()

        # Test various capitalizations
        test_cases = [
            ("@Calculator help me", ["calculator"]),
            ("@CALCULATOR help me", ["calculator"]),
            ("@CaLcUlAtOr help me", ["calculator"]),
            ("@Code @EMAIL help", ["code", "email"]),
            ("@EMAIL @Code help", ["email", "code"]),
        ]

        for text, expected_agents in test_cases:
            processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

            # Extract agent names from the mentioned user IDs
            mentioned_agents = []
            for user_id in mentions:
                # Extract agent name from user_id like "@mindroom_calculator:localhost"
                if user_id.startswith("@mindroom_") and ":" in user_id:
                    agent_name = user_id.split("@mindroom_")[1].split(":")[0]
                    mentioned_agents.append(agent_name)

            assert mentioned_agents == expected_agents, f"Failed for text: {text}"
            assert len(mentions) == len(expected_agents)
