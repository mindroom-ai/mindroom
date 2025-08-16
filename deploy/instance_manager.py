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

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Mindroom Instance Manager - Simple multi-instance deployment")
console = Console()

REGISTRY_FILE = "instances.json"
ENV_TEMPLATE = ".env.template"


def load_registry() -> dict[str, Any]:
    """Load the instance registry."""
    registry_path = Path(REGISTRY_FILE)
    if not registry_path.exists():
        # Use local data directory for testing, can be changed to /mnt/data in production
        data_base = os.environ.get("MINDROOM_DATA_BASE", "./instance_data")
        return {
            "instances": {},
            "allocated_ports": {"backend": [], "frontend": []},
            "defaults": {"backend_port_start": 8765, "frontend_port_start": 3003, "data_dir_base": data_base},
        }
    with registry_path.open() as f:
        return json.load(f)


def save_registry(registry: dict[str, Any]) -> None:
    """Save the instance registry."""
    with Path(REGISTRY_FILE).open("w") as f:
        json.dump(registry, f, indent=2)


def find_next_ports(registry: dict[str, Any]) -> tuple[int, int]:
    """Find the next available ports."""
    defaults = registry["defaults"]
    allocated = registry["allocated_ports"]

    backend_port = defaults["backend_port_start"]
    while backend_port in allocated["backend"]:
        backend_port += 1

    frontend_port = defaults["frontend_port_start"]
    while frontend_port in allocated["frontend"]:
        frontend_port += 1

    return backend_port, frontend_port


@app.command()
def create(
    name: str = typer.Argument(..., help="Instance name"),
    domain: str | None = typer.Option(None, help="Domain for the instance (default: NAME.localhost)"),
) -> None:
    """Create a new instance with automatic port allocation."""
    registry = load_registry()

    if name in registry["instances"]:
        console.print(f"[red]✗[/red] Instance '{name}' already exists!")
        raise typer.Exit(1)

    backend_port, frontend_port = find_next_ports(registry)
    data_dir = f"{registry['defaults']['data_dir_base']}/{name}"

    instance = {
        "name": name,
        "backend_port": backend_port,
        "frontend_port": frontend_port,
        "data_dir": data_dir,
        "domain": domain or f"{name}.localhost",
        "status": "created",
    }

    # Create instance env file
    env_file = f".env.{name}"
    if Path(ENV_TEMPLATE).exists():
        shutil.copy(ENV_TEMPLATE, env_file)
    else:
        # Create basic env file if template doesn't exist
        Path(env_file).touch()

    # Append instance-specific vars
    with Path(env_file).open("a") as f:
        f.write("\n# Instance configuration\n")
        f.write(f"INSTANCE_NAME={name}\n")
        f.write(f"BACKEND_PORT={backend_port}\n")
        f.write(f"FRONTEND_PORT={frontend_port}\n")
        f.write(f"DATA_DIR={data_dir}\n")
        f.write(f"INSTANCE_DOMAIN={instance['domain']}\n")

    # Update registry
    registry["instances"][name] = instance
    registry["allocated_ports"]["backend"].append(backend_port)
    registry["allocated_ports"]["frontend"].append(frontend_port)
    save_registry(registry)

    console.print(f"[green]✓[/green] Created instance '[cyan]{name}[/cyan]'")
    console.print(f"  [dim]Backend port:[/dim] {backend_port}")
    console.print(f"  [dim]Frontend port:[/dim] {frontend_port}")
    console.print(f"  [dim]Data dir:[/dim] {data_dir}")
    console.print(f"  [dim]Domain:[/dim] {instance['domain']}")
    console.print(f"  [dim]Env file:[/dim] .env.{name}")


@app.command()
def start(name: str = typer.Argument(..., help="Instance name to start")) -> None:
    """Start a Mindroom instance."""
    registry = load_registry()
    if name not in registry["instances"]:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    env_file = f".env.{name}"
    if not Path(env_file).exists():
        console.print(f"[red]✗[/red] Environment file {env_file} not found!")
        raise typer.Exit(1)

    # Create data directories
    instance = registry["instances"][name]
    for subdir in ["config", "tmp", "logs"]:
        Path(f"{instance['data_dir']}/{subdir}").mkdir(parents=True, exist_ok=True)

    # Start with docker compose (modern syntax)
    cmd = f"docker compose --env-file {env_file} -p {name} up -d"

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

    cmd = f"docker compose -p {name} down"

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
    instances = registry["instances"]

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

        table.add_row(
            name,
            status_display,
            str(config["backend_port"]),
            str(config["frontend_port"]),
            config["domain"],
            config["data_dir"],
        )

    console.print(table)


if __name__ == "__main__":
    app()
