"""Capture the account's login-shell environment once for locally approved desktop commands."""

from __future__ import annotations

import asyncio
import os
import pwd
import tempfile
from pathlib import Path
from uuid import uuid4

from mindroom.constants import subprocess_path_with_prepends
from mindroom.desktop.shell import DesktopShellOutput
from mindroom.logging_config import get_logger
from mindroom.shell_execution import ProcessRecord, kill_all_records, run_command

logger = get_logger(__name__)

LOGIN_ENVIRONMENT_TIMEOUT_SECONDS = 5.0
_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_SYSTEM_PATH_PREPENDS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin")
_HELPER_ALLOWLIST = ("TMPDIR", "LANG")
# Per-process shell state describes the capture shell, not later commands in their own directories.
_SHELL_STATE_NAMES = frozenset({"OLDPWD", "PWD", "SHLVL", "_"})


async def capture_login_environment(
    *,
    shell: str | None = None,
    timeout_seconds: float = LOGIN_ENVIRONMENT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Return the login shell's exported environment, or a fixed allowlist when capture fails."""
    account = pwd.getpwuid(os.getuid())
    login_shell = shell or account.pw_shell or "/bin/sh"
    base = _base_environment(account)
    sentinel = f"MINDROOM_LOGIN_ENVIRONMENT_{uuid4().hex}"
    registry: dict[str, ProcessRecord] = {}
    environment = None
    with tempfile.TemporaryDirectory(prefix="mindroom-desktop-environment-") as directory:
        output = DesktopShellOutput(directory)
        try:
            result = await run_command(
                registry,
                namespace="login-environment",
                argv=[login_shell, "-l", "-i", "-c", f"printf '%s' {sentinel}; exec /usr/bin/env -0"],
                env=base,
                cwd=account.pw_dir if Path(account.pw_dir).is_dir() else "/",
                tail=0,
                timeout=timeout_seconds,
                output_capture=output,
                stdin=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                kill_group_after_exit=True,
            )
            if result.handle is None and output.completed and output.exit_code == 0 and not output.truncated:
                environment = _parse_environment(output.read(), sentinel)
        finally:
            # A login shell still running at the bound became a handle; stop its whole process group.
            kill_all_records(registry)
            output.release()
    if environment is None:
        logger.warning("desktop_login_environment_fallback", shell=login_shell)
        return base
    return environment


def _base_environment(account: pwd.struct_passwd) -> dict[str, str]:
    """Build the clean login-shell input, which is also the fallback when capture fails."""
    environment = {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": account.pw_shell or "/bin/sh",
    }
    environment.update({name: os.environ[name] for name in _HELPER_ALLOWLIST if name in os.environ})
    path = subprocess_path_with_prepends(
        _SYSTEM_PATH,
        prepend_entries=(*_SYSTEM_PATH_PREPENDS, str(Path(account.pw_dir) / ".local" / "bin")),
    )
    assert path is not None
    environment["PATH"] = path
    return environment


def _parse_environment(listing: bytes, sentinel: str) -> dict[str, str] | None:
    _, found, entries = listing.partition(sentinel.encode())
    if not found:
        return None
    environment: dict[str, str] = {}
    # `env -0` ends every entry with NUL, so text after the last NUL is not an entry.
    for entry in entries.split(b"\0")[:-1]:
        name, separator, value = os.fsdecode(entry).partition("=")
        if separator and name and name not in _SHELL_STATE_NAMES:
            environment[name] = value
    return environment if "PATH" in environment else None


__all__ = ["LOGIN_ENVIRONMENT_TIMEOUT_SECONDS", "capture_login_environment"]
