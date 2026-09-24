"""Pandas toolkit limited to allow-listed calls over agent-workspace files."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from agno.tools import Toolkit

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.logging_config import get_logger
from mindroom.path_confinement import (
    open_directory_within_root,
    open_regular_file_within_root,
    resolve_path_within_root,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

logger = get_logger(__name__)


@dataclass(frozen=True)
class _Constructor:
    """One allow-listed constructor, the parameter naming its source file, and its permitted keywords."""

    path_parameter: str | None
    keywords: frozenset[str]


# Keywords are allow-listed because engines, engine keywords, compression, and storage
# options can hand another file or URL to a third-party reader that ignores the opened handle.
_CONSTRUCTORS: dict[str, _Constructor] = {
    "DataFrame": _Constructor(None, frozenset({"data", "index", "columns", "dtype"})),
    "read_csv": _Constructor(
        "filepath_or_buffer",
        frozenset(
            {
                "filepath_or_buffer",
                "sep",
                "delimiter",
                "header",
                "names",
                "index_col",
                "usecols",
                "dtype",
                "true_values",
                "false_values",
                "skipinitialspace",
                "skiprows",
                "skipfooter",
                "nrows",
                "na_values",
                "keep_default_na",
                "na_filter",
                "skip_blank_lines",
                "parse_dates",
                "date_format",
                "dayfirst",
                "thousands",
                "decimal",
                "lineterminator",
                "quotechar",
                "quoting",
                "doublequote",
                "escapechar",
                "comment",
                "encoding",
                "encoding_errors",
                "on_bad_lines",
            },
        ),
    ),
    "read_excel": _Constructor(
        "io",
        frozenset(
            {
                "io",
                "sheet_name",
                "header",
                "names",
                "index_col",
                "usecols",
                "dtype",
                "true_values",
                "false_values",
                "skiprows",
                "nrows",
                "na_values",
                "keep_default_na",
                "na_filter",
                "parse_dates",
                "date_format",
                "thousands",
                "decimal",
                "comment",
                "skipfooter",
            },
        ),
    ),
    "read_json": _Constructor(
        "path_or_buf",
        frozenset(
            {
                "path_or_buf",
                "orient",
                "dtype",
                "convert_axes",
                "convert_dates",
                "keep_default_dates",
                "precise_float",
                "date_unit",
                "encoding",
                "encoding_errors",
                "lines",
                "nrows",
            },
        ),
    ),
}
# Methods that only compute over the dataframe: none takes a path, buffer, expression, or function name.
_OPERATIONS = frozenset(
    {
        "abs",
        "all",
        "any",
        "corr",
        "count",
        "cov",
        "cummax",
        "cummin",
        "cumprod",
        "cumsum",
        "describe",
        "diff",
        "drop",
        "drop_duplicates",
        "dropna",
        "duplicated",
        "fillna",
        "head",
        "idxmax",
        "idxmin",
        "isin",
        "isna",
        "kurt",
        "max",
        "mean",
        "median",
        "melt",
        "min",
        "mode",
        "nlargest",
        "notna",
        "nsmallest",
        "nunique",
        "pct_change",
        "pivot",
        "prod",
        "quantile",
        "rank",
        "rename",
        "replace",
        "reset_index",
        "round",
        "sample",
        "select_dtypes",
        "sem",
        "set_index",
        "skew",
        "sort_index",
        "sort_values",
        "std",
        "sum",
        "tail",
        "transpose",
        "value_counts",
        "var",
    },
)
_DATAFRAME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DATAFRAMES_DIR = "pandas_dataframes"


@dataclass(frozen=True)
class _DataFrameSource:
    """One allow-listed constructor call that rebuilds a named dataframe."""

    function: str
    parameters: dict[str, Any]


def _validate_dataframe_name(name: object) -> str:
    if not isinstance(name, str) or not _DATAFRAME_NAME.fullmatch(name):
        msg = f"Invalid dataframe name {name!r}: use 1-64 letters, digits, underscores, or hyphens"
        raise ValueError(msg)
    return name


def _validate_source(function: object, parameters: object) -> _DataFrameSource:
    """Check a model-supplied or saved call against the constructor allow-list."""
    if not isinstance(function, str) or function not in _CONSTRUCTORS:
        msg = f"Unsupported function {function!r}: use one of {', '.join(_CONSTRUCTORS)}"
        raise ValueError(msg)
    if not isinstance(parameters, dict):
        msg = "Function parameters must be an object"
        raise TypeError(msg)
    keywords = {str(key): value for key, value in parameters.items()}
    if unsupported := sorted(keywords.keys() - _CONSTRUCTORS[function].keywords):
        msg = f"Unsupported {function} parameters {unsupported}: use {', '.join(sorted(_CONSTRUCTORS[function].keywords))}"
        raise ValueError(msg)
    return _DataFrameSource(function, keywords)


def _validate_operation(operation: object, parameters: object) -> tuple[str, dict[str, Any]]:
    """Check a model-supplied method against the operation allow-list."""
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        msg = f"Unsupported operation {operation!r}: use one of {', '.join(sorted(_OPERATIONS))}"
        raise ValueError(msg)
    if not isinstance(parameters, dict):
        msg = "Operation parameters must be an object"
        raise TypeError(msg)
    return operation, {str(key): value for key, value in parameters.items()}


@contextmanager
def _open_regular_file(root: Path, relative_path: Path) -> Iterator[BinaryIO]:
    """Open one regular file below a canonical root without following links or blocking on a FIFO."""
    with (
        open_regular_file_within_root(root, relative_path) as descriptor,
        os.fdopen(descriptor, "rb", closefd=False) as handle,
    ):
        yield handle


@contextmanager
def _open_workspace_file(base_dir: Path | None, path: object) -> Iterator[BinaryIO]:
    """Open one model-named regular file inside the workspace."""
    if base_dir is None:
        msg = "Reading files requires an agent workspace"
        raise ValueError(msg)
    if not isinstance(path, str):
        msg = f"File path must be a string inside the agent workspace, got {path!r}"
        raise TypeError(msg)
    root = base_dir.resolve()
    try:
        resolved = resolve_path_within_root(root, path, symlinks="internal")
    except ValueError:
        msg = f"File path {path!r} is outside the agent workspace"
        raise ValueError(msg) from None
    with _open_regular_file(root, resolved.relative_to(root)) as handle:
        yield handle


def _build_dataframe(base_dir: Path | None, source: _DataFrameSource) -> pd.DataFrame:
    """Run one validated call, handing file readers an already-opened workspace file instead of a path."""
    constructor = getattr(pd, source.function)
    path_parameter = _CONSTRUCTORS[source.function].path_parameter
    if path_parameter is None:
        dataframe = constructor(**source.parameters)
    else:
        if path_parameter not in source.parameters:
            msg = f"{source.function} requires the {path_parameter!r} parameter"
            raise ValueError(msg)
        with _open_workspace_file(base_dir, source.parameters[path_parameter]) as handle:
            dataframe = constructor(**{**source.parameters, path_parameter: handle})
    if not isinstance(dataframe, pd.DataFrame):
        msg = f"{source.function} did not return a dataframe"
        raise TypeError(msg)
    return dataframe


class PandasTools(Toolkit):
    """Named dataframes saved as their constructor call and rebuilt for each operation.

    With a workspace, names persist under ``pandas_dataframes/`` so separate
    worker calls share them; without one they last for this toolkit instance.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        enable_create_pandas_dataframe: bool = True,
        enable_run_dataframe_operation: bool = True,
        all: bool = False,  # noqa: A002
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.base_dir = base_dir
        self._sources: dict[str, _DataFrameSource] = {}
        tools: list[Any] = []
        if all or enable_create_pandas_dataframe:
            tools.append(self.create_pandas_dataframe)
        if all or enable_run_dataframe_operation:
            tools.append(self.run_dataframe_operation)
        super().__init__(name="pandas_tools", tools=tools, **kwargs)

    def _save_source(self, name: str, source: _DataFrameSource) -> None:
        if self.base_dir is None:
            self._sources[name] = source
            return
        with open_directory_within_root(self.base_dir.resolve(), _DATAFRAMES_DIR, create=True) as directory:
            atomic_write_bytes_at(directory, f"{name}.json", json.dumps(asdict(source)).encode())

    def _load_source(self, name: str) -> _DataFrameSource | None:
        if self.base_dir is None:
            return self._sources.get(name)
        try:
            with _open_regular_file(self.base_dir.resolve(), Path(_DATAFRAMES_DIR, f"{name}.json")) as handle:
                saved = json.load(handle)
        except FileNotFoundError:
            return None
        if not isinstance(saved, dict):
            msg = f"Saved dataframe is invalid: {name}"
            raise TypeError(msg)
        return _validate_source(saved.get("function"), saved.get("parameters"))

    def create_pandas_dataframe(
        self,
        dataframe_name: str,
        create_using_function: str,
        function_parameters: dict[str, Any],
    ) -> str:
        """Create or replace the dataframe `dataframe_name` by calling `create_using_function` with `function_parameters`.

        `create_using_function` must be one of: DataFrame, read_csv, read_excel, read_json.
        File readers only open files inside the agent workspace; relative paths resolve from it and URLs are not supported.
        Only common parsing keywords are accepted; an unsupported keyword returns the permitted ones.
        The call is saved under the name and rerun for every operation, so the dataframe reflects the current file.

        For Example:
        - To create a dataframe `sales` from a CSV file, use: {"dataframe_name": "sales", "create_using_function": "read_csv", "function_parameters": {"filepath_or_buffer": "data/sales.csv"}}
        - To create a dataframe `events` from a JSON file, use: {"dataframe_name": "events", "create_using_function": "read_json", "function_parameters": {"path_or_buf": "events.json"}}
        - To create a dataframe `scores` from inline data, use: {"dataframe_name": "scores", "create_using_function": "DataFrame", "function_parameters": {"data": {"name": ["a", "b"], "score": [1, 2]}}}

        :param dataframe_name: The dataframe name: 1-64 letters, digits, underscores, or hyphens.
        :param create_using_function: The allow-listed Pandas constructor to call.
        :param function_parameters: The keyword arguments to pass to the constructor.
        :return: The name of the created dataframe if successful, otherwise an error message.
        """
        try:
            name = _validate_dataframe_name(dataframe_name)
            source = _validate_source(create_using_function, function_parameters)
            if _build_dataframe(self.base_dir, source).empty:
                return f"Dataframe is empty: {name}"
            self._save_source(name, source)
        except Exception as e:
            logger.warning("pandas_dataframe_create_failed", function=create_using_function, error=str(e))
            return f"Error creating dataframe: {e}"
        return name

    def run_dataframe_operation(self, dataframe_name: str, operation: str, operation_parameters: dict[str, Any]) -> str:
        """Run the dataframe method `operation` on `dataframe_name` with `operation_parameters` and return the result as text.

        `operation` must be an allow-listed method that only computes over the dataframe, such as head, tail, describe, value_counts, sort_values, mean, or corr; an unsupported method returns the full list.
        Each operation starts from the saved dataframe and never changes it.

        For Example:
        - To get the first 5 rows of a dataframe `sales`, use: {"dataframe_name": "sales", "operation": "head", "operation_parameters": {"n": 5}}
        - To summarize a dataframe `sales`, use: {"dataframe_name": "sales", "operation": "describe", "operation_parameters": {}}

        :param dataframe_name: The name of the dataframe to run the operation on.
        :param operation: The allow-listed dataframe method to run.
        :param operation_parameters: The keyword arguments to pass to the method.
        :return: The result of the operation if successful, otherwise an error message.
        """
        try:
            method, parameters = _validate_operation(operation, operation_parameters)
            name = _validate_dataframe_name(dataframe_name)
            source = self._load_source(name)
            if source is None:
                return f"Dataframe not found: {name}"
            result = getattr(_build_dataframe(self.base_dir, source), method)(**parameters)
        except Exception as e:
            logger.warning("pandas_dataframe_operation_failed", operation=operation, error=str(e))
            return f"Error running operation: {e}"
        if isinstance(result, (pd.DataFrame, pd.Series)):
            return result.to_string()
        return str(result)
