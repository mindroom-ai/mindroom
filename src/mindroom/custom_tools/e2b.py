"""E2B sandbox toolkit whose uploads follow ``file_access`` and whose downloads stay in the agent workspace."""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import httpx
from agno.agent import Agent  # noqa: TC002  # resolved by Agno function schema introspection
from agno.team.team import Team  # noqa: TC002  # resolved by Agno function schema introspection
from agno.tools.e2b import E2BTools
from agno.tools.function import ToolResult
from e2b.envd.api import ENVD_API_FILES_ROUTE, handle_envd_api_exception

from mindroom.atomic_file import atomic_write_file_at
from mindroom.file_access import resolve_agent_file
from mindroom.path_confinement import (
    MAX_READ_BYTES,
    open_directory_within_root,
    read_regular_file_within_root,
    resolve_path_within_root,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from e2b_code_interpreter import Sandbox

    from mindroom.config.models import FileAccess


def _write_within_root(root: Path, relative: Path, chunks: Iterable[bytes]) -> None:
    """Atomically publish chunks at a canonical path below a pinned root."""
    with (
        open_directory_within_root(root, relative.parent, create=True) as directory,
        atomic_write_file_at(directory, relative.name) as output,
    ):
        output.writelines(chunks)


@contextmanager
def _sandbox_file_chunks(sandbox: Sandbox, path: str) -> Iterator[Iterator[bytes]]:
    """Stream one sandbox file in chunks, refusing it once it passes the shared read limit.

    The SDK's ``files.read`` buffers the whole body even with ``format="stream"``,
    so this sends the same request without reading the body up front.
    """
    config = sandbox.connection_config
    with httpx.stream(
        "GET",
        f"{sandbox.envd_api_url}{ENVD_API_FILES_ROUTE}",
        params={"path": path, "username": "user"},
        headers=config.sandbox_headers,
        proxy=config.proxy,
        timeout=config.get_request_timeout(),
    ) as response:
        if error := handle_envd_api_exception(response):
            raise error
        yield _within_read_limit(response.iter_bytes())


def _within_read_limit(chunks: Iterable[bytes]) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_READ_BYTES:
            message = f"Sandbox file exceeds the {MAX_READ_BYTES >> 20} MiB transfer limit"
            raise ValueError(message)
        yield chunk


class MindRoomE2BTools(E2BTools):
    """Confine the local paths of the E2B file transfers.

    Uploads follow the agent's ``file_access``: ``workspace`` confines them to the
    agent workspace, ``unrestricted`` allows any regular file MindRoom can read.
    Downloads always land inside the workspace at workspace-relative paths without
    ``..``; links that leave the workspace are rejected. Reads and writes use
    descriptors pinned below their root, so a link swapped in after resolution
    cannot redirect them. Uploads, downloads, and file reads refuse files above
    the shared read limit, and downloads stream so no partial file is published.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 300,
        sandbox_options: dict[str, Any] | None = None,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(api_key=api_key, timeout=timeout, sandbox_options=sandbox_options, **kwargs)

    def _workspace_location(self, path: str) -> tuple[Path, Path]:
        """Return the workspace root as spelled, so a replaced workspace is refused, and the canonical path below it."""
        if self._workspace_root is None:
            msg = "E2B local file transfers require an agent workspace"
            raise ValueError(msg)
        requested = Path(path)
        canonical_root = self._workspace_root.resolve()
        if not requested.is_absolute() and ".." not in requested.parts:
            with suppress(ValueError):
                resolved = resolve_path_within_root(canonical_root, requested, symlinks="internal")
                if resolved != canonical_root:
                    return self._workspace_root, resolved.relative_to(canonical_root)
        msg = f"Local path must name a file inside the agent workspace, relative to it and without '..': {path}"
        raise ValueError(msg)

    @override
    def upload_file(self, file_path: str, sandbox_path: str | None = None) -> str:
        """Upload a local file to the E2B sandbox.

        Args:
            file_path (str): Local file path; with ``file_access: workspace`` it must be inside the agent workspace
            sandbox_path (str, optional): Destination path in the sandbox. Defaults to the same filename.

        Returns:
            str: Path to the file in the sandbox or error message

        """
        try:
            authorized = resolve_agent_file(
                file_path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="E2B upload",
            )
            payload = read_regular_file_within_root(authorized.root, authorized.relative)
            file_in_sandbox = self.sandbox.files.write(sandbox_path or Path(file_path).name, payload)
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Error uploading file: {e}"})
        return file_in_sandbox.path

    @override
    def download_file_from_sandbox(self, sandbox_path: str, local_path: str | None = None) -> str:
        """Download a file from the E2B sandbox into the agent workspace.

        Args:
            sandbox_path (str): Path to the file in the sandbox
            local_path (str, optional): Workspace-relative destination path. Defaults to the same filename.

        Returns:
            str: Workspace-relative path of the downloaded file or error message

        """
        local_path = local_path or Path(sandbox_path).name
        try:
            root, relative = self._workspace_location(local_path)
            with _sandbox_file_chunks(self.sandbox, sandbox_path) as chunks:
                _write_within_root(root, relative, chunks)
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Error downloading file: {e}"})
        return local_path

    @override
    def read_file_content(self, file_path: str, encoding: str = "utf-8") -> str:
        """Read the content of a file from the sandbox.

        Args:
            file_path (str): Path to the file in the sandbox
            encoding (str): Encoding to use for text files (default: utf-8)

        Returns:
            str: File content or error message

        """
        try:
            with _sandbox_file_chunks(self.sandbox, file_path) as chunks:
                content = b"".join(chunks)
            return content.decode(encoding, errors="replace")
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Error reading file: {e}"})

    @override
    def download_png_result(
        self,
        agent: Agent | Team,
        result_index: int = 0,
        output_path: str | None = None,
    ) -> ToolResult:
        """Add a PNG image result from the last code execution as an Image object.

        Args:
            agent: The agent to add the image artifact to
            result_index (int): Index of the result to use (default: 0, the first result)
            output_path (str, optional): Optional workspace-relative path to also save the PNG file.

        Returns:
            ToolResult: Contains the PNG image or error message.

        """
        result = super().download_png_result(agent, result_index)
        png = self.last_execution.results[result_index].png if result.images and self.last_execution else None
        if not output_path or png is None:
            return result
        try:
            root, relative = self._workspace_location(output_path)
            _write_within_root(root, relative, [base64.b64decode(png)])
        except Exception as e:
            return ToolResult(content=f"{result.content}, but saving it failed: {e}", images=result.images)
        self.downloaded_files[result_index] = output_path
        return ToolResult(content=f"{result.content} and saved to {output_path}", images=result.images)

    @override
    def download_chart_data(
        self,
        agent: Agent,
        result_index: int = 0,
        output_path: str | None = None,
        add_as_artifact: bool = True,
    ) -> ToolResult:
        """Extract chart data from an interactive chart in the execution results.

        Args:
            agent: The agent to add the chart artifact to
            result_index (int): Index of the result to extract data from (default: 0)
            output_path (str, optional): Workspace-relative path to save the JSON data. Defaults to 'chart-data-{result_index}.json'
            add_as_artifact (bool): Whether to add the chart as an image artifact (default: True)

        Returns:
            ToolResult: Contains chart information and optionally the chart image.

        """
        if self.last_execution is None:
            return ToolResult(content="No code has been executed yet")
        results = self.last_execution.results
        output_path = output_path or f"chart-data-{result_index}.json"
        try:
            if result_index >= len(results):
                return ToolResult(
                    content=f"Result index {result_index} is out of range. Only {len(results)} results available.",
                )
            result = results[result_index]
            if result.chart is None:
                return ToolResult(content=f"Result at index {result_index} does not contain interactive chart data")
            chart = result.chart.to_dict()
            root, relative = self._workspace_location(output_path)
            _write_within_root(root, relative, [json.dumps(chart, indent=2).encode()])
        except Exception as e:
            return ToolResult(content=f"Error extracting chart data: {e}")
        labels = (("title", "Title"), ("x_label", "X-axis"), ("y_label", "Y-axis"))
        summary = "\n".join(
            [
                f"Interactive {chart.get('type', 'unknown')} chart data saved to {output_path}",
                *(f"{label}: {chart[key]}" for key, label in labels if key in chart),
            ],
        )
        if not add_as_artifact or not result.png:
            return ToolResult(content=summary)
        image = super().download_png_result(agent, result_index)
        return ToolResult(content=f"{summary}\nChart image: {image.content}", images=image.images)
