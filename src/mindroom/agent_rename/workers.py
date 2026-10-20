"""Plan moving the worker roots whose worker keys a rename changes.

A worker's state root is named after its worker key (``worker_dir_name``), and its metadata and OAuth stores
hold the key itself. Renaming a requester's Matrix ID or an agent changes the keys that embed it,
so each affected root moves to its new key's name and its contents follow;
the old key's Docker records are dropped and its container is retired after apply.
The moves and OAuth store reads here are shared with the other file providers of a rename.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.agent_rename.steps import DropPath, FileRewrite, FinalAction, MovePath, RenameRefusedError, SqliteEdit
from mindroom.constants import sandbox_startup_manifest_path
from mindroom.credentials.manager import PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
from mindroom.tool_system.worker_routing import worker_dir_name, worker_key_agent_name, worker_log_id
from mindroom.workers.backends.docker import WORKER_CONTROL_DIRNAME
from mindroom.workers.backends.docker_projection import PROJECTED_CONFIGS_DIRNAME
from mindroom.workers.runtime import lease_configured_primary_worker_manager, primary_worker_backend_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.workers.runtime import PrimaryWorkerManagerLease

_WORKER_OAUTH_STORES = ("credentials/*_credentials.sqlite3", ".shared_credentials/*_credentials.sqlite3")
# Primary-runtime OAuth stores sit in private_oauth/<requester or _agents>/<agent or _shared>/.
_PRIMARY_OAUTH_STORES = "*/*/*_credentials.sqlite3"
# The Docker backend bind-mounts an agent's workspace below agents/<the key's agent>/ inside the worker root.
_AGENT_MOUNTS_DIRNAME = "agents"


class DestinationExistsError(RenameRefusedError):
    """A move's destination already exists and is not discarded; a caller may add how to resolve that."""


def move_steps(source: Path, destination: Path, label: str, *, discard_new_state: bool) -> list[Step]:
    """Move ``source`` to ``destination``, first dropping a destination that is discardable new state.

    Without ``discard_new_state`` an existing destination raises ``DestinationExistsError``.
    With it, a destination holding nothing beyond credential-free OAuth stores, lock files and empty directories
    is dropped into the backup before the move, and any other destination refuses.
    """
    move = MovePath(source, destination, f"Move {label} {source} to {destination}")
    if not os.path.lexists(destination):
        return [move]
    if not discard_new_state:
        msg = f"Cannot move {source} to {destination}: {destination} already exists."
        raise DestinationExistsError(msg)
    blocker = _credential_free_blocker(destination)
    if blocker is not None:
        msg = f"Cannot move {source} to {destination}: {destination} already exists and holds {blocker}."
        raise RenameRefusedError(msg)
    return [DropPath(destination, f"Drop {destination}, new-ID state that holds no credentials"), move]


def _credential_free_blocker(path: Path) -> str | None:
    """Describe the first thing under ``path`` beyond credential-free OAuth stores, lock files and empty directories.

    Returns ``None`` when there is nothing else, so dropping ``path`` loses no credentials or data.
    """
    # rglob lists symlinks without following them, so a symlink below is reported, never walked through.
    entries = sorted(path.rglob("*")) if path.is_dir() and not path.is_symlink() else [path]
    for entry in entries:
        if entry.is_symlink():
            return f"the symlink {entry}"
        if entry.is_dir() or entry.name.endswith(".lock"):
            continue
        if not entry.name.endswith("_credentials.sqlite3"):
            return f"the file {entry}"
        if _oauth_store_holds_credentials(entry):
            return f"credentials in {entry}"
    return None


def _oauth_store_holds_credentials(database: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"{database.absolute().as_uri()}?immutable=1", uri=True)) as connection:
            if not _has_oauth_state(connection):
                return False
            query = "SELECT 1 FROM oauth_credential_state WHERE credential_present != 0 LIMIT 1"
            return connection.execute(query).fetchone() is not None
    except sqlite3.Error as exc:
        msg = f"Cannot read the OAuth credential store {database}: {exc}"
        raise RenameRefusedError(msg) from exc


def _has_oauth_state(connection: sqlite3.Connection) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'oauth_credential_state'"
    return connection.execute(query).fetchone() is not None


def oauth_worker_keys(database: Path) -> list[str]:
    """Read the worker keys an OAuth credential store is bound to, without writing anything next to it."""
    try:
        with closing(sqlite3.connect(f"{database.absolute().as_uri()}?immutable=1", uri=True)) as connection:
            if not _has_oauth_state(connection):
                return []
            return [key for (key,) in connection.execute("SELECT worker_key FROM oauth_credential_state")]
    except sqlite3.Error as exc:
        msg = f"Cannot read the OAuth credential store {database}: {exc}"
        raise RenameRefusedError(msg) from exc


