"""One-time merge of the team memory copies kept in every member's storage into one store per team."""

# LEGACY_COMPAT: Team memory copied into every member's storage root.
# Legacy format: each team turn's memories in every member's `agents/<member>/chroma` Mem0 store, one Mem0 extraction
#   per member under user_id `team_<sorted members>` with metadata `mindroom_replica_key` naming the turn, and in every
#   member's `agents/<member>/memory_files/team_<sorted members>/MEMORY.md`, each entry under one shared `[id=...]`.
#   v2026.3.72 started both the copies and the replica key, so every native copy carries the key; only a planted
#   record lacks one, and it counts as a turn of its own.
# Last legacy release: v2026.10.696; replacement: unreleased; writes each team scope once, to
#   `team_memory/<team scope>/`.
# Handling: before serving, once per storage root, the copies merge into their team's store and leave the members.
#   File entries union by id; a copy holding anything MindRoom did not write stays where it was. Mem0 copies merge
#   from the stores the runtime opens, those of configured shared agents on Mem0, since no copy elsewhere was read.
#   Each member's extraction of a turn shares the turn's replica key, so a turn keeps the extraction with the most
#   distinct facts, the first member's by name among equals; an edit or delete through another
#   member had rewritten or removed the turn in the other copies. Records move with their ids, embeddings, and
#   metadata, so nothing is embedded or extracted again. A member store holding links or special files, or that Chroma
#   cannot read, and a team whose store refuses its records stay as they are, with a warning starting with "Left",
#   and no later start merges them, so none undoes an edit made since. Every step is idempotent, so a start
#   interrupted anywhere finishes on the next; a receipt stops later starts from opening every store, and waits while
#   Chroma cannot be installed or no config loaded.
# Coverage: tests/test_legacy_team_memory.py.

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.resolution import memory_backend
from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment
from mindroom.tool_system.worker_routing import agent_state_root_path

from ._file_backend import write_scope_markdown_file
from ._policy import team_scope
from ._shared import FILE_MEMORY_ENTRY_PATTERN, FILE_MEMORY_ENTRYPOINT
from .config import MEM0_COLLECTION_PREFIX, mem0_chroma_path

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_RECEIPT = "team_memory_merged.json"
_TEAM_SCOPE_PREFIX = "team_"
_REPLICA_KEY = "mindroom_replica_key"
_MEM0_ENTITY_SUFFIX = "_entities"
_ENTRYPOINT_HEADER = "# Memory"
_MAX_COPY_BYTES = 64 << 20


async def migrate_team_memory(runtime_paths: RuntimePaths, config: Config | None) -> None:
    """Finish the one-time merge before any reply reads or writes team memory.

    ``config`` names the agents whose Mem0 stores hold copies anyone read; without one, the Mem0 copies wait for a
    start that loads one.
    """
    await run_blocking_until_complete(_migrate, runtime_paths, config)


def _migrate(runtime_paths: RuntimePaths, config: Config | None) -> None:
    receipt = tracking_dir(runtime_paths) / _RECEIPT
    if receipt.exists():
        return
    _merge_file_copies(runtime_paths, _subdirectories(runtime_paths.storage_root, Path("agents")))
    if config is not None and _merge_mem0_copies(runtime_paths, _mem0_member_stores(config, runtime_paths)):
        write_json_file_durable(receipt, {"version": 1}, strict_atomic_replace=True)


def _subdirectories(storage_root: Path, relative: Path) -> list[str]:
    """Return the sorted names of the directories in one directory below the storage root, reached without links."""
    try:
        with open_directory_within_root(storage_root, relative) as directory, os.scandir(directory) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        logger.warning("Left team memory in a directory that is not usable", path=str(relative), error=str(error))
        return []


