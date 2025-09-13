#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["typer", "rich", "pydantic", "pyyaml", "httpx", "python-dotenv", "jinja2", "matty"]
# ///
"""Matrix Bridge Manager for Mindroom instances."""
# ruff: noqa: S602  # subprocess with shell=True needed for docker compose
# ruff: noqa: C901  # complexity is acceptable for CLI commands
# ruff: noqa: B008  # typer.Argument in defaults is the standard pattern
# ruff: noqa: PLR0912  # complexity is acceptable for CLI commands

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

import matty
import typer
import yaml
from dotenv import load_dotenv
from jinja2 import Template
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Matrix Bridge Manager - Manage bridges for Mindroom instances",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()

# Get the script's directory to ensure paths are relative to it
SCRIPT_DIR = Path(__file__).parent.absolute()
BRIDGES_DIR = SCRIPT_DIR / "templates" / "bridges"
BRIDGE_REGISTRY_FILE = SCRIPT_DIR / "bridge_instances.json"
INSTANCES_FILE = SCRIPT_DIR / "instances.json"  # From deploy.py

# Load environment variables from .env files
load_dotenv(SCRIPT_DIR / ".env.slack")
load_dotenv(SCRIPT_DIR / ".env.telegram")


# Bridge types and their configurations
class BridgeType(str, Enum):
    """Supported bridge types."""

    TELEGRAM = "telegram"
    SLACK = "slack"
    EMAIL = "email"


class BridgeStatus(str, Enum):
    """Bridge status enum."""

    CONFIGURED = "configured"
    REGISTERED = "registered"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class BridgeConfig(BaseModel):
    """Bridge configuration model."""

    bridge_type: BridgeType
    instance_name: str
    port: int
    status: BridgeStatus = BridgeStatus.CONFIGURED
    data_dir: str
    config_file: str | None = None
    registration_file: str | None = None
    matrix_server: str | None = None
    matrix_domain: str | None = None

    # Bridge-specific credentials (stored separately from config)
    credentials: dict[str, Any] = Field(default_factory=dict)


class BridgeDefaults(BaseModel):
    """Default configuration for bridge types."""

    telegram_port_start: int = 29317
    slack_port_start: int = 29400
    email_port_start: int = 29500


class BridgeRegistry(BaseModel):
    """Complete bridge registry model."""

    bridges: dict[str, list[BridgeConfig]] = Field(default_factory=dict)  # instance_name -> bridges
    allocated_ports: dict[BridgeType, list[int]] = Field(default_factory=dict)
    defaults: BridgeDefaults = Field(default_factory=BridgeDefaults)


