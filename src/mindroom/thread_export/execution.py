"""Per-client thread retrieval, authorization, and target fan-out."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.joined_members import read_joined_members
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
    thread_file_identities,
    thread_payload,
    write_room_index,
    write_thread_payload,
)
from mindroom.thread_summary.state import human_summary_authorizer, summary_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.thread_export.projected_history import ProjectedThreadReader
    from mindroom.thread_export.storage import FileIdentity, WrittenIndexEntry

# A thread's summary, and whether it follows from the thread's messages and trusted senders alone.
type _CurrentSummary = Callable[[str, str, Sequence[ResolvedVisibleMessage]], tuple[str | None, bool]]


@dataclass(frozen=True, slots=True)
class _ExportedThread:
    """What one pass wrote for a thread, so a later pass can skip it while nothing it was built from changed."""

    # The thread's hydration marker and the rows the export read, its room, and the trusted title senders, hashed.
    fingerprint: int
    entries: dict[Path, WrittenIndexEntry]


# One source's exported threads by room and thread ID.
type ExportedThreads = dict[tuple[str, str], _ExportedThread]
# The rows a strict read returned, as `thread_fingerprint` gives them: the read leaves no refresh owed.
type _ReadRows = frozenset[tuple[str, str, None]]


logger = get_logger(__name__)


def retract_room_export(accumulator: ThreadExportAccumulator, room: ThreadExportRoom) -> None:
    """Remove one target's room export or record a room-scoped storage failure."""
    try:
        remove_room_export(
            accumulator.target.output_dir,
            room,
            trusted_root=accumulator.target.trusted_root,
        )
    except (OSError, RuntimeError) as exc:
        accumulator.failed_items.append(failure_for_room(room, f"Room removal failed: {exc}"))


async def _joined_member_ids(client: nio.AsyncClient, room_id: str) -> frozenset[str]:
    """Return the current joined Matrix user IDs for one room."""
    response = await read_joined_members(client, room_id)
    if isinstance(response, nio.JoinedMembersResponse):
        return frozenset(member.user_id for member in response.members)
    msg = f"Membership lookup failed: {response}"
    raise RuntimeError(msg)


async def _fetch_thread_payload(
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_id: str,
    *,
    current_summary: _CurrentSummary,
) -> tuple[dict[str, object], _ReadRows | None]:
    """Fetch and build one thread payload, and the rows it was read from unless its summary needs more than the thread."""
    messages = await fetch_projected_thread_history(
        reader,
        room_id=room.room_id,
        thread_id=thread_id,
    )
    summary, summary_lasts = current_summary(room.room_id, thread_id, messages)
    payload = thread_payload(
        room=room,
        thread_id=thread_id,
        messages=messages,
        exported_at=datetime.now(UTC),
        summary=summary,
    )
    return payload, frozenset((m.event_id, m.latest_event_id, None) for m in messages) if summary_lasts else None


async def _authorized_room_accumulators(
    client: nio.AsyncClient,
    room: ThreadExportRoom,
    accumulators: Sequence[ThreadExportAccumulator],
) -> list[ThreadExportAccumulator]:
    """Return targets authorized for one room, removing exports only on definitive revocation."""
    eligible = [accumulator for accumulator in accumulators if target_accepts_room(accumulator.target, room)]
    for accumulator in accumulators:
        if not target_accepts_room(accumulator.target, room):
            await asyncio.to_thread(retract_room_export, accumulator, room)

    scoped = [accumulator for accumulator in eligible if accumulator.target.required_member_user_ids]
    authorized = [accumulator for accumulator in eligible if not accumulator.target.required_member_user_ids]
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
        required_member_user_ids = accumulator.target.required_member_user_ids
        if member_ids.issuperset(required_member_user_ids):
            authorized.append(accumulator)
        else:
            await asyncio.to_thread(retract_room_export, accumulator, room)
    return authorized


