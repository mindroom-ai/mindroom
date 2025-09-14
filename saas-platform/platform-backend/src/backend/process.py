"""Process execution utilities for backend operations."""

from __future__ import annotations

import asyncio

from backend.config import logger


async def run_cmd(cmd: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command asynchronously and return (returncode, stdout, stderr).

    - cmd: full argv, e.g., ["helm", "install", ...]
    - env: optional environment overlay
    - cwd: optional working directory
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd
        )
        stdout_b, stderr_b = await proc.communicate()
        return proc.returncode, stdout_b.decode(), stderr_b.decode()
    except FileNotFoundError:
        logger.exception("command not found: %s", cmd[0] if cmd else "<empty>")
        return 127, "", f"command not found: {cmd[0] if cmd else '<empty>'}"
    except Exception:
        logger.exception("Error executing command: %s", " ".join(cmd))
        return 1, "", "unknown execution error"


async def run_helm(args: list[str]) -> tuple[int, str, str]:
    """Run a helm command and return (returncode, stdout, stderr)."""
    cmd = ["helm", *args]
    return await run_cmd(cmd)
