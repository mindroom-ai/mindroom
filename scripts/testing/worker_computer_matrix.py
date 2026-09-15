"""Provision disposable local Matrix identities for the worker-computer Chat probe."""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path


def create_matrix_fixture(output: Path, image: str) -> dict[str, Any]:
    """Start one loopback-only container; return its exact ID and test credentials."""
    # Resolve locally first: never silently pull or mutate a remote deployment.
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    container_id = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--publish",
            "127.0.0.1::8008",
            "--env",
            "TUWUNEL_SERVER_NAME=computer.localhost",
            "--env",
            "TUWUNEL_DATABASE_PATH=/var/lib/tuwunel",
            "--env",
            "TUWUNEL_PORT=8008",
            "--env",
            "TUWUNEL_ADDRESS=0.0.0.0",
            "--env",
            "TUWUNEL_ALLOW_REGISTRATION=true",
            "--env",
            "TUWUNEL_YES_I_AM_VERY_VERY_SURE_I_WANT_AN_OPEN_REGISTRATION_SERVER_PRONE_TO_ABUSE=true",
            "--env",
            "TUWUNEL_CREATE_ADMIN_ROOM=true",
            image_id,
        ],
        text=True,
    ).strip()
    try:
        address = subprocess.check_output(["docker", "port", container_id, "8008"], text=True).strip()
        origin = "http://" + address
        state: dict[str, Any] = {
            "container_id": container_id,
            "homeserver": origin,
            "server_name": "computer.localhost",
            "users": {},
        }
        with httpx.Client(base_url=origin, timeout=15) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if client.get("/_matrix/client/versions").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "Matrix fixture readiness timeout"
                time.sleep(0.1)
            for username, display in [
                ("computer_viewer", "Computer Viewer"),
                ("mindroom_writer", "Writer"),
                ("computer_other", "Other Viewer"),
            ]:
                password = secrets.token_urlsafe(24)
                payload = {"username": username, "password": password, "initial_device_display_name": "Computer test"}
                response = client.post("/_matrix/client/v3/register", json=payload)
                if response.status_code == 401:
                    response = client.post(
                        "/_matrix/client/v3/register",
                        json={
                            **payload,
                            "auth": {"type": "m.login.dummy", "session": response.json()["session"]},
                        },
                    )
                response.raise_for_status()
                account = response.json()
                state["users"][username] = {"username": username, "password": password, **account}
                client.put(
                    "/_matrix/client/v3/profile/" + account["user_id"] + "/displayname",
                    headers={"Authorization": "Bearer " + account["access_token"]},
                    json={"displayname": display},
                ).raise_for_status()
            viewer = state["users"]["computer_viewer"]
            agent = state["users"]["mindroom_writer"]
            headers = {"Authorization": "Bearer " + viewer["access_token"]}
            response = client.post(
                "/_matrix/client/v3/createRoom",
                headers=headers,
                json={
                    "name": "Worker Computer Test",
                    "preset": "private_chat",
                    "invite": [agent["user_id"]],
                },
            )
            response.raise_for_status()
            room = response.json()["room_id"]
            client.post(
                "/_matrix/client/v3/join/" + room,
                headers={"Authorization": "Bearer " + agent["access_token"]},
                json={},
            ).raise_for_status()
            response = client.put(
                "/_matrix/client/v3/rooms/" + room + "/send/m.room.message/root",
                headers=headers,
                json={"msgtype": "m.text", "body": "Inspect the local fixture page."},
            )
            response.raise_for_status()
            state["room_id"] = room
            state["thread_id"] = response.json()["event_id"]
            client.put(
                "/_matrix/client/v3/rooms/" + room + "/send/m.room.message/reply",
                headers={"Authorization": "Bearer " + agent["access_token"]},
                json={
                    "msgtype": "m.text",
                    "body": "I am ready to use the browser.",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": state["thread_id"],
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": state["thread_id"]},
                    },
                },
            ).raise_for_status()
        path = output / "matrix-fixture.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(state, indent=2) + "\n")
    except BaseException:
        subprocess.run(["docker", "rm", "-f", container_id], check=True)
        raise
    else:
        return state
