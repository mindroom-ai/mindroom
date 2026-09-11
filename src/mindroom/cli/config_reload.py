"""Fingerprint config sources and confirm their runtime reload."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer

from mindroom.cli.api import get_api_response
from mindroom.cli.config import CONFIG_PATH_OPTION, activate_cli_runtime

if TYPE_CHECKING:
    from mindroom.config_reload import ConfigReloadStatus
    from mindroom.constants import RuntimePaths


def _request_status(runtime_paths: RuntimePaths, url: str | None, timeout: float) -> ConfigReloadStatus:
    from mindroom.config_reload import ConfigReloadStatus  # noqa: PLC0415

    response = get_api_response(runtime_paths, url, "/api/config/reload-status", timeout, require_key=True)
    if response.status_code not in {200, 503}:
        msg = f"Reload status request failed (HTTP {response.status_code})."
        raise ValueError(msg)
    payload = response.json()
    if not isinstance(payload, dict) or not {"status", "fingerprint"} <= payload.keys():
        msg = "MindRoom returned an invalid reload status."
        raise ValueError(msg)
    status = ConfigReloadStatus.model_validate(payload)
    if response.status_code == 503 and status.status != "unavailable":
        msg = "MindRoom returned a conflicting reload status."
        raise ValueError(msg)
    return status


def _wait_for_applied(
    runtime_paths: RuntimePaths,
    url: str | None,
    expected: str,
    wait: float,
    timeout: float,
) -> ConfigReloadStatus:
    from mindroom.config_reload import ConfigReloadStatus  # noqa: PLC0415

    for name, value in (("wait", wait), ("timeout", timeout)):
        if not math.isfinite(value):
            msg = f"--{name} must be finite."
            raise ValueError(msg)
    ConfigReloadStatus(status="pending", fingerprint=expected)
    deadline = time.monotonic() + wait
    status: ConfigReloadStatus | None = None
    while True:
        request_timeout = min(timeout, max(0.001, deadline - time.monotonic())) if wait else timeout
        try:
            status = _request_status(runtime_paths, url, request_timeout)
        except TimeoutError:
            if status is not None and wait and time.monotonic() >= deadline:
                return status
            raise
        if status.status != "unavailable" and status.fingerprint != expected:
            status = ConfigReloadStatus(status="pending", fingerprint=status.fingerprint)
        remaining = deadline - time.monotonic()
        if status.status != "pending" or remaining <= 0:
            return status
        time.sleep(min(1.0, remaining))
        if time.monotonic() >= deadline:
            return status


def _fingerprint(path: Path) -> str:
    from mindroom.config.legacy_access import access_config_needs_migration  # noqa: PLC0415
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415
    from mindroom.config.yaml_includes import (  # noqa: PLC0415
        load_yaml_config_source_with_digests,
        source_files_fingerprint,
    )

    try:
        data, digests = load_yaml_config_source_with_digests(path)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        msg = "Cannot fingerprint config; check its YAML and include paths."
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = "Config YAML must contain a mapping."
        raise ValueError(msg)  # noqa: TRY004 - invalid file contents, not a caller's argument type
    if access_config_needs_migration(data):
        msg = "Run 'mindroom config migrate --path <config-path>' before fingerprinting legacy access settings."
        raise ValueError(msg)
    return source_files_fingerprint(path, digests)


def config_fingerprint(path: Path | None = CONFIG_PATH_OPTION) -> None:
    """Print the config source SHA-256, including all transitively included files."""
    try:
        typer.echo(_fingerprint(activate_cli_runtime(path).config_path))
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


def config_check_applied(
    path: Path | None = CONFIG_PATH_OPTION,
    fingerprint: str | None = typer.Option(None, help="Expected source fingerprint; defaults to the selected config."),
    url: str | None = typer.Option(None, help="MindRoom base URL; defaults to MINDROOM_URL or localhost:8765."),
    wait: float = typer.Option(0.0, min=0.0, help="Wait up to this many seconds for the matching reload."),
    timeout: float = typer.Option(10.0, min=0.001, help="HTTP timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Confirm config application; exit 0 applied, 1 pending/mismatch, 2 failed/restart-required/unavailable.

    The expected fingerprint is captured once, before polling. This reads
    status only: it does not write config or trigger a reload.
    """
    try:
        runtime_paths = activate_cli_runtime(path)
        expected = fingerprint if fingerprint is not None else _fingerprint(runtime_paths.config_path)
        status = _wait_for_applied(runtime_paths, url, expected, wait, timeout)
    except (ValueError, OSError) as exc:
        if json_output:
            typer.echo(json.dumps({"status": "unavailable", "detail": str(exc)}))
        else:
            typer.echo(f"Config reload status unavailable: {exc}", err=True)
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(json.dumps({**status.model_dump(), "expected_fingerprint": expected}))
    else:
        typer.echo(f"Config reload {status.status}: {expected}")
    raise typer.Exit(0 if status.status == "applied" else 1 if status.status == "pending" else 2)
