"""Dependency-free in-memory store for passively observed provider rate-limit windows."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "PassiveLimitReading",
    "PassiveWindow",
    "WindowKind",
    "build_window",
    "claude_window_metadata",
    "clear",
    "codex_window_metadata",
    "finite_float",
    "format_utc_iso",
    "get_passive_windows",
    "normalize_codex_scope_key",
    "parse_reset_timestamp",
    "record_claude_rate_limit",
    "record_codex_headers",
    "sort_windows",
]

WindowKind = Literal["session", "weekly", "monthly"]
_CLAUDE_MODEL_LABELS = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "oauth_apps": "OAuth apps",
    "cowork": "Cowork",
}
_WINDOW_ORDER = {"five_hour": 0, "seven_day": 1, "credit_limit": 3, "extra_usage": 3, "overage": 3}


@dataclass(frozen=True, slots=True)
class PassiveWindow:
    """One normalized provider rate-limit or credit window."""

    id: str
    kind: WindowKind
    label: str
    used_percent: float
    remaining_percent: float
    window_duration_mins: int | None = None
    resets_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract window shape."""
        base: dict[str, object] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
        }
        extra = {
            k: v
            for k, v in (
                ("window_duration_mins", self.window_duration_mins),
                ("resets_at", self.resets_at),
                ("detail", self.detail),
            )
            if v is not None
        }
        return {**base, **extra}


@dataclass(frozen=True, slots=True)
class PassiveLimitReading:
    """Latest passively observed windows for one provider account scope."""

    windows: tuple[PassiveWindow, ...]
    observed_at: datetime
    plan: str | None = None


@dataclass(slots=True)
class _ScopeState:
    windows: dict[str, PassiveWindow]
    observed_at: datetime
    plan: str | None = None


_LOCK = threading.Lock()
_STORE: dict[tuple[str, str], _ScopeState] = {}


