"""Matrix thread export helpers."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from urllib.parse import quote

import nio
import yaml

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.durable_write import fsync_directory, write_json_file_durable
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

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.state import MatrixAccount


logger = get_logger(__name__)
_EXPORT_SCHEMA_VERSION = 1
_ROOM_INDEX_FILENAME = "index.json"
_THREAD_SUMMARY_CONTENT_KEY = "io.mindroom.thread_summary"


@dataclass(frozen=True)
class _ThreadExportRoom:
    """One Matrix room selected for thread export."""

    key: str
    room_id: str
    alias: str
    name: str
    invited: bool = False


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


@dataclass(frozen=True)
class ThreadExportTarget:
    """One export destination and its optional room-membership scope."""

    output_dir: Path
    required_member_user_id: str | None = None
    include_invited_rooms: bool = True


@dataclass
class _ThreadExportAccumulator:
    """Mutable statistics and reconciliation state for one export target."""

    target: ThreadExportTarget
    rooms_exported: int = 0
    threads_seen: int = 0
    threads_exported: int = 0
    threads_unchanged: int = 0
    truncated_rooms: int = 0
    failed_items: list[_ThreadExportFailure] = field(default_factory=list)
    retained_room_keys: set[str] = field(default_factory=set)

    def stats(self) -> ThreadExportStats:
        """Return the immutable public statistics for this target."""
        return ThreadExportStats(
            output_dir=self.target.output_dir,
            rooms_exported=self.rooms_exported,
            threads_seen=self.threads_seen,
            threads_exported=self.threads_exported,
            threads_unchanged=self.threads_unchanged,
            truncated_rooms=self.truncated_rooms,
            failed_items=tuple(self.failed_items),
        )


@dataclass(frozen=True)
class _ThreadExportGroup:
    """Rooms that must be read with one persisted Matrix account."""

    rooms: tuple[_ThreadExportRoom, ...]
    user: AgentMatrixUser | None = None
    error: str | None = None


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


def _latest_thread_summary(messages: list[ResolvedVisibleMessage]) -> str | None:
    """Return the latest thread-summary notice text, when one exists."""
    for message in reversed(messages):
        meta = message.content.get(_THREAD_SUMMARY_CONTENT_KEY)
        if isinstance(meta, dict):
            summary = meta.get("summary")
            return summary if isinstance(summary, str) and summary else message.body
    return None


def _thread_payload(
    *,
    room: _ThreadExportRoom,
    thread_id: str,
    messages: list[ResolvedVisibleMessage],
    exported_at: datetime,
) -> dict[str, object]:
    """Build one YAML document for a Matrix thread."""
    thread_block: dict[str, object] = {
        "id": thread_id,
        "source": "matrix",
    }
    if summary := _latest_thread_summary(messages):
        thread_block["summary"] = summary
    thread_block["exported_at"] = exported_at.isoformat()
    thread_block["message_count"] = len(messages)
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "room": {
            "key": room.key,
            "id": room.room_id,
            "name": room.name,
            "alias": room.alias,
        },
        "thread": thread_block,
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


def _thread_index_entry(thread_file: Path) -> tuple[int, dict[str, object]] | None:
    """Return one (last-activity, entry) index pair from an exported thread file."""
    try:
        payload = yaml.safe_load(thread_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread")
    messages = payload.get("messages")
    if not isinstance(thread, dict) or not isinstance(messages, list):
        return None
    message_dicts = [message for message in messages if isinstance(message, dict)]
    entry: dict[str, object] = {
        "file": thread_file.name,
        "thread_id": thread.get("id"),
        "message_count": thread.get("message_count"),
        "participants": sorted(
            {sender for message in message_dicts if isinstance(sender := message.get("sender"), str)},
        ),
    }
    summary = thread.get("summary")
    if isinstance(summary, str):
        entry["summary"] = summary
    last_timestamp = 0
    if message_dicts:
        last_message = message_dicts[-1]
        if isinstance(raw_timestamp := last_message.get("timestamp"), int):
            last_timestamp = raw_timestamp
            entry["last_timestamp"] = raw_timestamp
        if isinstance(timestamp_iso := last_message.get("timestamp_iso"), str):
            entry["last_timestamp_iso"] = timestamp_iso
    return last_timestamp, entry


def _room_index_payload(room_dir: Path, room: _ThreadExportRoom) -> dict[str, object]:
    """Build one room index document from the exported thread files on disk."""
    indexed = [
        indexed_entry
        for thread_file in sorted(room_dir.glob("*.yaml"))
        if (indexed_entry := _thread_index_entry(thread_file)) is not None
    ]
    indexed.sort(key=lambda item: item[0], reverse=True)
    entries = [entry for _, entry in indexed]
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "room": {
            "key": room.key,
            "id": room.room_id,
            "name": room.name,
            "alias": room.alias,
        },
        "thread_count": len(entries),
        "threads": entries,
    }


def _write_room_index(output_dir: Path, room: _ThreadExportRoom) -> None:
    """Write one room's index.json when its content changed."""
    room_dir = output_dir / _safe_path_segment(room.key)
    if not room_dir.is_dir():
        return
    index_path = room_dir / _ROOM_INDEX_FILENAME
    payload = _room_index_payload(room_dir, room)
    text = f"{json.dumps(payload, indent=2)}\n"
    if index_path.exists() and index_path.read_text(encoding="utf-8") == text:
        return
    write_json_file_durable(index_path, payload, indent=2, trailing_newline=True)


