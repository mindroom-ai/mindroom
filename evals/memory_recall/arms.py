"""Candidate per-turn memory recalls ("arms") and their replay over logged turns.

Each arm turns the agent's memory directory into a corpus (dropping excluded files, optionally one file per entry),
indexes it with MindRoom's own knowledge indexer and the configured embedder, and searches it with the file-memory
overlay and hybrid search the per-turn recall uses. The `shipped` arm indexes the whole directory through the shipped
overlay, which leaves files out and splits entries itself; the other arms index through the overlay as it was before,
with fixed 5,000-character chunks and no excludes. Hits that may have been written after a turn are dropped before
selection, so a note written from that very turn cannot leak its answer.

The `shipped` arm measures the agent's own semantic hits, the part of the shipped per-turn recall that
`select_recall_hits` chooses. The file backend appends team keyword hits after them, and its keyword fallback for a
cold index keeps the top 3; neither is replayed, since the benchmarked agents have no team memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from mindroom.config.memory import MemorySearchConfig
from mindroom.constants import runtime_paths_with_storage_root
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.file_memory_knowledge import resolve_file_memory_knowledge
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.memory_entries import split_memory_entries
from mindroom.knowledge.read_protocol import SOURCE_PATH_KEY
from mindroom.knowledge.utils import resolve_knowledge_base_access_async
from mindroom.memory._file_backend import _RECALL_SEARCH_LIMIT
from mindroom.memory._prompting import select_recall_hits
from mindroom.memory._semantic_file_search import _memory_results_from_hits
from mindroom.path_globs import matches_root_glob
from mindroom.runtime_resolution import resolve_knowledge_binding
from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.file_memory_knowledge import _FileMemoryKnowledgeResolution
    from mindroom.knowledge.read_protocol import SearchHit
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.memory._shared import MemoryResult

    from .turns import Turn


#: When a memory entry was written: the exact UTC time of its id, else a calendar day, else unknown.
type _Written = datetime | date | None


@dataclass(frozen=True)
class Arm:
    """One candidate recall: which files it indexes, how it chunks them, and which hits it injects."""

    name: str
    #: Workspace-relative globs (`memory/...`) of files left out of the corpus.
    exclude: tuple[str, ...]
    #: Index each memory entry as its own file instead of the stock 5,000-character chunks of whole files.
    entry_chunks: bool
    #: `top3` injects the three best hits, as today; `gap` uses `select_recall_hits`.
    selection: Literal["top3", "gap"]
    #: Inject nothing for a short reply in an existing thread.
    skip_short: bool
    #: Index through the shipped overlay and search as the shipped per-turn recall does.
    shipped: bool = False


# Archived copies, raw session dumps (`YYYY-MM-DD-HHMM.md`) and other agents' memory.
_EXCLUDES = (
    "memory/archive/**",
    "memory/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9].md",
    "memory/agent_*/**",
)
ARMS: dict[str, Arm] = {
    "A0": Arm("A0", exclude=(), entry_chunks=False, selection="top3", skip_short=False),
    "A1": Arm("A1", exclude=_EXCLUDES, entry_chunks=False, selection="top3", skip_short=False),
    "A2": Arm("A2", exclude=_EXCLUDES, entry_chunks=True, selection="top3", skip_short=False),
    "A3": Arm("A3", exclude=_EXCLUDES, entry_chunks=True, selection="gap", skip_short=False),
    "A4": Arm("A4", exclude=_EXCLUDES, entry_chunks=True, selection="gap", skip_short=True),
    "shipped": Arm("shipped", exclude=(), entry_chunks=False, selection="gap", skip_short=False, shipped=True),
}

# The most hits that keep live recall's ranking: the store fuses `max(limit * 2, 50)` candidates per ranking, 50 for
# the live per-turn limit of 3, and any larger limit would fuse more and could reorder the top hits.
_OVER_FETCH = 25
_TOP_K = 3
# Query embeddings share one local embedder with live agents, so few run at once.
_SEARCH_CONCURRENCY = 4
# Waits before each retry of a failed search: four attempts ride out an embedder swapping models or under load.
_SEARCH_RETRY_DELAYS_S = (2.0, 5.0, 15.0)
_SCOPE_USER_ID = "memory_recall_eval"
# The shipped file-memory search over `memory/**/*.md`, semantic.
_SEARCH_CONFIG = MemorySearchConfig(mode="semantic")
# The file-memory search before excludes, whose overlay the arms other than `shipped` index through.
_STOCK_SEARCH_CONFIG = MemorySearchConfig(mode="semantic", exclude=[])
# A reply in a thread shorter than this skips recall in A4.
_SHORT_FOLLOW_UP_CHARS = 40
_ENTRIES_SUFFIX = ".entries"
_UNDATED = "undated"
_TIME_LABEL = "%Y%m%dT%H%M%SZ"
_ID_TIME_RE = re.compile(r"^(?:- )?\[id=m_(\d{14})_")
_FILE_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Entry chunks are capped as the shipped overlay caps them.
_ENTRY_MAX_CHARS = 1200
# A cap no entry reaches, which splits a file into its whole entries.
_UNCAPPED = sys.maxsize
# The stock overlay's fixed chunk size, before entry chunks.
_STOCK_CHUNK_SIZE = 5000
_READY_MARKER = "ready"
# Collapses whitespace the way the indexer's chunker does.
_CLEANER = SafeFixedSizeChunking()
_logger = logging.getLogger(__name__)


async def replay(
    turns: Sequence[Turn],
    arm: Arm,
    *,
    memory_dir: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    index_dir: Path,
) -> list[list[MemoryResult] | None]:
    """Return the hits `arm` injects for each turn, in order; the arm's index is built once under `index_dir`.

    A turn whose search still fails after its retries is None, so the caller can leave it out instead of reading it
    as a turn that recalled nothing.
    """
    first_added = _FirstAdded(memory_dir)
    corpus = _build_corpus(memory_dir, arm, first_added)
    if not corpus:
        return [[] for _turn in turns]
    knowledge = await _indexed_knowledge(
        corpus,
        config=config,
        runtime_paths=runtime_paths,
        index_dir=index_dir,
        shipped=arm.shipped,
    )
    limit = _RECALL_SEARCH_LIMIT if arm.shipped else _OVER_FETCH
    # Loaded up front: until it is, selection estimates o200k tokens instead of counting them.
    load_o200k()
    hit_times = _HitTimes(corpus, entry_chunks=arm.entry_chunks, first_added=first_added)
    semaphore = asyncio.Semaphore(_SEARCH_CONCURRENCY)

    async def recall(turn: Turn) -> list[MemoryResult] | None:
        if arm.skip_short and is_short_follow_up(turn.query, in_thread=_in_thread(turn.session_id)):
            return []
        async with semaphore:
            hits = await _search_with_retries(knowledge, turn.query, limit)
        if hits is None:
            return None
        kept = [hit for hit in hits if not any(_written_after(written, turn.ts) for written in hit_times(hit))]
        if arm.selection == "top3":
            # Converted after the cut, as live recall converts its three hits, so the scores match too.
            return _memory_results_from_hits(kept[:_TOP_K], scope_user_id=_SCOPE_USER_ID)
        return select_recall_hits(_memory_results_from_hits(kept, scope_user_id=_SCOPE_USER_ID))

    return list(await asyncio.gather(*(recall(turn) for turn in turns)))


def is_short_follow_up(message: str, *, in_thread: bool) -> bool:
    """Return whether `message` is a short reply inside an existing thread, where A4 skips recall."""
    return in_thread and len(message.strip()) < _SHORT_FOLLOW_UP_CHARS


async def _search_with_retries(knowledge: AgentKnowledge, query: str, limit: int) -> list[SearchHit] | None:
    """Search `query`, retrying a failure after each delay in `_SEARCH_RETRY_DELAYS_S`; None when every attempt failed."""
    for attempt, delay in enumerate((0.0, *_SEARCH_RETRY_DELAYS_S), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await knowledge.asearch(query, limit)
        except Exception as exc:  # embedder HTTP errors, timeouts: all worth another attempt
            error = f"{type(exc).__name__}: {exc}"
            _logger.warning("Replay search attempt %d failed: %.300s", attempt, error)
    _logger.warning("Replay search failed after %d attempts; the turn counts as a search error", attempt)
    return None


def _build_corpus(memory_dir: Path, arm: Arm, first_added: _FirstAdded) -> dict[str, bytes]:
    """Return the arm's corpus: workspace-relative path (`memory/...`) to file content.

    With entry chunks, each entry of `memory/x.md` becomes `memory/x.md.entries/<written>__<n>.md`, where
    `<written>` is `YYYYMMDDThhmmssZ` for an id's time, `YYYY-MM-DD` for a day, or `undated`.
    """
    corpus: dict[str, bytes] = {}
    for path in sorted(memory_dir.rglob("*.md")):
        relative = PurePosixPath(path.relative_to(memory_dir).as_posix())
        corpus_path = f"memory/{relative}"
        if any(part.startswith(".") for part in relative.parts) or not path.is_file():
            continue
        if any(matches_root_glob(corpus_path, pattern) for pattern in arm.exclude):
            continue
        if not arm.entry_chunks:
            corpus[corpus_path] = path.read_bytes()
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, (entry, written) in enumerate(_written_entries(text, relative, first_added), start=1):
            corpus[f"{corpus_path}{_ENTRIES_SUFFIX}/{_label(written)}__{number}.md"] = entry.encode()
    return corpus


class _FirstAdded:
    """The day each memory file was first committed, from `git log` in the memory directory; None outside Git."""

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._days: dict[PurePosixPath, date | None] = {}

    def __call__(self, relative: PurePosixPath) -> date | None:
        """Return the day `relative` (to the memory directory) was first added, or None when unknown."""
        if relative not in self._days:
            self._days[relative] = self._lookup(relative)
        return self._days[relative]

    def _lookup(self, relative: PurePosixPath) -> date | None:
        try:
            log = subprocess.run(
                ["git", "-C", str(self._memory_dir), "log", "--diff-filter=A", "--format=%cs", "--", str(relative)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            return None
        # Newest first, so the last line is the first time the file was added.
        return date.fromisoformat(log[-1]) if log else None


def _written_after(written: _Written, turn_ts: datetime) -> bool:
    """Return whether a note written at `written` may postdate the turn at `turn_ts` (an aware time).

    An exact time is compared exactly. A bare day counts from the turn's own local day on, since the note may have
    been written later that day. An unknown date never counts, so such notes are kept.
    """
    if isinstance(written, datetime):
        return written > turn_ts
    if isinstance(written, date):
        return written >= turn_ts.date()
    return False


def _written_entries(
    text: str,
    relative: PurePosixPath,
    first_added: Callable[[PurePosixPath], date | None],
) -> list[tuple[str, _Written]]:
    """Split a memory file into its entry chunks, each with when its entry was written.

    That is the UTC time of an `m_YYYYMMDDhhmmss_` id, else the id's day, else a `YYYY-MM-DD` prefix of the file
    name, else the file's first commit. Every piece of an over-long entry takes its entry's time.
    """
    entries: list[tuple[str, _Written]] = []
    for entry in split_memory_entries(text, _UNCAPPED):
        written = _id_time(entry) or _id_day(entry) or _file_day(relative.name) or first_added(relative)
        entries.extend((piece, written) for piece in split_memory_entries(entry, _ENTRY_MAX_CHARS))
    return entries


def _id_time(text: str) -> datetime | None:
    """Return the UTC time in an entry's leading `m_YYYYMMDDhhmmss_` id, or None."""
    match = _ID_TIME_RE.match(text)
    return None if match is None else _parse_time(match[1], "%Y%m%d%H%M%S")


