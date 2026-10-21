"""A conversation keeps the MEMORY.md preamble it last sent while entries are added and its cache may be reused."""

from __future__ import annotations

from pathlib import Path

from mindroom.memory import ConversationRef
from mindroom.memory import _entrypoint_snapshots as snapshots

MEMORY = Path("/workspace/MEMORY.md")


def _preamble(
    text: str,
    *,
    agent: str = "mind",
    session: str = "thread-1",
    summary: str | None = None,
    path: Path = MEMORY,
    now: float = 0.0,
) -> str:
    """Return the preamble sent for a file whose inlined text is ``text``; the preamble names that text."""
    return snapshots.stable_entrypoint_preamble(
        agent,
        ConversationRef(session, (summary, None)),
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
    assert list(snapshots._snapshots) == [("mind", "new", MEMORY)]
