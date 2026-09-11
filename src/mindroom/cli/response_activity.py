"""Check response activity through the running MindRoom API."""

from __future__ import annotations

import json
import math
from ipaddress import ip_address
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer

from mindroom.cli.config import activate_cli_runtime
from mindroom.constants import DEFAULT_MINDROOM_URL

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.response_activity import ResponseActivity


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _request_activity(runtime_paths: RuntimePaths, url: str | None, timeout: float) -> ResponseActivity:
    import httpx  # noqa: PLC0415

    from mindroom.response_activity import ResponseActivity  # noqa: PLC0415

    if not math.isfinite(timeout):
        msg = "--timeout must be finite."
        raise ValueError(msg)
    base_url = url or runtime_paths.env_value("MINDROOM_URL") or DEFAULT_MINDROOM_URL
    try:
        parsed_url = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        msg = "Invalid MindRoom URL."
        raise ValueError(msg) from exc
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.host
        or parsed_url.userinfo
        or parsed_url.query
        or parsed_url.fragment
    ):
        msg = "Use an absolute HTTP(S) URL without credentials, query, or fragment."
        raise ValueError(msg)
    token = runtime_paths.env_value("MINDROOM_API_KEY")
    if token and parsed_url.scheme == "http" and not _is_loopback(parsed_url.host):
        msg = "Use HTTPS when sending MINDROOM_API_KEY to a remote endpoint."
        raise ValueError(msg)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/api/responses/activity",
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        msg = "Cannot reach MindRoom; check --url / MINDROOM_URL and that its API is running."
        raise ValueError(msg) from exc
    if response.status_code not in {200, 503}:
        msg = f"Activity request failed (HTTP {response.status_code}); check authentication and the running version."
        raise ValueError(msg)
    try:
        payload = response.json()
        snapshot = ResponseActivity.model_validate(payload)
    except ValueError as exc:
        msg = "MindRoom returned an invalid activity snapshot; check the URL and running version."
        raise ValueError(msg) from exc
    if payload.get("status") != snapshot.status:
        msg = "MindRoom returned a missing or conflicting activity status."
        raise ValueError(msg)
    if response.status_code == 503 and snapshot.status != "unavailable":
        msg = "MindRoom returned an unavailable HTTP status with a conflicting activity snapshot."
        raise ValueError(msg)
    return snapshot


def check_active_responses(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Select the runtime environment file."),  # noqa: B008
    url: str | None = typer.Option(
        None,
        "--url",
        help="MindRoom base URL; defaults to MINDROOM_URL or localhost:8765.",
    ),
    timeout: float = typer.Option(10.0, "--timeout", min=0.001, help="HTTP timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check live responses; exit 0 idle, 1 busy, or 2 unavailable."""
    try:
        runtime_paths = activate_cli_runtime(path=config_path)
        snapshot = _request_activity(runtime_paths, url, timeout)
    except (ValueError, OSError) as exc:
        if json_output:
            typer.echo(json.dumps({"status": "unavailable", "detail": str(exc)}))
        else:
            typer.echo(f"Response activity unavailable: {exc}", err=True)
        raise typer.Exit(2) from None

    if json_output:
        typer.echo(snapshot.model_dump_json())
    elif snapshot.status == "idle":
        typer.echo("No active responses.")
    elif snapshot.status == "busy":
        typer.echo(
            f"Active work: {snapshot.active_matrix_operations} Matrix operation(s), "
            f"{snapshot.active_openai_requests} OpenAI request(s).",
        )
    else:
        typer.echo(
            f"Response activity unavailable (runtime: {snapshot.runtime_phase}, admission paused: {snapshot.admission_paused}).",
        )
    raise typer.Exit({"idle": 0, "busy": 1, "unavailable": 2}[snapshot.status])
