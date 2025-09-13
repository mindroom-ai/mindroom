"""Rate limit tests for Stripe webhook endpoint."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING


# Stub stripe module if not installed
def _dummy_construct_event(_b: bytes, _s: str, _k: str) -> object:
    """Placeholder so tests can import stripe if missing."""
    return object()


sys.modules.setdefault(
    "stripe",
    types.SimpleNamespace(api_key="", Webhook=types.SimpleNamespace(construct_event=_dummy_construct_event)),
)

if TYPE_CHECKING:  # pragma: no cover
    import pytest
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


class _EventData:
    def __init__(self, obj: dict) -> None:
        self.object = obj


class _Event:
    def __init__(self, event_type: str, payload: dict) -> None:
        self.type = event_type
        self.data = _EventData(payload)
        self.id = "evt_test_123"


class _DummyTable:
    def __init__(self) -> None:
        pass

    def insert(self, *args, **kwargs) -> _DummyTable:  # noqa: ANN002, ANN003, ARG002
        return self

    def execute(self) -> types.SimpleNamespace:
        """Return a dummy result."""
        return types.SimpleNamespace(data=[])


class _DummySB:
    def table(self, name: str) -> _DummyTable:  # noqa: ARG002
        return _DummyTable()


def test_stripe_webhook_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """20 requests allowed; 21st should return 429."""
    import backend.routes.webhooks as wh  # noqa: PLC0415

    # Monkeypatch stripe construct_event to return a benign event
    def _construct_event(_body: bytes, _sig: str, _secret: str) -> _Event:
        return _Event("unhandled.event", {"note": "test"})

    monkeypatch.setattr(wh.stripe.Webhook, "construct_event", _construct_event)  # type: ignore[attr-defined]
    # Stub Supabase insert for webhook_events
    monkeypatch.setattr(wh, "ensure_supabase", lambda: _DummySB())

    client = TestClient(app)

    headers = {"Stripe-Signature": "t=1,foo=bar", "X-Forwarded-For": "10.22.33.44"}
    statuses: list[int] = []
    for _ in range(21):
        r = client.post("/webhooks/stripe", headers=headers, content=b"{}")
        statuses.append(r.status_code)

    assert all(code in (200, 201) for code in statuses[:20])
    assert statuses[20] == 429
