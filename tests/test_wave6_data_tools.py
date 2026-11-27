"""Tests for native Wave 6 Batch 1 local data & analytics toolkits (csv, pandas, duckdb)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import duckdb
import pytest

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.toolkits import adapt_toolkit

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_config(tools: list[object], rules: list[dict[str, object]] | None = None) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"analyst": {"display_name": "Analyst", "tools": tools}},
            **({"tool_approval": {"default": "auto_approve", "rules": rules}} if rules is not None else {}),
        },
    )


def test_csv_inspect_and_query_end_to_end(tmp_path: Path) -> None:
    """CsvTools lists, inspects, reads with row_limit, queries with DuckDB, and refuses unconfigured files."""
    orders = tmp_path / "my-orders.csv"
    orders.write_text("id,item,price\n1,apple,3\n2,pear,5\n3,plum,4\n", encoding="utf-8")
    secret = tmp_path / "secret.csv"
    secret.write_text("k,v\ntoken,abc\n", encoding="utf-8")

    csv_cls = cast("Any", builtin_tool_registry().factory("csv")())
    tool = csv_cls(csvs=[orders], row_limit=2)

    assert json.loads(tool.csv_inspect("list")) == ["my-orders"]
    assert json.loads(tool.csv_inspect("columns", "my-orders")) == ["id", "item", "price"]
    assert len(json.loads(tool.csv_inspect("read", "my-orders"))) == 2
    assert len(json.loads(tool.csv_inspect("read", "my-orders", row_limit=1))) == 1

    queried = tool.csv_query(
        "my-orders",
        'SELECT item FROM "my-orders" WHERE price >= 4 ORDER BY id; DROP TABLE x',
    )
    assert queried == "item\npear\nplum"

    # Unconfigured CSV stems and raw paths are refused.
    assert "not found" in tool.csv_inspect("read", "secret")
    assert "not found" in tool.csv_inspect("read", str(secret))
    assert "not found" in tool.csv_query("secret", "SELECT * FROM secret")


def test_csv_query_dotted_stem_and_preserves_persistent_duckdb_table(tmp_path: Path) -> None:
    """Dotted CSV stems quote as one identifier and TEMP TABLE never clobbers a supplied connection's table."""
    sales = tmp_path / "sales.2025.csv"
    sales.write_text("id,amount\n1,100\n2,250\n", encoding="utf-8")

    csv_cls = cast("Any", builtin_tool_registry().factory("csv")())
    con = duckdb.connect(":memory:")
    try:
        con.execute('CREATE TABLE "sales.2025" AS SELECT 999 AS persistent_col')
        tool = csv_cls(csvs=[sales], duckdb_connection=con)
        queried = tool.csv_query("sales.2025", 'SELECT id, amount FROM "sales.2025" ORDER BY id')
        assert queried == "id,amount\n1,100\n2,250"
        # Existing persistent table on the supplied connection stays intact and unshadowed.
        assert con.sql('SELECT persistent_col FROM "sales.2025"').fetchall() == [(999,)]

        # Existing caller-owned TEMP table of the same name is never overwritten or dropped on collision.
        con.execute('CREATE TEMP TABLE "sales.2025" AS SELECT 777 AS caller_temp_col')
        err = tool.csv_query("sales.2025", 'SELECT * FROM "sales.2025"')
        assert "Error querying csv" in err
        assert "already exists" in err
        assert con.sql('SELECT caller_temp_col FROM temp.main."sales.2025"').fetchall() == [(777,)]
    finally:
        con.close()


def test_csv_explicit_none_toggles_never_enable_tools_and_narrow_schema(tmp_path: Path) -> None:
    """Explicit None disables toggles instead of falling back to True, and offered_function narrows enum."""
    csv_cls = cast("Any", builtin_tool_registry().factory("csv")())
    tool = csv_cls(
        csvs=[tmp_path / "a.csv"],
        enable_list_csv_files=True,
        enable_get_columns=None,
        enable_read_csv_file=False,
        enable_query_csv_file=None,
    )

    assert set(tool.functions) == {"csv_inspect"}
    adapted = adapt_toolkit(tool)
    assert adapted.functions[0].parameters["properties"]["action"]["enum"] == ["list"]
    assert "disabled" in tool.csv_inspect("read", "a")


