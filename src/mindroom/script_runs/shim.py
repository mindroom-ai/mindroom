"""Private stdlib-only entry point for one supervised background script."""

from __future__ import annotations

import hashlib
import os
import runpy
import secrets
import signal
import stat
import sys
from pathlib import Path

_CONTROL_STATE_PATH_ENV = "MINDROOM_CONTROL_STATE_PATH"
_SOURCE_DIGEST_ENV = "MINDROOM_SCRIPT_SOURCE_DIGEST"
_TOKEN_PATH_ENV = "MINDROOM_SCRIPT_TOKEN_PATH"  # noqa: S105 - this names a path, not a token.
_WORKSPACE_ROOT_ENV = "MINDROOM_SCRIPT_WORKSPACE_ROOT"
_MAX_SOURCE_BYTES = 128 * 1024
_MAX_TOKEN_BYTES = 4096


def _validated_file(
    raw_path: str,
    *,
    workspace_root: Path,
    label: str,
    byte_limit: int,
) -> Path:
    path = Path(raw_path)
    if path.is_symlink():
        msg = f"{label} must not be a symbolic link."
        raise ValueError(msg)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(workspace_root):
        msg = f"{label} must stay inside the worker workspace."
        raise ValueError(msg)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        msg = f"{label} must be a regular file."
        raise ValueError(msg)
    if metadata.st_size <= 0 or metadata.st_size > byte_limit:
        msg = f"{label} exceeds its supported size."
        raise ValueError(msg)
    return resolved


def _validate_source_digest(source_path: Path) -> None:
    expected_digest = os.environ.get(_SOURCE_DIGEST_ENV, "")
    actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if not expected_digest or not secrets.compare_digest(actual_digest, expected_digest):
        msg = "Script source digest does not match the launch receipt."
        raise ValueError(msg)


def _handle_sigterm(_signum: int, _frame: object) -> None:
    raise SystemExit(128 + signal.SIGTERM)


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: python -m mindroom.script_runs.shim SOURCE_PATH TOKEN_PATH", file=sys.stderr)
        return 2

    workspace_root = Path(os.environ.get(_WORKSPACE_ROOT_ENV, "")).resolve(strict=True)
    token_path = _validated_file(
        argv[2],
        workspace_root=workspace_root,
        label="Script capability file",
        byte_limit=_MAX_TOKEN_BYTES,
    )
    try:
        source_path = _validated_file(
            argv[1],
            workspace_root=workspace_root,
            label="Script source",
            byte_limit=_MAX_SOURCE_BYTES,
        )
        if Path(os.environ.get(_TOKEN_PATH_ENV, "")).resolve(strict=True) != token_path:
            msg = "Script capability path does not match the launch environment."
            raise ValueError(msg)
        _validate_source_digest(source_path)
        os.environ.pop(_CONTROL_STATE_PATH_ENV, None)
        signal.signal(signal.SIGTERM, _handle_sigterm)
        runpy.run_path(str(source_path), run_name="__main__")
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        token_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
