"""Admission, execution, settlement, and retry ownership for durable callbacks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import nio

from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.dispatch_admission import DispatchSourceAdmission
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.logging_config import get_logger

from .events import (
    SOURCE_CALLBACK_POLICIES,
    ApprovalCallback,
    CallbackBindings,
    DecryptionFailureCallback,
    DispatchCallback,
    DispatchCallbackResult,
    DispatchEvent,
    InviteCallback,
    MediaCallback,
    MessageCallback,
    ReactionCallback,
    RedactionCallback,
    RoomLifecycleCallback,
    dispatch_event_source,
    dispatch_source_event_id,
    parse_recovery_event,
)
from .storage import (
    DispatchCallbackKind,
    DispatchCreateResult,
    DispatchObligation,
    DispatchObligationCorruptionError,
    DispatchObligationKey,
    DispatchObligationStore,
    DispatchSemanticConsumer,
    DispatchTerminalOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .events import SourceCallbackPolicy

logger = get_logger(__name__)

_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 30.0
_TURN_BACKED_KINDS = frozenset({DispatchCallbackKind.MESSAGE, DispatchCallbackKind.MEDIA})

_SourceAdmission = Callable[
    [str, str, DispatchCallbackKind, nio.TimelineEventProvenance | None],
    Awaitable[DispatchSourceAdmission],
]
_EventProvenanceObserver = Callable[[str, nio.TimelineEventProvenance], None]
_HistoricalEventCache = Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]]
_SourceRejectionCallback = Callable[
    [nio.MatrixRoom, DispatchEvent, DispatchCallbackKind, DispatchSourceAdmission],
    Awaitable[None],
]


async def _admit_all_sources(
    _room_id: str,
    _source_event_id: str,
    _callback_kind: DispatchCallbackKind,
    _provenance: nio.TimelineEventProvenance | None,
) -> DispatchSourceAdmission:
    return DispatchSourceAdmission.ACCEPTED


def _ignore_event_provenance(
    _source_event_id: str,
    _provenance: nio.TimelineEventProvenance,
) -> None:
    pass


async def _ignore_historical_event_cache(
    _room: nio.MatrixRoom,
    _event: nio.Event,
) -> None:
    pass


_ADMITTED_OBLIGATION: ContextVar[DispatchObligation | None] = ContextVar(
    "admitted_dispatch_obligation",
    default=None,
)
_RUNNING_OBLIGATION: ContextVar[DispatchObligation | None] = ContextVar(
    "running_dispatch_obligation",
    default=None,
)


@dataclass
class DispatchObligationRunner:
    """Persist, execute, and directly recover exact Matrix callbacks."""

    store: DispatchObligationStore
    callbacks: Mapping[DispatchCallbackKind, DispatchCallback]
    room_for_id: Callable[[str], nio.MatrixRoom]
    turn_is_terminal: Callable[[str], bool]
    on_persist_failure: Callable[[], None] | None = None
    source_admission: _SourceAdmission = _admit_all_sources
    observe_event_provenance: _EventProvenanceObserver = _ignore_event_provenance
    cache_historical_event: _HistoricalEventCache = _ignore_historical_event_cache
    on_source_rejected: _SourceRejectionCallback | None = None
    background_task_owner: object | None = None
    room_lifecycle_admission_enabled: Callable[[], bool] = lambda: False
    _retry_initial_delay_seconds: float = field(default=_RETRY_INITIAL_DELAY_SECONDS, repr=False)
    _retry_max_delay_seconds: float = field(default=_RETRY_MAX_DELAY_SECONDS, repr=False)
    _active: set[DispatchObligationKey] = field(default_factory=set, init=False, repr=False)
    _active_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _retry_keys: dict[DispatchObligationKey, int] = field(default_factory=dict, init=False, repr=False)
    _retry_corrupt: set[DispatchObligationKey] = field(default_factory=set, init=False, repr=False)
    _retry_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @staticmethod
    def callbacks_for(
        *,
        on_message: MessageCallback,
        on_media: MediaCallback,
        on_reaction: ReactionCallback,
        on_approval: ApprovalCallback,
        on_invite: InviteCallback,
        on_room_lifecycle: RoomLifecycleCallback,
        on_redaction: RedactionCallback,
        on_decryption_failure: DecryptionFailureCallback,
        source_has_live_owner: Callable[[str], bool],
    ) -> Mapping[DispatchCallbackKind, DispatchCallback]:
        """Bind typed Matrix callbacks to explicit durable outcomes."""
        return CallbackBindings(
            on_message=on_message,
            on_media=on_media,
            on_reaction=on_reaction,
            on_approval=on_approval,
            on_invite=on_invite,
            on_room_lifecycle=on_room_lifecycle,
            on_redaction=on_redaction,
            on_decryption_failure=on_decryption_failure,
            source_has_live_owner=source_has_live_owner,
        ).as_mapping()

    def task_wrapper(
        self,
        callback_kind: DispatchCallbackKind,
        *,
        owner: object,
    ) -> _DispatchObligationTaskWrapper:
        """Return an ordinary callback that executes already-admitted work."""
        return _DispatchObligationTaskWrapper(
            runner=self,
            callback_kind=callback_kind,
            owner=owner,
        )

    def retry_pending_turn_source(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Return one deferred turn source to its exact stored callback owner."""
        if callback_kind not in _TURN_BACKED_KINDS:
            msg = "Deferred turn retry requires a message or media callback kind"
            raise ValueError(msg)
        self._schedule_retry(
            DispatchObligationKey(
                principal_id=self.store.principal_id,
                entity_name=self.store.entity_name,
                source_event_id=source_event_id,
                callback_kind=callback_kind,
            ),
        )

    def retry_pending_turn_sources(self, source_event_ids: tuple[str, ...]) -> None:
        """Return turn sources of either ingress kind to their stored callback owners."""
        for source_event_id in source_event_ids:
            for callback_kind in _TURN_BACKED_KINDS:
                self.retry_pending_turn_source(source_event_id, callback_kind)

    def semantic_consumer(self) -> DispatchSemanticConsumer | None:
        """Return the durable application consumer for the running callback."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            return None
        self.store.validate_bound_key(obligation.key)
        return obligation.semantic_consumer

    async def receipt_order(self) -> int:
        """Return the durable admission order of the running callback."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            msg = "Dispatch receipt order is only available inside a durable callback"
            raise RuntimeError(msg)
        return await asyncio.to_thread(self.store.receipt_order, obligation.key)

    async def claim_semantic_consumer(self, consumer: DispatchSemanticConsumer) -> None:
        """Freeze the running callback's application consumer before side effects."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            msg = "A semantic consumer can be claimed only inside a durable callback"
            raise RuntimeError(msg)
        claimed_consumer = await run_blocking_until_complete(
            self.store.claim_semantic_consumer,
            obligation.key,
            consumer,
        )
        if claimed_consumer is not consumer:
            msg = f"Dispatch callback is already owned by {claimed_consumer.value!r}"
            raise RuntimeError(msg)
        _RUNNING_OBLIGATION.set(replace(obligation, semantic_consumer=consumer))

    async def settle_intentionally_ignored_turn_sources(
        self,
        source_event_ids: tuple[str, ...],
    ) -> None:
        """Settle deferred turn sources that downstream dispatch intentionally ignored."""
        await run_blocking_until_complete(
            self.store.settle_intentionally_ignored_turn_sources,
            source_event_ids,
        )

    def register_source_callbacks(self, client: nio.AsyncClient, *, owner: object) -> None:
        """Register every source-backed correctness callback except delayed room lifecycle."""
        client.add_event_admission_callback(self._admit_source_event)
        for policy in SOURCE_CALLBACK_POLICIES:
            callback = self.task_wrapper(policy.callback_kind, owner=owner)
            if policy.predicate is None:
                for event_type in policy.event_types:
                    client.add_event_callback(callback, event_type)
                continue

            async def dispatch_matching(
                room: nio.MatrixRoom,
                event: nio.Event,
                *,
                callback: _DispatchObligationTaskWrapper = callback,
                policy: SourceCallbackPolicy = policy,
            ) -> None:
                if policy.matches(event):
                    await callback(room, event)

            for event_type in policy.event_types:
                client.add_event_callback(dispatch_matching, event_type)

    async def _admit_source_event(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        """Route every correctness-critical timeline event through one nio owner."""
        self.observe_event_provenance(event.event_id, provenance)
        if provenance is nio.TimelineEventProvenance.HISTORY:
            try:
                await self.cache_historical_event(room, event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise nio.CallbackNotAcceptedError(str(error)) from error
        callback_kind = self._admission_kind(event)
        if callback_kind is None:
            return
        _ADMITTED_OBLIGATION.set(None)
        try:
            obligation = await self.persist(room, event, callback_kind, provenance)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise nio.CallbackNotAcceptedError(str(error)) from error
        _ADMITTED_OBLIGATION.set(obligation)

    def _admission_kind(self, event: nio.Event) -> DispatchCallbackKind | None:
        """Return the one durable callback kind owned by a timeline event."""
        for policy in SOURCE_CALLBACK_POLICIES:
            if policy.matches(event):
                return policy.callback_kind
        if isinstance(event, nio.RoomMemberEvent) and self.room_lifecycle_admission_enabled():
            return DispatchCallbackKind.ROOM_LIFECYCLE
        return None

    async def dispatch(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Persist exact work before invoking its fallible callback."""
        obligation = await self.persist(room, event, callback_kind)
        if obligation is None:
            return
        try:
            await self._run_persisted(obligation, room=room, event=event)
        except Exception:
            self._schedule_retry(obligation.key)
            raise

    async def dispatch_background(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        callback_kind: DispatchCallbackKind,
        *,
        owner: object,
    ) -> None:
        """Persist exact work before scheduling its fallible callback."""
        obligation = await self.persist(room, event, callback_kind)
        if obligation is None:
            return
        self._schedule_background_obligation(
            obligation,
            room=room,
            event=event,
            owner=owner,
        )

    async def persist(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        callback_kind: DispatchCallbackKind,
        provenance: nio.TimelineEventProvenance | None = None,
    ) -> DispatchObligation | None:
        """Persist exact work before its background task may be created."""
        try:
            obligation = self._obligation_for_event(room, event, callback_kind)
            admission = await self.source_admission(
                room.room_id,
                obligation.source_event_id,
                callback_kind,
                provenance,
            )
            if admission is not DispatchSourceAdmission.ACCEPTED:
                if self.on_source_rejected is not None:
                    await self.on_source_rejected(
                        room,
                        event,
                        callback_kind,
                        admission,
                    )
                return None
            if await self._settle_from_turn_store_if_owned(obligation):
                return None
            create_result = await run_blocking_until_complete(self.store.create_pending, obligation)
            if create_result is DispatchCreateResult.ALREADY_TERMINAL:
                persisted_obligation = None
            elif create_result is DispatchCreateResult.ALREADY_PENDING:
                persisted_obligation = await asyncio.to_thread(self.store.pending_for, obligation.key)
            else:
                persisted_obligation = None if await self._settle_from_turn_store_if_owned(obligation) else obligation
        except (asyncio.CancelledError, Exception):
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        return persisted_obligation

    def _obligation_for_event(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> DispatchObligation:
        event_source = dispatch_event_source(event)
        event_source_json = json.dumps(
            event_source,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return DispatchObligation(
            principal_id=self.store.principal_id,
            entity_name=self.store.entity_name,
            source_event_id=dispatch_source_event_id(
                room.room_id,
                event,
                callback_kind,
                event_source_json,
            ),
            callback_kind=callback_kind,
            room_id=room.room_id,
            event_source=event_source,
        )

    async def _run_admitted(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Execute one exact obligation previously accepted by nio admission."""
        key = self._obligation_for_event(room, event, callback_kind).key
        try:
            obligation = await asyncio.to_thread(self.store.pending_for, key)
            if obligation is None:
                return
            await self._run_persisted(obligation, room=room, event=event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._schedule_retry(key)
            raise

    def _schedule_background_obligation(
        self,
        obligation: DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        owner: object,
    ) -> None:
        """Schedule one exact accepted payload without reloading it from SQLite."""
        create_background_task(
            self._run_background_obligation(obligation, room=room, event=event),
            owner=owner,
        )

    async def _run_background_obligation(
        self,
        obligation: DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> None:
        try:
            await self._run_persisted(obligation, room=room, event=event)
        except asyncio.CancelledError:
            return
        except Exception:
            self._schedule_retry(obligation.key)
            logger.exception(
                "dispatch_obligation_callback_failed",
                source_event_id=obligation.source_event_id,
                callback_kind=obligation.callback_kind.value,
                room_id=obligation.room_id,
            )

    async def _run_persisted(
        self,
        obligation: DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> None:
        """Execute work whose exact durable obligation already exists."""
        if room.room_id != obligation.room_id or dispatch_event_source(event) != obligation.event_source:
            room = self.room_for_id(obligation.room_id)
            event = parse_recovery_event(obligation)
        if obligation.requires_pending_check:
            with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                await self._run_obligation(obligation, room=room, event=event)
            return
        await self._run_obligation(obligation, room=room, event=event)

    async def recover_pending(self, *, turn_backed: bool | None = None) -> None:
        """Retry every valid pending callback without waiting for another sync response."""
        failed_keys: list[DispatchObligationKey] = []
        for obligation in await asyncio.to_thread(self.store.pending):
            if turn_backed is not None and (obligation.callback_kind in _TURN_BACKED_KINDS) != turn_backed:
                continue
            try:
                event = parse_recovery_event(obligation)
                room = self.room_for_id(obligation.room_id)
                with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                    await self._run_obligation(
                        obligation,
                        room=room,
                        event=event,
                    )
            except asyncio.CancelledError:
                raise
            except DispatchObligationCorruptionError:
                logger.error(  # noqa: TRY400
                    "dispatch_obligation_recovery_corrupt",
                    source_event_id=obligation.source_event_id,
                    callback_kind=obligation.callback_kind.value,
                    room_id=obligation.room_id,
                )
            except Exception:
                logger.exception(
                    "dispatch_obligation_recovery_failed",
                    source_event_id=obligation.source_event_id,
                    callback_kind=obligation.callback_kind.value,
                    room_id=obligation.room_id,
                )
                failed_keys.append(obligation.key)
        for key in failed_keys:
            self._schedule_retry(key)

    def _schedule_retry(self, key: DispatchObligationKey) -> None:
        """Ensure one failed exact callback remains autonomously retry-owned."""
        if key in self._retry_corrupt:
            return
        self._retry_keys.setdefault(key, 0)
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_task = create_background_task(
            self._retry_failed_obligations(),
            name=f"retry_dispatch_obligations_{self.store.entity_name}",
            owner=self.background_task_owner,
        )

    async def _drop_settled_retry_keys(self) -> None:
        """Remove retry keys whose durable obligation is no longer pending."""
        for key in tuple(self._retry_keys):
            try:
                is_pending = await asyncio.to_thread(
                    self.store.has_pending,
                    key.source_event_id,
                    key.callback_kind,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "dispatch_obligation_retry_discovery_failed",
                    source_event_id=key.source_event_id,
                    callback_kind=key.callback_kind.value,
                )
                continue
            if not is_pending:
                self._retry_keys.pop(key, None)

    async def _retry_failed_obligations(self) -> None:
        """Retry only callback failures, with one capped-backoff task per runner."""
        retry_delay_seconds = self._retry_initial_delay_seconds
        try:
            while self._retry_keys:
                await self._drop_settled_retry_keys()
                if not self._retry_keys:
                    break
                await asyncio.sleep(retry_delay_seconds)
                for key in tuple(self._retry_keys):
                    completed_attempts = self._retry_keys.pop(key)
                    try:
                        obligation = await asyncio.to_thread(self.store.pending_for, key)
                        if obligation is None:
                            continue
                        event = parse_recovery_event(obligation)
                        with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                            claimed = await self._run_obligation(
                                obligation,
                                room=self.room_for_id(obligation.room_id),
                                event=event,
                            )
                            if not claimed:
                                self._retry_keys.setdefault(key, completed_attempts)
                    except asyncio.CancelledError:
                        raise
                    except DispatchObligationCorruptionError:
                        self._retry_corrupt.add(key)
                        logger.exception(
                            "dispatch_obligation_retry_corrupt",
                            source_event_id=key.source_event_id,
                            callback_kind=key.callback_kind.value,
                        )
                    except Exception:
                        completed_attempts += 1
                        self._retry_keys.setdefault(key, completed_attempts)
                        logger.exception(
                            "dispatch_obligation_retry_failed",
                            source_event_id=key.source_event_id,
                            callback_kind=key.callback_kind.value,
                            retry_attempt=completed_attempts,
                        )
                retry_delay_seconds = min(
                    retry_delay_seconds * 2,
                    self._retry_max_delay_seconds,
                )
        finally:
            self._retry_task = None

    async def _run_obligation(
        self,
        obligation: DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> bool:
        """Run one obligation, returning whether this caller acquired its live claim."""
        if not await self._claim(obligation.key):
            return False
        try:
            if obligation.requires_pending_check and not await asyncio.to_thread(
                self.store.has_pending,
                obligation.source_event_id,
                obligation.callback_kind,
            ):
                return True
            if obligation.callback_completed:
                if await self._settle_from_turn_store_if_owned(obligation):
                    return True
                callback_reclaimed = await run_blocking_until_complete(
                    self.store.mark_callback_pending,
                    obligation.key,
                )
                if not callback_reclaimed:
                    return True
                obligation = replace(obligation, callback_completed=False, requires_pending_check=False)
            callback = self.callbacks.get(obligation.callback_kind)
            if callback is None:
                msg = f"No callback registered for {obligation.callback_kind.value!r}"
                raise RuntimeError(msg)
            running_token = _RUNNING_OBLIGATION.set(obligation)
            try:
                callback_result = await callback(room, event)
            finally:
                _RUNNING_OBLIGATION.reset(running_token)
            await self._settle_callback_result(obligation, callback_result)
            return True
        finally:
            await self._release(obligation.key)

    async def _settle_from_turn_store_if_owned(self, obligation: DispatchObligation) -> bool:
        if obligation.callback_kind not in _TURN_BACKED_KINDS:
            return False
        if not await asyncio.to_thread(self.turn_is_terminal, obligation.source_event_id):
            return False
        await run_blocking_until_complete(
            self.store.settle_from_turn_store,
            obligation.source_event_id,
            obligation.callback_kind,
        )
        return True

    async def _settle_callback_result(
        self,
        obligation: DispatchObligation,
        result: DispatchCallbackResult,
    ) -> None:
        if not isinstance(result, DispatchCallbackResult):
            msg = f"Dispatch callback returned invalid result {result!r}"
            raise TypeError(msg)
        if await self._settle_from_turn_store_if_owned(obligation):
            return
        if result is DispatchCallbackResult.DEFERRED:
            await run_blocking_until_complete(self.store.mark_callback_deferred, obligation.key)
            await self._settle_from_turn_store_if_owned(obligation)
            return
        if obligation.callback_kind is DispatchCallbackKind.INVITE:
            await run_blocking_until_complete(self.store.discard_pending, obligation.key)
            return
        outcome = (
            DispatchTerminalOutcome.SUCCEEDED
            if result is DispatchCallbackResult.SUCCEEDED
            else DispatchTerminalOutcome.INTENTIONALLY_IGNORED
        )
        await run_blocking_until_complete(self.store.settle, obligation.key, outcome)

    async def _claim(self, key: DispatchObligationKey) -> bool:
        async with self._active_lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    async def _release(self, key: DispatchObligationKey) -> None:
        async with self._active_lock:
            self._active.discard(key)


@dataclass(frozen=True, slots=True)
class _DispatchObligationTaskWrapper:
    """Schedule execution only after nio admits every matching callback."""

    runner: DispatchObligationRunner
    callback_kind: DispatchCallbackKind
    owner: object

    async def __call__(self, room: nio.MatrixRoom, event: DispatchEvent) -> None:
        """Schedule already-persisted work without repeating durable admission."""
        key = self.runner._obligation_for_event(room, event, self.callback_kind).key
        obligation = _ADMITTED_OBLIGATION.get()
        _ADMITTED_OBLIGATION.set(None)
        if obligation is not None and obligation.key == key:
            self.runner._schedule_background_obligation(
                obligation,
                room=room,
                event=event,
                owner=self.owner,
            )
            return
        create_background_task(
            self._run(room=room, event=event),
            owner=self.owner,
        )

    async def _run(
        self,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> None:
        try:
            await self.runner._run_admitted(
                room,
                event,
                self.callback_kind,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "dispatch_obligation_callback_failed",
                source_event_id=self.runner._obligation_for_event(
                    room,
                    event,
                    self.callback_kind,
                ).source_event_id,
                callback_kind=self.callback_kind.value,
                room_id=room.room_id,
            )
