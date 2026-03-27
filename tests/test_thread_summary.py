"""Tests for AI thread summary generation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.thread_summary import (
    _FIRST_THRESHOLD,
    _SUBSEQUENT_INTERVAL,
    _last_summary_counts,
    _next_threshold,
    _recover_last_summary_count,
    _send_summary_event,
    maybe_generate_thread_summary,
)


def _make_thread_history(count: int) -> list[dict[str, Any]]:
    """Build a fake thread history with *count* messages."""
    return [
        {
            "sender": f"@user{i}:localhost",
            "body": f"Message {i}",
            "timestamp": 1700000000 + i * 1000,
            "event_id": f"$event{i}",
        }
        for i in range(count)
    ]


# -- threshold arithmetic --


class TestNextThreshold:
    """Threshold arithmetic for summary generation triggers."""

    def test_first_threshold(self) -> None:
        """First summary triggers at 5 messages."""
        assert _next_threshold(0) == _FIRST_THRESHOLD

    def test_at_first_threshold(self) -> None:
        """After first summary, next threshold is 15."""
        assert _next_threshold(_FIRST_THRESHOLD) == _FIRST_THRESHOLD + _SUBSEQUENT_INTERVAL

    def test_after_first_threshold(self) -> None:
        """Subsequent thresholds increment by 10."""
        assert _next_threshold(15) == 25
        assert _next_threshold(25) == 35

    def test_below_first_threshold(self) -> None:
        """Counts below 5 still target the first threshold."""
        assert _next_threshold(3) == _FIRST_THRESHOLD


# -- _recover_last_summary_count --


def _make_summary_event(
    thread_id: str,
    message_count: object,
    *,
    msgtype: str = "m.notice",
    include_metadata: bool = True,
    relates_to: object | None = None,
) -> MagicMock:
    """Build a fake nio event whose source matches a thread summary payload."""
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": "Some summary",
        "m.relates_to": relates_to
        if relates_to is not None
        else {
            "rel_type": "m.thread",
            "event_id": thread_id,
        },
    }
    if include_metadata:
        content["io.mindroom.thread_summary"] = {
            "version": 1,
            "summary": "Some summary",
            "message_count": message_count,
            "model": "default",
        }

    event = MagicMock()
    event.source = {"content": content}
    return event


def _make_text_event() -> MagicMock:
    """Build a fake nio event that is a normal text message."""
    event = MagicMock()
    event.source = {
        "content": {
            "msgtype": "m.text",
            "body": "Hello world",
        },
    }
    return event


def _make_notice_event() -> MagicMock:
    """Build a fake nio event that is a normal notice without summary metadata."""
    event = MagicMock()
    event.source = {
        "content": {
            "msgtype": "m.notice",
            "body": "Normal notice",
        },
    }
    return event


@pytest.mark.asyncio
class TestRecoverLastSummaryCount:
    """Tests for recovery of summary counts from existing Matrix events."""

    async def test_recovers_count_from_notice_summary_event(self) -> None:
        """Finds a new m.notice summary event and returns its message_count."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_text_event(),
            _make_summary_event("$thread1", 15, msgtype="m.notice"),
            _make_text_event(),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 15

    async def test_recovers_count_from_legacy_summary_event(self) -> None:
        """Older m.thread.summary events remain valid for recovery."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 15, msgtype="m.thread.summary"),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 15

    async def test_returns_highest_count(self) -> None:
        """When multiple summary events exist, returns the highest count."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 25, msgtype="m.notice"),
            _make_summary_event("$thread1", 15, msgtype="m.thread.summary"),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 25

    async def test_ignores_other_threads(self) -> None:
        """Summary events for a different thread are ignored."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$other_thread", 20, msgtype="m.notice"),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_returns_zero_on_api_error(self) -> None:
        """Returns 0 when room_messages fails."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_messages = AsyncMock(return_value=nio.RoomMessagesError(message="forbidden"))

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_returns_zero_when_no_summaries(self) -> None:
        """Returns 0 when no summary events exist."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [_make_text_event(), _make_notice_event()]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_ignores_legacy_msgtype_without_metadata(self) -> None:
        """Old custom msgtype alone is not enough without thread summary metadata."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 15, msgtype="m.thread.summary", include_metadata=False),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_skips_non_dict_relates_to_and_continues_scanning(self) -> None:
        """Malformed m.relates_to values are ignored without aborting recovery."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 15, relates_to="bad-relates-to"),
            _make_summary_event("$thread1", 25),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 25

    async def test_skips_non_int_message_count_and_continues_scanning(self) -> None:
        """Malformed message_count values are ignored without aborting recovery."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", "15"),
            _make_summary_event("$thread1", 25),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 25


# -- maybe_generate_thread_summary --


