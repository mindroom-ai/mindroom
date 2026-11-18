"""A conversation keeps the system prompt it last sent while its prompt cache may be reused, and hears what changed."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindroom.memory import ConversationRef, FreshPrompt, held_system_prompt, stamp_prompt_request
from mindroom.memory import _prompt_snapshots as snapshots

STORE = Path("prompt_snapshots")
HOUR = snapshots.prompt_cache_ttl_seconds(extended_cache_time=True)
MARGIN = snapshots._MARGIN_SECONDS


@pytest.fixture(autouse=True)
def _store_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the relative ``STORE`` each test writes inside its own directory."""
    monkeypatch.chdir(tmp_path)


def _send(
    *system: str,
    agent: str = "mind",
    session: str = "thread-1",
    summary: str | None = None,
    model: str = "anthropic/opus",
    tools: str = "tools-1",
    memory_path: str | None = "/workspace/MEMORY.md",
    memory: str = "",
    now: float = 0.0,
    ttl: float = HOUR,
    store: Path = STORE,
) -> tuple[tuple[str, ...], str]:
    """Return the system texts a turn sends, and what it is told changed, when its fresh prompt is ``system``."""
    return held_system_prompt(
        store,
        agent,
        ConversationRef(session, (summary, None)),
        FreshPrompt(system, model=model, tools=tools, memory_path=memory_path, memory_body=memory),
        ttl=ttl,
        now=now,
    )


def test_warm_conversation_keeps_its_prompt_and_hears_each_change_once() -> None:
    """A changed file must not change the prefix cached requests share; the change follows the conversation."""
    assert _send("role\nAGENTS v1", "date") == (("role\nAGENTS v1", "date"), "")

    system, changes = _send("role\nAGENTS v2", "date", now=10)
    assert system == ("role\nAGENTS v1", "date")
    assert "-AGENTS v1" in changes
    assert "+AGENTS v2" in changes

    # The conversation was told, so the same fresh prompt says nothing again.
    assert _send("role\nAGENTS v2", "date", now=20) == (("role\nAGENTS v1", "date"), "")

    # A later change is told against what the conversation last heard, not against the prompt it keeps.
    system, changes = _send("role\nAGENTS v3", "date", now=30)
    assert system == ("role\nAGENTS v1", "date")
    assert "-AGENTS v2" in changes
    assert "+AGENTS v3" in changes
    assert "v1" not in changes


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
    stamp_prompt_request(STORE, "mind", "thread-1", now=3000)
    stamp_prompt_request(STORE, "mind", "thread-1", now=6000)
    assert _send("v2", now=9000)[0] == ("v1",)

    _send("v1", session="quiet", now=0)
    assert _send("v2", session="quiet", now=9000)[0] == ("v2",)


def test_stamping_a_conversation_without_a_kept_prompt_does_nothing() -> None:
    """A run of a conversation that keeps no prompt, such as a team member's, stamps nothing into existence."""
    stamp_prompt_request(STORE, "mind", "unknown", now=1)
    assert not STORE.exists()


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
    """A memory added mid-conversation is told at the tail instead of reloading the prompt."""
    _send("memory: a", memory="a")
    system, changes = _send("memory: a\nb", memory="a\nb")
    assert system == ("memory: a",)
    assert "+b" in changes


def test_edited_or_deleted_memory_takes_the_fresh_prompt() -> None:
    """A memory the user asked to change or forget must not keep reaching the model."""
    _send("memory: a\nb", memory="a\nb")
    assert _send("memory: a", memory="a") == (("memory: a",), "")
    assert _send("memory: a2", memory="a2") == (("memory: a2",), "")
    # A line the conversation heard of at its tail counts too.
    _send("memory: a2\nb", memory="a2\nb")
    assert _send("memory: a2\nb2", memory="a2\nb2") == (("memory: a2\nb2",), "")


def test_empty_memory_takes_the_fresh_prompt_once_it_gains_lines() -> None:
    """An emptied file sends no header, so its first new line must load the header that points at the file."""
    _send("", memory="")
    assert _send("header\na", memory="header\na") == (("header\na",), "")


def test_prompts_are_kept_per_agent_conversation_and_memory_file() -> None:
    """Another conversation, agent, or requester's private memory never receives this conversation's prompt."""
    _send("v1", memory="a")
    assert _send("v2", memory="a", session="thread-2")[0] == ("v2",)
    assert _send("v2", memory="a", agent="other")[0] == ("v2",)
    assert _send("private", memory="a", memory_path="/private/MEMORY.md") == (("private",), "")


def test_idle_prompts_are_dropped() -> None:
    """Prompts of idle conversations are deleted, so the store stays as small as the active conversations."""
    _send("v1", session="old", now=0)
    _send("v1", session="new", now=HOUR + MARGIN)
    assert len(list(STORE.iterdir())) == 1
    assert _send("v2", session="new", now=HOUR + MARGIN + 1)[0] == ("v1",)


@pytest.mark.parametrize("content", ["[[", "{}", "null", '{"system": "v1"}', '{"system": ["v1"], "known": 3}'])
def test_unreadable_prompt_takes_the_fresh_one(content: str) -> None:
    """A malformed file costs one cache write, never the turn."""
    _send("v1")
    [kept] = STORE.iterdir()
    kept.write_text(content, encoding="utf-8")
    assert _send("v2") == (("v2",), "")
    assert _send("v3")[0] == ("v2",)


def test_store_that_cannot_be_written_sends_the_fresh_prompt() -> None:
    """A store path under a regular file fails every read and save, and turns still get their prompt."""
    Path("not-a-dir").write_text("", encoding="utf-8")
    assert _send("v1", store=Path("not-a-dir/prompts")) == (("v1",), "")
    assert _send("v2", store=Path("not-a-dir/prompts")) == (("v2",), "")