def test_pandas_lifecycle_and_private_attr_blocking(tmp_path: Path) -> None:
    """PandasTools creates/operates on DataFrames, reports missing ones, and blocks private/dunder attrs."""
    data = tmp_path / "sales.csv"
    data.write_text("region,amount\nnorth,10\nsouth,20\n", encoding="utf-8")

    pandas_cls = cast("Any", builtin_tool_registry().factory("pandas")())
    tool = pandas_cls()

    assert tool.pandas_create_dataframe("sales", "read_csv", {"filepath_or_buffer": str(data)}) == "sales"
    assert "already exists" in tool.pandas_create_dataframe("sales", "read_csv", {"filepath_or_buffer": str(data)})
    assert "10" in tool.pandas_run_operation("sales", "head", {"n": 1})
    assert tool.pandas_run_operation("sales", "shape") == "(2, 2)"
    assert "Dataframe not found" in tool.pandas_run_operation("missing", "head")

    # Private/dunder names are rejected.
    assert "Invalid pandas function" in tool.pandas_create_dataframe("x", "__import__", {"name": "os"})
    assert "Invalid dataframe operation" in tool.pandas_run_operation("sales", "__class__")
    assert "Invalid dataframe operation" in tool.pandas_run_operation("sales", "iloc.__class__")

    # Explicit None disables tools instead of defaulting to True.
    disabled = pandas_cls(enable_create_pandas_dataframe=None, enable_run_dataframe_operation=None)
    assert disabled.functions == {}


def test_duckdb_inspect_query_and_manage_lifecycle(tmp_path: Path) -> None:
    """DuckDbTools loads CSV/Parquet, safely quotes identifiers, summarizes, exports, and enforces read_only."""
    csv_file = tmp_path / "select.csv"  # Reserved keyword stem!
    csv_file.write_text("id,val\n1,10\n2,30\n", encoding="utf-8")

    duckdb_cls = cast("Any", builtin_tool_registry().factory("duckdb")())
    tool = duckdb_cls(init_commands=["CREATE TABLE seed (n INTEGER)", "INSERT INTO seed VALUES (7)"])

    assert "seed" in tool.duckdb_inspect("show_tables")
    assert tool.duckdb_run_query("SELECT n FROM seed") == "n\n7"

    # Reserved keyword stem gets suffixed to stay SQL-safe.
    loaded_table = tool.duckdb_manage("load_path", path=str(csv_file))
    assert loaded_table == "select_"
    assert "id" in tool.duckdb_inspect("describe_table", table=loaded_table)
    assert "val" in tool.duckdb_inspect("summarize_table", table=loaded_table)
    assert "PROJECTION" in tool.duckdb_inspect("inspect_query", query="SELECT id FROM select_").upper()

    # Export validates format and writes CSV.
    export_dir = tmp_path / "out"
    export_dir.mkdir()
    tool.duckdb_manage("export_table", table=loaded_table, file_format="CSV", path=str(export_dir))
    assert (export_dir / f"{loaded_table}.csv").exists()
    assert "Unsupported export format" in tool.duckdb_manage("export_table", table=loaded_table, file_format="BAD")  # type: ignore[arg-type]

    # NUL bytes and bad identifiers fail cleanly.
    assert "Error" in tool.duckdb_inspect("describe_table", table="bad\x00name")

    # Persistent read-only database refuses writes.
    db_path = tmp_path / "ro.duckdb"
    writer = duckdb_cls(db_path=str(db_path))
    writer.duckdb_run_query("CREATE TABLE items (x INTEGER);")
    writer.connection.close()

    reader = duckdb_cls(db_path=str(db_path), read_only=True)
    assert "items" in reader.duckdb_inspect("show_tables")
    assert "read-only" in reader.duckdb_run_query("INSERT INTO items VALUES (1)").lower()
    reader.connection.close()


def test_wave6_batch1_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Action-scoped grants bind to (tool, action), and retired rules fail closed on config load."""
    registry = builtin_tool_registry()
    config = _minimal_config(["csv", "duckdb"])

    op_list = grant_operation(config, "csv_inspect", {"action": "list"}, tools=registry, toolkit_name="csv")
    op_read = grant_operation(config, "csv_inspect", {"action": "read"}, tools=registry, toolkit_name="csv")
    assert op_list is not None
    assert op_read is not None
    assert op_list != op_read

    op_load = grant_operation(config, "duckdb_manage", {"action": "load_path"}, tools=registry, toolkit_name="duckdb")
    op_export = grant_operation(
        config,
        "duckdb_manage",
        {"action": "export_table"},
        tools=registry,
        toolkit_name="duckdb",
    )
    assert op_load is not None
    assert op_export is not None
    assert op_load != op_export

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    for toolkit, retired in (("csv", "read_csv_file"), ("duckdb", "export_table_to_path")):
        with pytest.raises(ConfigRuntimeValidationError, match=retired):
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {"analyst": {"display_name": "Analyst", "tools": [toolkit]}},
                    "tool_approval": {
                        "default": "auto_approve",
                        "rules": [{"match": retired, "action": "require_approval"}],
                    },
                },
                runtime_paths,
            )
