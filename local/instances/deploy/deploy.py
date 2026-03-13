#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich", "pydantic", "jinja2"]
# ///
"""Docker MindRoom instance manager."""
# ruff: noqa: S602  # subprocess with shell=True needed for docker compose
# ruff: noqa: C901  # complexity is acceptable for CLI commands

import base64
import contextlib
import json
import os
import platform as plat
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import typer
from jinja2 import Template
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="MindRoom Instance Manager - Simple multi-instance deployment",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

# Get the script's directory to ensure paths are relative to it
SCRIPT_DIR = Path(__file__).parent.absolute()
REPO_ROOT = SCRIPT_DIR.parents[2]
REGISTRY_FILE = SCRIPT_DIR / "instances.json"
ENV_DIR = SCRIPT_DIR / "envs"
ENV_TEMPLATE = SCRIPT_DIR / ".env.template"
DEFAULT_REGISTRY = "ghcr.io/mindroom-ai"
EXTERNAL_NETWORK = "mynetwork"
DEFAULT_TRAEFIK_WEB_ENTRYPOINT = "websecure"
DEFAULT_TRAEFIK_MATRIX_ENTRYPOINT = "matrix-fed"
DEFAULT_TRAEFIK_CERTRESOLVER = "porkbun"


# Pydantic Models
class InstanceStatus(str, Enum):
    """Instance status enum."""

    CREATED = "created"
    RUNNING = "running"
    PARTIAL = "partial"  # Only Matrix server running
    STOPPED = "stopped"


class MatrixType(str, Enum):
    """Matrix server type enum."""

    TUWUNEL = "tuwunel"
    SYNAPSE = "synapse"


class AuthType(str, Enum):
    """Authentication type enum."""

    AUTHELIA = "authelia"


class Instance(BaseModel):
    """Instance configuration model."""

    name: str
    mindroom_port: int
    matrix_port: int | None = None
    data_dir: str
    domain: str
    status: InstanceStatus = InstanceStatus.CREATED
    matrix_type: MatrixType | None = None
    auth_type: AuthType | None = None


class AllocatedPorts(BaseModel):
    """Allocated ports tracking model."""

    mindroom: list[int] = Field(default_factory=list)
    matrix: list[int] = Field(default_factory=list)


class RegistryDefaults(BaseModel):
    """Registry defaults configuration."""

    mindroom_port_start: int = 8765
    matrix_port_start: int = 8448
    data_dir_base: str = Field(default_factory=lambda: str(SCRIPT_DIR / "instance_data"))


class Registry(BaseModel):
    """Complete registry model."""

    instances: dict[str, Instance] = Field(default_factory=dict)
    allocated_ports: AllocatedPorts = Field(default_factory=AllocatedPorts)
    defaults: RegistryDefaults = Field(default_factory=RegistryDefaults)


@dataclass(frozen=True)
class TraefikSettings:
    """Traefik labels that the instance publishes."""

    web_entrypoint: str = DEFAULT_TRAEFIK_WEB_ENTRYPOINT
    matrix_entrypoint: str = DEFAULT_TRAEFIK_MATRIX_ENTRYPOINT
    certresolver: str = DEFAULT_TRAEFIK_CERTRESOLVER


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


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use on the system."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("", port))
        except OSError:
            return True
        else:
            return False


def _find_available_port(start_port: int, allocated_ports: list[int], port_type: str) -> int:
    """Find next available port starting from start_port."""
    port = start_port
    skipped = []

    while port in allocated_ports or _is_port_in_use(port):
        if _is_port_in_use(port) and port not in allocated_ports:
            skipped.append(port)
        port += 1
        # Safety check to avoid infinite loop
        if port > start_port + 1000:
            msg = f"Could not find available {port_type} port starting from {start_port}"
            raise RuntimeError(msg)

    if skipped:
        console.print(
            f"[yellow]Note:[/yellow] {port_type.capitalize()} port(s) {skipped} already in use by other processes, using port {port}",
        )

    return port


def _find_next_ports(registry: Registry) -> tuple[int, int]:
    """Find the next available ports that are not in use on the system."""
    defaults = registry.defaults
    allocated = registry.allocated_ports

    mindroom_port = _find_available_port(defaults.mindroom_port_start, allocated.mindroom, "MindRoom")
    matrix_port = _find_available_port(defaults.matrix_port_start, allocated.matrix, "matrix")

    return mindroom_port, matrix_port


def _prepare_matrix_config(
    instance: Instance,
    matrix_type: MatrixType,
    config_file_name: str,
    template_dir: Path,
    target_dir: Path,
) -> None:
    """Prepare Matrix configuration files using Jinja2 templates."""
    if not template_dir.exists():
        return

    matrix_server_name = f"m-{instance.domain}"

    # Look for Jinja2 template
    template_file = template_dir / f"{config_file_name}.j2"
    if template_file.exists():
        template_content = template_file.read_text()
        template = Template(template_content)

        # Render template with variables
        if matrix_type == MatrixType.SYNAPSE:
            content = template.render(
                matrix_server_name=matrix_server_name,
                postgres_host=f"{instance.name}-postgres",
                redis_host=f"{instance.name}-redis",
                macaroon_secret_key=secrets.token_hex(32),
            )
        else:
            # For Tuwunel or other matrix types
            content = template.render(
                matrix_server_name=matrix_server_name,
            )

        with (target_dir / config_file_name).open("w") as f:
            f.write(content)

    # Copy other files (like signing.key, log.config, etc.)
    for file in template_dir.glob("*"):
        if not file.is_file() or file.suffix == ".j2":
            continue

        if file.name == config_file_name:
            # Skip - already handled by template
            continue

        if file.name == "signing.key" and matrix_type == MatrixType.SYNAPSE:
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


