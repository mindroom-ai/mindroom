"""Workspace output-file destination policy and atomic publication composing ``SpoolCapture``."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.path_confinement import open_directory_within_root, open_regular_file_at
from mindroom.shell.background_log import sweep_generated_output
from mindroom.shell.output import SpoolCapture, format_shell_completion

if TYPE_CHECKING:
    from typing import BinaryIO

    from mindroom.shell.output import ShellOutputBuffer

_SPILL_DIRECTORY = "mindroom_tool_outputs"
_SPILL_NAME = re.compile(r"shell-spill-[0-9a-f]{32}\.txt")
_SPILL_INSPECT = " (inspect in parts with read_file offset/limit or grep/sed)."
_SPILL_HINT_PATH = re.compile(
    r"(^\[[^\n]*?\. (?:Full output|First [0-9]+ (?:MiB|bytes)) saved to )"
    rf"(?:/(?:[^\n]*/)?)?{_SPILL_DIRECTORY}/({_SPILL_NAME.pattern})(?={re.escape(_SPILL_INSPECT)}\]$)",
    re.MULTILINE,
)


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
        self.stdout = self.spool.stdout
        self.stderr = self.spool.stderr
        # Why incomplete output went unsaved, when its owner knows better than "interrupted".
        self.interruption: str | None = None

    @property
    def incomplete(self) -> bool:
        """Return whether stream capture ended before full command output was collected."""
        return self.spool.incomplete

    @incomplete.setter
    def incomplete(self, value: bool) -> None:
        self.spool.incomplete = value

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


@dataclass(frozen=True)
class ShellSpillPolicy:
    """Workspace policy for best-effort spill, serializable across the supervisor boundary."""

    workspace_root: str
    max_bytes: int
    tools_use_workdir: bool = False

    @classmethod
    def from_payload(cls, payload: object) -> ShellSpillPolicy | None:
        """Validate the optional serialized spill policy sent to the supervisor."""
        if payload is None:
            return None
        if isinstance(payload, dict):
            root = payload.get("workspace_root")
            cap = payload.get("max_bytes")
            use_workdir = payload.get("tools_use_workdir")
            if isinstance(root, str) and root and type(cap) is int and cap > 0 and isinstance(use_workdir, bool):
                return cls(root, cap, use_workdir)
        msg = "Invalid shell spill policy."
        raise ValueError(msg)


class ShellOutputSpill:
    """Start best-effort capture just before either rolling buffer first drops data."""

    def __init__(self, policy: ShellSpillPolicy, stdout_buf: ShellOutputBuffer, stderr_buf: ShellOutputBuffer) -> None:
        self._policy = policy
        self._stdout_buf = stdout_buf
        self._stderr_buf = stderr_buf
        self._spool: SpoolCapture | None = None
        self._closed = False

    def append(self, text: str, *, is_stderr: bool = False) -> None:
        """Receive a decoded chunk before it is appended to its rolling buffer."""
        if self._closed:
            return
        if (spool := self._spool) is None:
            buf = self._stderr_buf if is_stderr else self._stdout_buf
            if not buf.would_cap(text):
                return
            try:
                spool = self._spool = SpoolCapture(max_bytes=self._policy.max_bytes, truncate_on_cap=True)
            except OSError:
                self.close()
                return
            # Both snapshots still contain their entire output; the triggering chunk is not in either yet.
            spool.stdout.append(self._stdout_buf.text())
            spool.stderr.append(self._stderr_buf.text(), other_bytes=spool.stdout.byte_count)
        if not (spool.stdout.truncated or spool.stderr.truncated):
            stream, other = (spool.stderr, spool.stdout) if is_stderr else (spool.stdout, spool.stderr)
            stream.append(text, other_bytes=other.byte_count)

    def close(self) -> None:
        """Discard capture and disable it even if the pipe readers continue to run."""
        self._closed = True
        if self._spool is not None:
            self._spool.close()
            self._spool = None

    def publish(self) -> str | None:
        """Save captured streams, returning a display hint only after successful publication."""
        spool = self._spool
        if spool is None or spool.stdout.error or spool.stderr.error:
            return None
        try:
            root = Path(self._policy.workspace_root).expanduser().absolute()
        except (OSError, RuntimeError):
            return None
        relative = Path(_SPILL_DIRECTORY, f"shell-spill-{uuid.uuid4().hex}.txt")
        truncated = spool.stdout.truncated or spool.stderr.truncated
        cap = self._policy.max_bytes
        mib = 1024 * 1024
        limit = f"{cap // mib} MiB" if cap % mib == 0 else f"{cap} bytes"
        trailer = f"\n[Spill truncated at {limit}; command output continued.]\n" if truncated else ""
        try:
            with open_directory_within_root(root, relative.parent, create=True) as directory_fd:
                name = relative.name
                fd = open_regular_file_at(directory_fd, name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb", closefd=False) as target:
                        _write_spill(spool, target, trailer)
                except OSError:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=directory_fd)
                    raise
                finally:
                    os.close(fd)
                sweep_generated_output(directory_fd, name_pattern=_SPILL_NAME, active_names={name})
        except (OSError, ValueError):
            return None
        saved_path = root / relative if self._policy.tools_use_workdir else relative
        prefix = f"First {limit}" if truncated else "Full output"
        return f"{prefix} saved to {saved_path}{_SPILL_INSPECT}"


def _write_spill(spool: SpoolCapture, target: BinaryIO, trailer: str) -> None:
    stdout, stderr = spool.stdout, spool.stderr
    stdout.file.seek(0)
    shutil.copyfileobj(stdout.file, target, length=65536)
    if stderr.size:
        if stdout.tail(1) not in (b"", b"\n"):
            target.write(b"\n")
        target.write(b"Stderr:\n")
        stderr.file.seek(0)
        shutil.copyfileobj(stderr.file, target, length=65536)
    if trailer:
        target.write(trailer.encode("utf-8"))


def rewrite_shell_spill_hint(text: str, mount_dir: str) -> str:
    """Translate the generated path in a shell truncation notice to the primary's mount-relative path."""
    return _SPILL_HINT_PATH.sub(lambda m: f"{m[1]}{mount_dir}/{_SPILL_DIRECTORY}/{m[2]}", text, count=1)
