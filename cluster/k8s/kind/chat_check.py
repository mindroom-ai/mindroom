"""Port-forward Tuwunel, send a Matrix prompt, and wait for a MindRoom agent reply."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import select
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from collections.abc import Iterator

NAMESPACE, SERVICE, ROOM, AGENT = "mindroom", "svc/matrix-mindroom-tuwunel", "lobby", "helper"
PROMPT, TIMEOUT = "Reply with the single word: pong", 180.0
ERROR_STATUSES = frozenset({"error", "cancelled", "interrupted"})
REG_TOKEN_TEMPLATE = "go-template={{.data.MATRIX_REGISTRATION_TOKEN | base64decode}}"  # noqa: S105
OPENER = build_opener(ProxyHandler({}))


def extract_completed_reply(events: list[dict[str, Any]], sender: str, prompt_id: str) -> str | None:
    """Return the completed reply body for *prompt_id* from *sender*, or ``None`` while still in flight."""
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
    if not replies:
        return None
    body, status = list(replies.values())[-1]
    if status in ERROR_STATUSES:
        msg = f"Agent {sender} returned status {status}"
        raise RuntimeError(msg)
    return text if (text := body.strip()) and status not in {"pending", "streaming", "approval_pending"} else None


def _req(method: str, url: str, token: str = "", body: dict[str, Any] | None = None, ok: int = 200) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = OPENER.open(Request(url, data=data, headers=headers, method=method), timeout=20.0)  # noqa: S310
    except HTTPError as exc:
        resp = exc
    with resp:
        raw, status = resp.read().decode("utf-8", errors="replace"), resp.status
    if status != ok:
        errcode = ""
        with contextlib.suppress(ValueError, AttributeError):
            errcode = str(json.loads(raw).get("errcode") or "")
        msg = f"{method} {urlparse(url).path} returned HTTP {status}" + (f" ({errcode})" if errcode else "")
        raise RuntimeError(msg)
    return json.loads(raw or "{}")


@contextlib.contextmanager
def port_forward() -> Iterator[str]:
    """Port-forward Tuwunel on an ephemeral loopback port and yield its base URL."""
    cmd = ["kubectl", "-n", NAMESPACE, "port-forward", "--address", "127.0.0.1", SERVICE, ":8008"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    stdout = proc.stdout
    try:
        line = stdout.readline() if stdout and select.select([stdout], [], [], 30.0)[0] else ""
        match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
        if not match:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=0.5)
            detail = f"exit {proc.returncode}" if proc.returncode is not None else "timed out"
            msg = f"kubectl port-forward failed ({detail})"
            raise RuntimeError(msg)
        yield f"http://127.0.0.1:{match.group(1)}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        if stdout is not None:
            stdout.close()


def register_user(base: str) -> tuple[str, str]:
    """Register a throwaway Matrix user using the ``matrix-registration`` Secret."""
    cmd = ["kubectl", "-n", NAMESPACE, "get", "secret", "matrix-registration", "-o", REG_TOKEN_TEMPLATE]
    res = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30.0)
    if res.returncode != 0 or not (reg_token := res.stdout.strip()):
        msg = f"Failed to read secret {NAMESPACE}/matrix-registration"
        raise RuntimeError(msg)
    payload: dict[str, Any] = {"username": f"tester-{secrets.token_hex(4)}", "password": secrets.token_urlsafe(24)}
    session = _req("POST", f"{base}/_matrix/client/v3/register", body=payload, ok=401)["session"]
    payload["auth"] = {"type": "m.login.registration_token", "token": reg_token, "session": session}
    data = _req("POST", f"{base}/_matrix/client/v3/register", body=payload, ok=200)
    return str(data["user_id"]), str(data["access_token"])


def join_and_chat(base: str, token: str, server_name: str) -> str:
    """Join ``#<ROOM>:<server_name>``, wait for ``@mindroom_<AGENT>``, send *PROMPT*, and return the reply."""
    deadline, alias = time.monotonic() + TIMEOUT, quote(f"#{ROOM}:{server_name}", safe="")
    while True:
        try:
            room_id = str(_req("POST", f"{base}/_matrix/client/v3/join/{alias}", token, body={})["room_id"])
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2.0)
    sender, qroom = f"@mindroom_{AGENT}:{server_name}", quote(room_id, safe="")
    members_url = f"{base}/_matrix/client/v3/rooms/{qroom}/joined_members"
    while sender not in _req("GET", members_url, token).get("joined", {}):
        if time.monotonic() >= deadline:
            msg = f"Timed out waiting for {sender} to join #{ROOM}:{server_name}"
            raise RuntimeError(msg)
        time.sleep(2.0)
    since = str(_req("GET", f"{base}/_matrix/client/v3/sync?timeout=0", token)["next_batch"])
    send_url = f"{base}/_matrix/client/v3/rooms/{qroom}/send/m.room.message/{secrets.token_hex(8)}"
    prompt_id = str(_req("PUT", send_url, token, body={"msgtype": "m.text", "body": PROMPT})["event_id"])
    events: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        sync = _req("GET", f"{base}/_matrix/client/v3/sync?since={quote(since, safe='')}&timeout=5000", token)
        since = str(sync.get("next_batch", since))
        events.extend(sync.get("rooms", {}).get("join", {}).get(room_id, {}).get("timeline", {}).get("events", []))
        if (reply := extract_completed_reply(events, sender, prompt_id)) is not None:
            return reply
    msg = f"Timed out after {TIMEOUT:.0f}s waiting for {sender}"
    raise RuntimeError(msg)


def main() -> int:
    """Run one Matrix prompt/reply check and print the agent reply."""
    try:
        with port_forward() as base:
            user_id, token = register_user(base)
            reply = join_and_chat(base, token, user_id.split(":", 1)[1])
    except (RuntimeError, KeyError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"chat_check failed: {exc}\n")
        return 1
    sys.stdout.write(f"Reply from @mindroom_{AGENT}: {reply}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
