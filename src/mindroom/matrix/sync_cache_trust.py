"""Own Matrix sync-checkpoint persistence and event-cache trust."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCertificationDecision,
    SyncCheckpoint,
    SyncTrustState,
    certify_sync_response,
    handle_unknown_pos,
    sync_cache_write_diagnostics,
)
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


@dataclass
class SyncCacheTrust:
    """Own one bot's cache-certified sync continuity."""

    storage_path: Path
    agent_name: str
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None
    _cache_scope_epoch: int = field(default=0, init=False, repr=False)
    _dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)
    _observed_dispatch_persist_failure_epoch: int = field(default=0, init=False, repr=False)

    async def prepare_startup(self) -> str | None:
        """Initialize cache trust, then restore a valid checkpoint or start cold."""
        cache = self.runtime.event_cache
        try:
            await cache.initialize()
        except Exception as exc:
            self.logger.warning("matrix_principal_event_cache_init_failed", error=str(exc))

        loaded = self._load_valid_checkpoint()
        if loaded is None and self.invalidate_for_cache_scope_cleanup():
            try:
                await cache.purge_principal()
            except Exception as exc:
                cache.disable("untrusted_principal_cache_cleanup_failed")
                self.logger.warning("matrix_untrusted_principal_cache_disabled", error=str(exc))

        self.state = SyncTrustState.PENDING if loaded is not None else SyncTrustState.COLD
        self.checkpoint = None
        return loaded.token if loaded is not None else None

    def _load_valid_checkpoint(self) -> SyncCheckpoint | None:
        """Load a checkpoint only when the current cache generation proves it."""
        try:
            checkpoint = load_sync_checkpoint(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            return None
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

    def save(self, checkpoint: SyncCheckpoint) -> None:
        """Persist one checkpoint against the current durable cache generation."""
        cache_generation = self.runtime.event_cache.cache_generation
        if cache_generation is None:
            self.logger.warning("matrix_sync_checkpoint_skipped_without_cache_generation")
            self._clear_saved()
            return
        try:
            save_sync_token(self.storage_path, self.agent_name, checkpoint.token, cache_generation=cache_generation)
        except (OSError, ValueError) as exc:
            self.logger.warning("matrix_sync_token_save_failed", error=str(exc))

    def _clear_saved(self) -> bool:
        """Clear the durable checkpoint, returning whether invalidation succeeded."""
        try:
            clear_sync_token(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_clear_failed", error=str(exc))
            return False
        return True

    def invalidate_for_cache_scope_cleanup(self) -> bool:
        """Invalidate continuity before principal- or room-owned rows are removed."""
        self._cache_scope_epoch += 1
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        if self._clear_saved():
            return True
        self.runtime.event_cache.disable("sync_checkpoint_clear_failed")
        self.logger.warning("matrix_cache_scope_cleanup_checkpoint_clear_failed")
        return False

    def record_dispatch_persist_failure(self) -> None:
        """Latch one source callback rejected before durable ownership."""
        self._dispatch_persist_failure_epoch += 1

    def _consume_dispatch_persist_failure(self) -> bool:
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

    def reject_response_before_certification(self) -> None:
        """Consume any admission failure owned by an aborted sync response."""
        self._consume_dispatch_persist_failure()

    def apply_response_after_dispatch_acceptance(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult,
    ) -> tuple[SyncCertificationDecision, bool]:
        """Apply a plan only when its source callbacks all reached durable ownership."""
        if self._consume_dispatch_persist_failure():
            return decision, True
        return self._apply_response(decision, cache_result=cache_result), False

    def certify_response(
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
        return self._apply_response(decision, cache_result=cache_result)

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

    def _apply_response(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult,
    ) -> SyncCertificationDecision:
        """Apply a planned response after its prerequisite durable work completes."""
        if decision.cache_scope_epoch != self._cache_scope_epoch:
            decision = SyncCertificationDecision(
                state=SyncTrustState.UNCERTAIN,
                clear_saved_token=True,
                reset_client_token=True,
                reason="cache_scope_invalidated",
                cache_scope_epoch=self._cache_scope_epoch,
            )
        self._apply_decision(decision, cache_result=cache_result)
        return decision

    def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""
        decision = handle_unknown_pos()
        self._apply_decision(decision)
        return decision

    def _apply_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
    ) -> None:
        """Apply one certifier decision to trust state and durable storage."""
        self.state = decision.state
        self.checkpoint = decision.checkpoint_to_save
        if decision.clear_saved_token:
            self._clear_saved()
        if decision.checkpoint_to_save is not None:
            self.save(decision.checkpoint_to_save)
        if decision.reason is not None:
            diagnostics = sync_cache_write_diagnostics(cache_result) if cache_result is not None else {}
            self.logger.warning("matrix_sync_certification_uncertain", reason=decision.reason, **diagnostics)

    def persist_current(self) -> None:
        """Persist the current certified checkpoint."""
        assert self.state is SyncTrustState.CERTIFIED
        assert self.checkpoint is not None
        self.save(self.checkpoint)

    def discard(self) -> None:
        """Discard runtime and durable checkpoint trust."""
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        self._clear_saved()

    def retry_token(self) -> str | None:
        """Return the generation-safe checkpoint for work rejected before durability."""
        if self.checkpoint is not None:
            return self.checkpoint.token
        try:
            saved = load_sync_checkpoint(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            return None
        cache_generation = self.runtime.event_cache.cache_generation
        if saved is None or cache_generation is None or saved.cache_generation != cache_generation:
            return None
        return saved.token
