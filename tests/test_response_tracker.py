"""Tests for the ResponseTracker class."""

from pathlib import Path
from unittest.mock import patch

import pytest

from mindroom.response_tracker import ResponseTracker


class TestResponseTracker:
    """Test cases for ResponseTracker."""

    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        """Create a temporary directory for testing."""
        return tmp_path

    def test_response_tracker_init(self, temp_dir: Path) -> None:
        """Test ResponseTracker initialization."""
        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = temp_dir / "response_tracking" / "test_agent"

            tracker = ResponseTracker("test_agent")

            assert tracker.agent_name == "test_agent"
            assert isinstance(tracker.responded_events, set)
            assert len(tracker.responded_events) == 0

    def test_has_responded_empty(self, temp_dir: Path) -> None:
        """Test has_responded when no events have been tracked."""
        store_path = temp_dir / "response_tracking" / "test_empty"
        store_path.mkdir(parents=True)

        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = store_path
            tracker = ResponseTracker("test_empty")
            assert not tracker.has_responded("event123")

    def test_mark_responded(self, temp_dir: Path) -> None:
        """Test marking an event as responded."""
        store_path = temp_dir / "response_tracking" / "test_mark"
        store_path.mkdir(parents=True)

        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = store_path
            tracker = ResponseTracker("test_mark")

            # Initially not responded
            assert not tracker.has_responded("event123")

            # Mark as responded
            tracker.mark_responded("event123")

            # Now it should be marked as responded
            assert tracker.has_responded("event123")
            assert "event123" in tracker.responded_events

    def test_persistence(self, temp_dir: Path) -> None:
        """Test that responses are persisted to disk."""
        store_path = temp_dir / "response_tracking" / "test_agent"
        store_path.mkdir(parents=True)

        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = store_path

            # Create tracker and mark some events
            tracker1 = ResponseTracker("test_agent")
            tracker1.mark_responded("event1")
            tracker1.mark_responded("event2")

            # Create new tracker instance - should load previous events
            tracker2 = ResponseTracker("test_agent")

            assert tracker2.has_responded("event1")
            assert tracker2.has_responded("event2")
            assert len(tracker2.responded_events) == 2

    def test_cleanup_old_events(self, temp_dir: Path) -> None:
        """Test cleanup of old events."""
        store_path = temp_dir / "response_tracking" / "test_cleanup"
        store_path.mkdir(parents=True)

        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = store_path

            tracker = ResponseTracker("test_cleanup")

            # Add many events
            for i in range(20):
                tracker.mark_responded(f"event{i:03d}")

            assert len(tracker.responded_events) == 20

            # Cleanup with max 10
            tracker.cleanup_old_events(max_events=10)

            # Should keep only the last 10 (sorted by event ID)
            assert len(tracker.responded_events) == 10
            assert tracker.has_responded("event019")  # Latest should be kept
            assert not tracker.has_responded("event000")  # Oldest should be removed

    def test_load_corrupted_file(self, temp_dir: Path) -> None:
        """Test loading from a corrupted file."""
        store_path = temp_dir / "response_tracking" / "test_agent"
        store_path.mkdir(parents=True)

        # Write corrupted JSON
        responses_file = store_path / "responded_events.json"
        responses_file.write_text("not valid json")

        with patch("mindroom.response_tracker.Path") as mock_path:
            mock_path.return_value = store_path

            # Should handle gracefully and start with empty set
            tracker = ResponseTracker("test_agent")
            assert len(tracker.responded_events) == 0