def _room_export_dir(output_dir: Path, room: _ThreadExportRoom) -> Path:
    """Return one room's export directory."""
    return output_dir / _safe_path_segment(room.key)


def _remove_room_export(output_dir: Path, room: _ThreadExportRoom) -> bool:
    """Remove one room's exported data and return whether anything changed."""
    room_dir = _room_export_dir(output_dir, room)
    if not room_dir.exists() and not room_dir.is_symlink():
        return False
    if room_dir.is_symlink() or not room_dir.is_dir():
        room_dir.unlink()
    else:
        shutil.rmtree(room_dir)
    fsync_directory(output_dir)
    return True


def _remove_stale_thread_exports(
    output_dir: Path,
    room: _ThreadExportRoom,
    thread_ids: Sequence[str],
) -> bool:
    """Remove thread files absent from a complete homeserver enumeration."""
    room_dir = _room_export_dir(output_dir, room)
    if not room_dir.is_dir():
        return False
    expected_names = {f"{_safe_path_segment(thread_id)}.yaml" for thread_id in thread_ids}
    stale_files = [thread_file for thread_file in room_dir.glob("*.yaml") if thread_file.name not in expected_names]
    for thread_file in stale_files:
        thread_file.unlink()
    if stale_files:
        fsync_directory(room_dir)
    return bool(stale_files)


def _reconcile_room_directories(output_dir: Path, retained_room_keys: set[str]) -> None:
    """Remove room directories outside the target's full-pass authorization scope."""
    if not output_dir.is_dir():
        return
    retained_names = {_safe_path_segment(room_key) for room_key in retained_room_keys}
    removed = False
    for candidate in output_dir.iterdir():
        if candidate.name in retained_names:
            continue
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            continue
        removed = True
    if removed:
        fsync_directory(output_dir)


def _payload_without_exported_at(payload: dict[str, object]) -> dict[str, object]:
    """Return one thread payload with the per-pass exported_at timestamp removed."""
    normalized = dict(payload)
    thread = normalized.get("thread")
    if isinstance(thread, dict):
        normalized["thread"] = {key: value for key, value in thread.items() if key != "exported_at"}
    return normalized


def _existing_payload_matches(path: Path, payload: dict[str, object]) -> bool:
    """Return whether one existing export file already holds this payload, ignoring exported_at."""
    if not path.is_file():
        return False
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
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
            entity_rooms.append(
                _ThreadExportRoom(
                    key=room_id,
                    room_id=room_id,
                    alias="",
                    name="",
                    invited=True,
                ),
            )
        if entity_rooms:
            grouped.append((entity_name, entity_rooms))
    return grouped


