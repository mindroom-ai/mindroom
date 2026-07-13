"""Tests for the one-shot thread auto-tagging sidecar."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import thread_auto_tag, thread_tag_vocabulary
from mindroom.config.main import Config
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.thread_auto_tag import (
    _FIRST_EXCHANGE_MAX_COUNT_HINT,
    _FIRST_MESSAGE_MAX_CHARS,
    _first_thread_message_text,
    _generate_tags,
    _resolve_auto_tag_model_name,
    _ThreadAutoTags,
    run_thread_auto_tag,
    should_queue_thread_auto_tag,
)
from mindroom.thread_tags import ThreadTagRecord, ThreadTagsError, ThreadTagsState

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_auto_tag_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate module-level once-per-thread and vocabulary freshness state."""
    monkeypatch.setattr(thread_auto_tag, "_auto_tagged_threads", set())
    monkeypatch.setattr(thread_auto_tag, "_thread_locks", defaultdict(asyncio.Lock))
    monkeypatch.setattr(thread_tag_vocabulary, "_last_confirmed_fresh_boundary", None)


def _mark_vocabulary_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        thread_tag_vocabulary,
        "_last_confirmed_fresh_boundary",
        thread_tag_vocabulary._most_recent_rebuild_boundary(datetime.now(UTC), "UTC"),
    )


def _config(**defaults: object) -> Config:
    return Config(defaults=defaults)


def _history(*bodies: str) -> list[ResolvedVisibleMessage]:
    return [
        ResolvedVisibleMessage.synthetic(
            sender=f"@user{i}:localhost",
            body=body,
            timestamp=1700000000 + i * 1000,
            event_id=f"$event{i}",
        )
        for i, body in enumerate(bodies)
    ]


def _tagged_state(*tags: str) -> ThreadTagsState:
    return ThreadTagsState(
        room_id="!room:localhost",
        thread_root_id="$thread:localhost",
        tags={tag: ThreadTagRecord(set_by="@user:localhost", set_at=datetime(2026, 7, 12, tzinfo=UTC)) for tag in tags},
    )


def _runtime_paths(tmp_path: Path) -> MagicMock:
    rp = MagicMock()
    rp.storage_root = tmp_path
    return rp


# -- pre-queue gate --


class TestShouldQueueThreadAutoTag:
    """The cheap in-memory pre-queue gate."""

    def test_queues_when_vocabulary_check_is_due(self) -> None:
        """A due vocabulary check queues the task even for old, done threads."""
        thread_auto_tag._auto_tagged_threads.add("!room:localhost:$thread:localhost")

        assert should_queue_thread_auto_tag(
            "!room:localhost",
            "$thread:localhost",
            _config(),
            message_count_hint=100,
        )

    def test_skips_done_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A thread already handled this process lifetime is not re-queued."""
        _mark_vocabulary_fresh(monkeypatch)
        thread_auto_tag._auto_tagged_threads.add("!room:localhost:$thread:localhost")

        assert not should_queue_thread_auto_tag(
            "!room:localhost",
            "$thread:localhost",
            _config(),
            message_count_hint=2,
        )

    def test_skips_threads_past_first_exchange(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hint beyond the first-exchange cap skips queueing."""
        _mark_vocabulary_fresh(monkeypatch)

        assert not should_queue_thread_auto_tag(
            "!room:localhost",
            "$thread:localhost",
            _config(),
            message_count_hint=_FIRST_EXCHANGE_MAX_COUNT_HINT + 1,
        )

    @pytest.mark.parametrize("message_count_hint", [None, 2, _FIRST_EXCHANGE_MAX_COUNT_HINT])
    def test_queues_first_exchange_candidates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        message_count_hint: int | None,
    ) -> None:
        """First-exchange hints (or no hint) queue the auto-tag task."""
        _mark_vocabulary_fresh(monkeypatch)

        assert should_queue_thread_auto_tag(
            "!room:localhost",
            "$thread:localhost",
            _config(),
            message_count_hint=message_count_hint,
        )


# -- first-message extraction --


def test_first_thread_message_text_skips_summary_notices_and_truncates() -> None:
    """The opening message skips summary notices, empty bodies, and is truncated."""
    summary_notice = ResolvedVisibleMessage.synthetic(
        sender="@mindroom:localhost",
        body="🧵 summary",
        event_id="$summary",
        content={"io.mindroom.thread_summary": {"version": 1}},
    )
    empty = ResolvedVisibleMessage.synthetic(sender="@user:localhost", body="", event_id="$empty")
    long_body = "x" * (_FIRST_MESSAGE_MAX_CHARS + 100)
    real = ResolvedVisibleMessage.synthetic(sender="@user:localhost", body=long_body, event_id="$real")

    text = _first_thread_message_text([summary_notice, empty, real])

    assert text == "x" * _FIRST_MESSAGE_MAX_CHARS


def test_first_thread_message_text_returns_none_for_empty_history() -> None:
    """An empty history yields no opening message."""
    assert _first_thread_message_text([]) is None


# -- model resolution --


def test_resolve_auto_tag_model_name_fallback_chain() -> None:
    """Model resolution falls back from tagger to summary model to default."""
    assert _resolve_auto_tag_model_name(_config(thread_auto_tag_model="tagger")) == "tagger"
    assert _resolve_auto_tag_model_name(_config(thread_summary_model="haiku")) == "haiku"
    assert _resolve_auto_tag_model_name(_config()) == "default"


# -- tag generation --


