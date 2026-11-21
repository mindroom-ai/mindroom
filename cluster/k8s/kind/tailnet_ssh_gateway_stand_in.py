"""Read sshd's banner through the worker Service from a pod with the gateway's chart labels."""

from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlsplit

_PRIMARY_SECONDS = 900  # The probe listens only once it has started sshd.
_WORKER_SECONDS = 60


def _dial(host: str, port: int, seconds: int) -> socket.socket:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return socket.create_connection((host, port), timeout=5)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def main() -> None:
    """Get the SSH endpoint from the primary and send back its first eight bytes."""
    primary = urlsplit(os.environ["TAILNET_SSH_PRIMARY_URL"])
    if primary.hostname is None or primary.port is None:
        msg = "Missing primary host or port"
        raise ValueError(msg)
    with _dial(primary.hostname, primary.port, _PRIMARY_SECONDS) as probe, probe.makefile("rb") as request:
        host, port = request.readline().decode().split()
        with _dial(host, int(port), _WORKER_SECONDS) as sshd, sshd.makefile("rb") as stream:
            banner = stream.read(8)
        probe.sendall(banner)
    print(f"[gateway] {host}:{port} sent {banner!r}", flush=True)


if __name__ == "__main__":
    main()
