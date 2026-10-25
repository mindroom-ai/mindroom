"""Borrow a CLI's saved OAuth login: refresh its token under the CLI's lock, request the refresh, save it privately.

The Codex and Kimi Code routes both lend a token that their CLI keeps in a JSON file. A refresh holds the CLI's
file lock, so MindRoom and a running CLI never refresh or write that file at the same time.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.providers.errors import CliLoginError


def borrow_under_lock[T](credentials_path: Path, *, usable: Callable[[], T | None], refresh: Callable[[], T]) -> T:
    """Return the stored token ``usable`` reads when it is still valid, else ``refresh`` it under the CLI's lock.

    ``usable`` reads the file again under the lock, so a refresh another borrower finished meanwhile is reused.
    """
    if (token := usable()) is not None:
        return token
    with advisory_file_lock(credentials_path.with_name(f"{credentials_path.name}.lock")):
        if (token := usable()) is not None:
            return token
        return refresh()


def post_token_refresh(
    url: str,
    *,
    service: str,
    error: type[CliLoginError],
    refused: Callable[[httpx.Response], str],
    json_body: Mapping[str, str] | None = None,
    form_body: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Post a refresh to ``url``, as JSON or as a form, and return the refreshed token fields it answers with.

    Raises ``error``, naming ``service``, when the request fails or its answer is no JSON object, and with the
    message ``refused`` writes when the login server refuses the refresh.
    """
    try:
        response = httpx.post(url, json=json_body, data=form_body, timeout=10)
    except httpx.HTTPError as exc:
        msg = f"{service} token refresh failed: {exc}"
        raise error(msg) from exc

    if not response.is_success:
        raise error(refused(response), status_code=response.status_code) from None

    try:
        refreshed = response.json()
    except json.JSONDecodeError as exc:
        msg = f"{service} token refresh returned invalid JSON."
        raise error(msg) from exc
    if not isinstance(refreshed, dict):
        msg = f"{service} token refresh returned an unexpected payload."
        raise error(msg)
    return refreshed


def write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace ``path`` with ``payload`` as indented JSON that only its owner can read, never exposing a partial file."""
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(payload, indent=2))
    temp_path.replace(path)
    path.chmod(0o600)
