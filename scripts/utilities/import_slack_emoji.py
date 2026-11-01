"""Import a Slack workspace's custom emoji into the server emoji room, so MindRoom Chat offers them to everyone.

Run from the repository with ``uv run python -m scripts.utilities.import_slack_emoji --homeserver <URL>``.
Supply a Slack token with the ``emoji:read`` scope in ``SLACK_TOKEN``, and in ``MATRIX_ACCESS_TOKEN``
the access token of an account that may change the room's emoji packs.
Each emoji keeps its Slack name, and a Slack alias becomes another name for its image.
Names the packs already have are left alone, so another run imports only the emoji that are new in Slack.
``--dry-run`` reports what a run would import, without uploading or changing anything.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from itertools import batched, count
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from PIL import Image

from scripts.utilities.create_server_emoji_room import EMOJI_PACK_EVENT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

# Matrix caps an event at 64 KiB with its envelope, so a pack's content stops well below that, as in MindRoom Chat.
MAX_PACK_CONTENT_BYTES = 60_000
# Each batch is written to the packs before the next one is uploaded, so a stopped run wastes at most one batch.
BATCH_SIZE = 100
MAX_ATTEMPTS = 5
SLACK_EMOJI_LIST = "https://slack.com/api/emoji.list"


@dataclass(frozen=True)
class ImportPlan:
    """The Slack emoji a run imports and the names it skips, each in name order."""

    originals: dict[str, str]  # name to the https:// URL of its image
    aliases: dict[str, str]  # name to the name of the emoji it is another name for
    taken: list[str]  # names the server packs already have
    unusable: list[str]  # names MindRoom Chat cannot use, or with neither an image URL nor an alias


def plan_import(slack_emoji: Mapping[str, str], taken: AbstractSet[str]) -> ImportPlan:
    """Sort Slack's emoji into images to import, aliases to add, and names to skip."""
    plan = ImportPlan(originals={}, aliases={}, taken=[], unusable=[])
    for name, value in sorted(slack_emoji.items()):
        # MindRoom Chat turns whitespace and ":" in a shortcode into "-", so it could never insert such a name.
        if not name or re.search(r"[\s:]", name) or not value.startswith(("alias:", "https://")):
            plan.unusable.append(name)
        elif name in taken:
            plan.taken.append(name)
        elif value.startswith("alias:"):
            plan.aliases[name] = value.removeprefix("alias:")
        else:
            plan.originals[name] = value
    return plan


def pack_content_bytes(content: Mapping[str, Any]) -> int:
    """Return the size of pack content as its event carries it: compact JSON in UTF-8."""
    return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode())


def _with_image(content: Mapping[str, Any], name: str, image: dict[str, Any]) -> dict[str, Any]:
    """Return the pack content with the image added as an emoji; a sticker pack's image is marked as one."""
    # A pack without a usage, or without a usage MindRoom Chat knows, holds both emoji and stickers.
    usage = (content.get("pack") or {}).get("usage")
    if isinstance(usage, list) and "sticker" in usage and "emoticon" not in usage:
        image = {**image, "usage": ["emoticon"]}
    return {**content, "images": {**(content.get("images") or {}), name: image}}


