#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich", "pydantic", "pyyaml"]
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
import yaml
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
    frontend_port_start: int = 3005
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


def _prepare_matrix_config(
    instance: Instance,
    matrix_type: MatrixType,
    config_file_name: str,
    template_dir: Path,
    target_dir: Path,
) -> None:
    """Prepare Matrix configuration files (shared logic for Synapse and Tuwunel)."""
    if not template_dir.exists():
        return

    matrix_server_name = f"m-{instance.domain}"

    for file in template_dir.glob("*"):
        if not file.is_file():
            continue

        if file.name == config_file_name:
            # Generate config file with correct values
            with file.open() as f:
                content = f.read()

            # Common replacements
            content = content.replace('server_name: "localhost"', f'server_name: "{matrix_server_name}"')
            content = content.replace(
                "public_baseurl: http://localhost:8008/",
                f"public_baseurl: https://{matrix_server_name}/",
            )

            # Matrix-specific replacements
            if matrix_type == MatrixType.SYNAPSE:
                # Replace postgres and redis hostnames with container names
                content = content.replace("host: postgres", f"host: {instance.name}-postgres")
                content = content.replace("host: redis", f"host: {instance.name}-redis")
            elif matrix_type == MatrixType.TUWUNEL:
                # Tuwunel-specific replacements if needed
                pass

            with (target_dir / file.name).open("w") as f:
                f.write(content)

        elif file.name == "signing.key" and matrix_type == MatrixType.SYNAPSE:
            # Generate a unique signing key for Synapse
            key_bytes = secrets.token_bytes(32)
            key_b64 = base64.b64encode(key_bytes).decode("ascii")
            key_id = f"{instance.name}_{secrets.token_hex(3)}"
            signing_key_content = f"ed25519 {key_id} {key_b64}\n"

            with (target_dir / file.name).open("w") as f:
                f.write(signing_key_content)
            console.print("  [dim]Generated unique signing key for instance[/dim]")

        else:
            shutil.copy(file, target_dir / file.name)


def _get_docker_compose_files(instance: Instance, env_file_relative: str, project_root: Path) -> str:
    """Get the docker-compose command with appropriate files based on matrix type."""
    base_cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.yml"

    if instance.matrix_type == MatrixType.TUWUNEL:
        return f"{base_cmd} -f deploy/docker-compose.tuwunel.yml"
    if instance.matrix_type == MatrixType.SYNAPSE:
        return f"{base_cmd} -f deploy/docker-compose.synapse.yml"
    return base_cmd


def _get_matrix_services(matrix_type: MatrixType | None) -> str:
    """Get the list of services to start based on matrix type."""
    if matrix_type == MatrixType.SYNAPSE:
        return " postgres redis synapse"
    if matrix_type == MatrixType.TUWUNEL:
        return " tuwunel"
    return ""


def _create_environment_file(instance: Instance, name: str, matrix_type: MatrixType | None) -> None:
    """Create and configure the environment file for an instance."""
    env_file = SCRIPT_DIR / f".env.{name}"
    if ENV_TEMPLATE.exists():
        shutil.copy(ENV_TEMPLATE, env_file)
    else:
        env_file.touch()

    # data_dir is already absolute from the registry defaults
    abs_data_dir = (
        instance.data_dir if Path(instance.data_dir).is_absolute() else str(Path(instance.data_dir).absolute())
    )

    with env_file.open("a") as f:
        f.write("\n# Instance configuration\n")
        f.write(f"INSTANCE_NAME={name}\n")
        f.write(f"BACKEND_PORT={instance.backend_port}\n")
        f.write(f"FRONTEND_PORT={instance.frontend_port}\n")
        f.write(f"DATA_DIR={abs_data_dir}\n")
        f.write(f"INSTANCE_DOMAIN={instance.domain}\n")

        # Extract base domain from instance domain
        if "." in instance.domain:
            base_domain = ".".join(instance.domain.split(".")[1:])
            f.write(f"DOMAIN={base_domain}\n")

        if matrix_type:
            f.write(f"\n# Matrix configuration ({matrix_type.value})\n")
            f.write(f"MATRIX_PORT={instance.matrix_port}\n")
            f.write(f"MATRIX_SERVER_NAME=m-{instance.domain}\n")

            if matrix_type == MatrixType.TUWUNEL:
                f.write("MATRIX_ALLOW_REGISTRATION=true\n")
                f.write("MATRIX_ALLOW_FEDERATION=true\n")
            elif matrix_type == MatrixType.SYNAPSE:
                f.write("POSTGRES_PASSWORD=synapse_password\n")
                f.write("SYNAPSE_REGISTRATION_ENABLED=true\n")
                f.write("SYNAPSE_ALLOW_PUBLIC_ROOMS=true\n")


