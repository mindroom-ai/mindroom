"""Manager-owned startup recovery and unavailable approval-owner settlement."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from mindroom.approval_failure import prepare_approval_failure
from mindroom.event_journal import DeliveryStage
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.approval_manager import ApprovalManager
    from mindroom.event_journal import ApprovalContinuation, ApprovalDeliveryView, EventJournalStore

logger = get_logger(__name__)
_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS = 1.0
_STARTUP_CLEANUP_MAX_RETRY_SECONDS = 30.0
_STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION = 10
_UNAVAILABLE_OWNER_SCAN_LIMIT = 100


@dataclass
class ApprovalRecovery:
    """Retain startup gates across transport binding; the manager owns task shutdown."""

    deliver_unavailable_notice: Callable[[ApprovalContinuation, str], Awaitable[ApprovalDeliveryView | None]]
    journal_provider: Callable[[], EventJournalStore] | None = None
    entity_configured: Callable[[str], bool] | None = None
    entity_permanently_unavailable: Callable[[str], bool] | None = None
    recover_unavailable_final: Callable[[str, ApprovalContinuation], Awaitable[bool]] | None = None
    manager: ApprovalManager | None = None
    _startup_router_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_runtime_support_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_done: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_cleanup_retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _startup_cleanup_retry_delay: float = field(
        default=_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS,
        init=False,
        repr=False,
    )
    _startup_cleanup_attempts: int = field(default=0, init=False, repr=False)

    def bind(self, manager: ApprovalManager) -> None:
        """Attach the runtime owner without losing readiness observed during bootstrap."""
        self.manager = manager
        if (
            self._startup_router_ready_for_cleanup
            and self._startup_runtime_support_ready_for_cleanup
            and not self._startup_cleanup_done
        ):
            self._schedule_startup_cleanup_retry()

    def _unavailable_entity_reason(self, entity_name: str) -> str | None:
        permanently_unavailable = (
            self.entity_permanently_unavailable is not None and self.entity_permanently_unavailable(entity_name)
        )
        configured = self.entity_configured is None or self.entity_configured(entity_name)
        if configured and not permanently_unavailable:
            return None
        if permanently_unavailable:
            return f"Requesting agent '{entity_name}' could not start and is unavailable."
        return f"Requesting agent '{entity_name}' is no longer available."

    async def reconcile_unavailable_entities(self, entity_names: Iterable[str]) -> None:
        """Fail closed continuations whose owner cannot ever run them."""
        names = set(entity_names)
        if not names:
            return
        if not await self._reconcile_unavailable_owner_pages(names):
            self._startup_cleanup_done = False
            self._schedule_startup_cleanup_retry()

    async def _reconcile_unavailable_owner_pages(self, entity_names: set[str] | None) -> bool:
        """Settle unavailable owners across one complete cursor scan."""
        journal = None if self.journal_provider is None else self.journal_provider()
        if journal is None:
            return True
        complete = True
        cursor: tuple[str, str] | None = None
        while True:
            if entity_names is None:
                owners = await journal.approval_continuations(
                    limit=_UNAVAILABLE_OWNER_SCAN_LIMIT,
                    after=cursor,
                )
            else:
                owners = await journal.approval_continuations_for_entities(
                    entity_names,
                    limit=_UNAVAILABLE_OWNER_SCAN_LIMIT,
                    after=cursor,
                )
            if not owners:
                break
            cursor = (owners[-1][1].entity_name, owners[-1][1].approval_id)
            for principal_id, continuation in owners:
                reason = self._unavailable_entity_reason(continuation.entity_name)
                if reason is not None:
                    complete = await self._discard_unavailable(principal_id, continuation, reason) and complete
            if len(owners) < _UNAVAILABLE_OWNER_SCAN_LIMIT:
                break
        return complete

    async def _discard_unavailable(
        self,
        principal_id: str,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> bool:
        """Expire visible cards, then atomically release the removed owner's sources."""
        assert self.journal_provider is not None
        store = self.journal_provider().principal(principal_id)
        current = await store.approval_continuation(continuation.approval_id)
        if current is None:
            return True
        final_delivery = await store.load_matrix_delivery(
            delivery_id=current.source_event_ids[0],
            stage=DeliveryStage.FINAL,
        )
        if final_delivery is not None:
            return self.recover_unavailable_final is not None and await self.recover_unavailable_final(
                principal_id,
                current,
            )
        manager = self.manager
        current = await prepare_approval_failure(
            current,
            reason,
            request_failure=partial(
                store.request_approval_failure,
                current.approval_id,
                expected_state=current.state,
                expected_generation=current.generation,
                expected_runtime_generation=current.runtime_generation,
            ),
            expire_cards=None if manager is None else manager.expire_continuation_cards,
        )
        if current is None:
            return False
        notice_store = await self.deliver_unavailable_notice(current, reason)
        if notice_store is None:
            return False
        return await store.discard_unavailable_approval_continuation(
            current.approval_id,
            notice_principal_id=notice_store.principal_id,
        )

    def reset_startup_cleanup_gate(self) -> None:
        """Reset one-shot startup approval cleanup state."""
        self._startup_router_ready_for_cleanup = False
        self._startup_runtime_support_ready_for_cleanup = False
        self._startup_cleanup_done = False
        self._startup_cleanup_retry_delay = _STARTUP_CLEANUP_INITIAL_RETRY_SECONDS
        self._startup_cleanup_attempts = 0
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None:
            retry.cancel()

    async def close(self) -> None:
        """Stop retry work before the approval manager releases its runtime."""
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None and not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)

    async def mark_startup_runtime_support_ready(self) -> None:
        """Record that startup cleanup may use runtime services."""
        self._startup_runtime_support_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def mark_router_ready(self) -> None:
        """Record router first sync without depending on Matrix bot internals."""
        self._startup_router_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def _run_startup_cleanup_if_ready(self) -> None:
        """Retry approval-card recovery and unavailable-owner cleanup until both finish."""
        if (
            self.manager is None
            or self._startup_cleanup_done
            or not self._startup_router_ready_for_cleanup
            or not self._startup_runtime_support_ready_for_cleanup
        ):
            return
        async with self._startup_cleanup_lock:
            if self._startup_cleanup_done:
                return
            self._startup_cleanup_attempts += 1
            cards_recovered = await self._recover_approval_cards_on_startup()
            owners_settled = False
            try:
                owners_settled = await self._reconcile_unavailable_owner_pages(None)
            except Exception:
                logger.warning(
                    "tool_approval_unavailable_owner_cleanup_failed",
                    attempt=self._startup_cleanup_attempts,
                    exc_info=True,
                )
            if not cards_recovered or not owners_settled:
                self._schedule_startup_cleanup_retry()
                return
            self._startup_cleanup_done = True
            self._retire_startup_cleanup_retry()

    async def _recover_approval_cards_on_startup(self) -> bool:
        """Recover current approval-card transport obligations."""
        try:
            manager = self.manager
            if manager is None:
                return False
            sweep = await manager.recover_cards_on_startup()
        except Exception as exc:
            logger.warning(
                "tool_approval_startup_recovery_failed",
                error=str(exc),
                attempt=self._startup_cleanup_attempts,
                exc_info=True,
            )
            return False
        logger.info(
            "approval_startup_recovery_finished",
            attempt=self._startup_cleanup_attempts,
            scanned=sweep.scanned,
            retired=sweep.discarded,
            owed_count=sweep.failed,
        )
        if not sweep.complete:
            incomplete = (
                logger.error
                if self._startup_cleanup_attempts >= _STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION
                else logger.warning
            )
            incomplete(
                "tool_approval_startup_recovery_incomplete",
                owed_count=sweep.failed,
                attempt=self._startup_cleanup_attempts,
            )
        return sweep.complete

    def _schedule_startup_cleanup_retry(self) -> None:
        """Arrange a later cleanup pass after a transient failure."""
        pending = self._startup_cleanup_retry
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            return
        self._startup_cleanup_retry = asyncio.create_task(
            self._run_startup_cleanup_after_delay(),
            name="approval_startup_cleanup_retry",
        )

    def _retire_startup_cleanup_retry(self) -> None:
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None and not retry.done() and retry is not asyncio.current_task():
            retry.cancel()

    async def _run_startup_cleanup_after_delay(self) -> None:
        delay = self._startup_cleanup_retry_delay
        self._startup_cleanup_retry_delay = min(delay * 2, _STARTUP_CLEANUP_MAX_RETRY_SECONDS)
        await asyncio.sleep(delay)
        await self._run_startup_cleanup_if_ready()
