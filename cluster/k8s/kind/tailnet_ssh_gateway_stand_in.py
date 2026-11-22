"""Read sshd's banner through the worker Service from a pod with the gateway's chart labels."""

from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlsplit


def main() -> None:
    """Wait for the primary to hand off an already-ready SSH endpoint."""
    primary = urlsplit(os.environ["TAILNET_SSH_PRIMARY_URL"])
    assert primary.hostname
    assert primary.port
    deadline = time.monotonic() + 900
    while True:
        try:
            probe = socket.create_connection((primary.hostname, primary.port), timeout=5)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    with probe, probe.makefile("rb") as request:
        host, port = request.readline().decode().split()
        with socket.create_connection((host, int(port)), timeout=10) as sshd, sshd.makefile("rb") as stream:
            probe.sendall(stream.read(8))


if __name__ == "__main__":
    main()
