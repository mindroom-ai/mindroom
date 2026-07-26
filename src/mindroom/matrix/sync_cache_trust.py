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


@dataclass(frozen=True)
class SyncCallbackDispatch:
    """One dispatched Matrix callback, bound to the batch that can replay it."""

    sequence: int
    replay_token: str | None


@dataclass
class SyncCacheTrust:
    """Own one bot's cache-certified sync continuity."""

    storage_path: Path
    agent_name: str
    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    state: SyncTrustState = SyncTrustState.COLD
    checkpoint: SyncCheckpoint | None = None
    _awaiting_initial_window: bool = field(default=False, init=False, repr=False)
    # The last token whose sync delta reached durable cache. A callback failure
    # replays from here, because the batch it belongs to came after it.
    _certified_token: str | None = field(default=None, init=False, repr=False)
    _dispatch_sequence: int = field(default=0, init=False, repr=False)
    # Dispatch sequence -> the token its batch must be replayed from.
    _failed_dispatches: dict[int, str | None] = field(default_factory=dict, init=False, repr=False)
    # Lifetime failures this trust bound to a batch, to tell them apart from the
    # ones only the runtime counted.
    _attributed_failures: int = field(default=0, init=False, repr=False)
    # Callback tickets handed out and not yet resolved either way.
    _inflight_dispatches: set[int] = field(default_factory=set, init=False, repr=False)
    # Inclusive dispatch-sequence range covered by the replay currently in flight.
    _replay_window: tuple[int, int] | None = field(default=None, init=False, repr=False)
    # A replay whose delta is durable, still waiting on the callbacks it dispatched:
    # (replayed_from, requested_at, replayed_through).
    _pending_repair: tuple[int, int, int] | None = field(default=None, init=False, repr=False)
    # Set while cached rows have been dropped, so no old position may be resumed.
    _scope_invalidated: bool = field(default=False, init=False, repr=False)
    # Positions already rewound to. A position is never retried, so a callback
    # that fails on every delivery cannot hold the sync loop on one token.
    _replayed_positions: set[str | None] = field(default_factory=set, init=False, repr=False)

    async def prepare_startup(self) -> str | None:
        """Initialize cache trust, then restore a valid checkpoint or start cold."""
        cache = self.runtime.event_cache
        try:
            await cache.initialize()
        except Exception as exc:
            self.logger.warning("matrix_principal_event_cache_init_failed", error=str(exc))

        # Startup either proves a checkpoint against the live cache generation or
        # purges the untrusted rows, so no earlier batch is left unaccounted for.
        self._failed_dispatches.clear()
        self._replayed_positions.clear()
        self._inflight_dispatches.clear()
        self._replay_window = None
        self._pending_repair = None
        self._scope_invalidated = False
        self._attributed_failures = self.runtime.callback_failure_count

        loaded = self._load_valid_checkpoint()
        self._certified_token = loaded.token if loaded is not None else None
        if loaded is None and self.invalidate_for_cache_scope_cleanup():
            try:
                await cache.purge_principal()
            except Exception as exc:
                cache.disable("untrusted_principal_cache_cleanup_failed")
                self.logger.warning("matrix_untrusted_principal_cache_disabled", error=str(exc))

        self.state = SyncTrustState.PENDING if loaded is not None else SyncTrustState.COLD
        self.checkpoint = None
        self._awaiting_initial_window = loaded is None
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
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        # Rows are about to disappear, so no earlier position stays resumable and
        # a callback failure must not put one back until something recertifies.
        self._scope_invalidated = True
        if self._clear_saved():
            return True
        self._record_callback_failure(self.dispatch_callback())
        self.runtime.event_cache.disable("sync_checkpoint_clear_failed")
        self.logger.warning("matrix_cache_scope_cleanup_deferred_until_checkpoint_replay")
        return False

    def _replay_floor(self) -> str | None:
        """Return the newest position the cache is provably complete through."""
        if self.checkpoint is not None:
            return self.checkpoint.token
        return self._certified_token

    def dispatch_callback(self) -> SyncCallbackDispatch:
        """Bind a callback about to run to the checkpoint that replays its batch."""
        self._dispatch_sequence += 1
        self._inflight_dispatches.add(self._dispatch_sequence)
        return SyncCallbackDispatch(sequence=self._dispatch_sequence, replay_token=self._replay_floor())

    def mark_callback_finished(self, dispatch: SyncCallbackDispatch) -> None:
        """Release a callback ticket that ran to completion without poisoning trust."""
        self._inflight_dispatches.discard(dispatch.sequence)

    @property
    def outstanding_callback_failures(self) -> int:
        """Return failed callbacks whose sync batch has not been replayed yet.

        Failures reported straight to the runtime carry no sync position, so no
        replay can ever vouch for them and they keep continuity uncertain for
        the rest of the process.
        """
        unattributed = self.runtime.callback_failure_count - self._attributed_failures
        return len(self._failed_dispatches) + max(unattributed, 0)

    def mark_callback_failed(self, dispatch: SyncCallbackDispatch | None = None) -> None:
        """Poison sync continuity until the failed callback's batch is replayed.

        ``dispatch`` must be the ticket taken when the callback was handed the
        event.  Without it the failure is attributed to the current position,
        which is only correct for callbacks that cannot outlive their batch.
        """
        self._record_callback_failure(dispatch if dispatch is not None else self.dispatch_callback())
        self.state = SyncTrustState.UNCERTAIN
        self.checkpoint = None
        self._preserve_replay_floor()

    def _record_callback_failure(self, dispatch: SyncCallbackDispatch) -> None:
        """Track one failed callback against the batch that has to be re-delivered."""
        self.runtime.mark_callback_failed()
        self._attributed_failures += 1
        self._inflight_dispatches.discard(dispatch.sequence)
        self._failed_dispatches[dispatch.sequence] = dispatch.replay_token

    def _preserve_replay_floor(self) -> None:
        """Hold durable continuity at the position the failed batches replay from.

        Keeping it lets a restart resume there and re-deliver the batch, which
        is the same repair the in-process replay performs.  A later checkpoint
        would not cover the failed batch, so it is rewound rather than kept.
        """
        unattributed = self.runtime.callback_failure_count - self._attributed_failures
        floor = None if unattributed > 0 or self._scope_invalidated else self._earliest_failed_floor()
        if floor is None:
            self._clear_saved()
            return
        self.save(SyncCheckpoint(floor))

    def _earliest_failed_floor(self) -> str | None:
        """Return the replay position of the oldest failure still outstanding."""
        if not self._failed_dispatches:
            return None
        return self._failed_dispatches[min(self._failed_dispatches)]

    def certify_response(
        self,
        *,
        next_batch: str | None,
        cache_result: SyncCacheWriteResult,
        first_sync: bool,
    ) -> SyncCertificationDecision:
        """Apply the certification decision for one completed sync response."""
        decision = certify_sync_response(
            self.state,
            next_batch=next_batch,
            cache_result=cache_result,
            first_sync=first_sync,
        )
        limited_timeline = bool(cache_result.limited_room_ids)
        if limited_timeline and not self._awaiting_initial_window:
            decision = replace(decision, reset_client_token=True)
        self._apply_decision(decision, cache_result=cache_result)
        decision = self._request_callback_replay(decision)
        # Re-arm from applied trust, not from the decision: _apply_decision rejects
        # certification while a callback failure is outstanding, and a rejected
        # certification must not license another since-less replay.
        if decision.reset_client_token:
            self._awaiting_initial_window = True
        elif self.state is SyncTrustState.CERTIFIED:
            self._awaiting_initial_window = False
        return decision

    def _request_callback_replay(self, decision: SyncCertificationDecision) -> SyncCertificationDecision:
        """Ask the sync loop to re-deliver the batches a failed callback left unfinished."""
        if not self._failed_dispatches or decision.reset_client_token or self._pending_repair is not None:
            return decision
        sequence = min(self._failed_dispatches)
        token = self._failed_dispatches[sequence]
        # Only a token replay proves the batch was re-delivered: the homeserver
        # either resends the whole delta from it or flags the timeline limited.
        # A since-less window carries a fresh timeline slice instead, so it says
        # nothing about a batch that has already scrolled out of it.
        if token is None or token in self._replayed_positions:
            # Replaying the same position again would only re-run the callback
            # that keeps failing, and the sync loop would never move forward.
            return decision
        self._replayed_positions.add(token)
        self._replay_window = (sequence, self._dispatch_sequence)
        self.logger.warning(
            "matrix_sync_callback_replay_requested",
            outstanding_callback_failures=len(self._failed_dispatches),
        )
        return replace(decision, replay_from_token=token)

    def _repair_replayed_callback_failures(self, decision: SyncCertificationDecision) -> None:
        """Forget the failures whose batches a certified replay re-delivered."""
        self._resolve_pending_repair()
        window = self._replay_window
        self._replay_window = None
        if window is None or decision.state is not SyncTrustState.CERTIFIED:
            return
        # The replay's delta is durable, but the callbacks it just handed out
        # are still running. Their batch is the one being vouched for, so the
        # repair waits for them instead of trusting the write alone.
        self._pending_repair = (*window, self._dispatch_sequence)
        self._resolve_pending_repair()

    def _resolve_pending_repair(self) -> None:
        """Complete a held repair once the replay's own callbacks have all landed."""
        if self._pending_repair is None:
            return
        replayed_from, requested_at, replayed_through = self._pending_repair
        replay_batch = range(requested_at + 1, replayed_through + 1)
        if any(sequence in self._inflight_dispatches for sequence in replay_batch):
            return
        self._pending_repair = None
        if any(sequence in self._failed_dispatches for sequence in replay_batch):
            # The replay re-delivered the batch and a callback failed again, so
            # it proves nothing. The newer failure carries its own replay.
            self.logger.warning("matrix_sync_callback_replay_failed_again")
            return
        repaired = [sequence for sequence in self._failed_dispatches if replayed_from <= sequence <= requested_at]
        for sequence in repaired:
            del self._failed_dispatches[sequence]
        self.logger.info(
            "matrix_sync_callback_replay_certified",
            repaired_callback_failures=len(repaired),
            outstanding_callback_failures=len(self._failed_dispatches),
        )

    def reject_unknown_pos(self) -> SyncCertificationDecision:
        """Invalidate a checkpoint rejected by the homeserver."""
        decision = handle_unknown_pos()
        self._awaiting_initial_window = True
        self._apply_decision(decision)
        return decision

    def _apply_decision(
        self,
        decision: SyncCertificationDecision,
        *,
        cache_result: SyncCacheWriteResult | None = None,
    ) -> None:
        """Apply one certifier decision to trust state and durable storage."""
        self._repair_replayed_callback_failures(decision)
        callback_failure_count = self.outstanding_callback_failures
        if callback_failure_count:
            self.state = SyncTrustState.UNCERTAIN
            self.checkpoint = None
            self._preserve_replay_floor()
            self.logger.warning(
                "matrix_sync_certification_uncertain",
                reason="callback_failed",
                callback_failure_count=callback_failure_count,
            )
            return

        self.state = decision.state
        self.checkpoint = decision.checkpoint_to_save
        if decision.clear_saved_token:
            self._clear_saved()
        if decision.checkpoint_to_save is not None:
            self._certified_token = decision.checkpoint_to_save.token
            self._scope_invalidated = False
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
        """Select a generation-safe token for replaying a failed sync response."""
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