def _verify_extra_hosts_for_federation(matrix_type: MatrixType | None, instances: dict[str, Instance]) -> None:
    """Verify that docker-compose files have required extra_hosts entries for federation."""
    if not matrix_type:
        return

    # Get all active Matrix domains that need to be in extra_hosts
    required_domains = set()
    for inst in instances.values():
        if inst.matrix_type:
            required_domains.add(f"m-{inst.domain}")

    if not required_domains:
        return

    # Check the appropriate docker-compose file
    compose_file = SCRIPT_DIR / f"docker-compose.{matrix_type.value}.yml"
    if not compose_file.exists():
        return

    # Parse the YAML file
    with compose_file.open() as f:
        compose_data = yaml.safe_load(f)

    # Get the service name (tuwunel or synapse)
    service_name = matrix_type.value

    # Check if the service exists and has extra_hosts
    if not compose_data.get("services", {}).get(service_name):
        console.print(f"[yellow]⚠️  Warning:[/yellow] Service '{service_name}' not found in {compose_file.name}")
        return

    service_config = compose_data["services"][service_name]
    extra_hosts = service_config.get("extra_hosts", [])

    # Parse existing extra_hosts entries
    existing_domains = set()
    for entry in extra_hosts:
        if ":" in entry:
            domain, _ = entry.split(":", 1)
            existing_domains.add(domain.strip('"').strip("'"))

    # Find missing domains
    missing_domains = required_domains - existing_domains

    if not extra_hosts:
        console.print("[yellow]⚠️  Federation Warning:[/yellow] Missing extra_hosts configuration")
        console.print(f"[dim]Add the following to {compose_file.name} under the {service_name} service:[/dim]")
        console.print("\n    extra_hosts:")
        for domain in sorted(required_domains):
            console.print(f'      - "{domain}:172.20.0.1"')
        console.print(
            "\n[dim]To find your gateway IP: docker network inspect mynetwork | jq '.[0].IPAM.Config[0].Gateway'[/dim]",
        )
        raise typer.Exit(1)

    if missing_domains:
        console.print(f"[yellow]⚠️  Federation Warning:[/yellow] Missing domains in extra_hosts for {compose_file.name}")
        console.print("[dim]Add these entries to the extra_hosts section:[/dim]")
        for domain in sorted(missing_domains):
            console.print(f'      - "{domain}:172.20.0.1"')
        console.print("\n[dim]This is required for local federation to work properly.[/dim]")
        raise typer.Exit(1)


def _create_wellknown_files(name: str, instance: Instance) -> None:
    """Create well-known files for Matrix federation."""
    matrix_server_name = f"m-{instance.domain}"

    # Server well-known (for federation discovery)
    server_wellknown = {"m.server": f"{matrix_server_name}:443"}
    with (SCRIPT_DIR / f"well-known-{name}.json").open("w") as wf:
        json.dump(server_wellknown, wf, indent=2)

    # Client well-known (for client discovery)
    client_wellknown = {
        "m.homeserver": {
            "base_url": f"https://{matrix_server_name}",
        },
    }
    with (SCRIPT_DIR / f"well-known-client-{name}.json").open("w") as wf:
        json.dump(client_wellknown, wf, indent=2)


def _setup_synapse_config(instance: Instance) -> None:
    """Set up Synapse configuration directory and files."""
    synapse_dir = Path(instance.data_dir) / "synapse"
    synapse_dir.mkdir(parents=True, exist_ok=True)
    (synapse_dir / "media_store").mkdir(parents=True, exist_ok=True)

    template_dir = SCRIPT_DIR / "synapse-template"
    _prepare_matrix_config(
        instance,
        MatrixType.SYNAPSE,
        "homeserver.yaml",
        template_dir,
        synapse_dir,
    )


def _update_registry(registry: Registry, instance: Instance, matrix_type: MatrixType | None) -> None:
    """Update the registry with the new instance."""
    registry.instances[instance.name] = instance
    registry.allocated_ports.backend.append(instance.backend_port)
    registry.allocated_ports.frontend.append(instance.frontend_port)
    if matrix_type and instance.matrix_port:
        registry.allocated_ports.matrix.append(instance.matrix_port)
    save_registry(registry)


def _print_instance_info(instance: Instance, matrix_type: MatrixType | None) -> None:
    """Print information about the created instance."""
    console.print(f"[green]✓[/green] Created instance '[cyan]{instance.name}[/cyan]'")
    console.print(f"  [dim]Backend port:[/dim] {instance.backend_port}")
    console.print(f"  [dim]Frontend port:[/dim] {instance.frontend_port}")
    if matrix_type:
        console.print(f"  [dim]Matrix port:[/dim] {instance.matrix_port}")
    console.print(f"  [dim]Data dir:[/dim] {instance.data_dir}")
    console.print(f"  [dim]Domain:[/dim] {instance.domain}")
    console.print(f"  [dim]Env file:[/dim] .env.{instance.name}")
    if matrix_type:
        matrix_name = "Tuwunel (lightweight)" if matrix_type == MatrixType.TUWUNEL else "Synapse (full)"
        console.print(f"  [dim]Matrix:[/dim] [green]{matrix_name}[/green]")


