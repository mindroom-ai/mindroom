"""One-time merge of the team memory copies kept in every member's storage into team memory."""

# LEGACY_COMPAT: Team memory copied into every member's storage root.
# Legacy format: each team turn's memories in every member's `agents/<member>/chroma` Mem0 store, one Mem0 extraction
#   per member under user_id `team_<sorted members>` with metadata `mindroom_replica_key` naming the turn, and in every
#   member's `agents/<member>/memory_files/team_<sorted members>/MEMORY.md`, each entry under one shared `[id=...]`.
#   v2026.3.72 started both the copies and the replica key, so every native copy carries the key; only a planted
#   record lacks one, and it counts as a turn of its own.
# Last legacy release: v2026.10.732; replacement: unreleased; writes each team scope once, below `team_memory/`: every
#   team's Mem0 records in one store, each team's file entries in its own directory.
# Handling: before serving, once per storage root, each member's copies merge into team memory and leave the member:
#   file entries by id, Mem0 copies as one extraction per turn, moved with their ids and embeddings. A copy the
#   merge cannot safely read, or whose embedding size nothing trusted vouches for, stays, with a warning starting with
#   "Left"; a start interrupted anywhere finishes on the next. docs/dev/2026-10-29-team-memory-store.md records each
#   rule's reason.
# Coverage: tests/test_legacy_team_memory.py.

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, replace
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.resolution import memory_backend
from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.embeddings.factory import resolve_embedder_settings
from mindroom.logging_config import get_logger
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT, OPENAI_EMBEDDING_DIMENSIONS
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment
from mindroom.tool_system.worker_routing import agent_state_root_path

from ._file_backend import write_scope_markdown_file
from ._policy import TEAM_SCOPE_PREFIX, team_memory_root, team_scope
from ._shared import FILE_MEMORY_ENTRY_PATTERN, FILE_MEMORY_ENTRYPOINT, FILE_MEMORY_ENTRYPOINT_HEADER
from .config import (
    MEM0_COLLECTION_PREFIX,
    create_memory_instance,
    mem0_chroma_path,
    mem0_collection_name,
    mem0_store_exists,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_RECEIPT = "team_memory_merged.json"
_REPLICA_KEY = "mindroom_replica_key"
_MEM0_ENTITY_SUFFIX = "_entities"
_MAX_COPY_BYTES = 64 << 20


async def migrate_team_memory(runtime_paths: RuntimePaths, config: Config) -> None:
    """Finish the one-time merge before any reply reads or writes team memory.

    ``config`` names the agents whose Mem0 stores hold copies anyone read and the embedder that wrote them.
    """
    if not await run_blocking_until_complete(_merge_file_copies_unless_done, runtime_paths):
        return
    if (copies := await run_blocking_until_complete(_read_mem0_copies, runtime_paths, config)) is None:
        return
    size = await _trusted_embedding_size(runtime_paths, config) if copies else 0
    if size is not None:
        await run_blocking_until_complete(_merge_mem0_copies, runtime_paths, config, copies, size)


def _merge_file_copies_unless_done(runtime_paths: RuntimePaths) -> bool:
    """Merge the file copies unless the receipt says the whole merge is done; return whether the Mem0 part remains."""
    if (tracking_dir(runtime_paths) / _RECEIPT).exists():
        return False
    _merge_file_copies(runtime_paths, _subdirectories(runtime_paths.storage_root, Path("agents")))
    return True


def _read_mem0_copies(runtime_paths: RuntimePaths, config: Config) -> list[_MemberCopy] | None:
    """Return the Mem0 copies to merge; None while Chroma cannot be installed."""
    storage_root = runtime_paths.storage_root
    stores = [
        storage_root / store
        for store in _mem0_member_stores(config, runtime_paths)
        if _holds_only_plain_files(storage_root, store)
    ]
    if not stores:
        return []
    try:
        # Mem0's first use would install it too; installing now keeps old team memories visible from the first reply.
        ensure_optional_deps(["chromadb"], "chromadb", runtime_paths)
    except ImportError as error:
        logger.warning("Left Mem0 team memory in member stores until Chroma is installed", error=str(error))
        return None
    collection_name = mem0_collection_name(config)
    return [copy for store in stores if (copy := _member_copy(storage_root, store, collection_name)) is not None]


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
            if not _names_member(storage_root, name, member):
                continue
            copy = member_files / name
            if (text := _recognized_copy(storage_root, copy)) is not None:
                copies.setdefault(name, []).append((copy, text))
    for scope_dir, team_copies in copies.items():
        target = team_scope(scope_dir, runtime_paths).root
        target.mkdir(parents=True, exist_ok=True)
        merged = _merged_entrypoint(_existing_entrypoint(target), [text for _, text in team_copies])
        write_scope_markdown_file(target, Path(FILE_MEMORY_ENTRYPOINT), merged.encode())
        for copy, _ in team_copies:
            try:
                _remove_copy(storage_root, copy)
            except OSError as error:
                # The runtime no longer reads member copies. Until the receipt is written, each start merges one left
                # behind again, so an entry deleted from the team's file meanwhile would come back.
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
    return (
        not line.strip() or line == FILE_MEMORY_ENTRYPOINT_HEADER or FILE_MEMORY_ENTRY_PATTERN.match(line) is not None
    )


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
    def key(self) -> tuple[str, str, str]:
        """Return what tells one source record from another, as only a planted record shares another's ID."""
        return (self.team, self.id, self.fact)

    @property
    def fact(self) -> str:
        """Return what tells the record's fact from another's: Mem0's hash of its text, else the record itself."""
        text_hash = self.metadata.get("hash")
        return text_hash if isinstance(text_hash, str) and text_hash else self.id


@dataclass(frozen=True)
class _MemberCopy:
    """One member store's team records in one Mem0 fact collection."""

    store: Path
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
        for agent_name, agent in sorted(config.agents.items())
        if memory_backend(config, agent_name) == "mem0" and agent.private is None
    ]


