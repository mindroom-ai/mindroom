"""Tests for native Wave 6 Batch 2 database toolkits (sql, postgres, redshift, neo4j)."""

from __future__ import annotations

import json
import socket
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any, cast

import neo4j
import psycopg
import psycopg.conninfo
import psycopg.pq
import pytest
import redshift_connector

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.registry import builtin_tool_registry

if TYPE_CHECKING:
    from pathlib import Path

_DUMMY_SECRET = "test-cred-value"  # noqa: S105 - synthetic test fixture


class _FakeCursor:
    def __init__(self, responses: list[tuple[list[tuple[str]] | None, list[Any]]]) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[object, object]] = []
        self.description: list[tuple[str]] | None = None
        self.statusmessage = "OK"
        self._rows: list[Any] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        self.executed.append((query, params))
        if self._responses:
            self.description, self._rows = self._responses.pop(0)
        else:
            self.description, self._rows = None, []

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:  # noqa: ANN401
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Any:  # noqa: ANN401
        return iter(self._rows)


class _FakeConnection:
    def __init__(self, responses: list[tuple[list[tuple[str]] | None, list[Any]]]) -> None:
        self.cursor_obj = _FakeCursor(responses)
        self.closed = False
        self.read_only = False
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _FakeNeo4jResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:  # noqa: ANN401
        return iter(self._rows)

    def data(self) -> list[dict[str, object]]:
        return list(self._rows)


class _FakeNeo4jSession:
    def __enter__(self) -> _FakeNeo4jSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, query: object) -> _FakeNeo4jResult:
        text = query.text if isinstance(query, neo4j.Query) else str(query)
        if "db.labels" in text:
            return _FakeNeo4jResult([{"label": "Person"}])
        if "db.relationshipTypes" in text:
            return _FakeNeo4jResult([{"relationshipType": "KNOWS"}])
        return _FakeNeo4jResult([{"n": 1}])


class _FakeNeo4jDriver:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _FakeNeo4jDriver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def session(self, *, database: str) -> _FakeNeo4jSession:
        assert database == "analytics"
        return _FakeNeo4jSession()


