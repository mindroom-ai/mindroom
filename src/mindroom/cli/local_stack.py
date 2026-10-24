"""Local stack setup command implementation for MindRoom CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import typer

from mindroom.matrix.health import matrix_versions_url, response_has_matrix_versions

from .config import activate_cli_runtime, console
from .env_file import env_path_for_config, upsert_env_values

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from mindroom.constants import RuntimePaths

_CINNY_DEFAULT_IMAGE = "ghcr.io/mindroom-ai/mindroom-chat:latest"
_CINNY_DEFAULT_CONTAINER = "mindroom-cinny-local"


def _httpx_get(url: str) -> httpx.Response:
    """Call httpx.get without importing httpx during CLI help rendering."""
    import httpx  # noqa: PLC0415

    return httpx.get(url, timeout=3, verify=False)  # noqa: S501 - probes the local stack's own services


def local_stack_setup(
    synapse_dir: Path = typer.Option(  # noqa: B008
        Path("local/matrix"),
        "--synapse-dir",
        help="Directory containing Synapse docker-compose.yml (core MindRoom repo: local/matrix).",
    ),
    homeserver_url: str = typer.Option(
        "http://localhost:8008",
        "--homeserver-url",
        help="Homeserver URL that MindRoom Chat and MindRoom should use.",
    ),
    server_name: str | None = typer.Option(
        None,
        "--server-name",
        help="Matrix server name (default: inferred from --homeserver-url hostname).",
    ),
    cinny_port: int = typer.Option(
        8080,
        "--cinny-port",
        min=1,
        max=65535,
        help="Local host port for the MindRoom Chat container.",
    ),
    cinny_image: str = typer.Option(
        _CINNY_DEFAULT_IMAGE,
        "--cinny-image",
        help="Docker image for MindRoom Chat.",
    ),
    cinny_container_name: str = typer.Option(
        _CINNY_DEFAULT_CONTAINER,
        "--cinny-container-name",
        help="Container name for MindRoom Chat (legacy default retained for compatibility).",
    ),
    skip_synapse: bool = typer.Option(
        False,
        "--skip-synapse",
        help="Skip starting Synapse (assume it is already running).",
    ),
    persist_env: bool = typer.Option(
        True,
        "--persist-env/--no-persist-env",
        help="Persist Matrix local dev settings to .env next to config.yaml.",
    ),
) -> None:
    """Start local Synapse + MindRoom Chat using Docker only."""
    runtime_paths = activate_cli_runtime()
    _require_supported_platform()
    if shutil.which("docker") is None:
        console.print("[red]Error:[/red] Docker is required but was not found in PATH.")
        raise typer.Exit(1)

    inferred_server_name = server_name or _infer_server_name(homeserver_url)
    synapse_dir = synapse_dir.expanduser().resolve()
    if not skip_synapse:
        _start_synapse_stack(synapse_dir)

    _wait_for_service(matrix_versions_url(homeserver_url), "Synapse", response_has_matrix_versions)

    cinny_config_path = _write_local_cinny_config(
        homeserver_url,
        inferred_server_name,
        runtime_paths,
    )
    console.print(f"MindRoom Chat config written: [dim]{cinny_config_path}[/dim]")

    cinny_url = f"http://localhost:{cinny_port}"
    _start_cinny_container(
        cinny_container_name=cinny_container_name,
        cinny_port=cinny_port,
        cinny_config_path=cinny_config_path,
        cinny_image=cinny_image,
    )
    _wait_for_service(f"{cinny_url}/config.json", "MindRoom Chat")

    _print_local_stack_summary(
        homeserver_url=homeserver_url,
        cinny_url=cinny_url,
        server_name=inferred_server_name,
        config_path=runtime_paths.config_path,
        persist_env=persist_env,
        cinny_container_name=cinny_container_name,
        synapse_dir=synapse_dir,
        skip_synapse=skip_synapse,
    )


def _infer_server_name(homeserver_url: str) -> str:
    """Infer Matrix server_name from a homeserver URL."""
    parsed = urlparse(homeserver_url)
    if not parsed.scheme or not parsed.hostname:
        console.print(f"[red]Error:[/red] Invalid homeserver URL: {homeserver_url}")
        raise typer.Exit(1)
    return parsed.hostname


def _write_local_cinny_config(
    homeserver_url: str,
    server_name: str,
    runtime_paths: RuntimePaths,
) -> Path:
    """Write a minimal MindRoom Chat config for local development."""
    config = {
        "defaultHomeserver": 0,
        "homeserverList": [homeserver_url],
        "allowCustomHomeservers": True,
        "featuredCommunities": {
            "openAsDefault": False,
            "spaces": [],
            "rooms": [f"#lobby:{server_name}"],
            "servers": [homeserver_url],
        },
        "hashRouter": {"enabled": False, "basename": "/"},
        "sidebar": {"showExploreCommunity": False, "showAddSpace": False},
        "auth": {"hideServerPickerWhenSingle": True},
    }
    target = runtime_paths.storage_root / "local" / "cinny-config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
    return target


def _needs_ssl_verify_override(homeserver_url: str) -> bool:
    """Return whether the local homeserver needs MATRIX_SSL_VERIFY=false for its self-signed certificate.

    Plain HTTP needs no certificate override, and a persisted one would stop verifying a hosted homeserver set later.
    """
    return urlparse(homeserver_url).scheme == "https"


def _persist_local_matrix_env(
    homeserver_url: str,
    server_name: str,
    *,
    config_path: Path,
) -> Path:
    """Write local Matrix settings to .env next to the active config file."""
    values = {"MATRIX_HOMESERVER": homeserver_url, "MATRIX_SERVER_NAME": server_name}
    if _needs_ssl_verify_override(homeserver_url):
        values["MATRIX_SSL_VERIFY"] = "false"
    return upsert_env_values(env_path_for_config(config_path), values)


def _require_supported_platform() -> None:
    """Ensure local-stack-setup runs only on Linux/macOS."""
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        return
    console.print("[red]Error:[/red] local-stack-setup currently supports Linux and macOS only.")
    raise typer.Exit(1)


def _start_synapse_stack(synapse_dir: Path) -> None:
    """Start Synapse via docker compose in the provided directory."""
    compose_file = synapse_dir / "docker-compose.yml"
    if not compose_file.exists():
        console.print(f"[red]Error:[/red] Synapse compose file not found: {compose_file}")
        raise typer.Exit(1)

    console.print(f"Starting Synapse stack from [bold]{synapse_dir}[/bold]...")
    result = _run_command(["docker", "compose", "up", "-d"], cwd=synapse_dir)
    if result.returncode != 0:
        _print_command_failure(result, "Failed to start Synapse stack")
        raise typer.Exit(1)


def _start_cinny_container(
    *,
    cinny_container_name: str,
    cinny_port: int,
    cinny_config_path: Path,
    cinny_image: str,
) -> None:
    """Start (or replace) the local MindRoom Chat container."""
    _run_command(["docker", "rm", "-f", cinny_container_name])

    run_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        cinny_container_name,
        "--restart",
        "unless-stopped",
        "-p",
        f"{cinny_port}:80",
        "-v",
        f"{cinny_config_path}:/app/config.json:ro",
        cinny_image,
    ]
    result = _run_command(run_cmd)
    if result.returncode != 0:
        _print_command_failure(result, "Failed to start MindRoom Chat container")
        raise typer.Exit(1)


def _wait_for_service(
    url: str,
    service_name: str,
    response_matches: Callable[[httpx.Response], bool] | None = None,
) -> None:
    """Wait up to 60 seconds for a service URL to answer with success, or with a response ``response_matches``."""
    import httpx  # noqa: PLC0415

    console.print(f"Waiting for {service_name}: [dim]{url}[/dim]")
    deadline = time.monotonic() + 60
    matcher = response_matches or (lambda response: response.is_success)
    while time.monotonic() < deadline:
        try:
            if matcher(_httpx_get(url)):
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    console.print(f"[red]Error:[/red] {service_name} did not become healthy at {url}")
    raise typer.Exit(1)


def _print_local_stack_summary(
    *,
    homeserver_url: str,
    cinny_url: str,
    server_name: str,
    config_path: Path,
    persist_env: bool,
    cinny_container_name: str,
    synapse_dir: Path,
    skip_synapse: bool,
) -> None:
    """Print final setup instructions."""
    console.print("\n[green]Local stack is ready.[/green]")
    console.print(f"  Synapse: {homeserver_url}")
    console.print(f"  MindRoom Chat: {cinny_url}")
    console.print(f"  Server:  {server_name}")
    if persist_env:
        env_path = _persist_local_matrix_env(
            homeserver_url,
            server_name,
            config_path=config_path,
        )
        console.print(f"  Env:     {env_path}")
        console.print("\nRun MindRoom backend:")
        console.print("  uv run mindroom run")
    else:
        ssl_override = " MATRIX_SSL_VERIFY=false" if _needs_ssl_verify_override(homeserver_url) else ""
        console.print("\nRun MindRoom backend against this stack:")
        console.print(f"  MATRIX_HOMESERVER={homeserver_url}{ssl_override} uv run mindroom run")
    console.print("\nStop commands:")
    console.print(f"  docker rm -f {cinny_container_name}")
    if not skip_synapse:
        console.print(f"  cd {synapse_dir} && docker compose down")


def _run_command(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return CompletedProcess."""
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False, capture_output=True, text=True)


def _print_command_failure(result: subprocess.CompletedProcess[str], prefix: str) -> None:
    """Print a compact subprocess failure summary."""
    details = result.stderr.strip() or result.stdout.strip() or "no error details"
    console.print(f"[red]Error:[/red] {prefix}: {details}")
