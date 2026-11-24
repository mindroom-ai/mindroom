"""Dependency-free in-memory store for passively observed provider rate-limit windows."""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, fields
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
_MAX_SCOPES = 64
_CLAUDE_MODEL_LABELS = {"oauth_apps": "OAuth apps"}
_CLAUDE_BUCKETS: dict[str, tuple[str, WindowKind, str, int | None]] = {
    "five_hour": ("five_hour", "session", "5-hour window", 300),
    "seven_day": ("seven_day", "weekly", "7-day window", 10080),
    "overage": ("overage", "monthly", "Extra usage", None),
    "extra_usage": ("extra_usage", "monthly", "Extra usage", None),
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
        return {f.name: val for f in fields(self) if (val := getattr(self, f.name)) is not None}


@dataclass(frozen=True, slots=True)
class PassiveLimitReading:
    """Latest passively observed windows for one provider account scope."""

    windows: tuple[PassiveWindow, ...]
    observed_at: datetime


_LOCK = threading.Lock()
_STORE: OrderedDict[tuple[str, str], tuple[dict[str, PassiveWindow], datetime]] = OrderedDict()


def _utc(now: datetime | None) -> datetime:
    current = datetime.now(UTC) if now is None else now
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def format_utc_iso(dt: datetime) -> str:
    """Format a datetime as canonical UTC ISO-8601 with trailing Z."""
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def finite_float(value: object) -> float | None:
    """Parse a finite float from int/float/str, rejecting booleans, NaN, and Inf."""
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        with suppress(ValueError):
            return parsed if math.isfinite(parsed := float(value)) else None
    return None


def parse_reset_timestamp(value: object) -> str | None:
    """Convert unix seconds or an ISO-8601 string to canonical UTC ISO-8601."""
    if (numeric := finite_float(value)) is not None:
        if numeric > 0:
            with suppress(OverflowError, OSError, ValueError):
                return format_utc_iso(datetime.fromtimestamp(numeric, tz=UTC))
    elif isinstance(value, str) and (text := value.strip()):
        with suppress(ValueError):
            return format_utc_iso(datetime.fromisoformat(f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text))
    return None


def build_window(
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
    return PassiveWindow(window_id, kind, label, used, round(100.0 - used, 1), window_duration_mins, resets_at, detail)


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
    if known := _CLAUDE_BUCKETS.get(normalized):
        return known
    if normalized.startswith("seven_day_"):
        suffix = normalized.removeprefix("seven_day_")
        label = _CLAUDE_MODEL_LABELS.get(suffix, suffix.replace("_", " ").title())
        return (normalized, "weekly", f"7-day {label}", 10080)
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
    key = (provider, scope_key)
    with _LOCK:
        merged = _STORE[key][0] if key in _STORE else {}
        merged.update({w.id: w for w in windows})
        _STORE[key] = (merged, observed_at)
        _STORE.move_to_end(key)
        while len(_STORE) > _MAX_SCOPES:
            _STORE.popitem(last=False)


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
        wid, kind, label = codex_window_metadata(slot, duration)
        resets_at = parse_reset_timestamp(lowered.get(f"x-codex-{slot}-reset-at"))
        parsed.append(build_window(wid, kind, label, used, duration, resets_at))
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
    wid, kind, label, duration = claude_window_metadata(rate_limit_type)
    window = build_window(wid, kind, label, fraction * 100.0, duration, parse_reset_timestamp(resets_at))
    _merge("claude_code", scope_key, [window], observed_at=_utc(now))


def get_passive_windows(provider: str, scope_key: str) -> PassiveLimitReading | None:
    """Return the latest passively observed windows for ``(provider, scope_key)``, or None."""
    key = (provider, scope_key)
    with _LOCK:
        state = _STORE.get(key)
        if state is None or not state[0]:
            return None
        _STORE.move_to_end(key)
        return PassiveLimitReading(windows=sort_windows(list(state[0].values())), observed_at=state[1])


def clear() -> None:
    """Remove all passively observed rate-limit readings (intended for tests)."""
    with _LOCK:
        _STORE.clear()
