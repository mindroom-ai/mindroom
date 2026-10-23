"""Plan the file changes outside the event journal after a Matrix server renamed an agent's account in place.

Several stores are named or keyed after the Matrix ID: ``matrix_state.yaml`` holds the account's localpart,
primary-runtime OAuth stores sit in a directory hashed from the requester's ID, worker and private-instance
directories are named after worker keys that embed the requester, and some JSON records hold the ID itself.
After the rename MindRoom looks for all of them under the new ID, so each moves there and its contents follow.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom import yaml_io
from mindroom.agent_rename.matching import replace_user_id
from mindroom.agent_rename.steps import DropPath, FileRewrite, FinalAction, MovePath, RenameRefusedError, SqliteEdit
from mindroom.constants import matrix_state_file, primary_records_dir, resolve_session_state_root
from mindroom.credentials.manager import PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME, scoped_credentials_dir_part
from mindroom.desktop.native_config import native_config_path
from mindroom.matrix.client_session import olm_store_dir, olm_store_exists
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.personal_room_store import personal_room_digest
from mindroom.private_storage.private_instance_identity_store import (
    PRIVATE_INSTANCE_RECORD_FILENAME,
    PrivateInstanceIdentityError,
    load_private_instance_record_payload,
    parse_private_instance_identity_payload,
)
from mindroom.tool_system.worker_routing import (
    agent_state_root_path,
    encode_worker_requester_part,
    normalize_worker_key_part,
    private_instance_scope_root_path,
    private_instances_root_path,
    worker_dir_name,
    worker_log_id,
)
from mindroom.workers.backends.docker import WORKER_CONTROL_DIRNAME
from mindroom.workers.backends.docker_projection import PROJECTED_CONFIGS_DIRNAME
from mindroom.workers.runtime import lease_configured_primary_worker_manager, primary_worker_backend_name

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REQUESTER_SCOPES = frozenset({"user", "user_agent"})
_REQUESTER_OAUTH_STORES = ("*/*_credentials.sqlite3",)
_WORKER_OAUTH_STORES = ("credentials/*_credentials.sqlite3", ".shared_credentials/*_credentials.sqlite3")


def _rewrite_worker_key(worker_key: str, *, old_user_id: str, new_user_id: str) -> str | None:
    """Return ``worker_key`` with its requester changed from ``old_user_id`` to ``new_user_id``.

    Requester keys read ``v1:<tenant>:user:<requester>`` and ``v1:<tenant>:user_agent:<requester>:<agent>``,
    with the requester spelled by ``encode_worker_requester_part``.
    Matrix IDs contain ``:``, so the requester is compared whole rather than split out:
    a ``user`` key ends with it, and in a ``user_agent`` key one colon-free agent component follows it.
    OAuth stores bound before the ``~`` prefix hold the raw ID there and MindRoom still accepts that binding
    (``oauth/legacy_credentials.py``), so that spelling is rewritten in kind.
    Keys of any other requester or scope give ``None``.
    """
    parts = worker_key.split(":", 3)
    if len(parts) != 4 or parts[0] != "v1" or parts[2] not in _REQUESTER_SCOPES:
        return None
    _version, tenant, scope, requester_and_agent = parts
    spellings = (
        (encode_worker_requester_part(old_user_id), encode_worker_requester_part(new_user_id)),
        (old_user_id, new_user_id),
    )
    for old_part, new_part in spellings:
        if scope == "user" and requester_and_agent == old_part:
            return f"v1:{tenant}:user:{new_part}"
        agent = requester_and_agent.removeprefix(f"{old_part}:")
        if scope == "user_agent" and agent != requester_and_agent and agent and ":" not in agent:
            return f"v1:{tenant}:user_agent:{new_part}:{agent}"
    return None


@dataclass(frozen=True)
class _AccountRename:
    old_user_id: str
    new_user_id: str
    discard_new_identity_state: bool

    def worker_key(self, worker_key: str) -> str | None:
        return _rewrite_worker_key(worker_key, old_user_id=self.old_user_id, new_user_id=self.new_user_id)

    def strings(self, value: object) -> object:
        """Return ``value`` with the old ID replaced in every string inside it, by the server's boundary rule."""
        if isinstance(value, str):
            return replace_user_id(value, self.old_user_id, self.new_user_id)
        if isinstance(value, list):
            return [self.strings(item) for item in value]
        if isinstance(value, dict):
            return {key: self.strings(item) for key, item in value.items()}
        return value


