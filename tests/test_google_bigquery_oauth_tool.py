"""Tests for the Google BigQuery tool on the shared Google Cloud connection."""

# ruff: noqa: D103

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_bigquery import GoogleBigQueryTools, _is_read_only_select
from mindroom.tool_system.catalog import TOOL_METADATA

if TYPE_CHECKING:
    from pathlib import Path


def _valid_credentials() -> GoogleOAuthCredentials:
    return GoogleOAuthCredentials(
        token="valid-access-token",  # noqa: S106
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("scope",),
        expiry=datetime(2100, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    )


class _FakeRows(list):
    def __init__(self, rows: list[dict[str, Any]], columns: list[str]) -> None:
        super().__init__(rows)
        self.schema = [SimpleNamespace(name=name) for name in columns]


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = [{"n": 1}, {"n": 2}, {"n": 3}]
        self.error: Exception | None = None

    def query_and_wait(self, sql: str, **kwargs: Any) -> _FakeRows:  # noqa: ANN401
        if self.error is not None:
            raise self.error
        self.queries.append({"sql": sql, **kwargs})
        limit = kwargs["max_results"]
        return _FakeRows(self.rows[:limit], ["n"])

    def list_tables(self, dataset: str, **kwargs: Any) -> list[SimpleNamespace]:  # noqa: ANN401
        if self.error is not None:
            raise self.error
        self.queries.append({"list_tables": dataset, **kwargs})
        return [SimpleNamespace(table_id="events"), SimpleNamespace(table_id="users")]

    def get_table(self, table_ref: str) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        # The real client parses the ID first and raises ValueError for a malformed one.
        bigquery.TableReference.from_string(table_ref)
        self.queries.append({"get_table": table_ref})
        field = SimpleNamespace(name="n", field_type="INTEGER", mode="NULLABLE", description="count")
        return SimpleNamespace(description="Event counts", schema=[field])


def _runtime_paths(tmp_path: Path) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )


def _tool(tmp_path: Path, **kwargs: Any) -> tuple[GoogleBigQueryTools, _FakeClient]:  # noqa: ANN401
    tool = GoogleBigQueryTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        dataset="analytics",
        project="example-project",
        location="US",
        **kwargs,
    )
    client = _FakeClient()
    tool.service = {"bigquery": client}
    return tool, client


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from t;",
        "  -- leading comment\nWITH a AS (SELECT 1) SELECT * FROM a",
        "/* block; comment */ SELECT ';' AS semicolon, \"x;y\" AS s, `odd;name` FROM t",
        "# hash comment\nSELECT '''multi; line''' AS s",
        "SELECT r'C:\\path;x' AS s",
        "SELECT 'it\\'s; fine' AS s",
        "SELECT '''a\\'''; DELETE FROM t; ''' AS s",
        "SELECT(1)",
        "SELECT*FROM t",
        "(SELECT 1) UNION ALL (SELECT 2)",
        "( WITH a AS (SELECT 1) SELECT * FROM a )",
    ],
)
def test_read_only_guard_accepts_single_select_statements(sql: str) -> None:
    assert _is_read_only_select(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "DELETE FROM t WHERE true",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1 WHERE true",
        "MERGE t USING s ON false WHEN NOT MATCHED THEN INSERT ROW",
        "DROP TABLE t",
        "CREATE TEMP FUNCTION f() AS (1); SELECT f()",
        "SELECT 1; DELETE FROM t WHERE true",
        "EXPORT DATA OPTIONS(uri='gs://b/*') AS SELECT 1",
        "BEGIN SELECT 1; END",
        "SELECT 1 /* unterminated",
        "SELECT 'unterminated",
        # Each of these ends a token differently under another reading and would hide a second statement.
        "SELECT r'\\' , '; DELETE FROM t; --'",
        "SELECT `a\\` , `; DELETE FROM t; --`",
        "SELECT 1 -- note\r; DELETE FROM t",
        "(DELETE FROM t WHERE true)",
        "SELECTX 1",
        "(SELECT 1); DELETE FROM t WHERE true",
    ],
)
def test_read_only_guard_rejects_everything_else(sql: str) -> None:
    assert _is_read_only_select(sql) is False


def test_run_sql_query_refuses_writes_before_calling_bigquery(tmp_path: Path) -> None:
    tool, client = _tool(tmp_path)

    result = json.loads(tool.run_sql_query("DELETE FROM events WHERE true"))

    assert result == {"error": "Only a single read-only SELECT query is allowed"}
    assert client.queries == []


