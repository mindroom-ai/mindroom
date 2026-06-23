"""CLI helpers for signed external triggers."""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import secrets
import stat
import time
from pathlib import Path  # noqa: TC003
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from rich.markup import escape

from mindroom.cli.config import console
from mindroom.external_triggers.auth import sign_trigger_request

_DEFAULT_BASE_URL = "http://127.0.0.1:8765"
_DEFAULT_KEY_ID = "default"
_DEFAULT_TIMEOUT = 10.0
_ED25519_PRIVATE_KEY_BYTES = 32
_DATA_JSON_OBJECT_ERROR = "--data-json must decode to a JSON object"
_PRIVATE_KEY_PATH_ERROR = "private key path must be a regular file"
_PRIVATE_KEY_ERROR = "raw 32-byte Ed25519 private key required"
_URL_ABSOLUTE_ERROR = "--url must be an absolute URL"
_URL_QUERY_FRAGMENT_ERROR = "--url must not include query or fragment"

trigger_app = typer.Typer(help="Send signed external triggers.")


@trigger_app.command("keygen")
def keygen(
    private_key_file: Path = typer.Option(  # noqa: B008
        ...,
        "--private-key-file",
        help="Path where the base64 raw Ed25519 private key should be written.",
    ),
) -> None:
    """Generate an Ed25519 trigger signing key."""
    private_key = Ed25519PrivateKey.generate()
    private_key_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_key_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    _write_private_key_file(private_key_file, base64.b64encode(private_key_bytes).decode("ascii"))

    console.out(f"private_key_file={private_key_file}")
    console.out(f"public_key={base64.b64encode(public_key_bytes).decode('ascii')}")


def _write_private_key_file(private_key_file: Path, private_key_b64: str) -> None:
    try:
        mode = private_key_file.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            raise typer.BadParameter(_PRIVATE_KEY_PATH_ERROR)
        private_key_file.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        file_descriptor = os.open(private_key_file, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise typer.BadParameter(_PRIVATE_KEY_PATH_ERROR) from exc
        raise
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(private_key_b64)


@trigger_app.command("send")
def send(
    trigger_id: str = typer.Argument(..., help="Configured external trigger id."),
    key_file: Path = typer.Option(  # noqa: B008
        ...,
        "--key-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Base64 raw Ed25519 private key file.",
    ),
    kind: str = typer.Option(..., "--kind", help="Trigger payload kind."),
    message: str = typer.Option(..., "--message", help="Trigger payload message."),
    event_id: str | None = typer.Option(None, "--event-id", help="Optional idempotency event id."),
    title: str | None = typer.Option(None, "--title", help="Optional trigger title."),
    data_json: str | None = typer.Option(None, "--data-json", help="Optional JSON object for trigger data."),
    timeout: float = typer.Option(_DEFAULT_TIMEOUT, "--timeout", help="HTTP request timeout in seconds."),
    verify_tls: bool = typer.Option(True, "--verify-tls/--no-verify-tls", help="Verify TLS certificates."),
    url: str = typer.Option(_DEFAULT_BASE_URL, "--url", help="MindRoom base URL."),
    key_id: str = typer.Option(_DEFAULT_KEY_ID, "--key-id", help="Trigger signing key id."),
) -> None:
    """Send a signed external trigger request."""
    request_url, path = _trigger_request_url_and_path(url, trigger_id)
    body = _trigger_body_bytes(
        kind=kind,
        message=message,
        event_id=event_id or secrets.token_hex(16),
        title=title,
        data=_decode_data_json(data_json),
    )
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    headers = sign_trigger_request(
        method="POST",
        path=path,
        body=body,
        key_id=key_id,
        timestamp=timestamp,
        nonce=nonce,
        private_key=_load_private_key(key_file),
    )
    headers["content-type"] = "application/json"

    try:
        response = httpx.post(
            request_url,
            content=body,
            headers=headers,
            timeout=timeout,
            verify=verify_tls,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        console.print(
            f"[red]Error:[/red] external trigger request failed: {escape(_status_error_detail(exc.response))}",
        )
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        console.print(f"[red]Error:[/red] external trigger request failed: {escape(str(exc))}")
        raise typer.Exit(1) from exc
    console.print_json(data=response.json())


def _trigger_request_url_and_path(base_url: str, trigger_id: str) -> tuple[str, str]:
    """Return the request URL and exact path covered by the trigger signature."""
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise typer.BadParameter(_URL_ABSOLUTE_ERROR)
    if parts.query or parts.fragment:
        raise typer.BadParameter(_URL_QUERY_FRAGMENT_ERROR)

    base_path = parts.path.rstrip("/")
    path = f"{base_path}/api/triggers/{trigger_id}" if base_path else f"/api/triggers/{trigger_id}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), path


def _status_error_detail(response: httpx.Response) -> str:
    detail: object
    try:
        data = response.json()
    except ValueError:
        detail = response.text
    else:
        detail = data.get("detail", data) if isinstance(data, dict) else data
    return f"HTTP {response.status_code}: {detail}"


def _trigger_body_bytes(
    *,
    kind: str,
    message: str,
    event_id: str,
    title: str | None,
    data: dict[str, object],
) -> bytes:
    payload: dict[str, object | None] = {
        "kind": kind,
        "message": message,
        "event_id": event_id,
        "title": title,
        "data": data,
    }
    return json.dumps(
        {key: value for key, value in payload.items() if value is not None},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _decode_data_json(data_json: str | None) -> dict[str, object]:
    if data_json is None:
        return {}
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(_DATA_JSON_OBJECT_ERROR) from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(_DATA_JSON_OBJECT_ERROR)
    return cast("dict[str, object]", data)


def _load_private_key(key_file: Path) -> Ed25519PrivateKey:
    try:
        key_bytes = base64.b64decode(key_file.read_text(encoding="utf-8").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise typer.BadParameter(_PRIVATE_KEY_ERROR) from exc
    if len(key_bytes) != _ED25519_PRIVATE_KEY_BYTES:
        raise typer.BadParameter(_PRIVATE_KEY_ERROR)
    try:
        return Ed25519PrivateKey.from_private_bytes(key_bytes)
    except ValueError as exc:
        raise typer.BadParameter(_PRIVATE_KEY_ERROR) from exc
