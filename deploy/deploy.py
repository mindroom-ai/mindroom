#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich", "pydantic"]
# ///
"""Docker Mindroom instance manager."""
# ruff: noqa: S602  # subprocess with shell=True needed for docker compose
# ruff: noqa: C901  # complexity is acceptable for CLI commands

import base64
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path

import typer
from pydantic import BaseModel, Field
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


# Pydantic Models
class InstanceStatus(str, Enum):
    """Instance status enum."""

    CREATED = "created"
    RUNNING = "running"
    BACKEND_ONLY = "backend-only"
    STOPPED = "stopped"


class MatrixType(str, Enum):
    """Matrix server type enum."""

    TUWUNEL = "tuwunel"
    SYNAPSE = "synapse"


class Instance(BaseModel):
    """Instance configuration model."""

    name: str
    backend_port: int
    frontend_port: int
    matrix_port: int | None = None
    data_dir: str
    domain: str
    status: InstanceStatus = InstanceStatus.CREATED
    matrix_type: MatrixType | None = None


class AllocatedPorts(BaseModel):
    """Allocated ports tracking model."""

    backend: list[int] = Field(default_factory=list)
    frontend: list[int] = Field(default_factory=list)
    matrix: list[int] = Field(default_factory=list)


class RegistryDefaults(BaseModel):
    """Registry defaults configuration."""

    backend_port_start: int = 8765
    frontend_port_start: int = 3003
    matrix_port_start: int = 8448
    data_dir_base: str = Field(default_factory=lambda: str(SCRIPT_DIR / "instance_data"))


class Registry(BaseModel):
    """Complete registry model."""

    instances: dict[str, Instance] = Field(default_factory=dict)
    allocated_ports: AllocatedPorts = Field(default_factory=AllocatedPorts)
    defaults: RegistryDefaults = Field(default_factory=RegistryDefaults)


def load_registry() -> Registry:
    """Load the instance registry."""
    # Override default data base if env var is set
    data_base = os.environ.get("MINDROOM_DATA_BASE")

    if not REGISTRY_FILE.exists():
        registry = Registry()
        if data_base:
            registry.defaults.data_dir_base = data_base
        return registry

    try:
        with REGISTRY_FILE.open() as f:
            data = json.load(f)
            registry = Registry(**data)
            if data_base:
                registry.defaults.data_dir_base = data_base
            return registry

    except (json.JSONDecodeError, OSError, ValueError) as e:
        console.print(f"[yellow]Warning: Could not load registry: {e}[/yellow]")
        console.print("[yellow]Creating new registry.[/yellow]")
        registry = Registry()
        if data_base:
            registry.defaults.data_dir_base = data_base
        return registry


def save_registry(registry: Registry) -> None:
    """Save the instance registry."""
    with REGISTRY_FILE.open("w") as f:
        # Convert to dict for JSON serialization
        data = registry.model_dump(mode="json")
        json.dump(data, f, indent=2)


def find_next_ports(registry: Registry) -> tuple[int, int, int]:
    """Find the next available ports."""
    defaults = registry.defaults
    allocated = registry.allocated_ports

    backend_port = defaults.backend_port_start
    while backend_port in allocated.backend:
        backend_port += 1

    frontend_port = defaults.frontend_port_start
    while frontend_port in allocated.frontend:
        frontend_port += 1

    # Matrix port allocation (starting at 8448)
    matrix_port = defaults.matrix_port_start
    while matrix_port in allocated.matrix:
        matrix_port += 1

    return backend_port, frontend_port, matrix_port