def account_file_steps(
    runtime_paths: RuntimePaths,
    *,
    agent: str,
    old_user_id: str,
    new_user_id: str,
    config: Config,
    discard_new_identity_state: bool,
) -> tuple[list[Step], list[str]]:
    """Return the steps that move ``agent``'s files from ``old_user_id`` to ``new_user_id``, and report lines.

    Covers the requester's OAuth directory, workers and private-instance scopes whose keys name the old requester,
    personal-room records, and Matrix send intents of the old sender; ``matrix_state_steps`` covers the account.
    ``config`` is only used when a Docker worker is retired after apply.
    Refuses when a destination already exists.
    MindRoom creates new-ID state when it runs after the server rename; with ``discard_new_identity_state``
    a destination holding no credentials is dropped into the backup before the move instead.
    """
    rename = _AccountRename(old_user_id, new_user_id, discard_new_identity_state)
    oauth_steps, requester_keys = _requester_oauth_steps(runtime_paths.storage_root, rename)
    worker_steps, worker_reports = _worker_steps(runtime_paths, config, rename, requester_keys=requester_keys)
    steps: list[Step] = [
        *oauth_steps,
        *worker_steps,
        *_private_instance_steps(runtime_paths, rename),
        *_personal_room_steps(runtime_paths, agent, rename),
        *_message_send_intent_steps(runtime_paths, rename),
    ]
    return steps, [*worker_reports, *_reports(runtime_paths, old_user_id)]


def _move(source: Path, destination: Path, label: str, rename: _AccountRename) -> list[Step]:
    """Move ``source`` to ``destination``, first dropping a destination that is discardable new-ID state."""
    move = MovePath(source, destination, f"Move {label} {source} to {destination}")
    if not os.path.lexists(destination):
        return [move]
    if not rename.discard_new_identity_state:
        msg = (
            f"Cannot move {source} to {destination}: {destination} already exists. "
            "--discard-new-principal drops new-ID state that holds no credentials."
        )
        raise RenameRefusedError(msg)
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


def _read_json(path: Path) -> object:
    """Return the parsed file, or ``None`` when it is missing or not JSON; MindRoom skips such files too."""
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _field(value: object, name: str) -> object:
    """Return the ``name`` member of a JSON object, or ``None`` for anything else."""
    return cast("dict[str, object]", value).get(name) if isinstance(value, dict) else None


def matrix_state_steps(
    runtime_paths: RuntimePaths,
    *,
    agent: str,
    old_user_id: str,
    new_user_id: str,
) -> tuple[list[Step], list[str]]:
    """Return the step that points ``agent``'s stored account at ``new_user_id``, and report lines.

    MindRoom takes the agent's Matrix ID from this username, so the step belongs last among the reversible ones:
    a run killed before it still shows the old ID.
    An account whose username was already changed by hand keeps the device it logged in with as the new ID.
    A device whose crypto store is under ``old_user_id`` came from the old login instead, because the username was
    changed without it; MindRoom refuses to start a device without a store under its ID, so that one is dropped.
    No step means the stored device stays.
    Refuses when ``matrix_state.yaml`` holds no account for ``agent``.
    """
    path = matrix_state_file(runtime_paths)
    account_key = managed_account_key(agent)
    state = yaml_io.safe_load(path.read_bytes()) if path.exists() else None
    accounts = state.get("accounts") if isinstance(state, dict) else None
    if not isinstance(accounts, dict) or account_key not in accounts:
        msg = f"{path} holds no account {account_key}, so there is no Matrix account to adopt."
        raise RenameRefusedError(msg)
    username = MatrixID.parse(new_user_id).username
    adopt = partial(_adopt_matrix_account, account_key=account_key, username=username)
    if _field(accounts[account_key], "username") != username:
        description = (
            f"Set the username of {account_key} in {path} to {username} and drop its old device and access token"
        )
        return [FileRewrite(path, adopt, description)], []
    device_id = _field(accounts[account_key], "device_id")
    if (
        isinstance(device_id, str)
        and olm_store_exists(old_user_id, device_id, runtime_paths)
        and not olm_store_exists(new_user_id, device_id, runtime_paths)
    ):
        description = (
            f"Drop the device {device_id} and access token of {account_key} in {path}, "
            f"which belong to {old_user_id}, so the next start logs in on a fresh device"
        )
        return [FileRewrite(path, adopt, description)], []
    return [], [f"{path} already names {new_user_id} for {account_key}; its device and access token stay."]


