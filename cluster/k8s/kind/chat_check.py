"""Register a Matrix test user, send a prompt, and wait for a MindRoom agent reply."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

NONTERMINAL_STATUSES = frozenset({"pending", "streaming", "approval_pending"})
ERROR_STATUSES = frozenset({"error", "cancelled", "interrupted"})


def extract_latest_reply(
    events: Iterable[dict[str, Any]],
    sender_id: str,
    prompt_event_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Return the latest ``(body, stream_status)`` from *sender_id*, applying ``m.replace`` edits."""
    order: list[str] = []
    replies: dict[str, tuple[str, str | None, bool]] = {}
    for event in events:
        if event.get("type") != "m.room.message" or event.get("sender") != sender_id:
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        rel = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
        if rel.get("rel_type") == "m.replace" and isinstance(content.get("m.new_content"), dict):
            target = rel.get("event_id")
            if isinstance(target, str):
                new = content["m.new_content"]
                status = new.get("io.mindroom.stream_status") or content.get("io.mindroom.stream_status")
                if target not in replies:
                    order.append(target)
                replies[target] = (str(new.get("body", "")), status, replies[target][2] if target in replies else False)
        elif isinstance(event_id := event.get("event_id"), str):
            in_reply = rel.get("m.in_reply_to") if isinstance(rel.get("m.in_reply_to"), dict) else {}
            matched = prompt_event_id is not None and prompt_event_id in (rel.get("event_id"), in_reply.get("event_id"))
            if event_id not in replies:
                order.append(event_id)
            replies[event_id] = (str(content.get("body", "")), content.get("io.mindroom.stream_status"), matched)

    chosen = [eid for eid in order if replies[eid][2]] or order
    if not chosen:
        return None, None
    body, status, _ = replies[chosen[-1]]
    return body.strip() or None, status


def is_terminal_reply(text: str | None, status: str | None) -> bool:
    """Return whether a reply has non-empty text and is no longer streaming."""
    return bool(text) and status not in NONTERMINAL_STATUSES


def _req(
    base: str,
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def _store_auth(token_file: Path, homeserver: str, data: dict[str, Any], password: str) -> tuple[str, str]:
    auth = {
        "homeserver": homeserver,
        "user_id": str(data["user_id"]),
        "access_token": str(data["access_token"]),
        "password": password,
    }
    token_file.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(auth, handle)
    token_file.chmod(0o600)
    return auth["user_id"], auth["access_token"]


def ensure_auth(homeserver: str, username: str, reg_token: str, token_file: Path) -> tuple[str, str]:
    """Return ``(user_id, access_token)``, caching credentials in *token_file* to avoid repeat logins."""
    cached = json.loads(token_file.read_text(encoding="utf-8")) if token_file.exists() else {}
    if cached.get("homeserver") == homeserver and cached.get("access_token"):
        st, who = _req(homeserver, "GET", "/_matrix/client/v3/account/whoami", token=cached["access_token"])
        if st == 200 and str(who.get("user_id", "")).startswith(f"@{username}:"):
            return str(who["user_id"]), str(cached["access_token"])

    password = cached.get("password") or secrets.token_urlsafe(24)
    payload: dict[str, Any] = {"username": username, "password": password, "initial_device_display_name": "chat_check"}
    for _ in range(4):
        st, data = _req(homeserver, "POST", "/_matrix/client/v3/register", body=payload)
        if st == 200 and data.get("access_token"):
            return _store_auth(token_file, homeserver, data, password)
        if st == 401 and data.get("session"):
            if "m.login.registration_token" not in set(data.get("completed", [])):
                if not reg_token:
                    msg = "Registration requires --registration-token or MATRIX_REGISTRATION_TOKEN"
                    raise RuntimeError(msg)
                payload["auth"] = {"type": "m.login.registration_token", "token": reg_token, "session": data["session"]}
            else:
                payload["auth"] = {"type": "m.login.dummy", "session": data["session"]}
            continue
        if st == 400 and data.get("errcode") == "M_USER_IN_USE" and cached.get("password"):
            login = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            }
            lst, ldata = _req(homeserver, "POST", "/_matrix/client/v3/login", body=login)
            if lst == 200 and ldata.get("access_token"):
                return _store_auth(token_file, homeserver, ldata, password)
            msg = f"Login failed ({lst}): {ldata}"
            raise RuntimeError(msg)
        msg = f"Registration failed ({st}): {data}"
        raise RuntimeError(msg)
    msg = "Registration did not finish within 4 UIAA rounds"
    raise RuntimeError(msg)


