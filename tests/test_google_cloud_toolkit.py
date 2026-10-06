"""Tests for the shared Google Cloud toolkit base."""

# ruff: noqa: D103

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from google.api_core import exceptions as google_exceptions
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.custom_tools.google_service import GoogleCloudToolkit
from mindroom.oauth.google_cloud import google_cloud_oauth_provider
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _valid_credentials(token: str = "valid-access-token") -> GoogleOAuthCredentials:  # noqa: S107
    return GoogleOAuthCredentials(
        token=token,
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("scope",),
        expiry=datetime(2100, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    )


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    get_runtime_credentials_manager(paths).save_credentials(
        "google_oauth_client",
        {"client_id": "client-id", "client_secret": "client-secret", "_source": "ui"},
    )
    return paths


class _ProbeCloudTools(GoogleCloudToolkit):
    _oauth_provider = google_cloud_oauth_provider()
    _oauth_tool_name = "probe_cloud"

    def __init__(self, *, error: Exception | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        self.built_with: list[object] = []
        self.error = error
        super().__init__(name="probe_cloud", tools=[self.probe_cloud], **kwargs)

    def probe_cloud(self) -> str:
        """Return the cached client name, or a sanitized error."""
        client = self._google_cloud_client("probe", self._build_client)
        if self.error is not None:
            return self._google_cloud_error_result("Probe", "probe", self.error)
        return json.dumps({"client": client})

    def _build_client(self, credentials: object) -> str:
        self.built_with.append(credentials)
        return f"client-{len(self.built_with)}"


def _tool(tmp_path: Path, **kwargs: Any) -> _ProbeCloudTools:  # noqa: ANN401
    paths = _runtime_paths(tmp_path)
    return _ProbeCloudTools(
        runtime_paths=paths,
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=None,
        **kwargs,
    )


def test_missing_connection_returns_google_cloud_connect_instruction(tmp_path: Path) -> None:
    result = json.loads(_tool(tmp_path).probe_cloud())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_cloud"


def test_client_is_built_once_with_the_authenticated_credentials(tmp_path: Path) -> None:
    creds = _valid_credentials()
    tool = _tool(tmp_path, creds=creds)

    assert json.loads(tool.probe_cloud()) == {"client": "client-1"}
    assert json.loads(tool.probe_cloud()) == {"client": "client-1"}
    assert len(tool.built_with) == 1
    assert tool.built_with[0].token == creds.token


def test_client_is_rebuilt_when_credentials_change(tmp_path: Path) -> None:
    tool = _tool(tmp_path, creds=_valid_credentials())
    tool.probe_cloud()

    tool.creds = _valid_credentials(token="other-token")  # noqa: S106
    tool.probe_cloud()

    assert len(tool.built_with) == 2


def test_non_auth_errors_report_status_without_provider_text(tmp_path: Path) -> None:
    tool = _tool(tmp_path, creds=_valid_credentials(), error=google_exceptions.NotFound("provider-controlled-secret"))

    result = tool.probe_cloud()

    assert json.loads(result) == {"error": "Probe request failed (HTTP 404)"}
    assert "provider-controlled" not in result


def test_final_401_on_stored_connection_requires_reconnect(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(paths)
    provider = google_cloud_oauth_provider()
    publish_oauth_credentials(
        provider,
        {
            "token": "stored-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
        credentials_manager=manager,
        worker_target=None,
    )
    tool = _ProbeCloudTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=None,
        error=google_exceptions.Unauthenticated("provider-controlled-401"),
    )

    result = tool.probe_cloud()
    payload = json.loads(result)

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "access_rejected"
    assert "provider-controlled" not in result