def _ensure_env_dir() -> None:
    """Create the env directory lazily when generated files are needed."""
    ENV_DIR.mkdir(parents=True, exist_ok=True)


def _get_docker_compose_files(instance: Instance, name: str, project_root: Path) -> str:
    """Get the docker-compose command with appropriate files based on matrix and auth type."""
    env_file = ENV_DIR / f"{name}.env"
    compose_files = [SCRIPT_DIR / "docker-compose.yml"]

    # Add Matrix server compose file if configured
    if instance.matrix_type == MatrixType.TUWUNEL:
        compose_files.extend(
            [
                SCRIPT_DIR / "docker-compose.tuwunel.yml",
                SCRIPT_DIR / "docker-compose.wellknown.yml",
            ],
        )
    elif instance.matrix_type == MatrixType.SYNAPSE:
        compose_files.extend(
            [
                SCRIPT_DIR / "docker-compose.synapse.yml",
                SCRIPT_DIR / "docker-compose.wellknown.yml",
            ],
        )
    matrix_host_override = _matrix_host_override_path(name)
    if matrix_host_override.exists():
        compose_files.append(matrix_host_override)

    # Add Authelia compose file if configured
    if instance.auth_type == AuthType.AUTHELIA:
        compose_files.append(SCRIPT_DIR / "docker-compose.authelia.yml")

    compose_args = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    return (
        f"cd {shlex.quote(str(project_root))} && docker compose --env-file {shlex.quote(str(env_file))} {compose_args}"
    )


def _get_docker_compose_down_command(name: str, *, remove_volumes: bool = False) -> str:
    """Return a teardown command that still works when config files are missing."""
    volume_flag = " -v" if remove_volumes else ""
    return f"docker compose -p {shlex.quote(name)} down{volume_flag}"


def _ensure_instance_env_file_reference(env_file: Path) -> None:
    """Ensure the per-instance env file can also be mounted into the container."""
    reference = f"INSTANCE_ENV_FILE={env_file}"
    if not env_file.exists():
        return

    content = env_file.read_text()
    if "INSTANCE_ENV_FILE=" in content:
        return

    suffix = "" if not content or content.endswith("\n") else "\n"
    with env_file.open("a") as f:
        f.write(f"{suffix}{reference}\n")


def _load_traefik_settings(env_file: Path) -> TraefikSettings:
    """Read optional Traefik label overrides from the instance env file."""
    values: dict[str, str] = {}
    if env_file.exists():
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")

    return TraefikSettings(
        web_entrypoint=values.get("TRAEFIK_WEB_ENTRYPOINT", DEFAULT_TRAEFIK_WEB_ENTRYPOINT),
        matrix_entrypoint=values.get("TRAEFIK_MATRIX_ENTRYPOINT", DEFAULT_TRAEFIK_MATRIX_ENTRYPOINT),
        certresolver=values.get("TRAEFIK_CERTRESOLVER", DEFAULT_TRAEFIK_CERTRESOLVER),
    )


def _matrix_host_override_path(name: str) -> Path:
    """Return the generated compose override that adds peer Matrix host mappings."""
    return ENV_DIR / f"{name}.matrix-hosts.yml"


def _matrix_peer_domains(instances: dict[str, Instance], *, exclude_name: str) -> list[str]:
    """Return peer Matrix domains that should resolve to the host gateway."""
    return sorted(
        {
            f"m-{instance.domain}"
            for name, instance in instances.items()
            if instance.matrix_type is not None and name != exclude_name
        },
    )


def _write_matrix_host_override(instance: Instance, instances: dict[str, Instance]) -> None:
    """Write a compose override that maps peer Matrix domains to the host gateway."""
    override_path = _matrix_host_override_path(instance.name)
    if instance.matrix_type is None:
        override_path.unlink(missing_ok=True)
        return

    peer_domains = _matrix_peer_domains(instances, exclude_name=instance.name)
    if not peer_domains:
        override_path.unlink(missing_ok=True)
        return

    _ensure_env_dir()
    service_name = instance.matrix_type.value
    lines = [
        "services:",
        f"  {service_name}:",
        "    extra_hosts:",
    ]
    lines.extend(f'      - "{domain}:host-gateway"' for domain in peer_domains)
    override_path.write_text("\n".join(lines) + "\n")


def _sync_matrix_host_overrides(instances: dict[str, Instance]) -> None:
    """Regenerate peer host-mapping overrides for every managed Matrix instance."""
    for instance in instances.values():
        _write_matrix_host_override(instance, instances)


def _running_matrix_peer_names(instances: dict[str, Instance], *, exclude_name: str) -> list[str]:
    """Return currently running Matrix peers excluding the named instance."""
    running_peers = []
    for name, instance in instances.items():
        if name == exclude_name or instance.matrix_type is None:
            continue
        _mindroom_running, matrix_running = get_actual_status(name)
        if matrix_running:
            running_peers.append(name)
    return sorted(running_peers)


