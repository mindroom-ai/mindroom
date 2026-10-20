"""Plan the derived state an agent's rename drops or moves, and report what it leaves as it is.

Knowledge indexes are derived from files and named after the knowledge base they serve. The file-memory search index
of the agent's scope carries the agent's name, so after the rename it is never read again; it is dropped into the
backup and rebuilds under the new name on next use. The Git directory of a knowledge checkout in the agent's state root
is named after the checkout's path, and a handled-turn ledger not imported yet is read under the agent's name, so
both move with the agent. What the rename does not change is reported: the knowledge graph named after the old state
root, private-instance state left from when the agent was private, mem0's history database outside the storage root,
plugin state, config lines that mention the agent in prose, scheduled tasks in Matrix room state, and files of older
MindRoom versions.
Names match exactly, never as substrings, so renaming ``code`` leaves ``code_team`` alone.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.agent_rename.config_edit import config_entity_steps
from mindroom.agent_rename.sqlite_read import read_only_connection
from mindroom.agent_rename.steps import DropPath
from mindroom.agent_rename.workers import move_steps
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import tracking_dir
from mindroom.knowledge.indexing_config import knowledge_git_dir
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.legacy_handled_turns import legacy_responses_file_path
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.runtime_resolution import shared_knowledge_path
from mindroom.tool_system.worker_routing import agent_state_root_path, private_instances_root_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# The spellings below copy names that the modules owning each store keep private; the comments say where they come from.
# knowledge/registry.py's published_index_storage_path keeps each knowledge base's index in knowledge_db/ below the
# binding's storage root, in a directory storage_key_for_base (knowledge/indexing_config.py) names
# `<base id>_<8 hex digits>`, with every character of the base id but letters, digits, `_` and `-` written as `_`.
_KNOWLEDGE_DB = "knowledge_db"
_STORE_DIGEST = "_[0-9a-f]{8}"
# knowledge/file_memory_knowledge.py's _memory_knowledge_base_id: `file_memory_<scope id>_<16 hex digits>`; the scope
# id of an agent, `agent_<name>`, holds no character that either name writes as `_`.
_FILE_MEMORY_BASE_PREFIX = "file_memory_"
_FILE_MEMORY_DIGEST = "_[0-9a-f]{16}"
# knowledge_graph/ingestion.py's _state_path: a graph's ingestion progress, written when ingestion starts the graph.
_GRAPH_INGESTION_STATE = ("knowledge_graph", "ingestion")
# mem0/configs/base.py's default history_db_path, which memory/config.py's _get_memory_config leaves unset,
# and mem0/memory/main.py's _build_session_scope, which keys mem0's recent messages by the memory scope.
_MEM0_DIR_VARIABLE = "MEM0_DIR"
_MEM0_HISTORY_DATABASE = "history.db"
# mem0/memory/storage.py's SQLiteManager keeps the recent messages in this table; older mem0 versions had none.
_MEM0_MESSAGES_TABLE = "messages"
# hooks/context.py's _resolve_plugin_state_root and tool_system/runtime_context.py's get_plugin_state_root.
_PLUGINS = "plugins"
# legacy_handled_turns.py's import_legacy_ledger renames an imported ledger with this suffix appended.
_IMPORTED_LEDGER_SUFFIX = ".imported"
# Legacy files whose readers are gone: the dispatch obligations before the event journal (#1800),
# `dispatch_obligations-<entity>-<12 hex digits>.sqlite3` in tracking/, the session databases before per-agent
# state roots, and the file-memory search indexes before knowledge indexes (#1165), `<scope id>_<16 hex digits>`.
_DISPATCH_OBLIGATIONS_PREFIX = "dispatch_obligations-"
_LEGACY_SESSIONS = "sessions"
_LEGACY_MEMORY_SEARCH = "memory_search_db"
# Agent names are letters, digits and `_`, so a name is mentioned where neither side continues it.
_NAME_CHARACTER = "A-Za-z0-9_"


def index_steps(
    runtime_paths: RuntimePaths,
    *,
    config: Config,
    old_name: str,
    new_name: str,
) -> tuple[list[Step], list[str], list[Path]]:
    """Return the steps that drop or move the agent's derived state, report lines, and the paths that stay.

    The report lines say what the rename leaves as it is; the paths are the ones those lines name as staying.

    The file-memory search indexes of the scope ``agent_<OLD>`` are dropped from ``knowledge_db/``, for every
    file-memory root they were built for. The Git directory of each knowledge checkout below ``agents/<OLD>`` moves
    to the name its path below ``agents/<NEW>`` gives, and ``tracking/<OLD>_responded.json`` moves to the new name
    unless it was imported already. These steps run after the state root moves to ``agents/<NEW>``.
    ``config`` is the config that still names the agent; the config text is read from ``runtime_paths.config_path``.
    Refuses when a move's destination already exists.
    """
    ledger_steps = _ledger_steps(runtime_paths, old_name, new_name)
    steps: list[Step] = [
        *_file_memory_index_drops(runtime_paths, old_name),
        *_knowledge_git_steps(runtime_paths, config, old_name, new_name),
        *ledger_steps,
    ]
    scheduled_tasks = (
        f"Scheduled tasks are Matrix room state, which the rename neither reads nor changes; "
        f"a task whose message mentions @{old_name} keeps that text, so edit such tasks to mention @{new_name}."
    )
    reported = [
        _knowledge_graph_reports(runtime_paths, config, old_name, new_name),
        _private_instance_reports(runtime_paths, old_name),
        _mem0_history_reports(runtime_paths, old_name, new_name),
        _plugin_state_reports(runtime_paths, old_name),
        _config_line_reports(runtime_paths, old_name, new_name),
        ([scheduled_tasks], []),
        _legacy_file_reports(runtime_paths, old_name, moved_ledger=bool(ledger_steps)),
    ]
    reports = [report for lines, _ in reported for report in lines]
    staying = [path for _, paths in reported for path in paths]
    return steps, reports, staying


def _entries(directory: Path, matches: Callable[[str], object]) -> list[Path]:
    """Return the entries of ``directory`` whose names ``matches`` accepts, or none when it is no directory."""
    if not directory.is_dir():
        return []
    return sorted(entry for entry in directory.iterdir() if matches(entry.name))


def _file_memory_index_drops(runtime_paths: RuntimePaths, name: str) -> list[Step]:
    """Drop the file-memory search indexes of the agent's scope, which are named after it and its memory root."""
    # File-memory bases are not private, so their indexes sit below the storage root itself.
    base_prefix = f"{_FILE_MEMORY_BASE_PREFIX}{agent_scope_user_id(name)}"
    store = re.compile(f"{re.escape(base_prefix)}{_FILE_MEMORY_DIGEST}{_STORE_DIGEST}")
    return [
        DropPath(path, f"Drop the file-memory search index {path} of agent {name}; it rebuilds on next use")
        for path in _entries(runtime_paths.storage_root / _KNOWLEDGE_DB, store.fullmatch)
    ]


