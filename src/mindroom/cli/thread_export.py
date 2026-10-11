"""Request thread exports from the selected running MindRoom installation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import TypeAdapter

from mindroom.cli.api import resolve_api_target
from mindroom.thread_export.models import ThreadExportStats

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


async def request_thread_export(
    *,
    runtime_paths: RuntimePaths,
    url: str | None,
    output_dir: Path | None,
    room_filter: str | None,
    max_thread_roots: int,
    include_invited_rooms: bool,
) -> ThreadExportStats:
    """Use the runtime API and its normal bearer authentication, with no offline fallback.

    Raises `ValueError` before any request when the URL is invalid or would carry `MINDROOM_API_KEY` over remote HTTP.
    """
    target = resolve_api_target(runtime_paths, url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, read=None),
            follow_redirects=False,
            trust_env=target.trust_env,
        ) as client:
            response = await client.post(
                f"{target.base_url}/api/threads/export",
                headers=target.headers,
                json={
                    "config_path": str(runtime_paths.config_path),
                    "storage_root": str(runtime_paths.storage_root),
                    "output_dir": str(output_dir.expanduser().absolute()) if output_dir is not None else None,
                    "room_filter": room_filter,
                    "max_thread_roots": max_thread_roots,
                    "include_invited_rooms": include_invited_rooms,
                },
            )
    except httpx.HTTPError as exc:
        msg = f"Cannot reach MindRoom at {target.base_url}; start MindRoom with its API enabled, or set --url / MINDROOM_URL"
        raise RuntimeError(msg) from exc
    if response.is_error:
        msg = f"Thread export request failed ({response.status_code}): {response.text}"
        raise RuntimeError(msg)
    try:
        return TypeAdapter(ThreadExportStats).validate_json(response.content)
    except ValueError as exc:
        msg = "MindRoom returned an invalid thread export response; check --url and the running version"
        raise RuntimeError(msg) from exc
