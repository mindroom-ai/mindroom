"""Adapt current bot lifecycle calls to pre-campaign sync-cache trust."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import (
    SyncCacheWriteResult,
    SyncCertificationDecision,
    SyncCheckpoint,
    SyncTrustState,
    handle_unknown_pos,
    start_from_loaded_token,
    sync_cache_write_diagnostics,
)
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_token_record, save_sync_token

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


@dataclass
class SyncCacheTrust:
    """Expose current lifecycle methods with pre-campaign checkpoint semantics."""

    storage_path: Path
    agent_name: str
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None

    def restore_saved_token(self) -> str | None:
        """Restore the pre-campaign token record without initializing storage."""
        try:
            loaded = load_sync_token_record(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_load_failed", error=str(exc))
            loaded = None
        startup = start_from_loaded_token(
            loaded.checkpoint
            if loaded is not None and loaded.checkpoint is not None
            else loaded.token
            if loaded
            else None,
        )
        self.state = startup.state
        self.checkpoint = None
        if loaded is not None:
            self.logger.info("matrix_sync_token_restored", certified=loaded.certified)
        if startup.legacy_token:
            self.logger.warning("matrix_sync_token_uncertified_legacy")
        return startup.sync_token

    def save(self, checkpoint: SyncCheckpoint) -> None:
        """Persist one pre-campaign cache-certified checkpoint."""
        try:
            save_sync_token(self.storage_path, self.agent_name, checkpoint.token)
        except (OSError, ValueError) as exc:
            self.logger.warning("matrix_sync_token_save_failed", error=str(exc))

    def clear_saved(self) -> bool:
        """Clear the durable checkpoint, returning whether invalidation succeeded."""
        try:
            clear_sync_token(self.storage_path, self.agent_name)
        except OSError as exc:
            self.logger.warning("matrix_sync_token_clear_failed", error=str(exc))
            return False
        return True

    def invalidate_for_cache_scope_cleanup(self) -> bool:
        """Invalidate continuity without campaign-era principal cache cleanup."""
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        if self.clear_saved():
            return True
        self.runtime.mark_callback_failed()
        self.logger.warning("matrix_sync_checkpoint_clear_failed")
        return False

    def mark_callback_failed(self) -> None:
        """Poison sync continuity after a Matrix callback failure."""
        self.runtime.mark_callback_failed()
        self.invalidate_for_cache_scope_cleanup()

    def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""
        decision = handle_unknown_pos()
        self.apply_decision(decision)
        return decision

    def apply_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
    ) -> None:
        """Apply one certifier decision to trust state and durable storage."""
        callback_failure_count = self.runtime.callback_failure_count
        if callback_failure_count:
            self.state = SyncTrustState.UNCERTAIN
            self.checkpoint = None
            self.clear_saved()
            self.logger.warning(
                "matrix_sync_certification_uncertain",
                reason="callback_failed",
                callback_failure_count=callback_failure_count,
            )
            return

        self.state = decision.state
        self.checkpoint = decision.checkpoint_to_save
        if decision.clear_saved_token:
            self.clear_saved()
        if decision.checkpoint_to_save is not None:
            self.save(decision.checkpoint_to_save)
        if decision.reason is not None:
            diagnostics = sync_cache_write_diagnostics(cache_result) if cache_result is not None else {}
            self.logger.warning("matrix_sync_certification_uncertain", reason=decision.reason, **diagnostics)