def _merge_file_copies(runtime_paths: RuntimePaths, members: list[str]) -> None:
    storage_root = runtime_paths.storage_root
    copies: dict[str, list[tuple[Path, str]]] = {}
    for member in members:
        member_files = Path("agents", member, "memory_files")
        for name in _subdirectories(storage_root, member_files):
            if name.startswith(_TEAM_SCOPE_PREFIX) and (text := _recognized_copy(storage_root, member_files / name)):
                copies.setdefault(name, []).append((member_files / name, text))
    for scope_dir, team_copies in copies.items():
        target = team_scope(scope_dir, runtime_paths).root
        target.mkdir(parents=True, exist_ok=True)
        merged = _merged_entrypoint(_existing_entrypoint(target), [text for _, text in team_copies])
        write_scope_markdown_file(target, Path(FILE_MEMORY_ENTRYPOINT), merged.encode())
        for copy, _ in team_copies:
            try:
                _remove_copy(storage_root, copy)
            except OSError as error:
                # The runtime no longer reads member copies, so one left behind is never read.
                logger.warning(
                    "Left a merged team memory copy MindRoom could not remove",
                    path=str(copy),
                    error=str(error),
                )


def _recognized_copy(storage_root: Path, copy: Path) -> str | None:
    """Return a copy's `MEMORY.md` when the copy holds only what the runtime wrote there, else leave it with a warning."""
    try:
        with open_directory_within_root(storage_root, copy) as directory:
            text = _read_lone_entrypoint(directory)
    except (OSError, ValueError) as error:
        logger.warning("Left a team memory copy that MindRoom cannot merge", path=str(copy), error=str(error))
        return None
    if not all(_is_runtime_line(line) for line in text.splitlines()):
        logger.warning("Left a team memory copy with lines MindRoom did not write", path=str(copy))
        return None
    return text


def _read_lone_entrypoint(directory: int) -> str:
    """Read the `MEMORY.md` of a directory that holds nothing else."""
    with os.scandir(directory) as entries:
        if sorted(entry.name for entry in entries) != [FILE_MEMORY_ENTRYPOINT]:
            msg = "it holds more than MEMORY.md"
            raise ValueError(msg)
    return _read_entrypoint(directory)


def _read_entrypoint(directory: Path | int) -> str:
    return read_regular_file_within_root(directory, FILE_MEMORY_ENTRYPOINT, max_bytes=_MAX_COPY_BYTES).decode()


def _is_runtime_line(line: str) -> bool:
    return not line.strip() or line == _ENTRYPOINT_HEADER or FILE_MEMORY_ENTRY_PATTERN.match(line) is not None


def _existing_entrypoint(target: Path) -> str | None:
    """Return the team's `MEMORY.md` an interrupted earlier start published, if any."""
    try:
        return _read_entrypoint(target)
    except FileNotFoundError:
        return None


def _merged_entrypoint(existing: str | None, copies: list[str]) -> str:
    """Return the first text plus, in order, every entry of the later ones whose id it lacks."""
    first, *rest = copies if existing is None else [existing, *copies]
    lines = first.splitlines()
    ids = {match.group("id") for line in lines if (match := FILE_MEMORY_ENTRY_PATTERN.match(line))}
    for copy in rest:
        for line in copy.splitlines():
            if (match := FILE_MEMORY_ENTRY_PATTERN.match(line)) is not None and match.group("id") not in ids:
                ids.add(match.group("id"))
                lines.append(line)
    return "\n".join(lines) + "\n"


def _remove_copy(storage_root: Path, copy: Path) -> None:
    with open_directory_within_root(storage_root, copy.parent) as parent:
        with open_directory_within_root(parent, copy.name) as directory:
            os.unlink(FILE_MEMORY_ENTRYPOINT, dir_fd=directory)
        os.rmdir(copy.name, dir_fd=parent)
    # The member's `memory_files/` held only team copies, so it goes once it is empty.
    with suppress(OSError), open_directory_within_root(storage_root, copy.parent.parent) as member:
        os.rmdir(copy.parent.name, dir_fd=member)