def _names_member(storage_root: Path, scope_user_id: object, agent_dir: str) -> bool:
    """Return whether a team scope names the agent whose directory is ``agent_dir``, the only ones earlier releases read.

    The scope ID may be a file copy's directory name, which lost a trailing underscore, as an agent's directory name
    loses its leading and trailing ones, so the two compare as directory names.
    """
    if not isinstance(scope_user_id, str) or not scope_user_id.startswith(TEAM_SCOPE_PREFIX):
        return False
    members = scope_user_id.removeprefix(TEAM_SCOPE_PREFIX).split("+")
    return agent_dir in {agent_state_root_path(storage_root, member).name for member in members}


async def _trusted_embedding_size(runtime_paths: RuntimePaths, config: Config) -> int | None:
    """Return the size the configured embedder gives its embeddings; None, with a warning, while it cannot answer.

    Every team's records share the one team store, so a record of another size would make it refuse the rest. Member
    stores are worker-writable, so the size comes from records the team store already holds, else from the settings of
    an embedder at OpenAI's own API, else from asking it once; settings that fix it keep an embedder outage from holding
    the merge back. Asking has no deadline, because building the embedder can install packages or download its model.
    """
    team_root = team_memory_root(runtime_paths)
    if (held := await asyncio.to_thread(_held_embedding_size, team_root, mem0_collection_name(config))) is not None:
        return held
    if (configured := _configured_embedding_size(config, runtime_paths)) is not None:
        return configured
    try:
        memory = await create_memory_instance(team_root, config, runtime_paths)
        embedding = await asyncio.to_thread(cast("Any", memory).embedding_model.embed, "team memory", "search")
    except Exception as error:
        logger.warning(
            "Left Mem0 team memory in member stores until the configured embedder answers",
            error_type=type(error).__name__,
            error=str(error),
        )
        return None
    return len(embedding)


def _configured_embedding_size(config: Config, runtime_paths: RuntimePaths) -> int | None:
    """Return the size OpenAI's own API gives the configured embeddings: their `dimensions`, or the model's own size."""
    settings = resolve_embedder_settings(config, runtime_paths)
    # Only OpenAI's own API applies `dimensions` for certain: other providers never receive it, and an OpenAI-compatible
    # server, set as `host` or through `OPENAI_BASE_URL`, may ignore it or serve another model under the same name.
    if settings.provider != "openai" or settings.host != OPENAI_BASE_URL_DEFAULT:
        return None
    if settings.dimensions is not None:
        return settings.dimensions
    return OPENAI_EMBEDDING_DIMENSIONS.get(settings.model)


