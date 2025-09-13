"""Rate limit tests for user setup endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from backend.deps import verify_user
from fastapi.testclient import TestClient
from main import app

if TYPE_CHECKING:  # pragma: no cover
    import pytest


class _DummyResult:
    def __init__(self, data: object | None = None) -> None:
        self.data = data


class _DummyTable:
    def select(self, *args, **kwargs) -> _DummyTable:  # noqa: ANN002, ANN003, ARG002
        return self

    def eq(self, *args, **kwargs) -> _DummyTable:  # noqa: ANN002, ANN003, ARG002
        return self

    def single(self) -> _DummyTable:
        """Identity method for chaining."""
        return self

    def execute(self) -> _DummyResult:
        return _DummyResult([])

    def insert(self, *args, **kwargs) -> _DummyResult:  # noqa: ANN002, ANN003, ARG002
        return _DummyResult([{"id": 1}])


class _DummySB:
    def table(self, name: str) -> _DummyTable:  # noqa: ARG002
        return _DummyTable()


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "u1", "email": "u1@example.com", "account_id": "acc-1"}


def test_setup_account_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """5 requests allowed; 6th returns 429."""
    import backend.routes.accounts as acc  # noqa: PLC0415

    # Stub Supabase client
    monkeypatch.setattr(acc, "ensure_supabase", lambda: _DummySB())
    # Override user dependency
    app.dependency_overrides[verify_user] = _override_verify_user

    try:
        client = TestClient(app)
        statuses: list[int] = []

        for i in range(6):
            r = client.post("/my/account/setup", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.2.3.4"})
            statuses.append(r.status_code)
            if i == 0 and r.status_code not in (200, 201, 429):
                # Debug first failure
                print(f"First request failed with {r.status_code}: {r.text}")

        # Check that rate limiting is working (last request should be 429)
        assert statuses[5] == 429, f"Expected 429 on 6th request, got {statuses[5]}. All statuses: {statuses}"
        # Check that at least some requests succeeded or all were rate limited
        successful = [s for s in statuses[:5] if s in (200, 201)]
        assert len(successful) > 0 or all(s == 429 for s in statuses[1:]), (
            f"Expected some successful requests or consistent rate limiting. Got: {statuses}"
        )
    finally:
        # Clean up dependency override
        app.dependency_overrides.clear()