def rewrite_oauth_worker_keys(connection: sqlite3.Connection, *, rewrite: Callable[[str], str | None]) -> None:
    """Replace every worker key of an OAuth credential store that ``rewrite`` changes."""
    rows = connection.execute("SELECT singleton, worker_key FROM oauth_credential_state").fetchall()
    for singleton, worker_key in rows:
        new_key = rewrite(worker_key)
        if new_key is not None:
            connection.execute(
                "UPDATE oauth_credential_state SET worker_key = ? WHERE singleton = ?",
                (new_key, singleton),
            )


def oauth_store_edits(
    source_root: Path,
    destination_root: Path,
    patterns: tuple[str, ...],
    *,
    rewrite: Callable[[str], str | None],
    description: str,
) -> list[SqliteEdit]:
    """Return edits for the stores under ``source_root`` bound to a key ``rewrite`` changes, applied once moved.

    Each edit is described by ``description`` followed by the store's moved path.
    """
    edits: list[SqliteEdit] = []
    for store in sorted(path for pattern in patterns for path in source_root.glob(pattern)):
        if not any(rewrite(key) is not None for key in oauth_worker_keys(store)):
            continue
        database = destination_root / store.relative_to(source_root)
        edits.append(
            SqliteEdit(database, partial(rewrite_oauth_worker_keys, rewrite=rewrite), f"{description} {database}"),
        )
    return edits


def _metadata_worker_key(path: Path) -> str | None:
    """Return the ``worker_key`` of a metadata file, or ``None`` when it is missing or unreadable, as MindRoom skips it."""
    try:
        metadata = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    worker_key = cast("dict[str, object]", metadata).get("worker_key") if isinstance(metadata, dict) else None
    return worker_key if isinstance(worker_key, str) else None


def _primary_oauth_worker_keys(storage_root: Path) -> set[str]:
    """Return the worker keys the primary-runtime OAuth stores are bound to.

    A store that cannot be read names no key here; the provider that rewrites a store reads it again and refuses.
    """
    keys: set[str] = set()
    for store in (storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME).glob(_PRIMARY_OAUTH_STORES):
        try:
            keys.update(oauth_worker_keys(store))
        except RenameRefusedError:
            continue
    return keys


def known_worker_keys(storage_root: Path) -> set[str]:
    """Return the worker keys named by worker metadata, Docker control records, and primary-runtime OAuth stores.

    Docker worker roots that credential sync created hold no metadata, so the OAuth stores bound to them
    are the only record of their key.
    """
    workers = storage_root / "workers"
    control = workers / WORKER_CONTROL_DIRNAME
    metadata_files = (*workers.glob("*/metadata/worker.json"), *control.glob("*/metadata/worker.json"))
    metadata_keys = {key for path in metadata_files if (key := _metadata_worker_key(path)) is not None}
    return metadata_keys | _primary_oauth_worker_keys(storage_root)


def _rewrite_worker_metadata(content: bytes, *, old_key: str, new_key: str) -> bytes:
    metadata = json.loads(content)
    metadata["worker_key"] = new_key
    if metadata.get("worker_id") == worker_dir_name(old_key):
        metadata["worker_id"] = worker_dir_name(new_key)
    # Written like workers/backends/_metadata_store.py's save_worker_metadata.
    return json.dumps(metadata, sort_keys=True).encode()


def _agent_mount_steps(old_root: Path, new_root: Path, *, old_key: str, new_key: str) -> list[Step]:
    """Move the agent's mount point inside a moved worker root when the key names another agent."""
    old_agent, new_agent = worker_key_agent_name(old_key), worker_key_agent_name(new_key)
    old_mounts = old_root / _AGENT_MOUNTS_DIRNAME
    if old_agent is None or new_agent is None or old_agent == new_agent or not os.path.lexists(old_mounts / old_agent):
        return []
    if os.path.lexists(old_mounts / new_agent):
        msg = f"Cannot move {old_mounts / old_agent} to {old_mounts / new_agent}: {old_mounts / new_agent} already exists."
        raise DestinationExistsError(msg)
    source, destination = new_root / _AGENT_MOUNTS_DIRNAME / old_agent, new_root / _AGENT_MOUNTS_DIRNAME / new_agent
    return [
        MovePath(source, destination, f"Move the worker's mount point of agent {old_agent} {source} to {destination}"),
    ]


