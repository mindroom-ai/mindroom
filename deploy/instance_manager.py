#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich"]
# ///
"""Ultra-simple Mindroom instance manager.

No over-engineering, just the basics.
"""
# ruff: noqa: S602  # subprocess with shell=True needed for docker compose
# ruff: noqa: C901, PLR0912, PLR0915  # complexity is acceptable for CLI commands

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Mindroom Instance Manager - Simple multi-instance deployment",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

# Get the script's directory to ensure paths are relative to it
SCRIPT_DIR = Path(__file__).parent.absolute()
REGISTRY_FILE = SCRIPT_DIR / "instances.json"
ENV_TEMPLATE = SCRIPT_DIR / ".env.template"


def load_registry() -> dict[str, Any]:
    """Load the instance registry."""
    # Default structure - use absolute path for data directory
    data_base = os.environ.get("MINDROOM_DATA_BASE", str(SCRIPT_DIR / "instance_data"))
    default_registry = {
        "instances": {},
        "allocated_ports": {"backend": [], "frontend": [], "matrix": []},
        "defaults": {
            "backend_port_start": 8765,
            "frontend_port_start": 3003,
            "matrix_port_start": 8448,
            "data_dir_base": data_base,
        },
    }

    if not REGISTRY_FILE.exists():
        return default_registry

    try:
        with REGISTRY_FILE.open() as f:
            data = json.load(f)
            # Ensure the loaded data has the required structure
            if not isinstance(data, dict):
                return default_registry
            # Merge with defaults to ensure all keys exist
            for key, value in default_registry.items():
                if key not in data:
                    data[key] = value
            return data
    except (json.JSONDecodeError, OSError):
        return default_registry


def save_registry(registry: dict[str, Any]) -> None:
    """Save the instance registry."""
    with REGISTRY_FILE.open("w") as f:
        json.dump(registry, f, indent=2)


def find_next_ports(registry: dict[str, Any]) -> tuple[int, int, int]:
    """Find the next available ports."""
    defaults = registry["defaults"]
    allocated = registry["allocated_ports"]

    backend_port = defaults["backend_port_start"]
    while backend_port in allocated["backend"]:
        backend_port += 1

    frontend_port = defaults["frontend_port_start"]
    while frontend_port in allocated["frontend"]:
        frontend_port += 1

    # Matrix port allocation (starting at 8448)
    matrix_port = defaults.get("matrix_port_start", 8448)
    matrix_allocated = allocated.get("matrix", [])
    while matrix_port in matrix_allocated:
        matrix_port += 1

    return backend_port, frontend_port, matrix_port


@app.command()
def create(
    name: str = typer.Argument(..., help="Instance name"),
    domain: str | None = typer.Option(None, help="Domain for the instance (default: NAME.localhost)"),
    matrix: str | None = typer.Option(
        None,
        "--matrix",
        help="Include Matrix server: 'tuwunel' (lightweight) or 'synapse' (full)",
    ),
) -> None:
    """Create a new instance with automatic port allocation."""
    registry = load_registry()

    if name in registry["instances"]:
        console.print(f"[red]✗[/red] Instance '{name}' already exists!")
        raise typer.Exit(1)

    if matrix and matrix not in ["tuwunel", "synapse"]:
        console.print(f"[red]✗[/red] Invalid matrix option '{matrix}'. Use 'tuwunel' or 'synapse'")
        raise typer.Exit(1)

    backend_port, frontend_port, matrix_port = find_next_ports(registry)
    data_dir = f"{registry['defaults']['data_dir_base']}/{name}"

    instance = {
        "name": name,
        "backend_port": backend_port,
        "frontend_port": frontend_port,
        "matrix_port": matrix_port if matrix else None,
        "data_dir": data_dir,
        "domain": domain or f"{name}.localhost",
        "status": "created",
        "matrix_type": matrix,  # 'tuwunel', 'synapse', or None
    }

    # Create instance env file in script directory
    env_file = SCRIPT_DIR / f".env.{name}"
    if ENV_TEMPLATE.exists():
        shutil.copy(ENV_TEMPLATE, env_file)
    else:
        # Create basic env file if template doesn't exist
        env_file.touch()

    # Append instance-specific vars
    # data_dir is already absolute from the registry defaults
    abs_data_dir = data_dir if Path(data_dir).is_absolute() else str(Path(data_dir).absolute())

    with env_file.open("a") as f:
        f.write("\n# Instance configuration\n")
        f.write(f"INSTANCE_NAME={name}\n")
        f.write(f"BACKEND_PORT={backend_port}\n")
        f.write(f"FRONTEND_PORT={frontend_port}\n")
        # Use absolute path to work correctly from any directory
        f.write(f"DATA_DIR={abs_data_dir}\n")
        f.write(f"INSTANCE_DOMAIN={instance['domain']}\n")

        if matrix:
            f.write(f"\n# Matrix configuration ({matrix})\n")
            f.write(f"MATRIX_PORT={matrix_port}\n")
            f.write(f"MATRIX_SERVER_NAME={instance['domain']}\n")

            if matrix == "tuwunel":
                f.write("MATRIX_ALLOW_REGISTRATION=true\n")
                f.write("MATRIX_ALLOW_FEDERATION=false\n")
            elif matrix == "synapse":
                f.write("POSTGRES_PASSWORD=synapse_password\n")
                f.write("SYNAPSE_REGISTRATION_ENABLED=true\n")

    # Update registry
    registry["instances"][name] = instance
    registry["allocated_ports"]["backend"].append(backend_port)
    registry["allocated_ports"]["frontend"].append(frontend_port)
    if matrix:
        if "matrix" not in registry["allocated_ports"]:
            registry["allocated_ports"]["matrix"] = []
        registry["allocated_ports"]["matrix"].append(matrix_port)
    save_registry(registry)

    console.print(f"[green]✓[/green] Created instance '[cyan]{name}[/cyan]'")
    console.print(f"  [dim]Backend port:[/dim] {backend_port}")
    console.print(f"  [dim]Frontend port:[/dim] {frontend_port}")
    if matrix:
        console.print(f"  [dim]Matrix port:[/dim] {matrix_port}")
    console.print(f"  [dim]Data dir:[/dim] {data_dir}")
    console.print(f"  [dim]Domain:[/dim] {instance['domain']}")
    console.print(f"  [dim]Env file:[/dim] .env.{name}")
    if matrix:
        matrix_name = "Tuwunel (lightweight)" if matrix == "tuwunel" else "Synapse (full)"
        console.print(f"  [dim]Matrix:[/dim] [green]{matrix_name}[/green]")