def _print_matrix_restart_hint(peer_names: list[str]) -> None:
    """Tell the user which running Matrix peers still need a manual restart."""
    if not peer_names:
        return

    peers = ", ".join(peer_names)
    console.print(
        "[yellow]i[/yellow] Restart running Matrix peers to pick up the new federation host mappings:"
        f" [cyan]{peers}[/cyan]",
    )
    console.print("  [dim]Use ./deploy.py restart --only-matrix <name> when convenient.[/dim]")


def _get_services_to_start(instance: Instance, only_matrix: bool = False) -> str:
    """Get the list of services to start based on instance configuration."""
    if only_matrix:
        if not instance.matrix_type:
            msg = f"Instance '{instance.name}' has no Matrix server configured!"
            raise ValueError(msg)
        return _get_matrix_services(instance.matrix_type).strip()

    # Start full stack: MindRoom + matrix + auth
    services = ["mindroom"]

    if instance.matrix_type == MatrixType.SYNAPSE:
        services.extend(["postgres", "redis", "synapse", "wellknown"])
    elif instance.matrix_type == MatrixType.TUWUNEL:
        services.extend(["tuwunel", "wellknown"])

    if instance.auth_type == AuthType.AUTHELIA:
        services.append("authelia")

    return " ".join(services)


def _get_matrix_services(matrix_type: MatrixType | None) -> str:
    """Get the list of services to start based on matrix type."""
    if matrix_type == MatrixType.SYNAPSE:
        return " postgres redis synapse wellknown"
    if matrix_type == MatrixType.TUWUNEL:
        return " tuwunel wellknown"
    return ""


def _get_auth_services(auth_type: AuthType | None) -> str:
    """Get the list of services to start based on auth type."""
    if auth_type == AuthType.AUTHELIA:
        return " authelia"  # Redis removed - using in-memory sessions
    return ""


def _pull_images_from_registry(registry_url: str, console: Console) -> None:
    """Pull images from registry and tag them locally.

    Args:
        registry_url: Registry URL to pull from
        console: Rich console for output

    Raises:
        typer.Exit: If pulling fails

    """
    console.print(f"[blue]🐳[/blue] Pulling images from {registry_url}...")
    # Detect platform
    arch = "arm64" if plat.machine() == "aarch64" else "amd64"

    images = [
        (f"{registry_url}/mindroom:{arch}", "deploy-mindroom:latest"),
    ]
    for source, target in images:
        pull_cmd = f"docker pull {source} && docker tag {source} {target}"
        console.print(f"  Pulling {source.split('/')[-1]}...")
        result = subprocess.run(pull_cmd, check=False, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]✗[/red] Failed to pull {source}")
            console.print(result.stderr)
            raise typer.Exit(1)


def _create_environment_file(instance: Instance, name: str, matrix_type: MatrixType | None) -> None:
    """Create and configure the environment file for an instance."""
    _ensure_env_dir()
    env_file = ENV_DIR / f"{name}.env"
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
        f.write(f"INSTANCE_ENV_FILE={env_file}\n")
        f.write(f"INSTANCE_NAME={name}\n")
        f.write(f"MINDROOM_PORT={instance.mindroom_port}\n")
        f.write(f"DATA_DIR={abs_data_dir}\n")
        f.write(f"INSTANCE_DOMAIN={instance.domain}\n")

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