def _adopt_matrix_account(content: bytes, *, account_key: str, username: str) -> bytes:
    state = yaml_io.safe_load(content)
    account = state["accounts"][account_key]
    account["username"] = username
    # The device and token belong to the old ID; without them the next start logs in on a fresh device.
    account["device_id"] = None
    account["access_token"] = None
    # Dumped like matrix/state.py's _write_matrix_state_file.
    return yaml_io.safe_dump(state, default_flow_style=False, sort_keys=False).encode()


def _oauth_worker_keys(database: Path) -> list[str]:
    """Read the worker keys an OAuth credential store is bound to, without writing anything next to it."""
    try:
        with closing(sqlite3.connect(f"{database.absolute().as_uri()}?immutable=1", uri=True)) as connection:
            if not _has_oauth_state(connection):
                return []
            return [key for (key,) in connection.execute("SELECT worker_key FROM oauth_credential_state")]
    except sqlite3.Error as exc:
        msg = f"Cannot read the OAuth credential store {database}: {exc}"
        raise RenameRefusedError(msg) from exc


def _rewrite_oauth_worker_keys(connection: sqlite3.Connection, *, rename: _AccountRename) -> None:
    rows = connection.execute("SELECT singleton, worker_key FROM oauth_credential_state").fetchall()
    for singleton, worker_key in rows:
        new_key = rename.worker_key(worker_key)
        if new_key is not None:
            connection.execute(
                "UPDATE oauth_credential_state SET worker_key = ? WHERE singleton = ?",
                (new_key, singleton),
            )


def _oauth_store_edits(
    source_root: Path,
    destination_root: Path,
    patterns: tuple[str, ...],
    rename: _AccountRename,
) -> tuple[list[SqliteEdit], set[str]]:
    """Return edits for the stores under ``source_root`` bound to the old requester, applied once moved.

    Also returns the old worker keys those stores are bound to.
    """
    edits: list[SqliteEdit] = []
    old_keys: set[str] = set()
    for store in sorted(path for pattern in patterns for path in source_root.glob(pattern)):
        keys = {key for key in _oauth_worker_keys(store) if rename.worker_key(key) is not None}
        if not keys:
            continue
        old_keys |= keys
        database = destination_root / store.relative_to(source_root)
        edits.append(
            SqliteEdit(
                database,
                partial(_rewrite_oauth_worker_keys, rename=rename),
                f"Replace {rename.old_user_id} with {rename.new_user_id} in the worker key of the OAuth store {database}",
            ),
        )
    return edits, old_keys


def _requester_oauth_steps(storage_root: Path, rename: _AccountRename) -> tuple[list[Step], set[str]]:
    """Move the old requester's primary-runtime OAuth directory and rebind its stores to the new requester."""
    oauth_root = storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
    source = oauth_root / scoped_credentials_dir_part(rename.old_user_id)
    if not os.path.lexists(source):
        return [], set()
    destination = oauth_root / scoped_credentials_dir_part(rename.new_user_id)
    moves = _move(source, destination, f"the OAuth credentials of {rename.old_user_id}", rename)
    edits, old_keys = _oauth_store_edits(source, destination, _REQUESTER_OAUTH_STORES, rename)
    return [*moves, *edits], old_keys


