"""Rate limit tests for Stripe webhook endpoint."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

if TYPE_CHECKING:  # pragma: no cover
    import pytest
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


class _EventData:
    def __init__(self, obj: dict) -> None:
        self.object = obj
        # Make object accessible as dict-like
        for key, value in obj.items():
            setattr(self, key, value)


class _Event:
    def __init__(self, event_type: str, payload: dict) -> None:
        self.type = event_type
        self.data = _EventData(payload)
        self.id = "evt_test_123"
        # Add object reference for compatibility
        self.object = payload


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
    """Test that webhook endpoint has rate limiting. 20 requests allowed; 21st should return 429."""
    import backend.routes.webhooks as wh  # noqa: PLC0415

    # Set the webhook secret to a test value
    monkeypatch.setattr(wh, "STRIPE_WEBHOOK_SECRET", "test_secret")

    # Monkeypatch stripe construct_event to raise an exception (invalid signature)
    def _construct_event(_body: bytes, _sig: str, _secret: str) -> _Event:
        # Simulate invalid signature by raising an exception
        msg = "Invalid signature"
        raise ValueError(msg)

    monkeypatch.setattr(wh.stripe.Webhook, "construct_event", _construct_event)  # type: ignore[attr-defined]
    # Stub Supabase insert for webhook_events
    monkeypatch.setattr(wh, "ensure_supabase", lambda: _DummySB())

    client = TestClient(app)

    # Note: TestClient doesn't properly propagate the Stripe-Signature header,
    # so we'll get 400 errors but can still verify rate limiting
    headers = {
        "stripe-signature": "t=1,foo=bar",
        "X-Forwarded-For": "10.22.33.44",
    }
    statuses: list[int] = []
    for _ in range(21):
        r = client.post("/webhooks/stripe", headers=headers, content=b"{}")
        statuses.append(r.status_code)

    # The first 20 requests should return 400 (invalid signature)
    # The 21st request should be rate limited (429)
    assert statuses[:20] == [400] * 20, f"Expected 20 400 responses, got: {statuses[:20]}"
    assert statuses[20] == 429, f"Expected 429 on 21st request, got: {statuses[20]}"
