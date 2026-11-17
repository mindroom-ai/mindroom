"""A conversation keeps the MEMORY.md preamble it last sent while entries are added and its cache may be reused."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindroom.memory import ConversationRef
from mindroom.memory import _entrypoint_snapshots as snapshots

MEMORY = Path("/workspace/MEMORY.md")
STORE = Path("snapshots.json")


@pytest.fixture(autouse=True)
def _store_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the relative ``STORE`` each test writes inside its own directory."""
    monkeypatch.chdir(tmp_path)


def _preamble(
    text: str,
    *,
    agent: str = "mind",
    session: str = "thread-1",
    summary: str | None = None,
    path: Path = MEMORY,
    now: float = 0.0,
    store: Path = STORE,
) -> str:
    """Return the preamble sent for a file whose inlined text is ``text``; the preamble names that text."""
    return snapshots.stable_entrypoint_preamble(
        agent,
        ConversationRef(session, (summary, None)),
        path,
        text,
        f"preamble of {text!r}",
        store=store,
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
    assert _preamble("a") == "preamble of 'a'"
    assert _preamble("a\nb") == "preamble of 'a'"


def test_idle_conversation_loads_the_file_again() -> None:
    """Once the cache has gone cold, a fresh file costs nothing, so the conversation catches up."""
    _preamble("a", now=0)
    assert _preamble("a\nb", now=3600) == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc", now=3700) == "preamble of 'a\\nb'"


def test_compaction_loads_the_file_again() -> None:
    """Compaction rewrites the replayed history, so its cache misses after the system prompt anyway."""
    _preamble("a", summary=None)
    assert _preamble("a\nb", summary="first summary") == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc", summary="first summary") == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc\nd", summary="second summary") == "preamble of 'a\\nb\\nc\\nd'"


def test_snapshots_are_kept_per_agent_conversation_and_file() -> None:
    """Another conversation, agent, or private memory file never receives this conversation's copy."""
    _preamble("a")
    assert _preamble("a\nb", session="thread-2") == "preamble of 'a\\nb'"
    assert _preamble("a\nb", agent="other") == "preamble of 'a\\nb'"
    assert _preamble("private", path=Path("/private/MEMORY.md")) == "preamble of 'private'"
    assert _preamble("a\nb") == "preamble of 'a'"


def test_idle_snapshots_are_dropped() -> None:
    """Copies of idle conversations are released, so the store stays as small as the active conversations."""
    _preamble("a", session="old", now=0)
    _preamble("a", session="new", now=4000)
    assert list(snapshots._snapshots[STORE.resolve()]) == [("mind", "new", MEMORY)]


@pytest.mark.parametrize(
    ("later", "kept"),
    [
        ({"text": "a\nb\nc", "now": 3000}, True),
        ({"text": "a\nb2"}, False),
        ({"text": "a"}, False),
        ({"text": "a\nb\nc", "now": 3600}, False),
        ({"text": "a\nb\nc", "summary": "compacted"}, False),
        ({"text": "a\nb\nc", "session": "thread-2"}, False),
        ({"text": "a\nb\nc", "path": Path("/private/MEMORY.md")}, False),
    ],
    ids=["added-line", "edited-line", "deleted-line", "idle", "compacted", "other-session", "private-file"],
)
def test_snapshots_keep_their_rules_across_a_restart(later: dict, kept: bool) -> None:
    """A restart keeps a warm conversation's preamble, and every reason to load the file again still applies."""
    _preamble("a\nb")
    snapshots._snapshots.clear()
    assert _preamble(**later) == ("preamble of 'a\\nb'" if kept else f"preamble of {later['text']!r}")


def test_each_store_restores_and_keeps_only_its_own_copies() -> None:
    """A store never receives another store's private copies, and each one restores its own."""
    first, second = Path("first.json"), Path("second.json")
    _preamble("a", session="in-second", store=second)
    snapshots._snapshots.clear()
    _preamble("a", session="in-first", store=first)
    assert _preamble("a\nb", session="in-second", store=second) == "preamble of 'a'"
    assert "in-second" not in first.read_text(encoding="utf-8")
    assert "in-first" not in second.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    ["[[", "{}", "null", '[["mind"]]', '[["mind","thread-1","/m","p","b",null,null,"x"]]'],
)
def test_unreadable_store_loads_the_file_again(content: str) -> None:
    """A malformed store costs one cache write, never the turn."""
    STORE.write_text(content, encoding="utf-8")
    assert _preamble("a\nb") == "preamble of 'a\\nb'"


def test_store_that_cannot_be_read_or_saved_keeps_copies_in_memory() -> None:
    """A store path under a regular file fails every read and save, and turns still keep their preamble."""
    Path("not-a-dir").write_text("", encoding="utf-8")
    assert _preamble("a", store=Path("not-a-dir/snapshots.json")) == "preamble of 'a'"
    assert _preamble("a\nb", store=Path("not-a-dir/snapshots.json")) == "preamble of 'a'"
