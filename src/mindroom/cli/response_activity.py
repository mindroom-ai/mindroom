"""Check response activity through the running MindRoom API."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer

from mindroom.cli.api import get_api_response
from mindroom.cli.config import activate_cli_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.response_activity import DetailedResponseActivity, ResponseActivity


def _request_activity(
    runtime_paths: RuntimePaths,
    url: str | None,
    timeout: float,
    *,
    details: bool = False,
) -> ResponseActivity | DetailedResponseActivity:
    from mindroom.response_activity import DetailedResponseActivity, ResponseActivity  # noqa: PLC0415

    response = get_api_response(
        runtime_paths,
        url,
        f"/api/responses/activity{'/details' if details else ''}",
        timeout,
        require_key=details,
    )
    if response.status_code not in {200, 503}:
        msg = f"Activity request failed (HTTP {response.status_code}); check authentication and the running version."
        raise ValueError(msg)
    try:
        payload = response.json()
        model = DetailedResponseActivity if details else ResponseActivity
        snapshot = model.model_validate(payload)
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
    details: bool = typer.Option(False, "--details", help="Show authenticated responder and requester details."),
) -> None:
    """Check live responses; exit 0 idle, 1 busy, or 2 unavailable."""
    try:
        runtime_paths = activate_cli_runtime(path=config_path)
        snapshot = _request_activity(runtime_paths, url, timeout, details=details)
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
    if details and not json_output:
        from mindroom.response_activity import DetailedResponseActivity  # noqa: PLC0415

        if not isinstance(snapshot, DetailedResponseActivity):
            msg = "Detailed response activity snapshot expected"
            raise TypeError(msg)
        for row in snapshot.responses:
            channel = "Matrix" if row.channel == "matrix" else "OpenAI"
            responder = row.responder or "unknown responder"
            requester = row.requester_id or "unknown requester"
            typer.echo(f"{channel}: {responder} for {requester}")
    raise typer.Exit({"idle": 0, "busy": 1, "unavailable": 2}[snapshot.status])
