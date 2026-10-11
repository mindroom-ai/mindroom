"""Personal usage uses signed identity while organization routes use separate auth."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom.api import main
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import _publish_config, _use_runtime_auth_settings
from tests.test_usage_stats_private import ALIAS, ALICE, BOB, private_usage_data

if TYPE_CHECKING:
    from pathlib import Path


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[str, str] | None = None,
) -> tuple[TestClient, dict[str, dict[str, str]]]:
    data = private_usage_data(tmp_path)
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    paths = replace(
        data.paths,
        process_env={
            **data.paths.process_env,
            **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
            "MINDROOM_CONNECTIONS_AGENT": "code",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Matrix-User",
            **(overrides or {}),
        },
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, data.config.model_dump(mode="json"))
    _use_runtime_auth_settings(main.app)
    headers = {
        name: {
            "X-Trusted-User": name,
            "X-Trusted-Jwt": _trusted_upstream_jwt(key, user_id=name, matrix_user_id=requester),
        }
        for name, requester in (("alice", ALIAS), ("bob", BOB))
    }
    return TestClient(main.app), headers


@pytest.mark.parametrize("include_daily", [False, True])
@pytest.mark.parametrize(("user", "tokens", "agents"), [("alice", 150, {"code", "helper"}), ("bob", 70, {"code"})])
def test_personal_private_usage_uses_signed_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_daily: bool,
    user: str,
    tokens: int,
    agents: set[str],
) -> None:
    """Signed identity scopes personal usage; contradictory identity headers are rejected."""
    client, headers = _client(tmp_path, monkeypatch)
    response = client.get(
        "/api/usage/me/private-agents",
        headers={**headers[user], "X-Matrix-User": ALICE},
    )
    assert response.status_code == 401
    before_scan = datetime.now(UTC)
    response = client.get(
        "/api/usage/me/private-agents",
        headers=headers[user],
        params={"include_daily": str(include_daily).lower()},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    report = response.json()
    assert report["schema_version"] == 1
    assert before_scan <= datetime.fromisoformat(report["generated_at"]) <= datetime.now(UTC)
    assert report["scope"] == "self"
    assert report["totals"]["total_tokens"] == tokens
    assert {row["agent_name"] for row in report["private_agent_breakdown"]} == agents
    assert "user_breakdown" not in report
    assert all("user_id" not in row for row in report["private_agent_breakdown"])
    assert ("daily_breakdown" in report) is include_daily
    assert str(tmp_path) not in response.text


def test_private_usage_api_rejects_requester_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The public API cannot select a different owner via query parameters."""
    client, headers = _client(tmp_path, monkeypatch)
    response = client.get("/api/usage/me/private-agents", headers=headers["bob"], params={"user_id": ALICE})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "false", "MINDROOM_API_KEY": "admin-key"},
        {"MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "false"},
        {"MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM": ""},
    ],
)
def test_private_usage_api_requires_verified_matrix_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    """Admin keys and unsigned requester headers cannot impersonate a personal user."""
    client, headers = _client(tmp_path, monkeypatch, overrides=overrides)
    response = client.get(
        "/api/usage/me/private-agents",
        headers={**headers["bob"], "Authorization": "Bearer admin-key"},
    )
    assert response.status_code == 403


def test_private_usage_api_rejects_missing_signed_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forged upstream user header alone never grants personal usage access."""
    client, _ = _client(tmp_path, monkeypatch)
    response = client.get("/api/usage/me/private-agents", headers={"X-Trusted-User": "alice"})
    assert response.status_code == 401
