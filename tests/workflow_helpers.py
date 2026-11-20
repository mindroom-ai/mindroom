"""Shared helpers for the workflow tests: a fake gateway the SDK talks to over real HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self

import mindroom.workflow as sdk
from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

SCRIPT_RUN_ID = "script-" + "a" * 32
TOKEN = "invented-capability"  # noqa: S105 - an invented placeholder, not a credential.


def completed(result: object, **extra: object) -> dict[str, object]:
    """Return a receipt for a node that completed with *result*."""
    return {"state": "completed", "cached": False, "result": result, "error": None, **extra}


def failed(kind: str, message: str, *, state: str = "failed") -> dict[str, object]:
    """Return a receipt for a node that ended without a result."""
    return {"state": state, "cached": False, "result": None, "error": {"kind": kind, "message": message}}


class FakeWorkflowGateway:
    """A threaded HTTP server that records the SDK's requests and answers nodes from `respond`."""

    def __init__(self, token_path: Path) -> None:
        self.token_path = token_path
        token_path.write_text(TOKEN + "\n", encoding="utf-8")
        self.args: object = {}
        self.respond: Callable[[dict[str, object]], dict[str, object]] = lambda _request: completed(None)
        self.requests: list[tuple[str, str]] = []
        self.nodes: list[dict[str, object]] = []
        self.logs: list[str] = []
        self.finished: dict[str, object] | None = None
        self.results: dict[str, object] = {}
        self.polls_before_settled = 0
        """How many `GET nodes/{id}` answers say `running` before the receipt of `respond` is returned."""
        self.drop_connections = 0
        """How many of the next requests are cut off without an answer, as a restarting gateway does."""
        self.refuse: tuple[int, object] | None = None
        """A status and JSON body answered to every request instead of the normal answer."""
        self._receipts: dict[str, dict[str, object]] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}/api/script-gateway"

    def __enter__(self) -> Self:
        """Start serving."""
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _answer(self, method: str, route: str, body: dict[str, object]) -> object:  # noqa: PLR0911
        if (method, route) == ("GET", "run"):
            return {"run_id": "wf-" + "b" * 32, "attempt": 1, "args": self.args}
        if (method, route) == ("POST", "nodes"):
            self.nodes.append(body)
            node_id = f"n{len(self.nodes) - 1}"
            self._receipts[node_id] = {"id": node_id, **self.respond(body)}
            if self.polls_before_settled:
                return {"id": node_id, "state": "running", "cached": False, "result": None, "error": None}
            return self._receipts[node_id]
        if method == "GET" and route.startswith("nodes/"):
            node_id = route.removeprefix("nodes/").partition("?")[0]
            self.polls_before_settled -= 1
            if self.polls_before_settled > 0:
                return {"id": node_id, "state": "running", "cached": False, "result": None, "error": None}
            return self._receipts[node_id]
        if method == "GET" and route.startswith("results/"):
            return self.results[route.removeprefix("results/")]
        if (method, route) == ("POST", "log"):
            self.logs.append(str(body["text"]))
            return {}
        if (method, route) == ("POST", "finish"):
            self.finished = body
            return {}
        msg = f"Unexpected request {method} {route}"
        raise AssertionError(msg)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        gateway = self
        prefix = f"/api/script-gateway/workflow/{SCRIPT_RUN_ID}/"

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                gateway.requests.append((self.command, self.path))
                if gateway.drop_connections:
                    gateway.drop_connections -= 1
                    self.close_connection = True
                    return
                if self.headers.get("Authorization") != f"Bearer {TOKEN}" or not self.path.startswith(prefix):
                    status, answer = 404, {"detail": "Workflow run not found."}
                elif gateway.refuse is not None:
                    status, answer = gateway.refuse
                else:
                    try:
                        status, answer = 200, gateway._answer(self.command, self.path.removeprefix(prefix), body)
                    except AssertionError as exc:
                        status, answer = 400, {"detail": str(exc)}
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _handle  # noqa: N815 - the names `http.server` dispatches to.

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        return Handler


def sdk_env(monkeypatch: pytest.MonkeyPatch, gateway: FakeWorkflowGateway) -> None:
    """Point the SDK at *gateway* as the launcher would, with no pause between retries."""
    monkeypatch.setenv(GATEWAY_URL_ENV, gateway.url)
    monkeypatch.setenv(RUN_ID_ENV, SCRIPT_RUN_ID)
    monkeypatch.setenv(TOKEN_PATH_ENV, str(gateway.token_path))
    monkeypatch.setattr(sdk, "_RETRY_SECONDS", 0.0)
    sdk._gateway.cache_clear()
