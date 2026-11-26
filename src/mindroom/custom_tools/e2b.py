"""Native E2B sandbox toolkit with workspace-confined file transfers and streaming output caps."""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import httpx

from mindroom.atomic_file import atomic_write_file_at
from mindroom.file_access import resolve_agent_file
from mindroom.llm.media import Image
from mindroom.path_confinement import (
    MAX_READ_BYTES,
    open_directory_within_root,
    read_regular_file_within_root,
    resolve_path_within_root,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tools.native_helpers import prepare_python_code, require_non_empty

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from mindroom.config.models import FileAccess

_E2BRunAction = Literal["python", "command", "stream_command", "background_command", "kill_background", "server"]
_E2BInspectAction = Literal["list_files", "read_file", "watch_directory", "status", "list_sandboxes", "public_url"]
_E2BFilesAction = Literal["write", "upload", "download", "download_png", "download_chart"]
_E2BLifecycleAction = Literal["set_timeout", "shutdown"]
_DEFAULT_E2B_DOMAIN = "e2b.app"


def _write_within_root(root: Path, relative: Path, chunks: Iterable[bytes]) -> None:
    """Atomically publish chunks at a canonical path below a pinned root."""
    with (
        open_directory_within_root(root, relative.parent, create=True) as directory,
        atomic_write_file_at(directory, relative.name) as output,
    ):
        output.writelines(chunks)


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


@contextmanager
def _sandbox_file_chunks(sandbox: Any, path: str) -> Iterator[Iterator[bytes]]:  # noqa: ANN401 - optional e2b Sandbox
    """Stream one sandbox file in chunks, refusing it once it passes the shared read limit."""
    from e2b.envd.api import ENVD_API_FILES_ROUTE, handle_envd_api_exception  # noqa: PLC0415

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


def _run_code_within_read_limit(sandbox: Any, code: str) -> Any:  # noqa: ANN401 - optional e2b Execution
    """Run one Python cell while bounding streamed response bytes to ``MAX_READ_BYTES``."""
    from e2b_code_interpreter.constants import DEFAULT_TIMEOUT, JUPYTER_PORT  # noqa: PLC0415
    from e2b_code_interpreter.exceptions import (  # noqa: PLC0415
        format_execution_timeout_error,
        format_request_timeout_error,
    )
    from e2b_code_interpreter.models import Execution, extract_exception, parse_output  # noqa: PLC0415

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


def _reject_ambient_e2b_debug() -> None:
    """Fail closed when ambient ``E2B_DEBUG=true`` would force E2B SDK routing to localhost."""
    from e2b.connection_config import ConnectionConfig  # noqa: PLC0415

    if ConnectionConfig._debug():
        msg = "Ambient E2B_DEBUG=true is set; refusing to redirect E2B traffic to http://localhost:3000."
        raise ValueError(msg)


def _error_json(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


class MindRoomE2BTools(Toolkit):
    """Run code, shell commands, and confined file transfers in an E2B sandbox.

    Uploads follow the agent's ``file_access``; downloads and execution artifacts always land
    inside the agent workspace via descriptor-pinned atomic writes. File transfers, reads,
    code output, and command output are bounded by ``MAX_READ_BYTES``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 300,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        self.api_key = require_non_empty(api_key, "E2B API key")
        self.timeout = timeout
        self._workspace_root = tool_output_workspace_root
        self._file_access: FileAccess = file_access
        self._sandbox: Any | None = None
        self.last_execution: Any | None = None
        self._background_commands: dict[int, Any] = {}

        super().__init__(name="e2b_tools", tools=[self.e2b_run, self.e2b_inspect, self.e2b_files, self.e2b_lifecycle])

    @property
    def sandbox(self) -> Any:  # noqa: ANN401 - optional e2b_code_interpreter.Sandbox
        """Create and return the E2B sandbox lazily."""
        if self._sandbox is None:
            from e2b_code_interpreter import Sandbox  # noqa: PLC0415

            _reject_ambient_e2b_debug()
            self._sandbox = Sandbox.create(
                api_key=self.api_key,
                timeout=self.timeout,
                domain=_DEFAULT_E2B_DOMAIN,
                debug=False,
            )
        return self._sandbox

    @sandbox.setter
    def sandbox(self, value: Any) -> None:  # noqa: ANN401
        self._sandbox = value

    def _workspace_location(self, path: str) -> tuple[Path, Path]:
        """Return the workspace root as spelled and the canonical relative path below it."""
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

    def _run_python(self, code: str) -> str:
        try:
            execution = _run_code_within_read_limit(self.sandbox, prepare_python_code(code))
        except Exception as exc:
            return _error_json(f"Error executing code: {exc}")
        self.last_execution = execution
        if (error := execution.error) is not None:
            return f"Error: {error.name}\n{error.value}\n{error.traceback}"
        results = [f"Logs:\n{execution.logs}"]
        for number, result in enumerate(execution.results, start=1):
            if result.text:
                results.append(f"Result {number}: {result.text}")
            elif result.png:
                results.append(f"Result {number}: Generated PNG image (use e2b_files action='download_png' to save)")
            elif result.chart:
                chart_type = result.chart.to_dict().get("type", "unknown")
                results.append(
                    f"Result {number}: Generated interactive {chart_type} chart (use e2b_files action='download_chart' to save)",
                )
            else:
                results.append(f"Result {number}: Output available")
        return json.dumps(results)

    def _run_shell_command(self, command: str, *, collect_stream: bool) -> str:
        received = 0
        streamed: list[str] = []

        def make_callback(prefix: str) -> Callable[[str], None]:
            def receive(chunk: str) -> None:
                nonlocal received
                received += len(chunk.encode())
                _check_read_limit(received, "Sandbox command output")
                if collect_stream:
                    streamed.append(f"{prefix}: {chunk}")

            return receive

        try:
            result = self.sandbox.commands.run(
                command,
                on_stdout=make_callback("STDOUT"),
                on_stderr=make_callback("STDERR"),
                background=False,
            )
        except Exception as exc:
            return _error_json(f"Error executing command: {exc}")
        if collect_stream:
            return json.dumps(streamed) if streamed else "Command completed with no output."
        output = [
            f"{label}:\n{value}"
            for label, attr in (("STDOUT", "stdout"), ("STDERR", "stderr"))
            if (value := getattr(result, attr, None))
        ]
        return json.dumps(output) if output else "Command executed successfully with no output."

    def e2b_run(  # noqa: PLR0911
        self,
        action: _E2BRunAction,
        code: str | None = None,
        command: str | None = None,
        port: int | None = None,
        pid: int | None = None,
    ) -> str:
        """Execute Python code, shell commands, background jobs, or web servers in the E2B sandbox.

        Actions:
        - `python`: execute Python `code` (output capped at 64 MiB).
        - `command` / `stream_command`: run shell `command` and capture stdout/stderr (capped at 64 MiB).
        - `background_command`: start shell `command` in background and return its `pid`.
        - `kill_background`: terminate background command identified by `pid`.
        - `server`: start background server `command` on `port` and return its public URL and `pid`.
        """
        try:
            if action == "python":
                return self._run_python(require_non_empty(code, "code"))
            if action == "command":
                return self._run_shell_command(require_non_empty(command, "command"), collect_stream=False)
            if action == "stream_command":
                return self._run_shell_command(require_non_empty(command, "command"), collect_stream=True)
            if action == "background_command":
                handle = self.sandbox.commands.run(require_non_empty(command, "command"), background=True)
                self._background_commands[handle.pid] = handle
                return json.dumps({"status": "started", "pid": handle.pid})
            if action == "kill_background":
                if pid is None or pid not in self._background_commands:
                    return _error_json(f"Unknown background command pid: {pid}")
                handle = self._background_commands[pid]
                killed = handle.kill()
                self._background_commands.pop(pid, None)
                return (
                    f"Background command {pid} terminated successfully."
                    if killed
                    else f"Background command {pid} was not found or already exited."
                )
            if action == "server":
                cmd = require_non_empty(command, "command")
                if port is None:
                    return _error_json("port is required for server")
                handle = self.sandbox.commands.run(cmd, background=True)
                self._background_commands[handle.pid] = handle
                time.sleep(2)
                return json.dumps({"url": f"http://{self.sandbox.get_host(port)}", "pid": handle.pid})
        except Exception as exc:
            return _error_json(f"Error in e2b_run ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")

    def e2b_inspect(  # noqa: C901, PLR0911
        self,
        action: _E2BInspectAction,
        path: str = "/",
        encoding: str = "utf-8",
        duration_seconds: int = 5,
        port: int | None = None,
    ) -> str:
        """Read-only inspection of E2B sandbox files, status, running sandboxes, and public URLs.

        Actions:
        - `list_files`: list entries under `path` (default `/`).
        - `read_file`: stream and decode sandbox file `path` using `encoding` (capped at 64 MiB).
        - `watch_directory`: observe changes under `path` for `duration_seconds`.
        - `status` / `list_sandboxes`: return current sandbox ID or list running sandboxes.
        - `public_url`: return public HTTP URL for sandbox `port`.
        """
        try:
            if action == "list_files":
                files = self.sandbox.files.list(path)
                if not files:
                    return f"No files found in {path}"
                lines = [f"Contents of {path}:"]
                for entry in files:
                    entry_type = str(getattr(entry, "type", "")).lower()
                    label = "Directory" if entry_type in {"dir", "directory", "filetype.dir"} else "File"
                    size = f"{entry.size} bytes" if getattr(entry, "size", None) is not None else "Unknown size"
                    lines.append(f"- {entry.name} ({label}, {size})")
                return "\n".join(lines) + "\n"
            if action == "read_file":
                with _sandbox_file_chunks(self.sandbox, require_non_empty(path, "path")) as chunks:
                    return b"".join(chunks).decode(encoding, errors="replace")
            if action == "watch_directory":
                target = require_non_empty(path, "path")
                watcher = self.sandbox.files.watch_dir(target)
                changes: list[str] = []
                try:
                    deadline = time.monotonic() + max(0, duration_seconds)
                    while time.monotonic() < deadline:
                        changes.extend(f"{event.type} - {event.name}" for event in watcher.get_new_events())
                        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                finally:
                    watcher.stop()
                summary = "\n".join(changes)
                msg = (
                    f"Changes detected in {target} over {duration_seconds} seconds:\n{summary}"
                    if changes
                    else f"No changes detected in {target} over {duration_seconds} seconds"
                )
                return json.dumps({"status": "success", "message": msg})
            if action == "status":
                return str(getattr(self.sandbox, "sandbox_id", None) or getattr(self.sandbox, "id", "Unknown"))
            if action == "list_sandboxes":
                from e2b_code_interpreter import Sandbox  # noqa: PLC0415

                _reject_ambient_e2b_debug()
                running = Sandbox.list(api_key=self.api_key, domain=_DEFAULT_E2B_DOMAIN, debug=False).next_items()
                info = [
                    {
                        "sandbox_id": getattr(item, "sandbox_id", "Unknown"),
                        "started_at": str(getattr(item, "started_at", "Unknown")),
                        "template_id": getattr(item, "template_id", "Unknown"),
                        "metadata": getattr(item, "metadata", {}),
                    }
                    for item in running
                ]
                return json.dumps({"status": "success", "sandboxes": info}, indent=2)
            if action == "public_url":
                return (
                    _error_json("port is required for public_url")
                    if port is None
                    else f"http://{self.sandbox.get_host(port)}"
                )
        except Exception as exc:
            return _error_json(f"Error in e2b_inspect ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")

    def _execution_result(self, result_index: int) -> tuple[Any | None, str | None]:
        if self.last_execution is None:
            return None, "No code has been executed yet"
        results = self.last_execution.results
        if result_index < 0 or result_index >= len(results):
            return None, f"Result index {result_index} is out of range. Only {len(results)} results available."
        return results[result_index], None

    def _download_png(self, result_index: int, output_path: str | None) -> ToolValue:
        result, error = self._execution_result(result_index)
        if error is not None or result is None:
            return ToolValue(content=error or "No code has been executed yet")
        if not result.png:
            return ToolValue(content=f"Result at index {result_index} is not a PNG image")
        png_bytes = base64.b64decode(result.png)
        image_id = str(uuid4())
        image = Image(id=image_id, content=png_bytes, mime_type="image/png", original_prompt=f"Result {result_index}")
        base_msg = f"Image added as artifact with ID {image_id}"
        if not output_path:
            return ToolValue(content=base_msg, images=(image,))
        try:
            root, relative = self._workspace_location(output_path)
            _write_within_root(root, relative, [png_bytes])
        except Exception as exc:
            return ToolValue(content=f"{base_msg}, but saving it failed: {exc}", images=(image,))
        return ToolValue(content=f"{base_msg} and saved to {output_path}", images=(image,))

    def _download_chart(self, result_index: int, output_path: str | None, *, add_as_artifact: bool) -> ToolValue:
        result, error = self._execution_result(result_index)
        if error is not None or result is None:
            prefix = "" if self.last_execution is None else "Error extracting chart data: "
            return ToolValue(content=f"{prefix}{error or 'No code has been executed yet'}")
        target_path = output_path or f"chart-data-{result_index}.json"
        if result.chart is None:
            return ToolValue(content=f"Result at index {result_index} does not contain interactive chart data")
        try:
            chart = result.chart.to_dict()
            root, relative = self._workspace_location(target_path)
            _write_within_root(root, relative, [json.dumps(chart, indent=2).encode()])
        except Exception as exc:
            return ToolValue(content=f"Error extracting chart data: {exc}")
        labels = (("title", "Title"), ("x_label", "X-axis"), ("y_label", "Y-axis"))
        summary = "\n".join(
            [
                f"Interactive {chart.get('type', 'unknown')} chart data saved to {target_path}",
                *(f"{label}: {chart[key]}" for key, label in labels if key in chart),
            ],
        )
        if not add_as_artifact or not result.png:
            return ToolValue(content=summary)
        image_value = self._download_png(result_index, None)
        return ToolValue(content=f"{summary}\nChart image: {image_value.content}", images=image_value.images)

    def e2b_files(  # noqa: PLR0911
        self,
        action: _E2BFilesAction,
        file_path: str | None = None,
        sandbox_path: str | None = None,
        local_path: str | None = None,
        content: str | None = None,
        result_index: int = 0,
        add_as_artifact: bool = True,
    ) -> str | ToolValue:
        """Write sandbox files, transfer files with the agent workspace, or save execution PNG/chart artifacts.

        Actions:
        - `write`: write UTF-8 `content` to `sandbox_path` (or `file_path`) in the sandbox.
        - `upload`: upload local `file_path` (confined by agent `file_access`, <=64 MiB) to `sandbox_path`.
        - `download`: stream `sandbox_path` (<=64 MiB) to workspace-relative `local_path`.
        - `download_png`: attach PNG result at `result_index` as image media and optionally save to `local_path`.
        - `download_chart`: save interactive chart JSON at `result_index` to `local_path` (optionally with PNG artifact).
        """
        if action == "download_png":
            return self._download_png(result_index, local_path)
        if action == "download_chart":
            return self._download_chart(result_index, local_path, add_as_artifact=add_as_artifact)
        try:
            if action == "write":
                dest = require_non_empty(sandbox_path or file_path, "sandbox_path")
                if content is None:
                    return _error_json("Error writing file: content is required")
                return str(self.sandbox.files.write(dest, content.encode("utf-8")).path)
            if action == "upload":
                src = require_non_empty(file_path, "file_path")
                authorized = resolve_agent_file(
                    src,
                    workspace_root=self._workspace_root,
                    file_access=self._file_access,
                    field_name="E2B upload",
                )
                payload = read_regular_file_within_root(authorized.root, authorized.relative)
                return str(self.sandbox.files.write(sandbox_path or Path(src).name, payload).path)
            if action == "download":
                src = require_non_empty(sandbox_path, "sandbox_path")
                dest = local_path or Path(src).name
                root, relative = self._workspace_location(dest)
                with _sandbox_file_chunks(self.sandbox, src) as chunks:
                    _write_within_root(root, relative, chunks)
                return dest
        except Exception as exc:
            verb = {"write": "writing", "upload": "uploading", "download": "downloading"}.get(action, action)
            return _error_json(f"Error {verb} file: {exc}")
        return _error_json(f"Unsupported action: {action}")

    def e2b_lifecycle(
        self,
        action: _E2BLifecycleAction,
        timeout: int | None = None,
    ) -> str:
        """Update timeout or shut down the E2B sandbox.

        Actions:
        - `set_timeout`: update sandbox timeout to `timeout` seconds.
        - `shutdown`: immediately kill the sandbox.
        """
        try:
            if action == "set_timeout":
                if timeout is None or timeout <= 0:
                    return _error_json("timeout must be a positive integer")
                self.sandbox.set_timeout(timeout)
                return str(timeout)
            if action == "shutdown":
                result = self.sandbox.kill()
                self._sandbox = None
                self._background_commands.clear()
                return json.dumps({"status": "success", "message": "Sandbox shut down successfully", "content": result})
        except Exception as exc:
            return _error_json(f"Error in e2b_lifecycle ({action}): {exc}")
        return _error_json(f"Unsupported action: {action}")
