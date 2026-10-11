"""API request validation errors answer 422 without repeating the rejected request values."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner_app
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_JSON_HEADERS = {"content-type": "application/json"}
_RUNNER_TOKEN = "test-runner-token"  # noqa: S105 - isolated test credential


# Python's JSON parser accepts NaN, Infinity, and out-of-range literals, which JSON responses cannot render.
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400", "123"])
def test_dashboard_api_rejects_a_wrongly_typed_field_with_422(test_client: TestClient, value: str) -> None:
    """Every wrongly typed value, including non-finite numbers, gets the same 422 detail without the value."""
    response = test_client.post("/api/auth/session", content=f'{{"api_key": {value}}}', headers=_JSON_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "string_type", "loc": ["body", "api_key"], "msg": "Input should be a valid string"}],
    }


def test_dashboard_api_validation_errors_do_not_echo_the_request_body(test_client: TestClient) -> None:
    """A missing field names the field without repeating the submitted body."""
    response = test_client.post("/api/auth/session", json={"api_kee": "body-sentinel"})

    assert response.status_code == 422
    assert "body-sentinel" not in response.text
    assert response.json() == {"detail": [{"type": "missing", "loc": ["body", "api_key"], "msg": "Field required"}]}


def test_dashboard_api_validation_errors_keep_their_context(test_client: TestClient) -> None:
    """Errors that carry context still include it."""
    response = test_client.post("/api/auth/session", content='{"api_key": ', headers=_JSON_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "json_invalid",
                "loc": ["body", 12],
                "msg": "JSON decode error",
                "ctx": {"error": "Expecting value"},
            },
        ],
    }


def test_sandbox_runner_rejects_a_non_finite_field_with_422(tmp_path: Path) -> None:
    """The sandbox runner answers a non-finite value in a typed body with 422 instead of failing to render it."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(sandbox_runner_app.app, paths, config=Config(), runner_token=_RUNNER_TOKEN)

    response = TestClient(sandbox_runner_app.app).post(
        "/api/sandbox-runner/execute/cancel",
        content='{"request_id": NaN}',
        headers={**_JSON_HEADERS, "x-mindroom-sandbox-token": _RUNNER_TOKEN},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "string_type", "loc": ["body", "request_id"], "msg": "Input should be a valid string"}],
    }
