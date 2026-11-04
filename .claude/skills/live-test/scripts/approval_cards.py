"""List, wait for, and approve the tool-approval cards in a Matrix room through the raw client API.

Reads and reacts as the account whose access token is in ``LIVE_TEST_ACCESS_TOKEN``, which must be the card's
approver to approve it, on ``MATRIX_HOMESERVER`` (default ``http://localhost:8008``):

    approval_cards.py list '!room:localhost'
    approval_cards.py wait '!room:localhost' --tool contacts --timeout 300
    approval_cards.py approve '!room:localhost' [card_event_id]

``list`` prints one JSON line per card with the status its latest edit gives it.
``wait`` prints the newest pending card, optionally for one tool, once one exists, and exits 1 on timeout.
``approve`` reacts with ✅ to the given card, or to the newest pending card.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

CARD_TYPE = "io.mindroom.tool_approval"


def _matrix(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one client API request and return its JSON response."""
    homeserver = os.environ.get("MATRIX_HOMESERVER", "http://localhost:8008").rstrip("/")
    request = urllib.request.Request(  # noqa: S310 - the operator chooses the homeserver URL
        f"{homeserver}/_matrix/client/v3{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['LIVE_TEST_ACCESS_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - see above
        return json.load(response)


def _room_events(room_id: str, limit: int) -> list[dict[str, Any]]:
    """Return the room's newest ``limit`` events, oldest first."""
    events: list[dict[str, Any]] = []
    token = None
    while len(events) < limit:
        query = {"dir": "b", "limit": min(100, limit - len(events))} | ({"from": token} if token else {})
        page = _matrix("GET", f"/rooms/{urllib.parse.quote(room_id, safe='')}/messages?{urllib.parse.urlencode(query)}")
        events += page.get("chunk", [])
        token = page.get("end")
        if not page.get("chunk") or not token:
            break
    return events[::-1]


def cards(room_id: str, limit: int) -> list[dict[str, Any]]:
    """Return every card among the room's newest ``limit`` events, oldest first, with its latest status."""
    originals: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    for event in _room_events(room_id, limit):
        if event["type"] != CARD_TYPE:
            continue
        content = event.get("content", {})
        relation = content.get("m.relates_to") or {}
        if relation.get("rel_type") == "m.replace":
            statuses[relation["event_id"]] = content.get("m.new_content", content).get("status")
        else:
            originals.append(event)
    return [
        {
            "event_id": event["event_id"],
            "sent": datetime.fromtimestamp(event["origin_server_ts"] / 1000, UTC).isoformat(timespec="seconds"),
            "status": statuses.get(event["event_id"], event["content"].get("status")),
            "tool": event["content"].get("tool_name"),
            "agent": event["content"].get("agent_name"),
            "arguments": event["content"].get("arguments"),
            "approver": event["content"].get("approver_user_id"),
            "thread": event["content"].get("thread_id"),
        }
        for event in originals
    ]


def _pending(room_id: str, limit: int, tool: str | None) -> list[dict[str, Any]]:
    return [card for card in cards(room_id, limit) if card["status"] == "pending" and tool in {None, card["tool"]}]


def main() -> None:
    """Run one command."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "wait", "approve"])
    parser.add_argument("room_id")
    parser.add_argument("card_event_id", nargs="?", help="approve: the card to approve instead of the newest pending")
    parser.add_argument("--tool", help="wait: only a card for this tool")
    parser.add_argument("--timeout", type=float, default=300, help="wait: seconds before giving up")
    parser.add_argument("--limit", type=int, default=300, help="how many recent room events to scan")
    args = parser.parse_args()
    if args.command == "list":
        for card in cards(args.room_id, args.limit):
            print(json.dumps(card, ensure_ascii=False))
    elif args.command == "wait":
        deadline = time.monotonic() + args.timeout
        while not (pending := _pending(args.room_id, args.limit, args.tool)):
            if time.monotonic() > deadline:
                sys.exit("TIMEOUT")
            time.sleep(1.5)
        print(json.dumps(pending[-1], ensure_ascii=False))
    else:
        target = args.card_event_id
        if target is None:
            if not (pending := _pending(args.room_id, args.limit, None)):
                sys.exit("No pending card")
            target = pending[-1]["event_id"]
        reaction = {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": "✅"}}
        room = urllib.parse.quote(args.room_id, safe="")
        _matrix("PUT", f"/rooms/{room}/send/m.reaction/{uuid.uuid4().hex}", reaction)
        print(f"approved {target}")


if __name__ == "__main__":
    main()
