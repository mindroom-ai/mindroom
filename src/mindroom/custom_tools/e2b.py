"""E2B sandbox toolkit whose uploads follow ``file_access`` and whose downloads stay in the agent workspace."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable  # noqa: TC003  # resolved by Agno function schema introspection
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import httpx
from agno.agent import Agent  # noqa: TC002  # resolved by Agno function schema introspection
from agno.team.team import Team  # noqa: TC002  # resolved by Agno function schema introspection
from agno.tools.e2b import E2BTools
from agno.tools.function import ToolResult
from agno.utils.code_execution import prepare_python_code
from e2b.envd.api import ENVD_API_FILES_ROUTE, handle_envd_api_exception
from e2b_code_interpreter.constants import DEFAULT_TIMEOUT, JUPYTER_PORT
from e2b_code_interpreter.exceptions import format_execution_timeout_error, format_request_timeout_error
from e2b_code_interpreter.models import Execution, extract_exception, parse_output

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


def _check_read_limit(total: int, subject: str) -> None:
    if total > MAX_READ_BYTES:
        message = f"{subject} exceeds the {MAX_READ_BYTES >> 20} MiB transfer limit"
        raise ValueError(message)


def _within_read_limit(chunks: Iterable[bytes], subject: str = "Sandbox file") -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        _check_read_limit(total, subject)
        yield chunk


def _run_code_within_read_limit(sandbox: Sandbox, code: str) -> Execution:
    """Run one cell like ``Sandbox.run_code``, refusing its output once it passes the shared read limit.

    The SDK reads each output line whole, so one large print or image would be buffered
    before any output callback could stop it; this sends the same request and stops reading at the limit.
    """
    config = sandbox.connection_config
    try:
        with httpx.stream(
            "POST",
            f"{'http' if config.debug else 'https'}://{sandbox.get_host(JUPYTER_PORT)}/execute",
            json={"code": code, "context_id": None, "language": None, "env_vars": None},
            headers=config.sandbox_headers,
            proxy=config.proxy,
            timeout=httpx.Timeout(config.request_timeout, read=DEFAULT_TIMEOUT),
        ) as response:
            if error := extract_exception(response):
                raise error
            output = b"".join(_within_read_limit(response.iter_bytes(), "Sandbox code output"))
    except httpx.ReadTimeout:
        raise format_execution_timeout_error() from None
    except httpx.TimeoutException:
        raise format_request_timeout_error() from None
    execution = Execution()
    for line in output.splitlines():
        parse_output(execution, line.decode())
    return execution


class MindRoomE2BTools(E2BTools):
    """Confine the local paths of the E2B file transfers.

    Uploads follow the agent's ``file_access``: ``workspace`` confines them to the
    agent workspace, ``unrestricted`` allows any regular file MindRoom can read.
    Downloads always land inside the workspace at workspace-relative paths without
    ``..``; links that leave the workspace are rejected. Reads and writes use
    descriptors pinned below their root, so a link swapped in after resolution
    cannot redirect them. Uploads, downloads, and file reads refuse files above
    the shared read limit, and downloads stream so no partial file is published.
    Command and code output above that limit is refused while it streams.
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

    # AGNO_COMPAT: E2BTools.run_python_code reads sandbox code output without a size bound.
    # Reason: Agno 3.0.9 calls Sandbox.run_code, which in e2b-code-interpreter 2.1.1 reads each output line whole,
    # so one large print or image grows the primary's memory before any callback runs. This override copies
    # Agno's result formatting, and _run_code_within_read_limit copies the SDK's /execute request.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: E2BTools stops reading code output past a caller-supplied byte limit; refusing output past
    # MAX_READ_BYTES with a tool error and keeping the previous last_execution is MindRoom policy and stays.
    # Coverage: tests/test_e2b_tools.py::test_run_python_code_refuses_output_past_the_limit_while_streaming,
    # tests/test_e2b_tools.py::test_run_python_code_reports_timeouts_like_the_sdk.
    @override
    def run_python_code(self, code: str) -> str:
        """Run Python code in an isolated E2B sandbox environment.

        Args:
            code (str): Python code to execute

        Returns:
            str: Execution results or error message

        """
        try:
            execution = _run_code_within_read_limit(self.sandbox, prepare_python_code(code))
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Error executing code: {e}"})
        self.last_execution = execution
        if (error := execution.error) is not None:
            return f"Error: {error.name}\n{error.value}\n{error.traceback}"
        results = [f"Logs:\n{execution.logs}"]
        for number, result in enumerate(execution.results, start=1):
            if result.text:
                results.append(f"Result {number}: {result.text}")
            elif result.png:
                results.append(f"Result {number}: Generated PNG image (use download_png_result to save)")
            elif result.chart:
                chart_type = result.chart.to_dict().get("type", "unknown")
                results.append(
                    f"Result {number}: Generated interactive {chart_type} chart (use download_chart_data to save)",
                )
            else:
                results.append(f"Result {number}: Output available")
        return json.dumps(results)

    # AGNO_COMPAT: E2BTools.run_command keeps a command's whole output without a size bound.
    # Reason: Agno 3.0.9 waits on the E2B SDK's commands.run, which in e2b 2.2.3 keeps all stdout and stderr
    # until the command ends, so a chatty command grows the primary's memory without limit.
    # Upstream issue: Tracking gap; no matching issue has been verified.
    # Upstream PR: No matching fix has been verified.
    # Remove when: E2BTools stops collecting command output past a caller-supplied byte limit; refusing output
    # past MAX_READ_BYTES with a tool error is MindRoom policy and stays.
    # Coverage: tests/test_e2b_tools.py::test_run_command_refuses_output_past_the_limit_while_streaming.
    @override
    def run_command(
        self,
        command: str,
        on_stdout: Callable | None = None,
        on_stderr: Callable | None = None,
        background: bool = False,
    ) -> str:
        """Run a shell command in the sandbox environment.

        Args:
            command (str): Shell command to execute
            on_stdout (callable, optional): Callback function for streaming stdout
            on_stderr (callable, optional): Callback function for streaming stderr
            background (bool): Whether to run the command in background

        Returns:
            str: Command results or error message, or the command object for background execution

        """
        # The SDK keeps all of a command's output until it ends, so its output callbacks stop it at the limit.
        received = 0

        def within_read_limit(callback: Callable | None) -> Callable[[str], None]:
            def receive(output: str) -> None:
                nonlocal received
                received += len(output.encode())
                _check_read_limit(received, "Sandbox command output")
                if callback:
                    callback(output)

            return receive

        return super().run_command(command, within_read_limit(on_stdout), within_read_limit(on_stderr), background)

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
