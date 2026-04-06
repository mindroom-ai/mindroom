"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

import mindroom.custom_tools._google_oauth as google_oauth_module
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path


def _default_scope_urls(tool_class: type[Any]) -> list[str]:
    default_scopes = tool_class.DEFAULT_SCOPES
    return list(default_scopes.values()) if isinstance(default_scopes, dict) else default_scopes


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create an isolated runtime context for Google tool wrapper tests."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={},
    )


@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
@pytest.mark.parametrize("tool_class", [GmailTools, GoogleCalendarTools, GoogleSheetsTools])
def test_google_wrappers_reject_isolating_worker_scopes(
    worker_scope: str,
    tool_class: type[Any],
    runtime_paths: RuntimePaths,
) -> None:
    """Google-backed tools are intentionally unsupported for isolating worker scopes."""
    with pytest.raises(ValueError, match="worker_scope=shared"):
        tool_class(
            runtime_paths=runtime_paths,
            credentials_manager=MagicMock(),
            worker_target=resolve_worker_target(
                worker_scope,
                "general",
                execution_identity=None,
                tenant_id=runtime_paths.env_value("CUSTOMER_ID"),
                account_id=runtime_paths.env_value("ACCOUNT_ID"),
            ),
        )


@pytest.mark.parametrize(
    ("tool_class", "expected_scopes"),
    [
        (
            GoogleCalendarTools,
            _default_scope_urls(GoogleCalendarTools),
        ),
        (
            GoogleSheetsTools,
            _default_scope_urls(GoogleSheetsTools),
        ),
    ],
)
def test_google_wrapper_build_credentials_uses_scope_urls_for_default_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[Any],
    expected_scopes: list[str],
    runtime_paths: RuntimePaths,
) -> None:
    """Agno DEFAULT_SCOPES should normalize to a list of scope URLs."""
    monkeypatch.setattr(google_oauth_module, "ensure_tool_deps", lambda *_args, **_kwargs: None)

    tool = object.__new__(tool_class)
    tool._oauth_tool_name = "google"
    tool._runtime_paths = runtime_paths
    creds = tool._build_credentials(
        {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )

    assert creds.scopes == expected_scopes


def test_google_bigquery_preserves_deprecated_boolean_config_aliases(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Persisted deprecated BigQuery flags should still control runtime tool enablement."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "google_bigquery",
        {
            "dataset": "demo_dataset",
            "project": "demo-project",
            "location": "us-central1",
            "enable_run_sql_query": False,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    class _FakeBigQueryClient:
        def __init__(self, *, project: str, credentials: object | None = None) -> None:
            self.project = project
            self.credentials = credentials

    monkeypatch.setattr("agno.tools.google.bigquery.bigquery.Client", _FakeBigQueryClient)

    tool = get_tool_by_name("google_bigquery", runtime_paths, worker_target=None)

    assert "run_sql_query" not in tool.functions
    assert "list_tables" in tool.functions
    assert "describe_table" in tool.functions