async def _write_thread_to_targets(
    *,
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_id: str,
    current_summary: _CurrentSummary,
    accumulators: Sequence[ThreadExportAccumulator],
    changed_accumulator_ids: set[int],
    index_entries: dict[int, list[WrittenIndexEntry]],
) -> tuple[_ReadRows, dict[Path, WrittenIndexEntry]] | None:
    """Fetch one thread once and write it to each target; return the rows read and the index entries, if remembered."""
    try:
        payload, rows = await _fetch_thread_payload(
            reader,
            room,
            thread_id,
            current_summary=current_summary,
        )
    except Exception as exc:
        for accumulator in accumulators:
            accumulator.failed_items.append(failure_for_room(room, str(exc), thread_id=thread_id))
        return None

    entries: dict[Path, WrittenIndexEntry] = {}
    for accumulator in accumulators:
        try:
            written = await asyncio.to_thread(
                write_thread_payload,
                accumulator.target.output_dir,
                room,
                thread_id,
                payload,
                trusted_root=accumulator.target.trusted_root,
            )
        except Exception as exc:
            accumulator.failed_items.append(failure_for_room(room, str(exc), thread_id=thread_id))
            continue
        accumulator.threads_exported += 1
        if written.index_entry is not None:
            index_entries.setdefault(id(accumulator), []).append(written.index_entry)
            entries[accumulator.target.output_dir] = written.index_entry
        if written.replaced:
            changed_accumulator_ids.add(id(accumulator))
        else:
            accumulator.threads_unchanged += 1
    return None if rows is None else (rows, entries)


async def _export_unless_unchanged(
    write: Callable[[], Awaitable[tuple[_ReadRows, dict[Path, WrittenIndexEntry]] | None]],
    *,
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_id: str,
    accumulators: Sequence[ThreadExportAccumulator],
    index_entries: dict[int, list[WrittenIndexEntry]],
    exported: ExportedThreads,
    file_identities: dict[int, dict[str, FileIdentity]],
    trusted_sender_ids: frozenset[str],
) -> None:
    """Count a thread unchanged when nothing it was built from changed and every target still holds its file, else write it."""
    key = (room.room_id, thread_id)
    try:
        thread_fingerprint = await reader.completeness.thread_fingerprint(room_id=room.room_id, thread_id=thread_id)
    except Exception as exc:
        for accumulator in accumulators:
            accumulator.failed_items.append(failure_for_room(room, str(exc), thread_id=thread_id))
        return
    if thread_fingerprint is None:
        await write()
        return
    marker, rows = thread_fingerprint

    def fingerprint(of_rows: frozenset[tuple[str, str, int | None]]) -> int:
        return hash((marker, of_rows, room, trusted_sender_ids))

    previous = exported.get(key)
    if (
        previous is not None
        and previous.fingerprint == fingerprint(rows)
        and all(
            (entry := previous.entries.get(accumulator.target.output_dir)) is not None
            and file_identities.get(id(accumulator), {}).get(entry.filename) == entry.file_identity
            for accumulator in accumulators
        )
    ):
        for accumulator in accumulators:
            accumulator.threads_exported += 1
            accumulator.threads_unchanged += 1
            index_entries.setdefault(id(accumulator), []).append(previous.entries[accumulator.target.output_dir])
        return
    # The rows come from what the export read, so one added during the read and deleted since cannot match.
    if (written := await write()) is not None:
        read_rows, entries = written
        exported[key] = _ExportedThread(fingerprint(read_rows), entries)


def _finish_room_exports_blocking(
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    *,
    truncated: bool,
    accumulators: Sequence[ThreadExportAccumulator],
    changed_accumulator_ids: set[int],
    index_entries: dict[int, list[WrittenIndexEntry]],
) -> None:
    """Reconcile removed threads and update indexes for one enumerated room.

    Parses the room's thread files this pass did not export or that changed since, so it runs off the event loop.
    """
    for accumulator in accumulators:
        try:
            output_dir = accumulator.target.output_dir
            skip_empty_reconciliation = (
                not truncated
                and not thread_ids
                and room_has_thread_exports(
                    output_dir,
                    room,
                    trusted_root=accumulator.target.trusted_root,
                )
            )
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
                    trusted_root=accumulator.target.trusted_root,
                )
                if removed_stale_threads:
                    changed_accumulator_ids.add(id(accumulator))
            write_room_index(
                output_dir,
                room,
                thread_files_changed=id(accumulator) in changed_accumulator_ids,
                written_entries=index_entries.get(id(accumulator), ()),
                trusted_root=accumulator.target.trusted_root,
            )
        except Exception as exc:
            accumulator.failed_items.append(failure_for_room(room, f"Room reconciliation failed: {exc}"))


