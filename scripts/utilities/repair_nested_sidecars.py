"""Repair one legacy double-prepared Matrix edit, without runtime compatibility code.

Run from the repository with ``uv run python -m scripts.utilities.repair_nested_sidecars``.
Supply the original author's token in ``MATRIX_ACCESS_TOKEN``.
The default is read-only; ``--apply`` publishes and verifies a corrected edit,
then redacts the broken edit so future imports cannot select it again.
Use during a quiet period for the target message.
Only unencrypted rooms are supported; no device crypto store is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx
from nio.crypto.attachments import decrypt_attachment

from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.matrix.sidecar_content import sidecar_content_to_resolve, sidecar_mxc_url


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        msg = "Expected a JSON object from Matrix"
        raise TypeError(msg)
    return value


def _sidecar(client: httpx.Client, content: dict[str, Any]) -> dict[str, Any]:
    owner = sidecar_content_to_resolve(content)
    mxc = sidecar_mxc_url(owner) if owner is not None else None
    if mxc is None:
        msg = "Expected a version-2 message sidecar"
        raise ValueError(msg)
    server, media_id = mxc[6:].split("/", 1)
    endpoint = f"/_matrix/client/v1/media/download/{quote(server, safe='')}/{quote(media_id, safe='')}"
    payload = bytearray()
    with client.stream("GET", endpoint) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > 2 * 1024 * 1024:
                msg = "Attachment exceeds the 2 MiB repair limit"
                raise ValueError(msg)
    plaintext = bytes(payload)
    file_info = owner.get("file") if owner is not None else None
    if isinstance(file_info, dict):
        plaintext = decrypt_attachment(plaintext, file_info["key"]["k"], file_info["hashes"]["sha256"], file_info["iv"])
    value = json.loads(plaintext)
    if not isinstance(value, dict):
        msg = "Expected a JSON object in the attachment"
        raise TypeError(msg)
    return value


def _latest(client: httpx.Client, room_id: str, original: dict[str, Any]) -> dict[str, Any]:
    """Read the complete edit list, refusing truncated or looping pagination."""
    event_id = original["event_id"]
    endpoint = f"/_matrix/client/v1/rooms/{quote(room_id, safe='')}/relations/{quote(event_id, safe='')}/m.replace"
    params: dict[str, str | int] = {"limit": 100, "dir": "b"}
    seen: set[str] = set()
    latest = original
    for _ in range(20):
        page = _json(client.get(endpoint, params=params))
        for event in page["chunk"]:
            content = event.get("content", {})
            relation = content.get("m.relates_to", {})
            if (
                event.get("sender") == original["sender"]
                and event.get("type") == "m.room.message"
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == event_id
                and isinstance(content.get("m.new_content"), dict)
                and (event["origin_server_ts"], event["event_id"]) > (latest["origin_server_ts"], latest["event_id"])
            ):
                latest = event
        token = page.get("next_batch")
        if not token:
            return latest
        if token in seen:
            break
        seen.add(token)
        params["from"] = token
    msg = "Edit history exceeded the repair pagination bound"
    raise ValueError(msg)


def repair(client: httpx.Client, room_id: str, event_id: str, *, apply: bool = False) -> dict[str, str]:  # noqa: C901, PLR0912, PLR0915
    """Replace one current nested edit, verify its source content, then retire it."""
    room_path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
    event_path = f"{room_path}/event/{quote(event_id, safe='')}"
    target = _json(client.get(event_path))
    identity = _json(client.get("/_matrix/client/v3/account/whoami"))
    if target.get("sender") != identity.get("user_id"):
        msg = "Use the original author's access token"
        raise ValueError(msg)
    if target.get("unsigned", {}).get("redacted_because") is not None:
        return {"status": "already_redacted", "event_id": event_id}
    encryption = client.get(f"{room_path}/state/m.room.encryption")
    if encryption.status_code != 404 or encryption.json().get("errcode") != "M_NOT_FOUND":
        msg = "This repair only supports confirmed unencrypted rooms"
        raise ValueError(msg)
    relation = target.get("content", {}).get("m.relates_to", {})
    if target.get("type") != "m.room.message" or relation.get("rel_type") != "m.replace":
        msg = "Select the broken edit event, not the original message"
        raise ValueError(msg)
    logical_id = relation["event_id"]
    original = _json(client.get(f"{room_path}/event/{quote(logical_id, safe='')}"))
    if (
        original.get("sender") != identity["user_id"]
        or original.get("type") != "m.room.message"
        or not original.get("content")
    ):
        msg = "The original message is unavailable or has a different author"
        raise ValueError(msg)

    intermediate = _sidecar(client, target["content"])
    inner = sidecar_content_to_resolve(intermediate)
    if inner is None:
        return {"status": "not_nested", "event_id": event_id}
    complete = _sidecar(client, intermediate)
    body = complete.get("m.new_content")
    if (
        sidecar_content_to_resolve(complete) is not None
        or not isinstance(body, dict)
        or not isinstance(body.get("body"), str)
    ):
        msg = "Expected exactly two layers ending in complete replacement text"
        raise ValueError(msg)
    preview_body = inner.get("body")
    if not isinstance(preview_body, str):
        msg = "Expected the inner sidecar to contain a text preview"
        raise TypeError(msg)
    replacement = build_matrix_edit_content(logical_id, inner)
    replacement["body"] = f"* {preview_body}"
    replacement["m.mentions"] = {}
    encoded = json.dumps(replacement, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > 60_000:
        msg = "The corrected edit is too large to publish without another upload"
        raise ValueError(msg)
    latest = _latest(client, room_id, original)
    already_sent = latest.get("content") == replacement
    if latest["event_id"] != event_id and not already_sent:
        msg = "A different edit is current; refusing to overwrite it"
        raise ValueError(msg)
    if not apply:
        return {"status": "would_repair", "event_id": event_id, "logical_event_id": logical_id}

    # Stable transactions cover a lost send acknowledgement; matching the latest
    # content also permits resuming cleanup with a refreshed access token.
    transaction = "repair-nested-" + hashlib.sha256(room_id.encode() + event_id.encode() + encoded).hexdigest()
    repaired_id = (
        latest["event_id"]
        if already_sent
        else _json(
            client.put(f"{room_path}/send/m.room.message/{transaction}", json=replacement),
        )["event_id"]
    )
    observed = _json(client.get(f"{room_path}/event/{quote(repaired_id, safe='')}"))
    if (
        observed.get("sender") != identity["user_id"]
        or observed.get("type") != "m.room.message"
        or observed.get("content") != replacement
        or _sidecar(client, observed["content"]) != complete
        or _latest(client, room_id, original)["event_id"] != repaired_id
    ):
        msg = "Replacement verification failed; the broken edit was not redacted"
        raise ValueError(msg)
    _json(
        client.put(
            f"{room_path}/redact/{quote(event_id, safe='')}/{transaction}",
            json={"reason": "Replaced legacy nested attachment with its verified single-layer reference"},
        ),
    )
    if _json(client.get(event_path)).get("unsigned", {}).get("redacted_because") is None:
        msg = "Redaction verification failed; rerun the repair to finish cleanup"
        raise ValueError(msg)
    return {"status": "repaired", "event_id": event_id, "replacement_event_id": repaired_id}


def main() -> int:
    """Preview or explicitly apply one repair without printing credentials or text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--event-id", required=True, help="The broken edit event ID")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish the verified replacement and redact the broken edit",
    )
    args = parser.parse_args()
    token = os.environ.get("MATRIX_ACCESS_TOKEN")
    if not token:
        parser.error("Set MATRIX_ACCESS_TOKEN to the original author's access token")
    try:
        with httpx.Client(
            base_url=args.homeserver.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
        ) as client:
            print(json.dumps(repair(client, args.room_id, args.event_id, apply=args.apply)))
    except (TypeError, ValueError, httpx.HTTPError) as error:
        print(f"Repair stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