def _knowledge_git_steps(runtime_paths: RuntimePaths, config: Config, old_name: str, new_name: str) -> list[Step]:
    """Move the Git directory of each Git-backed knowledge checkout in the agent's state root along with it.

    ``knowledge_git_dir`` names a Git directory after its checkout's absolute path, so the moved checkout would find
    none and Git sync would refuse to clone into it, as it is not empty (knowledge/git_source.py's
    _initialize_repository). Every Git command names its work tree, so the moved directory needs no edit.
    """
    storage = runtime_paths.storage_root
    old_root = agent_state_root_path(storage, old_name).resolve()
    new_root = agent_state_root_path(storage, new_name).resolve()
    checkouts: dict[Path, list[str]] = {}
    for base_id, base in config.knowledge_bases.items():
        if base.git is None:
            continue
        try:
            checkout = shared_knowledge_path(base.path, runtime_paths)
        except (OSError, RuntimeError, ValueError):
            # MindRoom cannot sync a base whose path it cannot resolve either.
            continue
        if checkout.is_relative_to(old_root):
            checkouts.setdefault(checkout, []).append(base_id)
    steps: list[Step] = []
    for checkout, base_ids in checkouts.items():
        git_dir = knowledge_git_dir(storage, checkout)
        if os.path.lexists(git_dir):
            moved = knowledge_git_dir(storage, new_root / checkout.relative_to(old_root))
            label = f"the Git directory of knowledge base {', '.join(base_ids)}, named after its checkout path,"
            steps += move_steps(git_dir, moved, label, discard_new_state=False)
    return steps


