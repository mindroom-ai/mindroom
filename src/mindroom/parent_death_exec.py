"""Supervise a command group that Linux kills when this process's parent exits."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType

_PR_SET_PDEATHSIG = 1
_PARENT_DEATH_SIGNAL = signal.SIGUSR1


def _kill_process_group(_signum: int, _frame: FrameType | None) -> None:
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _keep_watchdog_alive(_signum: int, _frame: FrameType | None) -> None:
    pass


def _main(argv: list[str]) -> int:
    expected_parent_pid = int(argv[1])
    command = argv[2:]
    signal.signal(_PARENT_DEATH_SIGNAL, _kill_process_group)
    signal.signal(signal.SIGTERM, _keep_watchdog_alive)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, _PARENT_DEATH_SIGNAL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent_pid:
        _kill_process_group(_PARENT_DEATH_SIGNAL, None)

    process = subprocess.Popen(command)
    return_code = process.wait()
    if return_code >= 0:
        return return_code

    child_signal = -return_code
    if child_signal not in {signal.SIGKILL, signal.SIGSTOP}:
        signal.signal(child_signal, signal.SIG_DFL)
    os.kill(os.getpid(), child_signal)
    return 128 + child_signal


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
