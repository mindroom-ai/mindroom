"""The program a worker's sshd runs for every session, as its ``ForceCommand``.

The image's passwd entry for the worker's uid gives ``/bin/sh`` and a home on the read-only root, so this module
starts the session instead: in the agent's workspace as its home, with the environment of the agent's shell, which
the runner keeps in ``LAUNCH_PATH`` (``mindroom.api.worker_ssh``). So a session reaches the network through the
proxy the agent's commands use, and ``scp notes.md host:`` lands where the agent looks.

A login gets Bash, but not as a login shell: that reads ``/etc/profile``, which on the image replaces ``PATH``, and
with it the agent's tools. The shell still reads the workspace's ``.bashrc``.

It imports only the standard library, because every session starts it.
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
# What the launch sets for the runner's own terminals: its prompt marker, and no readline settings.
_TERMINAL_ONLY = ("PROMPT_COMMAND", "INPUTRC")
# What sshd says about the connection and its terminal.
_FROM_SSHD = ("TERM", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


def session_command(launch: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[list[str], dict[str, str], str]:
    """Return the program, environment and directory of the session sshd started with ``environ``.

    ``launch`` is the stored launch of the agent's shell. The session runs the command the client named, the SFTP
    server for the ``sftp`` subsystem (``internal-sftp`` in ``sshd_config``), or else an interactive Bash.
    """
    cwd = launch["cwd"]
    env = {name: value for name, value in launch["env"].items() if name not in _TERMINAL_ONLY}
    env.update(HOME=cwd, USER="mindroom", LOGNAME="mindroom", SHELL="/bin/bash")
    env.update({name: environ[name] for name in _FROM_SSHD if name in environ})
    command = environ.get("SSH_ORIGINAL_COMMAND")
    if command is None:
        argv = ["bash"]
    elif command == "internal-sftp":
        argv = [_SFTP_SERVER]
    else:
        argv = ["bash", "-c", command]
    return argv, env, cwd


def main() -> None:
    """Become the session's program."""
    argv, env, cwd = session_command(json.loads(LAUNCH_PATH.read_text(encoding="utf-8")), os.environ)
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)  # noqa: S606


if __name__ == "__main__":
    main()
