"""Public thread-export orchestration across Matrix account groups."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import runtime_matrix_homeserver
from mindroom.logging_config import get_logger
from mindroom.matrix.users import login_agent_user
from mindroom.runtime_support import build_owned_runtime_support, close_owned_runtime_support
from mindroom.thread_export.execution import (
    export_threads_for_targets_for_client,
    reconcile_full_pass,
    room_failure,
    target_accepts_room,
)
from mindroom.thread_export.models import (
    ThreadExportAccumulator,
    ThreadExportGroup,
    ThreadExportRoom,
    ThreadExportStats,
    ThreadExportTarget,
)
from mindroom.thread_export.selection import (
    build_export_groups,
    export_rooms,
    invited_export_rooms,
    select_export_account,
)
from mindroom.thread_export.storage import remove_room_export

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache


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
    """Record one account-level failure without leaking scoped room exports."""
    for room in rooms:
        for accumulator in accumulators:
            target = accumulator.target
            if not target_accepts_room(target, room):
                remove_room_export(target.output_dir, room)
                continue
            if target.required_member_user_id is None:
                accumulator.retained_room_keys.add(room.key)
            else:
                remove_room_export(target.output_dir, room)
            accumulator.failed_items.append(room_failure(room, error))


async def _run_export_group(
    group: ThreadExportGroup,
    *,
    homeserver: str,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache | None,
    targets: Sequence[ThreadExportTarget],
    accumulators: Sequence[ThreadExportAccumulator],
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
        group_accumulators = await export_threads_for_targets_for_client(
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
    state_rooms = export_rooms(runtime_paths, room_filter)
    invited_groups = (
        invited_export_rooms(
            config,
            runtime_paths,
            room_filter,
            known_room_ids={room.room_id for room in state_rooms},
        )
        if any(target.include_invited_rooms for target in resolved_targets)
        else []
    )
    export_groups = build_export_groups(
        runtime_paths=runtime_paths,
        homeserver=homeserver,
        state_rooms=state_rooms,
        invited_groups=invited_groups,
    )

    accumulators = tuple(ThreadExportAccumulator(target=target) for target in resolved_targets)
    if not export_groups:
        select_export_account(runtime_paths, homeserver)
        if room_filter is None:
            reconcile_full_pass(accumulators)
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
        reconcile_full_pass(accumulators)
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
