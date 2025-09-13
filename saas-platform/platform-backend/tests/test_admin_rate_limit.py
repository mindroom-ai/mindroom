"""Tests for admin endpoint rate limiting (proxy-safe)."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

import backend.routes.admin as admin_mod  # noqa: E402
from backend.deps import verify_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


class _DummyResult:
    def __init__(self, data: list | None = None, count: int = 0) -> None:
        self.data = data or []
        self.count = count


class _DummyQuery:
    def select(self, *args, **kwargs) -> _DummyQuery:  # noqa: ANN002, ANN003, ARG002
        return self

    def eq(self, *args, **kwargs) -> _DummyQuery:  # noqa: ANN002, ANN003, ARG002
        return self

    def order(self, *args, **kwargs) -> _DummyQuery:  # noqa: ANN002, ANN003, ARG002
        return self

    def limit(self, *args, **kwargs) -> _DummyQuery:  # noqa: ANN002, ANN003, ARG002
        return self

    def range(self, *args, **kwargs) -> _DummyQuery:  # noqa: ANN002, ANN003, ARG002
        return self

    def execute(self) -> _DummyResult:
        return _DummyResult()


class _DummySB:
    def table(self, name: str) -> _DummyQuery:  # noqa: ARG002
        return _DummyQuery()


def _override_verify_admin() -> dict[str, str]:
    return {"user_id": "admin-user", "email": "admin@example.com"}


def test_admin_stats_rate_limit() -> None:
    """30 requests allowed; 31st returns 429."""
    # Inject dummy DB client and admin override
    admin_mod.ensure_supabase = lambda: _DummySB()  # type: ignore[assignment]
    app.dependency_overrides[verify_admin] = _override_verify_admin

    client = TestClient(app)
    statuses = []
    for _ in range(31):  # limit is 30/min
        r = client.get("/admin/stats")
        statuses.append(r.status_code)

    assert all(code == 200 for code in statuses[:30])
    assert statuses[30] == 429