def test_sql_end_to_end_and_explicit_none_toggles(tmp_path: Path) -> None:
    """SQLTools inspects tables, queries with limit semantics, commits DML, and respects None toggles."""
    db_file = tmp_path / "shop.sqlite"
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)")
        conn.execute("INSERT INTO orders VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        conn.commit()

    sql_cls = cast("Any", builtin_tool_registry().factory("sql")())
    tool = sql_cls(db_url=f"sqlite:///{db_file}")
    try:
        assert json.loads(tool.sql_inspect("list_tables")) == ["orders"]
        cols = json.loads(tool.sql_inspect("describe_table", table_name="orders"))
        assert [c["name"] for c in cols] == ["id", "item"]
        assert len(json.loads(tool.run_sql_query("SELECT * FROM orders ORDER BY id", limit=2))) == 2
        assert len(json.loads(tool.run_sql_query("SELECT * FROM orders ORDER BY id", limit=None))) == 3
        assert json.loads(tool.run_sql_query("SELECT * FROM orders", limit=0)) == []
        assert json.loads(tool.run_sql_query("INSERT INTO orders VALUES (4, 'd')")) == []
        assert len(json.loads(tool.run_sql_query("SELECT * FROM orders", limit=None))) == 4
    finally:
        tool.db_engine.dispose()

    limited = sql_cls(
        db_url=f"sqlite:///{db_file}",
        enable_list_tables=True,
        enable_describe_table=None,
        enable_run_sql_query=None,
    )
    try:
        assert set(limited.functions) == {"sql_inspect"}
        adapted = adapt_toolkit(limited)
        assert adapted.functions[0].parameters["properties"]["action"]["enum"] == ["list_tables"]
        assert "disabled" in limited.sql_inspect("describe_table", table_name="orders")
    finally:
        limited.db_engine.dispose()

    with pytest.raises(ValueError, match="Could not build the database connection"):
        sql_cls()


def test_postgres_scoped_credentials_read_only_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgresTools fails closed on missing creds, disables libpq ambient files, enforces read_only, and exports CSV."""
    pg_cls = cast("Any", builtin_tool_registry().factory("postgres")())
    monkeypatch.setenv("PGHOST", "ambient-host")
    monkeypatch.setenv("PGPASSWORD", "ambient-secret")

    connect_calls: list[dict[str, object]] = []
    fake_conn = _FakeConnection(
        [
            (None, []),  # SET search_path
            ([("table_name",)], [{"table_name": "events"}]),
        ],
    )

    def fake_connect(**kwargs: object) -> _FakeConnection:
        connect_calls.append(dict(kwargs))
        return fake_conn

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    unconfigured = pg_cls(host="db.internal", db_name="app", user="u")
    assert "requires configured credentials: password" in unconfigured.run_query("SELECT 1")
    assert connect_calls == []

    tool = pg_cls(
        host="db.internal",
        port=5433,
        db_name="app",
        user="u",
        password=_DUMMY_SECRET,
        table_schema="analytics",
    )
    assert tool.postgres_inspect("show_tables") == "table_name\nevents"
    assert connect_calls[0]["passfile"] == ""
    assert connect_calls[0]["port"] == 5433
    assert "service" not in connect_calls[0]
    conninfo_kwargs = {k: v for k, v in connect_calls[0].items() if k != "row_factory"}
    conninfo_str = psycopg.conninfo.make_conninfo("", **conninfo_kwargs)
    psycopg.pq.Conninfo.parse(conninfo_str.encode())
    assert fake_conn.read_only is True
    assert fake_conn.closed is True

    # Ambient PGSERVICE fails closed before reaching libpq.
    monkeypatch.setenv("PGSERVICE", "host_prod")
    connect_calls.clear()
    assert "PGSERVICE" in tool.run_query("SELECT 1")
    assert connect_calls == []
    monkeypatch.delenv("PGSERVICE")

    export_conn = _FakeConnection([([("id",), ("name",)], [{"id": 1, "name": "alice"}])])
    export_tool = pg_cls(connection=export_conn, table_schema="public")
    out_csv = tmp_path / "users.csv"
    assert "Successfully exported" in export_tool.export_table_to_path("users", str(out_csv))
    assert out_csv.read_text(encoding="utf-8").splitlines() == ["id,name", "1,alice"]
    assert export_conn.closed is False


def test_redshift_scoped_credentials_ssl_default_and_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RedshiftTools fails closed without creds/IAM keys, keeps SSL enabled on None, and quotes identifiers."""
    rs_cls = cast("Any", builtin_tool_registry().factory("redshift")())
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")

    connect_calls: list[dict[str, object]] = []
    fake_conn = _FakeConnection([([("id",), ("total",)], [(1, 42)])])

    def fake_connect(**kwargs: object) -> _FakeConnection:
        connect_calls.append(dict(kwargs))
        return fake_conn

    monkeypatch.setattr(redshift_connector, "connect", fake_connect)

    iam_missing = rs_cls(host="rs.example", database="dev", iam=True)
    assert "IAM authentication requires" in iam_missing.run_query("SELECT 1")
    assert connect_calls == []

    assert rs_cls(host="rs.example", database="dev", ssl=None).ssl is True
    assert rs_cls(host="rs.example", database="dev", ssl=False).ssl is False

    tool = rs_cls(
        host="rs.example",
        database="dev",
        user="admin",
        password=_DUMMY_SECRET,
        table_schema="reporting",
    )
    out_csv = tmp_path / "orders.csv"
    assert "Successfully exported" in tool.export_table_to_path('my"orders', str(out_csv))
    assert out_csv.read_text(encoding="utf-8").splitlines() == ["id,total", "1,42"]
    assert '"reporting"."my""orders"' in str(fake_conn.cursor_obj.executed[0][0])
    assert fake_conn.closed is True
    assert connect_calls[0]["ssl"] is True


def test_neo4j_scoped_credentials_lazy_driver_and_toggles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neo4jTools rejects ambient-only creds, connects lazily per call, and respects None toggles."""
    neo_cls = cast("Any", builtin_tool_registry().factory("neo4j")())
    monkeypatch.setenv("NEO4J_USERNAME", "ambient")
    monkeypatch.setenv("NEO4J_PASSWORD", "ambient-pw")

    with pytest.raises(ValueError, match="Username and password"):
        neo_cls()

    driver_calls: list[tuple[str, object]] = []
    created_drivers: list[_FakeNeo4jDriver] = []

    def fake_driver(uri: str, *, auth: object) -> _FakeNeo4jDriver:
        driver_calls.append((uri, auth))
        drv = _FakeNeo4jDriver()
        created_drivers.append(drv)
        return drv

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", fake_driver)

    tool = neo_cls(uri="bolt://graph.internal:7687", user="neo4j", password=_DUMMY_SECRET, database="analytics")
    assert driver_calls == []

    assert json.loads(tool.neo4j_inspect("list_labels")) == ["Person"]
    assert json.loads(tool.neo4j_inspect("list_relationship_types")) == ["KNOWS"]
    assert json.loads(tool.run_cypher_query("MATCH (n) RETURN count(n) AS n")) == [{"n": 1}]
    assert all(drv.closed for drv in created_drivers)
    assert driver_calls[0] == ("bolt://graph.internal:7687", ("neo4j", _DUMMY_SECRET))

    read_only = neo_cls(
        user="neo4j",
        password=_DUMMY_SECRET,
        enable_list_labels=True,
        enable_list_relationships=None,
        enable_get_schema=False,
        enable_run_cypher=None,
    )
    assert set(read_only.functions) == {"neo4j_inspect"}
    adapted = adapt_toolkit(read_only)
    assert adapted.functions[0].parameters["properties"]["action"]["enum"] == ["list_labels"]


def test_wave6_batch2_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Batch 2 inspect tools bind timed grants per action, and retired names fail closed."""
    registry = builtin_tool_registry()
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"dba": {"display_name": "DBA", "tools": ["sql", "postgres", "redshift", "neo4j"]}},
        },
    )

    for toolkit, fn, a1, a2 in (
        ("sql", "sql_inspect", "list_tables", "describe_table"),
        ("postgres", "postgres_inspect", "show_tables", "inspect_query"),
        ("redshift", "redshift_inspect", "show_tables", "summarize_table"),
        ("neo4j", "neo4j_inspect", "list_labels", "get_schema"),
    ):
        op1 = grant_operation(config, fn, {"action": a1}, tools=registry, toolkit_name=toolkit)
        op2 = grant_operation(config, fn, {"action": a2}, tools=registry, toolkit_name=toolkit)
        assert op1 is not None
        assert op2 is not None
        assert op1 != op2

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    for toolkit, retired in (
        ("sql", "describe_table"),
        ("postgres", "summarize_table"),
        ("redshift", "inspect_query"),
        ("neo4j", "list_labels"),
    ):
        with pytest.raises(ConfigRuntimeValidationError, match=retired):
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {"dba": {"display_name": "DBA", "tools": [toolkit]}},
                    "tool_approval": {
                        "default": "auto_approve",
                        "rules": [{"match": retired, "action": "require_approval"}],
                    },
                },
                runtime_paths,
            )


