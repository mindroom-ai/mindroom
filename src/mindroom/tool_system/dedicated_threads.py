"""An executor that gives each call its own thread, for blocking work that may run as long as a job does.

A bounded pool would let a few long calls starve every short offload behind them.
"""

from __future__ import annotations

import threading
from concurrent.futures import Executor, Future
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class _ThreadPerCallExecutor(Executor):
    def __init__(self, thread_name: str) -> None:
        self._thread_name = thread_name

    def submit[Result](self, fn: Callable[..., Result], /, *args: object, **kwargs: object) -> Future[Result]:
        future: Future[Result] = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        # Daemon, so an unlimited call never holds up process exit.
        threading.Thread(target=run, name=self._thread_name, daemon=True).start()
        return future


# Runs the blocking calls of background jobs, in the primary and in sandbox runners.
JOB_CALL_EXECUTOR: Executor = _ThreadPerCallExecutor("mindroom-job-call")
# Runs worker proxy calls, which wait as long as their worker's command, such as a shell command with a long timeout.
WORKER_PROXY_CALL_EXECUTOR: Executor = _ThreadPerCallExecutor("mindroom-worker-proxy")
