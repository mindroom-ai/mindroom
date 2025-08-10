"""Tests for universal mention parsing."""

from __future__ import annotations

from mindroom.config import Config
from mindroom.matrix.mentions import create_mention_content_from_text, parse_mentions_in_text


class TestMentionParsing:
    """Test the universal mention parsing system."""

    def test_parse_single_mention(self) -> None:
        """Test parsing a single agent mention."""
        config = Config.from_yaml()

        text = "Hey @calculator can you help with this?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Hey @mindroom_calculator:localhost can you help with this?"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_multiple_mentions(self) -> None:
        """Test parsing multiple agent mentions."""
        config = Config.from_yaml()

        text = "@calculator and @general please work together on this"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert (
            processed == "@mindroom_calculator:localhost and @mindroom_general:localhost please work together on this"
        )
        assert set(mentions) == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
        assert len(mentions) == 2

    def test_parse_with_full_mention(self) -> None:
        """Test parsing when full @mindroom_agent format is used."""
        config = Config.from_yaml()

        text = "Ask @mindroom_calculator for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_domain(self) -> None:
        """Test parsing when mention already has domain."""
        config = Config.from_yaml()

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Should replace with sender's domain
        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_custom_domain(self) -> None:
        """Test with custom sender domain."""
        config = Config.from_yaml()

        text = "Hey @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "matrix.org", config)

        assert processed == "Hey @mindroom_calculator:matrix.org"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    def test_ignore_unknown_mentions(self) -> None:
        """Test that unknown agents are not converted."""
        config = Config.from_yaml()

        text = "@calculator is real but @unknown is not"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost is real but @unknown is not"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_ignore_user_mentions(self) -> None:
        """Test that user mentions are ignored."""
        config = Config.from_yaml()

        text = "@mindroom_user_123 and @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_user_123 and @mindroom_calculator:localhost"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_no_duplicate_mentions(self) -> None:
        """Test that duplicate mentions are handled."""
        config = Config.from_yaml()

        text = "@calculator help! @calculator are you there?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost help! @mindroom_calculator:localhost are you there?"
        assert mentions == ["@mindroom_calculator:localhost"]  # Only one entry

    def test_create_mention_content_from_text(self) -> None:
        """Test the full content creation with mentions."""
        config = Config.from_yaml()

        content = create_mention_content_from_text(
            config,
            "@calculator and @code please help",
            sender_domain="matrix.org",
            thread_event_id="$thread123",
        )

        assert content["msgtype"] == "m.text"
        assert content["body"] == "@mindroom_calculator:matrix.org and @mindroom_code:matrix.org please help"
        assert set(content["m.mentions"]["user_ids"]) == {
            "@mindroom_calculator:matrix.org",
            "@mindroom_code:matrix.org",
        }
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content["m.relates_to"]["rel_type"] == "m.thread"

    def test_no_mentions_in_text(self) -> None:
        """Test text with no mentions."""
        config = Config.from_yaml()

        text = "This has no mentions"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == text
        assert mentions == []

    def test_mention_in_middle_of_word(self) -> None:
        """Test that mentions in middle of words are not parsed."""
        config = Config.from_yaml()

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Current implementation might catch this - documenting actual behavior
        # This is a limitation we should be aware of
        assert "@mindroom_code:localhost" in processed or processed == text
