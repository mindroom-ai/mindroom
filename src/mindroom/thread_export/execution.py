"""Per-client thread retrieval, authorization, and target fan-out."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.client_thread_history import (
    bulk_refresh_room_thread_histories,
    enumerate_room_thread_root_ids,
    fetch_thread_history,
    refresh_thread_history_from_source,
    untrusted_cached_thread_ids,
)
from mindroom.thread_export.models import (
    ThreadExportAccumulator,
    ThreadExportRoom,
    ThreadExportTarget,
    failure_for_room,
)
from mindroom.thread_export.policy import target_accepts_room
from mindroom.thread_export.selection import trusted_sender_ids_for_export
from mindroom.thread_export.storage import (
    remove_room_export,
    remove_stale_thread_exports,
    room_index_exists,
    thread_payload,
    write_room_index,
    write_thread_payload,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache


logger = get_logger(__name__)

# The bulk scan stops as soon as every requested root has been seen, so its cost is roughly one
# walk to the deepest requested root while the per-thread path pays one walk per thread. Two or
# more untrusted threads therefore already favor the bulk path; a single one is a wash.
_BULK_BACKFILL_MIN_THREADS = 2


async def _bulk_backfill_untrusted_threads(
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    *,
    event_cache: ConversationEventCache,
) -> frozenset[str]:
    """Warm the thread cache with one room scan when many threads would miss.

    Returns the thread roots the scan proved absent so callers can fail them without paying one
    full per-thread history walk each. Any bulk failure falls back to the per-thread path.
    """
    try:
        untrusted = await untrusted_cached_thread_ids(event_cache, room.room_id, thread_ids)
    except Exception as exc:
        logger.warning(
            "Untrusted-thread probe failed; using per-thread fetches",
            room_id=room.room_id,
            error=str(exc),
        )
        return frozenset()
    if len(untrusted) < _BULK_BACKFILL_MIN_THREADS:
        return frozenset()
    try:
        stats = await bulk_refresh_room_thread_histories(
            client,
            room.room_id,
            event_cache,
            thread_root_ids=untrusted,
            caller_label="thread_export_bulk",
        )
    except Exception as exc:
        logger.warning(
            "Bulk thread cache refresh failed; using per-thread fetches",
            room_id=room.room_id,
            error=str(exc),
        )
        return frozenset()
    return stats.missing_root_ids


async def _joined_member_ids(client: nio.AsyncClient, room_id: str) -> frozenset[str]:
    """Return the current joined Matrix user IDs for one room."""
    response = await client.joined_members(room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return frozenset(member.user_id for member in response.members)
    msg = f"Membership lookup failed: {response}"
    raise RuntimeError(msg)


async def _fetch_thread_payload(
    client: nio.AsyncClient,
    room: ThreadExportRoom,
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
    return thread_payload(
        room=room,
        thread_id=thread_id,
        messages=list(history),
        exported_at=datetime.now(UTC),
    )


async def _authorized_room_accumulators(
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    accumulators: Sequence[ThreadExportAccumulator],
) -> list[ThreadExportAccumulator]:
    """Return targets authorized for one room and remove fail-closed exports."""
    eligible = [accumulator for accumulator in accumulators if target_accepts_room(accumulator.target, room)]
    for accumulator in accumulators:
        if not target_accepts_room(accumulator.target, room):
            remove_room_export(accumulator.target.output_dir, room)

    scoped = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is not None]
    authorized = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is None]
    if not scoped:
        return authorized
    try:
        member_ids = await _joined_member_ids(client, room.room_id)
    except Exception as exc:
        for accumulator in scoped:
            remove_room_export(accumulator.target.output_dir, room)
            accumulator.failed_items.append(failure_for_room(room, str(exc)))
        return authorized

    for accumulator in scoped:
        member_user_id = accumulator.target.required_member_user_id
        if member_user_id in member_ids:
            authorized.append(accumulator)
        else:
            remove_room_export(accumulator.target.output_dir, room)
    return authorized


async def _write_thread_to_targets(
    *,
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    thread_id: str,
    event_cache: ConversationEventCache,
    trusted_sender_ids: frozenset[str],
    prefer_cache: bool,
    accumulators: Sequence[ThreadExportAccumulator],
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
            accumulator.failed_items.append(failure_for_room(room, str(exc), thread_id=thread_id))
        return

    for accumulator in accumulators:
        try:
            wrote_file = write_thread_payload(
                accumulator.target.output_dir,
                room,
                thread_id,
                payload,
            )
        except Exception as exc:
            accumulator.failed_items.append(failure_for_room(room, str(exc), thread_id=thread_id))
            continue
        accumulator.threads_exported += 1
        if wrote_file:
            room_changed[id(accumulator)] = True
        else:
            accumulator.threads_unchanged += 1


def _finish_room_exports(
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    *,
    truncated: bool,
    accumulators: Sequence[ThreadExportAccumulator],
    room_changed: dict[int, bool],
) -> None:
    """Reconcile removed threads and update indexes for one enumerated room."""
    for accumulator in accumulators:
        try:
            if not truncated and remove_stale_thread_exports(
                accumulator.target.output_dir,
                room,
                thread_ids,
            ):
                room_changed[id(accumulator)] = True
            if room_changed[id(accumulator)] or not room_index_exists(accumulator.target.output_dir, room):
                write_room_index(accumulator.target.output_dir, room)
        except Exception as exc:
            accumulator.failed_items.append(failure_for_room(room, f"Room reconciliation failed: {exc}"))


async def export_threads_for_targets_for_client(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    rooms: Sequence[ThreadExportRoom],
    targets: Sequence[ThreadExportTarget],
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
) -> tuple[ThreadExportAccumulator, ...]:
    """Fetch each Matrix thread once and fan it out to authorized destinations."""
    trusted_sender_ids = trusted_sender_ids_for_export(config, runtime_paths)
    accumulators = tuple(ThreadExportAccumulator(target=target) for target in targets)

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
                accumulator.failed_items.append(failure_for_room(room, str(exc)))
            continue

        for accumulator in authorized:
            accumulator.rooms_exported += 1
            accumulator.threads_seen += len(thread_ids)
            if truncated:
                accumulator.truncated_rooms += 1
        await _export_enumerated_room_threads(
            client=client,
            room=room,
            thread_ids=thread_ids,
            truncated=truncated,
            event_cache=event_cache,
            trusted_sender_ids=trusted_sender_ids,
            prefer_cache=prefer_cache,
            authorized=authorized,
        )

    return accumulators


async def _export_enumerated_room_threads(
    *,
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    truncated: bool,
    event_cache: ConversationEventCache,
    trusted_sender_ids: frozenset[str],
    prefer_cache: bool,
    authorized: Sequence[ThreadExportAccumulator],
) -> None:
    """Export one enumerated room's threads to every authorized accumulator."""
    room_changed = {id(accumulator): False for accumulator in authorized}
    missing_root_ids: frozenset[str] = frozenset()
    if prefer_cache and thread_ids:
        missing_root_ids = await _bulk_backfill_untrusted_threads(
            client,
            room,
            thread_ids,
            event_cache=event_cache,
        )

    for thread_id in thread_ids:
        if thread_id in missing_root_ids:
            for accumulator in authorized:
                accumulator.failed_items.append(
                    failure_for_room(
                        room,
                        "thread root not found during bulk room scan",
                        thread_id=thread_id,
                    ),
                )
            continue
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