@app.command()
def create(  # noqa: PLR0912, PLR0915
    name: str = typer.Argument("default", help="Instance name"),
    domain: str | None = typer.Option(None, help="Domain for the instance (default: NAME.localhost)"),
    matrix: str | None = typer.Option(
        None,
        "--matrix",
        help="Include Matrix server: 'tuwunel' (lightweight) or 'synapse' (full)",
    ),
) -> None:
    """Create a new instance with automatic port allocation."""
    registry = load_registry()

    if name in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' already exists!")
        raise typer.Exit(1)

    # Validate and convert matrix type to enum
    matrix_type: MatrixType | None = None
    if matrix:
        try:
            matrix_type = MatrixType(matrix)
        except ValueError:
            console.print(f"[red]✗[/red] Invalid matrix option '{matrix}'. Use 'tuwunel' or 'synapse'")
            raise typer.Exit(1)  # noqa: B904

    backend_port, frontend_port, matrix_port_value = find_next_ports(registry)
    data_dir = f"{registry.defaults.data_dir_base}/{name}"

    instance = Instance(
        name=name,
        backend_port=backend_port,
        frontend_port=frontend_port,
        matrix_port=matrix_port_value if matrix_type else None,
        data_dir=data_dir,
        domain=domain or f"{name}.localhost",
        status=InstanceStatus.CREATED,
        matrix_type=matrix_type,
    )

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
        f.write(f"INSTANCE_DOMAIN={instance.domain}\n")

        if matrix:
            f.write(f"\n# Matrix configuration ({matrix})\n")
            f.write(f"MATRIX_PORT={matrix_port_value}\n")
            f.write(f"MATRIX_SERVER_NAME={instance.domain}\n")

            if matrix == "tuwunel":
                f.write("MATRIX_ALLOW_REGISTRATION=true\n")
                f.write("MATRIX_ALLOW_FEDERATION=false\n")
            elif matrix == "synapse":
                f.write("POSTGRES_PASSWORD=synapse_password\n")
                f.write("SYNAPSE_REGISTRATION_ENABLED=true\n")

    # If Synapse, prepare the config directory
    if matrix == "synapse":
        synapse_dir = Path(data_dir) / "synapse"
        synapse_dir.mkdir(parents=True, exist_ok=True)
        # Create media_store directory for Synapse
        (synapse_dir / "media_store").mkdir(parents=True, exist_ok=True)

        # Copy and customize Synapse config template
        template_dir = SCRIPT_DIR / "synapse-template"
        if template_dir.exists():
            for file in template_dir.glob("*"):
                if file.is_file():
                    if file.name == "homeserver.yaml":
                        # Generate homeserver.yaml with correct values
                        with file.open() as f:
                            content = f.read()
                        # Replace hardcoded values with instance-specific ones
                        server_name = instance.domain
                        content = content.replace('server_name: "localhost"', f'server_name: "{server_name}"')
                        content = content.replace(
                            "public_baseurl: http://localhost:8008/",
                            f"public_baseurl: http://{server_name}:{matrix_port_value}/",
                        )
                        with (synapse_dir / file.name).open("w") as f:
                            f.write(content)
                    elif file.name == "signing.key":
                        # Generate a unique signing key for this instance
                        # Generate 32 random bytes for ed25519 key
                        key_bytes = secrets.token_bytes(32)
                        key_b64 = base64.b64encode(key_bytes).decode("ascii")
                        # Use instance name as key ID for uniqueness
                        key_id = f"{name}_{secrets.token_hex(3)}"
                        signing_key_content = f"ed25519 {key_id} {key_b64}\n"
                        with (synapse_dir / file.name).open("w") as f:
                            f.write(signing_key_content)
                        console.print("  [dim]Generated unique signing key for instance[/dim]")
                    else:
                        shutil.copy(file, synapse_dir / file.name)

    # Update registry
    registry.instances[name] = instance
    registry.allocated_ports.backend.append(backend_port)
    registry.allocated_ports.frontend.append(frontend_port)
    if matrix_type:
        registry.allocated_ports.matrix.append(matrix_port_value)
    save_registry(registry)

    console.print(f"[green]✓[/green] Created instance '[cyan]{name}[/cyan]'")
    console.print(f"  [dim]Backend port:[/dim] {backend_port}")
    console.print(f"  [dim]Frontend port:[/dim] {frontend_port}")
    if matrix:
        console.print(f"  [dim]Matrix port:[/dim] {matrix_port_value}")
    console.print(f"  [dim]Data dir:[/dim] {data_dir}")
    console.print(f"  [dim]Domain:[/dim] {instance.domain}")
    console.print(f"  [dim]Env file:[/dim] .env.{name}")
    if matrix:
        matrix_name = "Tuwunel (lightweight)" if matrix == "tuwunel" else "Synapse (full)"
        console.print(f"  [dim]Matrix:[/dim] [green]{matrix_name}[/green]")


