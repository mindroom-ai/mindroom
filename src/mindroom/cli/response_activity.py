"""Check response activity through the running MindRoom API."""

from __future__ import annotations

import json
import math
from pathlib import Path  # noqa: TC003
from time import monotonic, sleep
from typing import TYPE_CHECKING

import typer

from mindroom.cli.api import get_api_response
from mindroom.cli.config import activate_cli_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.response_activity import DetailedResponseActivity, ResponseActivity

_WAIT_POLL_INTERVAL_SECONDS = 5.0


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


def _wait_for_idle(
    runtime_paths: RuntimePaths,
    url: str | None,
    timeout: float,
    *,
    details: bool,
    wait: float,
) -> ResponseActivity | DetailedResponseActivity:
    """Poll while busy; idle, unavailable, or the deadline ends the wait with that snapshot."""
    if not math.isfinite(wait):
        msg = "--wait must be finite."
        raise ValueError(msg)
    deadline = monotonic() + wait
    while True:
        snapshot = _request_activity(runtime_paths, url, timeout, details=details)
        remaining = deadline - monotonic()
        if snapshot.status != "busy" or remaining <= 0:
            return snapshot
        sleep(min(_WAIT_POLL_INTERVAL_SECONDS, remaining))


def _echo_text(snapshot: ResponseActivity) -> None:
    if snapshot.status == "idle":
        typer.echo("No active responses, calls, or interruptible script runs.")
    elif snapshot.status == "busy":
        typer.echo(
            f"Active work: {snapshot.active_matrix_operations} Matrix operation(s), "
            f"{snapshot.active_openai_requests} OpenAI request(s), {snapshot.active_calls} call(s), "
            f"{snapshot.interruptible_script_runs} interruptible script run(s).",
        )
    else:
        typer.echo(
            f"Response activity unavailable (runtime: {snapshot.runtime_phase}, admission paused: {snapshot.admission_paused}).",
        )
    if snapshot.status != "unavailable" and snapshot.recoverable_script_runs:
        typer.echo(f"Not counted as busy: {snapshot.recoverable_script_runs} recoverable script run(s).")


def _echo_details(snapshot: DetailedResponseActivity) -> None:
    channels = {"matrix": "Matrix", "openai": "OpenAI", "call": "Call"}
    for row in snapshot.responses:
        responder = row.responder or "unknown responder"
        requester = row.requester_id or "unknown requester"
        typer.echo(f"{channels[row.channel]}: {responder} for {requester}")
    for run in snapshot.script_runs:
        restart = "recoverable" if run.recoverable else "interrupted by restart"
        typer.echo(f"Script: {run.responder} for {run.requester_id} ({run.run_id}, {restart})")


def check_active_responses(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Select the runtime environment file."),  # noqa: B008
    url: str | None = typer.Option(
        None,
        "--url",
        help="MindRoom base URL; defaults to MINDROOM_URL or localhost:8765.",
    ),
    timeout: float = typer.Option(10.0, "--timeout", min=0.001, help="HTTP timeout in seconds."),
    wait: float = typer.Option(
        0.0,
        "--wait",
        min=0,
        help="Poll up to this many seconds while busy; unavailable ends the wait.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    details: bool = typer.Option(False, "--details", help="Show authenticated responder and requester details."),
) -> None:
    """Check live work; exit 0 idle, 1 busy, or 2 unavailable."""
    try:
        runtime_paths = activate_cli_runtime(path=config_path)
        snapshot = _wait_for_idle(runtime_paths, url, timeout, details=details, wait=wait)
    except (ValueError, OSError) as exc:
        if json_output:
            typer.echo(json.dumps({"status": "unavailable", "detail": str(exc)}))
        else:
            typer.echo(f"Response activity unavailable: {exc}", err=True)
        raise typer.Exit(2) from None

    if json_output:
        typer.echo(snapshot.model_dump_json())
    else:
        _echo_text(snapshot)
    if details and not json_output:
        from mindroom.response_activity import DetailedResponseActivity  # noqa: PLC0415

        if not isinstance(snapshot, DetailedResponseActivity):
            msg = "Detailed response activity snapshot expected"
            raise TypeError(msg)
        _echo_details(snapshot)
    raise typer.Exit({"idle": 0, "busy": 1, "unavailable": 2}[snapshot.status])
