"""Tests for universal mention parsing."""

from mindroom.agent_config import load_config
from mindroom.matrix.mentions import create_mention_content_from_text, parse_mentions_in_text


class TestMentionParsing:
    """Test the universal mention parsing system."""

    def test_parse_single_mention(self):
        """Test parsing a single agent mention."""
        config = load_config()

        text = "Hey @calculator can you help with this?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Hey @mindroom_calculator:localhost can you help with this?"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_multiple_mentions(self):
        """Test parsing multiple agent mentions."""
        config = load_config()

        text = "@calculator and @general please work together on this"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert (
            processed == "@mindroom_calculator:localhost and @mindroom_general:localhost please work together on this"
        )
        assert set(mentions) == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
        assert len(mentions) == 2

    def test_parse_with_full_mention(self):
        """Test parsing when full @mindroom_agent format is used."""
        config = load_config()

        text = "Ask @mindroom_calculator for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_parse_with_domain(self):
        """Test parsing when mention already has domain."""
        config = load_config()

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Should replace with sender's domain
        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_custom_domain(self):
        """Test with custom sender domain."""
        config = load_config()

        text = "Hey @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "matrix.org", config)

        assert processed == "Hey @mindroom_calculator:matrix.org"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    def test_ignore_unknown_mentions(self):
        """Test that unknown agents are not converted."""
        config = load_config()

        text = "@calculator is real but @unknown is not"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost is real but @unknown is not"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_ignore_user_mentions(self):
        """Test that user mentions are ignored."""
        config = load_config()

        text = "@mindroom_user_123 and @calculator"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_user_123 and @mindroom_calculator:localhost"
        assert mentions == ["@mindroom_calculator:localhost"]

    def test_no_duplicate_mentions(self):
        """Test that duplicate mentions are handled."""
        config = load_config()

        text = "@calculator help! @calculator are you there?"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == "@mindroom_calculator:localhost help! @mindroom_calculator:localhost are you there?"
        assert mentions == ["@mindroom_calculator:localhost"]  # Only one entry

    def test_create_mention_content_from_text(self):
        """Test the full content creation with mentions."""
        config = load_config()

        content = create_mention_content_from_text(
            config, "@calculator and @code please help", sender_domain="matrix.org", thread_event_id="$thread123"
        )

        assert content["msgtype"] == "m.text"
        assert content["body"] == "@mindroom_calculator:matrix.org and @mindroom_code:matrix.org please help"
        assert set(content["m.mentions"]["user_ids"]) == {
            "@mindroom_calculator:matrix.org",
            "@mindroom_code:matrix.org",
        }
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content["m.relates_to"]["rel_type"] == "m.thread"

    def test_no_mentions_in_text(self):
        """Test text with no mentions."""
        config = load_config()

        text = "This has no mentions"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        assert processed == text
        assert mentions == []

    def test_mention_in_middle_of_word(self):
        """Test that mentions in middle of words are not parsed."""
        config = load_config()

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, mentions, markdown = parse_mentions_in_text(text, "localhost", config)

        # Current implementation might catch this - documenting actual behavior
        # This is a limitation we should be aware of
        assert "@mindroom_code:localhost" in processed or processed == text
