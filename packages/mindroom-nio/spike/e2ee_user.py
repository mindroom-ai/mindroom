"""Independent E2EE Matrix user for the live gate, built on upstream matrix-nio.

Usage:
  e2ee_user.py STATE_DIR register USER PASSWORD
  e2ee_user.py STATE_DIR chat ROOM_NAME_SUBSTRING AGENT_USER_ID TOKEN [--timeout S] [--no-wait]
  e2ee_user.py STATE_DIR await ROOM_NAME_SUBSTRING AGENT_USER_ID TOKEN EVENT_ID [--timeout S]

`chat` joins invited rooms, sends an encrypted message asking the agent to echo TOKEN,
and waits for a decrypted agent reply in that message's thread that contains TOKEN.
Prints one JSON object with the outcome.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    MegolmEvent,
    RegisterResponse,
    RoomMessageText,
    RoomSendResponse,
)

HS = "http://localhost:8008"


def client_for(state: pathlib.Path) -> AsyncClient:
    creds = json.loads((state / "creds.json").read_text())
    client = AsyncClient(
        HS,
        creds["user_id"],
        device_id=creds["device_id"],
        store_path=str(state),
        config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
    )
    client.restore_login(creds["user_id"], creds["device_id"], creds["access_token"])
    return client


async def crypto_upkeep(client: AsyncClient) -> None:
    if client.should_upload_keys:
        await client.keys_upload()
    if client.should_query_keys:
        await client.keys_query()
    if client.should_claim_keys:
        await client.keys_claim(client.get_users_for_key_claiming())


async def sync_once(client: AsyncClient) -> object:
    # A restored client keeps its sync token but not room state, so the first sync asks for full state.
    resp = await client.sync(timeout=2000, full_state=not client.rooms)
    await crypto_upkeep(client)
    for room_id in list(client.invited_rooms):
        await client.join(room_id)
    return resp


async def register(state: pathlib.Path, user: str, password: str) -> dict:
    client = AsyncClient(HS, f"@{user}:localhost", store_path=str(state))
    resp = await client.register(user, password)
    if not isinstance(resp, RegisterResponse):
        resp = await client.login(password)
        assert isinstance(resp, LoginResponse), resp
    await client.close()
    return {
        "user_id": resp.user_id,
        "device_id": resp.device_id,
        "access_token": resp.access_token,
    }


def find_room(client: AsyncClient, needle: str) -> str | None:
    if needle.startswith("!"):
        return needle if needle in client.rooms else None
    for room_id, room in client.rooms.items():
        if needle.lower() in (room.display_name or "").lower() or needle.lower() in (
            room.canonical_alias or ""
        ):
            return room_id
    return None


async def wait_reply(
    client: AsyncClient,
    room_id: str,
    agent: str,
    token: str,
    root: str,
    wait_seconds: float,
) -> dict:
    deadline = time.monotonic() + wait_seconds
    undecryptable = 0
    replies: list[dict] = []
    while time.monotonic() < deadline:
        resp = await sync_once(client)
        rooms = getattr(resp, "rooms", None)
        if rooms is None or room_id not in rooms.join:
            continue
        for event in rooms.join[room_id].timeline.events:
            if event.sender != agent:
                continue
            if isinstance(event, MegolmEvent):
                undecryptable += 1
                continue
            if not isinstance(event, RoomMessageText):
                continue
            content = event.source.get("content", {})
            relates = content.get("m.relates_to", {})
            new = content.get("m.new_content", {})
            body = new.get("body") or event.body
            replies.append({
                "event_id": event.event_id,
                "decrypted": event.decrypted,
                "body": body[:200],
            })
            in_thread = (
                relates.get("event_id") == root
                or relates.get("rel_type") == "m.replace"
            )
            if event.decrypted and in_thread and token in body:
                return {
                    "ok": True,
                    "undecryptable": undecryptable,
                    "replies": replies[-5:],
                }
    return {"ok": False, "undecryptable": undecryptable, "replies": replies[-5:]}


async def chat(
    state: pathlib.Path,
    needle: str,
    agent: str,
    token: str,
    wait_seconds: float,
    wait: bool,
) -> None:
    client = client_for(state)
    room_id = None
    deadline = time.monotonic() + wait_seconds
    while room_id is None and time.monotonic() < deadline:
        await sync_once(client)
        room_id = find_room(client, needle)
    if room_id is None:
        print(
            json.dumps({
                "ok": False,
                "error": f"no room matching {needle!r}",
                "rooms": {r: x.display_name for r, x in client.rooms.items()},
            })
        )
        await client.close()
        return
    room = client.rooms[room_id]
    await client.joined_members(room_id)
    await client.keys_query() if client.should_query_keys else None
    send = await client.room_send(
        room_id,
        "m.room.message",
        {
            "msgtype": "m.text",
            "body": f"{agent.split(':')[0][1:]}: Reply with only the word {token} and nothing else.",
        },
        ignore_unverified_devices=True,
    )
    if not isinstance(send, RoomSendResponse):
        print(json.dumps({"ok": False, "error": str(send)}))
        await client.close()
        return
    out = {
        "room_id": room_id,
        "room_encrypted": room.encrypted,
        "sent_event_id": send.event_id,
    }
    if wait:
        out |= await wait_reply(
            client, room_id, agent, token, send.event_id, wait_seconds
        )
    print(json.dumps(out))
    await client.close()


async def await_only(
    state: pathlib.Path,
    needle: str,
    agent: str,
    token: str,
    root: str,
    wait_seconds: float,
) -> None:
    client = client_for(state)
    await sync_once(client)
    room_id = find_room(client, needle)
    out = {"room_id": room_id} | await wait_reply(
        client, room_id, agent, token, root, wait_seconds
    )
    print(json.dumps(out))
    await client.close()


def main() -> None:
    state, cmd, *args = sys.argv[1:]
    timeout = float(args[args.index("--timeout") + 1]) if "--timeout" in args else 240.0
    state_dir = pathlib.Path(state)
    if cmd == "register":
        state_dir.mkdir(parents=True, exist_ok=True)
        creds = asyncio.run(register(state_dir, args[0], args[1]))
        (state_dir / "creds.json").write_text(json.dumps(creds))
        print(
            json.dumps({"user_id": creds["user_id"], "device_id": creds["device_id"]})
        )
    elif cmd == "chat":
        asyncio.run(
            chat(state_dir, args[0], args[1], args[2], timeout, "--no-wait" not in args)
        )
    elif cmd == "await":
        asyncio.run(await_only(state_dir, args[0], args[1], args[2], args[3], timeout))


if __name__ == "__main__":
    main()