@dataclass(frozen=True)
class _Record:
    """One Mem0 record as Chroma stores it."""

    id: str
    metadata: dict[str, Any]
    embedding: Any

    @property
    def team(self) -> str:
        return self.metadata["user_id"]

    @property
    def turn(self) -> str:
        """Return the team turn the record came from; a record without a text replica key is a turn of its own."""
        replica_key = self.metadata.get(_REPLICA_KEY)
        return replica_key if isinstance(replica_key, str) and replica_key else self.id

    @property
    def fact(self) -> str:
        """Return what tells the record's fact from another's: Mem0's hash of its text, else the record itself."""
        text_hash = self.metadata.get("hash")
        return text_hash if isinstance(text_hash, str) and text_hash else self.id


@dataclass(frozen=True)
class _MemberCopy:
    """One member store's team records in one Mem0 fact collection."""

    client: Any
    collection: Any
    records: list[_Record]


def _mem0_member_stores(config: Config, runtime_paths: RuntimePaths) -> list[Path]:
    """Return the Mem0 stores the runtime itself opens below `agents/`: those of configured shared agents on Mem0.

    Earlier releases read team copies only from these, so a copy anywhere else was already unread, and the merge opens
    no store the runtime would not open anyway.
    """
    storage_root = runtime_paths.storage_root.resolve()
    return [
        mem0_chroma_path(agent_state_root_path(storage_root, agent_name).relative_to(storage_root))
        for agent_name in sorted(config.agents)
        if memory_backend(config, agent_name) == "mem0" and config.get_agent(agent_name).private is None
    ]


def _merge_mem0_copies(runtime_paths: RuntimePaths, member_stores: list[Path]) -> bool:
    """Merge the member stores' Mem0 team records into their teams' stores; False while Chroma cannot be installed."""
    storage_root = runtime_paths.storage_root
    stores = [storage_root / store for store in member_stores if _holds_only_plain_files(storage_root, store)]
    if not stores:
        return True
    try:
        # Mem0's first use would install it too; installing now keeps old team memories visible from the first reply.
        ensure_optional_deps(["chromadb"], "chromadb", runtime_paths)
    except ImportError as error:
        logger.warning("Left Mem0 team memory in member stores until Chroma is installed", error=str(error))
        return False
    copies = [copy for store in stores for copy in _member_copies(store)]
    kept = _kept_records(copies)
    for (scope, collection_name), records in sorted(kept.items()):
        try:
            _write_team_records(mem0_chroma_path(team_scope(scope, runtime_paths).root), collection_name, records)
        except Exception as error:
            # A planted record can make the team's store refuse a batch, such as one of another dimension. The copies
            # stay, and no later start writes them again, so none undoes an edit made since.
            logger.warning("Left a team's Mem0 copies its store refused", team=scope, error=str(error))
            kept[(scope, collection_name)] = []
    # Only after every team's store holds its records do the member copies go, the dropped extractions first: a start
    # interrupted meanwhile still finds each kept extraction where it was, so it keeps the same one.
    kept_ids = {record.id for records in kept.values() for record in records}
    left = {team for team, records in kept.items() if not records}
    for keep in (False, True):
        for copy in copies:
            removed = [
                record.id
                for record in copy.records
                if (record.id in kept_ids) is keep and (record.team, copy.collection.name) not in left
            ]
            try:
                for batch in batched(removed, copy.client.get_max_batch_size()):
                    copy.collection.delete(ids=list(batch))
            except Exception as error:
                # The runtime reads only an agent's own records from its store, so a copy left there is never read.
                logger.warning("Left Mem0 team copies a member store refused to delete", error=str(error))
    return True


def _write_team_records(chroma_path: Path, collection_name: str, records: list[_Record]) -> None:
    target = _chroma_client(chroma_path)
    collection = target.get_or_create_collection(collection_name, embedding_function=None)
    # Records keep their IDs, so moving one again after an interrupted start changes nothing.
    for batch in batched(records, target.get_max_batch_size()):
        collection.upsert(
            ids=[record.id for record in batch],
            embeddings=[record.embedding for record in batch],
            metadatas=[record.metadata for record in batch],
        )