def _held_embedding_size(team_root: Path, collection_name: str) -> int | None:
    """Return the size of the embeddings the team store holds for the configured embedder, if it holds any."""
    if not mem0_store_exists(team_root):
        return None
    client = _chroma_client(mem0_chroma_path(team_root))
    if collection_name not in {collection.name for collection in client.list_collections()}:
        return None
    payload = client.get_collection(collection_name, embedding_function=None).get(limit=1, include=["embeddings"])
    return len(payload["embeddings"][0]) if payload["ids"] else None


def _merge_mem0_copies(runtime_paths: RuntimePaths, config: Config, copies: list[_MemberCopy], size: int) -> None:
    """Merge the member stores' Mem0 team records of the trusted size into the team store, then write the receipt."""
    copies = [_sized(copy, size) for copy in copies]
    kept = _kept_records(copies)
    target = mem0_chroma_path(team_memory_root(runtime_paths))
    collection_name = mem0_collection_name(config)
    rejected: set[str] = set()
    merged: set[tuple[str, str, str]] = set()
    for scope, records in sorted(kept.items()):
        try:
            merged |= _write_team_records(target, collection_name, records)
        except _rejected_record_errors() as error:
            # A planted record can make the team store reject a batch, such as one with malformed metadata. The copies
            # stay, and no later start writes them again, so none undoes an edit made since. Any other failure, such as
            # a storage error, stops the start before a Mem0 copy goes, and the next start merges again.
            logger.warning("Left a team's Mem0 copies its store rejected", team=scope, error=str(error))
            rejected.add(scope)
    # Only after the team store holds every team's records do the member copies go, the dropped extractions first, and the
    # kept ones only once every dropped one went: a start interrupted meanwhile still finds each kept extraction beside
    # any dropped one, so it keeps the same one.
    # A kept record goes only once team memory holds its fact, so one a planted record kept out stays where it was;
    # records are told apart by scope, ID, and fact, as only a planted record shares another's ID.
    kept_keys = {record.key for records in kept.values() for record in records}
    for keep in (False, True):
        all_removed = True
        for copy in copies:
            removed = [
                record.id
                for record in copy.records
                if record.team not in rejected and (record.key in merged if keep else record.key not in kept_keys)
            ]
            try:
                for batch in batched(removed, copy.client.get_max_batch_size(), strict=False):
                    copy.collection.delete(ids=list(batch))
            except Exception as error:
                # The runtime reads only an agent's own records from its store, so a copy left there is never read.
                logger.warning(
                    "Left Mem0 team copies a member store refused to delete",
                    path=str(copy.store),
                    error=str(error),
                )
                all_removed = False
        if not all_removed:
            break
    write_json_file_durable(tracking_dir(runtime_paths) / _RECEIPT, {"version": 1}, strict_atomic_replace=True)


def _sized(copy: _MemberCopy, size: int) -> _MemberCopy:
    """Return a copy without its records of another embedding size, which stay where they are."""
    sized = [record for record in copy.records if len(record.embedding) == size]
    if len(sized) < len(copy.records):
        logger.warning(
            "Left Mem0 team copies of another embedding size",
            path=str(copy.store),
            count=len(copy.records) - len(sized),
        )
    return replace(copy, records=sized)


def _rejected_record_errors() -> tuple[type[Exception], ...]:
    """Return the errors Chroma raises for a record it rejects, such as one of another dimension or a malformed one."""
    with optional_dependency_environment():
        from chromadb.errors import InvalidArgumentError  # noqa: PLC0415

    return (InvalidArgumentError, ValueError)


