"""Shared limits and outcomes for tool-free judgment backends."""

from __future__ import annotations

import asyncio
import hashlib
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING

from mindroom.judgment.answers import JudgmentError, JudgmentFailure, JudgmentResponse, JudgmentResult
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(slots=True)
class _CapacityLease:
    capacity: JudgmentCapacity
    owner: str
    references: int = 1

    def release(self) -> None:
        self.references -= 1
        if self.references == 0:
            self.capacity.release(self.owner)


class JudgmentCapacity:
    """One process-local, non-waiting global and per-owner request budget."""

    def __init__(self, *, max_concurrent: int, max_per_owner: int) -> None:
        if max_concurrent < 1 or max_per_owner < 1 or max_per_owner > max_concurrent:
            msg = "judgment capacity limits must be positive and per-owner must not exceed global"
            raise ValueError(msg)
        self._max_concurrent = max_concurrent
        self._max_per_owner = max_per_owner
        self._active = 0
        self._active_by_owner: dict[str, int] = {}
        self._lock = Lock()

    def _acquire_nowait(self, owner: str) -> _CapacityLease | None:
        """Acquire immediately or return none without creating a waiter."""
        with self._lock:
            owner_active = self._active_by_owner.get(owner, 0)
            if self._active >= self._max_concurrent or owner_active >= self._max_per_owner:
                return None
            self._active += 1
            self._active_by_owner[owner] = owner_active + 1
        return _CapacityLease(self, owner)

    def release(self, owner: str) -> None:
        """Release one acquired owner slot."""
        with self._lock:
            owner_active = self._active_by_owner[owner]
            self._active -= 1
            if owner_active == 1:
                del self._active_by_owner[owner]
            else:
                self._active_by_owner[owner] = owner_active - 1


SHARED_CAPACITY = JudgmentCapacity(max_concurrent=8, max_per_owner=1)
_CURRENT_LEASE: ContextVar[_CapacityLease] = ContextVar("judgment_capacity_lease")


async def run_judgment_thread[T](function: Callable[[], T]) -> T:
    """Keep uncancellable synchronous work charged until its worker actually finishes."""
    lease = _CURRENT_LEASE.get()
    future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, function)
    lease.references += 1

    def finished(done: asyncio.Future[T]) -> None:
        # Retrieve late failures even when the caller already timed out or was cancelled.
        if not done.cancelled():
            done.exception()
        lease.release()

    future.add_done_callback(finished)
    return await asyncio.shield(future)


def _result(
    request: JudgmentRequest,
    started: float,
    failure: JudgmentFailure | None,
    response: JudgmentResponse | None = None,
) -> JudgmentResult:
    usage = response.usage if response is not None else None
    return JudgmentResult(
        decision=None if response is None else response.decision,
        probability=None if response is None else response.probability,
        failure=failure,
        model_id=None if response is None else response.model,
        latency_ms=max(0, round((perf_counter() - started) * 1000)),
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        state_bytes=request.state_bytes,
    )


async def run_judgment(
    request: JudgmentRequest,
    evaluate: Callable[[JudgmentRequest], Awaitable[JudgmentResponse]],
    *,
    owner: str,
    timeout_seconds: float,
    allow_network: bool,
    capacity: JudgmentCapacity = SHARED_CAPACITY,
) -> JudgmentResult:
    """Run one bounded attempt; abandoned worker threads retain their capacity."""
    started = perf_counter()
    if not request.complete or request.body is None:
        return _result(request, started, "incomplete_state")
    if (
        request.state_bytes != len(request.body)
        or len(request.body) > MAX_REQUEST_BYTES
        or hashlib.sha256(request.body).hexdigest() != request.request_hash
        or not owner
    ):
        return _result(request, started, "invalid_request")
    if not allow_network:
        return _result(request, started, "network_disabled")
    lease = capacity._acquire_nowait(owner)
    if lease is None:
        return _result(request, started, "capacity_exhausted")
    response: JudgmentResponse | None = None
    failure: JudgmentFailure | None = None
    token = _CURRENT_LEASE.set(lease)
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await evaluate(request)
        # Synchronous decoding can overrun a deadline without yielding.
        if perf_counter() - started > timeout_seconds:
            response, failure = None, "timeout"
    except TimeoutError:
        failure = "timeout"
    except JudgmentError as error:
        failure = error.failure
    except Exception:
        # SDK errors can include request bodies and credentials. Only expose a category.
        failure = "provider_error"
    finally:
        _CURRENT_LEASE.reset(token)
        lease.release()
    return _result(request, started, failure, response)
