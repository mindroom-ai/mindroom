"""A conversation keeps the system prompt it last sent while its prompt cache may be reused, and hears what changed."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindroom.memory import (
    ConversationRef,
    FreshPrompt,
    held_system_prompt,
    prompt_snapshot_path,
    stamp_prompt_request,
)
from mindroom.memory import _prompt_snapshots as snapshots

DATABASE = Path("agents/mind/sessions/mind.db")
_STORE = prompt_snapshot_path(DATABASE, "thread-1").parent
HOUR = snapshots.prompt_cache_ttl_seconds(extended_cache_time=True)
MARGIN = snapshots._MARGIN_SECONDS


@pytest.fixture(autouse=True)
def _store_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the relative ``DATABASE`` each test's prompts lie beside inside its own directory."""
    monkeypatch.chdir(tmp_path)


def _send(
    *system: str,
    session: str = "thread-1",
    summary: str | None = None,
    model: str = "anthropic/opus",
    tools: str = "tools-1",
    memory_path: str | None = "/workspace/MEMORY.md",
    memory: str = "",
    now: float = 0.0,
    ttl: float = HOUR,
    database: Path = DATABASE,
) -> tuple[tuple[str, ...], str]:
    """Return the system texts a turn sends, and what it is told changed, when its fresh prompt is ``system``."""
    return held_system_prompt(
        prompt_snapshot_path(database, session),
        ConversationRef(session, (summary, None)),
        FreshPrompt(system, model=model, tools=tools, memory_path=memory_path, memory_body=memory),
        ttl=ttl,
        now=now,
    )


def test_warm_conversation_keeps_its_prompt_and_every_turn_hears_the_whole_difference() -> None:
    """A changed file must not change the prefix cached requests share; the change ends each turn's requests.

    Nothing records what a turn was told, so a turn that fails loses nothing: the next one is told the same.
    """
    assert _send("role\nAGENTS v1", "date") == (("role\nAGENTS v1", "date"), "")

    system, changes = _send("role\nAGENTS v2", "date", now=10)
    assert system == ("role\nAGENTS v1", "date")
    assert "-AGENTS v1" in changes
    assert "+AGENTS v2" in changes

    assert _send("role\nAGENTS v2", "date", now=20) == (system, changes)

    # The difference is always to the prompt the conversation keeps, never to what an earlier turn was told.
    system, changes = _send("role\nAGENTS v3", "date", now=30)
    assert system == ("role\nAGENTS v1", "date")
    assert "-AGENTS v1" in changes
    assert "+AGENTS v3" in changes
    assert "v2" not in changes

    # A change that is taken back leaves nothing to tell.
    assert _send("role\nAGENTS v1", "date", now=40) == (("role\nAGENTS v1", "date"), "")


def test_unchanged_prompt_tells_nothing() -> None:
    """A turn whose fresh prompt is the kept one adds no note."""
    _send("a", "b")
    assert _send("a", "b", now=5) == (("a", "b"), "")


def test_idle_conversation_takes_the_fresh_prompt() -> None:
    """Once the cache is surely gone, the fresh prompt costs nothing, so the conversation catches up."""
    _send("v1", now=0)
    assert _send("v2", now=HOUR + MARGIN - 1)[0] == ("v1",)
    assert _send("v3", now=2 * (HOUR + MARGIN) - 1) == (("v3",), "")


def test_idle_time_follows_the_models_cache_lifetime() -> None:
    """A model whose cache lasts five minutes releases its prompt after five minutes, not after an hour."""
    short = snapshots.prompt_cache_ttl_seconds(extended_cache_time=False)
    assert (short, HOUR) == (300, 3600)
    _send("v1", now=0, ttl=short)
    assert _send("v2", now=short + MARGIN - 1, ttl=short)[0] == ("v1",)
    assert _send("v3", now=2 * (short + MARGIN), ttl=short) == (("v3",), "")


def test_every_model_request_restarts_the_idle_time() -> None:
    """A long tool loop keeps the cache warm with each request, so its next turn still keeps the prompt."""
    _send("v1", now=0)
    stamp_prompt_request(prompt_snapshot_path(DATABASE, "thread-1"), now=3000)
    stamp_prompt_request(prompt_snapshot_path(DATABASE, "thread-1"), now=6000)
    assert _send("v2", now=9000)[0] == ("v1",)

    _send("v1", session="quiet", now=0)
    assert _send("v2", session="quiet", now=9000)[0] == ("v2",)


