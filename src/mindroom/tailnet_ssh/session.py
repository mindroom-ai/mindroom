"""Run sshd's ForceCommand using the runner's stored launch, not the image's passwd shell and home.

The workspace is HOME, not a file boundary. Keep imports stdlib-only for session startup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

LAUNCH_PATH = Path("/tmp/mindroom-ssh/launch.json")  # noqa: S108
_SFTP_SERVER = "/usr/lib/openssh/sftp-server"
_TERMINAL_ONLY = ("PROMPT_COMMAND", "INPUTRC")
_FROM_SSHD = ("TERM", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


def _session_command(launch: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[list[str], dict[str, str], str]:
    """Resolve the SSH session's command, environment, and workspace."""
    cwd = launch["cwd"]
    env = {name: value for name, value in launch["env"].items() if name not in _TERMINAL_ONLY}
    env.update(HOME=cwd, USER="mindroom", LOGNAME="mindroom", SHELL="/bin/bash")
    env.update({name: environ[name] for name in _FROM_SSHD if name in environ})
    command = environ.get("SSH_ORIGINAL_COMMAND")
    if command is None:
        # A login shell would read /etc/profile and overwrite the worker's PATH.
        argv = ["bash"]
    elif command == "internal-sftp":
        argv = [_SFTP_SERVER]
    else:
        argv = ["bash", "-c", command]
    return argv, env, cwd


def _main() -> None:
    argv, env, cwd = _session_command(json.loads(LAUNCH_PATH.read_text(encoding="utf-8")), os.environ)
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)  # noqa: S606


if __name__ == "__main__":
    _main()
