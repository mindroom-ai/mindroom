"""Own Matrix sync-checkpoint persistence and event-cache trust."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
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
    from collections.abc import Iterable

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore


@dataclass
class SyncCacheTrust:
    """Own one bot's cache-certified sync continuity."""

    continuity_store: SyncContinuityStore
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None
    _cache_scope_epoch: int = field(default=0, init=False, repr=False)
    _saved_checkpoint: SyncCheckpoint | None = field(default=None, init=False, repr=False)
    _mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)
    _observed_dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)

    async def prepare_startup(self) -> str | None:
        """Initialize cache trust, then restore a valid checkpoint or start cold."""
        cache = self.runtime.event_cache
        try:
            await cache.initialize()
        except Exception as exc:
            self.logger.warning("matrix_principal_event_cache_init_failed", error=str(exc))

        try:
            record = await run_blocking_until_complete(self.continuity_store.load)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            record = None
        except RuntimeError as exc:
            self.logger.warning("matrix_sync_continuity_invalid", error=str(exc))
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

    async def _persist_checkpoint_locked(
        self,
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: Iterable[str] | None = None,
    ) -> SyncContinuityRecord | None:
        """Persist one checkpoint while the mutation lock owns publication order."""
        cache_generation = self.runtime.event_cache.cache_generation
        if cache_generation is None:
            return None
        durable_checkpoint = SyncCheckpoint(
            token=checkpoint.token,
            cache_generation=cache_generation,
        )
        if joined_room_ids is None:
            record = await run_blocking_until_complete(
                self.continuity_store.replace_checkpoint,
                durable_checkpoint,
            )
        else:
            record = await run_blocking_until_complete(
                partial(
                    self.continuity_store.accept_classic_response,
                    joined_room_ids=joined_room_ids,
                ),
                durable_checkpoint,
            )
        self._saved_checkpoint = record.checkpoint
        return record

    async def _clear_saved_locked(self) -> bool:
        """Clear durable checkpoint while the mutation lock owns publication order."""
        self._saved_checkpoint = None
        try:
            await run_blocking_until_complete(self.continuity_store.clear_checkpoint)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_clear_failed", error=str(exc))
            return False
        return True

    async def invalidate_for_cache_scope_cleanup(self) -> bool:
        """Invalidate continuity before principal- or room-owned rows are removed."""

        async def invalidate() -> bool:
            async with self._mutation_lock:
                self._cache_scope_epoch += 1
                self.state = SyncTrustState.UNCERTAIN
                self.checkpoint = None
                if await self._clear_saved_locked():
                    return True
                self.runtime.event_cache.disable("sync_checkpoint_clear_failed")
                self.logger.warning("matrix_cache_scope_cleanup_checkpoint_clear_failed")
                return False

        return await run_coroutine_until_complete(invalidate())

    def record_dispatch_persist_failure(self) -> None:
        """Latch one source callback rejected before durable ownership."""
        self._dispatch_persist_failure_epoch += 1

    def consume_dispatch_persist_failure(self) -> bool:
        """Reject certification once for every newly observed failure epoch."""
        failure_epoch = self._dispatch_persist_failure_epoch
        if failure_epoch == self._observed_dispatch_persist_failure_epoch:
            return False
        self._observed_dispatch_persist_failure_epoch = failure_epoch
        self.logger.warning(
            "matrix_sync_certification_rejected_after_dispatch_persist_failure",
            dispatch_persist_failure_epoch=failure_epoch,
        )
        return True

    def acknowledge_dispatch_persist_failures(self) -> None:
        """Settle source failures irrelevant to non-checkpointed transports."""
        self._observed_dispatch_persist_failure_epoch = self._dispatch_persist_failure_epoch

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
        return replace(decision, cache_scope_epoch=self._cache_scope_epoch)

    async def apply_response(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult,
        joined_room_ids: Iterable[str] = (),
    ) -> tuple[SyncCertificationDecision, SyncContinuityRecord | None]:
        """Apply a planned response after its prerequisite durable work completes."""

        async def apply() -> tuple[SyncCertificationDecision, SyncContinuityRecord | None]:
            nonlocal decision
            async with self._mutation_lock:
                if decision.cache_scope_epoch != self._cache_scope_epoch:
                    decision = SyncCertificationDecision(
                        state=SyncTrustState.UNCERTAIN,
                        clear_saved_token=True,
                        reset_client_token=True,
                        reason="cache_scope_invalidated",
                        cache_scope_epoch=self._cache_scope_epoch,
                    )
                record = await self._apply_decision_locked(
                    decision,
                    cache_result=cache_result,
                    joined_room_ids=joined_room_ids,
                )
                return decision, record

        return await run_coroutine_until_complete(apply())

    async def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""

        async def reject() -> SyncCertificationDecision:
            async with self._mutation_lock:
                decision = handle_unknown_pos()
                await self._apply_decision_locked(decision)
                return decision

        return await run_coroutine_until_complete(reject())

    async def _apply_decision_locked(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
        joined_room_ids: Iterable[str] = (),
    ) -> SyncContinuityRecord | None:
        """Apply one certifier decision while mutation order is serialized."""
        if decision.checkpoint_to_save is not None:
            record = await self._persist_checkpoint_locked(
                decision.checkpoint_to_save,
                joined_room_ids=joined_room_ids,
            )
            if record is None:
                msg = "Cannot certify Matrix sync continuity without a cache generation"
                raise RuntimeError(msg)
        elif decision.clear_saved_token:
            # Fail runtime closed before awaiting the durable fresh-read transform.
            # Cancellation may propagate only after that worker commits, so no
            # stale runtime checkpoint may survive long enough to be re-persisted.
            self.state = decision.state
            self.checkpoint = None
            self._saved_checkpoint = None
            if not await self._clear_saved_locked():
                self.runtime.event_cache.disable("sync_checkpoint_clear_failed")
            record = None
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

        async def persist() -> None:
            async with self._mutation_lock:
                if self.state is not SyncTrustState.CERTIFIED or self.checkpoint is None:
                    return
                record = await self._persist_checkpoint_locked(self.checkpoint)
                if record is not None:
                    return
                self.state = SyncTrustState.UNCERTAIN
                self.checkpoint = None
                self.logger.warning("matrix_sync_checkpoint_skipped_without_cache_generation")
                await self._clear_saved_locked()

        await run_coroutine_until_complete(persist())

    def retry_token(self) -> str | None:
        """Return the generation-safe checkpoint for work rejected before durability."""
        if self.checkpoint is not None:
            return self.checkpoint.token
        saved = self._saved_checkpoint
        cache_generation = self.runtime.event_cache.cache_generation
        if saved is None or cache_generation is None or saved.cache_generation != cache_generation:
            return None
        return saved.token
