"""Tests for AI thread summary generation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.thread_summary import (
    _MAX_MESSAGES_BEFORE_TRUNCATION,
    _TRUNCATION_SAMPLE_SIZE,
    THREAD_SUMMARY_MAX_LENGTH,
    ThreadSummaryWriteError,
    _build_conversation_text,
    _count_non_summary_messages,
    _generate_summary,
    _is_thread_summary_message,
    _last_summary_counts,
    _next_threshold,
    _recover_last_summary_count,
    _thread_locks,
    _ThreadSummary,
    maybe_generate_thread_summary,
    next_thread_summary_threshold,
    normalize_thread_summary_text,
    send_thread_summary_event,
    set_manual_thread_summary,
    should_queue_thread_summary,
    thread_summary_cache_key,
    thread_summary_message_count_hint,
    update_last_summary_count,
)
from tests.conftest import make_matrix_client_mock


def _make_thread_history(count: int) -> list[ResolvedVisibleMessage]:
    """Build a fake thread history with *count* messages."""
    return [
        ResolvedVisibleMessage.synthetic(
            sender=f"@user{i}:localhost",
            body=f"Message {i}",
            timestamp=1700000000 + i * 1000,
            event_id=f"$event{i}",
        )
        for i in range(count)
    ]


def _make_summary_notice_message(
    thread_id: str,
    *,
    message_count: int,
    event_id: str = "$summary-event",
) -> ResolvedVisibleMessage:
    """Build a synthetic thread summary notice for history-counting regressions."""
    summary = "🧵 Existing thread summary"
    return ResolvedVisibleMessage.synthetic(
        sender="@mindroom:localhost",
        body=summary,
        event_id=event_id,
        content={
            "msgtype": "m.notice",
            "body": summary,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": thread_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": thread_id},
            },
            "io.mindroom.thread_summary": {
                "version": 1,
                "summary": summary,
                "message_count": message_count,
                "model": "manual",
            },
        },
        thread_id=thread_id,
    )


def _mock_client() -> AsyncMock:
    """Return a typed AsyncClient mock with an initialized room cache."""
    return make_matrix_client_mock()


# -- model validation --


def test_thread_summary_model_rejects_overlong_summary() -> None:
    """Structured summary responses should reject content beyond the hard length limit."""
    with pytest.raises(ValidationError):
        _ThreadSummary(summary="x" * (THREAD_SUMMARY_MAX_LENGTH + 1))


def test_normalize_thread_summary_text_strips_common_markdown_syntax() -> None:
    """Thread summary normalization should remove markdown syntax while preserving readable text."""
    raw_summary = "# **Fix** [ISSUE-116](http://example.com)\n> `deploy` ~~done~~"

    assert normalize_thread_summary_text(raw_summary) == "Fix ISSUE-116 deploy done"


# -- threshold arithmetic --


class TestNextThreshold:
    """Threshold arithmetic for summary generation triggers."""

    def test_first_threshold(self) -> None:
        """First summary threshold should come from config values."""
        assert _next_threshold(0, first_threshold=5, subsequent_interval=10) == 5

    def test_at_first_threshold(self) -> None:
        """After first summary, next threshold should advance by the configured interval."""
        assert _next_threshold(5, first_threshold=5, subsequent_interval=10) == 15

    def test_after_first_threshold(self) -> None:
        """Subsequent thresholds should increment by the configured interval."""
        assert _next_threshold(15, first_threshold=5, subsequent_interval=10) == 25
        assert _next_threshold(25, first_threshold=5, subsequent_interval=10) == 35

    def test_manual_summary_below_first_threshold_uses_subsequent_interval(self) -> None:
        """Any existing summary count should become the new baseline, even below the first threshold."""
        assert _next_threshold(3, first_threshold=5, subsequent_interval=10) == 13

    def test_custom_thresholds(self) -> None:
        """Custom config values should shift both first and subsequent thresholds."""
        assert _next_threshold(0, first_threshold=1, subsequent_interval=4) == 1
        assert _next_threshold(1, first_threshold=1, subsequent_interval=4) == 5
        assert _next_threshold(5, first_threshold=1, subsequent_interval=4) == 9


class TestUpdateLastSummaryCount:
    """In-memory cache updates for summary baselines."""

    def test_ignores_lower_write_after_higher_write(self) -> None:
        """A later stale write must not move the summary baseline backwards."""
        update_last_summary_count("!room:x", "$thread1", 12)
        update_last_summary_count("!room:x", "$thread1", 7)

        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 12


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
        client = _mock_client()
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
        client = _mock_client()
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 15, msgtype="m.thread.summary"),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 15

    async def test_returns_highest_count(self) -> None:
        """When multiple summary events exist, returns the highest count."""
        client = _mock_client()
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
        client = _mock_client()
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$other_thread", 20, msgtype="m.notice"),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_returns_zero_on_api_error(self) -> None:
        """Returns 0 when room_messages fails."""
        client = _mock_client()
        client.room_messages = AsyncMock(return_value=nio.RoomMessagesError(message="forbidden"))

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_returns_zero_when_no_summaries(self) -> None:
        """Returns 0 when no summary events exist."""
        client = _mock_client()
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [_make_text_event(), _make_notice_event()]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_ignores_legacy_msgtype_without_metadata(self) -> None:
        """Old custom msgtype alone is not enough without thread summary metadata."""
        client = _mock_client()
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", 15, msgtype="m.thread.summary", include_metadata=False),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 0

    async def test_skips_non_dict_relates_to_and_continues_scanning(self) -> None:
        """Malformed m.relates_to values are ignored without aborting recovery."""
        client = _mock_client()
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
        client = _mock_client()
        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [
            _make_summary_event("$thread1", "15"),
            _make_summary_event("$thread1", 25),
        ]
        client.room_messages = AsyncMock(return_value=response)

        result = await _recover_last_summary_count(client, "!room:x", "$thread1")
        assert result == 25


# -- maybe_generate_thread_summary --


def _mock_config(
    model_name: str | None = None,
    *,
    first_threshold: int = 5,
    subsequent_interval: int = 10,
) -> MagicMock:
    config = MagicMock()
    config.defaults.thread_summary_model = model_name
    config.defaults.thread_summary_first_threshold = first_threshold
    config.defaults.thread_summary_subsequent_interval = subsequent_interval
    return config


def _mock_runtime_paths() -> MagicMock:
    rp = MagicMock()
    rp.storage_root = "/var/empty/test_storage"
    return rp


@pytest.fixture(autouse=True)
def _clear_summary_counts() -> None:
    """Reset in-memory state between tests."""
    _last_summary_counts.clear()
    _thread_locks.clear()


class TestThreadSummaryMessageCountHint:
    """Lower-bound message-count hints used by the pre-queue gate."""

    def test_ignores_existing_summary_notice_and_accounts_for_new_reply(self) -> None:
        """Summary notices must not count, but the just-sent reply must."""
        thread_history = [
            *_make_thread_history(4),
            _make_summary_notice_message("$thread1", message_count=4),
        ]

        assert thread_summary_message_count_hint(thread_history) == 5


class TestShouldQueueThreadSummary:
    """Cheap pre-queue gating based on the cached threshold and lower-bound hint."""

    def test_margin_near_first_threshold_queues(self) -> None:
        """Hints within the concurrency margin should still queue a live recheck."""
        config = _mock_config()

        assert should_queue_thread_summary(
            "!room:x",
            "$thread1",
            config,
            message_count_hint=4,
        )

    def test_far_below_first_threshold_skips_queue(self) -> None:
        """Hints that are still clearly below threshold should skip queueing."""
        config = _mock_config()

        assert not should_queue_thread_summary(
            "!room:x",
            "$thread1",
            config,
            message_count_hint=2,
        )

    def test_cached_summary_uses_subsequent_threshold(self) -> None:
        """Once a summary baseline exists, the gate should honor the same margin."""
        update_last_summary_count("!room:x", "$thread1", 5)
        config = _mock_config()

        assert next_thread_summary_threshold("!room:x", "$thread1", config) == 15
        assert not should_queue_thread_summary(
            "!room:x",
            "$thread1",
            config,
            message_count_hint=12,
        )
        assert should_queue_thread_summary(
            "!room:x",
            "$thread1",
            config,
            message_count_hint=13,
        )


@pytest.mark.asyncio
class TestMaybeGenerateThreadSummary:
    """Integration tests for the threshold-gated summary pipeline."""

    @pytest.fixture(autouse=True)
    def _conversation_cache(self) -> None:
        """Provide one explicit conversation-cache mock per test."""
        self.conversation_cache = MagicMock()
        self.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$thread1")
        self.conversation_cache.notify_outbound_message = Mock()

    async def _maybe_generate(
        self,
        client: AsyncMock,
        config: MagicMock,
        rp: MagicMock,
        *,
        message_count_hint: int | None = None,
    ) -> None:
        """Run the production helper through the explicit access seam."""
        await maybe_generate_thread_summary(
            client,
            "!room:x",
            "$thread1",
            config,
            rp,
            conversation_cache=self.conversation_cache,
            message_count_hint=message_count_hint,
        )

    async def test_below_threshold_skips(self) -> None:
        """No LLM call when message count is below the first threshold."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        mock_fetch.assert_awaited_once()
        mock_gen.assert_not_awaited()

    async def test_below_threshold_skips_timed_generation_helper(self) -> None:
        """Timing should only wrap actual generation attempts, not early threshold skips."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(3),
            ),
            patch(
                "mindroom.thread_summary._timed_generate_summary",
                new=AsyncMock(return_value="Summary"),
            ) as mock_timed_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await self._maybe_generate(client, config, rp)

        mock_timed_gen.assert_not_awaited()

    async def test_at_threshold_generates(self) -> None:
        """LLM is called and event sent when count reaches threshold."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary1", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    @pytest.mark.parametrize(
        ("message_count", "should_generate"),
        [
            (4, False),
            (5, True),
            (6, True),
        ],
    )
    async def test_first_threshold_boundaries(self, message_count: int, should_generate: bool) -> None:
        """The first-threshold boundary should trigger only at count 5 or above."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary1", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(message_count),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Boundary summary",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await self._maybe_generate(client, config, rp)

        assert mock_gen.await_count == int(should_generate)
        assert client.room_send.await_count == int(should_generate)

    @pytest.mark.parametrize(
        ("message_count", "should_generate"),
        [
            (14, False),
            (15, True),
            (16, True),
        ],
    )
    async def test_second_threshold_boundaries(self, message_count: int, should_generate: bool) -> None:
        """The second-threshold boundary should trigger only at count 15 or above."""
        update_last_summary_count("!room:x", "$thread1", 5)
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary2", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(message_count),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Boundary summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        assert mock_gen.await_count == int(should_generate)
        assert client.room_send.await_count == int(should_generate)

    async def test_concurrent_calls_generate_and_send_once_per_thread(self) -> None:
        """Concurrent calls for one thread should share a single generation/send path."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()
        generation_started = asyncio.Event()
        release_generation = asyncio.Event()

        async def _blocked_generate(*_: object) -> str:
            generation_started.set()
            await release_generation.wait()
            return "Users discussed testing strategies"

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                new=AsyncMock(side_effect=_blocked_generate),
            ) as mock_gen,
            patch(
                "mindroom.thread_summary.send_thread_summary_event",
                new=AsyncMock(return_value="$summary1"),
            ) as mock_send,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            task_one = asyncio.create_task(self._maybe_generate(client, config, rp))
            task_two = asyncio.create_task(
                self._maybe_generate(client, config, rp),
            )
            await generation_started.wait()
            await asyncio.sleep(0)
            release_generation.set()
            await asyncio.gather(task_one, task_two)

        assert mock_gen.await_count == 1
        mock_send.assert_awaited_once_with(
            client,
            "!room:x",
            "$thread1",
            "Users discussed testing strategies",
            5,
            "default",
            self.conversation_cache,
        )
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    async def test_auto_generated_summary_strips_markdown_before_send(self) -> None:
        """Auto summaries should be converted to plain text before the Matrix event is sent."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="# **Fix** [ISSUE-116](http://example.com)",
            ),
            patch(
                "mindroom.thread_summary.send_thread_summary_event",
                new=AsyncMock(return_value="$summary1"),
            ) as mock_send,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await self._maybe_generate(client, config, rp)

        mock_send.assert_awaited_once_with(
            client,
            "!room:x",
            "$thread1",
            "Fix ISSUE-116",
            5,
            "default",
            self.conversation_cache,
        )
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    async def test_stale_below_threshold_hint_still_fetches_live_thread_history(self) -> None:
        """A stale low hint must not suppress a fetch when concurrent posts crossed the threshold."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()
        thread_history = _make_thread_history(5)

        with (
            patch("mindroom.thread_summary._load_thread_history", return_value=thread_history) as mock_fetch,
            patch("mindroom.thread_summary._generate_summary", return_value="Summary") as mock_gen,
            patch("mindroom.thread_summary._recover_last_summary_count", return_value=0),
        ):
            await self._maybe_generate(client, config, rp, message_count_hint=4)

        mock_fetch.assert_awaited_once_with(self.conversation_cache, "!room:x", "$thread1")
        mock_gen.assert_awaited_once_with(thread_history, config, rp)
        client.room_send.assert_awaited_once()

    async def test_threshold_hint_fetches_on_boundary(self) -> None:
        """A hint at the threshold should still fetch and generate the summary."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()
        thread_history = _make_thread_history(5)

        with (
            patch("mindroom.thread_summary._load_thread_history", return_value=thread_history) as mock_fetch,
            patch("mindroom.thread_summary._generate_summary", return_value="Summary") as mock_gen,
            patch("mindroom.thread_summary._recover_last_summary_count", return_value=0),
        ):
            await self._maybe_generate(client, config, rp, message_count_hint=5)

        mock_fetch.assert_awaited_once_with(self.conversation_cache, "!room:x", "$thread1")
        mock_gen.assert_awaited_once_with(thread_history, config, rp)
        client.room_send.assert_awaited_once()

    async def test_already_summarized_skips(self) -> None:
        """No LLM call when count hasn't crossed the next threshold."""
        update_last_summary_count("!room:x", "$thread1", 5)
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(10),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_not_awaited()

    async def test_crosses_second_threshold(self) -> None:
        """Summary is generated when crossing the second threshold (15)."""
        update_last_summary_count("!room:x", "$thread1", 5)
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary2", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(15),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Team decided on approach B",
            ),
        ):
            await self._maybe_generate(client, config, rp)

        client.room_send.assert_awaited_once()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 15

    async def test_first_threshold_one_triggers_on_first_message(self) -> None:
        """A configured first threshold of 1 should summarize the first thread message."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary-first", room_id="!room:x"))
        config = _mock_config(first_threshold=1)
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(1),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="🧵 First thread message summarized",
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 1

    async def test_custom_subsequent_interval_controls_next_threshold(self) -> None:
        """A custom interval should defer the next summary until the configured count is reached."""
        update_last_summary_count("!room:x", "$thread1", 3)
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary-custom", room_id="!room:x"))
        config = _mock_config(first_threshold=3, subsequent_interval=4)
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(6),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_not_awaited()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(7),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="🧵 Custom interval threshold reached",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 7

    async def test_manual_summary_below_first_threshold_delays_next_auto_summary(self) -> None:
        """A manual summary below the first threshold should suppress auto-summary until the interval is reached."""
        update_last_summary_count("!room:x", "$thread1", 3)
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary-manual", room_id="!room:x"))
        config = _mock_config(first_threshold=5, subsequent_interval=10)
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_not_awaited()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(12),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_not_awaited()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(13),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="🧵 Manual baseline respected",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 13

    async def test_existing_summary_notice_does_not_advance_threshold(self) -> None:
        """Existing thread summary notices must not count toward the next automatic threshold."""
        update_last_summary_count("!room:x", "$thread1", 5)
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()
        thread_history = [
            *_make_thread_history(14),
            _make_summary_notice_message("$thread1", message_count=5),
        ]

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=thread_history,
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
            ) as mock_gen,
        ):
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_not_awaited()
        client.room_send.assert_not_awaited()

    async def test_generation_failure_no_event(self) -> None:
        """No Matrix event sent when LLM returns None; count is recorded to prevent retries."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        client.room_send.assert_not_awaited()
        # Count is recorded to prevent retry storms
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    async def test_generation_exception_records_count(self) -> None:
        """Exception in _generate_summary records count to prevent retry storms."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        client.room_send.assert_not_awaited()
        # Count is recorded to prevent retry storms
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    async def test_send_failure_still_records_count(self) -> None:
        """When _send_summary_event fails (returns None), count is still recorded to prevent cost amplification."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendError(message="forbidden"))
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()
        # Count must be recorded even though send failed
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 5

    async def test_recovery_seeds_cache_on_restart(self) -> None:
        """On cache miss, recovery from existing events seeds _last_summary_counts."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
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
            await self._maybe_generate(client, config, rp)

        # Recovered count 10 → next threshold 20 → 12 messages < 20 → skip
        mock_gen.assert_not_awaited()
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$thread1")] == 10

    async def test_concurrent_calls_generate_one_summary_per_thread(self) -> None:
        """Concurrent summary checks should serialize on the per-thread critical section."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$summary1", room_id="!room:x"))
        config = _mock_config()
        rp = _mock_runtime_paths()
        release_generation = asyncio.Event()

        async def _blocked_summary(*_args: object, **_kwargs: object) -> str:
            await release_generation.wait()
            return "Users discussed testing strategies"

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                return_value=_make_thread_history(5),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                side_effect=_blocked_summary,
            ) as mock_gen,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            first = asyncio.create_task(self._maybe_generate(client, config, rp))
            second = asyncio.create_task(self._maybe_generate(client, config, rp))
            await asyncio.sleep(0)
            release_generation.set()
            await asyncio.gather(first, second)

        mock_gen.assert_awaited_once()
        client.room_send.assert_awaited_once()

    async def test_concurrent_calls_serialize_history_fetch_inside_lock(self) -> None:
        """Only one concurrent task should fetch history before the per-thread lock is released."""
        client = _mock_client()
        config = _mock_config()
        rp = _mock_runtime_paths()
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        fetch_calls = 0

        async def _blocked_fetch(*_args: object, **_kwargs: object) -> list[ResolvedVisibleMessage]:
            nonlocal fetch_calls
            fetch_calls += 1
            fetch_started.set()
            await release_fetch.wait()
            return _make_thread_history(5)

        with (
            patch(
                "mindroom.thread_summary._load_thread_history",
                new=AsyncMock(side_effect=_blocked_fetch),
            ),
            patch(
                "mindroom.thread_summary._generate_summary",
                return_value="Users discussed testing strategies",
            ),
            patch(
                "mindroom.thread_summary.send_thread_summary_event",
                new=AsyncMock(return_value="$summary1"),
            ) as mock_send,
            patch(
                "mindroom.thread_summary._recover_last_summary_count",
                return_value=0,
            ),
        ):
            first = asyncio.create_task(self._maybe_generate(client, config, rp))
            second = asyncio.create_task(self._maybe_generate(client, config, rp))
            await fetch_started.wait()
            await asyncio.sleep(0)
            assert fetch_calls == 1
            release_fetch.set()
            await asyncio.gather(first, second)

        assert fetch_calls == 2
        mock_send.assert_awaited_once()


