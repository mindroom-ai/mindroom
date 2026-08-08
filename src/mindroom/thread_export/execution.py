"""Per-client thread retrieval, authorization, and target fan-out."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.room_history_reads import enumerate_room_thread_root_ids
from mindroom.thread_export.models import (
    ThreadExportAccumulator,
    ThreadExportRoom,
    ThreadExportTarget,
    failure_for_room,
)
from mindroom.thread_export.policy import target_accepts_room
from mindroom.thread_export.projected_history import fetch_projected_thread_history
from mindroom.thread_export.selection import trusted_sender_ids_for_export
from mindroom.thread_export.storage import (
    remove_room_export,
    remove_stale_thread_exports,
    room_has_thread_exports,
    thread_payload,
    write_room_index,
    write_thread_payload,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.thread_export.projected_history import ProjectedThreadReader


logger = get_logger(__name__)


def retract_room_export(accumulator: ThreadExportAccumulator, room: ThreadExportRoom) -> None:
    """Remove one target's room export or record a room-scoped storage failure."""
    try:
        remove_room_export(accumulator.target.output_dir, room)
    except (OSError, RuntimeError) as exc:
        accumulator.failed_items.append(failure_for_room(room, f"Room removal failed: {exc}"))


async def _joined_member_ids(client: nio.AsyncClient, room_id: str) -> frozenset[str]:
    """Return the current joined Matrix user IDs for one room."""
    response = await client.joined_members(room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return frozenset(member.user_id for member in response.members)
    msg = f"Membership lookup failed: {response}"
    raise RuntimeError(msg)


async def _fetch_thread_payload(
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_id: str,
    *,
    trusted_sender_ids: frozenset[str],
) -> dict[str, object]:
    """Fetch and build one thread payload independently of export destinations."""
    messages = await fetch_projected_thread_history(
        reader,
        room_id=room.room_id,
        thread_id=thread_id,
    )
    return thread_payload(
        room=room,
        thread_id=thread_id,
        messages=messages,
        exported_at=datetime.now(UTC),
        trusted_sender_ids=trusted_sender_ids,
    )


async def _authorized_room_accumulators(
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    accumulators: Sequence[ThreadExportAccumulator],
) -> list[ThreadExportAccumulator]:
    """Return targets authorized for one room, removing exports only on definitive revocation."""
    eligible = [accumulator for accumulator in accumulators if target_accepts_room(accumulator.target, room)]
    for accumulator in accumulators:
        if not target_accepts_room(accumulator.target, room):
            retract_room_export(accumulator, room)

    scoped = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is not None]
    authorized = [accumulator for accumulator in eligible if accumulator.target.required_member_user_id is None]
    if not scoped:
        return authorized
    try:
        member_ids = await _joined_member_ids(client, room.room_id)
    except Exception as exc:
        # Authorization is unknown here, not revoked: removal is a retraction action and must be
        # driven by a definitive "not a member" answer, never by a lookup error. A boot-time 429
        # burst on joined_members once deleted every previously exported room before this guard.
        for accumulator in scoped:
            accumulator.retained_room_keys.add(room.key)
            accumulator.failed_items.append(failure_for_room(room, str(exc)))
        return authorized

    for accumulator in scoped:
        member_user_id = accumulator.target.required_member_user_id
        if member_user_id in member_ids:
            authorized.append(accumulator)
        else:
            retract_room_export(accumulator, room)
    return authorized


async def _write_thread_to_targets(
    *,
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_id: str,
    trusted_sender_ids: frozenset[str],
    accumulators: Sequence[ThreadExportAccumulator],
    changed_accumulator_ids: set[int],
) -> None:
    """Fetch one thread once and write it independently to each target."""
    try:
        payload = await _fetch_thread_payload(
            reader,
            room,
            thread_id,
            trusted_sender_ids=trusted_sender_ids,
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
            changed_accumulator_ids.add(id(accumulator))
        else:
            accumulator.threads_unchanged += 1


def _finish_room_exports(
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    *,
    truncated: bool,
    accumulators: Sequence[ThreadExportAccumulator],
    changed_accumulator_ids: set[int],
) -> None:
    """Reconcile removed threads and update indexes for one enumerated room."""
    for accumulator in accumulators:
        try:
            output_dir = accumulator.target.output_dir
            skip_empty_reconciliation = not truncated and not thread_ids and room_has_thread_exports(output_dir, room)
            if skip_empty_reconciliation:
                logger.warning(
                    "Skipping stale thread reconciliation after empty enumeration",
                    output_dir=str(output_dir),
                    room_key=room.key,
                    room_id=room.room_id,
                )
            elif not truncated:
                removed_stale_threads = remove_stale_thread_exports(
                    output_dir,
                    room,
                    thread_ids,
                )
                if removed_stale_threads:
                    changed_accumulator_ids.add(id(accumulator))
            write_room_index(
                output_dir,
                room,
                thread_files_changed=id(accumulator) in changed_accumulator_ids,
            )
        except Exception as exc:
            accumulator.failed_items.append(failure_for_room(room, f"Room reconciliation failed: {exc}"))


async def export_threads_for_targets_for_client(
    *,
    client: nio.AsyncClient,
    reader: ProjectedThreadReader,
    config: Config,
    runtime_paths: RuntimePaths,
    rooms: Sequence[ThreadExportRoom],
    targets: Sequence[ThreadExportTarget],
    max_thread_roots: int = 2000,
) -> tuple[ThreadExportAccumulator, ...]:
    """Fetch each Matrix thread once and fan it out to authorized destinations.

    ``client`` answers only what the projection cannot: which threads a room
    has, and who is currently joined to it. Thread bodies come from ``reader``.
    """
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
            reader=reader,
            room=room,
            thread_ids=thread_ids,
            truncated=truncated,
            trusted_sender_ids=trusted_sender_ids,
            authorized=authorized,
        )

    return accumulators


async def _export_enumerated_room_threads(
    *,
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    truncated: bool,
    trusted_sender_ids: frozenset[str],
    authorized: Sequence[ThreadExportAccumulator],
) -> None:
    """Export one enumerated room's threads to every authorized accumulator."""
    changed_accumulator_ids: set[int] = set()

    for thread_id in thread_ids:
        await _write_thread_to_targets(
            reader=reader,
            room=room,
            thread_id=thread_id,
            trusted_sender_ids=trusted_sender_ids,
            accumulators=authorized,
            changed_accumulator_ids=changed_accumulator_ids,
        )

    _finish_room_exports(
        room,
        thread_ids,
        truncated=truncated,
        accumulators=authorized,
        changed_accumulator_ids=changed_accumulator_ids,
    )
