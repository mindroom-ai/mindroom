"""Port-forward Tuwunel, send a Matrix prompt, and wait for a MindRoom agent reply."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Iterator

NAMESPACE, SERVICE = "mindroom", "svc/matrix-mindroom-tuwunel"
USERNAME, ROOM, AGENT, TIMEOUT = "tester", "lobby", "helper", 180.0
TOKEN_FILE = Path.home() / ".mindroom-kind-auth.json"
ERROR_STATUSES = frozenset({"error", "cancelled", "interrupted"})
REG_TOKEN_TEMPLATE = "go-template={{.data.MATRIX_REGISTRATION_TOKEN | base64decode}}"  # noqa: S105


def extract_latest_reply(events: list[dict[str, Any]], sender: str, prompt_id: str) -> tuple[str | None, str | None]:
    """Return the latest ``(body, stream_status)`` for *prompt_id* from *sender*."""
    replies: dict[str, tuple[str, str | None]] = {}
    for ev in events:
        content = ev.get("content") or {}
        if ev.get("type") != "m.room.message" or ev.get("sender") != sender or content.get("msgtype") == "m.notice":
            continue
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace" and "m.new_content" in content:
            if (target := rel.get("event_id")) in replies:
                new = content["m.new_content"]
                status = new.get("io.mindroom.stream_status") or content.get("io.mindroom.stream_status")
                replies[target] = (str(new.get("body", "")), status)
        elif rel.get("event_id") == prompt_id and (event_id := ev.get("event_id")):
            replies[event_id] = (str(content.get("body", "")), content.get("io.mindroom.stream_status"))
    body, status = list(replies.values())[-1] if replies else ("", None)
    return body.strip() or None, status


def is_terminal_reply(text: str | None, status: str | None) -> bool:
    """Return whether a reply has non-empty text and is no longer streaming."""
    return bool(text) and status not in {"pending", "streaming", "approval_pending"}


def _req(method: str, url: str, token: str = "", body: dict[str, Any] | None = None, ok: int = 200) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = urlopen(Request(url, data=data, headers=headers, method=method), timeout=20.0)  # noqa: S310
    except HTTPError as exc:
        resp = exc
    with resp:
        raw, status = resp.read().decode("utf-8", errors="replace"), resp.status
    if status != ok:
        msg = f"{method} {url} returned {status} (expected {ok}): {raw[:200]}"
        raise RuntimeError(msg)
    return json.loads(raw or "{}")


@contextlib.contextmanager
def port_forward() -> Iterator[str]:
    """Port-forward Tuwunel on a free loopback port and yield its base URL."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cmd = ["kubectl", "-n", NAMESPACE, "port-forward", "--address", "127.0.0.1", SERVICE, f"{port}:8008"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base, ready = f"http://127.0.0.1:{port}", False
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and proc.poll() is None:
            with contextlib.suppress(OSError, RuntimeError):
                _req("GET", f"{base}/_matrix/client/versions")
                ready = True
                break
            time.sleep(0.5)
        if ready:
            yield base
    finally:
        proc.terminate()
        try:
            err = proc.communicate(timeout=5.0)[1].strip()
        except subprocess.TimeoutExpired:
            proc.kill()
            err = proc.communicate()[1].strip()
    if not ready:
        msg = f"kubectl port-forward failed: {err or 'timed out'}"
        raise RuntimeError(msg)


def ensure_auth(base: str, token_file: Path = TOKEN_FILE) -> tuple[str, str]:
    """Return ``(user_id, access_token)``, reusing the cached account or registering once."""
    with contextlib.suppress(OSError, ValueError, AttributeError, RuntimeError):
        if cached := str(json.loads(token_file.read_text(encoding="utf-8")).get("access_token") or ""):
            return str(_req("GET", f"{base}/_matrix/client/v3/account/whoami", cached)["user_id"]), cached
    cmd = ["kubectl", "-n", NAMESPACE, "get", "secret", "matrix-registration", "-o", REG_TOKEN_TEMPLATE]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30.0)
    if res.returncode != 0 or not res.stdout.strip():
        msg = f"Failed to read matrix-registration secret: {res.stderr.strip() or 'empty token'}"
        raise RuntimeError(msg)
    payload: dict[str, Any] = {"username": USERNAME, "password": secrets.token_urlsafe(24)}
    session = _req("POST", f"{base}/_matrix/client/v3/register", body=payload, ok=401)["session"]
    payload["auth"] = {"type": "m.login.registration_token", "token": res.stdout.strip(), "session": session}
    data = _req("POST", f"{base}/_matrix/client/v3/register", body=payload, ok=200)
    auth = {"user_id": str(data["user_id"]), "access_token": str(data["access_token"])}
    token_file.unlink(missing_ok=True)
    with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        json.dump(auth, handle)
    return auth["user_id"], auth["access_token"]


def join_and_chat(base: str, token: str, server_name: str, prompt: str) -> str:
    """Join ``#<ROOM>:<server_name>``, send *prompt*, and wait for ``@mindroom_<AGENT>`` to reply."""
    deadline, alias = time.monotonic() + TIMEOUT, quote(f"#{ROOM}:{server_name}", safe="")
    while True:
        try:
            room_id = str(_req("POST", f"{base}/_matrix/client/v3/join/{alias}", token, body={})["room_id"])
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2.0)
    since = str(_req("GET", f"{base}/_matrix/client/v3/sync?timeout=0", token)["next_batch"])
    send_url = f"{base}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{secrets.token_hex(8)}"
    prompt_id = str(_req("PUT", send_url, token, body={"msgtype": "m.text", "body": prompt})["event_id"])
    sender, events, last = f"@mindroom_{AGENT}:{server_name}", [], (None, None)
    while time.monotonic() < deadline:
        sync = _req("GET", f"{base}/_matrix/client/v3/sync?since={quote(since, safe='')}&timeout=5000", token)
        since = str(sync.get("next_batch", since))
        events.extend(sync.get("rooms", {}).get("join", {}).get(room_id, {}).get("timeline", {}).get("events", []))
        last = extract_latest_reply(events, sender, prompt_id)
        if is_terminal_reply(*last):
            if last[1] in ERROR_STATUSES:
                msg = f"Agent {sender} returned {last[1]}: {last[0]}"
                raise RuntimeError(msg)
            return str(last[0])
    msg = f"Timed out after {TIMEOUT:.0f}s waiting for {sender} (last seen: {last!r})"
    raise RuntimeError(msg)


def main(argv: list[str] | None = None) -> int:
    """Run one Matrix prompt/reply check and print the agent reply."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="Reply with the single word: pong")
    args = parser.parse_args(argv)
    try:
        with port_forward() as base:
            user_id, token = ensure_auth(base)
            reply = join_and_chat(base, token, user_id.split(":", 1)[1], args.prompt)
    except (RuntimeError, KeyError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"chat_check failed: {exc}\n")
        return 1
    sys.stdout.write(f"Reply from @mindroom_{AGENT}: {reply}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
