"""Create the custom emoji room that a homeserver advertises to MindRoom Chat.

Run from the repository with ``uv run python -m scripts.utilities.create_server_emoji_room``.
Supply an operator's access token in ``MATRIX_ACCESS_TOKEN``.
The room is hidden from room lists and local to the homeserver, and it holds one empty emoji pack.
Everyone may add emoji by default; ``--moderators-only`` limits adding, editing, and removing them to moderators.
Members cannot post messages in the room, because every message would notify the whole server.
The script prints the room ID and the ``io.mindroom.emoji`` key that the homeserver's
``/.well-known/matrix/client`` must serve for MindRoom Chat to find the room.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

EMOJI_PACK_EVENT = "im.ponies.room_emotes"


def _json(response: httpx.Response) -> dict[str, Any]:
    """Return the JSON object Matrix answered with, or fail with the error it gave."""
    if not response.is_success:
        msg = f"{response.request.method} {response.url.path} answered {response.status_code}: {response.text}"
        raise httpx.HTTPStatusError(msg, request=response.request, response=response)
    value = response.json()
    if not isinstance(value, dict):
        msg = "Expected a JSON object from Matrix"
        raise TypeError(msg)
    return value


def create_server_emoji_room(client: httpx.Client, *, name: str, alias: str, moderators_only: bool) -> tuple[str, str]:
    """Create the room and return its ID and its full alias, which lives on the operator's server."""
    # Ask first, so that a bad token fails before a room exists.
    user_id = _json(client.get("/_matrix/client/v3/account/whoami"))["user_id"]
    room_id = _json(
        client.post(
            "/_matrix/client/v3/createRoom",
            json={
                "name": name,
                "room_alias_name": alias,
                "preset": "public_chat",
                "visibility": "private",
                "creation_content": {"type": "io.mindroom.emoji", "m.federate": False},
                "initial_state": [
                    {
                        "type": EMOJI_PACK_EVENT,
                        "state_key": "",
                        "content": {"pack": {"display_name": name, "usage": ["emoticon"]}, "images": {}},
                    },
                ],
            },
        ),
    )["room_id"]
    # A create-time override would replace the whole events map and drop the levels the server chose.
    levels_path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.power_levels"
    levels = _json(client.get(levels_path))
    events = levels.setdefault("events", {})
    events[EMOJI_PACK_EVENT] = 50 if moderators_only else 0
    # Members join this hidden room by themselves and cannot mute it, so a message there would notify the whole server.
    # Explicit levels for message types would undercut events_default, so raise those too.
    levels["events_default"] = max(levels.get("events_default", 0), 50)
    for message_type in ("m.room.message", "m.room.encrypted"):
        if message_type in events:
            events[message_type] = max(events[message_type], 50)
    _json(client.put(levels_path, json=levels))
    server_name = user_id.partition(":")[2]
    return room_id, f"#{alias}:{server_name}"


def main(argv: list[str] | None = None) -> int:
    """Create the room, then print its ID and the well-known key that advertises it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", required=True, help="Base URL of the homeserver")
    parser.add_argument("--name", required=True, help="Name of the room and of its emoji pack")
    parser.add_argument("--alias", default="emoji", help="Local part of the room alias (default: %(default)s)")
    parser.add_argument("--moderators-only", action="store_true", help="Let only moderators add or change emoji")
    args = parser.parse_args(argv)
    token = os.environ.get("MATRIX_ACCESS_TOKEN")
    if not token:
        parser.error("Set MATRIX_ACCESS_TOKEN to an operator's access token")
    try:
        with httpx.Client(
            base_url=args.homeserver.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
        ) as client:
            room_id, full_alias = create_server_emoji_room(
                client,
                name=args.name,
                alias=args.alias,
                moderators_only=args.moderators_only,
            )
    except (TypeError, ValueError, httpx.HTTPError) as error:
        print(f"Room creation stopped: {error}", file=sys.stderr)
        return 1
    key = json.dumps({"io.mindroom.emoji": {"room": full_alias}})
    print(f"Room ID: {room_id}")
    print(f"Add to /.well-known/matrix/client: {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