@app.command()
def start(name: str = typer.Argument(..., help="Instance name to start")) -> None:
    """Start a Mindroom instance."""
    registry = load_registry()
    if name not in registry["instances"]:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    env_file = SCRIPT_DIR / f".env.{name}"
    if not env_file.exists():
        console.print(f"[red]✗[/red] Environment file .env.{name} not found!")
        raise typer.Exit(1)

    # Create data directories
    instance = registry["instances"][name]
    for subdir in ["config", "tmp", "logs"]:
        Path(f"{instance['data_dir']}/{subdir}").mkdir(parents=True, exist_ok=True)

    # Create Matrix data directories if enabled
    matrix_type = instance.get("matrix_type")
    if matrix_type == "tuwunel":
        Path(f"{instance['data_dir']}/tuwunel").mkdir(parents=True, exist_ok=True)
    elif matrix_type == "synapse":
        Path(f"{instance['data_dir']}/synapse").mkdir(parents=True, exist_ok=True)
        Path(f"{instance['data_dir']}/synapse/media").mkdir(parents=True, exist_ok=True)
        Path(f"{instance['data_dir']}/postgres").mkdir(parents=True, exist_ok=True)
        Path(f"{instance['data_dir']}/redis").mkdir(parents=True, exist_ok=True)

        # Copy Synapse config template if needed
        synapse_dir = Path(f"{instance['data_dir']}/synapse")
        if not (synapse_dir / "homeserver.yaml").exists():
            template_dir = SCRIPT_DIR / "synapse-template"
            if template_dir.exists():
                for file in template_dir.glob("*"):
                    if file.is_file():
                        shutil.copy(file, synapse_dir / file.name)

    # Start with docker compose (modern syntax) - run from parent directory for build context
    # Get the parent directory (project root)
    project_root = SCRIPT_DIR.parent
    env_file_relative = f"deploy/.env.{name}"

    if matrix_type == "tuwunel":
        cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.yml -f deploy/docker-compose.tuwunel.yml -p {name} up -d --build"
    elif matrix_type == "synapse":
        cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.yml -f deploy/docker-compose.synapse.yml -p {name} up -d --build"
    else:
        cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.yml -p {name} up -d --build"

    with console.status(f"[yellow]Starting instance '{name}'...[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        registry["instances"][name]["status"] = "running"
        save_registry(registry)
        console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' started successfully!")
    else:
        console.print(f"[red]✗[/red] Failed to start instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)


@app.command()
def stop(name: str = typer.Argument(..., help="Instance name to stop")) -> None:
    """Stop a running Mindroom instance."""
    registry = load_registry()
    if name not in registry["instances"]:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    # Run from parent directory to match start command
    project_root = SCRIPT_DIR.parent
    cmd = f"cd {project_root} && docker compose -p {name} down"

    with console.status(f"[yellow]Stopping instance '{name}'...[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        registry["instances"][name]["status"] = "stopped"
        save_registry(registry)
        console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' stopped!")
    else:
        console.print(f"[red]✗[/red] Failed to stop instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)


@app.command("list")
def list_instances() -> None:
    """List all configured instances."""
    registry = load_registry()
    instances = registry.get("instances", {})

    if not instances:
        console.print("[yellow]No instances configured.[/yellow]")
        console.print("\n[dim]Create your first instance with:[/dim]")
        console.print("  [cyan]./instance_manager.py create my-instance[/cyan]")
        return

    table = Table(title="Mindroom Instances", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Backend", justify="right")
    table.add_column("Frontend", justify="right")
    table.add_column("Matrix", justify="right")
    table.add_column("Domain")
    table.add_column("Data Directory")

    for name, config in instances.items():
        status = config["status"]
        if status == "running":
            status_display = "[green]● running[/green]"
        elif status == "stopped":
            status_display = "[red]● stopped[/red]"
        else:
            status_display = "[yellow]● created[/yellow]"

        matrix_display = ""
        matrix_type = config.get("matrix_type")
        if matrix_type:
            matrix_port = config.get("matrix_port", "N/A")
            if matrix_type == "tuwunel":
                matrix_display = f"[cyan]{matrix_port}[/cyan] [dim](T)[/dim]"
            elif matrix_type == "synapse":
                matrix_display = f"[cyan]{matrix_port}[/cyan] [dim](S)[/dim]"
        else:
            matrix_display = "[dim]disabled[/dim]"

        table.add_row(
            name,
            status_display,
            str(config["backend_port"]),
            str(config["frontend_port"]),
            matrix_display,
            config["domain"],
            config["data_dir"],
        )

    console.print(table)


if __name__ == "__main__":
    app()
