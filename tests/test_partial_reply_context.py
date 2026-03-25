"""Tests for partial reply context preservation (ISSUE-016)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mindroom.ai import (
    _build_prompt_with_unseen,
    _clean_partial_reply_body,
    _get_unseen_messages,
    _is_agent_partial_reply,
)
from mindroom.config.main import Config
from mindroom.streaming import (
    _STREAM_ERROR_RESPONSE_NOTE,
    CANCELLED_RESPONSE_NOTE,
    IN_PROGRESS_MARKER,
    PROGRESS_PLACEHOLDER,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _make_config() -> Config:
    """Return a minimal runtime-bound config for partial-reply tests."""
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper", "role": "test"}},
            "models": {"default": {"provider": "openai", "id": "gpt-4"}},
        },
    )
    return bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))


# -- _is_agent_partial_reply --------------------------------------------------


class TestIsAgentPartialReply:
    """Cover partial-reply detection markers."""

    def test_in_progress_plain(self) -> None:
        """Treat the in-progress marker as a partial reply."""
        assert _is_agent_partial_reply(f"Some text{IN_PROGRESS_MARKER}") is True

    def test_in_progress_with_dots(self) -> None:
        """Treat marker variants with trailing dots as partial replies."""
        assert _is_agent_partial_reply(f"Some text{IN_PROGRESS_MARKER}.") is True
        assert _is_agent_partial_reply(f"Some text{IN_PROGRESS_MARKER}..") is True

    def test_cancelled(self) -> None:
        """Treat cancelled responses as partial replies."""
        assert _is_agent_partial_reply(f"Partial answer\n\n{CANCELLED_RESPONSE_NOTE}") is True

    def test_error_interrupted(self) -> None:
        """Treat interrupted stream error notes as partial replies."""
        error_note = f"{_STREAM_ERROR_RESPONSE_NOTE}: connection reset]**"
        assert _is_agent_partial_reply(f"Partial\n\n{error_note}") is True

    def test_completed_message(self) -> None:
        """Ignore normal completed responses."""
        assert _is_agent_partial_reply("This is a completed response.") is False

    def test_empty(self) -> None:
        """Ignore empty or missing bodies."""
        assert _is_agent_partial_reply("") is False
        assert _is_agent_partial_reply(None) is False


# -- _clean_partial_reply_body -------------------------------------------------


class TestCleanPartialReplyBody:
    """Cover cleanup of partial-reply bodies."""

    def test_strips_in_progress_marker(self) -> None:
        """Strip the in-progress marker from content."""
        result = _clean_partial_reply_body(f"Hello world{IN_PROGRESS_MARKER}")
        assert result == "Hello world"

    def test_strips_in_progress_marker_with_dots(self) -> None:
        """Strip marker variants with trailing dots."""
        result = _clean_partial_reply_body(f"Hello world{IN_PROGRESS_MARKER}..")
        assert result == "Hello world"

    def test_strips_cancelled_note(self) -> None:
        """Strip the cancelled note from partial output."""
        result = _clean_partial_reply_body(f"Partial answer\n\n{CANCELLED_RESPONSE_NOTE}")
        assert result == "Partial answer"

    def test_placeholder_only(self) -> None:
        """Drop placeholder-only content."""
        # "Thinking..." placeholder with marker → excluded (no real content)
        result = _clean_partial_reply_body(f"{PROGRESS_PLACEHOLDER}{IN_PROGRESS_MARKER}")
        assert result == ""

    def test_placeholder_with_marker_format(self) -> None:
        """Drop the canonical placeholder-plus-marker format."""
        # Real placeholder format: "Thinking... ⋯"
        result = _clean_partial_reply_body(f"{PROGRESS_PLACEHOLDER}{IN_PROGRESS_MARKER}")
        assert result == ""

    def test_marker_only(self) -> None:
        """Drop bare progress markers."""
        result = _clean_partial_reply_body(f"{IN_PROGRESS_MARKER}")
        assert result == ""

    def test_cancelled_note_only(self) -> None:
        """Drop bare cancelled notes."""
        result = _clean_partial_reply_body(CANCELLED_RESPONSE_NOTE)
        assert result == ""

    def test_preserves_thinking_in_real_content(self) -> None:
        """Preserve meaningful text that happens to start with 'Thinking...'."""
        # "Thinking... about options ⋯" should keep "Thinking... about options"
        result = _clean_partial_reply_body(f"Thinking... about options{IN_PROGRESS_MARKER}")
        assert result == "Thinking... about options"

    def test_preserves_thinking_mid_text(self) -> None:
        """Preserve intermediate 'Thinking...' text inside real content."""
        result = _clean_partial_reply_body(f"Before\nThinking...\nafter{IN_PROGRESS_MARKER}")
        assert result == "Before\nThinking...\nafter"

    def test_strips_error_note(self) -> None:
        """Strip interrupted stream error notes from partial output."""
        error_note = f"{_STREAM_ERROR_RESPONSE_NOTE}: some error]**"
        result = _clean_partial_reply_body(f"Partial output\n\n{error_note}")
        assert result == "Partial output"

    def test_error_note_only(self) -> None:
        """Drop bare interrupted stream error notes."""
        error_note = f"{_STREAM_ERROR_RESPONSE_NOTE}. Please retry.]**"
        result = _clean_partial_reply_body(error_note)
        assert result == ""

    def test_truncates_long_content(self) -> None:
        """Truncate very long partial content after cleanup."""
        long_text = "x" * 5000 + f"{IN_PROGRESS_MARKER}"
        result = _clean_partial_reply_body(long_text)
        assert result.startswith("[... earlier content truncated ...]")
        assert len(result) < 5000 + 50  # truncated prefix + 4000 chars


# -- _get_unseen_messages with partial replies ---------------------------------


class TestGetUnseenMessagesPartialReplies:
    """Cover unseen-message extraction for partial replies."""

    def test_includes_in_progress_agent_message(self) -> None:
        """Include partial in-progress agent replies as unseen context."""
        config = _make_config()
        rp = runtime_paths_for(config)
        agent_id = config.get_ids(rp)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": "@user:localhost", "body": "Hello"},
            {"event_id": "e2", "sender": agent_id, "body": f"Partial reply{IN_PROGRESS_MARKER}"},
            {"event_id": "e3", "sender": "@user:localhost", "body": "New question"},
        ]

        unseen, has_partial = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            rp,
            seen_event_ids={"e1"},
            current_event_id="e3",
        )

        assert has_partial is True
        assert len(unseen) == 1
        assert unseen[0]["body"] == "Partial reply"
        assert unseen[0]["event_id"] == "e2"

    def test_includes_cancelled_agent_message(self) -> None:
        """Include cancelled agent replies as unseen partial context."""
        config = _make_config()
        rp = runtime_paths_for(config)
        agent_id = config.get_ids(rp)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": "@user:localhost", "body": "Hello"},
            {"event_id": "e2", "sender": agent_id, "body": f"Partial\n\n{CANCELLED_RESPONSE_NOTE}"},
            {"event_id": "e3", "sender": "@user:localhost", "body": "Try again"},
        ]

        unseen, has_partial = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            rp,
            seen_event_ids={"e1"},
            current_event_id="e3",
        )

        assert has_partial is True
        assert len(unseen) == 1
        assert unseen[0]["body"] == "Partial"

    def test_excludes_completed_agent_message(self) -> None:
        """Exclude completed agent replies from partial context."""
        config = _make_config()
        rp = runtime_paths_for(config)
        agent_id = config.get_ids(rp)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": "@user:localhost", "body": "Hello"},
            {"event_id": "e2", "sender": agent_id, "body": "Complete response."},
            {"event_id": "e3", "sender": "@user:localhost", "body": "Follow up"},
        ]

        unseen, has_partial = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            rp,
            seen_event_ids={"e1"},
            current_event_id="e3",
        )

        assert has_partial is False
        assert len(unseen) == 0

    def test_seen_event_ids_excludes_partial_self_message(self) -> None:
        """Skip partial self-messages that are already marked seen."""
        config = _make_config()
        rp = runtime_paths_for(config)
        agent_id = config.get_ids(rp)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": agent_id, "body": f"Partial\n\n{CANCELLED_RESPONSE_NOTE}"},
            {"event_id": "e2", "sender": "@user:localhost", "body": "Try again"},
        ]

        unseen, has_partial = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            rp,
            seen_event_ids={"e1"},
            current_event_id="e2",
        )

        assert has_partial is False
        assert len(unseen) == 0

    def test_excludes_placeholder_only(self) -> None:
        """Ignore placeholder-only partial messages."""
        config = _make_config()
        rp = runtime_paths_for(config)
        agent_id = config.get_ids(rp)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": agent_id, "body": f"{IN_PROGRESS_MARKER}"},
            {"event_id": "e2", "sender": "@user:localhost", "body": "Question"},
        ]

        unseen, has_partial = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            rp,
            seen_event_ids=set(),
            current_event_id="e2",
        )

        assert has_partial is False
        assert len(unseen) == 0


# -- _build_prompt_with_unseen header ------------------------------------------


class TestPromptHeaderPartialReplies:
    """Cover prompt header rendering for partial replies."""

    def test_header_with_partial_replies(self) -> None:
        """Show the interrupted-reply note when partial replies exist."""
        unseen = [{"sender": "@agent:localhost", "body": "Partial text"}]
        result = _build_prompt_with_unseen("user prompt", unseen, has_partial_replies=True)
        assert "interrupted/in-progress reply text" in result
        assert "user prompt" in result

    def test_header_without_partial_replies(self) -> None:
        """Omit the interrupted-reply note when no partial replies exist."""
        unseen = [{"sender": "@other:localhost", "body": "Hey"}]
        result = _build_prompt_with_unseen("user prompt", unseen, has_partial_replies=False)
        assert "interrupted" not in result
        assert "Messages from other participants" in result
        assert "user prompt" in result