def _utc(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def format_utc_iso(dt: datetime) -> str:
    """Format a datetime as canonical UTC ISO-8601 with trailing Z."""
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def finite_float(value: object) -> float | None:
    """Parse a finite float from int/float/str, rejecting booleans, NaN, and Inf."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value) if isinstance(value, (int, float, str)) else math.nan
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def parse_reset_timestamp(value: object) -> str | None:
    """Convert unix seconds or an ISO-8601 string to canonical UTC ISO-8601."""
    parsed: datetime | None = None
    if (numeric := finite_float(value)) is not None:
        if numeric > 0:
            try:
                parsed = datetime.fromtimestamp(numeric, tz=UTC)
            except (OverflowError, OSError, ValueError):
                parsed = None
    elif isinstance(value, str) and (text := value.strip()):
        try:
            parsed = datetime.fromisoformat(f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            parsed = None
    return None if parsed is None else format_utc_iso(parsed)


def build_window(
    *,
    window_id: str,
    kind: WindowKind,
    label: str,
    used_percent: float,
    window_duration_mins: int | None = None,
    resets_at: str | None = None,
    detail: str | None = None,
) -> PassiveWindow:
    """Create a clamped, consistently rounded window."""
    used = round(max(0.0, min(100.0, used_percent)), 1)
    return PassiveWindow(
        id=window_id,
        kind=kind,
        label=label,
        used_percent=used,
        remaining_percent=round(100.0 - used, 1),
        window_duration_mins=window_duration_mins,
        resets_at=resets_at,
        detail=detail,
    )


def normalize_codex_scope_key(codex_home: str | Path | None) -> str:
    """Return the canonical in-memory scope key for a Codex home directory (never exposed externally)."""
    raw = str(codex_home).strip() if codex_home is not None else ""
    return str(Path(raw or "~/.codex").expanduser().resolve(strict=False))


def codex_window_metadata(slot: str, duration_mins: int | None) -> tuple[str, WindowKind, str]:
    """Return ``(id, kind, label)`` for a Codex primary/secondary rate-limit window."""
    if duration_mins == 300 or (duration_mins is None and slot == "primary"):
        return ("five_hour", "session", "5-hour window")
    if duration_mins == 10080 or (duration_mins is None and slot == "secondary"):
        return ("seven_day", "weekly", "7-day window")
    assert duration_mins is not None
    if duration_mins <= 1440:
        label = f"{duration_mins // 60}-hour window" if duration_mins % 60 == 0 else f"{duration_mins}-minute window"
        return (slot, "session", label)
    kind: WindowKind = "weekly" if duration_mins <= 14400 else "monthly"
    label = f"{duration_mins // 1440}-day window" if duration_mins % 1440 == 0 else f"{duration_mins // 60}-hour window"
    return (slot, kind, label)


def claude_window_metadata(bucket: str) -> tuple[str, WindowKind, str, int | None]:
    """Return ``(id, kind, label, window_duration_mins)`` for one Claude Code rate-limit bucket."""
    normalized = bucket.strip().lower().replace("-", "_")
    if normalized == "five_hour":
        return ("five_hour", "session", "5-hour window", 300)
    if normalized == "seven_day":
        return ("seven_day", "weekly", "7-day window", 10080)
    if normalized.startswith("seven_day_"):
        suffix = normalized.removeprefix("seven_day_")
        return (
            normalized,
            "weekly",
            f"7-day {_CLAUDE_MODEL_LABELS.get(suffix, suffix.replace('_', ' ').title())}",
            10080,
        )
    if normalized in {"overage", "extra_usage"}:
        return (normalized, "monthly", "Extra usage", None)
    kind: WindowKind = (
        "weekly"
        if "seven" in normalized or "week" in normalized
        else ("monthly" if "month" in normalized else "session")
    )
    return (normalized, kind, normalized.replace("_", " ").capitalize(), None)


def sort_windows(windows: tuple[PassiveWindow, ...] | list[PassiveWindow]) -> tuple[PassiveWindow, ...]:
    """Return windows in canonical display order (5h, 7d, model-scoped, credit/extra, other)."""
    return tuple(
        sorted(
            windows,
            key=lambda w: (
                _WINDOW_ORDER.get(w.id, 2 if w.id.startswith("seven_day_") else 4),
                w.window_duration_mins or 0,
                w.id,
            ),
        ),
    )


def _merge(provider: str, scope_key: str, windows: list[PassiveWindow], *, observed_at: datetime) -> None:
    if not windows:
        return
    with _LOCK:
        state = _STORE.setdefault((provider, scope_key), _ScopeState(windows={}, observed_at=observed_at))
        for window in windows:
            state.windows[window.id] = window
        state.observed_at = observed_at


def record_codex_headers(scope_key: str, headers: Mapping[str, object], *, now: datetime | None = None) -> None:
    """Parse ``x-codex-primary-*`` and ``x-codex-secondary-*`` headers into the passive store."""
    if not scope_key:
        return
    lowered = {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
    parsed: list[PassiveWindow] = []
    for slot in ("primary", "secondary"):
        if (used := finite_float(lowered.get(f"x-codex-{slot}-used-percent"))) is None:
            continue
        raw_mins = finite_float(lowered.get(f"x-codex-{slot}-window-minutes"))
        duration = round(raw_mins) if raw_mins is not None and raw_mins > 0 else None
        window_id, kind, label = codex_window_metadata(slot, duration)
        parsed.append(
            build_window(
                window_id=window_id,
                kind=kind,
                label=label,
                used_percent=used,
                window_duration_mins=duration,
                resets_at=parse_reset_timestamp(lowered.get(f"x-codex-{slot}-reset-at")),
            ),
        )
    _merge("codex", scope_key, parsed, observed_at=_utc(now))


def record_claude_rate_limit(
    scope_key: str,
    *,
    rate_limit_type: str | None,
    utilization: float | None,
    resets_at: float | str | None,
    now: datetime | None = None,
) -> None:
    """Record one Claude Code ``RateLimitEvent`` bucket (0..1 utilization -> 0..100 percent)."""
    if not scope_key or not isinstance(rate_limit_type, str) or not rate_limit_type.strip():
        return
    if (fraction := finite_float(utilization)) is None:
        return
    window_id, kind, label, duration = claude_window_metadata(rate_limit_type)
    window = build_window(
        window_id=window_id,
        kind=kind,
        label=label,
        used_percent=fraction * 100.0,
        window_duration_mins=duration,
        resets_at=parse_reset_timestamp(resets_at),
    )
    _merge("claude_code", scope_key, [window], observed_at=_utc(now))


def get_passive_windows(provider: str, scope_key: str) -> PassiveLimitReading | None:
    """Return the latest passively observed windows for ``(provider, scope_key)``, or None."""
    with _LOCK:
        state = _STORE.get((provider, scope_key))
        if state is None or not state.windows:
            return None
        return PassiveLimitReading(
            windows=sort_windows(list(state.windows.values())),
            observed_at=state.observed_at,
            plan=state.plan,
        )


def clear() -> None:
    """Remove all passively observed rate-limit readings (intended for tests)."""
    with _LOCK:
        _STORE.clear()