def join_and_chat(
    homeserver: str,
    token: str,
    server_name: str,
    room_key: str,
    agent: str,
    prompt: str,
    timeout: float,
) -> str:
    """Join ``#<room_key>:<server_name>``, send *prompt*, and wait for ``@mindroom_<agent>`` to reply."""
    alias = f"#{room_key}:{server_name}"
    st, data = _req(
        homeserver,
        "POST",
        f"/_matrix/client/v3/join/{urllib.parse.quote(alias, safe='')}",
        token=token,
        body={},
    )
    if st != 200 or not data.get("room_id"):
        msg = f"Failed to join {alias} ({st}): {data}"
        raise RuntimeError(msg)
    room_id = str(data["room_id"])
    st, sync = _req(homeserver, "GET", "/_matrix/client/v3/sync?timeout=0", token=token)
    if st != 200 or not sync.get("next_batch"):
        msg = f"Initial sync failed ({st}): {sync}"
        raise RuntimeError(msg)
    since = str(sync["next_batch"])
    path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/send/m.room.message/{secrets.token_hex(8)}"
    st, sent = _req(homeserver, "PUT", path, token=token, body={"msgtype": "m.text", "body": prompt})
    if st != 200 or not sent.get("event_id"):
        msg = f"Failed to send message ({st}): {sent}"
        raise RuntimeError(msg)

    prompt_id, sender, events, deadline, last = (
        str(sent["event_id"]),
        f"@mindroom_{agent}:{server_name}",
        [],
        time.monotonic() + timeout,
        (None, None),
    )
    while time.monotonic() < deadline:
        qs = urllib.parse.urlencode(
            {"since": since, "timeout": max(500, min(5000, int((deadline - time.monotonic()) * 1000)))},
        )
        st, sync = _req(homeserver, "GET", f"/_matrix/client/v3/sync?{qs}", token=token)
        if st != 200:
            msg = f"Sync failed ({st}): {sync}"
            raise RuntimeError(msg)
        since = str(sync.get("next_batch", since))
        events.extend(sync.get("rooms", {}).get("join", {}).get(room_id, {}).get("timeline", {}).get("events", []))
        last = extract_latest_reply(events, sender, prompt_id)
        if is_terminal_reply(*last):
            if last[1] in ERROR_STATUSES:
                msg = f"Agent {sender} returned {last[1]}: {last[0]}"
                raise RuntimeError(msg)
            return str(last[0])
    msg = f"Timed out after {timeout:.0f}s waiting for {sender} (last seen: {last!r})"
    raise RuntimeError(msg)


def main(argv: list[str] | None = None) -> int:
    """Run one Matrix prompt/reply check and print the agent reply."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--homeserver", default="http://127.0.0.1:8008")
    p.add_argument("--username", default="tester")
    p.add_argument("--room", default="lobby")
    p.add_argument("--agent", default="helper")
    p.add_argument("--prompt", default="Reply with the single word: pong")
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--token-file", type=Path, default=Path.home() / ".mindroom-kind-auth.json")
    p.add_argument("--registration-token", default=os.environ.get("MATRIX_REGISTRATION_TOKEN", ""))
    args = p.parse_args(argv)
    try:
        user_id, token = ensure_auth(args.homeserver, args.username, args.registration_token, args.token_file)
        reply = join_and_chat(
            args.homeserver,
            token,
            user_id.split(":", 1)[1],
            args.room,
            args.agent,
            args.prompt,
            args.timeout,
        )
    except (RuntimeError, OSError) as exc:
        sys.stderr.write(f"chat_check failed: {exc}\n")
        return 1
    sys.stdout.write(f"Reply from @mindroom_{args.agent}: {reply}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
