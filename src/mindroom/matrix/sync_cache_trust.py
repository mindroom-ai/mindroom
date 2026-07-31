"""Own Matrix sync-checkpoint persistence and event-cache trust."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypeVar

from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCertificationDecision,
    SyncCheckpoint,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    sync_cache_write_diagnostics,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore

_ContinuityResult = TypeVar("_ContinuityResult")


async def _run_continuity_operation(
    operation: Callable[..., _ContinuityResult],
    *args: object,
    **kwargs: object,
) -> _ContinuityResult:
    """Finish one continuity operation off-loop before propagating cancellation."""
    worker_task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker_task)
    except asyncio.CancelledError as cancellation:
        worker_error: Exception | None = None
        while not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                worker_error = exc
                break
        if worker_error is None:
            try:
                worker_task.result()
            except Exception as exc:
                worker_error = exc
        if worker_error is not None:
            raise cancellation from worker_error
        raise


@dataclass
class SyncCacheTrust:
    """Own one bot's cache-certified sync continuity."""

    continuity_store: SyncContinuityStore
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None
    _awaiting_initial_window: bool = field(default=False, init=False, repr=False)
    _cache_scope_epoch: int = field(default=0, init=False, repr=False)
    _saved_checkpoint: SyncCheckpoint | None = field(default=None, init=False, repr=False)

    async def prepare_startup(self) -> str | None:
        """Initialize cache trust, then restore a valid checkpoint or start cold."""
        cache = self.runtime.event_cache
        try:
            await cache.initialize()
        except Exception as exc:
            self.logger.warning("matrix_principal_event_cache_init_failed", error=str(exc))

        try:
            record = await _run_continuity_operation(self.continuity_store.load)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            record = None
        self._saved_checkpoint = None if record is None else record.checkpoint
        loaded = self._load_valid_checkpoint(self._saved_checkpoint)
        if loaded is None and await self.invalidate_for_cache_scope_cleanup():
            try:
                await cache.purge_principal()
            except Exception as exc:
                cache.disable("untrusted_principal_cache_cleanup_failed")
                self.logger.warning("matrix_untrusted_principal_cache_disabled", error=str(exc))

        self.state = SyncTrustState.PENDING if loaded is not None else SyncTrustState.COLD
        self.checkpoint = None
        self._awaiting_initial_window = loaded is None
        return loaded.token if loaded is not None else None

    def _load_valid_checkpoint(self, checkpoint: SyncCheckpoint | None) -> SyncCheckpoint | None:
        """Accept a loaded checkpoint only when current cache generation proves it."""
        if checkpoint is None:
            return None

        cache_generation = self.runtime.event_cache.cache_generation
        if cache_generation is None:
            self.logger.warning("matrix_sync_token_cache_generation_unavailable")
            return None
        if checkpoint.cache_generation != cache_generation:
            self.logger.warning("matrix_sync_token_cache_generation_mismatch")
            return None
        self.logger.info("matrix_sync_token_restored", certified=True)
        return checkpoint

    async def save(self, checkpoint: SyncCheckpoint) -> SyncContinuityRecord:
        """Persist one checkpoint against the current durable cache generation."""
        cache_generation = self.runtime.event_cache.cache_generation
        if cache_generation is None:
            msg = "Cannot persist Matrix sync continuity without a cache generation"
            raise RuntimeError(msg)
        record = await _run_continuity_operation(
            self.continuity_store.replace_checkpoint,
            SyncCheckpoint(token=checkpoint.token, cache_generation=cache_generation),
        )
        self._saved_checkpoint = record.checkpoint
        return record

    async def _clear_saved(self) -> bool:
        """Clear the durable checkpoint, returning whether invalidation succeeded."""
        self._saved_checkpoint = None
        try:
            await _run_continuity_operation(self.continuity_store.replace_checkpoint, None)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_clear_failed", error=str(exc))
            return False
        return True

    async def invalidate_for_cache_scope_cleanup(self) -> bool:
        """Invalidate continuity before principal- or room-owned rows are removed."""
        self._cache_scope_epoch += 1
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        if await self._clear_saved():
            return True
        self.runtime.event_cache.disable("sync_checkpoint_clear_failed")
        self.logger.warning("matrix_cache_scope_cleanup_checkpoint_clear_failed")
        return False

    async def certify_response(
        self,
        *,
        next_batch: str | None,
        cache_result: SyncCacheWriteResult,
        first_sync: bool,
    ) -> SyncCertificationDecision:
        """Apply the certification decision for one completed sync response."""
        decision = self.plan_response(
            next_batch=next_batch,
            cache_result=cache_result,
            first_sync=first_sync,
        )
        applied, _record = await self.apply_response(decision, cache_result=cache_result)
        return applied

    def plan_response(
        self,
        *,
        next_batch: str | None,
        cache_result: SyncCacheWriteResult,
        first_sync: bool,
    ) -> SyncCertificationDecision:
        """Plan certification without advancing runtime or durable continuity."""
        decision = certify_sync_response(
            self.state,
            next_batch=next_batch,
            cache_result=cache_result,
            first_sync=first_sync,
        )
        limited_timeline = bool(cache_result.limited_room_ids)
        if limited_timeline and not self._awaiting_initial_window:
            decision = replace(decision, reset_client_token=True)
        return replace(decision, cache_scope_epoch=self._cache_scope_epoch)

    async def apply_response(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult,
        joined_room_ids: Iterable[str] = (),
    ) -> tuple[SyncCertificationDecision, SyncContinuityRecord | None]:
        """Apply a planned response after its prerequisite durable work completes."""
        if decision.cache_scope_epoch != self._cache_scope_epoch:
            decision = SyncCertificationDecision(
                state=SyncTrustState.UNCERTAIN,
                clear_saved_token=True,
                reset_client_token=True,
                reason="cache_scope_invalidated",
                cache_scope_epoch=self._cache_scope_epoch,
            )
        record = await self._apply_decision(
            decision,
            cache_result=cache_result,
            joined_room_ids=joined_room_ids,
        )
        # Re-arm from applied trust so a replaced stale-scope decision cannot
        # license another since-less replay.
        if decision.reset_client_token:
            self._awaiting_initial_window = True
        elif self.state is SyncTrustState.CERTIFIED:
            self._awaiting_initial_window = False
        return decision, record

    async def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""
        decision = handle_unknown_pos()
        await self._apply_decision(decision, force_clear=True)
        self._awaiting_initial_window = True
        return decision

    async def _apply_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
        joined_room_ids: Iterable[str] = (),
        force_clear: bool = False,
    ) -> SyncContinuityRecord | None:
        """Apply one certifier decision to trust state and durable storage."""
        if decision.checkpoint_to_save is not None:
            cache_generation = self.runtime.event_cache.cache_generation
            if cache_generation is None:
                msg = "Cannot certify Matrix sync continuity without a cache generation"
                raise RuntimeError(msg)
            record = await _run_continuity_operation(
                self.continuity_store.accept_classic_response,
                SyncCheckpoint(
                    token=decision.checkpoint_to_save.token,
                    cache_generation=cache_generation,
                ),
                joined_room_ids=joined_room_ids,
            )
            self._saved_checkpoint = record.checkpoint
        elif decision.clear_saved_token:
            if self._saved_checkpoint is None and not force_clear:
                record = None
            else:
                record = await _run_continuity_operation(
                    self.continuity_store.replace_checkpoint,
                    None,
                )
            self._saved_checkpoint = None
        else:
            record = None
        self.state = decision.state
        self.checkpoint = decision.checkpoint_to_save
        if decision.reason is not None:
            diagnostics = sync_cache_write_diagnostics(cache_result) if cache_result is not None else {}
            self.logger.warning("matrix_sync_certification_uncertain", reason=decision.reason, **diagnostics)
        return record

    async def persist_current(self) -> None:
        """Persist the current certified checkpoint."""
        assert self.state is SyncTrustState.CERTIFIED
        assert self.checkpoint is not None
        await self.save(self.checkpoint)

    async def discard(self) -> None:
        """Discard runtime and durable checkpoint trust."""
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        await self._clear_saved()

    def retry_token(self) -> str | None:
        """Return the generation-safe checkpoint for work rejected before durability."""
        if self.checkpoint is not None:
            return self.checkpoint.token
        saved = self._saved_checkpoint
        cache_generation = self.runtime.event_cache.cache_generation
        if saved is None or cache_generation is None or saved.cache_generation != cache_generation:
            return None
        return saved.token
