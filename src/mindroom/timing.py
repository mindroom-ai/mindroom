"""Lightweight timing instrumentation controlled by MINDROOM_TIMING env var."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

    from structlog.stdlib import BoundLogger

logger = get_logger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")

# When set, log lines include the scope for grouping related timers.
timing_scope: ContextVar[str | None] = ContextVar("timing_scope", default=None)
_DISPATCH_PIPELINE_TIMING_KEY = "com.mindroom.dispatch_pipeline_timing"


def _is_enabled() -> bool:
    return os.environ.get("MINDROOM_TIMING", "") == "1"


def _debug_enabled(bound_logger: object) -> bool:
    """Return whether one logger would keep a debug event.

    Only used to skip work that would be discarded, so anything that cannot
    answer the question counts as enabled: test doubles and non-stdlib structlog
    loggers must never lose a diagnostic to this check.
    """
    is_enabled_for = getattr(bound_logger, "isEnabledFor", None)
    if not callable(is_enabled_for):
        return True
    try:
        return bool(is_enabled_for(logging.DEBUG))
    except Exception:
        return True


def elapsed_ms_between(start: float, end: float, *, ndigits: int = 1) -> float:
    """Return elapsed milliseconds rounded to the shared precision policy."""
    return round((end - start) * 1000, ndigits)


def elapsed_ms_since(
    start: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    ndigits: int = 1,
) -> float:
    """Return elapsed milliseconds from ``start`` using the shared rounding policy."""
    return elapsed_ms_between(start, clock(), ndigits=ndigits)


type _TimingMetadataValue = str | int | float | bool

_PRIMARY_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("seg_ingress_ms", "message_received", "gate_enter"),
    ("seg_coalescing_ms", "gate_enter", "gate_exit"),
    ("seg_dispatch_ms", "gate_exit", "lock_wait_start"),
    ("seg_response_queue_ms", "lock_wait_start", "lock_acquired"),
    ("seg_first_visible_reply_ms", "lock_acquired", "first_visible_reply"),
    ("seg_after_first_visible_ms", "first_visible_reply", "response_complete"),
)

_PRIMARY_TOTALS: tuple[tuple[str, str, str], ...] = (
    ("time_to_first_visible_reply_ms", "message_received", "first_visible_reply"),
    ("time_to_first_substantive_reply_ms", "message_received", "first_substantive_reply"),
    ("total_pipeline_ms", "message_received", "response_complete"),
)

_DIAGNOSTIC_SPANS: tuple[tuple[str, str, str], ...] = (
    ("diag_ingress_normalize_ms", "ingress_normalize_start", "ingress_normalize_ready"),
    ("diag_dispatch_prepare_ms", "dispatch_prepare_start", "dispatch_prepare_ready"),
    ("diag_dispatch_plan_ms", "dispatch_plan_start", "dispatch_plan_ready"),
    ("diag_response_payload_setup_ms", "response_payload_start", "response_payload_ready"),
    ("diag_thread_refresh_ms", "thread_refresh_start", "thread_refresh_ready"),
    ("diag_lock_wait_ms", "lock_wait_start", "lock_acquired"),
    ("diag_runtime_prepare_ms", "response_runtime_start", "response_runtime_ready"),
    ("diag_llm_prepare_ms", "ai_prepare_start", "history_ready"),
    ("diag_prompt_branch_join_ms", "prompt_branches_start", "prompt_branches_ready"),
    ("diag_memory_prepare_ms", "memory_prepare_start", "memory_prepare_ready"),
    ("diag_agent_build_ms", "agent_build_start", "agent_build_ready"),
    ("diag_history_classify_ms", "history_classify_start", "history_classify_ready"),
    ("diag_required_compaction_ms", "required_compaction_start", "required_compaction_ready"),
    ("diag_replay_plan_ms", "replay_plan_start", "replay_plan_ready"),
    ("diag_prompt_assembly_ms", "prompt_assembly_start", "prompt_assembly_ready"),
    ("diag_history_ready_to_model_request_ms", "history_ready", "model_request_sent"),
    ("diag_provider_ttft_ms", "model_request_sent", "model_first_token"),
    (
        "diag_first_visible_to_first_substantive_reply_ms",
        "first_visible_reply",
        "first_substantive_reply",
    ),
    (
        "diag_model_first_token_to_first_substantive_reply_ms",
        "model_first_token",
        "first_substantive_reply",
    ),
    ("diag_first_visible_to_stream_complete_ms", "first_visible_reply", "streaming_complete"),
    ("diag_model_request_to_completion_ms", "model_request_sent", "response_complete"),
)


@dataclass(slots=True)
class DispatchPipelineTiming:
    """Collect phase timestamps for one dispatch turn and emit a summary."""

    source_event_id: str
    room_id: str
    marks: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, _TimingMetadataValue] = field(default_factory=dict)
    summary_emitted: bool = False

    def mark(self, label: str, *, overwrite: bool = False) -> None:
        """Record one high-level phase boundary."""
        if overwrite or label not in self.marks:
            self.marks[label] = time.perf_counter()

    def note(self, **metadata: _TimingMetadataValue | None) -> None:
        """Attach diagnostic metadata for the eventual summary log."""
        for key, value in metadata.items():
            if value is not None:
                self.metadata[key] = value

    def mark_first_visible_reply(self, kind: str, *, substantive: bool = False) -> None:
        """Record the first visible reply and, when confirmed, the first substantive reply."""
        needs_visible = "first_visible_reply" not in self.marks
        needs_substantive = substantive and "first_substantive_reply" not in self.marks
        if not needs_visible and not needs_substantive:
            return
        now = time.perf_counter()
        if needs_visible:
            self.marks["first_visible_reply"] = now
            self.metadata["first_visible_kind"] = kind
        if needs_substantive:
            self.marks["first_substantive_reply"] = now
            self.metadata["first_substantive_kind"] = kind

    def _elapsed_ms(self, start_label: str, end_label: str) -> float | None:
        """Return elapsed time between two recorded phase boundaries."""
        start = self.marks.get(start_label)
        end = self.marks.get(end_label)
        if start is None or end is None:
            return None
        return elapsed_ms_between(start, end)

    def emit_summary(self, logger: BoundLogger, *, outcome: str) -> None:
        """Log one structured end-to-end timing summary."""
        if self.summary_emitted:
            return
        self.summary_emitted = True
        if not _debug_enabled(logger):
            return
        summary: dict[str, Any] = {
            "source_event_id": self.source_event_id,
            "room_id": self.room_id,
            "outcome": outcome,
            **self.metadata,
        }
        duration_pairs = (*_PRIMARY_SEGMENTS, *_PRIMARY_TOTALS, *_DIAGNOSTIC_SPANS)
        for key, start_label, end_label in duration_pairs:
            elapsed = self._elapsed_ms(start_label, end_label)
            if elapsed is not None:
                summary[key] = elapsed
        logger.debug("Dispatch pipeline timing", **summary)


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


def event_timing_scope(event_id: str | None) -> str:
    """Return the stable timing scope identifier for one event."""
    return event_id[:20] if event_id else "unknown"


def emit_timing_event(event_name: str, **event_data: object) -> None:
    """Emit one structured timing event when timing instrumentation is enabled.

    Emitted at ``debug`` level so callers can keep ``MINDROOM_TIMING=1`` enabled
    in long-running processes and still flip emission off cheaply via the global
    log level. The ``isEnabledFor`` check runs before the payload is assembled,
    so a dropped event costs one level lookup rather than a discarded dict.
    """
    if not _is_enabled() or not _debug_enabled(logger):
        return
    scope = event_data.pop("timing_scope", None)
    if not isinstance(scope, str) or not scope:
        scope = timing_scope.get()
    filtered_event_data = {key: value for key, value in event_data.items() if value is not None}
    if scope:
        filtered_event_data["timing_scope"] = scope
    logger.debug(event_name, **filtered_event_data)


def emit_elapsed_timing(label: str, start: float, **event_data: object) -> None:
    """Emit one elapsed timing event relative to a previously recorded start time."""
    if not _is_enabled():
        return
    emit_timing_event(
        "timing_elapsed",
        label=label,
        duration_ms=elapsed_ms_since(start),
        **event_data,
    )


@contextmanager
def timed_block(
    label: str,
    *,
    scope: str | None = None,
    **event_data: object,
) -> Iterator[None]:
    """Emit elapsed timing for a small inline block when timing diagnostics are enabled."""
    if not _is_enabled():
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        if scope is not None:
            event_data["timing_scope"] = scope
        emit_elapsed_timing(label, start, **event_data)


def timed(label: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator that logs elapsed time for sync/async functions.

    When MINDROOM_TIMING != "1", returns the original function unchanged (zero overhead).
    Log format: TIMING [<scope>] <label>: <elapsed>s  (scope omitted if not set)
    """

    def decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        if not _is_enabled():
            return fn

        def emit_timing(start: float, kwargs: Mapping[str, Any]) -> None:
            emit_elapsed_timing(label, start, timing_scope=kwargs.get("timing_scope"))

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def async_generator_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> AsyncIterator[object]:
                start = time.monotonic()
                try:
                    async_generator_fn = cast("Callable[_P, AsyncIterator[object]]", fn)
                    async for item in async_generator_fn(*args, **kwargs):
                        yield item
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[_P, _R]", async_generator_wrapper)

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
                start = time.monotonic()
                try:
                    async_fn = cast("Callable[_P, Awaitable[_R]]", fn)
                    return await async_fn(*args, **kwargs)
                finally:
                    emit_timing(start, kwargs)

            return cast("Callable[_P, _R]", async_wrapper)

        @functools.wraps(fn)
        def sync_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                emit_timing(start, kwargs)

        return sync_wrapper

    return decorator
