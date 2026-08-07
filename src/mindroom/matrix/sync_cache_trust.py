"""Own Matrix sync-checkpoint persistence and event-cache trust."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.matrix.sync_certification import (
    SyncCertificationDecision,
    SyncRecoveryOutcome,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    sync_recovery_diagnostics,
)
from mindroom.matrix.sync_recovery_escape import SkippedRecoveryGap, SyncRecoveryStallTracker
from mindroom.matrix.sync_token_values import SyncCheckpoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore


@dataclass
class SyncCacheTrust:
    """Own one bot's cache-certified sync continuity."""

    continuity_store: SyncContinuityStore
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    # Resolves the event journal's identity. A sync token only means something
    # beside the store that consumed the events it covers, and the journal is
    # that store now -- the cache generation this replaced certified a database
    # nothing reads for conversation history any more.
    #
    # A provider rather than a value, resolved on demand and memoized. Reading
    # it once during startup would mean anything that reaches certification by
    # another route -- and several things do -- silently had no generation and
    # refused every checkpoint.
    store_generation_provider: Callable[[], Awaitable[str | None]] | None = None
    store_generation: str | None = None
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None
    _tokenless_baseline_pending: bool = field(default=False, init=False, repr=False)
    _cache_scope_epoch: int = field(default=0, init=False, repr=False)
    _saved_checkpoint: SyncCheckpoint | None = field(default=None, init=False, repr=False)
    _mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _recovery_stalls: SyncRecoveryStallTracker = field(
        default_factory=SyncRecoveryStallTracker,
        init=False,
        repr=False,
    )
    _dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)
    _observed_dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)

    async def _resolve_store_generation(self) -> str | None:
        """Return the journal's identity, asking for it the first time it is needed."""
        if self.store_generation is None and self.store_generation_provider is not None:
            self.store_generation = await self.store_generation_provider()
        return self.store_generation

    async def prepare_startup(
        self,
    ) -> str | None:
        """Initialize cache trust and choose the safe startup transport position."""
        await self._resolve_store_generation()
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

        self.checkpoint = None
        safe_token = loaded.token if loaded is not None else None
        self.state = SyncTrustState.PENDING if safe_token is not None else SyncTrustState.COLD
        self._refresh_tokenless_baseline_pending()
        return safe_token

    def _load_valid_checkpoint(self, checkpoint: SyncCheckpoint | None) -> SyncCheckpoint | None:
        """Accept a loaded checkpoint only when current cache generation proves it."""
        if checkpoint is None:
            return None

        cache_generation = self.store_generation
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
        cache_generation = self.store_generation
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

    def _dispatch_persist_failed(self) -> bool:
        """Return whether a refused admission is outstanding, without consuming it.

        Planning a response has to know this, and planning happens before the
        gate that acts on it. Consuming here would mark the failure observed and
        leave that gate looking at a clean epoch -- certifying the exact response
        the failure exists to reject.
        """
        return self._dispatch_persist_failure_epoch != self._observed_dispatch_persist_failure_epoch

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

    def reject_response_before_certification(self) -> bool:
        """Consume and report any admission failure owned by an aborted response."""
        admission_failed = self.consume_dispatch_persist_failure()
        self._refresh_tokenless_baseline_pending()
        return admission_failed

    def _refresh_tokenless_baseline_pending(self) -> None:
        """Permit a positioning baseline exactly when no safe retry cursor exists."""
        self._tokenless_baseline_pending = self.retry_token() is None

    def tokenless_baseline_pending(self) -> bool:
        """Return whether the next accepted response establishes a tokenless baseline."""
        return self._tokenless_baseline_pending

    def acknowledge_dispatch_persist_failures(self) -> None:
        """Settle source failures irrelevant to non-checkpointed transports."""
        self._observed_dispatch_persist_failure_epoch = self._dispatch_persist_failure_epoch

    def observed_recovery(
        self,
        response: nio.SyncResponse | nio.SlidingSyncResponse,
    ) -> SyncRecoveryOutcome:
        """Return what this response settled about durable ownership of its events.

        The single constructor callers use, so that ``admission_refused`` can
        only ever come from the non-consuming peek. Building the outcome at a
        call site would let one be built from ``consume_dispatch_persist_failure``
        instead, which marks the failure observed and disarms the gate that acts
        on it.
        """
        return SyncRecoveryOutcome.from_sync_response(
            response,
            admission_refused=self._dispatch_persist_failed(),
        )

    def plan_response(
        self,
        *,
        next_batch: str | None,
        recovery: SyncRecoveryOutcome,
    ) -> SyncCertificationDecision:
        """Plan certification without advancing runtime or durable continuity."""
        skipped = self._observe_recovery_stalls(recovery)
        decision = certify_sync_response(
            next_batch=next_batch,
            recovery=recovery,
            skipped_recovery_room_ids=frozenset(gap.room_id for gap in skipped),
        )
        if skipped and decision.checkpoint_to_save is not None:
            self._report_skipped_recovery_gaps(skipped, skipped_to_token=decision.checkpoint_to_save.token)
        return replace(decision, cache_scope_epoch=self._cache_scope_epoch)

    def _observe_recovery_stalls(self, recovery: SyncRecoveryOutcome) -> tuple[SkippedRecoveryGap, ...]:
        """Return rooms whose rebuild has stopped converging from this checkpoint."""
        if not recovery.recovery_conclusive:
            return ()
        return self._recovery_stalls.observe(
            unrecovered_room_ids=recovery.unrecovered_room_ids,
            checkpoint_token=self.retry_token(),
        )

    def _report_skipped_recovery_gaps(
        self,
        skipped: tuple[SkippedRecoveryGap, ...],
        *,
        skipped_to_token: str,
    ) -> None:
        """Announce every gap this principal gave up on, loudly and by name."""
        for gap in skipped:
            self.logger.error(
                "matrix_sync_recovery_gap_skipped_after_stalled_rebuild",
                room_id=gap.room_id,
                failed_attempts=gap.failed_attempts,
                skipped_from_token=gap.skipped_from_token,
                skipped_to_token=skipped_to_token,
            )

    async def apply_response(
        self,
        decision: SyncCertificationDecision,
        *,
        recovery: SyncRecoveryOutcome,
        joined_room_ids: Iterable[str] = (),
        publish_record: Callable[[SyncContinuityRecord], None] | None = None,
    ) -> SyncCertificationDecision:
        """Apply a planned response after its prerequisite durable work completes.

        ``publish_record`` must stay synchronous and non-blocking because it
        runs under the mutation lock before cancellation may escape.
        """

        async def apply() -> SyncCertificationDecision:
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
                    recovery=recovery,
                    joined_room_ids=joined_room_ids,
                )
                if record is not None and publish_record is not None:
                    publish_record(record)
                if decision.reset_client_token:
                    self._refresh_tokenless_baseline_pending()
                else:
                    self._tokenless_baseline_pending = False
                return decision

        return await run_coroutine_until_complete(apply())

    async def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""

        async def reject() -> SyncCertificationDecision:
            async with self._mutation_lock:
                decision = handle_unknown_pos()
                await self._apply_decision_locked(decision)
                self._refresh_tokenless_baseline_pending()
                return decision

        return await run_coroutine_until_complete(reject())

    async def _apply_decision_locked(
        self,
        decision: SyncCertificationDecision,
        *,
        recovery: SyncRecoveryOutcome | None = None,
        joined_room_ids: Iterable[str] = (),
    ) -> SyncContinuityRecord | None:
        """Apply one certifier decision while mutation order is serialized."""
        # Every path that can persist a checkpoint funnels through here, so this
        # is where the journal's identity has to be known -- resolving it only
        # in startup left anything reaching certification another way with no
        # generation, which reads as "refuse this checkpoint" rather than as
        # the wiring mistake it is.
        await self._resolve_store_generation()
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
            diagnostics = sync_recovery_diagnostics(recovery) if recovery is not None else {}
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
        cache_generation = self.store_generation
        if saved is None or cache_generation is None or saved.cache_generation != cache_generation:
            return None
        return saved.token