def test_stamping_a_conversation_without_a_kept_prompt_does_nothing() -> None:
    """A run of a conversation that keeps no prompt, such as a team member's, stamps nothing into existence."""
    stamp_prompt_request(prompt_snapshot_path(DATABASE, "unknown"), now=1)
    assert not DATABASE.parent.parent.exists()


@pytest.mark.parametrize(
    "changed",
    [{"summary": "compacted"}, {"model": "openai/sol"}, {"tools": "tools-2"}],
    ids=["compaction", "model", "tools"],
)
def test_a_changed_prefix_takes_the_fresh_prompt(changed: dict) -> None:
    """Compaction, another model, and other tools miss the cache anyway, so nothing is gained by keeping the prompt."""
    _send("v1")
    assert _send("v2", **changed) == (("v2",), "")
    # The fresh prompt is the kept one from then on.
    assert _send("v3", **changed)[0] == ("v2",)


def test_memory_that_only_gained_lines_keeps_the_prompt() -> None:
    """A memory added mid-conversation ends the turn's requests instead of reloading the prompt."""
    _send("memory: a", memory="a")
    system, changes = _send("memory: a\nb", memory="a\nb")
    assert system == ("memory: a",)
    assert "+b" in changes


def test_edited_or_deleted_memory_takes_the_fresh_prompt() -> None:
    """A memory the user asked to change or forget must not keep reaching the model."""
    _send("memory: a\nb", memory="a\nb")
    assert _send("memory: a", memory="a") == (("memory: a",), "")
    assert _send("memory: a2", memory="a2") == (("memory: a2",), "")


def test_a_memory_added_and_deleted_again_is_never_kept() -> None:
    """A line a turn only heard of at its tail leaves no trace once it is deleted or replaced."""
    _send("memory: a", memory="a")
    assert "+secret" in _send("memory: a\nsecret", memory="a\nsecret")[1]
    assert _send("memory: a", memory="a") == (("memory: a",), "")
    system, changes = _send("memory: a\nother", memory="a\nother")
    assert system == ("memory: a",)
    assert "secret" not in changes
    assert all("secret" not in kept.read_text(encoding="utf-8") for kept in _STORE.glob("*.json"))


def test_empty_memory_takes_the_fresh_prompt_once_it_gains_lines() -> None:
    """An emptied file sends no header, so its first new line must load the header that points at the file."""
    _send("", memory="")
    assert _send("header\na", memory="header\na") == (("header\na",), "")


def test_prompts_are_kept_per_state_root_conversation_and_memory_file() -> None:
    """Another conversation, another requester's private instance, or another memory file never gets this prompt.

    Two private instances of one agent can share a session id; each keeps its prompts beside its own database.
    """
    _send("alice's private context", memory="a")
    assert _send("v2", memory="a", session="thread-2")[0] == ("v2",)
    bob = Path("private/bob/sessions/mind.db")
    assert _send("bob's private context", memory="a", database=bob) == (("bob's private context",), "")
    assert "alice" not in "".join(kept.read_text(encoding="utf-8") for kept in bob.parent.parent.rglob("*.json"))
    assert _send("private", memory="a", memory_path="/private/MEMORY.md") == (("private",), "")


def test_idle_prompts_are_dropped() -> None:
    """Prompts of idle conversations are deleted, so the store stays as small as the active conversations."""
    _send("v1", session="old", now=0)
    _send("v1", session="new", now=HOUR + MARGIN)
    assert len(list(_STORE.glob("*.json"))) == 1
    assert _send("v2", session="new", now=HOUR + MARGIN + 1)[0] == ("v1",)


@pytest.mark.parametrize("content", ["[[", "{}", "null", '{"prompt": "v1"}', '{"prompt": {"system": ["v1"]}}'])
def test_unreadable_prompt_takes_the_fresh_one(content: str) -> None:
    """A malformed file costs one cache write, never the turn."""
    _send("v1")
    [kept] = _STORE.glob("*.json")
    kept.write_text(content, encoding="utf-8")
    assert _send("v2") == (("v2",), "")
    assert _send("v3")[0] == ("v2",)


def test_store_that_cannot_be_written_sends_the_fresh_prompt() -> None:
    """A store path under a regular file fails every read and save, and turns still get their prompt."""
    Path("not-a-dir").write_text("", encoding="utf-8")
    assert _send("v1", database=Path("not-a-dir/sessions/mind.db")) == (("v1",), "")
    assert _send("v2", database=Path("not-a-dir/sessions/mind.db")) == (("v2",), "")