def _ensure_external_network(name: str) -> bool:
    """Create an external Docker network when the stack expects it."""
    inspect_cmd = f"docker network inspect {shlex.quote(name)}"
    result = subprocess.run(inspect_cmd, shell=True, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return False

    create_cmd = f"docker network create {shlex.quote(name)}"
    result = subprocess.run(create_cmd, shell=True, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        console.print(f"[red]✗[/red] Failed to create Docker network '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)
    console.print(f"  [dim]Created external Docker network '{name}'[/dim]")
    return True


def _traefik_proxy_names(network_name: str) -> list[str]:
    """Return running Traefik container names attached to the external network."""
    cmd = f"docker ps --filter network={shlex.quote(network_name)} --format '{{{{.Image}}}}\t{{{{.Names}}}}'"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    proxies = set()
    for line in result.stdout.strip().splitlines():
        image, _, name = line.partition("\t")
        if "traefik" in image.lower() or "traefik" in name.lower():
            proxies.add(name or image)
    return sorted(proxies)


def _auth_url(instance: Instance) -> str:
    """Return the external Authelia URL for an instance domain."""
    domain_parts = instance.domain.split(".")
    if len(domain_parts) >= 2 and domain_parts[-1] != "localhost":
        root_domain = ".".join(domain_parts[-2:])
        subdomain = domain_parts[0]
        return f"https://auth-{subdomain}.{root_domain}"
    return f"https://auth-{instance.domain}"


def _print_traefik_label_requirements(instance: Instance, settings: TraefikSettings) -> None:
    """Print the Traefik settings that must match this instance's labels."""
    requirements = [f"web={settings.web_entrypoint}", f"resolver={settings.certresolver}"]
    if instance.matrix_type is not None:
        requirements.append(f"matrix={settings.matrix_entrypoint}")

    console.print(
        f"  [dim]Match Traefik entrypoints/certresolver to envs/{instance.name}.env: {', '.join(requirements)}[/dim]",
    )


def _print_missing_traefik_warning(
    instance: Instance,
    settings: TraefikSettings,
    *,
    only_matrix: bool,
) -> None:
    """Explain that only direct localhost access is available without Traefik."""
    blocked_features = ["HTTPS domain routes"]
    if not only_matrix and instance.auth_type is not None:
        blocked_features.append("Authelia")
    if instance.matrix_type is not None:
        blocked_features.append("Matrix .well-known routes and domain-based federation")

    console.print(f"[yellow]i[/yellow] No Traefik container detected on Docker network '{EXTERNAL_NETWORK}'.")
    console.print("  [dim]Use the localhost ports above for direct access.[/dim]")
    console.print(
        f"  [dim]{', '.join(blocked_features)} remain unavailable until Traefik joins that network.[/dim]",
    )
    _print_traefik_label_requirements(instance, settings)
    console.print(
        f"  [dim]Attach Traefik with: docker network connect {EXTERNAL_NETWORK} <traefik-container>[/dim]",
    )


def _print_running_instance_access(
    instance: Instance,
    *,
    only_matrix: bool,
    traefik_proxies: list[str],
    traefik_settings: TraefikSettings,
) -> None:
    """Print the endpoints that are actually usable for the running instance."""
    if only_matrix:
        console.print(f"  [dim]Matrix local:[/dim] http://localhost:{instance.matrix_port}")
    else:
        console.print(f"  [dim]MindRoom local:[/dim] http://localhost:{instance.mindroom_port}")
        if instance.matrix_type is not None:
            console.print(f"  [dim]Matrix local:[/dim] http://localhost:{instance.matrix_port}")

    if not traefik_proxies:
        _print_missing_traefik_warning(instance, traefik_settings, only_matrix=only_matrix)
        return

    console.print(f"  [dim]Traefik detected:[/dim] {', '.join(traefik_proxies)} on {EXTERNAL_NETWORK}")
    console.print(
        "  [dim]HTTPS/domain routes below are published through Traefik labels and only work after the proxy"
        " matches this instance's entrypoint and certresolver names.[/dim]",
    )
    _print_traefik_label_requirements(instance, traefik_settings)
    if not only_matrix:
        console.print(f"  [dim]Configured MindRoom domain:[/dim] https://{instance.domain}")
    if instance.matrix_type is not None:
        console.print(f"  [dim]Configured Matrix domain:[/dim] https://m-{instance.domain}")
    if not only_matrix and instance.auth_type is not None:
        console.print(f"  [dim]Configured Auth URL:[/dim] {_auth_url(instance)}")


def _create_directory_with_permissions(path: Path, uid: int = 1000, gid: int = 1000) -> None:
    """Create a directory with proper ownership and permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError, PermissionError):
        os.chown(path, uid, gid)
        path.chmod(0o755)


def _copy_credentials_to_instance(instance: Instance) -> None:
    """Copy credentials from ~/.mindroom/credentials to instance data directory."""
    source_dir = Path.home() / ".mindroom" / "credentials"
    if not source_dir.exists():
        return

    target_dir = Path(instance.data_dir) / "mindroom_data" / "credentials"

    # Copy all credential files
    for cred_file in source_dir.glob("*.json"):
        target_file = target_dir / cred_file.name
        if not target_file.exists():
            shutil.copy2(cred_file, target_file)
            # Set proper permissions for Docker
            with contextlib.suppress(OSError, PermissionError):
                os.chown(target_file, 1000, 1000)
                target_file.chmod(0o644)


def _copy_config_to_instance(instance: Instance) -> None:
    """Copy config.yaml from the main mindroom directory to instance config directory."""
    source_config = REPO_ROOT / "config.yaml"
    if not source_config.exists():
        console.print("[yellow]Warning:[/yellow] config.yaml not found in mindroom directory")
        return

    target_config = Path(instance.data_dir) / "config" / "config.yaml"

    # Only copy if target doesn't exist (preserve customizations)
    if not target_config.exists():
        shutil.copy2(source_config, target_config)
        # Set proper permissions for Docker
        with contextlib.suppress(OSError, PermissionError):
            os.chown(target_config, 1000, 1000)
            target_config.chmod(0o644)
        console.print("[green]✓[/green] Copied config.yaml to instance")


def _create_instance_directories(instance: Instance) -> None:
    """Create all necessary directories for an instance with proper permissions."""
    # Base directories needed by all instances
    base_dirs = [
        "config",
        "mindroom_data",
        "mindroom_data/sessions",
        "mindroom_data/learning",
        "mindroom_data/tracking",
        "mindroom_data/memory",  # For mem0/chroma vector DB
        "mindroom_data/credentials",
        "logs",
    ]

    for subdir in base_dirs:
        dir_path = Path(f"{instance.data_dir}/{subdir}")
        _create_directory_with_permissions(dir_path)

    # Copy credentials from ~/.mindroom/credentials if they exist
    _copy_credentials_to_instance(instance)

    # Copy config.yaml to instance
    _copy_config_to_instance(instance)


def _create_synapse_directories(instance: Instance) -> None:
    """Create directories needed for Synapse."""
    for matrix_dir in ["synapse", "synapse/media_store", "postgres", "redis"]:
        dir_path = Path(f"{instance.data_dir}/{matrix_dir}")
        _create_directory_with_permissions(dir_path)


def _setup_tuwunel_directory(instance: Instance, env_file: Path) -> None:
    """Set up Tuwunel directory, clearing if server name changed."""
    tuwunel_dir = Path(f"{instance.data_dir}/tuwunel")

    # Check if Tuwunel database exists and might have wrong server name
    if tuwunel_dir.exists() and any(tuwunel_dir.iterdir()):
        # Read current server name from env file
        current_server_name = None
        with env_file.open() as f:
            for line in f:
                if line.startswith("MATRIX_SERVER_NAME="):
                    current_server_name = line.split("=", 1)[1].strip()
                    break

        # If server name changed, clear the database (safe - Tuwunel recreates it)
        expected_server_name = f"m-{instance.domain}"
        if current_server_name and current_server_name != expected_server_name:
            console.print("[yellow]i[/yellow] Clearing Tuwunel database due to server name change")
            shutil.rmtree(tuwunel_dir)

    _create_directory_with_permissions(tuwunel_dir)


def _create_wellknown_files(instance: Instance) -> None:
    """Create well-known files for Matrix federation."""
    matrix_server_name = f"m-{instance.domain}"

    # Create well-known directory in instance data
    wellknown_dir = Path(instance.data_dir) / "well-known"
    wellknown_dir.mkdir(parents=True, exist_ok=True)

    # Server well-known (for federation discovery)
    server_wellknown = {"m.server": f"{matrix_server_name}:443"}
    with (wellknown_dir / "server.json").open("w") as wf:
        json.dump(server_wellknown, wf, indent=2)

    # Client well-known (for client discovery)
    client_wellknown = {
        "m.homeserver": {
            "base_url": f"https://{matrix_server_name}",
        },
    }
    with (wellknown_dir / "client.json").open("w") as wf:
        json.dump(client_wellknown, wf, indent=2)


def _setup_synapse_config(instance: Instance) -> None:
    """Set up Synapse configuration directory and files."""
    synapse_dir = Path(instance.data_dir) / "synapse"
    synapse_dir.mkdir(parents=True, exist_ok=True)
    (synapse_dir / "media_store").mkdir(parents=True, exist_ok=True)

    template_dir = SCRIPT_DIR / "templates" / "synapse"
    _prepare_matrix_config(
        instance,
        MatrixType.SYNAPSE,
        "homeserver.yaml",
        template_dir,
        synapse_dir,
    )


def _setup_authelia_config(instance: Instance) -> None:
    """Set up Authelia configuration directory and files."""
    authelia_dir = Path(instance.data_dir) / "authelia"
    authelia_dir.mkdir(parents=True, exist_ok=True)

    # Use Jinja2 template
    jinja_template = SCRIPT_DIR / "templates" / "authelia" / "configuration.yml.j2"
    users_template = SCRIPT_DIR / "templates" / "authelia" / "users_database.yml"

    if jinja_template.exists():
        # Use Jinja2 template
        template_content = jinja_template.read_text()
        template = Template(template_content)

        # Determine domain configuration
        domain_parts = instance.domain.split(".")
        is_production = len(domain_parts) >= 2 and domain_parts[-1] != "localhost"

        if is_production:
            root_domain = ".".join(domain_parts[-2:])  # e.g., "mindroom.chat"
            subdomain = domain_parts[0]  # e.g., "try"
            cookie_domain = f".{root_domain}"  # e.g., ".mindroom.chat"
            auth_domain = f"auth-{subdomain}.{root_domain}"  # e.g., "auth-try.mindroom.chat"
        else:
            root_domain = instance.domain
            cookie_domain = instance.domain
            auth_domain = f"auth-{instance.domain}"

        # Render the template with variables
        config_content = template.render(
            jwt_secret=secrets.token_hex(32),
            session_secret=secrets.token_hex(32),
            encryption_key=secrets.token_hex(32),
            instance_domain=instance.domain,
            root_domain=root_domain,
            cookie_domain=cookie_domain,
            auth_domain=auth_domain,
            is_production=is_production,
        )

        # Write the rendered configuration
        (authelia_dir / "configuration.yml").write_text(config_content)
    else:
        console.print("[red]✗[/red] Authelia configuration template not found!")
        raise typer.Exit(1)

    # Copy users database
    if users_template.exists():
        shutil.copy(users_template, authelia_dir / "users_database.yml")


def _update_registry(registry: Registry, instance: Instance, matrix_type: MatrixType | None) -> None:
    """Update the registry with the new instance."""
    registry.instances[instance.name] = instance
    registry.allocated_ports.mindroom.append(instance.mindroom_port)
    if matrix_type and instance.matrix_port:
        registry.allocated_ports.matrix.append(instance.matrix_port)
    _sync_matrix_host_overrides(registry.instances)
    save_registry(registry)


def _print_instance_info(instance: Instance, matrix_type: MatrixType | None, auth_type: AuthType | None = None) -> None:
    """Print information about the created instance."""
    console.print(f"[green]✓[/green] Created instance '[cyan]{instance.name}[/cyan]'")
    console.print(f"  [dim]MindRoom port:[/dim] {instance.mindroom_port}")
    if matrix_type:
        console.print(f"  [dim]Matrix port:[/dim] {instance.matrix_port}")
    console.print(f"  [dim]Data dir:[/dim] {instance.data_dir}")
    console.print(f"  [dim]Domain:[/dim] {instance.domain}")
    console.print(f"  [dim]Env file:[/dim] envs/{instance.name}.env")
    if matrix_type:
        matrix_name = "Tuwunel (lightweight)" if matrix_type == MatrixType.TUWUNEL else "Synapse (full)"
        console.print(f"  [dim]Matrix:[/dim] [green]{matrix_name}[/green]")
        console.print(
            f"  [dim]Matrix domain:[/dim] https://m-{instance.domain} [yellow](requires Traefik on {EXTERNAL_NETWORK})[/yellow]",
        )
    if auth_type:
        console.print("  [dim]Auth:[/dim] [green]Authelia (production-ready)[/green]")
        console.print("    [yellow]Default login:[/yellow] admin / mindroom")
        console.print(
            f"    [dim]Auth URL:[/dim] {_auth_url(instance)} [yellow](requires Traefik on {EXTERNAL_NETWORK})[/yellow]",
        )


@app.command()
def create(
    name: str = typer.Argument("default", help="Instance name"),
    domain: str | None = typer.Option(None, help="Domain for the instance (default: NAME.localhost)"),
    matrix: str | None = typer.Option(
        None,
        "--matrix",
        help="Include Matrix server: 'tuwunel' (lightweight) or 'synapse' (full)",
    ),
    auth: str | None = typer.Option(
        None,
        "--auth",
        help="Include authentication: 'authelia' (production-ready auth server)",
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

    # Validate and convert auth type to enum
    auth_type: AuthType | None = None
    if auth:
        try:
            auth_type = AuthType(auth)
        except ValueError:
            console.print(f"[red]✗[/red] Invalid auth option '{auth}'. Use 'authelia'")
            raise typer.Exit(1)  # noqa: B904

    # Allocate ports and create instance
    try:
        mindroom_port, matrix_port_value = _find_next_ports(registry)
    except RuntimeError as e:
        console.print(f"[red]✗[/red] Port allocation failed: {e}")
        console.print(
            "[yellow]Tip:[/yellow] Check for other processes using these ports with 'sudo lsof -i' or 'netstat -tulpn'",
        )
        raise typer.Exit(1) from e

    data_dir = f"{registry.defaults.data_dir_base}/{name}"

    instance = Instance(
        name=name,
        mindroom_port=mindroom_port,
        matrix_port=matrix_port_value if matrix_type else None,
        data_dir=data_dir,
        domain=domain or f"{name}.localhost",
        status=InstanceStatus.CREATED,
        matrix_type=matrix_type,
        auth_type=auth_type,
    )

    # Create environment file
    _create_environment_file(instance, name, matrix_type)

    # Create well-known files for Matrix federation
    if matrix_type:
        _create_wellknown_files(instance)

    # Set up Synapse configuration if needed
    if matrix_type == MatrixType.SYNAPSE:
        _setup_synapse_config(instance)

    # Set up Authelia configuration if needed
    if auth_type == AuthType.AUTHELIA:
        _setup_authelia_config(instance)

    # Update registry
    _update_registry(registry, instance, matrix_type)

    # Print instance information
    _print_instance_info(instance, matrix_type, auth_type)


@app.command()
def start(  # noqa: PLR0912, PLR0915
    name: str = typer.Argument("default", help="Instance name to start"),
    only_matrix: bool = typer.Option(
        False,
        "--only-matrix",
        help="Start only Matrix server without the MindRoom runtime",
    ),
    use_registry: bool = typer.Option(False, "--registry", "-r", help="Pull images from registry instead of building"),
    registry_url: str = typer.Option(DEFAULT_REGISTRY, "--registry-url", help="Registry URL to pull from"),
    no_build: bool = typer.Option(False, "--no-build", help="Skip building images (use existing local images)"),
) -> None:
    """Start a MindRoom instance."""
    registry = load_registry()
    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    env_file = ENV_DIR / f"{name}.env"
    if not env_file.exists():
        console.print(f"[red]✗[/red] Environment file {env_file} not found!")
        raise typer.Exit(1)

    instance = registry.instances[name]
    previous_status = instance.status
    _sync_matrix_host_overrides(registry.instances)

    # Create data directories with proper permissions
    _create_instance_directories(instance)

    # Create Matrix data directories if enabled
    if instance.matrix_type == MatrixType.TUWUNEL:
        _setup_tuwunel_directory(instance, env_file)
    elif instance.matrix_type == MatrixType.SYNAPSE:
        _create_synapse_directories(instance)

        # Copy Synapse config template if needed
        synapse_dir = Path(f"{instance.data_dir}/synapse")
        if not (synapse_dir / "homeserver.yaml").exists():
            template_dir = SCRIPT_DIR / "templates" / "synapse"
            _prepare_matrix_config(
                instance,
                MatrixType.SYNAPSE,
                "homeserver.yaml",
                template_dir,
                synapse_dir,
            )

    # Start with docker compose (modern syntax) - run from parent directory for build context
    # Get the parent directory (project root)
    project_root = REPO_ROOT

    # Build the docker compose command using helper
    _ensure_instance_env_file_reference(env_file)
    compose_files = _get_docker_compose_files(instance, name, project_root)

    # Determine which services to start based on flags
    try:
        services = _get_services_to_start(instance, only_matrix)
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from e

    status_msg = f"Starting Matrix server for '{name}'..." if only_matrix else f"Starting instance '{name}'..."
    if only_matrix:
        console.print("[yellow]ℹ[/yellow] Starting only Matrix server (no MindRoom runtime)")  # noqa: RUF001

    # Pull images from registry if requested
    if use_registry:
        _pull_images_from_registry(registry_url, console)
        build_flag = ""
    elif no_build:
        build_flag = ""
    else:
        build_flag = "--build"

    _ensure_external_network(EXTERNAL_NETWORK)
    traefik_proxies = _traefik_proxy_names(EXTERNAL_NETWORK)
    traefik_settings = _load_traefik_settings(env_file)
    cmd = f"{compose_files} -p {name} up -d {build_flag} {services}"

    with console.status(f"[yellow]{status_msg}[/yellow]"):
        result = subprocess.run(cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        # Update status to reflect what's actually running
        if only_matrix:
            registry.instances[name].status = InstanceStatus.PARTIAL
        else:
            registry.instances[name].status = InstanceStatus.RUNNING
        save_registry(registry)

        if only_matrix:
            console.print(f"[green]✓[/green] Matrix server for '[cyan]{name}[/cyan]' started successfully!")
        else:
            console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' started successfully!")
        _print_running_instance_access(
            instance,
            only_matrix=only_matrix,
            traefik_proxies=traefik_proxies,
            traefik_settings=traefik_settings,
        )
        if instance.matrix_type and previous_status == InstanceStatus.CREATED:
            _print_matrix_restart_hint(
                _running_matrix_peer_names(registry.instances, exclude_name=name),
            )
    else:
        console.print(f"[red]✗[/red] Failed to start instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)


@app.command()
def stop(name: str = typer.Argument("default", help="Instance name to stop")) -> None:
    """Stop a running MindRoom instance."""
    registry = load_registry()
    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    cmd = _get_docker_compose_down_command(name)

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
def restart(
    name: str = typer.Argument(None, help="Instance name to restart (or use --all)"),
    all_instances: bool = typer.Option(False, "--all", help="Restart all running instances"),
    only_matrix: bool = typer.Option(
        False,
        "--only-matrix",
        help="Restart only Matrix server without the MindRoom runtime",
    ),
    use_registry: bool = typer.Option(False, "--registry", "-r", help="Pull images from registry instead of building"),
    registry_url: str = typer.Option(DEFAULT_REGISTRY, "--registry-url", help="Registry URL to pull from"),
    no_build: bool = typer.Option(False, "--no-build", help="Skip building images (use existing local images)"),
) -> None:
    """Restart a MindRoom instance (stop and start)."""
    registry = load_registry()

    # Handle --all flag
    if all_instances:
        if name is not None:
            console.print("[red]✗[/red] Cannot specify instance name with --all flag")
            raise typer.Exit(1)

        # Get all running instances (including partial)
        running_instances = [
            n
            for n, inst in registry.instances.items()
            if inst.status in [InstanceStatus.RUNNING, InstanceStatus.PARTIAL]
        ]

        if not running_instances:
            console.print("[yellow]No running instances to restart[/yellow]")
            return

        console.print(f"[cyan]Restarting {len(running_instances)} instances...[/cyan]")

        # Restart each instance
        for instance_name in running_instances:
            instance = registry.instances[instance_name]
            console.print(f"\n[yellow]Restarting '[cyan]{instance_name}[/cyan]'...[/yellow]")
            _restart_instance(instance_name, instance, registry, only_matrix, use_registry, registry_url, no_build)

        console.print("\n[green]✓[/green] All instances restarted successfully!")
        return

    # Single instance restart
    if name is None:
        name = "default"  # Default to "default" if no name specified and not --all

    if name not in registry.instances:
        console.print(f"[red]✗[/red] Instance '{name}' not found!")
        raise typer.Exit(1)

    instance = registry.instances[name]
    console.print(f"[yellow]Restarting instance '[cyan]{name}[/cyan]'...[/yellow]")
    _restart_instance(name, instance, registry, only_matrix, use_registry, registry_url, no_build)


def _restart_instance(  # noqa: PLR0912, PLR0915
    name: str,
    instance: Instance,
    registry: Registry,
    only_matrix: bool,
    use_registry: bool = False,
    registry_url: str = DEFAULT_REGISTRY,
    no_build: bool = False,
) -> None:
    """Helper function to restart a single instance."""
    _sync_matrix_host_overrides(registry.instances)

    # Stop the instance
    stop_cmd = _get_docker_compose_down_command(name)

    with console.status(f"[yellow]Stopping instance '{name}'...[/yellow]"):
        result = subprocess.run(stop_cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode != 0:
        console.print(f"[red]✗[/red] Failed to stop instance '{name}'")
        if result.stderr:
            console.print(f"[dim]{result.stderr}[/dim]")
        raise typer.Exit(1)

    # Start the instance
    env_file = ENV_DIR / f"{name}.env"
    if not env_file.exists():
        console.print(f"[red]✗[/red] Environment file not found: {env_file}")
        raise typer.Exit(1)

    # Build compose command
    project_root = REPO_ROOT
    _ensure_instance_env_file_reference(env_file)
    compose_files = _get_docker_compose_files(instance, name, project_root)

    # Determine which services to restart based on flags
    try:
        services = _get_services_to_start(instance, only_matrix)
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from e

    # Pull images from registry if requested
    if use_registry:
        _pull_images_from_registry(registry_url, console)
        build_flag = ""
    elif no_build:
        build_flag = ""
    else:
        build_flag = "--build"

    _ensure_external_network(EXTERNAL_NETWORK)
    traefik_proxies = _traefik_proxy_names(EXTERNAL_NETWORK)
    traefik_settings = _load_traefik_settings(env_file)
    start_cmd = f"{compose_files} -p {name} up -d {build_flag} {services}"

    with console.status(f"[yellow]Starting instance '{name}'...[/yellow]"):
        result = subprocess.run(start_cmd, check=False, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        # Update status
        if only_matrix:
            registry.instances[name].status = InstanceStatus.PARTIAL
        else:
            registry.instances[name].status = InstanceStatus.RUNNING
        save_registry(registry)

        if only_matrix:
            console.print(f"[green]✓[/green] Matrix server for '[cyan]{name}[/cyan]' restarted successfully!")
        else:
            console.print(f"[green]✓[/green] Instance '[cyan]{name}[/cyan]' restarted successfully!")
        _print_running_instance_access(
            instance,
            only_matrix=only_matrix,
            traefik_proxies=traefik_proxies,
            traefik_settings=traefik_settings,
        )
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
            if registry.instances:
                save_registry(registry)

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
        console.print(f"  - Environment file envs/{name}.env")
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
        stop_cmd = _get_docker_compose_down_command(name, remove_volumes=True)
        result = subprocess.run(stop_cmd, check=False, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]✗[/red] Failed to tear down containers for instance '{name}'")
            if result.stderr:
                console.print(f"[dim]{result.stderr}[/dim]")
            raise typer.Exit(1)

        # Remove data directory
        data_dir = Path(instance.data_dir)
        if data_dir.exists():
            shutil.rmtree(data_dir)

        # Remove env file
        env_file = ENV_DIR / f"{name}.env"
        if env_file.exists():
            env_file.unlink()
        _matrix_host_override_path(name).unlink(missing_ok=True)

        # Update registry - remove instance and free up ports
        del registry.instances[name]

        # Remove allocated ports
        if instance.mindroom_port in registry.allocated_ports.mindroom:
            registry.allocated_ports.mindroom.remove(instance.mindroom_port)
        if instance.matrix_port and instance.matrix_port in registry.allocated_ports.matrix:
            registry.allocated_ports.matrix.remove(instance.matrix_port)

        _sync_matrix_host_overrides(registry.instances)


def get_actual_status(name: str) -> tuple[bool, bool]:
    """Check which containers are actually running.

    Returns: (mindroom_running, matrix_running)
    """
    cmd = (
        "docker ps --filter "
        f"label=com.docker.compose.project={shlex.quote(name)} "
        "--format '{{.Label \"com.docker.compose.service\"}}'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        return False, False

    running_containers = {line.strip() for line in result.stdout.strip().splitlines() if line.strip()}

    mindroom_running = "mindroom" in running_containers
    matrix_running = any(m in running_containers for m in ["synapse", "tuwunel", "postgres", "redis", "wellknown"])

    return mindroom_running, matrix_running


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

    table = Table(title="MindRoom Instances", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("MindRoom", justify="right")
    table.add_column("Matrix", justify="right")
    table.add_column("Domain")
    table.add_column("Data Directory")

    for name, instance in instances.items():
        # Get actual container status
        mindroom_up, matrix_up = get_actual_status(name)

        # Determine status display based on actual running containers
        if not any([mindroom_up, matrix_up]):
            status_display = "[red]● stopped[/red]"
        elif mindroom_up:
            status_display = "[green]● running[/green]"
        elif matrix_up and not mindroom_up:
            # Only Matrix server running (partial mode)
            status_display = "[yellow]● partial[/yellow]"
        else:
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

        # Add auth indicator to domain if configured
        domain_display = instance.domain
        if instance.auth_type == AuthType.AUTHELIA:
            domain_display = f"{instance.domain} 🔒"

        table.add_row(
            name,
            status_display,
            str(instance.mindroom_port),
            matrix_display,
            domain_display,
            instance.data_dir,
        )

    console.print(table)


@app.command()
def pull(
    registry_url: str = typer.Option(DEFAULT_REGISTRY, "--registry-url", help="Registry URL to pull from"),
    tag: str = typer.Option(None, "--tag", "-t", help="Image tag to pull (default: auto-detect platform)"),
) -> None:
    """Pull latest images from registry."""
    # Auto-detect platform if tag not specified
    if tag is None:
        tag = "arm64" if plat.machine() == "aarch64" else "amd64"
        console.print(f"[blue]🔍[/blue] Auto-detected platform: {tag}")

    console.print(f"[blue]🐳[/blue] Pulling images from {registry_url}:{tag}...")

    images = [
        (f"{registry_url}/mindroom:{tag}", "deploy-mindroom:latest"),
    ]

    for source, target in images:
        with console.status(f"Pulling {source.split('/')[-1]}..."):
            pull_cmd = f"docker pull {source}"
            result = subprocess.run(pull_cmd, check=False, shell=True, capture_output=True, text=True)

            if result.returncode == 0:
                # Tag the image
                tag_cmd = f"docker tag {source} {target}"
                subprocess.run(tag_cmd, check=False, shell=True, capture_output=True, text=True)
                console.print(f"[green]✓[/green] Pulled {source.split('/')[-1]}")
            else:
                console.print(f"[red]✗[/red] Failed to pull {source}")
                console.print(f"[dim]{result.stderr}[/dim]")
                raise typer.Exit(1)

    console.print("[green]✓[/green] All images pulled successfully!")


if __name__ == "__main__":
    # If no arguments provided, show help
    if len(sys.argv) == 1:
        # Show help by appending --help to argv
        sys.argv.append("--help")
    app()
