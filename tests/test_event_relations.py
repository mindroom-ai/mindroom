"""Tests for comprehensive event relation analysis."""

import pytest

from mindroom.event_journal import replacement_target, thread_root
from mindroom.matrix.event_info import EventInfo, origin_server_ts_from_event_source, reply_to_event_id_from_content


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

        # All relation types should be False
        assert info.is_edit is False
        assert info.is_thread is False
        assert info.is_reaction is False
        assert info.is_reply is False
        assert info.relation_type is None

    def test_malformed_relation_content_is_ignored(self) -> None:
        """Malformed raw Matrix content should not crash relation analysis."""
        for event_source in (
            {"content": "not-a-dict"},
            {"content": {"m.relates_to": "not-a-dict"}},
            {"content": {"m.relates_to": {"m.in_reply_to": "not-a-dict"}}},
            {"content": {"m.relates_to": {"m.in_reply_to": {"event_id": 123}}}},
        ):
            info = EventInfo.from_event(event_source)

            assert info.is_reply is False
            assert info.reply_to_event_id is None

    @pytest.mark.parametrize("malformed_target", ["", 7, None, ["$root:localhost"]])
    def test_relation_target_that_names_no_event_matches_the_journal(
        self,
        malformed_target: object,
    ) -> None:
        """A relation target the journal refuses must not become a relation here.

        ``event_journal.projection`` reads the same raw relation and keeps only a
        non-empty string, so any looser answer places a turn in a thread -- or against
        an edit or reaction target -- that the durable row admitting the same event
        says does not exist.
        """
        for rel_type in ("m.thread", "m.replace", "m.annotation", "m.reference"):
            content = {"m.relates_to": {"rel_type": rel_type, "event_id": malformed_target}}
            info = EventInfo.from_event({"type": "m.room.message", "content": content})

            assert info.relates_to_event_id is None
            assert info.thread_id is None
            assert info.original_event_id is None
            assert info.reaction_target_event_id is None
            assert info.next_related_event_id("$self:localhost") is None
            # The journal projection is the authority this must agree with.
            assert thread_root(content) is None
            assert replacement_target(content) is None

        edit_content = {
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original:localhost"},
            "m.new_content": {"m.relates_to": {"rel_type": "m.thread", "event_id": malformed_target}},
        }
        assert EventInfo.from_event({"type": "m.room.message", "content": edit_content}).thread_id_from_edit is None

    def test_reply_to_event_id_from_content_extracts_only_string_reply_targets(self) -> None:
        """Reply target extraction should share the Matrix relation traversal."""
        assert (
            reply_to_event_id_from_content(
                {"m.relates_to": {"m.in_reply_to": {"event_id": "$target:localhost"}}},
            )
            == "$target:localhost"
        )

        assert reply_to_event_id_from_content(None) is None
        assert reply_to_event_id_from_content({"m.relates_to": "invalid"}) is None
        assert reply_to_event_id_from_content({"m.relates_to": {"m.in_reply_to": "invalid"}}) is None
        assert (
            reply_to_event_id_from_content(
                {"m.relates_to": {"m.in_reply_to": {"event_id": 123}}},
            )
            is None
        )

    def test_origin_server_ts_from_event_source_returns_numeric_timestamp(self) -> None:
        """Raw Matrix timestamp extraction should have one shared non-throwing helper."""
        assert origin_server_ts_from_event_source({"origin_server_ts": 1234}) == 1234
        assert origin_server_ts_from_event_source({"origin_server_ts": 12.5}) == 12.5
        assert origin_server_ts_from_event_source({"origin_server_ts": True}) is None
        assert origin_server_ts_from_event_source({"origin_server_ts": "1234"}) is None
        assert origin_server_ts_from_event_source(None) is None

    def test_edit_relates_to_original_event(self) -> None:
        """Edit relation metadata should point at the original event only."""
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
        assert info.original_event_id == "$original_msg"
        assert info.relates_to_event_id == "$original_msg"

    def test_reaction_relates_to_target_event(self) -> None:
        """Reaction relation metadata should point at the target event only."""
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
        assert info.reaction_target_event_id == "$reacted_msg"
        assert info.relates_to_event_id == "$reacted_msg"

    def test_next_related_event_id_prefers_original_event_for_edits(self) -> None:
        """Edits should follow the edited event before any copied reply metadata."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$original_msg",
                    "m.in_reply_to": {
                        "event_id": "$reply_target",
                    },
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.next_related_event_id(current_event_id="$edit_event") == "$original_msg"

    def test_next_related_event_id_does_not_follow_thread_root_for_thread_events(self) -> None:
        """Thread replies should expose their thread root separately, not as a reply-chain parent."""
        event_source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        }

        info = EventInfo.from_event(event_source)

        assert info.thread_id == "$thread_root"
        assert info.next_related_event_id(current_event_id="$thread_reply") is None