def _id_day(text: str) -> date | None:
    """Return the day in an entry's leading id, for an id whose time of day is not a valid time."""
    match = _ID_TIME_RE.match(text)
    time = None if match is None else _parse_time(match[1][:8], "%Y%m%d")
    return None if time is None else time.date()


def _file_day(file_name: str) -> date | None:
    """Return the day a `YYYY-MM-DD` prefix of a memory file's name records, or None."""
    match = _FILE_DAY_RE.match(file_name)
    time = None if match is None else _parse_time(match[0], "%Y-%m-%d")
    return None if time is None else time.date()


def _parse_time(value: str, time_format: str) -> datetime | None:
    try:
        return datetime.strptime(value, time_format).replace(tzinfo=UTC)
    except ValueError:
        return None


def _label(written: _Written) -> str:
    """Return the entry file name prefix recording `written`."""
    if isinstance(written, datetime):
        return written.strftime(_TIME_LABEL)
    return written.isoformat() if written else _UNDATED


def _parse_label(label: str) -> _Written:
    """Return the time an entry file name prefix from `_label` records."""
    if label == _UNDATED:
        return None
    try:
        return datetime.strptime(label, _TIME_LABEL).replace(tzinfo=UTC)
    except ValueError:
        return date.fromisoformat(label)


