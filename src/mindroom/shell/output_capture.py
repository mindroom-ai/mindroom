"""Workspace output-file destination policy and atomic publication composing ``SpoolCapture``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import uuid
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.path_confinement import open_directory_within_root, open_regular_file_at
from mindroom.shell.background_log import sweep_generated_output
from mindroom.shell.output import SpoolCapture, format_shell_completion

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from mindroom.shell.output import ShellOutputBuffer

_SPILL_DIRECTORY = "mindroom_tool_outputs"
_SPILL_NAME = re.compile(r"shell-spill-[0-9a-f]{32}\.txt")
_SPILL_INSPECT = " (inspect in parts with read_file offset/limit or grep/sed)."
_SPILL_STDERR_HEADER = b"Stderr:\n"
_SPILL_HINT_PATH = re.compile(
    r"^\[[^\n]*?(?:Full output|First (?P<limit>[0-9]+ (?:MiB|bytes))) saved to "
    rf"(?P<path>(?:[^\n]*/)?{_SPILL_DIRECTORY}/(?P<name>{_SPILL_NAME.pattern}))"
    rf"(?:{re.escape(_SPILL_INSPECT)})?\]$",
    re.MULTILINE,
)
_SPILL_STATUS = re.compile(r"(?:Exit code -?[0-9]+|Stopped after [0-9.e+]+s\.)")


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
    auto_save_threshold_bytes: int
    tools_use_workdir: bool = False

    @classmethod
    def from_payload(cls, payload: object) -> ShellSpillPolicy | None:
        """Validate the optional serialized spill policy sent to the supervisor."""
        if payload is None:
            return None
        if isinstance(payload, dict):
            root = payload.get("workspace_root")
            cap = payload.get("max_bytes")
            threshold = payload.get("auto_save_threshold_bytes")
            use_workdir = payload.get("tools_use_workdir")
            if (
                isinstance(root, str)
                and root
                and type(cap) is int
                and cap > 0
                and type(threshold) is int
                and threshold > 0
                and isinstance(use_workdir, bool)
            ):
                return cls(root, cap, threshold, use_workdir)
        msg = "Invalid shell spill policy."
        raise ValueError(msg)


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class PublishedShellSpill:
    """A saved file whose receipt takes priority over output when bounding the inline preview."""

    path: Path
    threshold: int
    truncated_at: str | None

    @property
    def hint(self) -> str:
        """Link to complete or capped output, with suggestions for reading it."""
        return self._summary + _SPILL_INSPECT

    @property
    def _summary(self) -> str:
        prefix = f"First {self.truncated_at}" if self.truncated_at else "Full output"
        return f"{prefix} saved to {self.path}"

    def bound_preview(
        self,
        message: str,
        *,
        status: str = "",
        measure: Callable[[str], int] = _utf8_size,
    ) -> str:
        """Keep the status and spill receipt, then fit as much of the tail as the byte budget allows."""
        if measure(message) <= self.threshold:
            return message
        prefix = f"{status}\n" if status else ""
        header = f"{prefix}[Inline preview clipped. {self.hint}]"
        room = self.threshold - measure(header)
        if room <= 0:
            # The labeled receipt is mandatory, even for a budget smaller than the path itself.
            return f"{prefix}[{self._summary}]"
        output = message.removeprefix(prefix).replace(f" {self.hint}]", "]", 1)
        # Both UTF-8 and JSON use at least one byte per character. Search for the longest whole-character suffix.
        count = bisect_right(
            range(1, min(len(output), room) + 1),
            self.threshold,
            key=lambda length: measure(f"{header}\n{output[-length:]}"),
        )
        return f"{header}\n{output[-count:]}" if count else header


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
                spool = self._spool = SpoolCapture(
                    max_bytes=self._policy.max_bytes,
                    directory=self._policy.workspace_root,
                    truncate_on_cap=True,
                )
            except OSError:
                self.close()
                return
            # Both snapshots still contain their entire output; the triggering chunk is not in either yet.
            self._append_spooled(spool, self._stdout_buf.text(), is_stderr=False)
            self._append_spooled(spool, self._stderr_buf.text(), is_stderr=True)
        self._append_spooled(spool, text, is_stderr=is_stderr)

    @staticmethod
    def _append_spooled(spool: SpoolCapture, text: str, *, is_stderr: bool) -> None:
        if not text or spool.stdout.truncated or spool.stderr.truncated:
            return
        stream, other = (spool.stderr, spool.stdout) if is_stderr else (spool.stdout, spool.stderr)
        # A combined file needs a stderr label and potentially a newline before it. Reserve those bytes up front,
        # even if stdout is still growing and we do not know whether it will end with its own newline.
        framing = len(_SPILL_STDERR_HEADER) + 1 if other.byte_count else 0
        stream.append(text, other_bytes=other.byte_count + framing)

    def close(self) -> None:
        """Discard capture and disable it even if the pipe readers continue to run."""
        self._closed = True
        if self._spool is not None:
            self._spool.close()
            self._spool = None

    async def publish(self) -> PublishedShellSpill | None:
        """Transfer finished spools to the publisher; cancellation can't close them during a copy."""
        self._closed = True
        spool, self._spool = self._spool, None
        if spool is None:
            return None
        return await asyncio.shield(asyncio.to_thread(self._publish_and_close, spool))

    def _publish_and_close(self, spool: SpoolCapture) -> PublishedShellSpill | None:
        try:
            return self._publish(spool)
        finally:
            spool.close()

    def _publish(self, spool: SpoolCapture) -> PublishedShellSpill | None:
        if spool.stdout.error or spool.stderr.error:
            return None
        try:
            root = Path(self._policy.workspace_root).expanduser().absolute()
        except (OSError, RuntimeError):
            return None
        relative = Path(_SPILL_DIRECTORY, f"shell-spill-{uuid.uuid4().hex}.txt")
        try:
            with open_directory_within_root(root, relative.parent, create=True) as directory_fd:
                name = relative.name
                fd = open_regular_file_at(directory_fd, name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb", closefd=False) as target:
                        _write_spill(spool, target)
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
        limit = None
        if spool.stdout.truncated or spool.stderr.truncated:
            cap = self._policy.max_bytes
            mib = 1024 * 1024
            limit = f"{cap // mib} MiB" if cap % mib == 0 else f"{cap} bytes"
        return PublishedShellSpill(saved_path, self._policy.auto_save_threshold_bytes, limit)


def _write_spill(spool: SpoolCapture, target: BinaryIO) -> None:
    stdout, stderr = spool.stdout, spool.stderr
    stdout.file.seek(0)
    shutil.copyfileobj(stdout.file, target, length=65536)
    if stderr.size:
        if ending := stdout.tail(1):
            if ending != b"\n":
                target.write(b"\n")
            target.write(_SPILL_STDERR_HEADER)
        stderr.file.seek(0)
        shutil.copyfileobj(stderr.file, target, length=65536)


def format_named_shell_spill(
    text: str,
    *,
    worker_header: str,
    mount_dir: str | None,
    auto_save_threshold_bytes: int,
) -> str | None:
    """Budget the preview again after a named worker adds its header and workspace path prefix."""
    if (match := _SPILL_HINT_PATH.search(text)) is None:
        return None
    first_line = text.partition("\n")[0]
    status = first_line if _SPILL_STATUS.fullmatch(first_line) else ""
    path = Path(mount_dir, _SPILL_DIRECTORY, match["name"]) if mount_dir is not None else Path(match["path"])
    text = f"{text[: match.start('path')]}{path}{text[match.end('path') :]}"
    receipt = PublishedShellSpill(path, auto_save_threshold_bytes, match["limit"])
    leading = f"{worker_header}\n{status}" if status else worker_header
    return receipt.bound_preview(f"{worker_header}\n{text}", status=leading)


def bound_shell_spill_preview(text: str, *, max_bytes: int, measure: Callable[[str], int]) -> str | None:
    """Return a labeled spill result fitted to the caller's encoding, or None for ordinary output."""
    if (match := _SPILL_HINT_PATH.search(text)) is None:
        return None
    first_line, _, rest = text.partition("\n")
    worker = first_line if first_line.startswith("[worker: ") and first_line.endswith("]") else ""
    status = rest.partition("\n")[0] if worker else first_line
    leading = "\n".join(line for line in (worker, status if _SPILL_STATUS.fullmatch(status) else "") if line)
    receipt = PublishedShellSpill(Path(match["path"]), max_bytes, match["limit"])
    return receipt.bound_preview(text, status=leading, measure=measure)
