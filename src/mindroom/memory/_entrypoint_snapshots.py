"""Keep a conversation's inlined MEMORY.md unchanged while its copy only gains lines and its prompt cache can be reused.

The preamble is part of the system prompt, so a changed preamble makes the provider re-read the whole
conversation after it. A conversation therefore keeps the preamble it last sent while its inlined copy only gains
lines (a file over a preload cap drops its oldest kept entry for each new one, so its copy loads again),
until it idles past the cache lifetime or its history is compacted; the conversation already shows the agent its own
memory writes as tool calls. The copies are kept in a file under the storage root, so a restart keeps them too; a
missing or unreadable file starts every conversation from MEMORY.md again.
"""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS
from mindroom.yaml_io import write_text_atomic


@dataclass(frozen=True)
class ConversationRef:
    """The conversation a turn continues, and what its history was last compacted into when the turn started.

    ``compaction`` holds the conversation's text summary and newest native checkpoint; a change means a compaction
    rewrote the replayed history.
    """

    session_id: str
    compaction: tuple[str | None, str | None]


@dataclass(frozen=True)
class _Snapshot:
    preamble: str
    body: str
    compaction: tuple[str | None, str | None]
    used_at: float


# Read from the store on first use; None until then.
_snapshots: dict[tuple[str, str, Path], _Snapshot] | None = None
_lock = threading.Lock()


def _load(store: Path) -> dict[tuple[str, str, Path], _Snapshot]:
    try:
        rows = json.loads(store.read_text(encoding="utf-8"))
        return {
            (str(agent), str(session), Path(path)): _Snapshot(
                str(preamble),
                str(body),
                (summary, checkpoint),
                float(at),
            )
            for agent, session, path, preamble, body, summary, checkpoint, at in rows
        }
    except (OSError, ValueError, TypeError):
        return {}


def _save(store: Path, snapshots: dict[tuple[str, str, Path], _Snapshot]) -> None:
    rows = [[*key[:2], str(key[2]), s.preamble, s.body, *s.compaction, s.used_at] for key, s in snapshots.items()]
    with suppress(OSError):
        store.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(store, json.dumps(rows))


def _only_added_lines(snapshot: str, fresh: str) -> bool:
    return fresh == snapshot or fresh.startswith(f"{snapshot}\n")


def stable_entrypoint_preamble(
    agent_name: str,
    conversation: ConversationRef,
    source_path: Path,
    fresh_body: str,
    fresh_preamble: str,
    *,
    store: Path,
    now: float,
) -> str:
    """Return the preamble this conversation last sent for the file, or ``fresh_preamble`` when reuse would mislead.

    ``fresh_body`` is this turn's preamble without the truncation marker, whose counts change as lines are added
    past a cap; its header is compared too, so an edited prompt override loads again.
    A changed compaction loads the file again: the request that carries a compaction misses after the system prompt
    anyway, and the turn after one costs only its compacted history.
    An edited or deleted line loads at once, so a memory the user asked to forget is not shown again.
    Keying by the file keeps one requester's private memory out of another requester's turns.
    ``now`` is wall-clock seconds, so idle times count across restarts; ``store`` is the file the copies are kept in.
    """
    global _snapshots
    with _lock:
        if _snapshots is None:
            _snapshots = _load(store)
        for key in [key for key, snapshot in _snapshots.items() if now - snapshot.used_at >= PROMPT_CACHE_IDLE_SECONDS]:
            del _snapshots[key]
        key = (agent_name, conversation.session_id, source_path)
        snapshot = _snapshots.get(key)
        if (
            snapshot is not None
            and snapshot.compaction == conversation.compaction
            and _only_added_lines(snapshot.body, fresh_body)
        ):
            preamble, body = snapshot.preamble, snapshot.body
        else:
            preamble, body = fresh_preamble, fresh_body
        _snapshots[key] = _Snapshot(preamble, body, conversation.compaction, now)
        _save(store, _snapshots)
    return preamble
