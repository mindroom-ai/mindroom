"""A conversation keeps the MEMORY.md preamble it last sent while entries are added and its cache may be reused."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS as HOUR
from mindroom.memory import ConversationRef, entrypoint_snapshot_path
from mindroom.memory import _entrypoint_snapshots as snapshots

MEMORY = Path("/workspace/MEMORY.md")


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
    frame: tuple[str, ...] = ("header",),
    capped: bool = False,
) -> str:
    """Return the preamble sent for a file whose inlined text is ``text``; the preamble names that text.

    Its body is ``text`` under the ``frame`` prompts, and a ``capped`` file withheld lines to fit its preload limit.
    """
    return snapshots.stable_entrypoint_preamble(
        ConversationRef((summary, None), _snapshot(session, root)),
        path,
        "\n".join((*frame, text)) if text else "",
        f"preamble of {text!r}",
        frame=list(frame),
        lines=None if capped else tuple(text.splitlines()),
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


def test_lines_inserted_anywhere_keep_the_preamble() -> None:
    """Curating MEMORY.md adds notes at its top or inside a section, which must not change the system prompt."""
    _preamble("## Work\na\n## Home\nb")
    assert _preamble("new\n## Work\na\n## Home\nb") == "preamble of '## Work\\na\\n## Home\\nb'"
    assert _preamble("new\n## Work\na\na2\n## Home\nb\nc") == "preamble of '## Work\\na\\n## Home\\nb'"


def test_an_edited_middle_line_loads_the_file_again() -> None:
    """A line changed anywhere is no longer in the file, so the copy must not keep showing it."""
    _preamble("a\nb\nc")
    assert _preamble("new\na\nb2\nc") == "preamble of 'new\\na\\nb2\\nc'"


def test_a_deleted_or_moved_line_loads_the_file_again() -> None:
    """A forgotten line, even with others inserted around it, must not keep reaching the model."""
    _preamble("a\nb\nc")
    assert _preamble("a\nnew\nc") == "preamble of 'a\\nnew\\nc'"
    assert _preamble("c\na\nnew") == "preamble of 'c\\na\\nnew'"


def test_a_capped_copy_keeps_only_lines_added_at_its_end() -> None:
    """A capped copy cannot tell a line the cap withholds from a deleted one, so only appended lines keep it."""
    _preamble("head\nb\nc", capped=True)
    assert _preamble("head\nb\nc\nd", capped=True) == "preamble of 'head\\nb\\nc'"
    assert _preamble("head\nb\nx\nc\nd", capped=True) == "preamble of 'head\\nb\\nx\\nc\\nd'"
    # A whole copy whose file now exceeds a cap keeps only appended lines too.
    _preamble("a\nb", session="whole")
    assert _preamble("x\na\nb", session="whole", capped=True) == "preamble of 'x\\na\\nb'"


def test_a_changed_frame_loads_the_file_again() -> None:
    """A prompt override that only adds a line to the header or the kept-copy notice still changes the copy's frame."""
    _preamble("a", frame=("header", "notice"))
    assert _preamble("x\na", frame=("header\nadded", "notice")) == "preamble of 'x\\na'"
    assert _preamble("x\na\ny", frame=("header\nadded", "notice\nadded")) == "preamble of 'x\\na\\ny'"


def test_a_changed_frame_loads_a_cut_file_whose_body_is_unchanged() -> None:
    """A new limit or cut marker changes only the marker after the kept text, so the frame alone must reload it."""

    def preamble(frame: list[str], marker: str) -> str:
        return snapshots.stable_entrypoint_preamble(
            ConversationRef((None, None), _snapshot()),
            MEMORY,
            "header\na",
            f"header\na\n{marker}",
            frame=frame,
            lines=None,
            now=0.0,
        )

    preamble(["header", "marker for 50"], "cut at 50")
    assert preamble(["header", "marker for 60"], "cut at 60") == "header\na\ncut at 60"


def test_empty_copy_loads_the_file_once_it_gains_lines() -> None:
    """An emptied file sends no header, so its first new line must load the header that points at the file."""
    _preamble("")
    assert _preamble("header\na") == "preamble of 'header\\na'"


def test_idle_conversation_loads_the_file_again() -> None:
    """Once the cache has gone cold, a fresh file costs nothing, so the conversation catches up."""
    _preamble("a", now=0)
    assert _preamble("a\nb", now=HOUR) == "preamble of 'a\\nb'"
    assert _preamble("a\nb\nc", now=HOUR + 100) == "preamble of 'a\\nb'"


def test_a_request_that_sent_the_copy_starts_its_idle_time_again() -> None:
    """A long turn reads the cache on each request, so its conversation stays warm past the turn's start."""
    _preamble("a", now=0)
    snapshots.stamp_entrypoint_snapshot(_snapshot(), now=3000)
    assert _preamble("a\nb", now=3000 + HOUR - 1) == "preamble of 'a'"
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


def test_copies_idle_for_a_day_are_deleted() -> None:
    """A copy idle past the cache lifetime is not reused, and its file is deleted once it has been idle for a day."""
    _preamble("a", session="old", now=0)
    _preamble("a", session="other", now=2 * HOUR)
    assert _snapshot("old").exists()
    assert _preamble("a\nb", session="old", now=2 * HOUR) == "preamble of 'a\\nb'"
    _preamble("a", session="new", now=30 * HOUR)
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
