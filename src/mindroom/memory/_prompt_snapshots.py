"""Keep a conversation's system prompt unchanged while the provider can still reuse its prompt cache.

The system prompt precedes the conversation, so a changed one makes the provider read the whole conversation again.
A conversation therefore keeps the system texts it last sent while its cache may be warm, and hears what changed in
them since, such as an edited AGENTS.md, a new MEMORY.md entry, or the date, once, as a diff at its tail; a turn that
is prepared again, whose first attempt may have stored nothing, hears its diff again. It takes the fresh prompt when the cache is gone or misses anyway: idle past the model's cache lifetime, compacted history,
another model, or other tool definitions. An edited or deleted MEMORY.md line also takes it at once, so a memory the
user asked to forget is not shown again.

Each conversation's prompt is one file under the store directory, whose modification time is the conversation's last
model request, so a restart keeps both; a missing or unreadable file starts the conversation from the fresh prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from difflib import unified_diff
from typing import TYPE_CHECKING

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.yaml_io import write_text_atomic

if TYPE_CHECKING:
    from pathlib import Path

_SHORT_CACHE_SECONDS = 300
# A provider counts its cache lifetime from when it served the request, which is later than when it was stamped.
# Keeping a prompt whose cache is gone costs nothing; releasing one whose cache is warm rewrites the conversation.
_MARGIN_SECONDS = 120


@dataclass(frozen=True)
class ConversationRef:
    """The conversation a turn continues, and what its history was last compacted into when the turn started.

    ``compaction`` holds the conversation's text summary and newest native checkpoint; a change means a compaction
    rewrote the replayed history.
    """

    session_id: str
    compaction: tuple[str | None, str | None]


@dataclass(frozen=True)
class FreshPrompt:
    """A turn's freshly rendered system texts, and what a request sends before them or reads them with.

    ``model`` and ``tools`` identify the model and its tool definitions. ``memory_body`` is the inlined MEMORY.md of
    ``memory_path`` without its truncation marker, whose counts change as lines are added past a cap.
    """

    system: tuple[str, ...]
    model: str
    tools: str
    memory_path: str | None = None
    memory_body: str = ""


@dataclass(frozen=True)
class _Kept:
    """The system texts a conversation sends, and ``known``, the newest fresh prompt it has been told about.

    ``told`` is the turn that was last told of changes, and the diff it was told.
    """

    system: tuple[str, ...]
    known: FreshPrompt
    compaction: tuple[str | None, str | None]
    told: tuple[str | None, str] = (None, "")


def prompt_cache_ttl_seconds(*, extended_cache_time: bool) -> int:
    """Return how long a provider keeps a prompt cache that no request reads."""
    return PROMPT_CACHE_IDLE_SECONDS if extended_cache_time else _SHORT_CACHE_SECONDS


def _path(store: Path, agent_name: str, session_id: str) -> Path:
    key = f"{agent_name}\0{session_id}".encode()
    return store / f"{hashlib.sha256(key).hexdigest()}.json"


def stamp_prompt_request(store: Path, agent_name: str, session_id: str, *, now: float) -> None:
    """Record a model request of the conversation at ``now``, which keeps its prompt cache warm from then."""
    with suppress(OSError):
        os.utime(_path(store, agent_name, session_id), (now, now))


def _load(path: Path, now: float) -> tuple[_Kept, float] | None:
    """Return the conversation's kept prompt and how long ago its last request was, or None without a readable one."""
    try:
        idle = now - path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
        known = data["known"]
        summary, checkpoint = data["compaction"]
        told_turn, told = data["told"]
        kept = _Kept(
            tuple(map(str, data["system"])),
            FreshPrompt(
                tuple(map(str, known["system"])),
                str(known["model"]),
                str(known["tools"]),
                known["memory_path"],
                str(known["memory_body"]),
            ),
            (summary, checkpoint),
            (told_turn, str(told)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return kept, idle


def _only_added_lines(snapshot: str, fresh: str) -> bool:
    return fresh == snapshot or fresh.startswith(f"{snapshot}\n")


def _changes(known: tuple[str, ...], fresh: tuple[str, ...]) -> str:
    """Return a unified diff from ``known`` to ``fresh`` without its file header, or ``""`` when they are the same."""
    if known == fresh:
        return ""
    before, after = ("\n\n".join(texts).splitlines() for texts in (known, fresh))
    return "\n".join(list(unified_diff(before, after, lineterm="", n=1))[2:])


def held_system_prompt(
    store: Path,
    agent_name: str,
    conversation: ConversationRef,
    fresh: FreshPrompt,
    *,
    turn: str | None,
    ttl: float,
    now: float,
) -> tuple[tuple[str, ...], str]:
    """Return the system texts this turn sends and a diff of what changed since the conversation last heard.

    The texts are the ones the conversation last sent while its prompt cache may be warm and would still match, and
    ``fresh.system`` otherwise; the diff is ``""`` when nothing changed or the fresh texts are sent.
    ``turn`` identifies the turn across its attempts, so a later attempt is told what an earlier one was; None for a
    turn nothing identifies.
    ``ttl`` is the model's cache lifetime and ``now`` wall-clock seconds, so idle times count across restarts.
    Comparing ``memory_path`` keeps one requester's private memory out of another requester's turns.
    """
    with suppress(OSError):
        for other in store.iterdir():
            if now - other.stat().st_mtime >= PROMPT_CACHE_IDLE_SECONDS + _MARGIN_SECONDS:
                other.unlink()
    path = _path(store, agent_name, conversation.session_id)
    loaded = _load(path, now)
    kept, changes, saved = _Kept(fresh.system, fresh, conversation.compaction), "", False
    if loaded is not None:
        previous, idle = loaded
        known = previous.known
        if (
            idle < ttl + _MARGIN_SECONDS
            and previous.compaction == conversation.compaction
            and (known.model, known.tools, known.memory_path) == (fresh.model, fresh.tools, fresh.memory_path)
            and _only_added_lines(known.memory_body, fresh.memory_body)
        ):
            new = _changes(known.system, fresh.system)
            repeated = previous.told[1] if turn is not None and previous.told[0] == turn else ""
            changes = "\n".join(part for part in (repeated, new) if part)
            told = (turn, changes) if new else previous.told
            kept, saved = replace(previous, known=fresh, told=told), known == fresh
    with suppress(OSError):
        if not saved:
            store.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps(asdict(kept)))
        os.utime(path, (now, now))
    return kept.system, changes