def _metadata_worker_key(path: Path) -> str | None:
    worker_key = _field(_read_json(path), "worker_key")
    return worker_key if isinstance(worker_key, str) else None


def _rewrite_worker_metadata(content: bytes, *, old_key: str, new_key: str) -> bytes:
    metadata = json.loads(content)
    metadata["worker_key"] = new_key
    if metadata.get("worker_id") == worker_dir_name(old_key):
        metadata["worker_id"] = worker_dir_name(new_key)
    # Written like workers/backends/_metadata_store.py's save_worker_metadata.
    return json.dumps(metadata, sort_keys=True).encode()


def _worker_state_steps(old_root: Path, new_root: Path, rename: _AccountRename) -> list[Step]:
    """Move one worker root, drop its venv, and rewrite the metadata and OAuth stores inside it at their new place."""
    if not os.path.lexists(old_root):
        return []
    steps = _move(old_root, new_root, "the worker", rename)
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
    metadata_key = _metadata_worker_key(old_root / "metadata" / "worker.json")
    new_metadata_key = None if metadata_key is None else rename.worker_key(metadata_key)
    if metadata_key is not None and new_metadata_key is not None:
        metadata = new_root / "metadata" / "worker.json"
        steps.append(
            FileRewrite(
                metadata,
                partial(_rewrite_worker_metadata, old_key=metadata_key, new_key=new_metadata_key),
                f"Rewrite the worker key in {metadata}",
            ),
        )
    edits, _keys = _oauth_store_edits(old_root, new_root, _WORKER_OAUTH_STORES, rename)
    return [*steps, *edits]


def _retire_docker_worker(runtime_paths: RuntimePaths, config: Config, worker_key: str) -> None:
    """Remove the old key's container; its state already moved, so the backend only finds the container."""
    lease = lease_configured_primary_worker_manager(runtime_paths, runtime_config=config)
    if lease is None:
        msg = "the Docker worker backend is not configured"
        raise RuntimeError(msg)
    with lease as manager:
        retire_worker = getattr(manager, "retire_worker", None)
        if retire_worker is None:
            msg = f"the {manager.backend_name} worker backend cannot retire workers"
            raise RuntimeError(msg)
        retire_worker(worker_key)


def _worker_steps(
    runtime_paths: RuntimePaths,
    config: Config,
    rename: _AccountRename,
    *,
    requester_keys: set[str],
) -> tuple[list[Step], list[str]]:
    """Move every worker whose key names the old requester.

    A worker's key comes from its metadata (the root's own, or the Docker control record) or, for a root
    without metadata, from the requester's OAuth stores. Roots that look like the old requester's but whose
    key is unknown are reported and left in place.
    """
    workers = runtime_paths.storage_root / "workers"
    control = workers / WORKER_CONTROL_DIRNAME
    projections = workers / PROJECTED_CONFIGS_DIRNAME
    metadata_files = (*workers.glob("*/metadata/worker.json"), *control.glob("*/metadata/worker.json"))
    keys = requester_keys | {key for path in metadata_files if (key := _metadata_worker_key(path)) is not None}
    docker = primary_worker_backend_name(runtime_paths) == "docker"
    steps: list[Step] = []
    for old_key in sorted(keys):
        new_key = rename.worker_key(old_key)
        old_dir = worker_dir_name(old_key)
        backend_paths = [path for path in (control / old_dir, projections / old_dir) if os.path.lexists(path)]
        if new_key is None or not (os.path.lexists(workers / old_dir) or backend_paths):
            continue
        steps += _worker_state_steps(workers / old_dir, workers / worker_dir_name(new_key), rename)
        steps += [DropPath(path, f"Drop the old worker's Docker record {path}") for path in backend_paths]
        if docker:
            steps.append(
                FinalAction(
                    partial(_retire_docker_worker, runtime_paths, config, old_key),
                    f"Retire the old Docker worker {worker_log_id(old_key)} of {rename.old_user_id}",
                ),
            )
    known = {worker_dir_name(key) for key in keys}
    return steps, _unknown_worker_reports(workers, known, rename.old_user_id)


