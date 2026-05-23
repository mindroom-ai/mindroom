"""Receive-time ingress coordination for human turn events.

Eligible human prompt events are admitted before slow resolution work.
The coordinator closes receive-time groups, waits for claimed async work,
retargets non-voice items from resolved voice outcomes, and emits sealed
ready batches to the downstream dispatch gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .coalescing import upload_grace_hard_cap_seconds
from .coalescing_batch import close_pending_event_metadata
from .dispatch_handoff import is_media_dispatch_event
from .dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_COALESCING_CLASS, VOICE_SOURCE_KIND
from .logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.coalescing import CoalescingGate
    from mindroom.coalescing_batch import CoalescingKey, PendingEvent

logger = get_logger(__name__)


@dataclass(frozen=True)
class IngressProvisionalKey:
    """Receive-time grouping key available before thread resolution finishes."""

    room_id: str
    requester_user_id: str


@dataclass(frozen=True)
class PromptReadyIngressResult:
    """Ready prompt event with both preliminary and final coalescing keys."""

    pending_event: PendingEvent
    key: CoalescingKey
    preliminary_key: CoalescingKey
    received_order: int
    received_wall_time: float


@dataclass(frozen=True)
class BarrierReadyIngressResult:
    """Ready barrier event that must dispatch independently from prompt batches."""

    pending_event: PendingEvent
    key: CoalescingKey
    received_order: int
    received_wall_time: float


@dataclass(frozen=True)
class DropReadyIngressResult:
    """Ready ingress item that is omitted and may split prompt batches."""

    received_order: int
    received_wall_time: float
    split_prompt_group: bool


type ReadyIngressResult = PromptReadyIngressResult | BarrierReadyIngressResult | DropReadyIngressResult


@dataclass(frozen=True)
class RawVoiceIngressItem:
    """One raw audio event accepted before speech-to-text has completed."""

    preliminary_key_task: asyncio.Task[CoalescingKey]
    ready_task: asyncio.Task[ReadyIngressResult | None]


def _target_key_for_non_voice_item(
    original_key: CoalescingKey,
    successful_voice_keys: Sequence[CoalescingKey],
) -> CoalescingKey:
    """Return the only successful voice key when a non-voice item can be retargeted."""
    unique_voice_keys = tuple(dict.fromkeys(successful_voice_keys))
    return unique_voice_keys[0] if len(unique_voice_keys) == 1 else original_key


@dataclass
class _ReadyIngressAdmission:
    ready_task: asyncio.Task[ReadyIngressResult | None]
    received_order: int
    received_wall_time: float
    source_kind: str | None = None
    coalescing_class: str | None = None
    is_raw_voice: bool = False
    is_barrier: bool = False
    preliminary_key_task: asyncio.Task[CoalescingKey] | None = None


@dataclass
class _IngressPromptGroup:
    items: list[_ReadyIngressAdmission] = field(default_factory=list)
    drain_task: asyncio.Task[None] | None = None
    predecessor_drain_tasks: tuple[asyncio.Task[None], ...] = ()
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_generation: int = 0
    deadline: float | None = None
    force_dispatch: bool = False
    drain_all_requested: bool = False
    accepting_late_prompts: bool = True


class TurnIngressCoalescingGate:
    """Coordinate receive-order human prompt events before ready dispatch."""

    class ReadyTaskError(Exception):
        """Ready-task failure that still has pending-event metadata to close."""

        def __init__(self, pending_event: PendingEvent, cause: BaseException) -> None:
            super().__init__(str(cause))
            self.pending_event = pending_event
            self.cause = cause

    def __init__(
        self,
        *,
        debounce_seconds: Callable[[], float],
        upload_grace_seconds: Callable[[], float],
        is_shutting_down: Callable[[], bool],
        coalescing_gate: CoalescingGate | None = None,
    ) -> None:
        self._debounce_seconds = debounce_seconds
        self._upload_grace_seconds = upload_grace_seconds
        self._is_shutting_down = is_shutting_down
        self._coalescing_gate = coalescing_gate
        self._next_received_order = 0
        self._ingress_open_groups: dict[IngressProvisionalKey, _IngressPromptGroup] = {}
        self._ingress_grace_groups: dict[IngressProvisionalKey, _IngressPromptGroup] = {}
        self._ingress_draining_groups: dict[IngressProvisionalKey, list[_IngressPromptGroup]] = {}
        self._ingress_claimed_voice_groups: dict[IngressProvisionalKey, list[_IngressPromptGroup]] = {}
        self._ingress_drain_tasks: set[asyncio.Task[None]] = set()
        self._ingress_drain_errors: list[BaseException] = []

    def claim_received_metadata(self) -> tuple[int, float]:
        """Reserve the coordinator-owned receive order and wall-clock timestamp."""
        self._next_received_order += 1
        return self._next_received_order, time.time()

    def bind_coalescing_gate(self, coalescing_gate: CoalescingGate) -> None:
        """Bind the downstream ready-dispatch gate after paired construction."""
        self._coalescing_gate = coalescing_gate

    async def admit_ready_task(
        self,
        provisional_key: IngressProvisionalKey,
        *,
        ready_task: asyncio.Task[ReadyIngressResult | None],
        source_kind: str | None = None,
        coalescing_class: str | None = None,
        barrier: bool,
    ) -> None:
        """Admit one text/media/barrier ready task before slow resolution completes."""
        received_order, received_wall_time = self.claim_received_metadata()
        admission = _ReadyIngressAdmission(
            ready_task=ready_task,
            received_order=received_order,
            received_wall_time=received_wall_time,
            source_kind=source_kind,
            coalescing_class=coalescing_class,
            is_barrier=barrier,
        )
        if barrier:
            if await self._dispatch_barrier_after_accepting_groups(provisional_key, admission):
                return
            task = self._admit_independent_barrier_admission(provisional_key, admission)
            await task
            return
        await self._admit_prompt_admission(provisional_key, admission)

    async def admit_raw_voice(self, provisional_key: IngressProvisionalKey, item: RawVoiceIngressItem) -> None:
        """Admit one raw voice event before preliminary key resolution or STT is awaited."""
        received_order, received_wall_time = self.claim_received_metadata()
        admission = _ReadyIngressAdmission(
            ready_task=item.ready_task,
            received_order=received_order,
            received_wall_time=received_wall_time,
            coalescing_class=VOICE_COALESCING_CLASS,
            is_raw_voice=True,
            preliminary_key_task=item.preliminary_key_task,
        )
        await self._admit_prompt_admission(provisional_key, admission)

    async def _admit_prompt_admission(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> None:
        if self._append_to_latest_claimed_voice_group(provisional_key, admission):
            return

        grace_group = self._ingress_grace_groups.get(provisional_key)
        if grace_group is not None and grace_group.accepting_late_prompts:
            if admission.is_raw_voice:
                self._append_to_group(provisional_key, grace_group, admission)
                grace_group.force_dispatch = True
                self._wake_ingress_group(grace_group)
                return
            if self._admission_is_known_media_prompt(admission):
                self._append_to_group(provisional_key, grace_group, admission)
                self._wake_ingress_group(grace_group)
                return
            if not admission.ready_task.done():
                await asyncio.sleep(0)
            grace_group = self._ingress_grace_groups.get(provisional_key)
            if (
                grace_group is not None
                and grace_group.accepting_late_prompts
                and self._admission_is_ready_media_prompt(admission)
            ):
                self._append_to_group(provisional_key, grace_group, admission)
                self._wake_ingress_group(grace_group)
                return
            if grace_group is not None and grace_group.accepting_late_prompts:
                self._seal_grace_group(provisional_key)

        open_group = self._ingress_open_groups.get(provisional_key)
        if open_group is not None:
            self._append_to_group(provisional_key, open_group, admission)
            return

        if await self._append_voice_admission_to_latest_draining_group(provisional_key, admission):
            return

        self._append_to_open_prompt_group(provisional_key, admission)

    def _append_to_open_prompt_group(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> None:
        group = self._ingress_open_groups.get(provisional_key)
        if group is None:
            group = self._new_ingress_prompt_group(provisional_key)
            self._ingress_open_groups[provisional_key] = group
        self._append_to_group(provisional_key, group, admission)

    def _new_ingress_prompt_group(self, provisional_key: IngressProvisionalKey) -> _IngressPromptGroup:
        """Create a group that dispatches after older groups for the same receive key."""
        return _IngressPromptGroup(
            predecessor_drain_tasks=tuple(
                task
                for task in (group.drain_task for group in self._ingress_draining_groups.get(provisional_key, ()))
                if task is not None and not task.done()
            ),
        )

    def _append_to_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
        admission: _ReadyIngressAdmission,
    ) -> None:
        group.items.append(admission)
        admission.ready_task.add_done_callback(lambda _task: self._wake_ingress_group(group))
        if admission.preliminary_key_task is not None:
            admission.preliminary_key_task.add_done_callback(lambda _task: self._wake_ingress_group(group))
        self._ensure_ingress_drain_task(provisional_key, group)
        self._wake_ingress_group(group)

    def _admit_independent_barrier_admission(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> asyncio.Task[None]:
        predecessor_groups = self._groups_for_provisional_key(provisional_key)
        group = _IngressPromptGroup(items=[admission], accepting_late_prompts=False, force_dispatch=True)
        admission.ready_task.add_done_callback(lambda _task: self._wake_ingress_group(group))
        task = asyncio.create_task(
            self._dispatch_independent_barrier_admission(
                provisional_key,
                group=group,
                predecessor_groups=predecessor_groups,
                admission=admission,
            ),
            name=f"turn_ingress_barrier:{provisional_key.room_id}:{provisional_key.requester_user_id}",
        )
        group.drain_task = task
        self._register_draining_group(provisional_key, group)
        self._ingress_drain_tasks.add(task)
        task.add_done_callback(self._finish_ingress_drain_task)
        self._wake_ingress_group(group)
        return task

    async def _dispatch_independent_barrier_admission(
        self,
        provisional_key: IngressProvisionalKey,
        *,
        group: _IngressPromptGroup,
        predecessor_groups: tuple[_IngressPromptGroup, ...],
        admission: _ReadyIngressAdmission,
    ) -> None:
        try:
            await self._wait_for_barrier_predecessor_groups(predecessor_groups)
            await self._forward_independent_ready_admission(admission)
        finally:
            group.accepting_late_prompts = False
            self._remove_draining_group(provisional_key, group)
            if group.drain_task is asyncio.current_task():
                group.drain_task = None

    def _groups_for_provisional_key(self, provisional_key: IngressProvisionalKey) -> tuple[_IngressPromptGroup, ...]:
        groups = [
            group
            for group in (
                self._ingress_open_groups.get(provisional_key),
                self._ingress_grace_groups.get(provisional_key),
            )
            if group is not None
        ]
        groups.extend(self._ingress_draining_groups.get(provisional_key, ()))
        groups.extend(self._ingress_claimed_voice_groups.get(provisional_key, ()))

        unique_groups: list[_IngressPromptGroup] = []
        seen_ids: set[int] = set()
        for group in groups:
            if id(group) in seen_ids:
                continue
            seen_ids.add(id(group))
            unique_groups.append(group)
        return tuple(unique_groups)

    async def _wait_for_barrier_predecessor_groups(self, groups: tuple[_IngressPromptGroup, ...]) -> None:
        current_task = asyncio.current_task()
        predecessor_tasks = [
            group.drain_task
            for group in groups
            if group.drain_task is not None
            and not group.drain_task.done()
            and group.drain_task is not current_task
            and self._predecessor_group_blocks_barrier(group)
        ]
        if not predecessor_tasks:
            return
        await asyncio.gather(*predecessor_tasks, return_exceptions=True)
        self._collect_finished_ingress_drain_tasks()
        self._raise_ingress_drain_errors()

    async def _dispatch_barrier_after_accepting_groups(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> bool:
        groups = [group for group in self._groups_for_provisional_key(provisional_key) if group.accepting_late_prompts]
        if not groups:
            return False

        ready_prefixes: list[_ReadyIngressAdmission] = []
        for group in groups:
            self._seal_group_for_barrier(provisional_key, group)
            ordered_items = sorted(group.items, key=lambda item: item.received_order)
            ready_prefix_length = self._ready_prefix_length(ordered_items)
            ready_prefixes.extend(ordered_items[:ready_prefix_length])
            group.items = ordered_items[ready_prefix_length:]

        await self._wait_for_ready_predecessor_groups(provisional_key, groups)
        if ready_prefixes:
            await self._dispatch_fixed_ingress_admissions(
                sorted(ready_prefixes, key=lambda item: item.received_order),
            )
        await self._forward_independent_ready_admission(admission)
        return True

    async def _wait_for_ready_predecessor_groups(
        self,
        provisional_key: IngressProvisionalKey,
        groups: list[_IngressPromptGroup],
    ) -> None:
        predecessor_tasks = {task for group in groups for task in group.predecessor_drain_tasks if not task.done()}
        if not predecessor_tasks:
            return
        current_task = asyncio.current_task()
        tasks_to_wait = [
            group.drain_task
            for group in self._ingress_draining_groups.get(provisional_key, ())
            if group.drain_task in predecessor_tasks
            and group.drain_task is not current_task
            and self._predecessor_group_blocks_barrier(group)
        ]
        if not tasks_to_wait:
            return
        await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        self._collect_finished_ingress_drain_tasks()
        self._raise_ingress_drain_errors()

    def _append_to_latest_claimed_voice_group(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> bool:
        claimed_groups = self._ingress_claimed_voice_groups.get(provisional_key)
        if not claimed_groups:
            return False
        for group in reversed(claimed_groups):
            if group.accepting_late_prompts:
                group.items.append(admission)
                admission.ready_task.add_done_callback(
                    lambda _task, target_group=group: self._wake_ingress_group(target_group),
                )
                if admission.preliminary_key_task is not None:
                    admission.preliminary_key_task.add_done_callback(
                        lambda _task, target_group=group: self._wake_ingress_group(target_group),
                    )
                self._wake_ingress_group(group)
                return True
        self._prune_claimed_voice_groups(provisional_key)
        return False

    async def _append_voice_admission_to_latest_draining_group(
        self,
        provisional_key: IngressProvisionalKey,
        admission: _ReadyIngressAdmission,
    ) -> bool:
        if not await self._admission_is_voice_prompt(admission):
            return False
        draining_groups = self._ingress_draining_groups.get(provisional_key)
        if not draining_groups:
            return False
        for group in reversed(draining_groups):
            if group.drain_task is None or group.drain_task.done() or not group.accepting_late_prompts:
                continue
            self._append_to_group(provisional_key, group, admission)
            self._register_claimed_voice_group_once(provisional_key, group)
            return True
        return False

    def _seal_group_for_barrier(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        group.accepting_late_prompts = False
        group.force_dispatch = True
        group.deadline = time.monotonic()
        if self._ingress_open_groups.get(provisional_key) is group:
            self._ingress_open_groups.pop(provisional_key, None)
        if self._ingress_grace_groups.get(provisional_key) is group:
            self._ingress_grace_groups.pop(provisional_key, None)
        self._ensure_ingress_drain_task(provisional_key, group)
        self._wake_ingress_group(group)

    def _seal_grace_group(self, provisional_key: IngressProvisionalKey) -> None:
        group = self._ingress_grace_groups.get(provisional_key)
        if group is None:
            return
        group.accepting_late_prompts = False
        group.force_dispatch = True
        group.deadline = time.monotonic()
        self._ingress_grace_groups.pop(provisional_key, None)
        self._ensure_ingress_drain_task(provisional_key, group)
        self._wake_ingress_group(group)

    def _register_claimed_voice_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        self._ingress_claimed_voice_groups.setdefault(provisional_key, []).append(group)

    def _register_claimed_voice_group_once(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        groups = self._ingress_claimed_voice_groups.setdefault(provisional_key, [])
        if not any(candidate is group for candidate in groups):
            groups.append(group)

    def _register_draining_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        self._ingress_draining_groups.setdefault(provisional_key, []).append(group)

    def _remove_draining_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        groups = self._ingress_draining_groups.get(provisional_key)
        if groups is None:
            return
        remaining = [
            candidate
            for candidate in groups
            if candidate is not group and candidate.drain_task is not None and not candidate.drain_task.done()
        ]
        if remaining:
            self._ingress_draining_groups[provisional_key] = remaining
            return
        self._ingress_draining_groups.pop(provisional_key, None)

    def _remove_claimed_voice_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        groups = self._ingress_claimed_voice_groups.get(provisional_key)
        if groups is None:
            return
        remaining = [candidate for candidate in groups if candidate is not group and candidate.accepting_late_prompts]
        if remaining:
            self._ingress_claimed_voice_groups[provisional_key] = remaining
            return
        self._ingress_claimed_voice_groups.pop(provisional_key, None)

    def _prune_claimed_voice_groups(self, provisional_key: IngressProvisionalKey) -> None:
        groups = self._ingress_claimed_voice_groups.get(provisional_key)
        if groups is None:
            return
        live_groups = [group for group in groups if group.accepting_late_prompts]
        if live_groups:
            self._ingress_claimed_voice_groups[provisional_key] = live_groups
            return
        self._ingress_claimed_voice_groups.pop(provisional_key, None)

    def _ensure_ingress_drain_task(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        if group.drain_task is not None and not group.drain_task.done():
            return
        group.drain_task = asyncio.create_task(
            self._drain_ingress_group(provisional_key, group),
            name=f"turn_ingress_drain:{provisional_key.room_id}:{provisional_key.requester_user_id}",
        )
        self._ingress_drain_tasks.add(group.drain_task)
        group.drain_task.add_done_callback(self._finish_ingress_drain_task)

    def _finish_ingress_drain_task(self, task: asyncio.Task[None]) -> None:
        if task not in self._ingress_drain_tasks:
            return
        self._ingress_drain_tasks.discard(task)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            return
        self._ingress_drain_errors.append(error)
        logger.error(
            "Turn ingress drain task failed",
            error=repr(error),
            exc_info=(type(error), error, error.__traceback__),
        )

    def _collect_finished_ingress_drain_tasks(self) -> None:
        for task in tuple(self._ingress_drain_tasks):
            if task.done():
                self._finish_ingress_drain_task(task)

    def _raise_ingress_drain_errors(self) -> None:
        if not self._ingress_drain_errors:
            return
        errors = self._ingress_drain_errors
        self._ingress_drain_errors = []
        if len(errors) == 1:
            raise errors[0]
        msg = "Turn ingress drain tasks failed"
        raise BaseExceptionGroup(msg, errors)

    def _all_ingress_prompt_groups(self) -> tuple[_IngressPromptGroup, ...]:
        groups = [
            *self._ingress_open_groups.values(),
            *self._ingress_grace_groups.values(),
            *(group for group_list in self._ingress_draining_groups.values() for group in group_list),
            *(group for group_list in self._ingress_claimed_voice_groups.values() for group in group_list),
        ]
        unique_groups: list[_IngressPromptGroup] = []
        seen_ids: set[int] = set()
        for group in groups:
            if id(group) in seen_ids:
                continue
            seen_ids.add(id(group))
            unique_groups.append(group)
        return tuple(unique_groups)

    def cancel_unresolved_admissions(self) -> None:
        """Cancel unresolved ingress admission work so shutdown can drain boundedly."""
        for group in self._all_ingress_prompt_groups():
            for admission in group.items:
                self._close_ready_metadata_when_cancelling_preliminary(admission)
                if admission.preliminary_key_task is not None and not admission.preliminary_key_task.done():
                    admission.preliminary_key_task.cancel()
                if not admission.ready_task.done():
                    admission.ready_task.cancel()
            self._wake_ingress_group(group)

    def _close_ready_metadata_when_cancelling_preliminary(self, admission: _ReadyIngressAdmission) -> None:
        if (
            admission.preliminary_key_task is None
            or admission.preliminary_key_task.done()
            or not admission.ready_task.done()
        ):
            return
        try:
            result = admission.ready_task.result()
        except (KeyboardInterrupt, SystemExit):
            raise
        except self.ReadyTaskError as error:
            close_pending_event_metadata([error.pending_event])
            return
        except BaseException:
            return
        if isinstance(result, (PromptReadyIngressResult, BarrierReadyIngressResult)):
            close_pending_event_metadata([result.pending_event])

    @staticmethod
    def _wake_ingress_group(group: _IngressPromptGroup) -> None:
        group.wake_generation += 1
        group.wake_event.set()

    def _require_coalescing_gate(self) -> CoalescingGate:
        if self._coalescing_gate is None:
            msg = "Receive-time ingress coordinator requires a downstream coalescing gate"
            raise RuntimeError(msg)
        return self._coalescing_gate

    @staticmethod
    def _pending_event_is_media(pending_event: PendingEvent) -> bool:
        return pending_event.source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND} or is_media_dispatch_event(
            pending_event.event,
        )

    @staticmethod
    def _admission_is_known_media_prompt(admission: _ReadyIngressAdmission) -> bool:
        return admission.source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND}

    def _admission_is_ready_media_prompt(self, admission: _ReadyIngressAdmission) -> bool:
        if admission.source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND}:
            return True
        if not admission.ready_task.done() or admission.ready_task.cancelled():
            return False
        try:
            result = admission.ready_task.result()
        except BaseException:
            return False
        return isinstance(result, PromptReadyIngressResult) and self._pending_event_is_media(result.pending_event)

    @staticmethod
    def _ready_result_is_voice_prompt(result: ReadyIngressResult | None) -> bool:
        return (
            isinstance(result, PromptReadyIngressResult)
            and result.pending_event.coalescing_class == VOICE_COALESCING_CLASS
        )

    def _admission_is_completed_voice_prompt(self, admission: _ReadyIngressAdmission) -> bool:
        if (
            admission.is_raw_voice
            or admission.source_kind == VOICE_SOURCE_KIND
            or admission.coalescing_class == VOICE_COALESCING_CLASS
        ):
            return True
        if not admission.ready_task.done() or admission.ready_task.cancelled():
            return False
        try:
            result = admission.ready_task.result()
        except BaseException:
            return False
        return self._ready_result_is_voice_prompt(result)

    async def _admission_is_voice_prompt(self, admission: _ReadyIngressAdmission) -> bool:
        if (
            admission.is_raw_voice
            or admission.source_kind == VOICE_SOURCE_KIND
            or admission.coalescing_class == VOICE_COALESCING_CLASS
        ):
            return True
        if not admission.ready_task.done():
            await asyncio.sleep(0)
        return self._admission_is_completed_voice_prompt(admission)

    def _group_has_voice_prompt(self, group: _IngressPromptGroup) -> bool:
        return any(self._admission_is_completed_voice_prompt(admission) for admission in group.items)

    def _group_should_wait_for_upload_grace(self, group: _IngressPromptGroup) -> bool:
        if not group.items or self._group_has_voice_prompt(group):
            return False
        return all(self._admission_can_start_upload_grace(admission) for admission in group.items)

    def _admission_can_start_upload_grace(self, admission: _ReadyIngressAdmission) -> bool:
        if (
            admission.is_raw_voice
            or admission.source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND}
            or admission.coalescing_class == VOICE_COALESCING_CLASS
        ):
            return False
        if not admission.ready_task.done() or admission.ready_task.cancelled():
            return True
        try:
            result = admission.ready_task.result()
        except BaseException:
            return False
        return (
            isinstance(result, PromptReadyIngressResult)
            and not self._pending_event_is_media(result.pending_event)
            and not self._ready_result_is_voice_prompt(result)
        )

    async def _wait_for_ingress_deadline(self, group: _IngressPromptGroup, deadline: float) -> bool:
        while True:
            delay = deadline - time.monotonic()
            if delay <= 0:
                return False
            wake_generation = group.wake_generation
            group.wake_event.clear()
            if group.deadline != deadline or group.wake_generation != wake_generation:
                return True
            try:
                await asyncio.wait_for(group.wake_event.wait(), timeout=delay)
            except TimeoutError:
                return False
            else:
                return True

    async def _wait_for_ingress_debounce(self, group: _IngressPromptGroup) -> None:
        debounce_seconds = max(self._debounce_seconds(), 0.0)
        if (
            debounce_seconds <= 0
            or self._is_shutting_down()
            or group.drain_all_requested
            or group.force_dispatch
            or self._group_has_completed_split(group)
        ):
            group.deadline = time.monotonic()
            return
        group.deadline = time.monotonic() + debounce_seconds
        while True:
            deadline = group.deadline or time.monotonic()
            if not await self._wait_for_ingress_deadline(group, deadline):
                return
            if (
                self._is_shutting_down()
                or group.drain_all_requested
                or group.force_dispatch
                or self._group_has_completed_split(group)
            ):
                return
            group.deadline = time.monotonic() + debounce_seconds

    async def _wait_for_ingress_upload_grace(self, group: _IngressPromptGroup) -> None:
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        if (
            grace_seconds <= 0
            or self._is_shutting_down()
            or group.drain_all_requested
            or group.force_dispatch
            or self._group_has_completed_split(group)
        ):
            return
        hard_deadline = time.monotonic() + upload_grace_hard_cap_seconds(grace_seconds)
        group.deadline = min(time.monotonic() + grace_seconds, hard_deadline)
        while True:
            deadline = group.deadline or time.monotonic()
            if not await self._wait_for_ingress_deadline(group, deadline):
                return
            if (
                self._is_shutting_down()
                or group.drain_all_requested
                or group.force_dispatch
                or self._group_has_completed_split(group)
            ):
                return
            remaining_seconds = max(hard_deadline - time.monotonic(), 0.0)
            if remaining_seconds <= 0:
                return
            group.deadline = time.monotonic() + min(grace_seconds, remaining_seconds)

    async def _wait_for_predecessor_drain_tasks(self, group: _IngressPromptGroup) -> None:
        predecessor_tasks = tuple(task for task in group.predecessor_drain_tasks if not task.done())
        if not predecessor_tasks:
            return
        await asyncio.gather(*predecessor_tasks, return_exceptions=True)
        self._collect_finished_ingress_drain_tasks()
        self._raise_ingress_drain_errors()

    async def _drain_ingress_group(
        self,
        provisional_key: IngressProvisionalKey,
        group: _IngressPromptGroup,
    ) -> None:
        registered_claimed_voice = False
        registered_draining = False
        try:
            await self._wait_for_ingress_debounce(group)
            if self._ingress_open_groups.get(provisional_key) is group:
                self._ingress_open_groups.pop(provisional_key, None)
            if self._group_should_wait_for_upload_grace(group):
                self._ingress_grace_groups[provisional_key] = group
                await self._wait_for_ingress_upload_grace(group)
                if self._ingress_grace_groups.get(provisional_key) is group:
                    self._ingress_grace_groups.pop(provisional_key, None)
            registered_draining = True
            self._register_draining_group(provisional_key, group)
            if self._group_has_voice_prompt(group):
                registered_claimed_voice = True
                self._register_claimed_voice_group(provisional_key, group)
            await self._wait_for_predecessor_drain_tasks(group)
            await self._dispatch_ingress_group(group)
        finally:
            group.accepting_late_prompts = False
            if self._ingress_open_groups.get(provisional_key) is group:
                self._ingress_open_groups.pop(provisional_key, None)
            if self._ingress_grace_groups.get(provisional_key) is group:
                self._ingress_grace_groups.pop(provisional_key, None)
            if registered_claimed_voice:
                self._remove_claimed_voice_group(provisional_key, group)
            if registered_draining:
                self._remove_draining_group(provisional_key, group)
            if group.drain_task is asyncio.current_task():
                group.drain_task = None

    async def _await_ready_admission(
        self,
        admission: _ReadyIngressAdmission,
    ) -> ReadyIngressResult | None:
        preliminary_key: CoalescingKey | None = None
        try:
            if admission.preliminary_key_task is not None:
                preliminary_key = await admission.preliminary_key_task
            result = await admission.ready_task
        except (KeyboardInterrupt, SystemExit):
            raise
        except self.ReadyTaskError as error:
            close_pending_event_metadata([error.pending_event])
            logger.warning(
                "Turn ingress ready task failed",
                event_id=error.pending_event.event.event_id,
                source_kind=error.pending_event.source_kind,
                received_order=admission.received_order,
                error=repr(error.cause),
                exc_info=(type(error.cause), error.cause, error.cause.__traceback__),
            )
            result = None
        except asyncio.CancelledError as error:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            logger.warning(
                "Turn ingress ready task was cancelled",
                received_order=admission.received_order,
                exc_info=(type(error), error, error.__traceback__),
            )
            result = None
        except BaseException as error:
            logger.warning(
                "Turn ingress ready task failed",
                received_order=admission.received_order,
                error=repr(error),
                exc_info=(type(error), error, error.__traceback__),
            )
            result = None
        else:
            result = self._ready_result_with_admission_metadata(
                result,
                admission=admission,
                preliminary_key=preliminary_key,
            )
        return result

    @staticmethod
    def _ready_result_with_admission_metadata(
        result: ReadyIngressResult | None,
        *,
        admission: _ReadyIngressAdmission,
        preliminary_key: CoalescingKey | None,
    ) -> ReadyIngressResult | None:
        if result is None:
            return None
        if isinstance(result, PromptReadyIngressResult):
            return replace(
                result,
                preliminary_key=preliminary_key or result.preliminary_key,
                received_order=admission.received_order,
                received_wall_time=admission.received_wall_time,
            )
        if isinstance(result, BarrierReadyIngressResult):
            return replace(
                result,
                received_order=admission.received_order,
                received_wall_time=admission.received_wall_time,
            )
        return replace(
            result,
            received_order=admission.received_order,
            received_wall_time=admission.received_wall_time,
        )

    async def _forward_independent_ready_admission(self, admission: _ReadyIngressAdmission) -> None:
        result = await self._await_ready_admission(admission)
        if result is None:
            return
        if isinstance(result, PromptReadyIngressResult):
            await self._dispatch_prompt_segment([(admission, result)])
            return
        if isinstance(result, BarrierReadyIngressResult):
            await self._dispatch_barrier_result(result)

    async def _dispatch_ingress_group(self, group: _IngressPromptGroup) -> None:
        child_tasks: list[asyncio.Task[None]] = []
        try:
            await self._dispatch_dynamic_ingress_group(group, child_tasks)
        finally:
            group.accepting_late_prompts = False
            if child_tasks:
                await asyncio.gather(*child_tasks)

    async def _dispatch_dynamic_ingress_group(
        self,
        group: _IngressPromptGroup,
        child_tasks: list[asyncio.Task[None]],
    ) -> None:
        processed_admission_ids: set[int] = set()
        while True:
            ordered_items = [
                admission
                for admission in sorted(group.items, key=lambda item: item.received_order)
                if id(admission) not in processed_admission_ids
            ]
            if not ordered_items:
                return
            completed_split = self._completed_split_admission(ordered_items)
            if completed_split is not None:
                split_index, split_admission, split_result = completed_split
                before_split = ordered_items[:split_index]
                if before_split:
                    processed_admission_ids.update(id(admission) for admission in before_split)
                    ready_prefix_length = self._ready_prefix_length(before_split)
                    ready_prefix = before_split[:ready_prefix_length]
                    unresolved_remainder = before_split[ready_prefix_length:]
                    if ready_prefix:
                        await self._dispatch_fixed_ingress_admissions(ready_prefix)
                    if unresolved_remainder:
                        child_tasks.append(
                            asyncio.create_task(
                                self._dispatch_fixed_ingress_admissions(unresolved_remainder),
                                name="turn_ingress_segment_before_split",
                            ),
                        )
                processed_admission_ids.add(id(split_admission))
                await self._dispatch_split_result(split_result)
                continue
            if self._admissions_have_pending_work(ordered_items):
                await self._wait_for_ingress_admission_progress(ordered_items, group=group)
                continue
            group.accepting_late_prompts = False
            processed_admission_ids.update(id(admission) for admission in ordered_items)
            await self._dispatch_prompt_admissions(ordered_items)
            return

    async def _dispatch_fixed_ingress_admissions(
        self,
        admissions: list[_ReadyIngressAdmission],
    ) -> None:
        child_tasks: list[asyncio.Task[None]] = []
        processed_admission_ids: set[int] = set()
        try:
            while True:
                ordered_items = [
                    admission
                    for admission in sorted(admissions, key=lambda item: item.received_order)
                    if id(admission) not in processed_admission_ids
                ]
                if not ordered_items:
                    return
                completed_split = self._completed_split_admission(ordered_items)
                if completed_split is not None:
                    split_index, split_admission, split_result = completed_split
                    before_split = ordered_items[:split_index]
                    if before_split:
                        processed_admission_ids.update(id(admission) for admission in before_split)
                        ready_prefix_length = self._ready_prefix_length(before_split)
                        ready_prefix = before_split[:ready_prefix_length]
                        unresolved_remainder = before_split[ready_prefix_length:]
                        if ready_prefix:
                            await self._dispatch_fixed_ingress_admissions(ready_prefix)
                        if unresolved_remainder:
                            child_tasks.append(
                                asyncio.create_task(
                                    self._dispatch_fixed_ingress_admissions(unresolved_remainder),
                                    name="turn_ingress_segment_before_split",
                                ),
                            )
                    processed_admission_ids.add(id(split_admission))
                    await self._dispatch_split_result(split_result)
                    continue
                if self._admissions_have_pending_work(ordered_items):
                    await self._wait_for_ingress_admission_progress(ordered_items, group=None)
                    continue
                processed_admission_ids.update(id(admission) for admission in ordered_items)
                await self._dispatch_prompt_admissions(ordered_items)
                return
        finally:
            if child_tasks:
                await asyncio.gather(*child_tasks)

    def _completed_split_admission(
        self,
        admissions: list[_ReadyIngressAdmission],
    ) -> tuple[int, _ReadyIngressAdmission, BarrierReadyIngressResult | DropReadyIngressResult] | None:
        for index, admission in enumerate(admissions):
            split_result = self._completed_split_result(admission)
            if split_result is not None:
                return index, admission, split_result
        return None

    def _completed_split_result(
        self,
        admission: _ReadyIngressAdmission,
    ) -> BarrierReadyIngressResult | DropReadyIngressResult | None:
        if admission.preliminary_key_task is not None and (
            not admission.preliminary_key_task.done() or admission.preliminary_key_task.cancelled()
        ):
            return None
        if not admission.ready_task.done() or admission.ready_task.cancelled():
            return None
        preliminary_key: CoalescingKey | None = None
        try:
            if admission.preliminary_key_task is not None:
                preliminary_key = admission.preliminary_key_task.result()
            result = admission.ready_task.result()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return None
        result = self._ready_result_with_admission_metadata(
            result,
            admission=admission,
            preliminary_key=preliminary_key,
        )
        if isinstance(result, BarrierReadyIngressResult):
            return result
        if isinstance(result, DropReadyIngressResult) and result.split_prompt_group:
            return result
        return None

    def _group_has_completed_split(self, group: _IngressPromptGroup) -> bool:
        return self._completed_split_admission(sorted(group.items, key=lambda item: item.received_order)) is not None

    @staticmethod
    def _admissions_have_pending_work(admissions: list[_ReadyIngressAdmission]) -> bool:
        return any(TurnIngressCoalescingGate._admission_has_pending_work(admission) for admission in admissions)

    @staticmethod
    def _group_has_barrier_admission(group: _IngressPromptGroup) -> bool:
        return any(admission.is_barrier for admission in group.items)

    @classmethod
    def _predecessor_group_blocks_barrier(cls, group: _IngressPromptGroup) -> bool:
        return cls._group_has_barrier_admission(group) or not cls._admissions_have_pending_work(group.items)

    @staticmethod
    def _admission_has_pending_work(admission: _ReadyIngressAdmission) -> bool:
        return not admission.ready_task.done() or (
            admission.preliminary_key_task is not None and not admission.preliminary_key_task.done()
        )

    @classmethod
    def _ready_prefix_length(cls, admissions: list[_ReadyIngressAdmission]) -> int:
        ready_count = 0
        for admission in admissions:
            if cls._admission_has_pending_work(admission):
                return ready_count
            ready_count += 1
        return ready_count

    async def _wait_for_ingress_admission_progress(
        self,
        admissions: list[_ReadyIngressAdmission],
        *,
        group: _IngressPromptGroup | None,
    ) -> None:
        pending_tasks: list[asyncio.Task[object]] = []
        for admission in admissions:
            if not admission.ready_task.done():
                pending_tasks.append(admission.ready_task)
            if admission.preliminary_key_task is not None and not admission.preliminary_key_task.done():
                pending_tasks.append(admission.preliminary_key_task)
        if not pending_tasks:
            return

        wake_task: asyncio.Task[bool] | None = None
        wait_tasks: list[asyncio.Task[object] | asyncio.Task[bool]] = list(pending_tasks)
        if group is not None:
            wake_generation = group.wake_generation
            group.wake_event.clear()
            if group.wake_generation != wake_generation:
                return
            wake_task = asyncio.create_task(group.wake_event.wait(), name="turn_ingress_group_wake")
            wait_tasks.append(wake_task)

        try:
            await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if wake_task is not None and not wake_task.done():
                wake_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wake_task

    async def _dispatch_split_result(
        self,
        result: BarrierReadyIngressResult | DropReadyIngressResult,
    ) -> None:
        if isinstance(result, BarrierReadyIngressResult):
            await self._dispatch_barrier_result(result)

    async def _dispatch_prompt_admissions(
        self,
        admissions: list[_ReadyIngressAdmission],
    ) -> None:
        prompt_segment: list[tuple[_ReadyIngressAdmission, PromptReadyIngressResult]] = []
        for admission in sorted(admissions, key=lambda item: item.received_order):
            result = await self._await_ready_admission(admission)
            if result is None:
                continue
            if isinstance(result, PromptReadyIngressResult):
                prompt_segment.append((admission, result))
                continue
            if isinstance(result, BarrierReadyIngressResult):
                await self._dispatch_prompt_segment(prompt_segment)
                prompt_segment.clear()
                await self._dispatch_barrier_result(result)
                continue
            if result.split_prompt_group:
                await self._dispatch_prompt_segment(prompt_segment)
                prompt_segment.clear()
        await self._dispatch_prompt_segment(prompt_segment)

    async def _dispatch_prompt_segment(
        self,
        prompt_segment: list[tuple[_ReadyIngressAdmission, PromptReadyIngressResult]],
    ) -> None:
        if not prompt_segment:
            return
        coalescing_gate = self._require_coalescing_gate()
        partitions: dict[CoalescingKey, list[tuple[_ReadyIngressAdmission, PromptReadyIngressResult]]] = {}
        for admission, result in sorted(prompt_segment, key=lambda item: item[1].received_order):
            partitions.setdefault(result.preliminary_key, []).append((admission, result))

        pending_groups: list[tuple[CoalescingKey, list[PendingEvent]]] = []
        for partition_items in partitions.values():
            successful_voice_keys = [result.key for admission, result in partition_items if admission.is_raw_voice]
            partition_pending_by_key: dict[CoalescingKey, list[PendingEvent]] = {}
            for admission, result in partition_items:
                target_key = (
                    result.key
                    if admission.is_raw_voice
                    else _target_key_for_non_voice_item(result.key, successful_voice_keys)
                )
                pending_event = replace(result.pending_event, enqueue_time=result.received_wall_time)
                if not admission.is_raw_voice and target_key != result.key:
                    close_pending_event_metadata([pending_event])
                    pending_event = replace(
                        pending_event,
                        coalescing_class=VOICE_COALESCING_CLASS,
                        dispatch_policy_source_kind=None,
                        dispatch_metadata=(),
                    )
                partition_pending_by_key.setdefault(target_key, []).append(pending_event)
            pending_groups.extend(partition_pending_by_key.items())

        for index, (key, pending_events) in enumerate(pending_groups):
            try:
                await coalescing_gate.enqueue_sealed_batch(key, pending_events)
            except BaseException:
                close_pending_event_metadata(
                    [
                        pending_event
                        for _pending_key, remaining_pending_events in pending_groups[index:]
                        for pending_event in remaining_pending_events
                    ],
                )
                raise

    async def _dispatch_barrier_result(self, result: BarrierReadyIngressResult) -> None:
        coalescing_gate = self._require_coalescing_gate()
        pending_event = replace(result.pending_event, enqueue_time=result.received_wall_time)
        try:
            await coalescing_gate.enqueue(result.key, pending_event)
        except BaseException:
            close_pending_event_metadata([pending_event])
            raise

    async def drain_all(self) -> None:
        """Force every pending ingress group to flush and await its drain task."""
        while True:
            self._collect_finished_ingress_drain_tasks()
            self._raise_ingress_drain_errors()
            ingress_groups = [
                *self._ingress_open_groups.items(),
                *self._ingress_grace_groups.items(),
            ]
            tasks_to_await = [task for task in self._ingress_drain_tasks if not task.done()]
            if not ingress_groups and not tasks_to_await:
                self._raise_ingress_drain_errors()
                return
            for provisional_key, group in ingress_groups:
                group.drain_all_requested = True
                group.deadline = time.monotonic()
                self._ensure_ingress_drain_task(provisional_key, group)
                self._wake_ingress_group(group)
            tasks_to_await = [task for task in self._ingress_drain_tasks if not task.done()]
            if tasks_to_await:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
                self._raise_ingress_drain_errors()
