"""Tests for universal mention parsing."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    import pytest

from mindroom import constants as constants_mod
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from mindroom.matrix.state import MatrixState
from mindroom.tool_system.events import _TOOL_TRACE_KEY, ToolTraceEntry
from tests.identity_helpers import actual_entity_usernames, persist_entity_accounts

_BOUND_RUNTIME_PATHS: dict[int, constants_mod.RuntimePaths] = {}


def _default_runtime_paths() -> constants_mod.RuntimePaths:
    tmp_path = Path(tempfile.mkdtemp())
    return constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _bind_config(
    runtime_paths: constants_mod.RuntimePaths,
    agents: dict[str, AgentConfig],
    teams: dict[str, TeamConfig] | None = None,
) -> Config:
    config = Config(
        agents=agents,
        teams=teams or {},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
    )
    bound = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    _persist_mentions_accounts(bound, runtime_paths)
    _BOUND_RUNTIME_PATHS[id(bound)] = runtime_paths
    return bound


def _persist_mentions_accounts(
    config: Config,
    runtime_paths: constants_mod.RuntimePaths,
    *,
    usernames: dict[str, str] | None = None,
) -> None:
    persist_entity_accounts(config, runtime_paths, usernames=usernames or actual_entity_usernames(config))


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
    config: Config,
) -> tuple[str, list[str], str]:
    return parse_mentions_in_text(text, config, _runtime_paths_for(config))


def _format_message_with_mentions(config: Config, text: str, **kwargs: object) -> dict[str, object]:
    return format_message_with_mentions(config, _runtime_paths_for(config), text, **kwargs)


class TestMentionParsing:
    """Test the universal mention parsing system."""

    def test_parse_single_mention(self) -> None:
        """Test parsing a single agent mention."""
        config = _make_config(_default_runtime_paths())

        text = "Hey @calculator can you help with this?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Hey @actual_calculator:localhost can you help with this?"
        assert mentions == ["@actual_calculator:localhost"]

    def test_parse_without_at_skips_regex_scanners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plain text without @ cannot contain mentions, so skip regex scanner work."""
        config = _make_config(_default_runtime_paths())

        def fail_scan(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "mention regex scanner should not run without @"
            raise AssertionError(msg)

        monkeypatch.setattr("mindroom.matrix.mentions._scan_explicit_matrix_id_tokens", fail_scan)
        monkeypatch.setattr("mindroom.matrix.mentions._scan_entity_alias_tokens", fail_scan)

        processed, mentions, markdown = _parse_mentions_in_text("plain **markdown** text", config)

        assert processed == "plain **markdown** text"
        assert markdown == "plain **markdown** text"
        assert mentions == []

    def test_parse_multiple_mentions(self) -> None:
        """Test parsing multiple agent mentions."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator and @general please work together on this"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "@actual_calculator:localhost and @actual_general:localhost please work together on this"
        assert set(mentions) == {"@actual_calculator:localhost", "@actual_general:localhost"}
        assert len(mentions) == 2

    def test_parse_with_generated_looking_localpart_resolves_entity_alias(self) -> None:
        """Generated Matrix localparts also work as stable entity aliases."""
        config = _make_config(_default_runtime_paths())

        text = "Ask @mindroom_calculator for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Ask @actual_calculator:localhost for help"
        assert mentions == ["@actual_calculator:localhost"]

    def test_parse_with_generated_looking_full_mxid_stays_literal(self) -> None:
        """Generated-looking full MXIDs are preserved as explicit literal users."""
        config = _make_config(_default_runtime_paths())

        text = "Ask @mindroom_calculator:localhost for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_namespaced_generated_localpart_stays_literal(self, tmp_path: Path) -> None:
        """Generated namespace localparts are not runtime aliases."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = _make_config(runtime_paths)

        text = "Ask @mindroom_calculator_a1b2c3d4:localhost for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Ask @mindroom_calculator_a1b2c3d4:localhost for help"
        assert mentions == ["@mindroom_calculator_a1b2c3d4:localhost"]

    def test_parse_with_unnamespaced_agent_full_mxid_in_namespaced_install(self, tmp_path: Path) -> None:
        """Explicit non-current MXIDs should remain literal in namespaced installs."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = _make_config(runtime_paths)

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Ask @mindroom_calculator:matrix.org for help"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    def test_custom_domain(self) -> None:
        """Configured entity mentions should use the current configured Matrix domain."""
        config = _make_config(_default_runtime_paths())

        text = "Hey @calculator"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "Hey @actual_calculator:localhost"
        assert mentions == ["@actual_calculator:localhost"]

    def test_ignore_unknown_mentions(self) -> None:
        """Test that unknown agents are not converted."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator is real but @unknown is not"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "@actual_calculator:localhost is real but @unknown is not"
        assert mentions == ["@actual_calculator:localhost"]

    def test_ignore_user_mentions(self) -> None:
        """Test that user mentions are ignored."""
        config = _make_config(_default_runtime_paths())

        text = "@mindroom_user_123 and @calculator"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "@mindroom_user_123 and @actual_calculator:localhost"
        assert mentions == ["@actual_calculator:localhost"]

    def test_user_like_entity_name_resolves_as_exact_alias(self) -> None:
        """User-style aliases resolve when the configured entity alias is exact."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "user_123": AgentConfig(display_name="UserLikeAgent"),
                "calculator": AgentConfig(display_name="Calculator"),
            },
        )

        processed, mentions, _markdown = _parse_mentions_in_text(
            "@user_123 and @calculator",
            config,
        )

        assert processed == "@actual_user_123:localhost and @actual_calculator:localhost"
        assert mentions == ["@actual_user_123:localhost", "@actual_calculator:localhost"]

    def test_no_duplicate_mentions(self) -> None:
        """Test that duplicate mentions are handled."""
        config = _make_config(_default_runtime_paths())

        text = "@calculator help! @calculator are you there?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == "@actual_calculator:localhost help! @actual_calculator:localhost are you there?"
        assert mentions == ["@actual_calculator:localhost"]  # Only one entry

    def test_format_message_with_mentions(self) -> None:
        """Test the full content creation with mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@calculator and @code please help",
            thread_event_id="$thread123",
            latest_thread_event_id="$thread123",  # For thread fallback
        )

        assert content["msgtype"] == "m.text"
        assert content["body"] == "@actual_calculator:localhost and @actual_code:localhost please help"
        assert set(content["m.mentions"]["user_ids"]) == {
            "@actual_calculator:localhost",
            "@actual_code:localhost",
        }
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content["m.relates_to"]["rel_type"] == "m.thread"

    def test_format_message_uses_persisted_actual_id(self) -> None:
        """Alias mentions should use persisted actual Matrix IDs."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@calculator please help",
        )

        assert content["body"] == "@actual_calculator:localhost please help"
        assert content["m.mentions"]["user_ids"] == ["@actual_calculator:localhost"]

    def test_format_plain_text_does_not_require_prepared_entity_accounts(self) -> None:
        """Plain text without mention tokens does not need runtime entity identity."""
        runtime_paths = _default_runtime_paths()
        config = Config(
            agents={"calculator": AgentConfig(display_name="Calculator")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )
        config = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)

        content = format_message_with_mentions(config, runtime_paths, "No mentions here.")

        assert content["body"] == "No mentions here."
        assert "m.mentions" not in content

    def test_format_message_with_team_mention(self) -> None:
        """Team aliases should format as Matrix mentions for router handoffs."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {"calculator": AgentConfig(display_name="Calculator")},
            {
                "ops": TeamConfig(
                    display_name="Ops Team",
                    role="Operations escalation team",
                    agents=["calculator"],
                ),
            },
        )

        content = _format_message_with_mentions(
            config,
            "@ops could you help with this?",
        )

        assert content["body"] == "@actual_ops:localhost could you help with this?"
        assert content["m.mentions"]["user_ids"] == ["@actual_ops:localhost"]
        assert (
            content["formatted_body"] == '<p><a href="https://matrix.to/#/@actual_ops:localhost">@Ops Team</a> '
            "could you help with this?</p>\n"
        )

    def test_format_message_with_mentions_uses_persisted_current_username_drift(self, tmp_path: Path) -> None:
        """Mention formatting should target the live persisted Matrix account ID."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "actual_general_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@general could you help with this?",
        )

        assert content["body"] == "@actual_general_live:localhost could you help with this?"
        assert content["m.mentions"]["user_ids"] == ["@actual_general_live:localhost"]

    def test_format_message_resolves_generated_alias_after_username_drift(self, tmp_path: Path) -> None:
        """Generated aliases target the current account even after username drift."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "actual_general_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@mindroom_general could you help with this?",
        )

        assert content["body"] == "@actual_general_live:localhost could you help with this?"
        assert content["m.mentions"]["user_ids"] == ["@actual_general_live:localhost"]

    def test_format_message_rejects_stale_generated_full_mxid_after_drift(self, tmp_path: Path) -> None:
        """After username drift, stale generated full MXIDs should stay literal."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "actual_general_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@mindroom_general:localhost could you help with this?",
        )

        assert content["body"] == "@mindroom_general:localhost could you help with this?"
        assert content["m.mentions"]["user_ids"] == ["@mindroom_general:localhost"]

    def test_format_message_rejects_stale_generated_router_full_mxid_after_drift(self, tmp_path: Path) -> None:
        """After router username drift, stale generated full MXIDs should stay literal."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_router", "actual_router_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@mindroom_router:localhost could you route this?",
        )

        assert content["body"] == "@mindroom_router:localhost could you route this?"
        assert content["m.mentions"]["user_ids"] == ["@mindroom_router:localhost"]

    def test_format_message_keeps_cross_domain_generated_full_mxid_literal_after_drift(
        self,
        tmp_path: Path,
    ) -> None:
        """Remote explicit MXIDs should stay literal even when their localpart looks stale locally."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "actual_general_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@mindroom_general:matrix.org could you help with this?",
        )

        assert content["body"] == "@mindroom_general:matrix.org could you help with this?"
        assert content["m.mentions"]["user_ids"] == ["@mindroom_general:matrix.org"]

    def test_format_message_rejects_bare_persisted_current_localpart(self, tmp_path: Path) -> None:
        """Bare actual localparts are not runtime aliases."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _make_config(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "actual_general_live", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        content = _format_message_with_mentions(
            config,
            "@actual_general_live could you help with this?",
        )

        assert content["body"] == "@actual_general_live could you help with this?"
        assert "m.mentions" not in content

    def test_tool_marker_followed_by_thematic_break_renders_as_paragraph_hr_heading_via_format_message_with_mentions(
        self,
    ) -> None:
        """Visible tool markers should stay paragraphs through the Matrix message formatter."""
        config = _make_config(_default_runtime_paths())
        text = "Some intro text.\n\n🔧 `run_shell_command` [1]\n---\n\n## Heading after"

        content = _format_message_with_mentions(config, text)

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
            extra_content={"m.mentions": {"user_ids": ["@mindroom_research:matrix.org"]}},
        )

        assert content["m.mentions"]["user_ids"] == ["@mindroom_research:matrix.org"]

    def test_format_message_with_full_matrix_user_id_creates_clickable_mention(self) -> None:
        """Non-agent full Matrix IDs should be rendered as clickable mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Yes, @bas.nijholt:chat-mindroom.example.com -- noted.",
        )

        assert content["body"] == "Yes, @bas.nijholt:chat-mindroom.example.com -- noted."
        assert content["m.mentions"]["user_ids"] == ["@bas.nijholt:chat-mindroom.example.com"]
        assert (
            content["formatted_body"] == '<p>Yes, <a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a> -- noted.</p>\n"
        )

    def test_format_message_with_backticked_full_matrix_user_id_creates_clickable_mention(self) -> None:
        """Accidentally backticked MXIDs should become normal clickable mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Mind ID is `@mindroom_mind_5ckzneqq:mindroom.chat`.",
        )

        assert content["body"] == "Mind ID is @mindroom_mind_5ckzneqq:mindroom.chat."
        assert content["m.mentions"]["user_ids"] == ["@mindroom_mind_5ckzneqq:mindroom.chat"]
        assert (
            content["formatted_body"]
            == '<p>Mind ID is <a href="https://matrix.to/#/@mindroom_mind_5ckzneqq:mindroom.chat">'
            "@mindroom_mind_5ckzneqq:mindroom.chat</a>.</p>\n"
        )

    def test_format_message_with_full_matrix_user_id_excludes_sentence_period(self) -> None:
        """Sentence punctuation should not become part of a full Matrix ID mention."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Ping @alice:matrix.org.",
        )

        assert content["body"] == "Ping @alice:matrix.org."
        assert content["m.mentions"]["user_ids"] == ["@alice:matrix.org"]
        assert (
            content["formatted_body"]
            == '<p>Ping <a href="https://matrix.to/#/@alice:matrix.org">@alice:matrix.org</a>.</p>\n'
        )

    def test_format_message_with_agent_and_full_matrix_user_id_preserves_both_mentions(self) -> None:
        """Agent mentions and explicit full Matrix user IDs should coexist cleanly."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@calculator please follow up with @bas.nijholt:chat-mindroom.example.com",
        )

        assert content["body"] == (
            "@actual_calculator:localhost please follow up with @bas.nijholt:chat-mindroom.example.com"
        )
        assert content["m.mentions"]["user_ids"] == [
            "@actual_calculator:localhost",
            "@bas.nijholt:chat-mindroom.example.com",
        ]
        assert (
            content["formatted_body"]
            == '<p><a href="https://matrix.to/#/@actual_calculator:localhost">@Calculator</a> '
            'please follow up with <a href="https://matrix.to/#/@bas.nijholt:chat-mindroom.example.com">'
            "@bas.nijholt:chat-mindroom.example.com</a></p>\n"
        )

    def test_format_message_with_duplicate_full_matrix_user_ids_deduplicates_mentions(self) -> None:
        """Repeated full Matrix user IDs should appear once in m.mentions."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@bas.nijholt:chat-mindroom.example.com and again @bas.nijholt:chat-mindroom.example.com",
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
        )

        assert content["body"] == "Please ask @code:matrix.org to review this."
        assert content["m.mentions"]["user_ids"] == ["@code:matrix.org"]
        assert (
            content["formatted_body"]
            == '<p>Please ask <a href="https://matrix.to/#/@code:matrix.org">@code:matrix.org</a> to review this.</p>\n'
        )

    def test_format_message_with_current_managed_full_matrix_id_uses_entity_display(self) -> None:
        """Full MXIDs only resolve as local entities when they match the persisted managed ID exactly."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Please ask @actual_code:localhost to review this.",
        )

        assert content["body"] == "Please ask @actual_code:localhost to review this."
        assert content["m.mentions"]["user_ids"] == ["@actual_code:localhost"]
        assert (
            content["formatted_body"]
            == '<p>Please ask <a href="https://matrix.to/#/@actual_code:localhost">@Code</a> to review this.</p>\n'
        )

    def test_format_message_with_remote_mindroom_matrix_id_keeps_explicit_user(self) -> None:
        """Remote full MindRoom-like MXIDs should not be retargeted to local entities."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "@mindroom_code:remote.example please look",
        )

        assert content["body"] == "@mindroom_code:remote.example please look"
        assert content["m.mentions"]["user_ids"] == ["@mindroom_code:remote.example"]
        assert (
            content["formatted_body"]
            == '<p><a href="https://matrix.to/#/@mindroom_code:remote.example">@mindroom_code:remote.example</a> '
            "please look</p>\n"
        )

    def test_format_message_with_uppercase_matrix_user_id_does_not_create_mention(self) -> None:
        """Non-compliant uppercase MXIDs should remain plain text."""
        config = _make_config(_default_runtime_paths())

        content = _format_message_with_mentions(
            config,
            "Please ask @Code:matrix.org to review this.",
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
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == text
        assert mentions == []

    def test_mention_in_middle_of_word(self) -> None:
        """Test that mentions in middle of words are not parsed."""
        config = _make_config(_default_runtime_paths())

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, _mentions, _markdown = _parse_mentions_in_text(text, config)

        assert processed == text

    def test_agent_name_starts_with_mindroom_prefix(self) -> None:
        """Agent config keys starting with 'mindroom_' resolve only as exact aliases."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "calculator": AgentConfig(display_name="Calculator"),
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )

        text = "@mindroom_dev can you look at this?"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert mentions == ["@actual_mindroom_dev:localhost"]
        assert processed == "@actual_mindroom_dev:localhost can you look at this?"

    def test_generated_localpart_for_prefixed_agent_key_resolves(self) -> None:
        """Generated aliases also work for entity keys that start with mindroom_."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )

        text = "@mindroom_mindroom_dev help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert mentions == ["@actual_mindroom_dev:localhost"]
        assert processed == "@actual_mindroom_dev:localhost help"

    def test_prefixed_agent_key_alias_survives_persisted_username_drift(self, tmp_path: Path) -> None:
        """Configured entity keys remain stable mention aliases after username drift."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        config = _bind_config(
            runtime_paths,
            {
                "mindroom_dev": AgentConfig(display_name="DevAgent"),
            },
        )
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_mindroom_dev", "actual_mindroom_dev_oldns", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)

        text = "@mindroom_dev help"
        processed, mentions, _markdown = _parse_mentions_in_text(text, config)

        assert mentions == ["@actual_mindroom_dev_oldns:localhost"]
        assert processed == "@actual_mindroom_dev_oldns:localhost help"

    def test_namespaced_generated_prefixed_agent_name_does_not_resolve(self, tmp_path: Path) -> None:
        """Namespaced generated localparts do not resolve to configured aliases."""
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
            config,
        )

        assert mentions == []
        assert processed == "@mindroom_dev_a1b2c3d4 help"

    def test_generated_looking_alias_resolves_when_configured_exactly(self) -> None:
        """@mindroom_calculator resolves only because that exact alias is configured."""
        runtime_paths = _default_runtime_paths()
        config = _bind_config(
            runtime_paths,
            {
                "calculator": AgentConfig(display_name="Calculator"),
                "mindroom_calculator": AgentConfig(display_name="PrefixedCalculator"),
            },
        )

        processed, mentions, _markdown = _parse_mentions_in_text("@mindroom_calculator help", config)

        assert mentions == ["@actual_mindroom_calculator:localhost"]
        assert processed == "@actual_mindroom_calculator:localhost help"

    def test_uppercase_generated_looking_mentions_resolve_case_insensitively(self) -> None:
        """Generated aliases resolve with the same case-insensitive matching as direct aliases."""
        config = _make_config(_default_runtime_paths())

        processed, mentions, _markdown = _parse_mentions_in_text("@MINDROOM_calculator help", config)

        assert mentions == ["@actual_calculator:localhost"]
        assert processed == "@actual_calculator:localhost help"

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
            _processed, mentions, _markdown = _parse_mentions_in_text(text, config)

            # Extract agent names from the mentioned user IDs
            mentioned_agents = []
            for user_id in mentions:
                if user_id.startswith("@actual_") and ":" in user_id:
                    agent_name = user_id.split("@actual_")[1].split(":")[0]
                    mentioned_agents.append(agent_name)

            assert mentioned_agents == expected_agents, f"Failed for text: {text}"
            assert len(mentions) == len(expected_agents)