def _trusted_sender_ids_for_export(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return trusted senders when Matrix accounts have already been prepared."""
    try:
        return trusted_visible_sender_ids(config, runtime_paths)
    except MissingManagedEntityAccountError:
        return frozenset()


async def _joined_member_ids(client: nio.AsyncClient, room_id: str) -> frozenset[str]:
    """Return the current joined Matrix user IDs for one room."""
    response = await client.joined_members(room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return frozenset(member.user_id for member in response.members)
    msg = f"Membership lookup failed: {response}"
    raise RuntimeError(msg)


async def _fetch_thread_payload(
    client: nio.AsyncClient,
    room: _ThreadExportRoom,
    thread_id: str,
    *,
    event_cache: ConversationEventCache,
    trusted_sender_ids: frozenset[str],
    prefer_cache: bool,
) -> dict[str, object]:
    """Fetch and build one thread payload independently of export destinations."""
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
    return _thread_payload(
        room=room,
        thread_id=thread_id,
        messages=list(history),
        exported_at=datetime.now(UTC),
    )


def _write_thread_payload(
    output_dir: Path,
    room: _ThreadExportRoom,
    thread_id: str,
    payload: dict[str, object],
) -> bool:
    """Write one thread payload when changed and return whether bytes were replaced."""
    export_path = _thread_export_path(output_dir, room, thread_id)
    if _existing_payload_matches(export_path, payload):
        return False
    _write_yaml_atomic(export_path, payload)
    return True


def _target_accepts_room(target: ThreadExportTarget, room: _ThreadExportRoom) -> bool:
    """Return whether one target includes the room's source category."""
    return target.include_invited_rooms or not room.invited


def _room_failure(room: _ThreadExportRoom, error: str, *, thread_id: str | None = None) -> _ThreadExportFailure:
    """Build one target-local room or thread failure."""
    return _ThreadExportFailure(
        room_key=room.key,
        room_id=room.room_id,
        thread_id=thread_id,
        error=error,
    )


async def _authorized_room_accumulators(
    client: nio.AsyncClient,
    room: _ThreadExportRoom,
    accumulators: Sequence[_ThreadExportAccumulator],
) -> list[_ThreadExportAccumulator]:
    """Return targets authorized for one room and remove fail-closed exports."""
    eligible = [accumulator for accumulator in accumulators if _target_accepts_room(accumulator.target, room)]
    for accumulator in accumulators:
        if not _target_accepts_room(accumulator.target, room):
            _remove_room_export(accumulator.target.output_dir, room)

    scoped = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is not None]
    authorized = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is None]
    if not scoped:
        return authorized
    try:
        member_ids = await _joined_member_ids(client, room.room_id)
    except Exception as exc:
        for accumulator in scoped:
            _remove_room_export(accumulator.target.output_dir, room)
            accumulator.failed_items.append(_room_failure(room, str(exc)))
        return authorized

    for accumulator in scoped:
        member_user_id = accumulator.target.required_member_user_id
        if member_user_id in member_ids:
            authorized.append(accumulator)
        else:
            _remove_room_export(accumulator.target.output_dir, room)
    return authorized


async def _write_thread_to_targets(
    *,
    client: nio.AsyncClient,
    room: _ThreadExportRoom,
    thread_id: str,
    event_cache: ConversationEventCache,
    trusted_sender_ids: frozenset[str],
    prefer_cache: bool,
    accumulators: Sequence[_ThreadExportAccumulator],
    room_changed: dict[int, bool],
) -> None:
    """Fetch one thread once and write it independently to each target."""
    try:
        payload = await _fetch_thread_payload(
            client,
            room,
            thread_id,
            event_cache=event_cache,
            trusted_sender_ids=trusted_sender_ids,
            prefer_cache=prefer_cache,
        )
    except Exception as exc:
        for accumulator in accumulators:
            accumulator.failed_items.append(_room_failure(room, str(exc), thread_id=thread_id))
        return

    for accumulator in accumulators:
        try:
            wrote_file = _write_thread_payload(
                accumulator.target.output_dir,
                room,
                thread_id,
                payload,
            )
        except Exception as exc:
            accumulator.failed_items.append(_room_failure(room, str(exc), thread_id=thread_id))
            continue
        accumulator.threads_exported += 1
        if wrote_file:
            room_changed[id(accumulator)] = True
        else:
            accumulator.threads_unchanged += 1


def _finish_room_exports(
    room: _ThreadExportRoom,
    thread_ids: Sequence[str],
    *,
    truncated: bool,
    accumulators: Sequence[_ThreadExportAccumulator],
    room_changed: dict[int, bool],
) -> None:
    """Reconcile removed threads and update indexes for one enumerated room."""
    for accumulator in accumulators:
        try:
            if not truncated and _remove_stale_thread_exports(
                accumulator.target.output_dir,
                room,
                thread_ids,
            ):
                room_changed[id(accumulator)] = True
            room_dir = _room_export_dir(accumulator.target.output_dir, room)
            index_path = room_dir / _ROOM_INDEX_FILENAME
            if room_changed[id(accumulator)] or not index_path.is_file():
                _write_room_index(accumulator.target.output_dir, room)
        except Exception as exc:
            accumulator.failed_items.append(_room_failure(room, f"Room reconciliation failed: {exc}"))


