"""Matrix thread export helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from urllib.parse import quote

import yaml

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.durable_write import fsync_directory
from mindroom.entity_resolution import MissingManagedEntityAccountError
from mindroom.logging_config import get_logger
from mindroom.matrix.client_thread_history import (
    enumerate_room_thread_root_ids,
    fetch_thread_history,
    refresh_thread_history_from_source,
)
from mindroom.matrix.client_visible_messages import trusted_visible_sender_ids
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.invited_rooms_store import invited_room_entity_names, invited_rooms_path, load_invited_rooms
from mindroom.matrix.state import MatrixRoom, matrix_state_for_runtime
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, INTERNAL_USER_AGENT_NAME, AgentMatrixUser, login_agent_user
from mindroom.matrix_identifiers import extract_server_name_from_homeserver
from mindroom.runtime_support import build_owned_runtime_support, close_owned_runtime_support

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.state import MatrixAccount


logger = get_logger(__name__)
_EXPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _ThreadExportRoom:
    """One Matrix room selected for thread export."""

    key: str
    room_id: str
    alias: str
    name: str


@dataclass(frozen=True)
class _ThreadExportFailure:
    """One room or thread export failure."""

    room_key: str
    room_id: str
    thread_id: str | None
    error: str


@dataclass(frozen=True)
class ThreadExportStats:
    """Summary for one export pass."""

    output_dir: Path
    rooms_exported: int = 0
    threads_seen: int = 0
    threads_exported: int = 0
    threads_unchanged: int = 0
    truncated_rooms: int = 0
    failed_items: tuple[_ThreadExportFailure, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> int:
        """Return failed room/thread count."""
        return len(self.failed_items)


def _default_thread_export_dir(runtime_paths: RuntimePaths) -> Path:
    """Return default thread export output directory."""
    return runtime_paths.storage_root / "thread_exports"


def _safe_path_segment(value: str) -> str:
    """Return one filesystem-safe path segment while keeping Matrix IDs reversible."""
    encoded = quote(value.strip() or "unknown", safe="")
    if encoded in {".", ".."}:
        return encoded.replace(".", "%2E")
    return encoded


def _timestamp_iso(timestamp_ms: int) -> str | None:
    """Return UTC ISO timestamp for one Matrix millisecond timestamp."""
    if timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def _message_payload(message: ResolvedVisibleMessage) -> dict[str, object]:
    """Return one grep-friendly YAML message entry."""
    payload: dict[str, object] = {
        "event_id": message.event_id,
        "latest_event_id": message.latest_event_id,
        "sender": message.sender,
        "timestamp": message.timestamp,
        "body": message.body,
    }
    if timestamp_iso := _timestamp_iso(message.timestamp):
        payload["timestamp_iso"] = timestamp_iso
    if message.thread_id is not None:
        payload["thread_id"] = message.thread_id
    if message.reply_to_event_id is not None:
        payload["reply_to_event_id"] = message.reply_to_event_id
    if message.stream_status is not None:
        payload["stream_status"] = message.stream_status
    msgtype = message.content.get("msgtype")
    if isinstance(msgtype, str) and msgtype != "m.text":
        payload["msgtype"] = msgtype
    return payload


def _thread_payload(
    *,
    room: _ThreadExportRoom,
    thread_id: str,
    messages: list[ResolvedVisibleMessage],
    exported_at: datetime,
) -> dict[str, object]:
    """Build one YAML document for a Matrix thread."""
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "room": {
            "key": room.key,
            "id": room.room_id,
            "name": room.name,
            "alias": room.alias,
        },
        "thread": {
            "id": thread_id,
            "source": "matrix",
            "exported_at": exported_at.isoformat(),
            "message_count": len(messages),
        },
        "messages": [_message_payload(message) for message in messages],
    }


def _write_yaml_atomic(path: Path, payload: dict[str, object]) -> None:
    """Atomically write one YAML payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            yaml.safe_dump(payload, temp_file, default_flow_style=False, sort_keys=False, allow_unicode=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
        fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _thread_export_path(output_dir: Path, room: _ThreadExportRoom, thread_id: str) -> Path:
    """Return output path for one thread export."""
    return output_dir / _safe_path_segment(room.key) / f"{_safe_path_segment(thread_id)}.yaml"


def _payload_without_exported_at(payload: dict[str, object]) -> dict[str, object]:
    """Return one thread payload with the per-pass exported_at timestamp removed."""
    normalized = dict(payload)
    thread = normalized.get("thread")
    if isinstance(thread, dict):
        normalized["thread"] = {key: value for key, value in thread.items() if key != "exported_at"}
    return normalized


def _existing_payload_matches(path: Path, payload: dict[str, object]) -> bool:
    """Return whether one existing export file already holds this payload, ignoring exported_at."""
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(existing, dict):
        return False
    return _payload_without_exported_at(existing) == _payload_without_exported_at(payload)


def _export_rooms(runtime_paths: RuntimePaths, room_filter: str | None) -> list[_ThreadExportRoom]:
    """Return persisted Matrix rooms selected for export."""
    rooms = matrix_state_for_runtime(runtime_paths).rooms
    selected_rooms: list[_ThreadExportRoom] = []
    normalized_filter = room_filter.strip() if isinstance(room_filter, str) and room_filter.strip() else None
    for room_key, room in rooms.items():
        if normalized_filter is not None and not _room_matches_filter(room_key, room, normalized_filter):
            continue
        selected_rooms.append(
            _ThreadExportRoom(
                key=room_key,
                room_id=room.room_id,
                alias=room.alias,
                name=room.name,
            ),
        )
    return selected_rooms


def _room_matches_filter(room_key: str, room: MatrixRoom, room_filter: str) -> bool:
    """Return whether one persisted room matches a CLI filter."""
    normalized_filter = room_filter.casefold()
    return any(
        normalized_filter in candidate.casefold()
        for candidate in (room_key, room.room_id, room.alias, room.name)
        if candidate
    )


def _invited_export_rooms(
    config: Config,
    runtime_paths: RuntimePaths,
    room_filter: str | None,
    *,
    known_room_ids: set[str],
) -> list[tuple[str, list[_ThreadExportRoom]]]:
    """Return invited rooms grouped by the entity whose account is a member."""
    normalized_filter = room_filter.strip().casefold() if isinstance(room_filter, str) and room_filter.strip() else None
    grouped: list[tuple[str, list[_ThreadExportRoom]]] = []
    for entity_name in invited_room_entity_names(config):
        entity_rooms: list[_ThreadExportRoom] = []
        for room_id in sorted(load_invited_rooms(invited_rooms_path(runtime_paths.storage_root, entity_name))):
            if room_id in known_room_ids:
                continue
            if normalized_filter is not None and normalized_filter not in room_id.casefold():
                continue
            known_room_ids.add(room_id)
            entity_rooms.append(_ThreadExportRoom(key=room_id, room_id=room_id, alias="", name=""))
        if entity_rooms:
            grouped.append((entity_name, entity_rooms))
    return grouped


def _trusted_sender_ids_for_export(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return trusted senders when Matrix accounts have already been prepared."""
    try:
        return trusted_visible_sender_ids(config, runtime_paths)
    except MissingManagedEntityAccountError:
        return frozenset()


async def _export_threads_for_client(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    rooms: Sequence[_ThreadExportRoom],
    output_dir: Path | None = None,
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
) -> ThreadExportStats:
    """Export Matrix-source thread histories to YAML files."""
    resolved_output_dir = output_dir or _default_thread_export_dir(runtime_paths)
    trusted_sender_ids = _trusted_sender_ids_for_export(config, runtime_paths)
    failures: list[_ThreadExportFailure] = []
    rooms_exported = 0
    threads_seen = 0
    threads_exported = 0
    threads_unchanged = 0
    truncated_rooms = 0

    for room in rooms:
        try:
            thread_ids, truncated = await enumerate_room_thread_root_ids(
                client,
                room.room_id,
                max_thread_roots=max_thread_roots,
            )
        except Exception as exc:
            failures.append(
                _ThreadExportFailure(
                    room_key=room.key,
                    room_id=room.room_id,
                    thread_id=None,
                    error=str(exc),
                ),
            )
            continue

        rooms_exported += 1
        if truncated:
            truncated_rooms += 1
        threads_seen += len(thread_ids)

        for thread_id in thread_ids:
            try:
                if prefer_cache:
                    history = await fetch_thread_history(
                        client,
                        room.room_id,
                        thread_id,
                        event_cache,
                        trusted_sender_ids=trusted_sender_ids,
                        caller_label="thread_export",
                    )
                else:
                    history = await refresh_thread_history_from_source(
                        client,
                        room.room_id,
                        thread_id,
                        event_cache,
                        allow_stale_fallback=False,
                        trusted_sender_ids=trusted_sender_ids,
                        caller_label="thread_export",
                    )
                payload = _thread_payload(
                    room=room,
                    thread_id=thread_id,
                    messages=list(history),
                    exported_at=datetime.now(UTC),
                )
                export_path = _thread_export_path(resolved_output_dir, room, thread_id)
                if _existing_payload_matches(export_path, payload):
                    threads_unchanged += 1
                else:
                    _write_yaml_atomic(export_path, payload)
            except Exception as exc:
                failures.append(
                    _ThreadExportFailure(
                        room_key=room.key,
                        room_id=room.room_id,
                        thread_id=thread_id,
                        error=str(exc),
                    ),
                )
                continue
            threads_exported += 1

    return ThreadExportStats(
        output_dir=resolved_output_dir,
        rooms_exported=rooms_exported,
        threads_seen=threads_seen,
        threads_exported=threads_exported,
        threads_unchanged=threads_unchanged,
        truncated_rooms=truncated_rooms,
        failed_items=tuple(failures),
    )


def _account_user_from_state(
    *,
    account_key: str,
    account: MatrixAccount,
    homeserver: str,
    runtime_paths: RuntimePaths,
) -> AgentMatrixUser:
    """Build one login-ready Matrix user from persisted state credentials."""
    domain = account.domain or extract_server_name_from_homeserver(homeserver, runtime_paths=runtime_paths)
    entity_name = (
        INTERNAL_USER_AGENT_NAME
        if account_key == INTERNAL_USER_ACCOUNT_KEY
        else account_key.removeprefix(
            "agent_",
        )
    )
    return AgentMatrixUser(
        agent_name=entity_name,
        user_id=MatrixID.from_username(account.username, domain).full_id,
        display_name=entity_name,
        password=account.password,
        device_id=account.device_id,
        access_token=account.access_token,
    )


def _select_export_account(runtime_paths: RuntimePaths, homeserver: str) -> AgentMatrixUser:
    """Select a persisted Matrix account for export reads."""
    state = matrix_state_for_runtime(runtime_paths)
    preferred_keys = [INTERNAL_USER_ACCOUNT_KEY, managed_account_key(ROUTER_AGENT_NAME)]
    candidate_keys = [*preferred_keys, *state.accounts]
    seen_keys: set[str] = set()

    for account_key in candidate_keys:
        if account_key in seen_keys:
            continue
        seen_keys.add(account_key)
        account = state.accounts.get(account_key)
        if account is None:
            continue
        return _account_user_from_state(
            account_key=account_key,
            account=account,
            homeserver=homeserver,
            runtime_paths=runtime_paths,
        )

    msg = "No persisted Matrix account found in matrix_state.yaml. Run MindRoom once before exporting threads."
    raise RuntimeError(msg)


async def export_threads_once(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    output_dir: Path | None = None,
    room_filter: str | None = None,
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
) -> ThreadExportStats:
    """Login using persisted Matrix credentials and run one export pass.

    Rooms come from ``matrix_state.yaml`` plus every entity's persisted invited rooms.
    Invited rooms are exported with the invited entity's own account, because the primary export
    account is not necessarily a member of user-created rooms.

    With ``prefer_cache`` thread bodies are served from the validated durable event cache and only
    fetched from the homeserver on miss or invalidation; a failing miss-refetch may then fall back to
    stale cached rows instead of failing the thread.
    Only use it while the runtime keeps the cache fresh (in-process or alongside a live ``mindroom run``).
    """
    homeserver = runtime_matrix_homeserver(runtime_paths=runtime_paths)
    resolved_output_dir = output_dir or _default_thread_export_dir(runtime_paths)
    state_rooms = _export_rooms(runtime_paths, room_filter)
    invited_groups = _invited_export_rooms(
        config,
        runtime_paths,
        room_filter,
        known_room_ids={room.room_id for room in state_rooms},
    )
    failures: list[_ThreadExportFailure] = []
    export_groups: list[tuple[AgentMatrixUser, list[_ThreadExportRoom]]] = []
    if state_rooms:
        export_groups.append((_select_export_account(runtime_paths, homeserver), list(state_rooms)))
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    for entity_name, entity_rooms in invited_groups:
        account_key = managed_account_key(entity_name)
        account = accounts.get(account_key)
        if account is None:
            failures.extend(
                _ThreadExportFailure(
                    room_key=room.key,
                    room_id=room.room_id,
                    thread_id=None,
                    error=f"No persisted Matrix account for invited-room entity '{entity_name}'",
                )
                for room in entity_rooms
            )
            continue
        export_groups.append(
            (
                _account_user_from_state(
                    account_key=account_key,
                    account=account,
                    homeserver=homeserver,
                    runtime_paths=runtime_paths,
                ),
                entity_rooms,
            ),
        )

    if not export_groups:
        _select_export_account(runtime_paths, homeserver)
        return ThreadExportStats(output_dir=resolved_output_dir, failed_items=tuple(failures))

    rooms_exported = 0
    threads_seen = 0
    threads_exported = 0
    threads_unchanged = 0
    truncated_rooms = 0
    support = build_owned_runtime_support(
        cache_config=config.cache,
        runtime_paths=runtime_paths,
        logger=logger,
        background_task_owner=object(),
    )
    try:
        await support.event_cache.initialize()
        for export_user, group_rooms in export_groups:
            client = await login_agent_user(homeserver, export_user, runtime_paths)
            try:
                stats = await _export_threads_for_client(
                    client=client,
                    config=config,
                    runtime_paths=runtime_paths,
                    event_cache=support.event_cache,
                    rooms=group_rooms,
                    output_dir=resolved_output_dir,
                    max_thread_roots=max_thread_roots,
                    prefer_cache=prefer_cache,
                )
            finally:
                await client.close()
            rooms_exported += stats.rooms_exported
            threads_seen += stats.threads_seen
            threads_exported += stats.threads_exported
            threads_unchanged += stats.threads_unchanged
            truncated_rooms += stats.truncated_rooms
            failures.extend(stats.failed_items)
    finally:
        await close_owned_runtime_support(support, logger=logger)

    return ThreadExportStats(
        output_dir=resolved_output_dir,
        rooms_exported=rooms_exported,
        threads_seen=threads_seen,
        threads_exported=threads_exported,
        threads_unchanged=threads_unchanged,
        truncated_rooms=truncated_rooms,
        failed_items=tuple(failures),
    )
