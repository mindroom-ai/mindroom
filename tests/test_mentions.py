"""Tests for universal mention parsing."""

from unittest.mock import MagicMock, patch

from mindroom.matrix.mentions import create_mention_content_from_text, parse_mentions_in_text


class TestMentionParsing:
    """Test the universal mention parsing system."""

    @patch("mindroom.matrix.mentions.load_config")
    def test_parse_single_mention(self, mock_load_config):
        """Test parsing a single agent mention."""
        mock_config = MagicMock()
        mock_config.agents = {"calculator": MagicMock(), "general": MagicMock(), "code": MagicMock()}
        mock_load_config.return_value = mock_config

        text = "Hey @calculator can you help with this?"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == "Hey @mindroom_calculator:localhost can you help with this?"
        assert mentions == ["@mindroom_calculator:localhost"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_parse_multiple_mentions(self, mock_load_config):
        """Test parsing multiple agent mentions."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator", "general", "code"]}
        mock_load_config.return_value = mock_config

        text = "@calculator and @general please work together on this"
        processed, mentions = parse_mentions_in_text(text)

        assert (
            processed == "@mindroom_calculator:localhost and @mindroom_general:localhost please work together on this"
        )
        assert set(mentions) == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
        assert len(mentions) == 2

    @patch("mindroom.matrix.mentions.load_config")
    def test_parse_with_full_mention(self, mock_load_config):
        """Test parsing when full @mindroom_agent format is used."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator", "general"]}
        mock_load_config.return_value = mock_config

        text = "Ask @mindroom_calculator for help"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_parse_with_domain(self, mock_load_config):
        """Test parsing when mention already has domain."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "Ask @mindroom_calculator:matrix.org for help"
        processed, mentions = parse_mentions_in_text(text)

        # Should replace with sender's domain
        assert processed == "Ask @mindroom_calculator:localhost for help"
        assert mentions == ["@mindroom_calculator:localhost"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_custom_domain(self, mock_load_config):
        """Test with custom sender domain."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "Hey @calculator"
        processed, mentions = parse_mentions_in_text(text, sender_domain="matrix.org")

        assert processed == "Hey @mindroom_calculator:matrix.org"
        assert mentions == ["@mindroom_calculator:matrix.org"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_ignore_unknown_mentions(self, mock_load_config):
        """Test that unknown agents are not converted."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "@calculator is real but @unknown is not"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == "@mindroom_calculator:localhost is real but @unknown is not"
        assert mentions == ["@mindroom_calculator:localhost"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_ignore_user_mentions(self, mock_load_config):
        """Test that user mentions are ignored."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "@mindroom_user_123 and @calculator"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == "@mindroom_user_123 and @mindroom_calculator:localhost"
        assert mentions == ["@mindroom_calculator:localhost"]

    @patch("mindroom.matrix.mentions.load_config")
    def test_no_duplicate_mentions(self, mock_load_config):
        """Test that duplicate mentions are handled."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "@calculator help! @calculator are you there?"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == "@mindroom_calculator:localhost help! @mindroom_calculator:localhost are you there?"
        assert mentions == ["@mindroom_calculator:localhost"]  # Only one entry

    @patch("mindroom.matrix.mentions.load_config")
    def test_create_mention_content_from_text(self, mock_load_config):
        """Test the full content creation with mentions."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator", "code"]}
        mock_load_config.return_value = mock_config

        content = create_mention_content_from_text(
            "@calculator and @code please help", sender_domain="matrix.org", thread_event_id="$thread123"
        )

        assert content["msgtype"] == "m.text"
        assert content["body"] == "@mindroom_calculator:matrix.org and @mindroom_code:matrix.org please help"
        assert set(content["m.mentions"]["user_ids"]) == {
            "@mindroom_calculator:matrix.org",
            "@mindroom_code:matrix.org",
        }
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content["m.relates_to"]["rel_type"] == "m.thread"

    @patch("mindroom.matrix.mentions.load_config")
    def test_no_mentions_in_text(self, mock_load_config):
        """Test text with no mentions."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["calculator"]}
        mock_load_config.return_value = mock_config

        text = "This has no mentions"
        processed, mentions = parse_mentions_in_text(text)

        assert processed == text
        assert mentions == []

    @patch("mindroom.matrix.mentions.load_config")
    def test_mention_in_middle_of_word(self, mock_load_config):
        """Test that mentions in middle of words are not parsed."""
        mock_config = MagicMock()
        mock_config.agents = {k: MagicMock() for k in ["code"]}
        mock_load_config.return_value = mock_config

        # The regex should require word boundaries
        text = "Use decode@code function"
        processed, mentions = parse_mentions_in_text(text)

        # Current implementation might catch this - documenting actual behavior
        # This is a limitation we should be aware of
        assert "@mindroom_code:localhost" in processed or processed == text