async def _export_threads_for_targets_for_client(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    rooms: Sequence[_ThreadExportRoom],
    targets: Sequence[ThreadExportTarget],
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
) -> tuple[_ThreadExportAccumulator, ...]:
    """Fetch each Matrix thread once and fan it out to authorized destinations."""
    trusted_sender_ids = _trusted_sender_ids_for_export(config, runtime_paths)
    accumulators = tuple(_ThreadExportAccumulator(target=target) for target in targets)

    for room in rooms:
        authorized = await _authorized_room_accumulators(client, room, accumulators)
        if not authorized:
            continue
        for accumulator in authorized:
            accumulator.retained_room_keys.add(room.key)

        try:
            thread_ids, truncated = await enumerate_room_thread_root_ids(
                client,
                room.room_id,
                max_thread_roots=max_thread_roots,
            )
        except Exception as exc:
            for accumulator in authorized:
                accumulator.failed_items.append(_room_failure(room, str(exc)))
            continue

        for accumulator in authorized:
            accumulator.rooms_exported += 1
            accumulator.threads_seen += len(thread_ids)
            if truncated:
                accumulator.truncated_rooms += 1
        room_changed = {id(accumulator): False for accumulator in authorized}

        for thread_id in thread_ids:
            await _write_thread_to_targets(
                client=client,
                room=room,
                thread_id=thread_id,
                event_cache=event_cache,
                trusted_sender_ids=trusted_sender_ids,
                prefer_cache=prefer_cache,
                accumulators=authorized,
                room_changed=room_changed,
            )

        _finish_room_exports(
            room,
            thread_ids,
            truncated=truncated,
            accumulators=authorized,
            room_changed=room_changed,
        )

    return accumulators


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
    required_member_user_id: str | None = None,
) -> ThreadExportStats:
    """Export Matrix-source thread histories to one YAML destination."""
    target = ThreadExportTarget(
        output_dir=output_dir or _default_thread_export_dir(runtime_paths),
        required_member_user_id=required_member_user_id,
    )
    accumulators = await _export_threads_for_targets_for_client(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=event_cache,
        rooms=rooms,
        targets=(target,),
        max_thread_roots=max_thread_roots,
        prefer_cache=prefer_cache,
    )
    return accumulators[0].stats()


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


def _merge_accumulator(target: _ThreadExportAccumulator, update: _ThreadExportAccumulator) -> None:
    """Merge one account group's target-local result into the pass total."""
    target.rooms_exported += update.rooms_exported
    target.threads_seen += update.threads_seen
    target.threads_exported += update.threads_exported
    target.threads_unchanged += update.threads_unchanged
    target.truncated_rooms += update.truncated_rooms
    target.failed_items.extend(update.failed_items)
    target.retained_room_keys.update(update.retained_room_keys)


def _record_group_failure(
    accumulators: Sequence[_ThreadExportAccumulator],
    rooms: Sequence[_ThreadExportRoom],
    error: str,
) -> None:
    """Record one account-level failure without leaking scoped room exports."""
    for room in rooms:
        for accumulator in accumulators:
            target = accumulator.target
            if not _target_accepts_room(target, room):
                _remove_room_export(target.output_dir, room)
                continue
            if target.required_member_user_id is None:
                accumulator.retained_room_keys.add(room.key)
            else:
                _remove_room_export(target.output_dir, room)
            accumulator.failed_items.append(_room_failure(room, error))


def _reconcile_full_pass(accumulators: Sequence[_ThreadExportAccumulator]) -> None:
    """Remove room directories that the completed full pass did not retain."""
    for accumulator in accumulators:
        _reconcile_room_directories(
            accumulator.target.output_dir,
            accumulator.retained_room_keys,
        )


def _build_export_groups(
    *,
    runtime_paths: RuntimePaths,
    homeserver: str,
    state_rooms: Sequence[_ThreadExportRoom],
    invited_groups: Sequence[tuple[str, list[_ThreadExportRoom]]],
) -> list[_ThreadExportGroup]:
    """Build account-specific export groups, retaining missing-account failures."""
    groups: list[_ThreadExportGroup] = []
    if state_rooms:
        groups.append(
            _ThreadExportGroup(
                user=_select_export_account(runtime_paths, homeserver),
                rooms=tuple(state_rooms),
            ),
        )
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    for entity_name, entity_rooms in invited_groups:
        account_key = managed_account_key(entity_name)
        account = accounts.get(account_key)
        if account is None:
            groups.append(
                _ThreadExportGroup(
                    rooms=tuple(entity_rooms),
                    error=f"No persisted Matrix account for invited-room entity '{entity_name}'",
                ),
            )
            continue
        groups.append(
            _ThreadExportGroup(
                user=_account_user_from_state(
                    account_key=account_key,
                    account=account,
                    homeserver=homeserver,
                    runtime_paths=runtime_paths,
                ),
                rooms=tuple(entity_rooms),
            ),
        )
    return groups


