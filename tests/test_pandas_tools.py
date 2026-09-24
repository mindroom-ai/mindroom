"""The Pandas toolkit only runs allow-listed calls over agent-workspace files."""

from __future__ import annotations

import json
import os
import pickle
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from mindroom.custom_tools.pandas import PandasTools

if TYPE_CHECKING:
    from pathlib import Path


class _MakesDirectoryWhenUnpickled:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return os.mkdir, (str(self.marker),)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return an agent workspace holding one small CSV file."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sales.csv").write_text("region,total\nwest,42\neast,7\n", encoding="utf-8")
    return root


def _create(toolkit: PandasTools, name: str, function: str, parameters: dict[str, object]) -> str:
    return toolkit.create_pandas_dataframe(name, function, parameters)


def test_read_pickle_is_refused_before_unpickling(tmp_path: Path, workspace: Path) -> None:
    """A model-chosen unpickling reader never runs, so a crafted pickle cannot execute code."""
    marker = tmp_path / "unpickled"
    (workspace / "evil.pkl").write_bytes(pickle.dumps(_MakesDirectoryWhenUnpickled(marker)))

    result = _create(PandasTools(base_dir=workspace), "x", "read_pickle", {"filepath_or_buffer": "evil.pkl"})

    assert result.startswith("Error creating dataframe: Unsupported function 'read_pickle'")
    assert not marker.exists()


@pytest.mark.parametrize(
    ("operation", "parameters"),
    [
        ("to_csv", {"path_or_buf": "{target}"}),
        ("to_pickle", {"path": "{target}"}),
        ("to_string", {"buf": "{target}"}),
        ("info", {"buf": "{target}"}),
        ("agg", {"func": "to_csv", "path_or_buf": "{target}"}),
        ("apply", {"func": "to_csv", "path_or_buf": "{target}"}),
        ("query", {"expr": "total > 0"}),
        ("eval", {"expr": "total + 1"}),
        ("__class__", {}),
    ],
)
def test_writing_and_dispatching_operations_are_refused(
    tmp_path: Path,
    workspace: Path,
    operation: str,
    parameters: dict[str, str],
) -> None:
    """Writers, expression evaluators, and name-dispatching methods are outside the allow-list."""
    target = tmp_path / "written"
    toolkit = PandasTools(base_dir=workspace)
    assert _create(toolkit, "sales", "read_csv", {"filepath_or_buffer": "sales.csv"}) == "sales"

    result = toolkit.run_dataframe_operation(
        "sales",
        operation,
        {key: value.format(target=target) for key, value in parameters.items()},
    )

    assert result.startswith(f"Error running operation: Unsupported operation {operation!r}")
    assert not target.exists()


@pytest.mark.parametrize(
    "path",
    [
        "{outside}",
        "../outside.csv",
        "linked.csv",
        "https://example.invalid/data.csv",
        "file://{outside}",
        "~/outside.csv",
    ],
)
def test_readers_cannot_leave_the_workspace(tmp_path: Path, workspace: Path, path: str) -> None:
    """Absolute paths, traversal, escaping links, and URLs never reach a file outside the workspace."""
    outside = tmp_path / "outside.csv"
    outside.write_text("secret\nprimary-only-value\n", encoding="utf-8")
    (workspace / "linked.csv").symlink_to(outside)

    result = _create(
        PandasTools(base_dir=workspace),
        "leak",
        "read_csv",
        {"filepath_or_buffer": path.format(outside=outside)},
    )

    assert result.startswith("Error creating dataframe:")
    assert "primary-only-value" not in result
    assert not (workspace / "pandas_dataframes").exists()


@pytest.mark.parametrize(
    ("function", "parameters"),
    [
        ("read_excel", {"io": "empty.xls", "engine": "xlrd", "engine_kwargs": {"filename": "{outside}"}}),
        ("read_csv", {"filepath_or_buffer": "sales.csv", "engine": "python"}),
        ("read_csv", {"filepath_or_buffer": "sales.csv", "compression": {"method": "gzip"}}),
        ("read_csv", {"filepath_or_buffer": "sales.csv", "storage_options": {"anon": True}}),
        ("read_csv", {"filepath_or_buffer": "sales.csv", "memory_map": True}),
        ("read_csv", {"filepath_or_buffer": "sales.csv", "chunksize": 1}),
        ("read_json", {"path_or_buf": "sales.csv", "engine": "ujson"}),
        ("DataFrame", {"data": {"value": [1]}, "copy": True}),
    ],
)
def test_reader_keywords_outside_the_allow_list_are_refused(
    tmp_path: Path,
    workspace: Path,
    function: str,
    parameters: dict[str, object],
) -> None:
    """Engine, compression, and storage keywords could hand another file to a third-party reader."""
    outside = tmp_path / "outside.csv"
    outside.write_text("OPENAI_API_KEY=primary-only-value\n", encoding="utf-8")
    (workspace / "empty.xls").write_bytes(b"")
    arguments = json.loads(json.dumps(parameters).replace("{outside}", str(outside)))

    result = _create(PandasTools(base_dir=workspace), "leak", function, arguments)

    assert result.startswith(f"Error creating dataframe: Unsupported {function} parameters")
    assert "OPENAI_A" not in result
    assert not (workspace / "pandas_dataframes").exists()


