"""Google BigQuery tools backed by the shared read-only Google Cloud connection."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import BadRequest, GoogleAPICallError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleCloudToolkit
from mindroom.oauth.google_cloud import google_cloud_oauth_provider
from mindroom.tool_system.metadata import coerce_optional_finite_number

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_SERVICE_NAME = "Google BigQuery"
_READ_ONLY_ERROR = "Only a single read-only SELECT query is allowed"
_MAX_ERROR_DETAIL = 500
_DEFAULT_MAX_ROWS = 100
_MAX_ROWS_LIMIT = 1000
_MAX_LISTED_TABLES = 1000
_READ_ONLY_KEYWORDS = frozenset({"select", "with"})
_MAX_STRING_PREFIX = 2
# The SQL lexer ends a single-line comment at either kind of line break.
_LINE_END = re.compile(r"[\r\n]")


def _has_raw_prefix(sql: str, index: int) -> bool:
    """Return whether the quote at index opens a raw string (an r, rb or br prefix)."""
    start = index
    while start > 0 and index - start < _MAX_STRING_PREFIX and sql[start - 1].isalpha():
        start -= 1
    if start > 0 and (sql[start - 1].isalnum() or sql[start - 1] == "_"):
        return False
    return sql[start:index].lower() in {"r", "rb", "br"}


def _skip_quoted(sql: str, index: int) -> int | None:
    """Return the index after the quoted token starting at index, or None if unterminated or ambiguous.

    A backslash before a quote is ambiguous inside raw strings and backtick identifiers, because the
    SQL lexer and a literal reading disagree about where the token ends, so such SQL is refused.
    """
    quote = sql[index]
    delimiter = quote * 3 if quote != "`" and sql.startswith(quote * 3, index) else quote
    literal_backslash = quote == "`" or _has_raw_prefix(sql, index)
    position = index + len(delimiter)
    while position < len(sql):
        if sql[position] == "\\":
            if literal_backslash and sql[position + 1 : position + 2] == quote:
                return None
            position += 2
        elif sql.startswith(delimiter, position):
            return position + len(delimiter)
        else:
            position += 1
    return None


def _statement_text(sql: str) -> str | None:
    """Return sql with comments and quoted tokens blanked, or None if a token is unterminated."""
    pieces: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in {"'", '"', "`"}:
            end = _skip_quoted(sql, index)
            if end is None:
                return None
            pieces.append(" ")
            index = end
        elif sql.startswith("--", index) or char == "#":
            line_end = _LINE_END.search(sql, index)
            index = len(sql) if line_end is None else line_end.end()
            pieces.append(" ")
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                return None
            index = end + 2
            pieces.append(" ")
        else:
            pieces.append(char)
            index += 1
    return "".join(pieces)


def _is_read_only_select(sql: str) -> bool:
    """Return whether sql is exactly one statement starting with SELECT or WITH."""
    text = _statement_text(sql)
    if text is None:
        return False
    body = text.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if not body or ";" in body:
        return False
    first_word = body.split(None, 1)[0].lower()
    return first_word in _READ_ONLY_KEYWORDS


def _coerce_max_rows(value: object) -> int:
    """Return max_rows as a whole number in range.

    Config and the dashboard may deliver a whole float or numeric text for a number field, and blank means the default.
    """
    msg = f"Google BigQuery max_rows must be a whole number between 1 and {_MAX_ROWS_LIMIT}"
    try:
        number = coerce_optional_finite_number(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(msg) from exc
    if number is None:
        return _DEFAULT_MAX_ROWS
    if isinstance(number, float) or not 1 <= number <= _MAX_ROWS_LIMIT:
        raise ValueError(msg)
    return number


class GoogleBigQueryTools(GoogleCloudToolkit):
    """List tables, describe schemas, and run read-only SQL in one BigQuery dataset."""

    _oauth_provider = google_cloud_oauth_provider()
    _oauth_tool_name = "google_bigquery"

    def __init__(
        self,
        *,
        dataset: str,
        project: str,
        location: str,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        list_tables: bool = True,
        describe_table: bool = True,
        run_sql_query: bool = True,
        all: bool = False,  # noqa: A002
        max_rows: float = _DEFAULT_MAX_ROWS,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.dataset = dataset
        self.project = project
        self.location = location
        self.max_rows = _coerce_max_rows(max_rows)
        tools = []
        if all or list_tables:
            tools.append(self.list_tables)
        if all or describe_table:
            tools.append(self.describe_table)
        if all or run_sql_query:
            tools.append(self.run_sql_query)
        super().__init__(
            name="google_bigquery",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def _client(self) -> Any:  # noqa: ANN401
        from google.cloud import bigquery  # noqa: PLC0415

        return self._google_cloud_client(
            "bigquery",
            lambda credentials: bigquery.Client(project=self.project, credentials=credentials, location=self.location),
        )

    def list_tables(self) -> str:
        """List table names in the configured dataset.

        Returns:
            JSON with up to 1000 table IDs.

        """
        try:
            tables = self._client().list_tables(f"{self.project}.{self.dataset}", max_results=_MAX_LISTED_TABLES)
            return json.dumps({"tables": [table.table_id for table in tables]})
        except GoogleAPICallError as exc:
            return self._google_cloud_error_result(_SERVICE_NAME, "list_tables", exc)

    def describe_table(self, table_id: str) -> str:
        """Describe one table in the configured dataset.

        Args:
            table_id: Table name inside the configured dataset.

        Returns:
            JSON with the table description and each column's name, type, mode and description.

        """
        try:
            table = self._client().get_table(f"{self.project}.{self.dataset}.{table_id}")
        except GoogleAPICallError as exc:
            return self._google_cloud_error_result(_SERVICE_NAME, "describe_table", exc)
        columns = [
            {"name": field.name, "type": field.field_type, "mode": field.mode, "description": field.description}
            for field in table.schema
        ]
        return json.dumps({"table_description": table.description or "", "columns": columns})

    def run_sql_query(self, query: str) -> str:
        """Run one read-only GoogleSQL SELECT query; unqualified tables resolve in the configured dataset.

        Args:
            query: A single SELECT or WITH statement.

        Returns:
            JSON with columns, at most max_rows rows, and whether more rows were available.

        """
        if not _is_read_only_select(query):
            return json.dumps({"error": _READ_ONLY_ERROR})
        from google.cloud import bigquery  # noqa: PLC0415

        job_config = bigquery.QueryJobConfig(default_dataset=f"{self.project}.{self.dataset}")
        try:
            rows = self._client().query_and_wait(
                query,
                job_config=job_config,
                location=self.location,
                max_results=self.max_rows + 1,
            )
            columns = [field.name for field in rows.schema]
            records = [dict(row) for row in rows]
        except BadRequest as exc:
            # The message describes the requester's own SQL and data, which this tool already returns.
            detail = str(exc.message)[:_MAX_ERROR_DETAIL]
            error = self._google_cloud_error_result(_SERVICE_NAME, "run_sql_query", exc)
            return json.dumps({"error": f"{json.loads(error)['error']}: {detail}"})
        except GoogleAPICallError as exc:
            return self._google_cloud_error_result(_SERVICE_NAME, "run_sql_query", exc)
        truncated = len(records) > self.max_rows
        return json.dumps(
            {"columns": columns, "rows": records[: self.max_rows], "truncated": truncated},
            default=str,
        )
