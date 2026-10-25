"""Plan carrying an agent's long-term memory to its new name.

mem0 keeps memories in a Chroma store at each agent's state root (``<state root>/chroma``, see
``memory/config.py``), and tags each memory with its scope: ``user_id`` ``agent_<name>`` for the agent's own memory,
``team_<members sorted, joined by +>`` for a team's. A team's memory is written to the store of every member,
for configured and ad-hoc teams alike. File memory without a workspace keeps one directory per scope,
named after the scope id, in ``<state root>/memory_files/``.
So the scope ids naming the agent change in the stores of every configured agent, its own included,
which sits under ``agents/<NEW>/`` because the state root moves first.
Names match exactly, never as substrings, so renaming ``code`` leaves ``code_team`` and ``team_code_team+helper`` alone.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import sqlite3
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.agent_rename.sqlite_read import read_only_connection
from mindroom.agent_rename.steps import DirectoryEdit, MovePath, RenameRefusedError
from mindroom.agent_rename.workers import DestinationExistsError
from mindroom.memory_scope_ids import agent_scope_user_id, build_team_user_id
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from chromadb.api.types import Metadata

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# The mem0 Chroma store inside a state root (memory/config.py's _get_memory_config) and its SQLite file.
_CHROMA_DIRECTORY = "chroma"
_CHROMA_DATABASE = "chroma.sqlite3"
# File memory's scope directories inside a state root (memory/_shared.py's FILE_MEMORY_DEFAULT_DIRNAME).
_FILE_MEMORY_DIRECTORY = "memory_files"
# The prefix build_team_user_id puts before the sorted member names.
_TEAM_SCOPE_PREFIX = "team_"


def memory_steps(runtime_paths: RuntimePaths, *, config: Config, old_name: str, new_name: str) -> list[Step]:
    """Return the steps that rename the memory scopes naming the agent in every configured agent's stores.

    In each Chroma store, memories of scope ``agent_<OLD>`` and of team scopes with OLD as a member get
    the new scope id, through the Chroma client, which keeps Chroma's full-text index in step.
    File memory scope directories with those names are renamed the same way.
    Refuses when a store already holds a new scope id.
    """
    storage_root = runtime_paths.storage_root
    old_root, new_root = agent_state_root_path(storage_root, old_name), agent_state_root_path(storage_root, new_name)
    # Keyed by the resolved path: an agent whose state root is a symlink to another's shares its stores,
    # and planning them twice would move the same scope directory twice.
    roots: dict[Path, Path] = {}
    for name in config.agents:
        root = agent_state_root_path(storage_root, name)
        roots.setdefault(root.resolve(), root)
    steps: list[Step] = []
    for resolved, root in roots.items():
        # The state root moves first, so steps for the agent's own stores address them under the new root.
        destination = new_root if resolved == old_root.resolve() else root
        steps += _chroma_steps(root / _CHROMA_DIRECTORY, destination / _CHROMA_DIRECTORY, old_name, new_name)
        steps += _file_memory_steps(
            root / _FILE_MEMORY_DIRECTORY,
            destination / _FILE_MEMORY_DIRECTORY,
            old_name,
            new_name,
        )
    return steps


def _team_members(scope_user_id: str) -> list[str]:
    """Return the member names of a team scope id, or no names for any other scope."""
    if not scope_user_id.startswith(_TEAM_SCOPE_PREFIX):
        return []
    return scope_user_id.removeprefix(_TEAM_SCOPE_PREFIX).split("+")


def _renamed_scope_user_id(scope_user_id: str, old_name: str, new_name: str) -> str | None:
    """Return the scope id ``scope_user_id`` becomes, or ``None`` when it does not name the agent."""
    if scope_user_id == agent_scope_user_id(old_name):
        return agent_scope_user_id(new_name)
    members = _team_members(scope_user_id)
    if old_name not in members:
        return None
    return build_team_user_id([new_name if member == old_name else member for member in members])


def _scope_renames(scope_user_ids: set[str], *, location: Path, old_name: str, new_name: str) -> dict[str, str]:
    """Map each scope id naming the agent to its new id.

    Refuses when a new id is already in use at ``location``, or when a team scope already names the new name
    beside the old one, since renaming would merge the two members.
    """
    renames: dict[str, str] = {}
    for scope_user_id in sorted(scope_user_ids):
        renamed = _renamed_scope_user_id(scope_user_id, old_name, new_name)
        if renamed is None:
            continue
        if new_name in _team_members(scope_user_id):
            msg = f"Cannot rename memory scope {scope_user_id} in {location}: {new_name} is already one of its members."
            raise RenameRefusedError(msg)
        if renamed in scope_user_ids:
            msg = f"Cannot rename memory scope {scope_user_id} to {renamed} in {location}: {renamed} already exists there."
            raise DestinationExistsError(msg)
        renames[scope_user_id] = renamed
    return renames


def _chroma_steps(store: Path, destination: Path, old_name: str, new_name: str) -> list[Step]:
    """Plan one edit of the Chroma store ``store``, found at ``destination`` once the state root has moved."""
    database = store / _CHROMA_DATABASE
    if not database.is_file():
        return []
    try:
        with read_only_connection(database) as connection:
            rows = connection.execute(
                "SELECT DISTINCT string_value FROM embedding_metadata "
                "WHERE key = 'user_id' AND string_value IS NOT NULL",
            ).fetchall()
    except sqlite3.Error as error:
        msg = f"Cannot read the memory scopes in {database}: {error}."
        raise RenameRefusedError(msg) from error
    renames = _scope_renames({value for (value,) in rows}, location=store, old_name=old_name, new_name=new_name)
    if not renames:
        return []
    if store.is_symlink():
        # The edit swaps a copy in, which would replace the link and leave the store it points to unedited.
        msg = (
            f"Cannot rename the memory scopes in {store}: it is a symlink. Replace it with the directory it points to."
        )
        raise RenameRefusedError(msg)
    listed = ", ".join(f"{old} to {new}" for old, new in renames.items())
    return [
        DirectoryEdit(
            destination,
            partial(_rename_chroma_scopes, renames=renames, old_name=old_name, new_name=new_name),
            f"Rename the memory scopes {listed} in {destination}",
        ),
    ]


def _rename_chroma_scopes(store: Path, *, renames: Mapping[str, str], old_name: str, new_name: str) -> None:
    """Give every memory in ``store`` whose scope is renamed its new scope, in all of the store's collections.

    MindRoom's own agent fields in those memories, ``agent`` and the ``team_members`` list, take the new name too.
    """
    # Imported here: chromadb is heavy, and only an apply that edits a memory store needs it.
    from chromadb.api.client import Client  # noqa: PLC0415
    from chromadb.config import Settings  # noqa: PLC0415

    # The settings of chromadb.PersistentClient, as a client that releases the store when closed.
    settings = Settings(anonymized_telemetry=False, is_persistent=True, persist_directory=str(store))
    with Client(settings=settings) as client:
        batch_size = client.get_max_batch_size()
        for collection in client.list_collections():
            where = {"user_id": {"$in": list(renames)}}
            found = collection.get(where=where, include=["metadatas"])
            ids = found["ids"]
            metadatas = [
                _renamed_metadata(metadata, renames=renames, old_name=old_name, new_name=new_name)
                for metadata in found["metadatas"] or []
            ]
            for start in range(0, len(ids), batch_size):
                collection.update(ids=ids[start : start + batch_size], metadatas=metadatas[start : start + batch_size])
            if left := collection.get(where=where, limit=1)["ids"]:
                msg = f"Memory {left[0]} in collection {collection.name} of {store} kept its old scope."
                raise RuntimeError(msg)


def _renamed_metadata(metadata: Metadata, *, renames: Mapping[str, str], old_name: str, new_name: str) -> Metadata:
    renamed = dict(metadata)
    renamed["user_id"] = renames[str(metadata["user_id"])]
    if renamed.get("agent") == old_name:
        renamed["agent"] = new_name
    if isinstance(members := metadata.get("team_members"), list):
        # MindRoom writes the member names; ty cannot tell this list from a list subclass of SparseVector.
        renamed["team_members"] = [new_name if member == old_name else member for member in cast("list[str]", members)]
    return renamed


def _file_memory_steps(directory: Path, destination: Path, old_name: str, new_name: str) -> list[Step]:
    """Plan renaming the scope directories in ``directory``, found at ``destination`` once the state root has moved.

    A scope directory's name is its scope id: the names of agents and their teams contain no character that
    the file backend's ``_scope_dir_name`` replaces.
    """
    if not directory.is_dir():
        return []
    names = {entry.name for entry in directory.iterdir()}
    renames = _scope_renames(names, location=directory, old_name=old_name, new_name=new_name)
    steps: list[Step] = []
    for old, new in renames.items():
        source, target = destination / old, destination / new
        steps.append(MovePath(source, target, f"Move the file memory {source} to {target}"))
    return steps