def _holds_only_plain_files(storage_root: Path, store: Path) -> bool:
    """Return whether a member's Chroma store is a directory tree of directories and regular files, without links.

    Chroma opens a store's files by path, so a link planted in the store could send its reads and writes elsewhere.
    """
    try:
        with open_directory_within_root(storage_root, store) as directory:
            if _plain_tree(directory):
                return True
    except FileNotFoundError:
        return False
    except (OSError, RecursionError, ValueError) as error:
        logger.warning("Left a Mem0 store that is not a usable directory", path=str(store), error=str(error))
        return False
    logger.warning("Left a Mem0 store holding links or special files", path=str(store))
    return False


def _plain_tree(directory: int) -> bool:
    with os.scandir(directory) as entries:
        for entry in entries:
            status = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                with open_directory_within_root(directory, entry.name) as child:
                    if not _plain_tree(child):
                        return False
            elif not stat.S_ISREG(status.st_mode):
                return False
    return True


def _member_copies(store: Path) -> list[_MemberCopy]:
    """Return each Mem0 fact collection of a member's store with its team records; none when Chroma cannot read it.

    The store is worker-writable in some deployments, so one Chroma cannot open leaves its copies where they are
    instead of stopping startup.
    """
    try:
        client = _chroma_client(store)
        copies = []
        for collection in client.list_collections():
            if not collection.name.startswith(f"{MEM0_COLLECTION_PREFIX}_") or collection.name.endswith(
                _MEM0_ENTITY_SUFFIX,
            ):
                continue
            payload = collection.get(include=["embeddings", "metadatas"])
            records = [
                _Record(record_id, metadata or {}, embedding)
                for record_id, metadata, embedding in zip(
                    payload["ids"],
                    payload["metadatas"],
                    payload["embeddings"],
                    strict=True,
                )
                if isinstance((metadata or {}).get("user_id"), str)
                and metadata["user_id"].startswith(_TEAM_SCOPE_PREFIX)
            ]
            if records:
                copies.append(_MemberCopy(client, collection, records))
    except Exception as error:
        logger.warning("Left a Mem0 store Chroma cannot read", path=str(store), error=str(error))
        return []
    return copies


def _kept_records(copies: list[_MemberCopy]) -> dict[tuple[str, str], list[_Record]]:
    """Return the records each team keeps, by team scope and collection.

    Each member extracted a team turn on its own, so a turn several members hold keeps one extraction, as if the team
    had extracted it once, as it now does: the one holding the most distinct facts, the first member's by name among
    equals. An edit or delete through another member rewrote or removed every fact of that turn in the other copies,
    so a copy it damaged holds fewer.
    """
    extractions: dict[tuple[str, str, str], list[list[_Record]]] = {}
    for copy in copies:
        by_turn: dict[tuple[str, str, str], list[_Record]] = {}
        for record in copy.records:
            by_turn.setdefault((record.team, copy.collection.name, record.turn), []).append(record)
        for turn, records in by_turn.items():
            extractions.setdefault(turn, []).append(records)
    kept: dict[tuple[str, str], list[_Record]] = {}
    for (scope, collection_name, _turn), candidates in extractions.items():
        best = max(candidates, key=lambda records: len({record.fact for record in records}))
        kept.setdefault((scope, collection_name), []).extend(best)
    return kept


def _chroma_client(path: Path) -> Any:  # noqa: ANN401 - chromadb is an optional dependency
    """Open one Chroma store with the settings Mem0 uses, so Mem0 later reuses the same cached system."""
    with optional_dependency_environment():
        import chromadb  # noqa: PLC0415
        from chromadb.config import Settings  # noqa: PLC0415

        return chromadb.Client(Settings(anonymized_telemetry=False, persist_directory=str(path), is_persistent=True))
