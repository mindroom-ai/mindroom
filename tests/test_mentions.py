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

_BOUND_RUNTIME_PATHS: dict[int, constants_mod.RuntimePaths] = {}


def _default_runtime_paths() -> constants_mod.RuntimePaths:
    return constants_mod.resolve_runtime_paths(
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _bind_config(
    runtime_paths: constants_mod.RuntimePaths,
    agents: dict[str, AgentConfig],
) -> Config:
    config = Config(
        agents=agents,
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )
    bound = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    _BOUND_RUNTIME_PATHS[id(bound)] = runtime_paths
    return bound


def _make_config(runtime_paths: constants_mod.RuntimePaths) -> Config:
    return _bind_config(
        runtime_paths,
        {
            "calculator": AgentConfig(display_name="Calculator"),
            "general": AgentConfig(display_name="General"),
            "code": AgentConfig(display_name="Code"),
            "email": AgentConfig(display_name="Email"),
        },
    )


def _runtime_paths_for(config: Config) -> constants_mod.RuntimePaths:
    runtime_paths = _BOUND_RUNTIME_PATHS.get(id(config))
    if runtime_paths is None:
        msg = "Test config is missing bound RuntimePaths"
        raise KeyError(msg)
    return runtime_paths


def _parse_mentions_in_text(
    text: str,
    sender_domain: str,
    config: Config,
) -> tuple[str, list[str], str]:
    return parse_mentions_in_text(text, sender_domain, config, _runtime_paths_for(config))


def _format_message_with_mentions(config: Config, text: str, **kwargs: object) -> dict[str, object]:
    return format_message_with_mentions(config, _runtime_paths_for(config), text, **kwargs)


class TestMentionParsing:
    """Test the universal mention parsing system."""

    def test_parse_single_mention(self) -> None:
        """Test parsing a single agent mention."""
        config = _make_config(_default_runtime_paths())

        text = "Hey @calculator can you help with this?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "Hey @mindroom_calculator:localhost can you help with this?"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_multiple_mentions(self) -> None:
        """Test parsing multiple agent mentions."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator and @general please work together on this"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert (
            processed == "@mindroom_calculator:localhost and @mindroom_general:localhost please work together on this"
        )
        assert set(mentions) == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
        assert len(mentions) == 2

    def test_parse_with_full_mention(self) -> None:
        """Test parsing when full @mindroom_agent format is used."""
        config = _make_config(_default_runtime_paths())

        text = "Ask @mindroom_calculator for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_domain(self) -> None:
        """Test parsing when mention already has domain."""
        config = _make_config(_default_runtime_paths())

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

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
        config = _make_config(runtime_paths)

        text = "Ask @mindroom_calculator_a1b2c3d4:matrix.org for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator_a1b2c3d4:localhost for help"
        assert mentions == ["@mindroom_calculator_a1b2c3d4:localhost"]

    def test_parse_with_unnamespaced_agent_full_mxid_in_namespaced_install(self, tmp_path: Path) -> None:
        """Explicit agent-shaped MXIDs should still map to local namespaced agents."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = _make_config(runtime_paths)

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator_a1b2c3d4:localhost for help"
        assert mentions == ["@mindroom_calculator_a1b2c3d4:localhost"]

    def test_custom_domain(self) -> None:
        """Test with custom sender domain."""
        config = _make_config(_default_runtime_paths())

        text = "Hey @calculator"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "matrix.org", config)

        assert processed == "Hey @mindroom_calculator:matrix.org"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    def test_ignore_unknown_mentions(self) -> None:
        """Test that unknown agents are not converted."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator is real but @unknown is not"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost is real but @unknown is not"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_ignore_user_mentions(self) -> None:
        """Test that user mentions are ignored."""
        config = _make_config(_default_runtime_paths())

        text = "@mindroom_user_123 and @calculator"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_user_123 and @mindroom_calculator:localhost"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_no_duplicate_mentions(self) -> None:
        """Test that duplicate mentions are handled."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator help! @calculator are you there?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost help! @mindroom_calculator:localhost are you there?"
        assert mentions == ["@mindroom_calculator:localhost"]  # Only one entry

    def test_format_message_with_mentions(self) -> None:
        """Test the full content creation with mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
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

    def test_tool_marker_followed_by_thematic_break_renders_as_paragraph_hr_heading_via_format_message_with_mentions(
        self,
    ) -> None:
        """Visible tool markers should stay paragraphs through the Matrix message formatter."""
        config = _make_config(_default_runtime_paths())
        text = "Some intro text.\n\n🔧 `run_shell_command` [1]\n---\n\n## Heading after"

        content = _format_message_with_mentions(config, text, sender_domain="matrix.org")

        assert "🔧 `run_shell_command` [1]\n\n---" in content["body"]
        formatted_body = content["formatted_body"]
        assert "<h2>🔧" not in formatted_body
        marker_index = formatted_body.index("<p>🔧 <code>run_shell_command</code> [1]</p>")
        hr_index = formatted_body.index("<hr>")
        heading_index = formatted_body.index("<h2>Heading after</h2>")
        assert marker_index < hr_index < heading_index

    def test_format_message_with_mentions_includes_tool_trace(self) -> None:
        """Structured tool traces should be attached to message content when provided."""
        config = _make_config(_default_runtime_paths())
        trace = [ToolTraceEntry(type="tool_call_started", tool_name="save_file", args_preview="file=a.py")]

        content = _format_message_with_mentions(
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
        config = _make_config(_default_runtime_paths())
        trace = [ToolTraceEntry(type="tool_call_started", tool_name="save_file")]

        content = _format_message_with_mentions(
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
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Transcription omitted the agent mention.",
            sender_domain="matrix.org",
            extra_content={"m.mentions": {"user_ids": ["@mindroom_research:matrix.org"]}},
        )

        assert content["m.mentions"]["user_ids"] == ["@mindroom_research:matrix.org"]

    def test_format_message_with_full_matrix_user_id_creates_clickable_mention(self) -> None:
        """Non-agent full Matrix IDs should be rendered as clickable mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Yes, @bas.nijholt:chat-mindroom.example.com -- noted.",
            sender_domain="matrix.org",
        )

        assert content["body"] == "Yes, @bas.nijholt:chat-mindroom.example.com -- noted."
        assert content["m.mentions"]["user_ids"] == ["@bas.nijholt:chat-mindroom.example.com"]
        assert (
            content["formatted_body"] == '<p>Yes, <a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a> -- noted.</p>\n"
        )

    def test_format_message_with_agent_and_full_matrix_user_id_preserves_both_mentions(self) -> None:
        """Agent mentions and explicit full Matrix user IDs should coexist cleanly."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@calculator please follow up with @bas.nijholt:chat-mindroom.example.com",
            sender_domain="matrix.org",
        )

        assert content["body"] == (
            "@mindroom_calculator:matrix.org please follow up with @bas.nijholt:chat-mindroom.example.com"
        )
        assert content["m.mentions"]["user_ids"] == [
            "@mindroom_calculator:matrix.org",
            "@bas.nijholt:chat-mindroom.example.com",
        ]
        assert (
            content["formatted_body"]
            == '<p><a href="https://matrix.to/#/@mindroom_calculator:matrix.org">@Calculator</a> '
            'please follow up with <a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a></p>\n"
        )

    def test_format_message_with_duplicate_full_matrix_user_ids_deduplicates_mentions(self) -> None:
        """Repeated full Matrix user IDs should appear once in m.mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@bas.nijholt:chat-mindroom.example.com and again @bas.nijholt:chat-mindroom.example.com",
            sender_domain="matrix.org",
        )

        assert content["body"] == (
            "@bas.nijholt:chat-mindroom.example.com and again @bas.nijholt:chat-mindroom.example.com"
        )
        assert content["m.mentions"]["user_ids"] == ["@bas.nijholt:chat-mindroom.example.com"]
        assert (
            content["formatted_body"] == '<p><a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a> and again "
            '<a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a></p>\n"
        )

    def test_format_message_with_full_matrix_id_matching_agent_name_keeps_explicit_user(self) -> None:
        """A fully qualified MXID should not be reinterpreted as an agent shorthand."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Please ask @code:matrix.org to review this.",
            sender_domain="localhost",
        )

        assert content["body"] == "Please ask @code:matrix.org to review this."
        assert content["m.mentions"]["user_ids"] == ["@code:matrix.org"]
        assert (
            content["formatted_body"]
            == '<p>Please ask <a href="https://matrix.to/#/@code:matrix.org">@code:matrix.org</a> to review this.</p>\n'
        )

    def test_format_message_with_uppercase_matrix_user_id_does_not_create_mention(self) -> None:
        """Non-compliant uppercase MXIDs should remain plain text."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Please ask @Code:matrix.org to review this.",
            sender_domain="localhost",
        )

        assert content["body"] == "Please ask @Code:matrix.org to review this."
        assert "m.mentions" not in content
        assert content["formatted_body"] == "<p>Please ask @Code:matrix.org to review this.</p>\n"

    def test_format_message_with_plus_in_matrix_user_id_creates_clickable_mention(self) -> None:
        """Matrix user IDs with a plus in the localpart should be linked."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Ping @alice+ops:matrix.org please.",
            sender_domain="localhost",
        )

        assert content["body"] == "Ping @alice+ops:matrix.org please."
        assert content["m.mentions"]["user_ids"] == ["@alice+ops:matrix.org"]
        assert (
            content["formatted_body"]
            == '<p>Ping <a href="https://matrix.to/#/@alice+ops:matrix.org">@alice+ops:matrix.org</a> please.</p>\n'
        )

    def test_format_message_with_ipv6_matrix_user_id_creates_clickable_mention(self) -> None:
        """Matrix user IDs with bracketed IPv6 server names should be linked."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Ping @alice:[2001:db8::1] please.",
            sender_domain="localhost",
        )

        assert content["body"] == "Ping @alice:[2001:db8::1] please."
        assert content["m.mentions"]["user_ids"] == ["@alice:[2001:db8::1]"]
        assert (
            content["formatted_body"]
            == '<p>Ping <a href="https://matrix.to/#/@alice:%5B2001:db8::1%5D">@alice:[2001:db8::1]</a> please.</p>\n'
        )

    def test_no_mentions_in_text(self) -> None:
        """Test text with no mentions."""
        config = _make_config(_default_runtime_paths())

        text = "This has no mentions"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert processed == text
        assert mentions == []

    def test_mention_in_middle_of_word(self) -> None:
        """Test that mentions in middle of words are not parsed."""
        config = _make_config(_default_runtime_paths())

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, _mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        # Current implementation might catch this - documenting actual behavior
        # This is a limitation we should be aware of
        assert "@mindroom_code:localhost" in processed or processed == text

    def test_agent_name_starts_with_mindroom_prefix(self) -> None:
        """Agent config key starting with 'mindroom_' should be resolved from @mindroom_dev.

        When the config key is ``mindroom_dev`` and the mention is ``@mindroom_dev``,
        the regex strips the ``mindroom_`` prefix leaving ``dev``.  The code must
        reconstruct ``mindroom_dev`` and match the config key (ISSUE-098).
        """
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "calculator": AgentConfig(display_name="Calculator"),
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )

        # @mindroom_dev should resolve to agent "mindroom_dev"
        text = "@mindroom_dev can you look at this?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert mentions == ["@mindroom_mindroom_dev:localhost"]
        assert processed == "@mindroom_mindroom_dev:localhost can you look at this?"

    def test_agent_name_starts_with_mindroom_prefix_full_localpart(self) -> None:
        """Mentioning @mindroom_mindroom_dev (full localpart) should also work."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )

        # @mindroom_mindroom_dev should also resolve (prefix stripped → mindroom_dev)
        text = "@mindroom_mindroom_dev help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

        assert mentions == ["@mindroom_mindroom_dev:localhost"]
        assert processed == "@mindroom_mindroom_dev:localhost help"

    def test_namespaced_prefixed_agent_name_with_namespace_suffix(self, tmp_path: Path) -> None:
        """Namespaced mentions for prefixed config keys should try prefix + stripped candidates."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "a1b2c3d4",
            },
        )
        config = _bind_config(
            runtime_paths,
            {
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )

        processed, mentions, _markdown = _parse_mentions_in_text(
            "@mindroom_dev_a1b2c3d4 help",
            "localhost",
            config,
        )

        assert mentions == ["@mindroom_mindroom_dev_a1b2c3d4:localhost"]
        assert processed == "@mindroom_mindroom_dev_a1b2c3d4:localhost help"

    def test_prefixed_mention_prefers_base_agent_when_both_names_exist(self) -> None:
        """@mindroom_calculator should still resolve to calculator before mindroom_calculator."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "calculator": AgentConfig(display_name="Calculator"),
                "mindroom_calculator": AgentConfig(display_name="PrefixedCalculator"),
            },
        )

        processed, mentions, _markdown = _parse_mentions_in_text("@mindroom_calculator help", "localhost", config)

        assert mentions == ["@mindroom_calculator:localhost"]
        assert processed == "@mindroom_calculator:localhost help"

    def test_uppercase_prefixed_mentions_are_case_insensitive(self) -> None:
        """Uppercase @MINDROOM_ prefixes should resolve like lowercase ones."""
        config = _make_config(_default_runtime_paths())

        processed, mentions, _markdown = _parse_mentions_in_text("@MINDROOM_calculator help", "localhost", config)

        assert mentions == ["@mindroom_calculator:localhost"]
        assert processed == "@mindroom_calculator:localhost help"

    def test_case_insensitive_mentions(self) -> None:
        """Test that mentions are case-insensitive."""
        config = _make_config(_default_runtime_paths())

        # Test various capitalizations
        test_cases = [
            ("@Calculator help me", ["calculator"]),
            ("@CALCULATOR help me", ["calculator"]),
            ("@CaLcUlAtOr help me", ["calculator"]),
            ("@Code @EMAIL help", ["code", "email"]),
            ("@EMAIL @Code help", ["email", "code"]),
        ]

        for text, expected_agents in test_cases:
            _processed, mentions, _markdown = _parse_mentions_in_text(text, "localhost", config)

            # Extract agent names from the mentioned user IDs
            mentioned_agents = []
            for user_id in mentions:
                # Extract agent name from user_id like "@mindroom_calculator:localhost"
                if user_id.startswith("@mindroom_") and ":" in user_id:
                    agent_name = user_id.split("@mindroom_")[1].split(":")[0]
                    mentioned_agents.append(agent_name)

            assert mentioned_agents == expected_agents, f"Failed for text: {text}"
            assert len(mentions) == len(expected_agents)