def test_postgres_real_conninfo_and_libpq_connect_without_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real psycopg/libpq connect attempts TCP instead of failing on invalid service=""."""
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.delenv("PGSERVICEFILE", raising=False)
    pg_cls = cast("Any", builtin_tool_registry().factory("postgres")())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]

    tool = pg_cls(
        host="127.0.0.1",
        port=unused_port,
        db_name="app",
        user="u",
        password=_DUMMY_SECRET,
    )
    err = tool.run_query("SELECT 1")
    assert 'definition of service "" not found' not in err
    assert "Connection refused" in err or "connection failed" in err


def test_sql_postgresql_scoped_credentials_and_libpq_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLTools fails closed on PostgreSQL URLs missing credentials or using libpq service/passfile."""
    import sqlalchemy  # noqa: PLC0415

    monkeypatch.setenv("PGPASSWORD", "ambient-secret")
    monkeypatch.setenv("PGHOST", "ambient-host")
    monkeypatch.setenv("PGPORT", "6543")
    monkeypatch.delenv("PGSERVICE", raising=False)
    sql_cls = cast("Any", builtin_tool_registry().factory("sql")())

    for bad_url in (
        "postgresql+psycopg://u@db.local/app",
        "postgres://u@db.local/app",
        "postgresql+psycopg://:pw@db.local/app",
        "postgresql+psycopg://u:pw@db.local/",
        "postgresql+psycopg://u:pw@db.local/app?service=prod",
        "postgresql+psycopg://u:pw@db.local/app?passfile=/tmp/pgpass",
    ):
        with pytest.raises(ValueError, match="PostgreSQL"):
            sql_cls(db_url=bad_url)

    monkeypatch.setenv("PGSERVICE", "host_prod")
    with pytest.raises(ValueError, match="PGSERVICE"):
        sql_cls(db_url=f"postgresql+psycopg://u:{_DUMMY_SECRET}@db.local/app")
    monkeypatch.delenv("PGSERVICE")

    captured: list[tuple[object, dict[str, object]]] = []
    real_create_engine = sqlalchemy.create_engine

    def spy_create_engine(url: object, **kwargs: object) -> object:
        captured.append((url, dict(kwargs)))
        return real_create_engine(url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlalchemy, "create_engine", spy_create_engine)
    tool = sql_cls(db_url=f"postgresql+psycopg://u:{_DUMMY_SECRET}@127.0.0.1/app")
    try:
        assert len(captured) == 1
        url_arg, engine_kwargs = captured[0]
        assert getattr(url_arg, "port", None) == 5432
        assert engine_kwargs["connect_args"] == {"passfile": ""}
    finally:
        tool.db_engine.dispose()