def add_to_packs(
    packs: dict[str, dict[str, Any]],
    entries: Mapping[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Add each image where MindRoom Chat would add it, and return the changed state keys and the names skipped as taken.

    An image goes into the first live pack, in order of state keys, that stays within the size limit.
    When none does, it starts a new pack under the smallest free number from 2 up, with the first pack's metadata.
    """
    changed: list[str] = []
    skipped: list[str] = []
    for name, image in entries.items():
        if any(name in (content.get("images") or {}) for content in packs.values()):
            skipped.append(name)
            continue
        # A deleted pack has empty content, so it takes no images and its state key is free for a new pack.
        live = sorted(key for key, content in packs.items() if "pack" in content or "images" in content)
        fits = (
            key for key in live if pack_content_bytes(_with_image(packs[key], name, image)) <= MAX_PACK_CONTENT_BYTES
        )
        key = next(fits, None)
        if key is None:
            key = next(str(number) for number in count(2) if str(number) not in live) if live else ""
            meta = next((content["pack"] for _, content in sorted(packs.items()) if content.get("pack")), None)
            packs[key] = {"pack": meta} if meta else {}
        packs[key] = _with_image(packs[key], name, image)
        if key not in changed:
            changed.append(key)
    return changed, skipped


def _matrix(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
    """Send a Matrix request, again after the wait a rate limit asks for, and fail on any other error."""
    response = client.request(method, path, **kwargs)
    for _ in range(MAX_ATTEMPTS - 1):
        if response.status_code != 429:
            break
        time.sleep(response.json().get("retry_after_ms", 1000) / 1000)
        response = client.request(method, path, **kwargs)
    if not response.is_success:
        msg = f"{method} {response.url.path} answered {response.status_code}: {response.text}"
        raise httpx.HTTPStatusError(msg, request=response.request, response=response)
    return response


def _slack_emoji(slack: httpx.Client, token: str) -> dict[str, str]:
    """Return the workspace's custom emoji: each name with its image URL or ``alias:<name>``."""
    response = slack.get(SLACK_EMOJI_LIST, headers={"Authorization": f"Bearer {token}"})
    answer = response.json() if response.is_success else {}
    if not answer.get("ok"):
        msg = f"Slack answered {answer.get('error', response.status_code)}"
        raise ValueError(msg)
    return answer["emoji"]


def _upload(matrix: httpx.Client, slack: httpx.Client, url: str) -> dict[str, Any]:
    """Copy a Slack image to the homeserver's media repository and return its pack entry."""
    response = slack.get(url)
    mimetype = response.headers.get("Content-Type", "").partition(";")[0].strip()
    if not response.is_success or not mimetype.startswith("image/"):
        msg = f"download answered {response.status_code} with {mimetype or 'no content type'}"
        raise ValueError(msg)
    with Image.open(io.BytesIO(response.content)) as image:
        width, height = image.size
    headers = {"Content-Type": mimetype}
    uploaded = _matrix(matrix, "POST", "/_matrix/media/v3/upload", content=response.content, headers=headers)
    info = {"w": width, "h": height, "mimetype": mimetype, "size": len(response.content)}
    return {"url": uploaded.json()["content_uri"], "info": info}


def _read_packs(matrix: httpx.Client, room_path: str) -> dict[str, dict[str, Any]]:
    """Return the room's emoji packs by state key, as the homeserver has them now."""
    state = _matrix(matrix, "GET", f"{room_path}/state").json()
    return {event["state_key"]: event["content"] for event in state if event["type"] == EMOJI_PACK_EVENT}


def _images(packs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return every image in the packs by name."""
    return {name: image for content in packs.values() for name, image in (content.get("images") or {}).items()}


def _write(matrix: httpx.Client, room_path: str, entries: Mapping[str, dict[str, Any]]) -> list[str]:
    """Add the images to the packs as they are now, send each changed pack, and return the names taken meanwhile."""
    packs = _read_packs(matrix, room_path)
    changed, skipped = add_to_packs(packs, entries)
    for key in changed:
        _matrix(matrix, "PUT", f"{room_path}/state/{EMOJI_PACK_EVENT}/{quote(key, safe='')}", json=packs[key])
    return skipped


def import_slack_emoji(matrix: httpx.Client, slack: httpx.Client, token: str, room: str | None, dry_run: bool) -> str:
    """Import the custom emoji of the Slack workspace the token belongs to, and return the summary to print."""
    # A bad Matrix token fails here, before anything else. Joining resolves an alias and changes nothing for a member.
    user_id = _matrix(matrix, "GET", "/_matrix/client/v3/account/whoami").json()["user_id"]
    room = room or f"#emoji:{user_id.partition(':')[2]}"
    room_id = _matrix(matrix, "POST", f"/_matrix/client/v3/join/{quote(room, safe='')}", json={}).json()["room_id"]
    room_path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
    slack_emoji = _slack_emoji(slack, token)
    images = _images(_read_packs(matrix, room_path))
    plan = plan_import(slack_emoji, images.keys())
    taken, unusable = len(plan.taken), len(plan.unusable)
    if dry_run:
        # An alias needs its target's image, which this run imports or the packs already have.
        aliases = sum(target in plan.originals or target in images for target in plan.aliases.values())
        unusable += len(plan.aliases) - aliases
        return f"Would import {len(plan.originals)} emoji and {aliases} aliases; skipped {taken} taken, {unusable} unusable."
    imported = failed = 0
    for batch in batched(plan.originals.items(), BATCH_SIZE, strict=False):
        entries: dict[str, dict[str, Any]] = {}
        for name, url in batch:
            try:
                entries[name] = _upload(matrix, slack, url)
            except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError) as error:
                failed += 1
                print(f"Could not import {name} ({url}): {error}", file=sys.stderr)
        skipped = _write(matrix, room_path, entries)
        imported += len(entries) - len(skipped)
        taken += len(skipped)
    # An alias of a standard emoji, or of an image that failed, finds no target and counts as unusable.
    images = _images(_read_packs(matrix, room_path))
    aliases = {
        name: {key: value for key, value in images[target].items() if key in ("url", "info")}
        for name, target in plan.aliases.items()
        if target in images
    }
    skipped = _write(matrix, room_path, aliases)
    taken += len(skipped)
    unusable += len(plan.aliases) - len(aliases)
    return (
        f"Imported {imported} emoji and {len(aliases) - len(skipped)} aliases; "
        f"skipped {taken} taken, {unusable} unusable, {failed} failed."
    )


def main(argv: list[str] | None = None) -> int:
    """Import the emoji, then print how many were imported and skipped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", required=True, help="Base URL of the homeserver")
    parser.add_argument("--room", help="Alias or ID of the server emoji room (default: #emoji:<server of the account>)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be imported and change nothing")
    args = parser.parse_args(argv)
    slack_token = os.environ.get("SLACK_TOKEN")
    matrix_token = os.environ.get("MATRIX_ACCESS_TOKEN")
    if not slack_token:
        parser.error("Set SLACK_TOKEN to a Slack token with the emoji:read scope")
    if not matrix_token:
        parser.error("Set MATRIX_ACCESS_TOKEN to the access token of an account that may change the server emoji")
    try:
        with (
            httpx.Client(
                base_url=args.homeserver.rstrip("/"),
                headers={"Authorization": f"Bearer {matrix_token}"},
                timeout=30,
                trust_env=False,
            ) as matrix,
            httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as slack,
        ):
            summary = import_slack_emoji(matrix, slack, slack_token, args.room, args.dry_run)
    except (TypeError, ValueError, httpx.HTTPError) as error:
        print(f"Import stopped: {error}", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