def _write_team_records(chroma_path: Path, collection_name: str, records: list[_Record]) -> set[tuple[str, str, str]]:
    """Write the records whose fact the team store does not hold yet; return the keys of those whose fact it then holds.

    Mem0 keeps one record per fact in a scope, so a fact two turns' kept extractions share, or one an interrupted
    earlier start already wrote, is written once per scope; every scope in the team store keeps its own. Only a planted
    record shares another's ID, which Chroma refuses within one write, so a record whose ID the store already gave to
    another stays in its member's store.
    """
    target = _chroma_client(chroma_path)
    collection = target.get_or_create_collection(collection_name, embedding_function=None)
    payload = collection.get(include=["metadatas"])
    held_ids = set(payload["ids"])
    held_facts = {
        (stored.team, stored.fact)
        for stored in (
            _Record(record_id, metadata or {}, None)
            for record_id, metadata in zip(payload["ids"], payload["metadatas"], strict=True)
        )
    }
    written: list[_Record] = []
    merged: set[tuple[str, str, str]] = set()
    for record in records:
        if (record.team, record.fact) in held_facts:
            merged.add(record.key)
        elif record.id in held_ids:
            logger.warning("Left a Mem0 team copy whose ID another team record holds", team=record.team, id=record.id)
        else:
            held_ids.add(record.id)
            held_facts.add((record.team, record.fact))
            written.append(record)
            merged.add(record.key)
    for batch in batched(written, target.get_max_batch_size(), strict=False):
        collection.upsert(
            ids=[record.id for record in batch],
            embeddings=[record.embedding for record in batch],
            metadatas=[record.metadata for record in batch],
        )
    return merged


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


def _member_copy(storage_root: Path, store: Path, collection_name: str) -> _MemberCopy | None:
    """Return an agent store's records of its teams written by the configured embedder; none when Chroma cannot read it.

    The store is worker-writable in some deployments, so one Chroma cannot open leaves its copies where they are
    instead of stopping startup. Copies of another embedder stay too: nothing read them, and nothing vouches for their
    size.
    """
    try:
        client = _chroma_client(store)
        copy = None
        for collection in client.list_collections():
            if not collection.name.startswith(f"{MEM0_COLLECTION_PREFIX}_") or collection.name.endswith(
                _MEM0_ENTITY_SUFFIX,
            ):
                continue
            current = collection.name == collection_name
            payload = collection.get(include=["embeddings", "metadatas"] if current else ["metadatas"])
            records = [
                _Record(record_id, metadata or {}, payload["embeddings"][index] if current else None)
                for index, (record_id, metadata) in enumerate(zip(payload["ids"], payload["metadatas"], strict=True))
                if _names_member(storage_root, (metadata or {}).get("user_id"), store.parent.name)
            ]
            if current and records:
                copy = _MemberCopy(store, client, collection, records)
            elif records:
                logger.warning("Left Mem0 team copies of another embedder", path=str(store), count=len(records))
    except Exception as error:
        logger.warning("Left a Mem0 store Chroma cannot read", path=str(store), error=str(error))
        return None
    return copy


def _kept_records(copies: list[_MemberCopy]) -> dict[str, list[_Record]]:
    """Return the records each team keeps, by team scope.

    Each member extracted a team turn on its own, so a turn several members hold keeps one extraction, as if the team
    had extracted it once, as it now does: the one holding the most distinct facts, the first member's by name among
    equals. An edit or delete through another member rewrote or removed every fact of that turn in the other copies,
    so a copy it damaged holds fewer.
    """
    extractions: dict[tuple[str, str], list[list[_Record]]] = {}
    for copy in copies:
        by_turn: dict[tuple[str, str], list[_Record]] = {}
        for record in copy.records:
            by_turn.setdefault((record.team, record.turn), []).append(record)
        for key, records in by_turn.items():
            extractions.setdefault(key, []).append(records)
    kept: dict[str, list[_Record]] = {}
    for (scope, _turn), candidates in extractions.items():
        best = max(candidates, key=lambda records: len({record.fact for record in records}))
        kept.setdefault(scope, []).extend(best)
    return kept


def _chroma_client(path: Path) -> Any:  # noqa: ANN401 - chromadb is an optional dependency
    """Open one Chroma store with the settings Mem0 uses, so Mem0 later reuses the same cached system."""
    with optional_dependency_environment():
        from chromadb import Client  # noqa: PLC0415
        from chromadb.config import Settings  # noqa: PLC0415

        return Client(Settings(anonymized_telemetry=False, persist_directory=str(path), is_persistent=True))