class _HitTimes:
    """When the entries a search hit holds were written: its entry file's name, or the entries a chunk overlaps.

    The indexer stores chunks with their whitespace collapsed, which hides the line starts entries split on, so a
    chunk's entries are found by locating it in its whole source file, collapsed the same way.
    """

    def __init__(self, corpus: dict[str, bytes], *, entry_chunks: bool, first_added: _FirstAdded) -> None:
        self._corpus = corpus
        self._entry_chunks = entry_chunks
        self._first_added = first_added
        self._files: dict[str, tuple[str, list[tuple[int, int, _Written]]]] = {}

    def __call__(self, hit: SearchHit) -> list[_Written]:
        """Return when each entry in `hit` was written."""
        source = str(hit.meta_data.get(SOURCE_PATH_KEY, ""))
        if self._entry_chunks:
            return [_parse_label(PurePosixPath(source).name.split("__", 1)[0])]
        flat, spans = self._entry_spans(source)
        start = flat.find(hit.content)
        end = start + len(hit.content)
        # A chunk not found verbatim (a chunk-context prefix, say) counts as covering the whole file.
        return [
            written for span_start, span_end, written in spans if start == -1 or (span_start < end and span_end > start)
        ]

    def _entry_spans(self, source: str) -> tuple[str, list[tuple[int, int, _Written]]]:
        """Return the source file's collapsed text and each entry's span in it with when it was written."""
        if source not in self._files:
            text = self._corpus.get(source, b"").decode("utf-8", errors="replace")
            relative = PurePosixPath(source).relative_to("memory")
            flat = _CLEANER.clean_text(text)
            spans: list[tuple[int, int, _Written]] = []
            cursor = 0
            for entry, written in _written_entries(text, relative, self._first_added):
                piece = _CLEANER.clean_text(entry).strip()
                start = flat.find(piece, cursor)
                if start == -1:
                    continue
                cursor = start + len(piece)
                spans.append((start, cursor, written))
            if not spans:
                spans.append((0, len(flat), self._first_added(relative)))
            self._files[source] = (flat, spans)
        return self._files[source]


