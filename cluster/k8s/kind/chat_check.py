"""Register a Matrix test user, send a prompt, and wait for a MindRoom agent reply."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

HOMESERVER = "http://127.0.0.1:8008"
USERNAME = "tester"
ROOM = "lobby"
AGENT = "helper"
TIMEOUT = 90.0
TOKEN_FILE = Path.home() / ".mindroom-kind-auth.json"
NONTERMINAL_STATUSES = frozenset({"pending", "streaming", "approval_pending"})
ERROR_STATUSES = frozenset({"error", "cancelled", "interrupted"})


def extract_latest_reply(events: list[dict[str, Any]], sender: str, prompt_id: str) -> tuple[str | None, str | None]:
    """Return the latest ``(body, stream_status)`` for *prompt_id* from *sender*."""
    replies: dict[str, tuple[str, str | None]] = {}
    for event in events:
        if event.get("type") != "m.room.message" or event.get("sender") != sender:
            continue
        content = event.get("content")
        if not isinstance(content, dict) or content.get("msgtype") == "m.notice":
            continue
        rel = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
        if rel.get("rel_type") == "m.replace" and isinstance(content.get("m.new_content"), dict):
            target, new = rel.get("event_id"), content["m.new_content"]
            if isinstance(target, str) and target in replies and new.get("msgtype") != "m.notice":
                status = new.get("io.mindroom.stream_status") or content.get("io.mindroom.stream_status")
                replies[target] = (str(new.get("body", "")), status)
        elif isinstance(event_id := event.get("event_id"), str) and rel.get("event_id") == prompt_id:
            replies[event_id] = (str(content.get("body", "")), content.get("io.mindroom.stream_status"))
    if not replies:
        return None, None
    body, status = list(replies.values())[-1]
    return body.strip() or None, status


def is_terminal_reply(text: str | None, status: str | None) -> bool:
    """Return whether a reply has non-empty text and is no longer streaming."""
    return bool(text) and status not in NONTERMINAL_STATUSES


def _req(method: str, path: str, token: str = "", body: dict[str, Any] | None = None, ok: int = 200) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(f"{HOMESERVER}{path}", data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urlopen(req, timeout=20.0) as resp:  # noqa: S310
            st, parsed = resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            st, parsed = exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            st, parsed = exc.code, {"error": raw}
    if st != ok:
        msg = f"{method} {path} returned {st} (expected {ok}): {parsed}"
        raise RuntimeError(msg)
    return parsed


def ensure_auth(reg_token: str, token_file: Path = TOKEN_FILE) -> tuple[str, str]:
    """Return ``(user_id, access_token)``, caching credentials in *token_file* to avoid repeat logins."""
    cached: object = None
    with contextlib.suppress(OSError, ValueError):
        cached = json.loads(token_file.read_text(encoding="utf-8"))
    if isinstance(cached, dict) and (cached_token := str(cached.get("access_token") or "")):
        with contextlib.suppress(RuntimeError):
            who = _req("GET", "/_matrix/client/v3/account/whoami", cached_token)
            if str(who.get("user_id", "")).startswith(f"@{USERNAME}:"):
                return str(who["user_id"]), cached_token
    if not reg_token:
        msg = "Registration requires MATRIX_REGISTRATION_TOKEN"
        raise RuntimeError(msg)
    payload: dict[str, Any] = {"username": USERNAME, "password": secrets.token_urlsafe(24)}
    init = _req("POST", "/_matrix/client/v3/register", body=payload, ok=401)
    payload["auth"] = {"type": "m.login.registration_token", "token": reg_token, "session": init["session"]}
    data = _req("POST", "/_matrix/client/v3/register", body=payload, ok=200)
    user_id, access_token = str(data["user_id"]), str(data["access_token"])
    token_file.unlink(missing_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"user_id": user_id, "access_token": access_token}, handle)
    return user_id, access_token


def join_and_chat(token: str, server_name: str, prompt: str) -> str:
    """Join ``#<ROOM>:<server_name>``, send *prompt*, and wait for ``@mindroom_<AGENT>`` to reply."""
    alias = quote(f"#{ROOM}:{server_name}", safe="")
    room_id = str(_req("POST", f"/_matrix/client/v3/join/{alias}", token, body={})["room_id"])
    since = str(_req("GET", "/_matrix/client/v3/sync?timeout=0", token)["next_batch"])
    send_path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{secrets.token_hex(8)}"
    prompt_id = str(_req("PUT", send_path, token, body={"msgtype": "m.text", "body": prompt})["event_id"])
    sender = f"@mindroom_{AGENT}:{server_name}"
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + TIMEOUT
    last: tuple[str | None, str | None] = (None, None)
    while time.monotonic() < deadline:
        wait_ms = max(500, min(5000, int((deadline - time.monotonic()) * 1000)))
        sync = _req("GET", f"/_matrix/client/v3/sync?{urlencode({'since': since, 'timeout': wait_ms})}", token)
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
        user_id, token = ensure_auth(os.environ.get("MATRIX_REGISTRATION_TOKEN", ""))
        reply = join_and_chat(token, user_id.split(":", 1)[1], args.prompt)
    except (RuntimeError, KeyError, OSError) as exc:
        sys.stderr.write(f"chat_check failed: {exc}\n")
        return 1
    sys.stdout.write(f"Reply from @mindroom_{AGENT}: {reply}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
