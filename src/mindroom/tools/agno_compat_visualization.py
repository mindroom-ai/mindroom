"""Save Visualization charts inside the agent workspace."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from agno.tools.visualization import VisualizationTools

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root

if TYPE_CHECKING:
    from collections.abc import Callable


class WorkspaceVisualizationTools(VisualizationTools):
    """Visualization toolkit whose charts land only in the agent workspace."""

    def __init__(
        self,
        tool_output_workspace_root: Path | None = None,
        output_dir: str = "charts",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        relative_output_dir = Path(output_dir)
        if relative_output_dir.is_absolute() or ".." in relative_output_dir.parts:
            msg = "visualization output_dir must be a relative path inside the agent workspace"
            raise ValueError(msg)
        # Upstream creates output_dir when it is missing; every chart is drawn into a private directory instead.
        super().__init__(output_dir=tempfile.gettempdir(), **kwargs)
        self._workspace_root = tool_output_workspace_root
        self._relative_output_dir = relative_output_dir

    # AGNO_COMPAT: VisualizationTools saves model-named charts by path below a working-directory folder.
    # Reason: Agno 3.0.9 joins the model's filename to output_dir, which defaults to "charts" in the
    # process working directory, and saves there following links, so an absolute or "../" filename wrote
    # anywhere the process can write and a link planted in a shared workspace redirected the write.
    # Upstream issue: Tracking gap; no matching issue identified on October 3, 2026.
    # Upstream PR: https://github.com/agno-agi/agno/pull/9470, open on October 3, 2026, keeps chart file names inside
    # output_dir but still saves by path below the working directory, following links.
    # Remove when: VisualizationTools accepts a writer or directory descriptor for charts;
    # retain plain file names, workspace-only saves through no-follow descriptors, and atomic replacement.
    # Coverage: tests/test_visualization_tool.py.
    def _save_chart(
        self,
        chart_type: str,
        draw: Callable[..., str],
        *args: Any,  # noqa: ANN401
        filename: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> str:
        """Draw one chart into a private directory, then publish it in the workspace output directory."""
        try:
            if filename is not None and (filename in {"", ".", ".."} or Path(filename).name != filename):
                msg = f"filename must be a file name without directories: {filename}"
                raise ValueError(msg)  # noqa: TRY301 - report it like upstream chart errors
            if self._workspace_root is None:
                msg = "Saving charts requires an agent workspace"
                raise ValueError(msg)  # noqa: TRY301 - report it like upstream chart errors
            with (
                open_directory_within_root(self._workspace_root, self._relative_output_dir, create=True) as directory,
                tempfile.TemporaryDirectory(prefix="mindroom-visualization-") as staging,
            ):
                name = filename or f"{chart_type}_{len(os.listdir(directory)) + 1}.png"  # noqa: PTH208 - a descriptor
                # An absolute path replaces upstream's output_dir when it joins the two.
                result = json.loads(draw(*args, filename=str(Path(staging, name)), **kwargs))
                if result.get("status") != "success":
                    return json.dumps(result)
                # Matplotlib appends its default extension to a name without one.
                (saved,) = Path(staging).iterdir()
                name = saved.name
                with saved.open("rb") as chart, atomic_write_file_at(directory, name) as output:
                    shutil.copyfileobj(chart, output)
        except (OSError, ValueError) as exc:
            return json.dumps({"chart_type": chart_type, "error": str(exc), "status": "error"})
        result["file_path"] = str(self._workspace_root / self._relative_output_dir / name)
        return json.dumps(result)

    @override
    @wraps(VisualizationTools.create_bar_chart)
    def create_bar_chart(self, *args: Any, **kwargs: Any) -> str:
        return self._save_chart("bar_chart", super().create_bar_chart, *args, **kwargs)

    @override
    @wraps(VisualizationTools.create_line_chart)
    def create_line_chart(self, *args: Any, **kwargs: Any) -> str:
        return self._save_chart("line_chart", super().create_line_chart, *args, **kwargs)

    @override
    @wraps(VisualizationTools.create_pie_chart)
    def create_pie_chart(self, *args: Any, **kwargs: Any) -> str:
        return self._save_chart("pie_chart", super().create_pie_chart, *args, **kwargs)

    @override
    @wraps(VisualizationTools.create_scatter_plot)
    def create_scatter_plot(self, *args: Any, **kwargs: Any) -> str:
        return self._save_chart("scatter_plot", super().create_scatter_plot, *args, **kwargs)

    @override
    @wraps(VisualizationTools.create_histogram)
    def create_histogram(self, *args: Any, **kwargs: Any) -> str:
        return self._save_chart("histogram", super().create_histogram, *args, **kwargs)