async def export_threads_for_targets_for_client(
    *,
    client: nio.AsyncClient,
    reader: ProjectedThreadReader,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    rooms: Sequence[ThreadExportRoom],
    targets: Sequence[ThreadExportTarget],
    max_thread_roots: int = 2000,
    exported: ExportedThreads | None = None,
) -> tuple[ThreadExportAccumulator, ...]:
    """Fetch each Matrix thread once and fan it out to authorized destinations.

    ``client`` answers only what the projection cannot: which threads a room
    has, and who is currently joined to it. Thread bodies come from ``reader``.
    Each thread's summary is the one ``entity_name`` would list for it, under
    the same trust and pin rules as thread listings.
    """
    trusted_sender_ids = trusted_sender_ids_for_export(config, runtime_paths)

    def current_summary(
        room_id: str,
        thread_id: str,
        messages: Sequence[ResolvedVisibleMessage],
    ) -> tuple[str | None, bool]:
        # A title a person set stands only while room membership authorizes them, which the thread alone does not show.
        authorize = human_summary_authorizer(client, room_id, config, runtime_paths, entity_name, membership_index)
        asked: list[str] = []

        def authorize_human(sender: str) -> bool:
            asked.append(sender)
            return authorize(sender)

        state = summary_state(
            messages,
            thread_id=thread_id,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
        )
        return (None if state is None else state.summary), not asked

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
            current_summary=current_summary,
            authorized=authorized,
            exported=exported,
            trusted_sender_ids=trusted_sender_ids,
        )

    return accumulators


async def _export_enumerated_room_threads(
    *,
    reader: ProjectedThreadReader,
    room: ThreadExportRoom,
    thread_ids: Sequence[str],
    truncated: bool,
    current_summary: _CurrentSummary,
    authorized: Sequence[ThreadExportAccumulator],
    exported: ExportedThreads | None,
    trusted_sender_ids: frozenset[str],
) -> None:
    """Export one enumerated room's threads to every authorized accumulator."""
    changed_accumulator_ids: set[int] = set()
    index_entries: dict[int, list[WrittenIndexEntry]] = {}
    # A thread is skipped only while each target still holds the file the pass that recorded it wrote.
    file_identities: dict[int, dict[str, FileIdentity]] = {}
    if exported is not None:
        for accumulator in authorized:
            with suppress(OSError, RuntimeError):
                file_identities[id(accumulator)] = await asyncio.to_thread(
                    thread_file_identities,
                    accumulator.target.output_dir,
                    room,
                    trusted_root=accumulator.target.trusted_root,
                )

    for thread_id in thread_ids:
        write = partial(
            _write_thread_to_targets,
            reader=reader,
            room=room,
            thread_id=thread_id,
            current_summary=current_summary,
            accumulators=authorized,
            changed_accumulator_ids=changed_accumulator_ids,
            index_entries=index_entries,
        )
        if exported is None:
            await write()
            continue
        await _export_unless_unchanged(
            write,
            reader=reader,
            room=room,
            thread_id=thread_id,
            accumulators=authorized,
            index_entries=index_entries,
            exported=exported,
            file_identities=file_identities,
            trusted_sender_ids=trusted_sender_ids,
        )

    await asyncio.to_thread(
        _finish_room_exports_blocking,
        room,
        thread_ids,
        truncated=truncated,
        accumulators=authorized,
        changed_accumulator_ids=changed_accumulator_ids,
        index_entries=index_entries,
    )
