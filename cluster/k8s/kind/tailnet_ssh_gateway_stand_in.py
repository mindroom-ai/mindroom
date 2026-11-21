"""The two connections the tailnet SSH gateway makes for a session, run in a pod by ``smoke_tailnet_ssh_worker``.

That pod is the one the runtime chart gives the gateway, with this file as its command and no tailnet: it dials the
primary at the address the chart gives the gateway, is told there where a worker's sshd is, dials that, and sends the
primary what sshd sent first. The chart's network policies admit both connections from the gateway's pods alone.
"""

from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlsplit

# The primary listens only once its worker's sshd runs, which follows two worker starts.
_PRIMARY_SECONDS = 900
# A name may take several tries to resolve under the gateway's egress policy, as it does with kind's network plugin.
_WORKER_SECONDS = 60


def _dial(host: str, port: int, seconds: float) -> socket.socket:
    """Connect to ``host`` and ``port``, tried again for ``seconds`` while nothing accepts there."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return socket.create_connection((host, port), timeout=5)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def main() -> None:
    """Ask the primary where sshd is, and answer with sshd's first eight bytes, or with why none came."""
    primary = urlsplit(os.environ["TAILNET_SSH_PRIMARY_URL"])
    if primary.hostname is None or primary.port is None:
        msg = f"TAILNET_SSH_PRIMARY_URL names no host and port: {primary.geturl()!r}"
        raise ValueError(msg)
    with _dial(primary.hostname, primary.port, _PRIMARY_SECONDS) as asked, asked.makefile("rwb") as stream:
        host, port = stream.readline().decode().split()
        try:
            with _dial(host, int(port), _WORKER_SECONDS) as sshd, sshd.makefile("rb") as sent:
                first = sent.read(8)
        except OSError as error:
            first = f"{type(error).__name__}: {error}".encode()
        stream.write(first)
        stream.flush()
    print(f"[gateway] {host}:{port} sent {first!r}", flush=True)


if __name__ == "__main__":
    main()
