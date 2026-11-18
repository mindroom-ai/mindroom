"""Keep a conversation's system prompt unchanged while the provider can still reuse its prompt cache.

The system prompt precedes the conversation, so a changed one makes the provider read the whole conversation again.
A conversation therefore keeps the system texts it last sent while its cache may be warm. Every turn that keeps them
is given the difference to the fresh ones, such as an edited AGENTS.md, a new MEMORY.md entry, or the date, as a diff
its requests end with and its run never stores, so nothing told this way is replayed once it no longer holds. The
conversation takes the fresh prompt when the cache is gone or misses anyway: idle past the model's cache lifetime,
compacted history, another model, or other tool definitions. An edited or deleted MEMORY.md line also takes it at
once, so a memory the user asked to forget is not shown again.

Each conversation's prompt is one file beside its agent's conversation database, so a requester's private agent
keeps its prompts in its private state root. The file's modification time is the conversation's last model request,
so a restart keeps both; a missing or unreadable file starts the conversation from the fresh prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from difflib import unified_diff
from typing import TYPE_CHECKING

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.file_locks import advisory_file_lock
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

    ``model`` and ``tools`` identify the model and the tool definitions as its requests present them. ``memory_body``
    is the inlined MEMORY.md of ``memory_path`` without its truncation marker, whose counts change as lines are added
    past a cap.
    """

    system: tuple[str, ...]
    model: str
    tools: str
    memory_path: str | None = None
    memory_body: str = ""


def prompt_cache_ttl_seconds(*, extended_cache_time: bool) -> int:
    """Return how long a provider keeps a prompt cache that no request reads."""
    return PROMPT_CACHE_IDLE_SECONDS if extended_cache_time else _SHORT_CACHE_SECONDS


def prompt_snapshot_path(database: Path, session_id: str) -> Path:
    """Return the file that keeps the prompt of conversation ``session_id``, whose runs ``database`` stores.

    It lies beside the database's ``sessions`` directory, in the same state root, so two requesters' private
    instances of one agent, which may share a session id, never read each other's prompt.
    """
    return database.parent.parent / "prompt_snapshots" / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"


def stamp_prompt_request(path: Path, *, now: float) -> None:
    """Record a model request at ``now`` of the conversation whose prompt ``path`` keeps, which warms its cache."""
    with suppress(OSError):
        os.utime(path, (now, now))


def _load(path: Path, now: float) -> tuple[FreshPrompt, tuple[str | None, str | None], float] | None:
    """Return the kept prompt, its compaction, and how long ago its last request was, or None without a readable one."""
    try:
        idle = now - path.stat().st_mtime
        data = json.loads(path.read_text(encoding="utf-8"))
        prompt = data["prompt"]
        summary, checkpoint = data["compaction"]
        kept = FreshPrompt(
            tuple(map(str, prompt["system"])),
            str(prompt["model"]),
            str(prompt["tools"]),
            prompt["memory_path"],
            str(prompt["memory_body"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return kept, (summary, checkpoint), idle


def _only_added_lines(snapshot: str, fresh: str) -> bool:
    return fresh == snapshot or fresh.startswith(f"{snapshot}\n")


def _changes(kept: tuple[str, ...], fresh: tuple[str, ...]) -> str:
    """Return a unified diff from ``kept`` to ``fresh`` without its file header, or ``""`` when they are the same."""
    if kept == fresh:
        return ""
    before, after = ("\n\n".join(texts).splitlines() for texts in (kept, fresh))
    return "\n".join(list(unified_diff(before, after, lineterm="", n=1))[2:])


def held_system_prompt(
    path: Path,
    conversation: ConversationRef,
    fresh: FreshPrompt,
    *,
    ttl: float,
    now: float,
) -> tuple[tuple[str, ...], str]:
    """Return the system texts this turn sends and a diff from them to ``fresh.system``, ``""`` when they are the same.

    The texts are the ones ``path`` keeps while the conversation's prompt cache may be warm and would still match,
    and ``fresh.system`` otherwise, which ``path`` keeps from then on. Nothing records that a diff was returned, so
    every turn that keeps its texts gets the whole difference, and a turn that fails loses none of it.
    ``ttl`` is the model's cache lifetime and ``now`` wall-clock seconds, so idle times count across restarts.
    Comparing ``memory_path`` keeps a memory file the turn does not read out of it. Turns of one agent take the
    file's lock in turn, so two that start a conversation together send one prompt.
    """
    store = path.parent
    try:
        with advisory_file_lock(store / ".lock"):
            for other in store.glob("*.json"):
                if now - other.stat().st_mtime >= PROMPT_CACHE_IDLE_SECONDS + _MARGIN_SECONDS:
                    other.unlink()
            loaded = _load(path, now)
            if loaded is not None:
                kept, compaction, idle = loaded
                if (
                    idle < ttl + _MARGIN_SECONDS
                    and compaction == conversation.compaction
                    and (kept.model, kept.tools, kept.memory_path) == (fresh.model, fresh.tools, fresh.memory_path)
                    and _only_added_lines(kept.memory_body, fresh.memory_body)
                ):
                    os.utime(path, (now, now))
                    return kept.system, _changes(kept.system, fresh.system)
            write_text_atomic(path, json.dumps({"prompt": asdict(fresh), "compaction": conversation.compaction}))
            os.utime(path, (now, now))
    except OSError:
        pass
    return fresh.system, ""