# -- event content structure --


@pytest.mark.asyncio
class TestSendSummaryEvent:
    """Verify the Matrix event payload structure."""

    async def test_event_content_structure(self) -> None:
        """Verify the public summary-send API writes the expected event payload."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$s1", room_id="!r:x"))
        conversation_cache = AsyncMock()
        conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$reply1")
        conversation_cache.notify_outbound_message = Mock()

        result = await send_thread_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary="Discussed deployment plan",
            message_count=15,
            model_name="haiku",
            conversation_cache=conversation_cache,
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
        assert relates_to["m.in_reply_to"] == {"event_id": "$reply1"}

        meta = content["io.mindroom.thread_summary"]
        assert meta["version"] == 1
        assert meta["summary"] == "Discussed deployment plan"
        assert meta["message_count"] == 15
        assert meta["model"] == "haiku"
        assert "generated_at" in meta
        conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with("!room:x", "$root1")
        conversation_cache.notify_outbound_message.assert_called_once_with("!room:x", "$s1", content)

    async def test_event_content_truncates_overlong_summary(self) -> None:
        """Overlong summaries should be truncated before sending to Matrix."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$s1", room_id="!r:x"))
        conversation_cache = AsyncMock()
        conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$reply1")
        conversation_cache.notify_outbound_message = Mock()
        summary = "x" * (THREAD_SUMMARY_MAX_LENGTH + 1)

        result = await send_thread_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary=summary,
            message_count=15,
            model_name="haiku",
            conversation_cache=conversation_cache,
        )

        assert result == "$s1"
        content = client.room_send.call_args.kwargs["content"]
        truncated_summary = ("x" * (THREAD_SUMMARY_MAX_LENGTH - 3)) + "..."
        assert content["body"] == truncated_summary
        assert len(content["body"]) == THREAD_SUMMARY_MAX_LENGTH
        assert content["io.mindroom.thread_summary"]["summary"] == truncated_summary

    async def test_send_failure_returns_none(self) -> None:
        """Return None when room_send fails."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendError(message="forbidden"))
        conversation_cache = AsyncMock()
        conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$reply1")
        conversation_cache.notify_outbound_message = Mock()

        result = await send_thread_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary="test",
            message_count=5,
            model_name="default",
            conversation_cache=conversation_cache,
        )

        assert result is None
        conversation_cache.notify_outbound_message.assert_not_called()

    async def test_latest_thread_lookup_failure_falls_back_to_thread_root(self) -> None:
        """Summary sending should remain threaded when latest-event lookup fails."""
        client = _mock_client()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$s1", room_id="!r:x"))
        conversation_cache = AsyncMock()
        conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(side_effect=RuntimeError("lookup boom"))
        conversation_cache.notify_outbound_message = Mock()

        result = await send_thread_summary_event(
            client,
            room_id="!room:x",
            thread_id="$root1",
            summary="test",
            message_count=5,
            model_name="default",
            conversation_cache=conversation_cache,
        )

        assert result == "$s1"
        relates_to = client.room_send.call_args.kwargs["content"]["m.relates_to"]
        assert relates_to["event_id"] == "$root1"
        assert relates_to["m.in_reply_to"] == {"event_id": "$root1"}
        conversation_cache.notify_outbound_message.assert_called_once()


@pytest.mark.asyncio
class TestSetManualThreadSummary:
    """Direct tests for the shared manual summary write path."""

    async def test_sets_summary_and_updates_cache(self) -> None:
        """Manual summary writes should normalize text, count non-summary messages, and update the cache."""
        client = _mock_client()
        conversation_cache = AsyncMock()
        conversation_cache.get_thread_history.return_value = [
            *_make_thread_history(3),
            _make_summary_notice_message("$root1", message_count=2),
        ]

        with patch(
            "mindroom.thread_summary.send_thread_summary_event",
            new=AsyncMock(return_value="$summary1"),
        ) as mock_send:
            result = await set_manual_thread_summary(
                client,
                "!room:x",
                "$root1",
                "  # **Fix** [ISSUE-116](http://example.com)  ",
                conversation_cache=conversation_cache,
            )

        assert result.event_id == "$summary1"
        assert result.summary == "Fix ISSUE-116"
        assert result.message_count == _count_non_summary_messages(conversation_cache.get_thread_history.return_value)
        mock_send.assert_awaited_once_with(
            client,
            "!room:x",
            "$root1",
            "Fix ISSUE-116",
            3,
            "manual",
            conversation_cache,
        )
        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$root1")] == 3

    async def test_send_failure_raises_and_leaves_cache_unchanged(self) -> None:
        """A failed manual summary send should not advance the cached threshold baseline."""
        client = _mock_client()
        conversation_cache = AsyncMock()
        conversation_cache.get_thread_history.return_value = _make_thread_history(5)
        update_last_summary_count("!room:x", "$root1", 2)

        with (
            patch(
                "mindroom.thread_summary.send_thread_summary_event",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ThreadSummaryWriteError, match=r"Failed to send thread summary event\."),
        ):
            await set_manual_thread_summary(
                client,
                "!room:x",
                "$root1",
                "failed write",
                conversation_cache=conversation_cache,
            )

        assert _last_summary_counts[thread_summary_cache_key("!room:x", "$root1")] == 2

    async def test_fetch_failure_raises_before_send(self) -> None:
        """A failed history fetch should raise the shared manual-summary fetch error."""
        client = _mock_client()
        conversation_cache = AsyncMock()
        conversation_cache.get_thread_history.side_effect = TimeoutError("timed out")

        with pytest.raises(ThreadSummaryWriteError, match=r"Failed to fetch thread history for the target thread\."):
            await set_manual_thread_summary(
                client,
                "!room:x",
                "$root1",
                "done",
                conversation_cache=conversation_cache,
            )


class TestBuildConversationText:
    """Tests for conversation text building and truncation."""

    def test_short_thread_not_truncated(self) -> None:
        """Threads below the truncation threshold are passed through intact."""
        history = _make_thread_history(5)
        text = _build_conversation_text(history)
        assert "omitted" not in text
        assert text.count("\n") == 4  # 5 messages, 4 newlines

    def test_long_thread_truncated(self) -> None:
        """Threads above the truncation threshold are sampled with an omission note."""
        count = _MAX_MESSAGES_BEFORE_TRUNCATION + 10
        history = _make_thread_history(count)
        text = _build_conversation_text(history)
        assert "omitted" in text
        omitted = count - 2 * _TRUNCATION_SAMPLE_SIZE
        assert f"{omitted} messages omitted" in text

    def test_exactly_at_threshold_not_truncated(self) -> None:
        """Exactly at the threshold boundary, no truncation occurs."""
        history = _make_thread_history(_MAX_MESSAGES_BEFORE_TRUNCATION)
        text = _build_conversation_text(history)
        assert "omitted" not in text


@pytest.mark.asyncio
class TestGenerateSummary:
    """Tests for basic summary generation flow."""

    async def test_prompt_instructions_and_delimiters(self) -> None:
        """The summarizer should keep the anti-echo instructions and wrap thread text explicitly."""
        history = _make_thread_history(3)
        config = _mock_config()
        rp = _mock_runtime_paths()
        mock_model = object()
        mock_response = MagicMock()
        mock_response.content = _ThreadSummary(summary="🧵 ISSUE-133 prompt preserved")

        with (
            patch("mindroom.thread_summary.get_model_instance", return_value=mock_model),
            patch("mindroom.thread_summary.Agent") as mock_agent_cls,
            patch("mindroom.thread_summary.cached_agent_run", new=AsyncMock(return_value=mock_response)) as mock_run,
        ):
            result = await _generate_summary(history, config, rp)

        assert result == "🧵 ISSUE-133 prompt preserved"
        instructions = "\n".join(mock_agent_cls.call_args.kwargs["instructions"])
        assert "NOVEL summary" in instructions
        assert "Do NOT copy" in instructions

        conversation = _build_conversation_text(history)
        prompt = mock_run.await_args.kwargs["run_input"]
        assert prompt == f"<thread_messages>\n{conversation}\n</thread_messages>\n\nSummarize the above thread."

    async def test_summary_returned(self) -> None:
        """A valid summary is returned directly."""
        history = _make_thread_history(5)
        config = _mock_config()
        rp = _mock_runtime_paths()
        mock_response = MagicMock()
        mock_response.content = _ThreadSummary(summary="\U0001f527 Auth deployment discussed and approved")

        with (
            patch("mindroom.thread_summary.get_model_instance"),
            patch("mindroom.thread_summary.Agent"),
            patch("mindroom.thread_summary.cached_agent_run", return_value=mock_response),
        ):
            result = await _generate_summary(history, config, rp)

        assert result == "\U0001f527 Auth deployment discussed and approved"

    async def test_none_content_returns_none(self) -> None:
        """When the agent returns None content, _generate_summary returns None."""
        history = _make_thread_history(5)
        config = _mock_config()
        rp = _mock_runtime_paths()
        mock_response = MagicMock()
        mock_response.content = None

        with (
            patch("mindroom.thread_summary.get_model_instance"),
            patch("mindroom.thread_summary.Agent"),
            patch("mindroom.thread_summary.cached_agent_run", return_value=mock_response),
        ):
            result = await _generate_summary(history, config, rp)

        assert result is None


# -- prior summary notice filtering --


class TestSummaryNoticeFiltering:
    """Prior io.mindroom.thread_summary events must be excluded from summaries."""

    def test_build_conversation_excludes_summary_notices(self) -> None:
        """Summary notices should not appear in conversation text."""
        history = [
            *_make_thread_history(3),
            _make_summary_notice_message("$thread1", message_count=3, event_id="$summary1"),
        ]
        text = _build_conversation_text(history)

        assert "Existing thread summary" not in text

    def test_summary_notice_detected(self) -> None:
        """_is_thread_summary_message correctly identifies summary notices."""
        notice = _make_summary_notice_message("$thread1", message_count=5)
        assert _is_thread_summary_message(notice)

    def test_regular_message_not_detected_as_summary(self) -> None:
        """Regular messages are not flagged as summary notices."""
        regular = _make_thread_history(1)[0]
        assert not _is_thread_summary_message(regular)