def _in_thread(session_id: str) -> bool:
    """Return whether a session is a thread: MindRoom thread sessions are `<room_id>:<$thread_root_event>`."""
    return ":$" in session_id


async def _indexed_knowledge(
    corpus: dict[str, bytes],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    index_dir: Path,
    shipped: bool,
) -> AgentKnowledge:
    """Return a search handle over `corpus`, indexing it first unless `index_dir` already holds its index."""
    home = index_dir / _corpus_hash(corpus, config, shipped=shipped)
    root = home / "corpus"
    arm_paths = runtime_paths_with_storage_root(runtime_paths, home / "storage")
    ready = (home / _READY_MARKER).exists()
    if not ready:
        # A partial build an interrupted run left behind starts over.
        if home.exists():
            shutil.rmtree(home)
        for relative, content in corpus.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    file_memory = _file_memory(root, config, shipped=shipped)
    if not ready:
        # The refresh child's path (`refresh_runner._knowledge_manager`), run in this process.
        binding = resolve_knowledge_binding(
            file_memory.base_id,
            file_memory.config,
            arm_paths,
            None,
            start_watchers=False,
            create=True,
        )
        manager = KnowledgeManager(
            base_id=file_memory.base_id,
            config=file_memory.config,
            runtime_paths=arm_paths,
            storage_path=binding.storage_root,
            knowledge_path=binding.knowledge_path,
        )
        outcome = await manager.reindex_all()
        if not outcome.published:
            msg = f"Indexing the replay corpus failed: {outcome.error}"
            raise RuntimeError(msg)
        (home / _READY_MARKER).touch()
    access = await resolve_knowledge_base_access_async(
        file_memory.base_id,
        file_memory.config,
        arm_paths,
        execution_identity=None,
    )
    if access.knowledge is None:
        msg = f"Replay index is not searchable: {access.availability.value} ({access.last_error})"
        raise RuntimeError(msg)
    return access.knowledge


def _file_memory(root: Path, config: Config, *, shipped: bool) -> _FileMemoryKnowledgeResolution:
    """Return the shipped file-memory overlay of `root`, or the one before excludes and entry chunks."""
    if shipped:
        return resolve_file_memory_knowledge(
            scope_user_id=_SCOPE_USER_ID,
            root=root,
            config=config,
            search_config=_SEARCH_CONFIG,
        )
    file_memory = resolve_file_memory_knowledge(
        scope_user_id=_SCOPE_USER_ID,
        root=root,
        config=config,
        search_config=_STOCK_SEARCH_CONFIG,
    )
    stock = file_memory.config.knowledge_bases[file_memory.base_id].model_copy(
        update={"chunk_strategy": "fixed", "chunk_size": _STOCK_CHUNK_SIZE},
    )
    return replace(
        file_memory,
        config=file_memory.config.with_runtime_knowledge_base_overlay(file_memory.base_id, stock),
    )


def _corpus_hash(corpus: dict[str, bytes], config: Config, *, shipped: bool) -> str:
    """Return a key for the corpus's index: its files, the overlay that indexes them, and its embedder."""
    digest = hashlib.sha256(config.memory.embedder.model_dump_json().encode())
    if shipped:
        # Only the shipped overlay changes the key, so the other arms keep the indexes they cached before it existed.
        overlay = _file_memory(Path(), config, shipped=True)
        digest.update(b"shipped overlay")
        digest.update(overlay.config.knowledge_bases[overlay.base_id].model_dump_json(exclude={"path"}).encode())
    for relative in sorted(corpus):
        digest.update(json.dumps([relative, hashlib.sha256(corpus[relative]).hexdigest()]).encode())
    return digest.hexdigest()[:16]