@pytest.mark.asyncio
async def test_generate_tags_coerces_and_dedupes(tmp_path: Path) -> None:
    """Model output is coerced to canonical tag names and deduplicated."""
    response = MagicMock()
    response.content = _ThreadAutoTags(tags=["Follow Up", "follow-up", "BUG!"])

    with (
        patch("mindroom.thread_auto_tag.model_loading.get_model_instance", return_value=MagicMock()),
        patch("mindroom.thread_auto_tag.Agent"),
        patch("mindroom.thread_auto_tag.cached_agent_run", new=AsyncMock(return_value=response)),
    ):
        tags = await _generate_tags("first message", _config(), _runtime_paths(tmp_path))

    assert tags == ["follow-up", "bug"]


@pytest.mark.asyncio
async def test_generate_tags_returns_empty_for_unstructured_content(tmp_path: Path) -> None:
    """Unstructured model output yields no tags instead of failing."""
    response = MagicMock()
    response.content = "not structured"

    with (
        patch("mindroom.thread_auto_tag.model_loading.get_model_instance", return_value=MagicMock()),
        patch("mindroom.thread_auto_tag.Agent"),
        patch("mindroom.thread_auto_tag.cached_agent_run", new=AsyncMock(return_value=response)),
    ):
        assert await _generate_tags("first message", _config(), _runtime_paths(tmp_path)) == []


# -- run_thread_auto_tag --


def _run_kwargs(tmp_path: Path, *history_bodies: str) -> dict[str, object]:
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_history = AsyncMock(return_value=_history(*history_bodies))
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    return {
        "client": client,
        "room_id": "!room:localhost",
        "thread_id": "$thread:localhost",
        "config": _config(),
        "runtime_paths": _runtime_paths(tmp_path),
        "conversation_cache": conversation_cache,
    }


@pytest.mark.asyncio
async def test_run_thread_auto_tag_applies_tags_exactly_once(tmp_path: Path) -> None:
    """The sidecar tags a first-exchange thread once and never re-fires."""
    kwargs = _run_kwargs(tmp_path, "Please fix the login bug", "On it!")

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()) as mock_rebuild,
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock(return_value=["bug", "login"])) as mock_generate,
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock()) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)
        await run_thread_auto_tag(**kwargs, message_count_hint=3)

    mock_rebuild.assert_awaited()
    mock_generate.assert_awaited_once()
    assert [call.args[3] for call in mock_set.await_args_list] == ["bug", "login"]
    assert mock_set.await_args_list[0].kwargs == {"set_by": "@mindroom_general:localhost"}


@pytest.mark.asyncio
async def test_run_thread_auto_tag_only_fires_after_first_exchange(tmp_path: Path) -> None:
    """A thread past the first exchange is never tagged."""
    kwargs = _run_kwargs(tmp_path, "old thread")

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)) as mock_get,
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock(return_value=["bug"])) as mock_generate,
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock()) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=_FIRST_EXCHANGE_MAX_COUNT_HINT + 1)

    mock_get.assert_not_awaited()
    mock_generate.assert_not_awaited()
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_thread_auto_tag_skips_already_tagged_thread(tmp_path: Path) -> None:
    """A thread that already has tags is marked done without any model call."""
    kwargs = _run_kwargs(tmp_path, "hello")

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=_tagged_state("bug"))),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock()) as mock_generate,
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock()) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)

    mock_generate.assert_not_awaited()
    mock_set.assert_not_awaited()
    assert "!room:localhost:$thread:localhost" in thread_auto_tag._auto_tagged_threads


@pytest.mark.asyncio
async def test_run_thread_auto_tag_marks_done_when_generation_fails(tmp_path: Path) -> None:
    """A failed model call must not retry on every subsequent response."""
    kwargs = _run_kwargs(tmp_path, "hello")

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock()) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)

    mock_set.assert_not_awaited()
    assert "!room:localhost:$thread:localhost" in thread_auto_tag._auto_tagged_threads


@pytest.mark.asyncio
async def test_run_thread_auto_tag_empty_history_stays_retriggerable(tmp_path: Path) -> None:
    """A transiently empty history leaves the thread eligible for the next response."""
    kwargs = _run_kwargs(tmp_path)

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock()) as mock_generate,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)

    mock_generate.assert_not_awaited()
    assert "!room:localhost:$thread:localhost" not in thread_auto_tag._auto_tagged_threads


@pytest.mark.asyncio
async def test_run_thread_auto_tag_survives_vocabulary_rebuild_failure(tmp_path: Path) -> None:
    """Vocabulary upkeep failures must not block the tagging pass."""
    kwargs = _run_kwargs(tmp_path, "hello")

    with (
        patch(
            "mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock(return_value=["bug"])),
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock()) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)

    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_thread_auto_tag_continues_past_single_write_failure(tmp_path: Path) -> None:
    """One failed tag write must not drop the remaining tags."""
    kwargs = _run_kwargs(tmp_path, "hello")
    write_results: list[object] = [ThreadTagsError("write failed"), None]

    with (
        patch("mindroom.thread_auto_tag.maybe_rebuild_tag_vocabulary", new=AsyncMock()),
        patch("mindroom.thread_auto_tag.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_auto_tag._generate_tags", new=AsyncMock(return_value=["bug", "docs"])),
        patch("mindroom.thread_auto_tag.set_thread_tag", new=AsyncMock(side_effect=write_results)) as mock_set,
    ):
        await run_thread_auto_tag(**kwargs, message_count_hint=2)

    assert [call.args[3] for call in mock_set.await_args_list] == ["bug", "docs"]