def test_json_and_excel_readers_open_workspace_files(workspace: Path) -> None:
    """The other file readers receive the confined handle as well."""
    (workspace / "events.jsonl").write_text('{"kind": "open"}\n{"kind": "close"}\n', encoding="utf-8")
    pd.read_csv(workspace / "sales.csv").to_excel(workspace / "sales.xlsx", index=False)
    toolkit = PandasTools(base_dir=workspace)
    assert _create(toolkit, "sales", "read_csv", {"filepath_or_buffer": "sales.csv"}) == "sales"
    sales = toolkit.run_dataframe_operation("sales", "head", {})

    assert _create(toolkit, "events", "read_json", {"path_or_buf": "events.jsonl", "lines": True}) == "events"
    assert _create(toolkit, "sheet", "read_excel", {"io": "sales.xlsx"}) == "sheet"
    assert toolkit.run_dataframe_operation("events", "count", {}).split() == ["kind", "2"]
    assert toolkit.run_dataframe_operation("sheet", "head", {}) == sales


def test_saved_dataframes_are_rebuilt_by_fresh_toolkits(workspace: Path) -> None:
    """A later toolkit, like a fresh worker call, rebuilds the saved dataframe from its current source."""
    assert _create(PandasTools(base_dir=workspace), "sales", "read_csv", {"filepath_or_buffer": "sales.csv"}) == "sales"
    assert json.loads((workspace / "pandas_dataframes" / "sales.json").read_text(encoding="utf-8")) == {
        "function": "read_csv",
        "parameters": {"filepath_or_buffer": "sales.csv"},
    }

    fresh = PandasTools(base_dir=workspace)
    assert "west" in fresh.run_dataframe_operation("sales", "nlargest", {"n": 1, "columns": "total"})
    assert fresh.run_dataframe_operation("sales", "sort_values", {"by": "total", "inplace": True}) == "None"
    assert fresh.run_dataframe_operation("sales", "head", {"n": 1}).splitlines()[1].split() == ["0", "west", "42"]

    (workspace / "sales.csv").write_text("region,total\nnorth,99\n", encoding="utf-8")
    assert "north" in fresh.run_dataframe_operation("sales", "head", {})

    replaced = _create(fresh, "sales", "DataFrame", {"data": {"region": ["south"], "total": [1]}})
    assert replaced == "sales"
    assert "south" in PandasTools(base_dir=workspace).run_dataframe_operation("sales", "head", {})


@pytest.mark.parametrize(
    "source",
    [
        {"function": "read_pickle", "parameters": {"filepath_or_buffer": "evil.pkl"}},
        {"function": "read_csv", "parameters": {"filepath_or_buffer": "{outside}"}},
        {"function": "read_excel", "parameters": {"io": "evil.pkl", "engine_kwargs": {"filename": "{outside}"}}},
        ["read_csv"],
    ],
)
def test_tampered_saved_dataframes_are_revalidated(tmp_path: Path, workspace: Path, source: object) -> None:
    """Saved calls are workspace data, so every rebuild passes the same allow-list and confinement."""
    marker = tmp_path / "unpickled"
    outside = tmp_path / "outside.csv"
    outside.write_text("secret\nprimary-only-value\n", encoding="utf-8")
    (workspace / "evil.pkl").write_bytes(pickle.dumps(_MakesDirectoryWhenUnpickled(marker)))
    (workspace / "pandas_dataframes").mkdir()
    (workspace / "pandas_dataframes" / "tampered.json").write_text(
        json.dumps(source).replace("{outside}", str(outside)),
        encoding="utf-8",
    )

    result = PandasTools(base_dir=workspace).run_dataframe_operation("tampered", "head", {})

    assert result.startswith("Error running operation:")
    assert "primary-only-value" not in result
    assert not marker.exists()


def test_saved_dataframes_do_not_follow_a_linked_store(tmp_path: Path, workspace: Path) -> None:
    """A store directory swapped for a link cannot redirect saves outside the workspace."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "pandas_dataframes").symlink_to(outside, target_is_directory=True)

    result = _create(PandasTools(base_dir=workspace), "sales", "read_csv", {"filepath_or_buffer": "sales.csv"})

    assert result.startswith("Error creating dataframe:")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden", "x" * 65])
def test_dataframe_names_cannot_name_other_files(tmp_path: Path, workspace: Path, name: str) -> None:
    """Names become store filenames, so they are limited to one safe path component."""
    result = _create(PandasTools(base_dir=workspace), name, "DataFrame", {"data": {"value": [1]}})

    assert result.startswith(f"Error creating dataframe: Invalid dataframe name {name!r}")
    assert not (workspace / "pandas_dataframes").exists()
    assert not (tmp_path / "escape.json").exists()


def test_empty_dataframes_are_not_saved(workspace: Path) -> None:
    """An empty result reports itself instead of saving a useless name."""
    (workspace / "empty.csv").write_text("region,total\n", encoding="utf-8")

    result = _create(PandasTools(base_dir=workspace), "empty", "read_csv", {"filepath_or_buffer": "empty.csv"})

    assert result == "Dataframe is empty: empty"
    assert not (workspace / "pandas_dataframes").exists()


def test_without_workspace_only_inline_dataframes_are_available() -> None:
    """Without an agent workspace nothing is read from disk and names last for the toolkit instance."""
    toolkit = PandasTools()

    assert _create(toolkit, "sales", "read_csv", {"filepath_or_buffer": "sales.csv"}) == (
        "Error creating dataframe: Reading files requires an agent workspace"
    )
    assert _create(toolkit, "scores", "DataFrame", {"data": {"score": [3, 1]}}) == "scores"
    assert toolkit.run_dataframe_operation("scores", "max", {}).split() == ["score", "3"]
    assert PandasTools().run_dataframe_operation("scores", "max", {}) == "Dataframe not found: scores"
