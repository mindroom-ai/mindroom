"""Keep a conversation's inlined MEMORY.md unchanged while entries are added and its prompt cache can be reused.

The preamble is part of the system prompt, so a changed preamble makes the provider re-read the whole
conversation after it. A conversation therefore keeps the preamble it last sent while the file only gains lines,
until it idles past the cache lifetime, its history is compacted, or the process restarts; the conversation
already shows the agent its own memory writes as tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Most provider prompt caches expire within an hour idle; after that the request misses anyway.
_IDLE_REFRESH_SECONDS = 3600.0


@dataclass(frozen=True)
class ConversationRef:
    """The conversation a turn continues: its session, and its history summary when the turn started."""

    session_id: str
    history_summary: str | None


@dataclass(frozen=True)
class _Snapshot:
    preamble: str
    text: str
    history_summary: str | None
    used_at: float


# Best effort: a restart starts every conversation from the file again.
_snapshots: dict[tuple[str, str, Path], _Snapshot] = {}


def _only_added_lines(snapshot: str, fresh: str) -> bool:
    return fresh == snapshot or not snapshot or fresh.startswith(f"{snapshot}\n")


def stable_entrypoint_preamble(
    agent_name: str,
    conversation: ConversationRef,
    source_path: Path,
    fresh_text: str,
    fresh_preamble: str,
    *,
    now: float,
) -> str:
    """Return the preamble this conversation last sent for the file, or ``fresh_preamble`` when reuse would mislead.

    ``fresh_text`` is the file's inlined text this turn.
    A compaction rewrites the replayed history, so the conversation's cache misses after the system prompt anyway.
    An edited or deleted line loads at once, so a memory the user asked to forget is not shown again.
    Keying by the file keeps one requester's private memory out of another requester's turns.
    """
    for key in [key for key, snapshot in _snapshots.items() if now - snapshot.used_at >= _IDLE_REFRESH_SECONDS]:
        del _snapshots[key]
    key = (agent_name, conversation.session_id, source_path)
    snapshot = _snapshots.get(key)
    if (
        snapshot is not None
        and snapshot.history_summary == conversation.history_summary
        and _only_added_lines(snapshot.text, fresh_text)
    ):
        preamble, text = snapshot.preamble, snapshot.text
    else:
        preamble, text = fresh_preamble, fresh_text
    _snapshots[key] = _Snapshot(preamble, text, conversation.history_summary, now)
    return preamble
