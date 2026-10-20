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
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom import yaml_io
from mindroom.agent_rename.matching import replace_user_id
from mindroom.agent_rename.steps import DropPath, FileRewrite, RenameRefusedError
from mindroom.agent_rename.workers import (
    DestinationExistsError,
    known_worker_keys,
    move_steps,
    oauth_store_edits,
    worker_move_steps,
)
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
)
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REQUESTER_SCOPES = frozenset({"user", "user_agent"})
_REQUESTER_OAUTH_STORES = ("*/*_credentials.sqlite3",)


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
    try:
        oauth_steps = _requester_oauth_steps(runtime_paths.storage_root, rename)
        worker_steps, worker_reports = worker_move_steps(
            runtime_paths,
            config=config,
            rewrite=rename.worker_key,
            discard_new_state=discard_new_identity_state,
            # Passed from this module so the account-rename tests can replace it here.
            lease_worker_manager=lease_configured_primary_worker_manager,
        )
        steps: list[Step] = [
            *oauth_steps,
            *worker_steps,
            *_private_instance_steps(runtime_paths, rename),
            *_personal_room_steps(runtime_paths, agent, rename),
            *_message_send_intent_steps(runtime_paths, rename),
        ]
    except DestinationExistsError as exc:
        msg = f"{exc} --discard-new-principal drops new-ID state that holds no credentials."
        raise RenameRefusedError(msg) from exc
    reports = [
        *worker_reports,
        *_unknown_worker_reports(runtime_paths.storage_root, old_user_id),
        *_reports(runtime_paths, old_user_id),
    ]
    return steps, reports


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


def _requester_oauth_steps(storage_root: Path, rename: _AccountRename) -> list[Step]:
    """Move the old requester's primary-runtime OAuth directory and rebind its stores to the new requester."""
    oauth_root = storage_root / PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
    source = oauth_root / scoped_credentials_dir_part(rename.old_user_id)
    if not os.path.lexists(source):
        return []
    destination = oauth_root / scoped_credentials_dir_part(rename.new_user_id)
    moves = move_steps(
        source,
        destination,
        f"the OAuth credentials of {rename.old_user_id}",
        discard_new_state=rename.discard_new_identity_state,
    )
    edits = oauth_store_edits(
        source,
        destination,
        _REQUESTER_OAUTH_STORES,
        rewrite=rename.worker_key,
        description=f"Replace {rename.old_user_id} with {rename.new_user_id} in the worker key of the OAuth store",
    )
    return [*moves, *edits]


def _unknown_worker_reports(storage_root: Path, old_user_id: str) -> list[str]:
    """Report worker roots whose key is unknown and whose name could hold the old requester.

    A worker directory name turns every run of other characters in the key into ``_``,
    so the old requester appears normalized, followed by ``_`` and the agent or by ``-`` and the digest.
    Anything else after it, like ``.example.org``, belongs to another user.
    """
    workers = storage_root / "workers"
    if not workers.is_dir():
        return []
    known = {worker_dir_name(key) for key in known_worker_keys(storage_root)}
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
            steps += move_steps(
                old_root,
                new_root,
                "the private instance",
                discard_new_state=rename.discard_new_identity_state,
            )
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
            steps += move_steps(
                path,
                destination,
                "the personal-room record",
                discard_new_state=rename.discard_new_identity_state,
            )
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
