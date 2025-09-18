"""Prometheus metrics helpers and FastAPI instrumentation wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator

if TYPE_CHECKING:
    from fastapi import FastAPI


REGISTRY = CollectorRegistry(auto_describe=True)

_instrumentator = Instrumentator(
    registry=REGISTRY,
    excluded_handlers={"/metrics"},
    should_instrument_requests_inprogress=False,
    should_group_status_codes=False,
)

# Counter for all authentication events (both user and admin paths).
# Labels are deliberately coarse to avoid unbounded cardinality.
_AUTH_EVENTS = Counter(
    "mindroom_auth_events_total",
    "Authentication outcomes grouped by actor type.",
    ("actor", "outcome"),
    registry=REGISTRY,
)

# Counter for explicit admin verification attempts.
_ADMIN_VERIFICATIONS = Counter(
    "mindroom_admin_verifications_total",
    "Admin verification outcomes by status.",
    ("outcome",),
    registry=REGISTRY,
)

# Gauge that tracks how many IPs are currently blocked by the in-memory guard.
_BLOCKED_IPS = Gauge(
    "mindroom_blocked_ips",
    "Current number of IP addresses blocked due to auth failures.",
    registry=REGISTRY,
)


def instrument_app(app: "FastAPI") -> None:
    """Attach Prometheus instrumentation to the FastAPI app exactly once."""

    if getattr(app.state, "_prometheus_instrumented", False):
        return

    _instrumentator.instrument(app)
    _instrumentator.expose(app, include_in_schema=False, should_gzip=True)
    app.state._prometheus_instrumented = True


def record_auth_event(*, actor: str, outcome: str) -> None:
    """Increment the auth event counter for the given actor/outcome pair."""

    _AUTH_EVENTS.labels(actor=actor, outcome=outcome).inc()


def record_admin_verification(outcome: str) -> None:
    """Increment the admin verification counter for the supplied outcome."""

    _ADMIN_VERIFICATIONS.labels(outcome=outcome).inc()


def set_blocked_ip_count(count: int) -> None:
    """Update the gauge representing currently blocked IP addresses."""

    _BLOCKED_IPS.set(count)


def reset_security_metrics() -> None:
    """Reset metrics for unit tests without touching the global registry."""

    _AUTH_EVENTS._metrics.clear()  # type: ignore[attr-defined]
    _ADMIN_VERIFICATIONS._metrics.clear()  # type: ignore[attr-defined]
    _BLOCKED_IPS.set(0)


__all__ = [
    "REGISTRY",
    "instrument_app",
    "record_auth_event",
    "record_admin_verification",
    "set_blocked_ip_count",
    "reset_security_metrics",
    "get_auth_metric",
    "get_admin_metric",
    "get_blocked_ip_count",
]


def get_auth_metric(*, actor: str, outcome: str) -> float:
    """Return the counter value for a specific auth event tuple."""

    return float(_AUTH_EVENTS.labels(actor=actor, outcome=outcome)._value.get())  # type: ignore[attr-defined]


def get_admin_metric(outcome: str) -> float:
    """Return the counter value for a given admin verification outcome."""

    return float(_ADMIN_VERIFICATIONS.labels(outcome=outcome)._value.get())  # type: ignore[attr-defined]


def get_blocked_ip_count() -> float:
    """Return the current blocked IP gauge value."""

    return float(_BLOCKED_IPS._value.get())  # type: ignore[attr-defined]