def _unimported_ledger(runtime_paths: RuntimePaths, name: str) -> Path | None:
    """Return the agent's pre-journal handled-turn ledger when a start has not imported it yet."""
    ledger = legacy_responses_file_path(runtime_paths.storage_root, name)
    imported = ledger.with_name(f"{ledger.name}{_IMPORTED_LEDGER_SUFFIX}")
    return ledger if ledger.is_file() and not os.path.lexists(imported) else None


def _ledger_steps(runtime_paths: RuntimePaths, old_name: str, new_name: str) -> list[Step]:
    """Move a ledger not imported yet to the new name, which the startup import reads it under (bot.py)."""
    ledger = _unimported_ledger(runtime_paths, old_name)
    if ledger is None:
        return []
    destination = legacy_responses_file_path(runtime_paths.storage_root, new_name)
    label = f"the handled-turn ledger of agent {old_name}, not imported yet,"
    return move_steps(ledger, destination, label, discard_new_state=False)


def _private_instance_reports(runtime_paths: RuntimePaths, name: str) -> tuple[list[str], list[Path]]:
    """Report the private-instance state roots of the agent, left from when it was private.

    A private agent keeps one state root per requester, ``<scope>/<agent>`` in ``private_instances/``; the rename
    supports only agents that are not private, so such roots are state of an earlier config.
    """
    roots = [
        root
        for scope in _entries(private_instances_root_path(runtime_paths.storage_root), lambda _: True)
        for root in _entries(scope, lambda entry: entry == name)
    ]
    if not roots:
        return [], []
    report = (
        f"These private-instance state roots of agent {name}, left from when it was private, stay as they are: "
        f"{', '.join(str(root) for root in roots)}."
    )
    return [report], roots


def _knowledge_graph_reports(
    runtime_paths: RuntimePaths,
    config: Config,
    old_name: str,
    new_name: str,
) -> tuple[list[str], list[Path]]:
    """Report the agent's started knowledge graph, which is named after its old state root, with its progress file."""
    graph = graph_scope(old_name, config, runtime_paths, None).graph_name
    progress = runtime_paths.storage_root.joinpath(*_GRAPH_INGESTION_STATE, f"{graph}.json")
    if not progress.exists():
        return [], []
    backfill_days = config.knowledge_graph.ingest.backfill_days
    report = (
        f"The knowledge graph {graph} of agent {old_name} stays in the graph store unused: a graph is named after "
        f"its agent's state root, so {new_name} starts a new graph from the conversations active within "
        f"knowledge_graph.ingest.backfill_days ({backfill_days}) of its first start. To cover older conversations, "
        "raise backfill_days before starting MindRoom, or later together with knowledge_graph.generation, "
        "which starts every graph over."
    )
    return [report], [progress]


def _mem0_history_reports(runtime_paths: RuntimePaths, old_name: str, new_name: str) -> tuple[list[str], list[Path]]:
    """Report the agent's recent messages in mem0's history database, which keys them by the old scope id."""
    mem0_dir = runtime_paths.process_env.get(_MEM0_DIR_VARIABLE) or str(Path("~").expanduser() / ".mem0")
    database = Path(mem0_dir) / _MEM0_HISTORY_DATABASE
    if not database.is_file():
        return [], []
    session_scope = f"user_id={agent_scope_user_id(old_name)}"
    try:
        with read_only_connection(database) as connection:
            has_messages = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_MEM0_MESSAGES_TABLE,),
            ).fetchone()
            # A database without mem0's messages table holds no recent messages to lose.
            found = (
                has_messages
                and connection.execute(
                    f"SELECT 1 FROM {_MEM0_MESSAGES_TABLE} WHERE session_scope = ? LIMIT 1",  # noqa: S608
                    (session_scope,),
                ).fetchone()
            )
    except (OSError, sqlite3.Error) as error:
        report = (
            f"mem0's history database {database} could not be read ({error}), so it may keep recent messages of "
            f"agent {old_name} under the scope {session_scope}, which the rename does not change."
        )
        return [report], [database]
    if not found:
        return [], []
    report = (
        f"mem0's history database {database}, outside the storage root, keeps the recent messages of agent "
        f"{old_name} under the scope {session_scope}, which the rename does not change, so memory extraction "
        f"for {new_name} starts without their context."
    )
    return [report], [database]


