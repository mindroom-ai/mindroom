"""Native matplotlib chart visualization toolkit with workspace-confined saving."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from mindroom.atomic_file import atomic_write_file_at
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.tool_system.authoring import Toolkit

logger = get_logger(__name__)

_ChartType = Literal["bar", "line", "pie", "scatter", "histogram"]

_CHART_META: dict[str, tuple[str, str, str, str]] = {
    "bar": ("bar_chart", "Bar Chart", "Categories", "Values"),
    "line": ("line_chart", "Line Chart", "X-axis", "Y-axis"),
    "pie": ("pie_chart", "Pie Chart", "", ""),
    "scatter": ("scatter_plot", "Scatter Plot", "X-axis", "Y-axis"),
    "histogram": ("histogram", "Histogram", "Values", "Frequency"),
}


def _normalize_series_data(data: Any) -> dict[str, float]:  # noqa: ANN401
    """Normalize dict, list of dicts, or list of numbers into label -> float pairs."""
    if data is None:
        msg = "data is required for bar, line, and pie charts"
        raise ValueError(msg)
    if isinstance(data, str):
        with suppress(json.JSONDecodeError):
            data = json.loads(data)
    if isinstance(data, dict):
        result = {str(key): float(value) if isinstance(value, (int, float)) else 0.0 for key, value in data.items()}
        if result:
            return result
    elif isinstance(data, list) and data:
        if isinstance(data[0], dict):
            result = {}
            for item in data:
                if isinstance(item, dict) and len(item) >= 2:
                    keys = list(item.keys())
                    raw_value = item[keys[1]]
                    result[str(item[keys[0]])] = float(raw_value) if isinstance(raw_value, (int, float)) else 0.0
            if result:
                return result
        else:
            return {
                f"Item {idx + 1}": float(value) if isinstance(value, (int, float)) else 0.0
                for idx, value in enumerate(data)
            }
    msg = "data must be a non-empty dict, list, or JSON string"
    raise ValueError(msg)


def _extract_scatter_xy(
    data: Any,  # noqa: ANN401
    x_data: list[int | float] | None,
    y_data: list[int | float] | None,
) -> tuple[list[int | float], list[int | float]]:
    if isinstance(data, str):
        with suppress(json.JSONDecodeError):
            data = json.loads(data)
    if data is not None:
        if isinstance(data, dict) and "x" in data and "y" in data:
            x_data = data["x"]
            y_data = data["y"]
        elif isinstance(data, list) and data and isinstance(data[0], list) and len(data[0]) == 2:
            x_data = [point[0] for point in data]
            y_data = [point[1] for point in data]
    if x_data is None or y_data is None:
        msg = "Missing x_data and y_data parameters"
        raise ValueError(msg)
    if len(x_data) != len(y_data):
        msg = "x_data and y_data must have the same length"
        raise ValueError(msg)
    return x_data, y_data


def _extract_histogram_values(data: Any) -> list[float]:  # noqa: ANN401
    if isinstance(data, str):
        with suppress(json.JSONDecodeError):
            data = json.loads(data)
    if not isinstance(data, list) or not data:
        msg = "Data must be a non-empty list of numbers"
        raise ValueError(msg)
    values: list[float] = []
    for item in data:
        try:
            values.append(float(item))
        except (ValueError, TypeError):
            continue
    if not values:
        msg = "No valid numeric data found"
        raise ValueError(msg)
    return values


class VisualizationTools(Toolkit):
    """Create charts with matplotlib and save them inside the agent workspace."""

    def __init__(
        self,
        tool_output_workspace_root: Path | None = None,
        output_dir: str = "charts",
        enable_create_bar_chart: bool = True,
        enable_create_line_chart: bool = True,
        enable_create_pie_chart: bool = True,
        enable_create_scatter_plot: bool = True,
        enable_create_histogram: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        try:
            import matplotlib as mpl  # noqa: PLC0415

            mpl.use("Agg")
        except ImportError as exc:
            msg = "matplotlib is not installed. Install with: `pip install matplotlib`"
            raise ImportError(msg) from exc

        relative_output_dir = Path(output_dir)
        if relative_output_dir.is_absolute() or ".." in relative_output_dir.parts:
            msg = "visualization output_dir must be a relative path inside the agent workspace"
            raise ValueError(msg)

        self._workspace_root = tool_output_workspace_root
        self._relative_output_dir = relative_output_dir
        self._enabled_types: frozenset[str] = frozenset(
            chart_type
            for chart_type, enabled in (
                ("bar", all or enable_create_bar_chart),
                ("line", all or enable_create_line_chart),
                ("pie", all or enable_create_pie_chart),
                ("scatter", all or enable_create_scatter_plot),
                ("histogram", all or enable_create_histogram),
            )
            if enabled
        )
        super().__init__(
            name="visualization",
            tools=[self.create_chart] if self._enabled_types else [],
        )

    def create_chart(
        self,
        chart_type: _ChartType,
        data: dict[str, int | float | list[int | float]] | list[Any] | str | None = None,
        x_data: list[int | float] | None = None,
        y_data: list[int | float] | None = None,
        title: str | None = None,
        x_label: str | None = None,
        y_label: str | None = None,
        bins: int = 10,
        filename: str | None = None,
    ) -> str:
        """Render a matplotlib chart and save it inside the agent workspace.

        - `bar` / `line` / `pie`: `data` as `{label: value}`, list of dicts, or JSON string.
        - `scatter`: `x_data` & `y_data`, or `data` as `{"x": [...], "y": [...]}` or `[[x, y], ...]`.
        - `histogram`: `data` as a list of numbers; optional `bins`.

        Args:
            chart_type: Chart kind to render.
            data: Primary chart dataset (dict, list, or JSON string).
            x_data: X-axis numeric series (`scatter`).
            y_data: Y-axis numeric series (`scatter`).
            title: Optional chart title.
            x_label: Optional X-axis label.
            y_label: Optional Y-axis label.
            bins: Histogram bin count (default 10).
            filename: Optional plain filename (no directory components); defaults to `<type>_<n>.png`.

        """
        chart_meta = _CHART_META.get(chart_type)
        if chart_meta is None:
            return json.dumps(
                {
                    "chart_type": str(chart_type),
                    "error": f"Unsupported chart_type: {chart_type}",
                    "status": "error",
                },
            )
        report_type, default_title, default_x, default_y = chart_meta
        if chart_type not in self._enabled_types:
            return json.dumps(
                {
                    "chart_type": report_type,
                    "error": f"'{chart_type}' charts are disabled in configuration",
                    "status": "error",
                },
            )
        resolved_title = default_title if title is None else title
        resolved_x = default_x if x_label is None else x_label
        resolved_y = default_y if y_label is None else y_label

        try:
            if filename is not None and (filename in {"", ".", ".."} or Path(filename).name != filename):
                msg = f"filename must be a file name without directories: {filename}"
                raise ValueError(msg)  # noqa: TRY301
            if self._workspace_root is None:
                msg = "Saving charts requires an agent workspace"
                raise ValueError(msg)  # noqa: TRY301

            series = _normalize_series_data(data) if chart_type in {"bar", "line", "pie"} else None
            scatter_xy = _extract_scatter_xy(data, x_data, y_data) if chart_type == "scatter" else None
            histogram_values = _extract_histogram_values(data) if chart_type == "histogram" else None

            with (
                open_directory_within_root(self._workspace_root, self._relative_output_dir, create=True) as directory,
                tempfile.TemporaryDirectory(prefix="mindroom-visualization-") as staging,
            ):
                target_name = filename or f"{report_type}_{len(os.listdir(directory)) + 1}.png"  # noqa: PTH208
                staging_path = Path(staging, target_name)
                extra_meta = self._draw_chart(
                    chart_type=chart_type,
                    series=series,
                    scatter_xy=scatter_xy,
                    histogram_values=histogram_values,
                    title=resolved_title,
                    x_label=resolved_x,
                    y_label=resolved_y,
                    bins=bins,
                    output_path=staging_path,
                )
                # Matplotlib appends its default extension when target_name has none.
                (saved,) = Path(staging).iterdir()
                final_name = saved.name
                with saved.open("rb") as chart_stream, atomic_write_file_at(directory, final_name) as output:
                    shutil.copyfileobj(chart_stream, output)
        except Exception as exc:
            logger.exception("Error creating chart", chart_type=report_type)
            return json.dumps({"chart_type": report_type, "error": str(exc), "status": "error"})

        result: dict[str, Any] = {
            "chart_type": report_type,
            "title": resolved_title,
            "file_path": str(self._workspace_root / self._relative_output_dir / final_name),
            **extra_meta,
            "status": "success",
        }
        return json.dumps(result)

    def _draw_chart(
        self,
        *,
        chart_type: _ChartType,
        series: dict[str, float] | None,
        scatter_xy: tuple[list[int | float], list[int | float]] | None,
        histogram_values: list[float] | None,
        title: str,
        x_label: str,
        y_label: str,
        bins: int,
        output_path: Path,
    ) -> dict[str, Any]:
        import matplotlib.pyplot as plt  # noqa: PLC0415

        figsize = (10, 8) if chart_type == "pie" else (10, 6)
        fig, ax = plt.subplots(figsize=figsize)
        try:
            if series is not None:
                labels = list(series.keys())
                values = list(series.values())
                if chart_type == "bar":
                    ax.bar(labels, values)
                    ax.set_xlabel(x_label)
                    ax.set_ylabel(y_label)
                    ax.tick_params(axis="x", labelrotation=45)
                elif chart_type == "line":
                    ax.plot(labels, values, marker="o", linewidth=2, markersize=6)
                    ax.set_xlabel(x_label)
                    ax.set_ylabel(y_label)
                    ax.tick_params(axis="x", labelrotation=45)
                    ax.grid(True, alpha=0.3)
                else:
                    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
                    ax.axis("equal")
                ax.set_title(title)
                meta: dict[str, Any] = {"data_points": len(series)}
            elif scatter_xy is not None:
                xs, ys = scatter_xy
                ax.scatter(xs, ys, alpha=0.7, s=50)
                ax.set_title(title)
                ax.set_xlabel(x_label)
                ax.set_ylabel(y_label)
                ax.grid(True, alpha=0.3)
                meta = {"data_points": len(xs)}
            else:
                assert histogram_values is not None
                ax.hist(histogram_values, bins=bins, alpha=0.7, edgecolor="black")
                ax.set_title(title)
                ax.set_xlabel(x_label)
                ax.set_ylabel(y_label)
                ax.grid(True, alpha=0.3)
                meta = {"data_points": len(histogram_values), "bins": bins}

            fig.tight_layout()
            fig.savefig(output_path, dpi=300, bbox_inches="tight")
        finally:
            plt.close(fig)
        return meta