@app.command()
def create(
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

    # Allocate ports and create instance
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

    # Create environment file
    _create_environment_file(instance, name, matrix_type)

    # Create well-known files for Matrix federation
    if matrix_type:
        _create_wellknown_files(name, instance)

    # Set up Synapse configuration if needed
    if matrix_type == MatrixType.SYNAPSE:
        _setup_synapse_config(instance)

    # Update registry
    _update_registry(registry, instance, matrix_type)

    # Print instance information
    _print_instance_info(instance, matrix_type)


@app.command()
def start(  # noqa: PLR0912, PLR0915
    name: str = typer.Argument("default", help="Instance name to start"),
    only_matrix: bool = typer.Option(False, "--only-matrix", help="Start only Matrix server without backend/frontend"),
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

    # Verify federation configuration if Matrix is enabled
    instance = registry.instances[name]
    if instance.matrix_type:
        _verify_extra_hosts_for_federation(instance.matrix_type, registry.instances)

    # Create data directories with proper permissions
    # Use UID/GID 1000 for Docker containers (standard non-root user)
    docker_uid, docker_gid = 1000, 1000

    for subdir in ["config", "tmp", "logs", "mindroom", "mindroom/credentials", "mem0"]:
        dir_path = Path(f"{instance.data_dir}/{subdir}")
        dir_path.mkdir(parents=True, exist_ok=True)
        # Ensure proper ownership and permissions for Docker containers
        with contextlib.suppress(OSError, PermissionError):
            os.chown(dir_path, docker_uid, docker_gid)
            dir_path.chmod(0o755)

    # Create Matrix data directories if enabled
    matrix_type = instance.matrix_type
    if matrix_type == MatrixType.TUWUNEL:
        tuwunel_dir = Path(f"{instance.data_dir}/tuwunel")

        # Check if Tuwunel database exists and might have wrong server name
        # If the .env file has a different MATRIX_SERVER_NAME than what's in the database,
        # we need to clear the database
        if tuwunel_dir.exists() and any(tuwunel_dir.iterdir()):
            # Read current server name from env file
            current_server_name = None
            with env_file.open() as f:
                for line in f:
                    if line.startswith("MATRIX_SERVER_NAME="):
                        current_server_name = line.split("=", 1)[1].strip()
                        break

            # If server name changed or we're having issues, clear the database
            # This is safe because Tuwunel will recreate it
            if current_server_name and current_server_name != instance.domain:
                console.print("[yellow]i[/yellow] Clearing Tuwunel database due to server name change")
                shutil.rmtree(tuwunel_dir)
                tuwunel_dir.mkdir(parents=True, exist_ok=True)
        else:
            tuwunel_dir.mkdir(parents=True, exist_ok=True)

        with contextlib.suppress(OSError, PermissionError):
            os.chown(tuwunel_dir, docker_uid, docker_gid)
            tuwunel_dir.chmod(0o755)
    elif matrix_type == MatrixType.SYNAPSE:
        for matrix_dir in ["synapse", "synapse/media_store", "postgres", "redis"]:
            dir_path = Path(f"{instance.data_dir}/{matrix_dir}")
            dir_path.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError, PermissionError):
                os.chown(dir_path, docker_uid, docker_gid)
                dir_path.chmod(0o755)

        # Copy Synapse config template if needed
        synapse_dir = Path(f"{instance.data_dir}/synapse")
        if not (synapse_dir / "homeserver.yaml").exists():
            template_dir = SCRIPT_DIR / "synapse-template"
            _prepare_matrix_config(
                instance,
                MatrixType.SYNAPSE,
                "homeserver.yaml",
                template_dir,
                synapse_dir,
            )

    # Start with docker compose (modern syntax) - run from parent directory for build context
    # Get the parent directory (project root)
    project_root = SCRIPT_DIR.parent
    env_file_relative = f"deploy/.env.{name}"

    # Build the docker compose command using helper
    compose_files = _get_docker_compose_files(instance, env_file_relative, project_root)

    # Determine which services to start based on flags
    if only_matrix:
        if not matrix_type:
            console.print(f"[red]✗[/red] Instance '{name}' has no Matrix server configured!")
            raise typer.Exit(1)
        services = _get_matrix_services(matrix_type).strip()
        status_msg = f"Starting Matrix server for '{name}'..."
        console.print("[yellow]ℹ[/yellow] Starting only Matrix server (no backend/frontend)")  # noqa: RUF001
    else:
        # Start full stack: frontend + backend + matrix
        services = "frontend backend"
        services += _get_matrix_services(matrix_type)
        status_msg = f"Starting instance '{name}'..."

    cmd = f"{compose_files} -p {name} up -d --build {services}"

    with console.status(f"[yellow]{status_msg}[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    # If Matrix is enabled, also start the well-known service for federation
    if result.returncode == 0 and matrix_type in [MatrixType.TUWUNEL, MatrixType.SYNAPSE]:
        wellknown_cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.wellknown.yml -p {name} up -d"
        with console.status(f"[yellow]Starting federation well-known service for '{name}'...[/yellow]"):
            wellknown_result = subprocess.run(wellknown_cmd, check=False, shell=True, capture_output=True, text=True)
            if wellknown_result.returncode != 0:
                console.print("[yellow]ℹ[/yellow] Well-known service start skipped (optional for federation)")  # noqa: RUF001

    if result.returncode == 0:
        # Update status to reflect what's actually running
        if not only_matrix:
            registry.instances[name].status = InstanceStatus.RUNNING
            save_registry(registry)

        if only_matrix:
            console.print(f"[green]✓[/green] Matrix server for '[cyan]{name}[/cyan]' started successfully!")
            matrix_url = f"https://m-{instance.domain}"
            console.print(f"  [dim]Matrix:[/dim] {matrix_url}")
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
def restart(  # noqa: PLR0912, PLR0915
    name: str = typer.Argument("default", help="Instance name to restart"),
    only_matrix: bool = typer.Option(
        False,
        "--only-matrix",
        help="Restart only Matrix server without backend/frontend",
    ),
) -> None:
    """Restart a Mindroom instance (stop and start)."""
    registry = load_registry()
    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    instance = registry.instances[name]

    # Verify federation configuration if Matrix is enabled
    if instance.matrix_type:
        _verify_extra_hosts_for_federation(instance.matrix_type, registry.instances)

    console.print(f"[yellow]Restarting instance '[cyan]{name}[/cyan]'...[/yellow]")

    # Stop the instance
    project_root = SCRIPT_DIR.parent
    stop_cmd = f"cd {project_root} && docker compose -p {name} down"

    with console.status(f"[yellow]Stopping instance '{name}'...[/yellow]"):
        result = subprocess.run(stop_cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode != 0:
        console.print(f"[red]✗[/red] Failed to stop instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)

    # Start the instance
    env_file = SCRIPT_DIR / f".env.{name}"
    if not env_file.exists():
        console.print(f"[red]✗[/red] Environment file not found: {env_file}")
        raise typer.Exit(1)

    # Get matrix type from registry
    matrix_type = instance.matrix_type

    # Build compose command
    env_file_relative = f"deploy/.env.{name}"
    compose_files = _get_docker_compose_files(instance, env_file_relative, project_root)

    # Determine which services to restart based on flags
    if only_matrix:
        if not matrix_type:
            console.print(f"[red]✗[/red] Instance '{name}' has no Matrix server configured!")
            raise typer.Exit(1)
        services = _get_matrix_services(matrix_type).strip()
    else:
        # Start full stack: frontend + backend + matrix
        services = "frontend backend"
        services += _get_matrix_services(matrix_type)

    start_cmd = f"{compose_files} -p {name} up -d --build {services}"

    with console.status(f"[yellow]Starting instance '{name}'...[/yellow]"):
        result = subprocess.run(start_cmd, check=False, shell=True, capture_output=True, text=True)

    # If Matrix is enabled, also restart the well-known service for federation
    if result.returncode == 0 and matrix_type in [MatrixType.TUWUNEL, MatrixType.SYNAPSE]:
        wellknown_cmd = f"cd {project_root} && docker compose --env-file {env_file_relative} -f deploy/docker-compose.wellknown.yml -p {name} up -d"
        subprocess.run(wellknown_cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        # Update status
        if not only_matrix:
            registry.instances[name].status = InstanceStatus.RUNNING
            save_registry(registry)

        if only_matrix:
            console.print(f"[green]✓[/green] Matrix server for '[cyan]{name}[/cyan]' restarted successfully!")
        else:
            console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' restarted successfully!")
    else:
        console.print(f"[red]✗[/red] Failed to start instance '{name}'")
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

        # Remove well-known files if they exist (for Matrix federation)
        wellknown_server = SCRIPT_DIR / f"well-known-{name}.json"
        if wellknown_server.exists():
            wellknown_server.unlink()
        wellknown_client = SCRIPT_DIR / f"well-known-client-{name}.json"
        if wellknown_client.exists():
            wellknown_client.unlink()

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
