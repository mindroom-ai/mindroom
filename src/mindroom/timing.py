"""Lightweight timing instrumentation controlled by MINDROOM_TIMING env var."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from structlog.stdlib import BoundLogger

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# When set, log lines include the scope for grouping related timers.
timing_scope: ContextVar[str | None] = ContextVar("timing_scope", default=None)
_DISPATCH_PIPELINE_TIMING_KEY = "com.mindroom.dispatch_pipeline_timing"


def _is_enabled() -> bool:
    return os.environ.get("MINDROOM_TIMING", "") == "1"


type TimingMetadataValue = str | int | float | bool


@dataclass(slots=True)
class DispatchPipelineTiming:
    """Collect phase timestamps for one dispatch turn and emit a summary."""

    source_event_id: str
    room_id: str
    marks: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, TimingMetadataValue] = field(default_factory=dict)
    summary_emitted: bool = False

    def mark(self, label: str, *, overwrite: bool = False) -> None:
        """Record one high-level phase boundary."""
        if overwrite or label not in self.marks:
            self.marks[label] = time.perf_counter()

    def note(self, **metadata: TimingMetadataValue) -> None:
        """Attach diagnostic metadata for the eventual summary log."""
        for key, value in metadata.items():
            if value is not None:
                self.metadata[key] = value

    def elapsed_ms(self, start_label: str, end_label: str) -> float | None:
        """Return elapsed time between two recorded phase boundaries."""
        start = self.marks.get(start_label)
        end = self.marks.get(end_label)
        if start is None or end is None:
            return None
        return round((end - start) * 1000, 1)

    def emit_summary(self, logger: BoundLogger, *, outcome: str) -> None:
        """Log one structured end-to-end timing summary."""
        if self.summary_emitted:
            return
        self.summary_emitted = True
        summary: dict[str, Any] = {
            "source_event_id": self.source_event_id,
            "room_id": self.room_id,
            "outcome": outcome,
            **self.metadata,
        }
        duration_pairs = {
            "arrival_to_gate_entry_ms": ("message_received", "gate_enter"),
            "coalescing_gate_ms": ("gate_enter", "gate_exit"),
            "gate_exit_to_dispatch_start_ms": ("gate_exit", "dispatch_start"),
            "prepare_dispatch_ms": ("dispatch_prepare_start", "dispatch_prepare_ready"),
            "plan_dispatch_ms": ("dispatch_plan_start", "dispatch_plan_ready"),
            "response_payload_setup_ms": ("response_payload_start", "response_payload_ready"),
            "lock_wait_ms": ("lock_wait_start", "lock_acquired"),
            "post_lock_thread_refresh_ms": ("lock_acquired", "thread_refresh_ready"),
            "lock_acquired_to_placeholder_ms": ("lock_acquired", "placeholder_sent"),
            "placeholder_to_runtime_start_ms": ("placeholder_sent", "response_runtime_start"),
            "runtime_prepare_ms": ("response_runtime_start", "response_runtime_ready"),
            "system_prompt_history_ms": ("ai_prepare_start", "history_ready"),
            "history_ready_to_model_request_ms": ("history_ready", "model_request_sent"),
            "model_request_to_first_token_ms": ("model_request_sent", "model_first_token"),
            "model_first_token_to_first_visible_stream_update_ms": (
                "model_first_token",
                "first_visible_stream_update",
            ),
            "first_visible_stream_update_to_stream_complete_ms": (
                "first_visible_stream_update",
                "streaming_complete",
            ),
            "placeholder_visible_ms": ("placeholder_sent", "response_complete"),
            "total_pipeline_ms": ("message_received", "response_complete"),
            "model_request_to_completion_ms": ("model_request_sent", "response_complete"),
        }
        for key, (start_label, end_label) in duration_pairs.items():
            elapsed = self.elapsed_ms(start_label, end_label)
            if elapsed is not None:
                summary[key] = elapsed
        logger.info("Dispatch pipeline timing", **summary)


def create_dispatch_pipeline_timing(*, event_id: str, room_id: str) -> DispatchPipelineTiming | None:
    """Return a new tracker when timing instrumentation is enabled."""
    if not _is_enabled():
        return None
    timing = DispatchPipelineTiming(source_event_id=event_id, room_id=room_id)
    timing.mark("message_received")
    return timing


def attach_dispatch_pipeline_timing(
    source: object,
    timing: DispatchPipelineTiming | None,
) -> DispatchPipelineTiming | None:
    """Persist one tracker on an in-memory Matrix event source dict."""
    if timing is None or not isinstance(source, dict):
        return timing
    source_dict = cast("dict[str, object]", source)
    source_dict[_DISPATCH_PIPELINE_TIMING_KEY] = timing
    return timing


def get_dispatch_pipeline_timing(source: object) -> DispatchPipelineTiming | None:
    """Return the tracker stored on one in-memory Matrix event source dict."""
    if not isinstance(source, dict):
        return None
    source_dict = cast("dict[str, object]", source)
    raw_timing = source_dict.get(_DISPATCH_PIPELINE_TIMING_KEY)
    if isinstance(raw_timing, DispatchPipelineTiming):
        return raw_timing
    return None


def timed(label: str) -> Callable[[Callable[P, R]], Callable[P, R]]:  # noqa: C901
    """Decorator that logs elapsed time for sync/async functions.

    When MINDROOM_TIMING != "1", returns the original function unchanged (zero overhead).
    Log format: TIMING [<scope>] <label>: <elapsed>s  (scope omitted if not set)
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        if not _is_enabled():
            return fn

        def emit_timing(start: float, kwargs: P.kwargs) -> None:
            scope = kwargs.get("timing_scope")
            if not isinstance(scope, str) or not scope:
                scope = timing_scope.get()
            prefix = f"[{scope}] " if scope else ""
            logger.info("TIMING %s%s: %.3fs", prefix, label, time.monotonic() - start)

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def async_generator_wrapper(*args: P.args, **kwargs: P.kwargs) -> AsyncIterator[object]:
                start = time.monotonic()
                try:
                    async_generator_fn = cast("Callable[P, AsyncIterator[object]]", fn)
                    async for item in async_generator_fn(*args, **kwargs):
                        yield item
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[P, R]", async_generator_wrapper)

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                start = time.monotonic()
                try:
                    async_fn = cast("Callable[P, Awaitable[R]]", fn)
                    return await async_fn(*args, **kwargs)
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                emit_timing(start, kwargs)

        return sync_wrapper

    return decorator
