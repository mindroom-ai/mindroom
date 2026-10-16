"""Request validation errors answer 422 without repeating the rejected request values."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from backend.deps import verify_user
from fastapi.testclient import TestClient
from main import app

_JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return a client signed in as a test account."""
    app.dependency_overrides[verify_user] = lambda: {"account_id": "acc_test_123", "email": "test@example.com"}
    yield TestClient(app)
    app.dependency_overrides.clear()


# Python's JSON parser accepts NaN, Infinity, and out-of-range literals, which JSON responses cannot render.
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400", "123"])
def test_wrongly_typed_field_answers_422_without_the_value(client: TestClient, value: str) -> None:
    """Every wrongly typed value, including non-finite numbers, gets the same 422 detail without the value."""
    response = client.post("/stripe/checkout", content=f'{{"tier": {value}}}', headers=_JSON_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "string_type", "loc": ["body", "tier"], "msg": "Input should be a valid string"}],
    }


def test_validation_errors_do_not_echo_the_request_body(client: TestClient) -> None:
    """A missing field names the field without repeating the submitted body."""
    response = client.post("/stripe/checkout", json={"tierr": "body-sentinel"})

    assert response.status_code == 422
    assert "body-sentinel" not in response.text
    assert response.json() == {"detail": [{"type": "missing", "loc": ["body", "tier"], "msg": "Field required"}]}


def test_validation_errors_keep_their_context(client: TestClient) -> None:
    """Errors that carry context still include it."""
    response = client.post("/stripe/checkout", content='{"tier": ', headers=_JSON_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "json_invalid",
                "loc": ["body", 9],
                "msg": "JSON decode error",
                "ctx": {"error": "Expecting value"},
            },
        ],
    }
