"""Worker warmup side-band state for streaming responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import WorkerProgressEvent
    from mindroom.workers.models import WorkerReadyProgress


def _shorten_warmup_error(error: str | None) -> str:
    """Return a concise one-line startup failure message."""
    normalized_error = " ".join((error or "Worker startup failed").split())
    if len(normalized_error) > 180:
        normalized_error = f"{normalized_error[:179]}…"
    return normalized_error


@dataclass
class _ActiveWarmup:
    """Live side-band worker warmup state rendered below the current stream body."""

    worker_key: str
    backend_name: str
    tool_labels: list[str]
    last_event: WorkerReadyProgress


@dataclass(frozen=True)
class _RenderedWarmupLine:
    """One warmup line rendered for plain-text and HTML Matrix bodies."""

    text: str
    html: str


def _render_tool_labels(tool_labels: list[str]) -> tuple[str, str]:
    """Render tool labels for plain-text and HTML output."""
    plain = ", ".join(f"`{label}`" for label in tool_labels)
    html = ", ".join(f"<code>{escape(label)}</code>" for label in tool_labels)
    return plain, html


def _render_worker_status_line(warmup: _ActiveWarmup, *, show_tool_calls: bool) -> _RenderedWarmupLine:
    """Render one worker warmup line without leaking hidden tool metadata."""
    if show_tool_calls and warmup.tool_labels:
        labels_text, labels_html = _render_tool_labels(warmup.tool_labels)
        waiting_copy_text = f"Preparing isolated worker for {labels_text}..."
        waiting_copy_html = f"Preparing isolated worker for {labels_html}..."
        failure_copy_text = f"Worker startup failed for {labels_text}"
        failure_copy_html = f"Worker startup failed for {labels_html}"
    else:
        waiting_copy_text = waiting_copy_html = "Preparing isolated worker..."
        failure_copy_text = failure_copy_html = "Worker startup failed"

    phase = warmup.last_event.phase
    if phase == "failed":
        error = _shorten_warmup_error(warmup.last_event.error)
        suffix = "" if error.endswith((".", "!", "?")) else "."
        return _RenderedWarmupLine(
            text=f"⚠️ {failure_copy_text}: {error}{suffix}",
            html=f"⚠️ {failure_copy_html}: {escape(error)}{suffix}",
        )
    if phase == "cold_start":
        return _RenderedWarmupLine(
            text=f"⏳ {waiting_copy_text}",
            html=f"⏳ {waiting_copy_html}",
        )

    elapsed_seconds = max(1, int(warmup.last_event.elapsed_seconds))
    return _RenderedWarmupLine(
        text=f"⏳ {waiting_copy_text} {elapsed_seconds}s elapsed.",
        html=f"⏳ {waiting_copy_html} {elapsed_seconds}s elapsed.",
    )


@dataclass
class WorkerWarmupState:
    """Tracks worker warmup state that is rendered beside the main stream body."""

    active_warmups: dict[str, _ActiveWarmup] = field(default_factory=dict)
    last_send_had_warmup_suffix: bool = False
    needs_warmup_clear_edit: bool = False

    def clear_for_terminal_transition(self) -> None:
        """Drop any warmup suffix state before terminal finalization."""
        self.active_warmups.clear()
        self.last_send_had_warmup_suffix = False
        self.needs_warmup_clear_edit = False

    def note_nonterminal_delivery(self, *, had_warmup_suffix: bool) -> None:
        """Track whether the last visible non-terminal edit carried warmup lines."""
        self.last_send_had_warmup_suffix = had_warmup_suffix
        self.needs_warmup_clear_edit = False

    def clear_terminal_failures(self) -> None:
        """Drop failed warmup notices once the stream resumes with normal content."""
        failed_worker_keys = [
            worker_key for worker_key, warmup in self.active_warmups.items() if warmup.last_event.phase == "failed"
        ]
        for worker_key in failed_worker_keys:
            self.active_warmups.pop(worker_key, None)

    def _clear_failed_retry_duplicates(
        self,
        *,
        worker_key: str,
        tool_label: str,
    ) -> None:
        """Drop stale failed warmups when a new retry starts for the same tool."""
        stale_failed_worker_keys = [
            active_worker_key
            for active_worker_key, warmup in self.active_warmups.items()
            if active_worker_key != worker_key
            and warmup.last_event.phase == "failed"
            and tool_label in warmup.tool_labels
        ]
        for stale_worker_key in stale_failed_worker_keys:
            self.active_warmups.pop(stale_worker_key, None)

    def render_lines(self, *, show_tool_calls: bool) -> list[_RenderedWarmupLine]:
        """Render all active worker warmup notices as side-band suffix lines."""
        if not self.active_warmups:
            return []

        return [
            _render_worker_status_line(warmup, show_tool_calls=show_tool_calls)
            for warmup in self.active_warmups.values()
        ]

    def apply_event(self, event: WorkerProgressEvent) -> bool:
        """Update side-band warmup state from one routed worker progress event."""
        progress = event.progress
        worker_key = progress.worker_key
        if progress.phase == "ready":
            removed = self.active_warmups.pop(worker_key, None)
            if removed is None:
                return False
            if not self.active_warmups and self.last_send_had_warmup_suffix:
                self.needs_warmup_clear_edit = True
            return True

        tool_label = f"{event.tool_name}.{event.function_name}"
        self.needs_warmup_clear_edit = False
        if progress.phase != "failed":
            self._clear_failed_retry_duplicates(
                worker_key=worker_key,
                tool_label=tool_label,
            )
        warmup = self.active_warmups.get(worker_key)
        if warmup is None:
            self.active_warmups[worker_key] = _ActiveWarmup(
                worker_key=worker_key,
                backend_name=progress.backend_name,
                tool_labels=[tool_label],
                last_event=progress,
            )
            return True

        if tool_label not in warmup.tool_labels:
            warmup.tool_labels.append(tool_label)
        warmup.backend_name = progress.backend_name
        warmup.last_event = progress
        return True
