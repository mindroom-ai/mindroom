"""Answer health probes on the API address while `mindroom run` waits for hosted pairing approval."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

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
            self._send_json(503, {"status": "starting", "detail": _PAIRING_WAIT_DETAIL})
        else:
            self._send_json(404, {"detail": "Not Found"})

    def _send_json(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep probe requests out of the pairing instructions printed to the terminal."""


class _ProbeServer(ThreadingHTTPServer):
    def __init__(self, family: socket.AddressFamily, address: tuple) -> None:
        self.address_family = family
        super().__init__(address, _ProbeHandler)

    def server_bind(self) -> None:
        # Like asyncio's create_server behind Uvicorn, an IPv6 listener leaves IPv4 to its own listener.
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


@contextmanager
def serve_pairing_probes(host: str, port: int) -> Iterator[None]:
    """Serve `/api/health` and a not-ready `/api/ready` on the API address until the block exits.

    Container probes then see a live process that is waiting for pairing instead of a closed port.
    Raises OSError naming the address when it cannot be resolved or bound, before pairing starts.
    """
    with ExitStack() as servers:
        try:
            # Bind every resolved address in resolver order, as the real API server does,
            # so `localhost` answers on both loopbacks.
            addresses = dict.fromkeys(
                (family, address)
                for family, _type, _proto, _canonname, address in socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                    flags=socket.AI_PASSIVE,
                )
            )
            for family, address in addresses:
                server = _ProbeServer(family, address)
                servers.callback(server.server_close)
                threading.Thread(target=server.serve_forever, name="pairing_probes", daemon=True).start()
                servers.callback(server.shutdown)
        except OSError as exc:
            msg = (
                f"Cannot listen on the API address {host}:{port} ({exc.strerror or exc}); "
                "change --api-host or --api-port, or pass --no-api."
            )
            raise OSError(msg) from exc
        yield