def _unknown_worker_reports(workers: Path, known: set[str], old_user_id: str) -> list[str]:
    """Report worker roots whose key is unknown and whose name could hold the old requester.

    A worker directory name turns every run of other characters in the key into ``_``,
    so the old requester appears normalized, followed by ``_`` and the agent or by ``-`` and the digest.
    Anything else after it, like ``.example.org``, belongs to another user.
    """
    if not workers.is_dir():
        return []
    requester = normalize_worker_key_part(encode_worker_requester_part(old_user_id))
    old_requester = re.compile(re.escape(requester) + "[_-]")
    return [
        f"{path} looks like a worker of {old_user_id}, but its worker key is unknown, so it stays in place."
        for path in sorted(workers.iterdir())
        if not path.name.startswith(".") and path.name not in known and old_requester.search(path.name)
    ]


def _private_instance_owner_key(record: Path, scope_name: str, old_user_id: str) -> str | None:
    """Return the worker key of a valid record that the old ID owns for the scope directory it sits in."""
    try:
        payload = load_private_instance_record_payload(record)
        identity = None if payload is None else parse_private_instance_identity_payload(payload)
    except PrivateInstanceIdentityError:
        return None
    if identity is None or identity.requester_id != old_user_id or worker_dir_name(identity.worker_key) != scope_name:
        return None
    return identity.worker_key


def _rewrite_private_instance_record(content: bytes, *, new_key: str, new_user_id: str) -> bytes:
    record = json.loads(content)
    record["worker_key"] = new_key
    record["requester_id"] = new_user_id
    # Written like private_storage/private_instance_identity_store.py's _write_record.
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()


def _private_instance_steps(runtime_paths: RuntimePaths, rename: _AccountRename) -> list[Step]:
    """Move private scopes owned by the old ID, with the primary's record copies below ``tracking/``.

    With ``MINDROOM_SESSION_STORAGE_PATH`` set, a scope's session databases sit in a mirror of the scope below
    that root, which moves too. Links in either namespace to a moved scope are dropped: they are the aliases a startup
    upgrade published at the old owner's key from before the ``~`` prefix, and MindRoom only accepts such an alias
    for the historical key of the scope's current owner, which the new ID never had.
    """
    storage = runtime_paths.storage_root
    scopes = private_instances_root_path(storage)
    session_scopes = resolve_session_state_root(scopes, runtime_paths)
    scope_names = sorted(
        {
            path.name
            for root in (scopes, primary_records_dir(scopes, runtime_paths))
            if root.is_dir()
            for path in root.iterdir()
        },
    )
    steps: list[Step] = []
    for name in scope_names:
        old_roots = (scopes / name, primary_records_dir(scopes / name, runtime_paths))
        owner_keys = [
            _private_instance_owner_key(root / PRIVATE_INSTANCE_RECORD_FILENAME, name, rename.old_user_id)
            for root in old_roots
        ]
        old_key = next((key for key in owner_keys if key is not None), None)
        new_key = None if old_key is None else rename.worker_key(old_key)
        if new_key is None:
            continue
        new_scope = private_instance_scope_root_path(storage, new_key)
        new_roots = (new_scope, primary_records_dir(new_scope, runtime_paths))
        moves = list(zip(old_roots, new_roots, owner_keys, strict=True))
        if session_scopes != scopes:
            moves.append((session_scopes / name, session_scopes / new_scope.name, None))
        for old_root, new_root, owner_key in moves:
            if not os.path.lexists(old_root):
                continue
            steps += _move(old_root, new_root, "the private instance", rename)
            if owner_key is not None:
                record = new_root / PRIVATE_INSTANCE_RECORD_FILENAME
                steps.append(
                    FileRewrite(
                        record,
                        partial(_rewrite_private_instance_record, new_key=new_key, new_user_id=rename.new_user_id),
                        f"Rewrite the owner of {record} to {rename.new_user_id}",
                    ),
                )
        steps += [
            DropPath(link, f"Drop the link {link} to the moved private instance {link.parent / name}")
            for namespace in dict.fromkeys((scopes, session_scopes))
            if namespace.is_dir()
            for link in sorted(namespace.iterdir())
            if link.is_symlink() and link.readlink().as_posix() == name
        ]
    return steps


