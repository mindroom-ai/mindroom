"""A conversation keeps the MEMORY.md preamble it last sent while entries are added and its cache may be reused."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindroom.memory import ConversationRef, entrypoint_snapshot_path, prompt_cache_ttl_seconds
from mindroom.memory import _entrypoint_snapshots as snapshots

MEMORY = Path("/workspace/MEMORY.md")
HOUR = prompt_cache_ttl_seconds(extended_cache_time=True)
MARGIN = snapshots._MARGIN_SECONDS


@pytest.fixture(autouse=True)
def _state_root_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the relative state roots each test writes inside its own directory."""
    monkeypatch.chdir(tmp_path)


def _snapshot(session: str = "thread-1", root: str = "agent") -> Path:
    return entrypoint_snapshot_path(Path(root) / "sessions" / "agent.db", session)


def _preamble(
    text: str,
    *,
    session: str = "thread-1",
    summary: str | None = None,
    path: Path = MEMORY,
    now: float = 0.0,
    root: str = "agent",
    ttl: float = HOUR,
) -> str:
    """Return the preamble sent for a file whose inlined text is ``text``; the preamble names that text."""
    return snapshots.stable_entrypoint_preamble(
        ConversationRef((summary, None), _snapshot(session, root), ttl),
        path,
        text,
        f"preamble of {text!r}",
        now=now,
    )


def test_active_conversation_keeps_its_preamble_while_entries_are_added() -> None:
    """A memory added mid-conversation must not change the system prompt its cached requests share."""
    assert _preamble("a", now=0) == "preamble of 'a'"
    assert _preamble("a\nb", now=3000) == "preamble of 'a'"
    # Each turn starts the idle window again.
    assert _preamble("a\nb\nc", now=6000) == "preamble of 'a'"


def test_edited_or_deleted_lines_load_the_file_again() -> None:
    """A memory the user asked to change or forget must not keep reaching the model."""
    _preamble("a\nb")
    assert _preamble("a") == "preamble of 'a'"
    assert _preamble("a2") == "preamble of 'a2'"
    assert _preamble("a2\nb") == "preamble of 'a2'"


def test_empty_copy_loads_the_file_once_it_gains_lines() -> None:
    """An emptied file sends no header, so its first new line must load the header that points at the file."""
    _preamble("")
    assert _preamble("header\na") == "preamble of 'header\\na'"


@pytest.mark.parametrize(
    ("extended_cache_time", "lifetime"),
    [(True, 3600), (False, 300)],
    ids=["one-hour-cache", "five-minute-cache"],
)
def test_idle_conversation_loads_the_file_again(extended_cache_time: bool, lifetime: int) -> None:
    """Once its model's cache has gone cold, a fresh file costs nothing, so the conversation catches up.

    The copy is kept a margin longer than the cache: keeping it for a cold cache costs nothing, and loading the
    file under a warm one rewrites the conversation.
    """
    ttl = prompt_cache_ttl_seconds(extended_cache_time=extended_cache_time)
    assert ttl == lifetime
    _preamble("a", now=0, ttl=ttl)
    assert _preamble("a\nb", now=lifetime + MARGIN - 1, ttl=ttl) == "preamble of 'a'"
    assert _preamble("a\nb\nc", now=2 * (lifetime + MARGIN) - 1, ttl=ttl) == "preamble of 'a\\nb\\nc'"


def test_every_model_request_starts_the_idle_time_again() -> None:
    """A long turn reads the cache on each request, so its conversation stays warm past the turn's start."""
    _preamble("a", now=0)
    snapshots.stamp_entrypoint_snapshot(_snapshot(), now=3000)
    assert _preamble("a\nb", now=3000 + HOUR) == "preamble of 'a'"
    # A request of a conversation that keeps no copy has nothing to stamp.
    snapshots.stamp_entrypoint_snapshot(_snapshot("unknown"), now=0)
    assert not _snapshot("unknown").exists()


def test_compaction_loads_the_file_again() -> None:
    """Compaction rewrites the replayed history, so its cache misses after the system prompt anyway."""
    _preamble("a", summary=None)
    assert _preamble("a\nb", summary="first summary") == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc", summary="first summary") == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc\nd", summary="second summary") == "preamble of 'a\\nb\\nc\\nd'"


def test_snapshots_are_kept_per_state_root_conversation_and_file() -> None:
    """Another conversation, requester's state root, or memory file never receives this conversation's copy."""
    _preamble("a")
    assert _preamble("a\nb", session="thread-2") == "preamble of 'a\\nb'"
    assert _preamble("secret", root="bob/agent") == "preamble of 'secret'"
    assert _preamble("a\nb") == "preamble of 'a'"
    assert _preamble("other file", path=Path("/private/MEMORY.md")) == "preamble of 'other file'"
    assert "secret" not in "".join(path.read_text(encoding="utf-8") for path in _snapshot().parent.iterdir())


def test_idle_snapshots_are_dropped() -> None:
    """Copies of idle conversations are released, so the store stays as small as the active conversations."""
    _preamble("a", session="old", now=0)
    _preamble("a", session="new", now=HOUR + MARGIN)
    assert list(_snapshot().parent.glob("*.json")) == [_snapshot("new")]


@pytest.mark.parametrize("content", ["[[", "{}", "null", '["/workspace/MEMORY.md"]'])
def test_unreadable_snapshot_loads_the_file_again(content: str) -> None:
    """A malformed copy costs one cache write, never the turn."""
    _preamble("a")
    _snapshot().write_text(content, encoding="utf-8")
    assert _preamble("a\nb") == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc") == "preamble of 'a\\nb'"


def test_state_root_that_cannot_be_written_sends_the_fresh_preamble() -> None:
    """A state root under a regular file fails every read and save, and turns still get their preamble."""
    Path("not-a-dir").write_text("", encoding="utf-8")
    assert _preamble("a", root="not-a-dir/agent") == "preamble of 'a'"
    assert _preamble("a\nb", root="not-a-dir/agent") == "preamble of 'a\\nb'"
