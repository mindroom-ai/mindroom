"""Public thread-export orchestration across Matrix account groups."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.constants import runtime_matrix_homeserver
from mindroom.event_journal_open import bind_event_journal, open_event_journal
from mindroom.logging_config import get_logger
from mindroom.matrix.users import login_agent_user
from mindroom.thread_export.execution import export_threads_for_targets_for_client, retract_room_export
from mindroom.thread_export.models import (
    ThreadExportAccumulator,
    ThreadExportGroup,
    ThreadExportGroupFailure,
    ThreadExportRoom,
    ThreadExportStats,
    ThreadExportTarget,
    failure_for_room,
    failure_for_target,
)
from mindroom.thread_export.policy import target_accepts_room
from mindroom.thread_export.projected_history import export_conversation_reader
from mindroom.thread_export.selection import (
    build_export_groups,
    export_rooms,
    invited_export_rooms,
    select_export_account,
)
from mindroom.thread_export.storage import (
    canonicalize_output_dir,
    prepare_export_root,
    reconcile_room_directories,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore
    from mindroom.matrix.users import AgentMatrixUser


logger = get_logger(__name__)


def _default_thread_export_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the default thread export output directory."""
    return runtime_paths.storage_root / "thread_exports"


def _merge_accumulator(target: ThreadExportAccumulator, update: ThreadExportAccumulator) -> None:
    """Merge one account group's target-local result into the pass total."""
    target.rooms_exported += update.rooms_exported
    target.threads_seen += update.threads_seen
    target.threads_exported += update.threads_exported
    target.threads_unchanged += update.threads_unchanged
    target.truncated_rooms += update.truncated_rooms
    target.failed_items.extend(update.failed_items)
    target.retained_room_keys.update(update.retained_room_keys)


def _record_group_failure(
    accumulators: Sequence[ThreadExportAccumulator],
    rooms: Sequence[ThreadExportRoom],
    error: str,
) -> None:
    """Record an account-level failure without retracting rooms whose authorization is unknown."""
    for room in rooms:
        for accumulator in accumulators:
            target = accumulator.target
            if not target_accepts_room(target, room):
                retract_room_export(accumulator, room)
                continue
            accumulator.retained_room_keys.add(room.key)
            accumulator.failed_items.append(failure_for_room(room, error))


def _requested_invited_groups(
    discovered_groups: Sequence[tuple[str, Sequence[ThreadExportRoom]]],
    accumulators: Sequence[ThreadExportAccumulator],
) -> list[tuple[str, list[ThreadExportRoom]]]:
    """Retract rooms excluded by every target and return groups that need Matrix work."""
    requested_groups: list[tuple[str, list[ThreadExportRoom]]] = []
    for entity_name, rooms in discovered_groups:
        accepted_rooms: list[ThreadExportRoom] = []
        for room in rooms:
            if any(target_accepts_room(accumulator.target, room) for accumulator in accumulators):
                accepted_rooms.append(room)
            else:
                for accumulator in accumulators:
                    retract_room_export(accumulator, room)
        if accepted_rooms:
            requested_groups.append((entity_name, accepted_rooms))
    return requested_groups


def _reconcile_full_pass(accumulators: Sequence[ThreadExportAccumulator]) -> None:
    """Remove room directories that the completed full pass did not retain."""
    for accumulator in accumulators:
        output_dir = accumulator.target.output_dir
        try:
            if accumulator.rooms_exported == 0:
                logger.warning(
                    "Skipping thread export directory reconciliation without exported rooms",
                    output_dir=str(output_dir),
                    retained_rooms=len(accumulator.retained_room_keys),
                    failures=len(accumulator.failed_items),
                )
                continue
            reconcile_room_directories(
                output_dir,
                accumulator.retained_room_keys,
            )
        except (OSError, RuntimeError) as exc:
            accumulator.failed_items.append(
                failure_for_target(f"Target reconciliation failed: {exc}"),
            )


def _validated_targets(
    accumulators: Sequence[ThreadExportAccumulator],
) -> tuple[ThreadExportAccumulator, ...]:
    """Reject every resolved overlap, then prepare only disjoint roots."""
    candidates: list[tuple[ThreadExportAccumulator, Path]] = []
    for accumulator in accumulators:
        authored_output_dir = accumulator.target.output_dir
        try:
            output_dir = canonicalize_output_dir(authored_output_dir)
            resolved_output_dir = output_dir.resolve()
        except (OSError, RuntimeError) as exc:
            accumulator.failed_items.append(failure_for_target(f"output directory validation failed: {exc}"))
            logger.warning(
                "Skipping thread export target whose output directory could not be validated",
                output_dir=str(authored_output_dir),
                error=str(exc),
            )
            continue
        accumulator.target = replace(accumulator.target, output_dir=output_dir)
        candidates.append((accumulator, resolved_output_dir))

    overlaps: dict[int, list[Path]] = {}
    for index, (_, resolved_output_dir) in enumerate(candidates):
        for other_index in range(index + 1, len(candidates)):
            other_accumulator, other_resolved_output_dir = candidates[other_index]
            if not (
                resolved_output_dir == other_resolved_output_dir
                or resolved_output_dir.is_relative_to(other_resolved_output_dir)
                or other_resolved_output_dir.is_relative_to(resolved_output_dir)
            ):
                continue
            overlaps.setdefault(index, []).append(other_accumulator.target.output_dir)
            overlaps.setdefault(other_index, []).append(candidates[index][0].target.output_dir)

    prepared: list[ThreadExportAccumulator] = []
    for index, (accumulator, resolved_output_dir) in enumerate(candidates):
        if overlapping := overlaps.get(index):
            conflicting = ", ".join(str(path) for path in overlapping)
            accumulator.failed_items.append(
                failure_for_target(
                    f"output directory resolving to {resolved_output_dir} "
                    f"overlaps another enabled target: {conflicting}",
                ),
            )
            logger.warning(
                "Skipping thread export target with overlapping output directory",
                output_dir=str(accumulator.target.output_dir),
                resolved_output_dir=str(resolved_output_dir),
                overlapping_output_dirs=[str(path) for path in overlapping],
            )
            continue
        try:
            prepare_export_root(accumulator.target.output_dir)
        except (OSError, RuntimeError) as exc:
            accumulator.failed_items.append(failure_for_target(f"output directory preparation failed: {exc}"))
            logger.warning(
                "Skipping thread export target with unusable output directory",
                output_dir=str(accumulator.target.output_dir),
                error=str(exc),
            )
            continue
        prepared.append(accumulator)
    return tuple(prepared)