class _StatefulRedshiftCursor:
    def __init__(self, conn: _StatefulRedshiftConnection) -> None:
        self._conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _StatefulRedshiftCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: object = None) -> None:
        if query.startswith("INSERT"):
            self._conn.staged.append((1, "alpha"))
            self.description = None
            self._rows = []
        elif query.startswith("FAIL"):
            self._conn.staged.append((2, "should-rollback"))
            msg = "boom"
            raise RuntimeError(msg)
        else:
            self.description = [("id",), ("name",)]
            self._rows = list(self._conn.backing_store) + list(self._conn.staged)

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _StatefulRedshiftConnection:
    def __init__(self, backing_store: list[tuple[int, str]]) -> None:
        self.backing_store = backing_store
        self.staged: list[tuple[int, str]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _StatefulRedshiftCursor:
        return _StatefulRedshiftCursor(self)

    def commit(self) -> None:
        self.backing_store.extend(self.staged)
        self.staged.clear()
        self.committed = True

    def rollback(self) -> None:
        self.staged.clear()
        self.rolled_back = True

    def close(self) -> None:
        self.staged.clear()
        self.closed = True


def test_redshift_run_query_commits_dml_and_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redshift run_query commits DML before closing fresh connections and rolls back on failure."""
    rs_cls = cast("Any", builtin_tool_registry().factory("redshift")())
    persisted_rows: list[tuple[int, str]] = []
    connections: list[_StatefulRedshiftConnection] = []

    def fake_connect(**_kwargs: object) -> _StatefulRedshiftConnection:
        conn = _StatefulRedshiftConnection(persisted_rows)
        connections.append(conn)
        return conn

    monkeypatch.setattr(redshift_connector, "connect", fake_connect)
    tool = rs_cls(host="rs.example", database="dev", user="admin", password=_DUMMY_SECRET)

    assert tool.run_query("INSERT INTO items VALUES (1, 'alpha')") == "Query executed successfully."
    assert connections[0].committed is True
    assert connections[0].closed is True

    # A second fresh connection observes the committed write.
    assert tool.run_query("SELECT id, name FROM items") == "id,name\n1,alpha"
    assert len(connections) == 2

    # Failing DML rolls back and never persists to subsequent connections.
    assert "Error executing query: boom" in tool.run_query("FAIL AFTER STAGING")
    assert connections[2].rolled_back is True
    assert connections[2].committed is False
    assert tool.run_query("SELECT id, name FROM items") == "id,name\n1,alpha"