def _rewrite_personal_room(content: bytes, *, rename: _AccountRename) -> bytes:
    # Written like matrix/personal_room_store.py's write_personal_room.
    return json.dumps(rename.strings(json.loads(content))).encode()


def _personal_room_steps(runtime_paths: RuntimePaths, agent: str, rename: _AccountRename) -> list[Step]:
    """Rewrite every agent's personal-room records that name the old ID; a record of the old ID moves too.

    A record's file name hashes its ``user_id``, so a record owned by the old ID moves to the new ID's name.
    """
    records = primary_records_dir(agent_state_root_path(runtime_paths.storage_root, agent).parent, runtime_paths)
    steps: list[Step] = []
    for path in sorted(records.glob("*/personal_rooms/*.json")):
        record = _read_json(path)
        if record is None or rename.strings(record) == record:
            continue
        destination = path
        if _field(record, "user_id") == rename.old_user_id:
            destination = path.with_name(f"{personal_room_digest(rename.new_user_id)}.json")
            steps += _move(path, destination, "the personal-room record", rename)
        steps.append(
            FileRewrite(
                destination,
                partial(_rewrite_personal_room, rename=rename),
                f"Replace {rename.old_user_id} with {rename.new_user_id} in the personal-room record {destination}",
            ),
        )
    return steps


def _without_sender(content: bytes, *, sender_id: str) -> bytes:
    store = json.loads(content)
    store["sends"] = {key: intent for key, intent in store["sends"].items() if _field(intent, "sender_id") != sender_id}
    # Written like custom_tools/matrix_message_idempotency.py's _write.
    return json.dumps(store).encode()


def _message_send_intent_steps(runtime_paths: RuntimePaths, rename: _AccountRename) -> list[Step]:
    """Remove the old sender's intents from idempotent Matrix send stores; MindRoom refuses to replay them.

    A store is keyed by requester, agent, and room, not by sender, so intents of the new ID in it stay.
    """
    if runtime_paths.control_state_root is None:
        return []
    steps: list[Step] = []
    for path in sorted((runtime_paths.control_state_root / "matrix_message_sends").glob("*.json")):
        sends = _field(_read_json(path), "sends")
        if isinstance(sends, dict) and any(
            _field(intent, "sender_id") == rename.old_user_id for intent in sends.values()
        ):
            steps.append(
                FileRewrite(
                    path,
                    partial(_without_sender, sender_id=rename.old_user_id),
                    f"Remove the Matrix send intents of {rename.old_user_id} from {path}",
                ),
            )
    return steps


def _reports(runtime_paths: RuntimePaths, old_user_id: str) -> list[str]:
    reports: list[str] = []
    crypto_store = olm_store_dir(old_user_id, runtime_paths)
    if crypto_store.exists():
        reports.append(
            f"{crypto_store} stays: only it decrypts the encrypted history of {old_user_id}; "
            "the new device starts its own crypto store.",
        )
    desktop_config_path = native_config_path(runtime_paths.storage_root)
    if _field(_field(_read_json(desktop_config_path), "controller"), "user_id") == old_user_id:
        reports.append(f"{desktop_config_path} pins {old_user_id}; re-pair the desktop bridge after the rename.")
    reports.append(
        f"Records of past tool jobs and scheduled runs outside the event journal keep {old_user_id} as history.",
    )
    return reports