def _journal_principal_id(user: AgentMatrixUser) -> str:
    """Return the journal principal one export login reads the projection as.

    The same identity the running bot for that account writes under, built the
    same way. A different spelling would not fail: it would open an empty
    projection and export every thread as if the room had no history.
    """
    return f"{user.agent_name}@{user.matrix_id.full_id}"


async def _run_export_group(
    group: ThreadExportGroup,
    *,
    homeserver: str,
    config: Config,
    runtime_paths: RuntimePaths,
    journal_store: EventJournalStore,
    accumulators: Sequence[ThreadExportAccumulator],
    max_thread_roots: int,
) -> None:
    """Run one account group without preventing later groups after a failure."""
    try:
        client = await login_agent_user(homeserver, group.user, runtime_paths)
    except Exception as exc:
        _record_group_failure(accumulators, group.rooms, f"Matrix login failed: {exc}")
        return
    try:
        group_accumulators = await export_threads_for_targets_for_client(
            client=client,
            reader=export_conversation_reader(
                client=client,
                config=config,
                store=journal_store.principal(_journal_principal_id(group.user)),
                self_sender=group.user.matrix_id.full_id,
            ),
            config=config,
            runtime_paths=runtime_paths,
            rooms=group.rooms,
            targets=tuple(accumulator.target for accumulator in accumulators),
            max_thread_roots=max_thread_roots,
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
) -> tuple[ThreadExportStats, ...]:
    """Login with persisted Matrix accounts and export once to every target.

    Rooms come from ``matrix_state.yaml`` plus every entity's persisted invited rooms.
    Invited rooms are exported with the invited entity's own account, because the primary export
    account is not necessarily a member of user-created rooms.

    Thread bodies come from the journal's visible-message projection, read as the same principal a
    running bot writes it under, so an export reduces edits, redactions, and sidecars exactly the
    way the prompt path does. A thread nobody has read yet is hydrated from the homeserver once;
    after that the body costs no Matrix history call at all.

    Each source thread is fetched once per room and fanned out to every authorized target.
    Scoped targets export only rooms where their required member is currently joined.
    A failed membership check leaves prior exports untouched, records a failure, and writes nothing new.
    A successful check that proves the member absent removes the prior room export.
    """
    if not targets:
        return ()
    accumulators = tuple(ThreadExportAccumulator(target=target) for target in targets)
    validated_targets = _validated_targets(accumulators)
    if not validated_targets:
        return tuple(accumulator.stats() for accumulator in accumulators)

    homeserver = runtime_matrix_homeserver(runtime_paths=runtime_paths)
    state_rooms = export_rooms(runtime_paths, room_filter)
    discovered_invited_groups = invited_export_rooms(
        config,
        runtime_paths,
        room_filter,
        known_room_ids={room.room_id for room in state_rooms},
    )
    invited_groups = _requested_invited_groups(discovered_invited_groups, validated_targets)
    export_groups = build_export_groups(
        runtime_paths=runtime_paths,
        homeserver=homeserver,
        state_rooms=state_rooms,
        invited_groups=invited_groups,
    )

    if not export_groups:
        select_export_account(runtime_paths, homeserver)
        if room_filter is None:
            _reconcile_full_pass(validated_targets)
        return tuple(accumulator.stats() for accumulator in accumulators)

    ready_groups: list[ThreadExportGroup] = []
    for group in export_groups:
        if isinstance(group, ThreadExportGroupFailure):
            _record_group_failure(validated_targets, group.rooms, group.error)
        else:
            ready_groups.append(group)

    open_journal = open_event_journal(
        config.event_journal,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
    )
    journal_store = open_journal.store
    try:
        # Its own process, so nothing has vouched for this database yet. An
        # export reading a stranger's journal reports the wrong history rather
        # than failing, which is the quietest way to be wrong.
        await bind_event_journal(
            journal_store,
            journal_config=config.event_journal,
            runtime_paths=runtime_paths,
            storage_path=runtime_paths.storage_root,
        )
        for group in ready_groups:
            await _run_export_group(
                group,
                homeserver=homeserver,
                config=config,
                runtime_paths=runtime_paths,
                journal_store=journal_store,
                accumulators=validated_targets,
                max_thread_roots=max_thread_roots,
            )
    finally:
        await open_journal.close()

    if room_filter is None:
        _reconcile_full_pass(validated_targets)
    return tuple(accumulator.stats() for accumulator in accumulators)


async def export_threads_once(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    output_dir: Path | None = None,
    room_filter: str | None = None,
    max_thread_roots: int = 2000,
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
    )
    return stats[0]