async def _run_export_group(
    group: _ThreadExportGroup,
    *,
    homeserver: str,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None,
    targets: Sequence[ThreadExportTarget],
    accumulators: Sequence[_ThreadExportAccumulator],
    max_thread_roots: int,
    prefer_cache: bool,
) -> None:
    """Run one account group without preventing later groups after a failure."""
    if group.error is not None:
        _record_group_failure(accumulators, group.rooms, group.error)
        return
    if group.user is None or event_cache is None:
        msg = "Export group is missing its Matrix user or event cache"
        raise RuntimeError(msg)
    try:
        client = await login_agent_user(homeserver, group.user, runtime_paths)
    except Exception as exc:
        _record_group_failure(accumulators, group.rooms, f"Matrix login failed: {exc}")
        return
    try:
        group_accumulators = await _export_threads_for_targets_for_client(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            event_cache=event_cache,
            rooms=group.rooms,
            targets=targets,
            max_thread_roots=max_thread_roots,
            prefer_cache=prefer_cache,
        )
    except Exception as exc:
        _record_group_failure(accumulators, group.rooms, f"Export group failed: {exc}")
        return
    finally:
        await client.close()
    for accumulator, group_accumulator in zip(accumulators, group_accumulators, strict=True):
        _merge_accumulator(accumulator, group_accumulator)


async def export_threads_to_targets_once(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    targets: Sequence[ThreadExportTarget],
    room_filter: str | None = None,
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
) -> tuple[ThreadExportStats, ...]:
    """Login with persisted Matrix accounts and export once to every target.

    Rooms come from ``matrix_state.yaml`` plus every entity's persisted invited rooms when at least
    one target includes invited rooms.
    Invited rooms are exported with the invited entity's own account, because the primary export
    account is not necessarily a member of user-created rooms.

    With ``prefer_cache`` thread bodies are served from the validated durable event cache and only
    fetched from the homeserver on miss or invalidation; a failing miss-refetch may then fall back to
    stale cached rows instead of failing the thread.
    Only use it while the runtime keeps the cache fresh (in-process or alongside a live ``mindroom run``).

    Each source thread is fetched once per room and fanned out to every authorized target.
    Scoped targets retain only rooms where their required member is currently joined; failed
    membership checks remove any prior room export before recording a failure.
    """
    resolved_targets = tuple(targets)
    if not resolved_targets:
        return ()
    homeserver = runtime_matrix_homeserver(runtime_paths=runtime_paths)
    state_rooms = _export_rooms(runtime_paths, room_filter)
    invited_groups = (
        _invited_export_rooms(
            config,
            runtime_paths,
            room_filter,
            known_room_ids={room.room_id for room in state_rooms},
        )
        if any(target.include_invited_rooms for target in resolved_targets)
        else []
    )
    export_groups = _build_export_groups(
        runtime_paths=runtime_paths,
        homeserver=homeserver,
        state_rooms=state_rooms,
        invited_groups=invited_groups,
    )

    accumulators = tuple(_ThreadExportAccumulator(target=target) for target in resolved_targets)
    if not export_groups:
        _select_export_account(runtime_paths, homeserver)
        if room_filter is None:
            _reconcile_full_pass(accumulators)
        return tuple(accumulator.stats() for accumulator in accumulators)

    login_groups = [group for group in export_groups if group.user is not None]
    support = (
        build_owned_runtime_support(
            cache_config=config.cache,
            runtime_paths=runtime_paths,
            logger=logger,
            background_task_owner=object(),
        )
        if login_groups
        else None
    )
    try:
        if support is not None:
            await support.event_cache.initialize()
        for group in export_groups:
            await _run_export_group(
                group,
                homeserver=homeserver,
                config=config,
                runtime_paths=runtime_paths,
                event_cache=support.event_cache if support is not None else None,
                targets=resolved_targets,
                accumulators=accumulators,
                max_thread_roots=max_thread_roots,
                prefer_cache=prefer_cache,
            )
    finally:
        if support is not None:
            await close_owned_runtime_support(support, logger=logger)

    if room_filter is None:
        _reconcile_full_pass(accumulators)
    return tuple(accumulator.stats() for accumulator in accumulators)


async def export_threads_once(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    output_dir: Path | None = None,
    room_filter: str | None = None,
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
    required_member_user_id: str | None = None,
    include_invited_rooms: bool = True,
) -> ThreadExportStats:
    """Run one thread export pass for a single destination."""
    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=(
            ThreadExportTarget(
                output_dir=output_dir or _default_thread_export_dir(runtime_paths),
                required_member_user_id=required_member_user_id,
                include_invited_rooms=include_invited_rooms,
            ),
        ),
        room_filter=room_filter,
        max_thread_roots=max_thread_roots,
        prefer_cache=prefer_cache,
    )
    return stats[0]
