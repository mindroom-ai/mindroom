"""Print the last N agent events in a room, decrypted with the user's keys. Usage: STATE ROOM_ID AGENT [N]"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from e2ee_user import client_for, sync_once

from nio import MessageDirection


async def main(state, room_id, agent, n):
    client = client_for(pathlib.Path(state))
    await sync_once(client)
    resp = await client.room_messages(
        room_id, start=client.next_batch, direction=MessageDirection.back, limit=n
    )
    for e in reversed(resp.chunk):
        if e.sender != agent:
            print(
                json.dumps({
                    "sender": e.sender,
                    "type": type(e).__name__,
                    "body": getattr(e, "body", "")[:60],
                })
            )
            continue
        content = e.source.get("content", {})
        print(
            json.dumps({
                "type": type(e).__name__,
                "decrypted": getattr(e, "decrypted", None),
                "rel": content.get("m.relates_to", {}).get("rel_type"),
                "body": (
                    content.get("m.new_content", {}).get("body")
                    or getattr(e, "body", "")
                )[:80],
            })
        )
    await client.close()


asyncio.run(
    main(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        int(sys.argv[4]) if len(sys.argv) > 4 else 20,
    )
)
