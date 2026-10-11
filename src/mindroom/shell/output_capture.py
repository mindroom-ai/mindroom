"""Bounded full shell capture owned by the process until output publication."""

from __future__ import annotations

import json
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast


@dataclass(frozen=True)
class ShellOutputDestination:
    """Non-executable destination policy that can cross the supervisor boundary."""

    workspace_root: str
    path: str
    max_bytes: int

    @classmethod
    def from_payload(cls, payload: object) -> ShellOutputDestination | None:
        """Validate the optional serialized destination sent to the supervisor."""
        if payload is None:
            return None
        if not isinstance(payload, dict):
            msg = "Invalid shell output destination policy."
            raise TypeError(msg)
        data = cast("dict[str, object]", payload)
        workspace_root, path, max_bytes = data.get("workspace_root"), data.get("path"), data.get("max_bytes")
        if (
            not isinstance(workspace_root, str)
            or not isinstance(path, str)
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
        ):
            msg = "Invalid shell output destination policy."
            raise ValueError(msg)
        return cls(workspace_root=workspace_root, path=path, max_bytes=max_bytes)


def format_shell_completion(stdout: str, stderr: str, *, return_code: int) -> str:
    """Format completed output without discarding either failure stream."""
    if return_code == 0:
        return stdout
    if stdout and stderr:
        return f"Error: {stdout}\nStderr:\n{stderr}"
    return f"Error: {stdout or stderr}"


class CapturedShellStream:
    """Spool decoded text up to the redirect cap without retaining it in memory."""

    def __init__(self, destination: ShellOutputDestination) -> None:
        self.file: BinaryIO = tempfile.TemporaryFile(dir=destination.workspace_root)  # noqa: SIM115
        self.max_bytes = destination.max_bytes
        self.byte_count = 0
        self.error: str | None = None
        self.reached_eof = False

    def append(self, text: str) -> None:
        """Append decoded output until the cap or a storage error is reached."""
        if self.error is not None or self.file.closed:
            return
        payload = text.encode("utf-8", errors="replace")
        self.byte_count += len(payload)
        if self.byte_count > self.max_bytes:
            self.error = f"Redirected shell output exceeds the {self.max_bytes} byte limit."
            return
        try:
            self.file.write(payload)
        except OSError:
            self.error = "Failed to capture complete shell output."

    def read(self) -> str:
        """Read the retained complete stream for atomic publication."""
        self.file.seek(0)
        return self.file.read().decode("utf-8")


class ShellOutputCapture:
    """Own both output streams and publish only a complete supported result."""

    def __init__(self, destination: ShellOutputDestination, cwd: str | None) -> None:
        self.destination = destination
        self.cwd = cwd
        self.stdout = CapturedShellStream(destination)
        try:
            self.stderr = CapturedShellStream(destination)
        except BaseException:
            self.stdout.file.close()
            raise
        self.incomplete = False
        self.closed = False

    def publish(self, return_code: int | None) -> str:
        """Revalidate and atomically publish using the shared output-file policy."""
        # Keep the supervisor's ordinary startup independent of Agno imports.
        from mindroom.tool_system.output_files import (  # noqa: PLC0415
            ToolOutputFilePolicy,
            finalize_tool_output_file,
            prepare_tool_output_file,
        )

        selected = self.stdout if return_code == 0 else None
        error = selected.error if selected is not None else self.stdout.error or self.stderr.error
        if return_code and self.stdout.byte_count + self.stderr.byte_count > self.destination.max_bytes:
            error = f"Redirected shell output exceeds the {self.destination.max_bytes} byte limit."
        if (
            self.incomplete
            or not self.stdout.reached_eof
            or not self.stderr.reached_eof
            or return_code is None
            or return_code < 0
        ):
            error = "Shell command or output capture was interrupted; no complete output file was saved."
        if error is not None:
            return json.dumps({"mindroom_tool_output": {"status": "error", "error": error}})
        assert return_code is not None
        try:
            request = prepare_tool_output_file(
                ToolOutputFilePolicy(
                    workspace_root=Path(self.destination.workspace_root),
                    max_bytes=self.destination.max_bytes,
                ),
                tool_name="run_shell_command",
                output_path=self.destination.path,
            )
            if isinstance(request, dict):
                return json.dumps(request)
            output = (
                selected.read()
                if selected is not None
                else format_shell_completion(self.stdout.read(), self.stderr.read(), return_code=return_code)
            )
            if self.cwd is not None:
                output = f"[cwd: {self.cwd}]\n{output}"
            return json.dumps(finalize_tool_output_file(request, output))
        except OSError:
            return json.dumps(
                {
                    "mindroom_tool_output": {"status": "error", "error": "Failed to publish complete shell output."},
                },
            )

    def close(self) -> None:
        """Release unlinked capture files on completion, cancellation, or eviction."""
        self.closed = True
        for stream in (self.stdout, self.stderr):
            with suppress(OSError):
                stream.file.close()