def _worker_state_steps(
    old_root: Path,
    new_root: Path,
    *,
    old_key: str,
    new_key: str,
    rewrite: Callable[[str], str | None],
    discard_new_state: bool,
) -> list[Step]:
    """Move one worker root, drop its venv and startup manifest, and rewrite what names the old key, at its new place."""
    if not os.path.lexists(old_root):
        return []
    steps = move_steps(old_root, new_root, "the worker", discard_new_state=discard_new_state)
    # The venv's console scripts name the old root in their shebangs, so the moved copy is dropped.
    # MindRoom rebuilds a missing venv on the worker's next use (workers/backends/local.py's venv_dir).
    if os.path.lexists(old_root / "venv"):
        venv = new_root / "venv"
        steps.append(
            DropPath(
                venv,
                f"Drop the moved worker's virtual environment {venv}; MindRoom rebuilds it on the worker's next use, "
                "so packages installed into it must be installed again",
            ),
        )
    # The Docker backend's startup manifest names the old key. It writes the manifest again before it creates the
    # container of the new key, which no container has yet (workers/backends/docker.py's _write_startup_manifest).
    if os.path.lexists(sandbox_startup_manifest_path(old_root)):
        manifest = sandbox_startup_manifest_path(new_root)
        steps.append(
            DropPath(
                manifest,
                f"Drop the moved worker's startup manifest {manifest}, which names the old worker key; "
                "MindRoom writes it again when it creates the worker's container",
            ),
        )
    steps += _agent_mount_steps(old_root, new_root, old_key=old_key, new_key=new_key)
    metadata_key = _metadata_worker_key(old_root / "metadata" / "worker.json")
    new_metadata_key = None if metadata_key is None else rewrite(metadata_key)
    if metadata_key is not None and new_metadata_key is not None:
        metadata = new_root / "metadata" / "worker.json"
        steps.append(
            FileRewrite(
                metadata,
                partial(_rewrite_worker_metadata, old_key=metadata_key, new_key=new_metadata_key),
                f"Rewrite the worker key in {metadata}",
            ),
        )
    edits = oauth_store_edits(
        old_root,
        new_root,
        _WORKER_OAUTH_STORES,
        rewrite=rewrite,
        description="Rewrite the worker key in the OAuth store",
    )
    return [*steps, *edits]


def _retire_docker_worker(
    lease_worker_manager: Callable[..., PrimaryWorkerManagerLease | None],
    runtime_paths: RuntimePaths,
    config: Config,
    worker_key: str,
) -> None:
    """Remove the old key's container; its state already moved, so the backend only finds the container."""
    lease = lease_worker_manager(runtime_paths, runtime_config=config)
    if lease is None:
        msg = "the Docker worker backend is not configured"
        raise RuntimeError(msg)
    with lease as manager:
        retire_worker = getattr(manager, "retire_worker", None)
        if retire_worker is None:
            msg = f"the {manager.backend_name} worker backend cannot retire workers"
            raise RuntimeError(msg)
        retire_worker(worker_key)


def worker_move_steps(
    runtime_paths: RuntimePaths,
    *,
    config: Config,
    rewrite: Callable[[str], str | None],
    discard_new_state: bool,
    lease_worker_manager: Callable[..., PrimaryWorkerManagerLease | None] | None = None,
) -> tuple[list[Step], list[str]]:
    """Return the steps that move every known worker whose key ``rewrite`` changes, and report lines.

    ``rewrite`` returns a worker key's new spelling, or ``None`` for keys the rename leaves alone.
    Known keys are those of ``known_worker_keys``. A worker whose key is unknown stays in place;
    only callers that recognize its directory name can report it, so this reports nothing itself.
    ``discard_new_state`` is passed to ``move_steps``.
    ``config`` is only used when a Docker worker is retired after apply,
    through ``lease_worker_manager`` (the configured primary worker manager unless given).
    """
    lease = lease_worker_manager or lease_configured_primary_worker_manager
    workers = runtime_paths.storage_root / "workers"
    control = workers / WORKER_CONTROL_DIRNAME
    projections = workers / PROJECTED_CONFIGS_DIRNAME
    docker = primary_worker_backend_name(runtime_paths) == "docker"
    steps: list[Step] = []
    for old_key in sorted(known_worker_keys(runtime_paths.storage_root)):
        new_key = rewrite(old_key)
        old_dir = worker_dir_name(old_key)
        backend_paths = [path for path in (control / old_dir, projections / old_dir) if os.path.lexists(path)]
        if new_key is None or not (os.path.lexists(workers / old_dir) or backend_paths):
            continue
        steps += _worker_state_steps(
            workers / old_dir,
            workers / worker_dir_name(new_key),
            old_key=old_key,
            new_key=new_key,
            rewrite=rewrite,
            discard_new_state=discard_new_state,
        )
        steps += [DropPath(path, f"Drop the old worker's Docker record {path}") for path in backend_paths]
        if docker:
            steps.append(
                FinalAction(
                    partial(_retire_docker_worker, lease, runtime_paths, config, old_key),
                    f"Retire the old Docker worker {worker_log_id(old_key)}",
                ),
            )
    return steps, []