def _mock_config(model_name: str | None = None) -> MagicMock:
    config = MagicMock()
    config.defaults.thread_summary_model = model_name
    return config


def _mock_runtime_paths() -> MagicMock:
    rp = MagicMock()
    rp.storage_root = "/var/empty/test_storage"
    return rp


@pytest.fixture(autouse=True)
def _clear_summary_counts() -> None:
    """Reset in-memory state between tests."""
    _last_summary_counts.clear()


@pytest.mark.asyncio
class TestMaybeGenerateThreadSummary:
    """Integration tests for the threshold-gated summary pipeline."""

    async def test_below_threshold_skips(self) -> None:
        """No LLM call when message count is below the first threshold."""
        client = AsyncMock(spec=nio.AsyncClient)
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(3),
            ) as mock_fetch,
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        mock_fetch.assert_awaited_once()
        mock_gen.assert_not_awaited()

    async def test_at_threshold_generates(self) -> None:
        """LLM is called and event sent when count reaches threshold."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary1", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Users discussed testing strategies",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        assert _last_summary_counts["!room:x:$thread1"] == 5

    async def test_already_summarized_skips(self) -> None:
        """No LLM call when count hasn't crossed the next threshold."""
        _last_summary_counts["!room:x:$thread1"] = 5
        client = AsyncMock(spec=nio.AsyncClient)
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(10),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        mock_gen.assert_not_awaited()

    async def test_crosses_second_threshold(self) -> None:
        """Summary is generated when crossing the second threshold (15)."""
        _last_summary_counts["!room:x:$thread1"] = 5
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary2", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(15),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Team decided on approach B",
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        client.room_send.assert_awaited_once()
        assert _last_summary_counts["!room:x:$thread1"] == 15

    async def test_generation_failure_no_event(self) -> None:
        """No Matrix event sent when LLM returns None; count is recorded to prevent retries."""
        client = AsyncMock(spec=nio.AsyncClient)
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value=None,
            ),
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        client.room_send.assert_not_awaited()
        # Count is recorded to prevent retry storms
        assert _last_summary_counts["!room:x:$thread1"] == 5

    async def test_generation_exception_records_count(self) -> None:
        """Exception in _generate_summary records count to prevent retry storms."""
        client = AsyncMock(spec=nio.AsyncClient)
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                side_effect=RuntimeError("LLM unavailable"),
            ),
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        client.room_send.assert_not_awaited()
        # Count is recorded to prevent retry storms
        assert _last_summary_counts["!room:x:$thread1"] == 5

    async def test_send_failure_still_records_count(self) -> None:
        """When _send_summary_event fails (returns None), count is still recorded to prevent cost amplification."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_send = AsyncMock(return_value=nio.RoomSendError(message="forbidden"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Users discussed testing strategies",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        # Count must be recorded even though send failed
        assert _last_summary_counts["!room:x:$thread1"] == 5

    async def test_recovery_seeds_cache_on_restart(self) -> None:
        """On cache miss, recovery from existing events seeds _last_summary_counts."""
        client = AsyncMock(spec=nio.AsyncClient)
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary.fetch_thread_history",
                return_value=_make_thread_history(12),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=10,
            ),
        ):
            await maybe_generate_thread_summary(client, "!room:x", "$thread1", config, rp)

        # Recovered count 10 → next threshold 20 → 12 messages < 20 → skip
        mock_gen.assert_not_awaited()
        assert _last_summary_counts["!room:x:$thread1"] == 10


# -- event content structure --


@pytest.mark.asyncio
class TestSendSummaryEvent:
    """Verify the Matrix event payload structure."""

    async def test_event_content_structure(self) -> None:
        """Verify the Matrix event has the expected fields."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$s1", room_id="!r:x"))

        result = await _send_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary="Discussed deployment plan",
            message_count=15,
            model_name="haiku",
        )

        assert result == "$s1"
        call_kwargs = client.room_send.call_args.kwargs
        assert call_kwargs["room_id"] == "!room:x"
        assert call_kwargs["message_type"] == "m.room.message"

        content = call_kwargs["content"]
        assert content["msgtype"] == "m.notice"
        assert content["body"] == "Discussed deployment plan"

        relates_to = content["m.relates_to"]
        assert relates_to["rel_type"] == "m.thread"
        assert relates_to["event_id"] == "$root1"

        meta = content["io.mindroom.thread_summary"]
        assert meta["version"] == 1
        assert meta["summary"] == "Discussed deployment plan"
        assert meta["message_count"] == 15
        assert meta["model"] == "haiku"
        assert "generated_at" in meta

    async def test_send_failure_returns_none(self) -> None:
        """Return None when room_send fails."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_send = AsyncMock(return_value=nio.RoomSendError(message="forbidden"))

        result = await _send_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary="test",
            message_count=5,
            model_name="default",
        )

        assert result is None
