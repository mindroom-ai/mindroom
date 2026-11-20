"""Keep a conversation's inlined MEMORY.md unchanged while its copy only gains lines and its prompt cache can be reused.

The preamble is part of the system prompt, so a changed preamble makes the provider re-read the whole
conversation after it. A conversation therefore keeps the preamble it last sent while its inlined copy only gains
lines, anywhere in a file within its preload limit and at the end of a cut one (whose preload keeps its first
lines, so an appended line leaves it as it was), until it idles past the cache lifetime or its history is compacted; the
conversation already shows the agent its own memory writes as tool calls. Each conversation's copy is a file in its
agent's state root, so a restart keeps it and one requester's private memory stays out of another's; the file's
modification time is the last model request of a turn that took the copy from it. A missing or unreadable file
starts the conversation from MEMORY.md again.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.file_locks import advisory_file_lock
from mindroom.yaml_io import write_text_atomic

if TYPE_CHECKING:
    from pathlib import Path

# A copy no turn reuses any more is deleted once it has been idle this long.
_PRUNE_IDLE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ConversationRef:
    """The conversation a turn continues, and what its history was last compacted into when the turn started.

    ``compaction`` holds the conversation's text summary and newest native checkpoint; a change means a compaction
    rewrote the replayed history. ``snapshot`` is the file that keeps the conversation's copy
    (``entrypoint_snapshot_path``).
    """

    compaction: tuple[str | None, str | None]
    snapshot: Path


def entrypoint_snapshot_path(database: Path, session_id: str) -> Path:
    """Return the file that keeps the copy of conversation ``session_id``, whose runs ``database`` stores.

    It lies beside the database's ``sessions`` directory, in the same state root, so two requesters' private
    instances of one agent, which may share a session id, never read each other's memory.
    """
    name = hashlib.sha256(session_id.encode()).hexdigest()
    return database.parent.parent / "memory_entrypoint_snapshots" / f"{name}.json"


def stamp_entrypoint_snapshot(path: Path, *, now: float) -> None:
    """Record a model request at ``now`` of a turn that took the copy ``path`` keeps, which warms its cache.

    A request that sent another preamble must not, such as a scheduled turn's or a resumed run's, whose copy a
    later turn may have replaced: it would keep a copy whose cache is gone from loading the file again.
    """
    with suppress(OSError):
        os.utime(path, (now, now))


def _only_added_lines(snapshot: str, fresh: str) -> bool:
    return fresh == snapshot or fresh.startswith(f"{snapshot}\n")


def _only_inserted_lines(kept: object, fresh: tuple[str, ...] | None) -> bool:
    """Return whether ``fresh`` holds every line of ``kept`` in order; either is None when its load withheld lines."""
    remaining = iter(fresh or ())
    return isinstance(kept, list) and fresh is not None and all(line in remaining for line in kept)


def _kept_preamble(
    conversation: ConversationRef,
    source_path: Path,
    fresh_body: str,
    frame: list[str],
    lines: tuple[str, ...] | None,
    now: float,
) -> str | None:
    """Return the preamble the conversation's file keeps when this turn may reuse it, else None."""
    path = conversation.snapshot
    try:
        idle = now - path.stat().st_mtime
        source, preamble, body, summary, checkpoint, kept_frame, kept_lines = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    reusable = (
        idle < PROMPT_CACHE_IDLE_SECONDS
        and source == str(source_path)
        and (summary, checkpoint) == conversation.compaction
        and kept_frame == frame
        and (
            _only_added_lines(str(body), fresh_body)
            # An empty copy sends no header that points at the file, so the file loads once it gains lines.
            or (bool(body) and _only_inserted_lines(kept_lines, lines))
        )
    )
    return str(preamble) if reusable else None


def stable_entrypoint_preamble(
    conversation: ConversationRef,
    source_path: Path,
    fresh_body: str,
    fresh_preamble: str,
    *,
    frame: list[str],
    lines: tuple[str, ...] | None,
    now: float,
) -> str:
    """Return the preamble this conversation last sent for the file, or ``fresh_preamble`` when reuse would mislead.

    ``fresh_body`` is this turn's preamble without the cut marker. Every prompt and setting in ``frame`` (its header,
    kept-copy notice, cut marker, and limit) must be unchanged, so an edited override or limit loads again even when
    the kept text is the same. A kept copy keeps its own marker, so it shows none after the file grows past the limit
    until it loads again.
    ``lines`` holds the file's lines when the preload cuts none, else None. A copy that cut none is also kept
    while ``lines`` holds its every line in order, so lines inserted anywhere keep it; ``lines`` fit the limit this
    turn, so the copy's, which they hold, still fit it.
    A changed compaction loads the file again: the request that carries a compaction misses after the system prompt
    anyway, and the turn after one costs only its compacted history.
    An edited or deleted line loads at once. A route that holds the system prompt tells that as a system prompt
    update, which quotes a deleted line until the hold starts over (``providers/pipeline/prompt_hold.py``).
    Comparing the file keeps a memory file the turn does not read out of it.
    ``now`` is wall-clock seconds, so idle times count across restarts. Turns of one agent take the store's lock in
    turn, so two that start a conversation together send one preamble.
    """
    path = conversation.snapshot
    try:
        with advisory_file_lock(path.parent / ".lock"):
            for other in path.parent.glob("*.json"):
                if now - other.stat().st_mtime >= _PRUNE_IDLE_SECONDS:
                    other.unlink()
            if (kept := _kept_preamble(conversation, source_path, fresh_body, frame, lines, now)) is None:
                row = [str(source_path), fresh_preamble, fresh_body, *conversation.compaction, frame, lines]
                write_text_atomic(path, json.dumps(row))
            os.utime(path, (now, now))
    except OSError:
        return fresh_preamble
    return fresh_preamble if kept is None else kept