def test_run_sql_query_uses_default_dataset_location_and_row_cap(tmp_path: Path) -> None:
    tool, client = _tool(tmp_path, max_rows=2)

    result = json.loads(tool.run_sql_query("SELECT n FROM events"))

    assert result == {"columns": ["n"], "rows": [{"n": 1}, {"n": 2}], "truncated": True}
    call = client.queries[0]
    assert call["sql"] == "SELECT n FROM events"
    assert call["location"] == "US"
    assert call["max_results"] == 3
    assert str(call["job_config"].default_dataset) == "example-project.analytics"


def test_run_sql_query_reports_bad_request_message_truncated(tmp_path: Path) -> None:
    tool, client = _tool(tmp_path)
    client.error = google_exceptions.BadRequest("Unrecognized name: missing at [1:8]" + "x" * 1000)

    result = json.loads(tool.run_sql_query("SELECT missing FROM events"))

    assert result["error"].startswith("Google BigQuery request failed (HTTP 400): Unrecognized name: missing")
    assert len(result["error"]) <= 600


def test_run_sql_query_hides_other_provider_text(tmp_path: Path) -> None:
    tool, client = _tool(tmp_path)
    client.error = google_exceptions.Forbidden("provider-controlled-secret")

    result = tool.run_sql_query("SELECT 1")

    assert json.loads(result) == {"error": "Google BigQuery request failed (HTTP 403)"}


@pytest.mark.parametrize("operation", ["list_tables", "describe_table", "run_sql_query"])
def test_retry_error_reports_status_only_without_provider_text(tmp_path: Path, operation: str) -> None:
    tool, client = _tool(tmp_path)
    client.error = google_exceptions.RetryError("Deadline exceeded", cause=ValueError("provider-controlled-secret"))
    calls = {
        "list_tables": lambda: tool.list_tables(),
        "describe_table": lambda: tool.describe_table("events"),
        "run_sql_query": lambda: tool.run_sql_query("SELECT 1"),
    }

    result = calls[operation]()

    assert json.loads(result) == {"error": "Google BigQuery request failed"}
    assert "provider-controlled" not in result


@pytest.mark.parametrize("table_id", ["events.other", "a.b.c"])
def test_describe_table_reports_malformed_table_id(tmp_path: Path, table_id: str) -> None:
    tool, client = _tool(tmp_path)

    result = json.loads(tool.describe_table(table_id))

    assert result == {"error": "Invalid table_id"}
    assert client.queries == []


def test_list_tables_and_describe_table(tmp_path: Path) -> None:
    tool, client = _tool(tmp_path)

    assert json.loads(tool.list_tables()) == {"tables": ["events", "users"]}
    assert json.loads(tool.describe_table("events")) == {
        "table_description": "Event counts",
        "columns": [{"name": "n", "type": "INTEGER", "mode": "NULLABLE", "description": "count"}],
    }
    assert client.queries[0]["list_tables"] == "example-project.analytics"
    assert client.queries[1] == {"get_table": "example-project.analytics.events"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(50, 50), (50.0, 50), ("50", 50), (" 7 ", 7), ("", 100), (None, 100), (1000, 1000)],
)
def test_max_rows_accepts_the_numbers_config_and_dashboard_send(tmp_path: Path, raw: object, expected: int) -> None:
    tool, _client = _tool(tmp_path, max_rows=raw)

    assert tool.max_rows == expected


@pytest.mark.parametrize("raw", [0, -1, 1001, 2.5, "abc", "inf", True, float("nan")])
def test_max_rows_rejects_values_outside_one_to_one_thousand(tmp_path: Path, raw: object) -> None:
    with pytest.raises(ValueError, match="max_rows must be a whole number between 1 and 1000"):
        _tool(tmp_path, max_rows=raw)


def test_config_flags_control_registered_functions(tmp_path: Path) -> None:
    tool, _client = _tool(tmp_path, list_tables=False, describe_table=False)

    assert set(tool.functions) == {"run_sql_query"}


def test_missing_connection_returns_google_cloud_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleBigQueryTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        dataset="analytics",
        project="example-project",
        location="US",
    )

    result = json.loads(tool.list_tables())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_cloud"


def test_metadata_uses_shared_google_cloud_connection() -> None:
    metadata = TOOL_METADATA["google_bigquery"]

    assert metadata.auth_provider == "google_cloud"
    assert metadata.setup_type.value == "oauth"
    assert metadata.requires_primary_runtime is True
    assert "credentials" not in {field.name for field in metadata.config_fields or []}
