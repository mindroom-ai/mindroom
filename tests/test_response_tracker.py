"""Tests for the ResponseTracker class."""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from mindroom.response_tracker import ResponseTracker


class TestResponseTracker:
    """Test cases for ResponseTracker."""

    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        """Create a temporary directory for testing."""
        return tmp_path

    def test_response_tracker_init(self, temp_dir: Path) -> None:
        """Test ResponseTracker initialization."""
        tracker = ResponseTracker("test_agent", base_path=temp_dir)

        assert tracker.agent_name == "test_agent"
        assert isinstance(tracker._responses, dict)
        assert len(tracker._responses) == 0

    def test_has_responded_empty(self, temp_dir: Path) -> None:
        """Test has_responded when no events have been tracked."""
        tracker = ResponseTracker("test_empty", base_path=temp_dir)
        assert not tracker.has_responded("event123")

    def test_mark_responded(self, temp_dir: Path) -> None:
        """Test marking an event as responded."""
        tracker = ResponseTracker("test_mark", base_path=temp_dir)

        # Initially not responded
        assert not tracker.has_responded("event123")

        # Mark as responded
        before_time = time.time()
        tracker.mark_responded("event123")
        after_time = time.time()

        # Now it should be marked as responded
        assert tracker.has_responded("event123")
        assert "event123" in tracker._responses

        # Check timestamp is reasonable
        timestamp = tracker._responses["event123"]["timestamp"]
        assert before_time <= timestamp <= after_time

    def test_persistence(self, temp_dir: Path) -> None:
        """Test that responses are persisted to disk."""
        # Create tracker and mark some events
        tracker1 = ResponseTracker("test_agent", base_path=temp_dir)
        tracker1.mark_responded("event1")
        time.sleep(0.1)  # Ensure different timestamps
        tracker1.mark_responded("event2")

        # Create new tracker instance - should load previous events
        tracker2 = ResponseTracker("test_agent", base_path=temp_dir)

        assert tracker2.has_responded("event1")
        assert tracker2.has_responded("event2")
        assert len(tracker2._responses) == 2

        # Timestamps should be preserved
        assert tracker1._responses["event1"]["timestamp"] == tracker2._responses["event1"]["timestamp"]
        assert tracker1._responses["event2"]["timestamp"] == tracker2._responses["event2"]["timestamp"]

    def test_cleanup_by_count(self, temp_dir: Path) -> None:
        """Test cleanup keeps most recent events by count."""
        tracker = ResponseTracker("test_cleanup", base_path=temp_dir)

        # Add events with known timestamps
        base_time = time.time()
        for i in range(20):
            tracker._responses[f"event{i:03d}"] = {
                "timestamp": base_time + i,
                "response_id": None,
            }

        assert len(tracker._responses) == 20

        # Cleanup with max 10
        tracker._cleanup_old_events(max_events=10)

        # Should keep only the last 10 (most recent by timestamp)
        assert len(tracker._responses) == 10
        assert tracker.has_responded("event019")  # Latest should be kept
        assert tracker.has_responded("event010")  # 10th most recent
        assert not tracker.has_responded("event009")  # Should be removed
        assert not tracker.has_responded("event000")  # Oldest should be removed

    def test_cleanup_by_age(self, temp_dir: Path) -> None:
        """Test cleanup removes events older than max age."""
        tracker = ResponseTracker("test_age_cleanup", base_path=temp_dir)

        current_time = time.time()

        # Add some old events (40 days old)
        for i in range(5):
            tracker._responses[f"old_event{i}"] = {
                "timestamp": current_time - (40 * 24 * 60 * 60),
                "response_id": None,
            }

        # Add some recent events
        for i in range(5):
            tracker._responses[f"new_event{i}"] = {
                "timestamp": current_time - (10 * 24 * 60 * 60),
                "response_id": None,
            }

        assert len(tracker._responses) == 10

        # Cleanup with 30 day max age
        tracker._cleanup_old_events(max_events=100, max_age_days=30)

        # Should keep only the recent events
        assert len(tracker._responses) == 5
        for i in range(5):
            assert tracker.has_responded(f"new_event{i}")
            assert not tracker.has_responded(f"old_event{i}")

    def test_concurrent_access(self, temp_dir: Path) -> None:
        """Test that file locking prevents corruption during concurrent writes."""
        tracker = ResponseTracker("test_concurrent", base_path=temp_dir)

        def mark_events(start: int, count: int) -> None:
            for i in range(start, start + count):
                tracker.mark_responded(f"event_{i}")

        # Create multiple threads marking events
        threads = []
        for i in range(0, 100, 25):
            thread = threading.Thread(target=mark_events, args=(i, 25))
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # All events should be marked
        assert len(tracker._responses) == 100

        # Verify file is valid JSON
        with tracker._responses_file.open() as f:
            data = json.load(f)
            assert len(data) == 100
