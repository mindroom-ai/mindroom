"""Local stack setup command implementation for MindRoom CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer

from mindroom.cli_config import console
from mindroom.constants import CONFIG_PATH, STORAGE_PATH

_CINNY_DEFAULT_IMAGE = "ghcr.io/mindroom-ai/mindroom-cinny:latest"
_CINNY_DEFAULT_CONTAINER = "mindroom-cinny-local"


def local_stack_setup(
    synapse_dir: Path = typer.Option(  # noqa: B008
        Path("local/matrix"),
        "--synapse-dir",
        help="Directory containing Synapse docker-compose.yml (from mindroom-stack settings).",
    ),
    homeserver_url: str = typer.Option(
        "http://localhost:8008",
        "--homeserver-url",
        help="Homeserver URL that Cinny and MindRoom should use.",
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
        help="Local host port for the MindRoom Cinny container.",
    ),
    cinny_image: str = typer.Option(
        _CINNY_DEFAULT_IMAGE,
        "--cinny-image",
        help="Docker image for MindRoom Cinny.",
    ),
    cinny_container_name: str = typer.Option(
        _CINNY_DEFAULT_CONTAINER,
        "--cinny-container-name",
        help="Container name for MindRoom Cinny.",
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
    """Start local Synapse + MindRoom Cinny using Docker only."""
    _require_supported_platform()
    _require_binary("docker", "Docker is required but was not found in PATH.")

    inferred_server_name = server_name or _infer_server_name(homeserver_url)
    synapse_dir = synapse_dir.expanduser().resolve()
    if not skip_synapse:
        _start_synapse_stack(synapse_dir)

    synapse_versions_url = f"{homeserver_url.rstrip('/')}/_matrix/client/versions"
    _wait_for_service(synapse_versions_url, "Synapse")

    cinny_config_path = _write_local_cinny_config(homeserver_url, inferred_server_name)
    console.print(f"Cinny config written: [dim]{cinny_config_path}[/dim]")

    cinny_url = f"http://localhost:{cinny_port}"
    _start_cinny_container(
        cinny_container_name=cinny_container_name,
        cinny_port=cinny_port,
        cinny_config_path=cinny_config_path,
        cinny_image=cinny_image,
    )
    _wait_for_service(f"{cinny_url}/config.json", "Cinny")

    _print_local_stack_summary(
        homeserver_url=homeserver_url,
        cinny_url=cinny_url,
        server_name=inferred_server_name,
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


def _write_local_cinny_config(homeserver_url: str, server_name: str) -> Path:
    """Write a minimal Cinny config for local MindRoom development."""
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
    target = Path(STORAGE_PATH).expanduser().resolve() / "local" / "cinny-config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
    return target


def _persist_local_matrix_env(homeserver_url: str, server_name: str) -> Path:
    """Write local Matrix settings to .env next to the active config file."""
    env_path = Path(CONFIG_PATH).expanduser().resolve().parent / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    updates = {
        "MATRIX_HOMESERVER": homeserver_url,
        "MATRIX_SSL_VERIFY": "false",
        "MATRIX_SERVER_NAME": server_name,
    }
    for key, value in updates.items():
        lines = _upsert_env_var(lines, key, value)

    env_path.write_text(f"{'\n'.join(lines)}\n", encoding="utf-8")
    return env_path


def _upsert_env_var(lines: list[str], key: str, value: str) -> list[str]:
    """Upsert a single KEY=value entry while preserving unrelated lines."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for idx, line in enumerate(lines):
        if pattern.match(line):
            lines[idx] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def _require_supported_platform() -> None:
    """Ensure local-stack-setup runs only on Linux/macOS."""
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        return
    console.print("[red]Error:[/red] local-stack-setup currently supports Linux and macOS only.")
    raise typer.Exit(1)


def _require_binary(name: str, message: str) -> None:
    """Ensure a required binary is present in PATH."""
    if shutil.which(name) is not None:
        return
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(1)


def _start_synapse_stack(synapse_dir: Path) -> None:
    """Start Synapse via docker compose in the provided directory."""
    compose_file = synapse_dir / "docker-compose.yml"
    if not compose_file.exists():
        console.print(f"[red]Error:[/red] Synapse compose file not found: {compose_file}")
        raise typer.Exit(1)

    console.print(f"Starting Synapse stack from [bold]{synapse_dir}[/bold]...")
    result = _run_command(["docker", "compose", "up", "-d"], cwd=synapse_dir, check=False)
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
    """Start (or replace) the local MindRoom Cinny container."""
    _run_command(["docker", "rm", "-f", cinny_container_name], check=False)

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
    result = _run_command(run_cmd, check=False)
    if result.returncode != 0:
        _print_command_failure(result, "Failed to start MindRoom Cinny container")
        raise typer.Exit(1)


def _wait_for_service(url: str, service_name: str) -> None:
    """Wait for a service URL to become healthy."""
    console.print(f"Waiting for {service_name}: [dim]{url}[/dim]")
    if _wait_for_http_success(url, timeout_seconds=60, verify=False):
        return
    console.print(f"[red]Error:[/red] {service_name} did not become healthy at {url}")
    raise typer.Exit(1)


def _print_local_stack_summary(
    *,
    homeserver_url: str,
    cinny_url: str,
    server_name: str,
    persist_env: bool,
    cinny_container_name: str,
    synapse_dir: Path,
    skip_synapse: bool,
) -> None:
    """Print final setup instructions."""
    console.print("\n[green]Local stack is ready.[/green]")
    console.print(f"  Synapse: {homeserver_url}")
    console.print(f"  Cinny:   {cinny_url}")
    console.print(f"  Server:  {server_name}")
    if persist_env:
        env_path = _persist_local_matrix_env(homeserver_url, server_name)
        console.print(f"  Env:     {env_path}")
        console.print("\nRun MindRoom backend:")
        console.print("  uv run mindroom run")
    else:
        console.print("\nRun MindRoom backend against this stack:")
        console.print(f"  MATRIX_HOMESERVER={homeserver_url} MATRIX_SSL_VERIFY=false uv run mindroom run")
    console.print("\nStop commands:")
    console.print(f"  docker rm -f {cinny_container_name}")
    if not skip_synapse:
        console.print(f"  cd {synapse_dir} && docker compose down")


def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return CompletedProcess."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def _print_command_failure(result: subprocess.CompletedProcess[str], prefix: str) -> None:
    """Print a compact subprocess failure summary."""
    details = result.stderr.strip() or result.stdout.strip() or "no error details"
    console.print(f"[red]Error:[/red] {prefix}: {details}")


def _wait_for_http_success(
    url: str,
    *,
    timeout_seconds: int,
    verify: bool,
) -> bool:
    """Wait until an HTTP GET request returns success."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=3, verify=verify)
            if response.is_success:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False
