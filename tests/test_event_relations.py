"""Tests for comprehensive event relation analysis."""

from mindroom.matrix.event_info import EventInfo


class TestEventRelations:
    """Test event relation analysis functionality."""

    def test_analyze_edit_event(self) -> None:
        """Test that edit events are properly detected."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original_event_123",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.is_edit is True
        assert info.original_event_id == "$original_event_123"
        assert info.relation_type == "m.replace"
        assert info.relates_to_event_id == "$original_event_123"
        assert info.has_relations is True
        assert info.can_be_thread_root is False

        # Other types should be False
        assert info.is_thread is False
        assert info.is_reaction is False
        assert info.is_reply is False

    def test_analyze_thread_event(self) -> None:
        """Test that thread events are properly detected."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root_456",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.is_thread is True
        assert info.thread_id == "$thread_root_456"
        assert info.relation_type == "m.thread"
        assert info.relates_to_event_id == "$thread_root_456"
        assert info.has_relations is True
        assert info.can_be_thread_root is False

        # Other types should be False
        assert info.is_edit is False
        assert info.is_reaction is False
        assert info.is_reply is False

    def test_analyze_reaction_event(self) -> None:
        """Test that reaction events are properly detected."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target_event_789",
                    "key": "👍",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.is_reaction is True
        assert info.reaction_target_event_id == "$target_event_789"
        assert info.reaction_key == "👍"
        assert info.relation_type == "m.annotation"
        assert info.relates_to_event_id == "$target_event_789"
        assert info.has_relations is True
        assert info.can_be_thread_root is False

        # Other types should be False
        assert info.is_edit is False
        assert info.is_thread is False
        assert info.is_reply is False

    def test_analyze_reply_in_thread(self) -> None:
        """Test that replies within threads are properly detected."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root_abc",
                    "m.in_reply_to": {
                        "event_id": "$reply_to_def",
                    },
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.is_thread is True
        assert info.thread_id == "$thread_root_abc"
        assert info.is_reply is True
        assert info.reply_to_event_id == "$reply_to_def"
        assert info.relation_type == "m.thread"
        assert info.relates_to_event_id == "$thread_root_abc"
        assert info.has_relations is True
        assert info.can_be_thread_root is False

        # Other types should be False
        assert info.is_edit is False
        assert info.is_reaction is False

    def test_analyze_standalone_reply(self) -> None:
        """Test that standalone replies (rich replies) are properly detected."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "m.in_reply_to": {
                        "event_id": "$reply_to_xyz",
                    },
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.is_reply is True
        assert info.reply_to_event_id == "$reply_to_xyz"
        assert info.has_relations is True
        assert info.can_be_thread_root is False
        assert info.safe_thread_root == "$reply_to_xyz"

        # Other types should be False
        assert info.is_edit is False
        assert info.is_thread is False
        assert info.is_reaction is False
        assert info.relation_type is None  # No explicit rel_type for rich replies

    def test_analyze_plain_message(self) -> None:
        """Test that plain messages without relations are properly detected."""
        event_source = {
            "content": {
                "body": "Hello world",
                "msgtype": "m.text",
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.has_relations is False
        assert info.can_be_thread_root is True
        assert info.safe_thread_root is None

        # All relation types should be False
        assert info.is_edit is False
        assert info.is_thread is False
        assert info.is_reaction is False
        assert info.is_reply is False
        assert info.relation_type is None
        assert info.relates_to_event_id is None

    def test_analyze_none_event(self) -> None:
        """Test that None event source is handled gracefully."""
        info = EventInfo.from_event(None)

        assert info.has_relations is False
        assert info.can_be_thread_root is True
        assert info.safe_thread_root is None

        # All relation types should be False
        assert info.is_edit is False
        assert info.is_thread is False
        assert info.is_reaction is False
        assert info.is_reply is False
        assert info.relation_type is None

    def test_safe_thread_root_for_edit(self) -> None:
        """Test that safe_thread_root is set correctly for edits."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original_msg",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.can_be_thread_root is False
        assert info.safe_thread_root == "$original_msg"

    def test_safe_thread_root_for_reaction(self) -> None:
        """Test that safe_thread_root is set correctly for reactions."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$reacted_msg",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.can_be_thread_root is False
        assert info.safe_thread_root == "$reacted_msg"