def _paths_named(root: Path, names: frozenset[str]) -> list[Path]:
    """Return the paths below ``root`` named one of ``names``, without descending into them or following links."""
    found: list[Path] = []
    for current, directory_names, file_names in os.walk(root):
        found += [Path(current, name) for name in (*directory_names, *file_names) if name in names]
        directory_names[:] = [name for name in directory_names if name not in names]
    return found


def _plugin_state_reports(runtime_paths: RuntimePaths, name: str) -> tuple[list[str], list[Path]]:
    """Report the paths below each plugin's state root named after the agent; each plugin owns its own format."""
    names = frozenset({name, f"{name}.json"})
    paths = sorted(
        path
        for plugin in _entries(runtime_paths.storage_root / _PLUGINS, lambda _: True)
        if plugin.is_dir() and not plugin.is_symlink()
        for path in _paths_named(plugin, names)
    )
    if not paths:
        return [], []
    report = (
        f"These plugin state paths name agent {name}, and the rename leaves them as they are: "
        f"{', '.join(str(path) for path in paths)}."
    )
    return [report], paths


def _config_line_reports(runtime_paths: RuntimePaths, old_name: str, new_name: str) -> tuple[list[str], list[Path]]:
    """Report the config lines, includes and prose files among them, that still mention the agent after the config edit."""
    config_path = runtime_paths.config_path.expanduser()
    rewrites = {
        step.path: step.rewrite
        for step in config_entity_steps(
            config_path,
            old_name=old_name,
            new_name=new_name,
            storage_root=runtime_paths.storage_root,
        )
    }
    mention = re.compile(f"(?<![{_NAME_CHARACTER}]){re.escape(old_name)}(?![{_NAME_CHARACTER}])")
    _, files = load_yaml_config_source(config_path)
    places: list[str] = []
    mentioning: list[Path] = []
    for path in sorted(files):
        content = path.read_bytes()
        if path in rewrites:
            content = rewrites[path](content)
        lines = [
            str(number)
            for number, line in enumerate(content.decode("utf-8", errors="replace").split("\n"), start=1)
            if mention.search(line)
        ]
        if lines:
            places.append(f"{path} (line{'s' if len(lines) > 1 else ''} {', '.join(lines)})")
            mentioning.append(path)
    if not places:
        return [], []
    report = (
        f"These config lines mention {old_name} and the rename does not change them, "
        f"such as other agents' instructions: {'; '.join(places)}."
    )
    return [report], mentioning


def _legacy_file_reports(runtime_paths: RuntimePaths, name: str, *, moved_ledger: bool) -> tuple[list[str], list[Path]]:
    """Report the files older MindRoom versions kept for the agent under its name, except a ledger that moves."""
    storage = runtime_paths.storage_root
    tracking = tracking_dir(runtime_paths)
    ledger = legacy_responses_file_path(storage, name)
    memory_search = re.compile(f"{re.escape(agent_scope_user_id(name))}{_FILE_MEMORY_DIGEST}")

    def session_database(entry: str) -> bool:
        # The database and the side files SQLite keeps beside it.
        return entry == f"{name}.db" or entry.startswith(f"{name}.db-")

    def leftover_ledger(entry: str) -> bool:
        # The pre-journal handled-turn ledger once imported, and copies of it older readers set aside.
        return entry.startswith(ledger.name) and not (moved_ledger and entry == ledger.name)

    paths = [
        *_entries(tracking, lambda entry: entry.startswith(f"{_DISPATCH_OBLIGATIONS_PREFIX}{name}-")),
        *_entries(storage / _LEGACY_SESSIONS, session_database),
        *_entries(storage, session_database),
        *_entries(storage / _LEGACY_MEMORY_SEARCH, memory_search.fullmatch),
        *_entries(tracking, leftover_ledger),
    ]
    if not paths:
        return [], []
    report = (
        f"These files from older MindRoom versions name agent {name} and stay as they are: "
        f"{', '.join(str(path) for path in paths)}."
    )
    return [report], paths