@app.command()
def start(  # noqa: PLR0912, PLR0915
    name: str = typer.Argument("default", help="Instance name to start"),
    no_frontend: bool = typer.Option(False, "--no-frontend", help="Start without frontend (for development)"),
) -> None:
    """Start a Mindroom instance."""
    registry = load_registry()
    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    env_file = SCRIPT_DIR / f".env.{name}"
    if not env_file.exists():
        console.print(f"[red]✗[/red] Environment file .env.{name} not found!")
        raise typer.Exit(1)

    # Create data directories with proper permissions
    instance = registry.instances[name]
    for subdir in ["config", "tmp", "logs", "mindroom", "mindroom/credentials", "mem0"]:
        dir_path = Path(f"{instance.data_dir}/{subdir}")
        dir_path.mkdir(parents=True, exist_ok=True)
        # Ensure proper ownership if we can
        with contextlib.suppress(OSError, PermissionError):
            os.chown(dir_path, os.getuid(), os.getgid())

    # Create Matrix data directories if enabled
    matrix_type = instance.matrix_type
    if matrix_type == MatrixType.TUWUNEL:
        Path(f"{instance.data_dir}/tuwunel").mkdir(parents=True, exist_ok=True)
    elif matrix_type == MatrixType.SYNAPSE:
        Path(f"{instance.data_dir}/synapse").mkdir(parents=True, exist_ok=True)
        Path(f"{instance.data_dir}/synapse/media_store").mkdir(parents=True, exist_ok=True)
        Path(f"{instance.data_dir}/postgres").mkdir(parents=True, exist_ok=True)
        Path(f"{instance.data_dir}/redis").mkdir(parents=True, exist_ok=True)

        # Copy Synapse config template if needed
        synapse_dir = Path(f"{instance.data_dir}/synapse")
        if not (synapse_dir / "homeserver.yaml").exists():
            template_dir = SCRIPT_DIR / "synapse-template"
            if template_dir.exists():
                for file in template_dir.glob("*"):
                    if file.is_file():
                        if file.name == "homeserver.yaml":
                            # Generate homeserver.yaml with correct values
                            with file.open() as f:
                                content = f.read()
                            # Replace hardcoded values with instance-specific ones
                            server_name = instance.domain
                            # For public_baseurl, use the external port that will be mapped
                            matrix_port_display = instance.matrix_port or 8008
                            content = content.replace('server_name: "localhost"', f'server_name: "{server_name}"')
                            content = content.replace(
                                "public_baseurl: http://localhost:8008/",
                                f"public_baseurl: http://{server_name}:{matrix_port_display}/",
                            )
                            with (synapse_dir / file.name).open("w") as f:
                                f.write(content)
                        else:
                            shutil.copy(file, synapse_dir / file.name)

    # Start with docker compose (modern syntax) - run from parent directory for build context
    # Get the parent directory (project root)
    project_root = SCRIPT_DIR.parent
    env_file_relative = f"deploy/.env.{name}"

    # Build the docker compose command
    base_cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.yml"

    if matrix_type == "tuwunel":
        compose_files = f"{base_cmd} -f deploy/docker-compose.tuwunel.yml"
    elif matrix_type == MatrixType.SYNAPSE:
        compose_files = f"{base_cmd} -f deploy/docker-compose.synapse.yml"
    else:
        compose_files = base_cmd

    # Add services to start (backend always, frontend only if not --no-frontend)
    services = "backend"
    if matrix_type:
        # Add Matrix-related services
        if matrix_type == "synapse":
            services += " postgres redis synapse"
        elif matrix_type == "tuwunel":
            services += " tuwunel"

    if not no_frontend:
        services = "frontend " + services  # Add frontend to the list
        status_msg = f"Starting instance '{name}'..."
    else:
        status_msg = f"Starting instance '{name}' (backend only)..."
        console.print("[yellow]ℹ[/yellow] Starting without frontend (development mode)")  # noqa: RUF001

    cmd = f"{compose_files} -p {name} up -d --build {services}"

    with console.status(f"[yellow]{status_msg}[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        # Update status to reflect what's actually running
        if no_frontend:
            registry.instances[name].status = InstanceStatus.BACKEND_ONLY
        else:
            registry.instances[name].status = InstanceStatus.RUNNING
        save_registry(registry)

        if no_frontend:
            console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' started successfully (backend only)!")
            console.print(f"  [dim]Backend:[/dim] http://localhost:{instance.backend_port}")
            if matrix_type:
                console.print(f"  [dim]Matrix:[/dim] http://localhost:{instance.matrix_port or 8008}")
        else:
            console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' started successfully!")
    else:
        console.print(f"[red]✗[/red] Failed to start instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)


@app.command()
def stop(name: str = typer.Argument("default", help="Instance name to stop")) -> None:
    """Stop a running Mindroom instance."""
    registry = load_registry()
    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    # Run from parent directory to match start command
    project_root = SCRIPT_DIR.parent
    cmd = f"cd {project_root} && docker compose -p {name} down"

    with console.status(f"[yellow]Stopping instance '{name}'...[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        registry.instances[name].status = InstanceStatus.STOPPED
        save_registry(registry)
        console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' stopped!")
    else:
        console.print(f"[red]✗[/red] Failed to stop instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)


@app.command()
def remove(
    name: str = typer.Argument(None, help="Instance name to remove (or use --all)"),
    all: bool = typer.Option(False, "--all", help="Remove ALL instances"),  # noqa: A002
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Remove instance(s) completely (containers, data, and configuration)."""
    registry = load_registry()
    instances = registry.instances

    # Handle --all flag
    if all:
        if not instances:
            console.print("[yellow]No instances to remove.[/yellow]")
            return

        # Confirmation prompt unless --force is used
        if not force:
            console.print(f"[red]⚠️  WARNING:[/red] This will permanently delete ALL {len(instances)} instance(s):")
            for instance_name in instances:
                console.print(f"  - {instance_name}")
            console.print("\n[yellow]All data, containers, and configurations will be lost![/yellow]")
            confirm = typer.confirm("Are you absolutely sure?")
            if not confirm:
                console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)

        # Remove each instance
        for instance_name in list(instances.keys()):
            _remove_instance(instance_name, registry, console)

        # Clear the registry completely
        REGISTRY_FILE.unlink(missing_ok=True)
        console.print("\n[green]✓[/green] All instances removed!")
        return

    # Handle single instance removal
    if not name:
        console.print("[red]✗[/red] Please specify an instance name or use --all")
        raise typer.Exit(1)

    if name not in instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    instance = instances[name]

    # Confirmation prompt unless --force is used
    if not force:
        console.print(f"[yellow]⚠️  Warning:[/yellow] This will permanently delete instance '[cyan]{name}[/cyan]'")
        console.print(f"  - All data in {instance.data_dir}")
        console.print(f"  - Environment file .env.{name}")
        console.print("  - All containers and volumes")
        confirm = typer.confirm("Are you sure you want to continue?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    _remove_instance(name, registry, console)
    save_registry(registry)
    console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' completely removed!")


def _remove_instance(name: str, registry: Registry, console: Console) -> None:
    """Helper function to remove a single instance."""
    instance = registry.instances[name]

    with console.status(f"[yellow]Removing instance '{name}'...[/yellow]"):
        # Stop containers if running
        project_root = SCRIPT_DIR.parent
        stop_cmd = f"cd {project_root} && docker compose -p {name} down -v 2>/dev/null"
        subprocess.run(stop_cmd, check=False, shell=True, capture_output=True, text=True)

        # Remove data directory
        data_dir = Path(instance.data_dir)
        if data_dir.exists():
            shutil.rmtree(data_dir)

        # Remove env file
        env_file = SCRIPT_DIR / f".env.{name}"
        if env_file.exists():
            env_file.unlink()

        # Update registry - remove instance and free up ports
        del registry.instances[name]

        # Remove allocated ports
        if instance.backend_port in registry.allocated_ports.backend:
            registry.allocated_ports.backend.remove(instance.backend_port)
        if instance.frontend_port in registry.allocated_ports.frontend:
            registry.allocated_ports.frontend.remove(instance.frontend_port)
        if instance.matrix_port and instance.matrix_port in registry.allocated_ports.matrix:
            registry.allocated_ports.matrix.remove(instance.matrix_port)


def get_actual_status(name: str) -> tuple[bool, bool, bool]:
    """Check which containers are actually running.

    Returns: (backend_running, frontend_running, matrix_running)
    """
    cmd = f"docker compose -p {name} ps --format json 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        return False, False, False

    running_containers = set()
    for line in result.stdout.strip().split("\n"):
        if line:
            try:
                data = json.loads(line)
                if data.get("State") == "running":
                    running_containers.add(data.get("Service"))
            except json.JSONDecodeError:
                pass

    backend_running = "backend" in running_containers
    frontend_running = "frontend" in running_containers
    matrix_running = any(m in running_containers for m in ["synapse", "tuwunel", "postgres", "redis"])

    return backend_running, frontend_running, matrix_running


@app.command("list")
def list_instances() -> None:
    """List all configured instances."""
    registry = load_registry()
    instances = registry.instances

    if not instances:
        console.print("[yellow]No instances configured.[/yellow]")
        console.print("\n[dim]Create your first instance with:[/dim]")
        console.print("  [cyan]./deploy.py create my-instance[/cyan]")
        return

    table = Table(title="Mindroom Instances", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Backend", justify="right")
    table.add_column("Frontend", justify="right")
    table.add_column("Matrix", justify="right")
    table.add_column("Domain")
    table.add_column("Data Directory")

    for name, instance in instances.items():
        # Get actual container status
        backend_up, frontend_up, matrix_up = get_actual_status(name)

        # Determine status display based on actual running containers
        if not any([backend_up, frontend_up, matrix_up]):
            status_display = "[red]● stopped[/red]"
        elif frontend_up and backend_up:
            status_display = "[green]● running[/green]"
        elif backend_up and not frontend_up:
            status_display = "[blue]● backend[/blue]"
        elif frontend_up and not backend_up:
            status_display = "[yellow]● frontend[/yellow]"
        else:
            # Some containers running but not the main ones
            status_display = "[yellow]● partial[/yellow]"

        matrix_display = ""
        if instance.matrix_type:
            matrix_port = instance.matrix_port or "N/A"
            if instance.matrix_type == MatrixType.TUWUNEL:
                matrix_display = f"[cyan]{matrix_port}[/cyan] [dim](T)[/dim]"
            elif instance.matrix_type == MatrixType.SYNAPSE:
                matrix_display = f"[cyan]{matrix_port}[/cyan] [dim](S)[/dim]"
        else:
            matrix_display = "[dim]disabled[/dim]"

        table.add_row(
            name,
            status_display,
            str(instance.backend_port),
            str(instance.frontend_port),
            matrix_display,
            instance.domain,
            instance.data_dir,
        )

    console.print(table)


if __name__ == "__main__":
    # If no arguments provided, show help
    if len(sys.argv) == 1:
        # Show help by appending --help to argv
        sys.argv.append("--help")
    app()
