"""Answer health probes on the API address while `mindroom run` waits for hosted pairing approval."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.runtime_state import get_runtime_state, reset_runtime_state, set_runtime_starting

if TYPE_CHECKING:
    from collections.abc import Iterator

_PAIRING_WAIT_DETAIL = "Waiting for local pairing approval"


class _ProbeHandler(BaseHTTPRequestHandler):
    """Report liveness and not-ready readiness; the real API replaces this server after pairing."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send_json(200, {"status": "healthy"})
        elif path == "/api/ready":
            state = get_runtime_state()
            self._send_json(503, {"status": state.phase, "detail": state.detail})
        else:
            self._send_json(404, {"detail": "Not Found"})

    def _send_json(self, status: int, payload: dict[str, str | None]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep probe requests out of the pairing instructions printed to the terminal."""


class _ProbeServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int) -> None:
        # Pick the address family from the host so IPv6 binds such as `::` work like the real API server.
        self.address_family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][0]
        super().__init__((host, port), _ProbeHandler)


@contextmanager
def serve_pairing_probes(host: str, port: int) -> Iterator[None]:
    """Serve `/api/health` and a not-ready `/api/ready` on the API address until the block exits.

    Container probes then see a live process that is waiting for pairing instead of a closed port.
    Raises OSError when the address cannot be bound, before pairing starts.
    """
    server = _ProbeServer(host, port)
    set_runtime_starting(_PAIRING_WAIT_DETAIL)
    thread = threading.Thread(target=server.serve_forever, name="pairing_probes", daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        reset_runtime_state()
