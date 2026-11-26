"""Pandas tools configuration and native implementation."""

from __future__ import annotations

from typing import Any

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import UNSET, resolve_bool_toggle

logger = get_logger(__name__)


def _validate_public_attr(name: str, label: str) -> str:
    stripped = name.strip()
    if not stripped or stripped.startswith("_") or "." in stripped or not stripped.isidentifier():
        msg = f"Invalid {label}: {name!r}"
        raise ValueError(msg)
    return stripped


class _PandasTools(Toolkit):
    """Create and transform in-memory Pandas DataFrames."""

    def __init__(
        self,
        enable_create_pandas_dataframe: bool | None = UNSET,
        enable_run_dataframe_operation: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
    ) -> None:
        self.dataframes: dict[str, Any] = {}

        enable_all = resolve_bool_toggle(all, default=False)
        tools: list[Any] = []
        if enable_all or resolve_bool_toggle(enable_create_pandas_dataframe, default=True):
            tools.append(self.create_pandas_dataframe)
        if enable_all or resolve_bool_toggle(enable_run_dataframe_operation, default=True):
            tools.append(self.run_dataframe_operation)

        super().__init__(name="pandas_tools", tools=tools)

    def create_pandas_dataframe(
        self,
        dataframe_name: str,
        create_using_function: str,
        function_parameters: dict[str, Any] | None = None,
    ) -> str:
        """Create and store a DataFrame under `dataframe_name` via `pandas.<create_using_function>(**function_parameters)`."""
        try:
            import pandas as pd
        except ImportError:
            return "Error creating dataframe: `pandas` is not installed."

        if dataframe_name in self.dataframes:
            return f"Dataframe already exists: {dataframe_name}"
        try:
            func_name = _validate_public_attr(create_using_function, "pandas function")
            creator = getattr(pd, func_name)
            if not callable(creator):
                return f"Error creating dataframe: {func_name!r} is not callable"
            dataframe = creator(**(function_parameters or {}))
        except Exception as exc:
            logger.exception("Error creating dataframe", dataframe_name=dataframe_name)
            return f"Error creating dataframe: {exc}"

        if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
            return (
                f"Dataframe is empty: {dataframe_name}"
                if isinstance(dataframe, pd.DataFrame)
                else f"Error creating dataframe: {dataframe_name}"
            )
        self.dataframes[dataframe_name] = dataframe
        return dataframe_name

    def run_dataframe_operation(
        self,
        dataframe_name: str,
        operation: str,
        operation_parameters: dict[str, Any] | None = None,
    ) -> str:
        """Run `operation` on stored DataFrame `dataframe_name` with optional `operation_parameters`."""
        try:
            dataframe = self.dataframes.get(dataframe_name)
            if dataframe is None:
                available = ", ".join(sorted(self.dataframes)) or "none"
                return f"Dataframe not found: {dataframe_name} (available: {available})"
            op_name = _validate_public_attr(operation, "dataframe operation")
            member = getattr(dataframe, op_name)
            params = operation_parameters or {}
            if callable(member):
                result = member(**params)
            elif not params:
                result = member
            else:
                return f"Error running operation: {op_name!r} is not callable"
            to_string = getattr(result, "to_string", None)
            if callable(to_string):
                return str(to_string())
            return str(result)
        except Exception as exc:
            logger.exception("Error running dataframe operation", dataframe_name=dataframe_name, operation=operation)
            return f"Error running operation: {exc}"


@register_tool_with_metadata(
    name="pandas",
    file_access=ToolFileAccess.UNCONFINED,
    requires_primary_runtime=True,
    display_name="Pandas",
    description="Advanced data manipulation and analysis",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Database",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="enable_create_pandas_dataframe",
            label="Enable Create Pandas Dataframe",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_run_dataframe_operation",
            label="Enable Run Dataframe Operation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["pandas"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#pandas",
    function_names=("create_pandas_dataframe", "run_dataframe_operation"),
)
def pandas_tools() -> type[Toolkit]:
    """Return Pandas tools for data manipulation and analysis."""
    return _PandasTools
