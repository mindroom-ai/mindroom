"""Workspace output-file destination policy and atomic publication composing ``SpoolCapture``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.shell.output import SpoolCapture, SpoolStream, format_shell_completion


@dataclass(frozen=True)
class ShellOutputDestination:
    """Non-executable destination policy that can cross the supervisor boundary."""

    workspace_root: str
    path: str
    max_bytes: int
    # Whether the agent's tools work in a workdir, so the receipt names the saved file by absolute path.
    tools_use_workdir: bool = False

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
        tools_use_workdir = data.get("tools_use_workdir")
        if (
            not isinstance(workspace_root, str)
            or not isinstance(path, str)
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
            or not isinstance(tools_use_workdir, bool)
        ):
            msg = "Invalid shell output destination policy."
            raise ValueError(msg)
        return cls(workspace_root=workspace_root, path=path, max_bytes=max_bytes, tools_use_workdir=tools_use_workdir)


class ShellOutputCapture:
    """Compose ``SpoolCapture`` with workspace destination validation and atomic publication."""

    def __init__(self, destination: ShellOutputDestination) -> None:
        self.destination = destination
        self.spool = SpoolCapture(max_bytes=destination.max_bytes, directory=destination.workspace_root)

    @property
    def stdout(self) -> SpoolStream:
        """Return the spooled stdout stream."""
        return self.spool.stdout

    @property
    def stderr(self) -> SpoolStream:
        """Return the spooled stderr stream."""
        return self.spool.stderr

    @property
    def incomplete(self) -> bool:
        """Return whether stream capture ended before full command output was collected."""
        return self.spool.incomplete

    @incomplete.setter
    def incomplete(self, value: bool) -> None:
        self.spool.incomplete = value

    @property
    def interruption(self) -> str | None:
        """Return the owner-provided explanation when incomplete output went unsaved."""
        return self.spool.interruption

    @interruption.setter
    def interruption(self, value: str | None) -> None:
        self.spool.interruption = value

    @property
    def closed(self) -> bool:
        """Return whether the underlying spool files have been closed."""
        return self.spool.closed

    def publish(self, return_code: int | None) -> str:
        """Revalidate and atomically publish using the shared output-file policy."""
        receipt = self._publish_receipt(return_code)
        if return_code and self.interruption is None:
            # The receipt is all the caller sees, so a failed or killed command, saved or not, must say how it
            # exited. A command its owner stopped, such as at its timeout, is explained by the interruption instead.
            receipt["mindroom_tool_output"]["exit_code"] = return_code
        if return_code == 0 and self.stderr.byte_count:
            # A success saves its stdout alone, so the receipt says how much it printed to stderr.
            receipt["mindroom_tool_output"]["stderr_bytes"] = self.stderr.byte_count
        return json.dumps(receipt)

    def _publish_receipt(self, return_code: int | None) -> dict[str, dict[str, object]]:
        # Keep the supervisor's ordinary startup independent of tool-output imports.
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
            error = (
                self.interruption
                or "Shell command or output capture was interrupted; no complete output file was saved."
            )
        if error is not None:
            return {"mindroom_tool_output": {"status": "error", "error": error}}
        assert return_code is not None
        try:
            request = prepare_tool_output_file(
                ToolOutputFilePolicy(
                    workspace_root=Path(self.destination.workspace_root),
                    max_bytes=self.destination.max_bytes,
                    tools_use_workdir=self.destination.tools_use_workdir,
                ),
                tool_name=SHELL_FUNCTION,
                output_path=self.destination.path,
            )
            if isinstance(request, dict):
                return cast("dict[str, dict[str, object]]", request)
            output = (
                selected.read()
                if selected is not None
                else format_shell_completion(self.stdout.read(), self.stderr.read() or None, return_code=return_code)
            )
            return cast("dict[str, dict[str, object]]", finalize_tool_output_file(request, output))
        except OSError:
            return {"mindroom_tool_output": {"status": "error", "error": "Failed to publish complete shell output."}}

    def close(self) -> None:
        """Release unlinked capture files on completion, cancellation, or eviction."""
        self.spool.close()