# Bridge template configurations
BRIDGE_TEMPLATES = {
    BridgeType.TELEGRAM: {
        "image": "dock.mau.dev/mautrix/telegram:latest",
        "config_template": "telegram-config-template.yaml",
        "env_vars": ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"],
        "required_credentials": ["api_id", "api_hash", "bot_token"],
    },
    BridgeType.SLACK: {
        "image": "dock.mau.dev/mautrix/slack:latest",
        "config_template": "slack-config-template.yaml",
        "env_vars": ["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        "required_credentials": ["app_token", "bot_token", "team_id"],
    },
    BridgeType.EMAIL: {
        "image": "etkecc/postmoogle:latest",
        "config_template": "email-config-template.yaml",
        "env_vars": ["EMAIL_DOMAIN", "SMTP_HOST", "SMTP_PORT"],
        "required_credentials": ["domain", "smtp_host", "smtp_port"],
    },
}


def load_registry() -> BridgeRegistry:
    """Load the bridge registry."""
    if not BRIDGE_REGISTRY_FILE.exists():
        return BridgeRegistry()

    try:
        with BRIDGE_REGISTRY_FILE.open() as f:
            data = json.load(f)
            return BridgeRegistry(**data)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        console.print(f"[yellow]Warning: Could not load bridge registry: {e}[/yellow]")
        return BridgeRegistry()


def save_registry(registry: BridgeRegistry) -> None:
    """Save the bridge registry."""
    with BRIDGE_REGISTRY_FILE.open("w") as f:
        data = registry.model_dump(mode="json")
        json.dump(data, f, indent=2)


def load_instances() -> dict[str, Any]:
    """Load Mindroom instances from deploy.py's registry."""
    if not INSTANCES_FILE.exists():
        console.print("[red]✗[/red] No Mindroom instances found. Run './deploy.py create' first.")
        raise typer.Exit(1)

    try:
        with INSTANCES_FILE.open() as f:
            data = json.load(f)
            return data.get("instances", {})
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[red]✗[/red] Could not load instances: {e}")
        raise typer.Exit(1) from e


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use on the system."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("", port))
        except OSError:
            return True
        else:
            return False


def _find_next_port(bridge_type: BridgeType, registry: BridgeRegistry) -> int:
    """Find the next available port for a bridge type."""
    defaults = registry.defaults
    allocated = registry.allocated_ports.get(bridge_type, [])

    # Determine starting port based on bridge type
    if bridge_type == BridgeType.TELEGRAM:
        port = defaults.telegram_port_start
    elif bridge_type == BridgeType.SLACK:
        port = defaults.slack_port_start
    else:  # EMAIL
        port = defaults.email_port_start

    # Find available port
    while port in allocated or _is_port_in_use(port):
        port += 1
        if port > 30000:  # Safety check
            msg = f"Could not find available port for {bridge_type}"
            raise RuntimeError(msg)

    return port


def _get_matrix_info(instance_name: str) -> tuple[str, str, str]:
    """Get Matrix server information for an instance."""
    instances = load_instances()

    if instance_name not in instances:
        console.print(f"[red]✗[/red] Instance '{instance_name}' not found!")
        raise typer.Exit(1)

    instance = instances[instance_name]

    # Check if Matrix is enabled
    if not instance.get("matrix_type"):
        console.print(f"[red]✗[/red] Instance '{instance_name}' has no Matrix server configured!")
        console.print("[yellow]Tip:[/yellow] Create an instance with Matrix: './deploy.py create --matrix tuwunel'")
        raise typer.Exit(1)

    matrix_type = instance["matrix_type"]
    matrix_domain = f"m-{instance['domain']}"

    # Determine Matrix server URL based on type - use container names for internal communication
    if matrix_type == "tuwunel":
        # Use container name for internal Docker communication
        matrix_url = f"http://{instance_name}-tuwunel:6167"
    else:  # synapse
        matrix_url = f"http://{instance_name}-synapse:8008"

    return matrix_type, matrix_domain, matrix_url


def _create_bridge_docker_compose(bridge: BridgeConfig, bridge_template: dict[str, Any]) -> Path:  # noqa: ARG001
    """Create a docker-compose file for a bridge using Jinja2 template."""
    compose_file = Path(bridge.data_dir) / "docker-compose.yml"

    # Load the appropriate Jinja2 template
    template_file = BRIDGES_DIR / f"docker-compose.{bridge.bridge_type.value}.j2"

    if not template_file.exists():
        console.print(f"[red]✗[/red] Template {template_file} not found for bridge type {bridge.bridge_type.value}")
        raise typer.Exit(1)

    # Read and render the template
    with template_file.open() as f:
        template = Template(f.read())

    # Prepare variables for template
    template_vars = {
        "instance_name": bridge.instance_name,
        "port": bridge.port,
        "data_dir": bridge.data_dir,
    }

    # Add bridge-specific variables
    if bridge.bridge_type == BridgeType.EMAIL:
        template_vars.update(
            {
                "smtp_port": bridge.port,
                "imap_port": bridge.port + 1,
                "email_domain": bridge.credentials.get("domain", "example.com"),
                "smtp_host": bridge.credentials.get("smtp_host", "smtp.example.com"),
            },
        )

    # Render the template
    compose_content = template.render(**template_vars)

    # Write to file
    with compose_file.open("w") as f:
        f.write(compose_content)

    return compose_file


def _generate_bridge_config(bridge: BridgeConfig, template_path: Path) -> Path:  # noqa: ARG001
    """Generate bridge configuration from template."""
    config_file = Path(bridge.data_dir) / "data" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # For now, use the existing bridge config as a template
    if bridge.bridge_type == BridgeType.TELEGRAM:
        # Copy from existing telegram bridge if it exists
        existing_config = BRIDGES_DIR / "telegram" / "data" / "config.yaml"
        if existing_config.exists():
            with existing_config.open() as f:
                config_data = yaml.safe_load(f)
        else:
            # Create minimal config
            config_data = {
                "homeserver": {
                    "address": bridge.matrix_server,
                    "domain": bridge.matrix_domain,
                },
                "appservice": {
                    "database": "sqlite:////data/mautrix-telegram.db",  # Absolute path in container
                    "address": "http://0.0.0.0:29317",
                },
                "telegram": {
                    "api_id": bridge.credentials.get("api_id"),
                    "api_hash": bridge.credentials.get("api_hash"),
                    "bot_token": bridge.credentials.get("bot_token"),
                },
                "bridge": {
                    "permissions": {
                        "*": "relaybot",
                        bridge.matrix_domain: "user",
                        f"@admin:{bridge.matrix_domain}": "admin",
                    },
                },
            }
    elif bridge.bridge_type == BridgeType.SLACK:
        # Create Slack bridge config
        config_data = {
            "homeserver": {
                "address": bridge.matrix_server,
                "domain": bridge.matrix_domain,
            },
            "appservice": {
                "database": "sqlite:////data/mautrix-slack.db",  # Absolute path in container
                "address": "http://0.0.0.0:29317",
            },
            "slack": {
                "app_token": bridge.credentials.get("app_token"),
                "bot_token": bridge.credentials.get("bot_token"),
                "team_id": bridge.credentials.get("team_id"),
            },
            "bridge": {
                "permissions": {
                    "*": "relaybot",
                    bridge.matrix_domain: "user",
                    f"@admin:{bridge.matrix_domain}": "admin",
                },
                "username_template": "slack_{{.}}",
                "displayname_template": "{{.RealName}} (Slack)",
            },
        }
    else:
        # Placeholder for other bridge types
        config_data = {
            "homeserver": {
                "address": bridge.matrix_server,
                "domain": bridge.matrix_domain,
            },
        }

    # Update with instance-specific values
    if "homeserver" in config_data:
        config_data["homeserver"]["address"] = bridge.matrix_server
        config_data["homeserver"]["domain"] = bridge.matrix_domain

    # Add credentials
    if bridge.bridge_type == BridgeType.TELEGRAM and "telegram" in config_data:
        config_data["telegram"]["api_id"] = int(bridge.credentials.get("api_id", 0))
        config_data["telegram"]["api_hash"] = bridge.credentials.get("api_hash", "")
        config_data["telegram"]["bot_token"] = bridge.credentials.get("bot_token", "")
    elif bridge.bridge_type == BridgeType.SLACK and "slack" in config_data:
        config_data["slack"]["app_token"] = bridge.credentials.get("app_token", "")
        config_data["slack"]["bot_token"] = bridge.credentials.get("bot_token", "")
        config_data["slack"]["team_id"] = bridge.credentials.get("team_id", "")

    with config_file.open("w") as f:
        yaml.dump(config_data, f, default_flow_style=False)

    bridge.config_file = str(config_file)
    return config_file


def _register_with_tuwunel(bridge: BridgeConfig, registration_yaml: str) -> bool:  # noqa: ARG001
    """Register bridge with Tuwunel/Conduit via admin room API."""
    console.print("[yellow]i[/yellow] Tuwunel registration requires manual steps:")
    console.print(f"1. Join the admin room: #admins:{bridge.matrix_domain}")
    console.print("2. Send: !admin appservices register")
    console.print("3. Paste the registration.yaml content")
    console.print("\n[dim]Registration content saved to:[/dim]")
    console.print(f"  {bridge.registration_file}")

    # Note about bot user creation
    console.print("\n[yellow]Note:[/yellow] After registration, you may need to manually create the bot user.")
    console.print("The bridge will attempt this automatically when started.")

    console.print("\n[yellow]After registration, run:[/yellow]")
    console.print(f"  ./bridge.py start {bridge.bridge_type} --instance {bridge.instance_name}")
    return True


def _register_with_synapse(bridge: BridgeConfig, registration_file: Path) -> bool:  # noqa: ARG001
    """Register bridge with Synapse by updating homeserver.yaml."""
    instances = load_instances()
    instance = instances[bridge.instance_name]

    # Find synapse config
    synapse_config = Path(instance["data_dir"]) / "synapse" / "homeserver.yaml"

    if not synapse_config.exists():
        console.print(f"[red]✗[/red] Synapse config not found: {synapse_config}")
        return False

    # Add registration file to app_service_config_files
    with synapse_config.open() as f:
        config = yaml.safe_load(f)

    if "app_service_config_files" not in config:
        config["app_service_config_files"] = []

    # Add registration file path (relative to synapse data dir)
    reg_path = f"/data/bridges/{bridge.bridge_type}/registration.yaml"
    if reg_path not in config["app_service_config_files"]:
        config["app_service_config_files"].append(reg_path)

    with synapse_config.open("w") as f:
        yaml.dump(config, f, default_flow_style=False)

    console.print("[green]✓[/green] Added to Synapse configuration")
    console.print("[yellow]i[/yellow] Restart Synapse to apply: './deploy.py restart --only-matrix'")
    return True


@app.command()
def add(  # noqa: PLR0915
    bridge_type: BridgeType = typer.Argument(..., help="Bridge type to add"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    api_id: str | None = typer.Option(None, "--api-id", help="Telegram API ID"),
    api_hash: str | None = typer.Option(None, "--api-hash", help="Telegram API Hash"),
    bot_token: str | None = typer.Option(None, "--bot-token", help="Telegram/Slack Bot Token"),
    app_token: str | None = typer.Option(None, "--app-token", help="Slack App Token"),
    team_id: str | None = typer.Option(None, "--team-id", help="Slack Team/Workspace ID"),
) -> None:
    """Add and configure a bridge for a Mindroom instance."""
    registry = load_registry()

    # Check if instance exists and has Matrix
    matrix_type, matrix_domain, matrix_url = _get_matrix_info(instance)

    # Check if bridge already exists
    instance_bridges = registry.bridges.get(instance, [])
    for bridge in instance_bridges:
        if bridge.bridge_type == bridge_type:
            console.print(f"[yellow]⚠[/yellow] {bridge_type} bridge already exists for instance '{instance}'")
            raise typer.Exit(1)

    # Get bridge template
    if bridge_type not in BRIDGE_TEMPLATES:
        console.print(f"[red]✗[/red] Bridge type '{bridge_type}' not yet supported")
        raise typer.Exit(1)

    template = BRIDGE_TEMPLATES[bridge_type]

    # Collect credentials
    credentials = {}
    if bridge_type == BridgeType.TELEGRAM:
        # Try to load from environment (dotenv already loaded above)
        if not api_id:
            api_id = os.environ.get("TELEGRAM_API_ID")
        if not api_hash:
            api_hash = os.environ.get("TELEGRAM_API_HASH")
        if not bot_token:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

        if all([api_id, api_hash, bot_token]):
            console.print("[green]✓[/green] Using Telegram credentials from .env.telegram")

        if not all([api_id, api_hash, bot_token]):
            console.print("[yellow]i[/yellow] Telegram bridge requires:")
            console.print("  1. API ID and Hash from https://my.telegram.org")
            console.print("  2. Bot token from @BotFather in Telegram")
            console.print("\n[cyan]Provide credentials:[/cyan]")

            if not api_id:
                api_id = typer.prompt("API ID")
            if not api_hash:
                api_hash = typer.prompt("API Hash")
            if not bot_token:
                bot_token = typer.prompt("Bot Token")

        credentials = {
            "api_id": api_id,
            "api_hash": api_hash,
            "bot_token": bot_token,
        }
    elif bridge_type == BridgeType.SLACK:
        # Try to load from environment (dotenv already loaded above)
        if not app_token:
            app_token = os.environ.get("SLACK_APP_TOKEN")
        if not bot_token:
            bot_token = os.environ.get("SLACK_BOT_TOKEN")
        if not team_id:
            team_id = os.environ.get("SLACK_TEAM_ID")

        if all([app_token, bot_token, team_id]):
            console.print("[green]✓[/green] Using Slack credentials from .env.slack")

        if not all([app_token, bot_token, team_id]):
            console.print("[yellow]i[/yellow] Slack bridge requires:")
            console.print("  1. Create a Slack App at https://api.slack.com/apps")
            console.print("  2. Enable Socket Mode and get App-Level Token (xapp-...)")
            console.print("  3. Install app to workspace and get Bot User OAuth Token (xoxb-...)")
            console.print("  4. Get your Team/Workspace ID from the URL (T...)")
            console.print("\n[cyan]Provide credentials:[/cyan]")

            if not app_token:
                app_token = typer.prompt("App Token (xapp-...)")
            if not bot_token:
                bot_token = typer.prompt("Bot Token (xoxb-...)")
            if not team_id:
                team_id = typer.prompt("Team ID (T...)")

        credentials = {
            "app_token": app_token,
            "bot_token": bot_token,
            "team_id": team_id,
        }

    # Allocate port
    port = _find_next_port(bridge_type, registry)

    # Create bridge configuration
    instances = load_instances()
    instance_data = instances[instance]
    base_data_dir = Path(instance_data["data_dir"])
    bridge_data_dir = base_data_dir / "bridges" / bridge_type.value

    bridge = BridgeConfig(
        bridge_type=bridge_type,
        instance_name=instance,
        port=port,
        data_dir=str(bridge_data_dir),
        matrix_server=matrix_url,
        matrix_domain=matrix_domain,
        credentials=credentials,
    )

    # Create directories
    bridge_data_dir.mkdir(parents=True, exist_ok=True)
    data_dir = bridge_data_dir / "data"
    data_dir.mkdir(exist_ok=True)

    # Note: Permissions are handled by Docker container

    # Generate configuration
    _generate_bridge_config(bridge, Path())  # Template path not used yet

    # Create docker-compose file
    _create_bridge_docker_compose(bridge, template)

    # Update registry
    if instance not in registry.bridges:
        registry.bridges[instance] = []
    registry.bridges[instance].append(bridge)

    if bridge_type not in registry.allocated_ports:
        registry.allocated_ports[bridge_type] = []
    registry.allocated_ports[bridge_type].append(port)

    save_registry(registry)

    console.print(f"[green]✓[/green] Added {bridge_type} bridge for instance '[cyan]{instance}[/cyan]'")
    console.print(f"  [dim]Port:[/dim] {port}")
    console.print(f"  [dim]Data:[/dim] {bridge_data_dir}")
    console.print(f"  [dim]Matrix:[/dim] {matrix_domain} ({matrix_type})")
    console.print("\n[yellow]Next step:[/yellow] Register the bridge")
    console.print(f"  ./bridge.py register {bridge_type} --instance {instance}")


@app.command()
def register(
    bridge_type: BridgeType = typer.Argument(..., help="Bridge type to register"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
) -> None:
    """Generate registration and register bridge with Matrix server."""
    registry = load_registry()

    # Find bridge
    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridge = None
    for b in registry.bridges[instance]:
        if b.bridge_type == bridge_type:
            bridge = b
            break

    if not bridge:
        console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
        raise typer.Exit(1)

    # Check Matrix server type
    matrix_type, _, _ = _get_matrix_info(instance)

    console.print(f"[yellow]Generating registration for {bridge_type} bridge...[/yellow]")

    # Start bridge temporarily to generate registration
    with console.status("[yellow]Starting bridge to generate registration...[/yellow]"):
        # Ensure networks exist
        subprocess.run(f"docker network create {instance}_mindroom-network 2>/dev/null", shell=True, check=False)
        subprocess.run("docker network create mynetwork 2>/dev/null", shell=True, check=False)

        # Start bridge
        cmd = f"cd {bridge.data_dir} && docker compose up"
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Wait for registration.yaml to be created
        registration_file = Path(bridge.data_dir) / "data" / "registration.yaml"
        max_wait = 30
        for _ in range(max_wait):
            if registration_file.exists():
                console.print("[green]✓[/green] Registration file generated")
                break
            time.sleep(1)
        else:
            console.print("[red]✗[/red] Registration file not generated after 30 seconds")
            process.terminate()
            raise typer.Exit(1)

        # Stop the bridge
        process.terminate()
        subprocess.run(f"cd {bridge.data_dir} && docker compose down", shell=True, check=False)

    # Update registration with correct URL (use container name for internal communication)
    with registration_file.open() as f:
        reg_data = yaml.safe_load(f)

    # Use the bridge container name for internal Docker network communication
    bridge_container = f"{bridge.instance_name}-{bridge.bridge_type.value}-bridge"
    reg_data["url"] = f"http://{bridge_container}:29317"

    with registration_file.open("w") as f:
        yaml.dump(reg_data, f, default_flow_style=False)

    bridge.registration_file = str(registration_file)

    # Register based on Matrix type
    if matrix_type == "tuwunel":
        success = _register_with_tuwunel(bridge, registration_file.read_text())
    else:  # synapse
        success = _register_with_synapse(bridge, registration_file)

    if success:
        bridge.status = BridgeStatus.REGISTERED
        save_registry(registry)
        console.print(f"[green]✓[/green] Bridge registration prepared for {matrix_type}")


@app.command("register-with-matrix")
def register_with_matrix(  # noqa: PLR0915
    bridge_type: BridgeType = typer.Argument(..., help="Bridge type to register"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    dry: bool = typer.Option(False, "--dry", help="Just print the command without executing"),
) -> None:
    """Send registration to Matrix admin room using matty (for Tuwunel/Conduit)."""
    registry = load_registry()

    # Find bridge
    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridge = None
    for b in registry.bridges[instance]:
        if b.bridge_type == bridge_type:
            bridge = b
            break

    if not bridge:
        console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
        raise typer.Exit(1)

    # Check if registration file exists
    registration_file = Path(bridge.data_dir) / "data" / "registration.yaml"
    if not registration_file.exists():
        console.print("[red]✗[/red] Registration file not found. Run 'register' command first.")
        raise typer.Exit(1)

    # Check Matrix server type
    matrix_type, _, _ = _get_matrix_info(instance)
    if matrix_type != "tuwunel":
        console.print(f"[yellow]![/yellow] This command is for Tuwunel/Conduit. Use 'register' for {matrix_type}.")
        raise typer.Exit(1)

    # Get admin room using the standard alias format
    admin_room = f"#admins:{bridge.matrix_domain}"

    # Read registration content
    registration_content = registration_file.read_text()

    # Build the message with proper formatting
    message = f"!admin appservices register\n```\n{registration_content}```"

    # Get Matrix credentials from matrix_state.yaml
    instances = load_instances()
    instance_data = instances[instance]
    matrix_state_file = Path(instance_data["data_dir"]) / "mindroom_data" / "matrix_state.yaml"

    username = None
    password = None
    if matrix_state_file.exists():
        with matrix_state_file.open() as f:
            state_data = yaml.safe_load(f)
            if state_data and "accounts" in state_data and "agent_user" in state_data["accounts"]:
                user_account = state_data["accounts"]["agent_user"]
                username = user_account["username"]
                password = user_account["password"]
                # Set environment for matty
                os.environ["MATRIX_USERNAME"] = username
                os.environ["MATRIX_PASSWORD"] = password
                os.environ["MATRIX_HOMESERVER"] = f"https://{bridge.matrix_domain}"

    if not username or not password:
        console.print("[red]✗[/red] Could not find Matrix credentials in matrix_state.yaml")
        raise typer.Exit(1)

    if dry:
        # In dry run mode, just show what would be sent
        console.print("[yellow]Dry run - Would send to Matrix:[/yellow]\n")
        console.print(f"Room: {admin_room}")
        console.print(f"Username: {username}")
        console.print(f"Homeserver: https://{bridge.matrix_domain}")
        console.print("\nMessage content:")
        console.print("!admin appservices register")
        console.print("```")
        console.print(registration_content)
        console.print("```")
        return

    # Send registration using matty module
    console.print(f"[yellow]Sending registration to {admin_room}...[/yellow]")

    try:
        # Use matty's internal send function (async)
        asyncio.run(
            matty._execute_send_command(
                room=admin_room,
                message=message,
                username=username,
                password=password,
                no_mentions=True,
            ),
        )
        console.print(f"[green]✓[/green] Registration sent to {admin_room}")

        # Update status
        bridge.status = BridgeStatus.REGISTERED
        save_registry(registry)

        # Verify registration
        console.print("\n[yellow]Verifying registration...[/yellow]")
        time.sleep(2)  # Give server time to process

        # Send verification command
        asyncio.run(
            matty._execute_send_command(
                room=admin_room,
                message="!admin appservices list",
                username=username,
                password=password,
                no_mentions=True,
            ),
        )

        console.print("[green]✓[/green] Registration complete! You can now start the bridge:")
        console.print(f"  ./bridge.py start {bridge_type} --instance {instance}")

    except Exception as e:
        console.print(f"[red]✗[/red] Failed to send registration: {e}")
        console.print("\nTry manually with:")
        console.print(f'  matty send "{admin_room}" "!admin appservices register"')
        console.print(f"  Then paste the contents of: {registration_file}")
        raise typer.Exit(1)  # noqa: B904


@app.command()
def start(
    bridge_type: BridgeType | None = typer.Argument(None, help="Bridge type to start (or use --all)"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    all_bridges: bool = typer.Option(False, "--all", help="Start all bridges for instance"),
) -> None:
    """Start bridge(s) for a Mindroom instance."""
    registry = load_registry()

    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridges_to_start = []

    if all_bridges:
        bridges_to_start = registry.bridges[instance]
    else:
        if not bridge_type:
            console.print("[red]✗[/red] Specify bridge type or use --all")
            raise typer.Exit(1)

        for b in registry.bridges[instance]:
            if b.bridge_type == bridge_type:
                bridges_to_start.append(b)
                break
        else:
            console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
            raise typer.Exit(1)

    for bridge in bridges_to_start:
        with console.status(f"[yellow]Starting {bridge.bridge_type} bridge...[/yellow]"):
            cmd = f"cd {bridge.data_dir} && docker compose up -d"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

            if result.returncode == 0:
                bridge.status = BridgeStatus.RUNNING
                console.print(f"[green]✓[/green] Started {bridge.bridge_type} bridge on port {bridge.port}")
            else:
                bridge.status = BridgeStatus.ERROR
                console.print(f"[red]✗[/red] Failed to start {bridge.bridge_type} bridge")
                if result.stderr:
                    console.print(f"[dim]{result.stderr}[/dim]")

    save_registry(registry)


@app.command()
def stop(
    bridge_type: BridgeType | None = typer.Argument(None, help="Bridge type to stop (or use --all)"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    all_bridges: bool = typer.Option(False, "--all", help="Stop all bridges for instance"),
) -> None:
    """Stop bridge(s) for a Mindroom instance."""
    registry = load_registry()

    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridges_to_stop = []

    if all_bridges:
        bridges_to_stop = registry.bridges[instance]
    else:
        if not bridge_type:
            console.print("[red]✗[/red] Specify bridge type or use --all")
            raise typer.Exit(1)

        for b in registry.bridges[instance]:
            if b.bridge_type == bridge_type:
                bridges_to_stop.append(b)
                break
        else:
            console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
            raise typer.Exit(1)

    for bridge in bridges_to_stop:
        with console.status(f"[yellow]Stopping {bridge.bridge_type} bridge...[/yellow]"):
            cmd = f"cd {bridge.data_dir} && docker compose down"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

            if result.returncode == 0:
                bridge.status = BridgeStatus.STOPPED
                console.print(f"[green]✓[/green] Stopped {bridge.bridge_type} bridge")
            else:
                console.print(f"[red]✗[/red] Failed to stop {bridge.bridge_type} bridge")

    save_registry(registry)


@app.command()
def status(
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
) -> None:
    """Show status of bridges for an instance."""
    registry = load_registry()

    if instance not in registry.bridges:
        console.print(f"[yellow]No bridges configured for instance '{instance}'[/yellow]")
        return

    bridges = registry.bridges[instance]

    table = Table(title=f"Bridges for instance '{instance}'", show_header=True, header_style="bold magenta")
    table.add_column("Type", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Port", justify="right")
    table.add_column("Registration")
    table.add_column("Data Directory")

    for bridge in bridges:
        # Check actual Docker status
        cmd = f"cd {bridge.data_dir} && docker compose ps --format json 2>/dev/null"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

        docker_running = False
        if result.returncode == 0 and result.stdout:
            try:
                for line in result.stdout.strip().split("\n"):
                    if line:
                        data = json.loads(line)
                        if data.get("State") == "running":
                            docker_running = True
                            break
            except json.JSONDecodeError:
                pass

        # Determine status display
        if docker_running:
            status_display = "[green]● running[/green]"
        elif bridge.status == BridgeStatus.REGISTERED:
            status_display = "[yellow]● registered[/yellow]"
        elif bridge.status == BridgeStatus.CONFIGURED:
            status_display = "[blue]● configured[/blue]"
        elif bridge.status == BridgeStatus.ERROR:
            status_display = "[red]● error[/red]"
        else:
            status_display = "[gray]● stopped[/gray]"

        reg_status = "✓" if bridge.registration_file and Path(bridge.registration_file).exists() else "✗"

        table.add_row(
            bridge.bridge_type.value,
            status_display,
            str(bridge.port),
            reg_status,
            bridge.data_dir,
        )

    console.print(table)


@app.command("list")
def list_bridges() -> None:
    """List all configured bridges across all instances."""
    registry = load_registry()

    if not registry.bridges:
        console.print("[yellow]No bridges configured.[/yellow]")
        console.print("\n[dim]Add your first bridge with:[/dim]")
        console.print("  [cyan]./bridge.py add telegram[/cyan]")
        return

    table = Table(title="All Matrix Bridges", show_header=True, header_style="bold magenta")
    table.add_column("Instance", style="cyan", no_wrap=True)
    table.add_column("Bridge", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Port", justify="right")
    table.add_column("Matrix Server")

    for instance_name, bridges in registry.bridges.items():
        for bridge in bridges:
            # Check actual Docker status
            cmd = f"cd {bridge.data_dir} && docker compose ps --format json 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

            docker_running = False
            if result.returncode == 0 and result.stdout:
                try:
                    for line in result.stdout.strip().split("\n"):
                        if line:
                            data = json.loads(line)
                            if data.get("State") == "running":
                                docker_running = True
                                break
                except json.JSONDecodeError:
                    pass

            if docker_running:
                status_display = "[green]● running[/green]"
            elif bridge.status == BridgeStatus.REGISTERED:
                status_display = "[yellow]● registered[/yellow]"
            elif bridge.status == BridgeStatus.CONFIGURED:
                status_display = "[blue]● configured[/blue]"
            else:
                status_display = "[gray]● stopped[/gray]"

            table.add_row(
                instance_name,
                bridge.bridge_type.value,
                status_display,
                str(bridge.port),
                bridge.matrix_domain or "N/A",
            )

    console.print(table)


@app.command()
def logs(
    bridge_type: BridgeType = typer.Argument(..., help="Bridge type to show logs for"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: int = typer.Option(100, "--tail", "-n", help="Number of lines to show"),
) -> None:
    """Show logs for a bridge."""
    registry = load_registry()

    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridge = None
    for b in registry.bridges[instance]:
        if b.bridge_type == bridge_type:
            bridge = b
            break

    if not bridge:
        console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
        raise typer.Exit(1)

    cmd = f"cd {bridge.data_dir} && docker compose logs"
    if follow:
        cmd += " -f"
    if tail:
        cmd += f" --tail {tail}"

    subprocess.run(cmd, shell=True, check=False)


@app.command()
def remove(
    bridge_type: BridgeType | None = typer.Argument(None, help="Bridge type to remove"),
    instance: str = typer.Option("default", "--instance", "-i", help="Mindroom instance name"),
    all_bridges: bool = typer.Option(False, "--all", help="Remove all bridges for instance"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Remove bridge(s) and their data."""
    registry = load_registry()

    if instance not in registry.bridges:
        console.print(f"[red]✗[/red] No bridges configured for instance '{instance}'")
        raise typer.Exit(1)

    bridges_to_remove = []

    if all_bridges:
        bridges_to_remove = registry.bridges[instance].copy()
    else:
        if not bridge_type:
            console.print("[red]✗[/red] Specify bridge type or use --all")
            raise typer.Exit(1)

        for b in registry.bridges[instance]:
            if b.bridge_type == bridge_type:
                bridges_to_remove.append(b)
                break
        else:
            console.print(f"[red]✗[/red] {bridge_type} bridge not found for instance '{instance}'")
            raise typer.Exit(1)

    # Confirmation
    if not force:
        console.print(
            f"[yellow]⚠️  Warning:[/yellow] This will remove {len(bridges_to_remove)} bridge(s) and their data",
        )
        for bridge in bridges_to_remove:
            console.print(f"  - {bridge.bridge_type}: {bridge.data_dir}")

        if not typer.confirm("Continue?"):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    # Remove bridges
    for bridge in bridges_to_remove:
        with console.status(f"[yellow]Removing {bridge.bridge_type} bridge...[/yellow]"):
            # Stop containers
            subprocess.run(f"cd {bridge.data_dir} && docker compose down -v 2>/dev/null", shell=True, check=False)

            # Remove data directory
            if Path(bridge.data_dir).exists():
                shutil.rmtree(bridge.data_dir)

            # Remove from registry
            registry.bridges[instance].remove(bridge)
            if bridge.port in registry.allocated_ports.get(bridge.bridge_type, []):
                registry.allocated_ports[bridge.bridge_type].remove(bridge.port)

            console.print(f"[green]✓[/green] Removed {bridge.bridge_type} bridge")

    # Clean up empty entries
    if not registry.bridges[instance]:
        del registry.bridges[instance]

    save_registry(registry)


if __name__ == "__main__":
    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    app()
