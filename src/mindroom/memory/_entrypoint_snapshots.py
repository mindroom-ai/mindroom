"""Keep a conversation's inlined MEMORY.md unchanged while its copy only gains lines and its prompt cache can be reused.

The preamble is part of the system prompt, so a changed preamble makes the provider re-read the whole
conversation after it. A conversation therefore keeps the preamble it last sent while its inlined copy only gains
lines (a file over a preload cap drops its oldest kept entry for each new one, so its copy loads again),
until it idles past the cache lifetime or its history is compacted; the conversation already shows the agent
its own memory writes as tool calls. Each conversation's copy is a file in its agent's state root, so a restart keeps
it and one requester's private memory stays out of another's; the file's modification time is the last model request
of a turn that took the copy from it. A missing or unreadable file starts the conversation from MEMORY.md again.
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


def _kept_preamble(conversation: ConversationRef, source_path: Path, fresh_body: str, now: float) -> str | None:
    """Return the preamble the conversation's file keeps when this turn may reuse it, else None."""
    path = conversation.snapshot
    try:
        idle = now - path.stat().st_mtime
        source, preamble, body, summary, checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    reusable = (
        idle < PROMPT_CACHE_IDLE_SECONDS
        and source == str(source_path)
        and (summary, checkpoint) == conversation.compaction
        and _only_added_lines(str(body), fresh_body)
    )
    return str(preamble) if reusable else None


def stable_entrypoint_preamble(
    conversation: ConversationRef,
    source_path: Path,
    fresh_body: str,
    fresh_preamble: str,
    *,
    now: float,
) -> str:
    """Return the preamble this conversation last sent for the file, or ``fresh_preamble`` when reuse would mislead.

    ``fresh_body`` is this turn's preamble without the truncation marker, whose counts change as lines are added
    past a cap; its header is compared too, so an edited prompt override loads again.
    A changed compaction loads the file again: the request that carries a compaction misses after the system prompt
    anyway, and the turn after one costs only its compacted history.
    An edited or deleted line loads at once, so a memory the user asked to forget is not shown again.
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
            if (kept := _kept_preamble(conversation, source_path, fresh_body, now)) is None:
                row = [str(source_path), fresh_preamble, fresh_body, *conversation.compaction]
                write_text_atomic(path, json.dumps(row))
            os.utime(path, (now, now))
    except OSError:
        return fresh_preamble
    return fresh_preamble if kept is None else kept
