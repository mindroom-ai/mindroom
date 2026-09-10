"""Run each native knowledge read in a bounded, short-lived child process."""

from __future__ import annotations

import os
import subprocess
import sys
from threading import BoundedSemaphore

from mindroom.knowledge.read_protocol import (
    MAX_FRAME_BYTES,
    ReadRequest,
    ReadResult,
    request_adapter,
    result_adapter,
)

# Avoid turning simultaneous searches into unbounded native index copies.
_read_slots = BoundedSemaphore(2)
_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "SYSTEMROOT",
    "WINDIR",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "NIX_LD_LIBRARY_PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
)


def read_chroma(request: ReadRequest, *, timeout: float = 30.0) -> ReadResult:
    """Run off the event loop; subprocess.run kills and reaps a timed-out child."""
    payload = request_adapter.dump_json(request)
    if len(payload) > MAX_FRAME_BYTES:
        message = "Knowledge read request exceeds transport size limit"
        raise ValueError(message)
    if not _read_slots.acquire(timeout=timeout):
        message = "Knowledge reader is busy; try again shortly"
        raise RuntimeError(message)
    try:
        env = {name: os.environ[name] for name in _CHILD_ENV_KEYS if name in os.environ}
        env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", TOKIO_WORKER_THREADS="2")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "mindroom.knowledge.read_worker"],
                input=payload,
                stdout=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            message = "Knowledge read timed out"
            raise TimeoutError(message) from exc
        if len(completed.stdout) > MAX_FRAME_BYTES:
            message = "Knowledge read result exceeds transport size limit"
            raise RuntimeError(message)
        result = result_adapter.validate_json(completed.stdout)
        if result.error_type is not None:
            message = f"Knowledge read failed ({result.error_type})"
            raise RuntimeError(message)
        return result
    finally:
        _read_slots.release()
