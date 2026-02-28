"""Configuration management CLI subcommands for MindRoom."""

from __future__ import annotations

import logging
import os
import platform
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.syntax import Syntax

from mindroom.config.main import Config
from mindroom.constants import (
    CONFIG_PATH,
    OWNER_MATRIX_USER_ID_PLACEHOLDER,
    config_search_locations,
    env_key_for_provider,
)

console = Console()

config_app = typer.Typer(
    name="config",
    help="Manage MindRoom configuration files.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

# Reusable option definitions
CONFIG_PATH_OPTION: Path | None = typer.Option(
    None,
    "--path",
    "-p",
    help="Override auto-detection and use this config file path.",
)

ProviderPreset = Literal["openai", "openrouter"]

_DEFAULT_MODEL_PRESETS: dict[ProviderPreset, tuple[str, str]] = {
    "openai": ("openai", "gpt-5.2"),
    "openrouter": ("openrouter", "anthropic/claude-sonnet-4-5"),
}

_REQUIRED_ENV_KEYS: dict[ProviderPreset, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}


def _resolve_config_path(path: Path | None) -> Path:
    """Resolve the config file path from explicit argument or default."""
    if path is not None:
        return path.expanduser().resolve()
    return Path(CONFIG_PATH).resolve()


def _get_editor() -> str:
    """Get the user's preferred editor.

    Checks $EDITOR, then $VISUAL, then falls back to platform defaults.
    """
    for env_var in ("EDITOR", "VISUAL"):
        editor = os.environ.get(env_var)
        if editor:
            return editor

    if platform.system() == "Windows":
        return "notepad"

    for editor in ("nano", "vim", "vi"):
        if shutil.which(editor):
            return editor

    return "vi"


def _format_validation_errors(exc: ValidationError, config_path: Path | None = None) -> None:
    """Print Pydantic validation errors in a user-friendly format."""
    if config_path:
        console.print(f"[red]Error:[/red] Invalid configuration in {config_path}\n")
    else:
        console.print("[red]Error:[/red] Invalid configuration\n")
    console.print("Issues found:")
    for error in exc.errors():
        loc = " -> ".join(str(x) for x in error["loc"])
        console.print(f"  [red]*[/red] {loc}: {error['msg']}")
    console.print("\nFix these issues:")
    console.print("  [cyan]mindroom config edit[/cyan]      Edit your config")
    console.print("  [cyan]mindroom config validate[/cyan]  Check config after editing")


@config_app.command("init")
def config_init(
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        "-p",
        help="Where to create the config file (default: auto-detected, usually ~/.mindroom/config.yaml).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing config without prompting.",
    ),
    minimal: bool = typer.Option(
        False,
        "--minimal",
        help="Generate a bare-minimum config instead of a richer example.",
    ),
    profile: str = typer.Option(
        "full",
        "--profile",
        help="Template profile: full, minimal, or public (public keeps full YAML and adjusts .env defaults).",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Provider preset for generated config: openai or openrouter.",
    ),
) -> None:
    """Create a starter config.yaml with example agents and models.

    Generates a YAML config with one agent, one model, and sensible defaults.
    """
    target = _resolve_config_path(path)

    if target.exists() and not force:
        console.print(f"[yellow]Config file already exists:[/yellow] {target}")
        if not typer.confirm("Overwrite existing config file?"):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    selected_profile = "minimal" if minimal else profile.strip().lower()
    valid_profiles = {"full", "minimal", "public"}
    if selected_profile not in valid_profiles:
        msg = f"Invalid profile '{profile}'. Expected one of: {', '.join(sorted(valid_profiles))}"
        raise typer.BadParameter(msg)

    provider_preset = _normalize_provider_preset(provider) if provider else None
    if provider and provider_preset is None:
        console.print("[red]Invalid --provider value.[/red] Use: openai or openrouter.")
        raise typer.Exit(1)

    if selected_profile == "minimal":
        selected_preset: ProviderPreset = provider_preset or "openai"
    elif provider_preset is not None:
        selected_preset = provider_preset
    elif selected_profile == "public":
        selected_preset = "openai"
    else:
        selected_preset = _prompt_provider_preset()

    content = _minimal_template(selected_preset) if selected_profile == "minimal" else _full_template(selected_preset)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    # Also create a .env file next to the config if one doesn't exist
    env_path = target.parent / ".env"
    env_created = False
    if not env_path.exists():
        env_path.write_text(_env_template(selected_profile, selected_preset), encoding="utf-8")
        console.print(f"[green]Env file created:[/green] {env_path}")
        env_created = True

    console.print(f"[green]Config created:[/green] {target}")
    console.print("\nNext steps:")
    if env_created:
        console.print(f"  [cyan]Edit {env_path.name}[/cyan]            Set your API keys and Matrix homeserver")
    console.print("  [cyan]mindroom config edit[/cyan]      Customize your config")
    console.print("  [cyan]mindroom config validate[/cyan]  Verify it's valid")
    console.print("  [cyan]mindroom run[/cyan]              Start the system")


@config_app.command("show")
def config_show(
    path: Path | None = CONFIG_PATH_OPTION,
    raw: bool = typer.Option(
        False,
        "--raw",
        "-r",
        help="Print plain file contents without syntax highlighting.",
    ),
) -> None:
    """Display the current config file with syntax highlighting."""
    config_file = _resolve_config_path(path)

    if not config_file.exists():
        console.print(f"[yellow]No config file found at:[/yellow] {config_file}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        console.print("\nSearch locations (first match wins):")
        for i, loc in enumerate(config_search_locations(), 1):
            status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
            console.print(f"  {i}. {loc} ({status})")
        raise typer.Exit(1)

    content = config_file.read_text(encoding="utf-8")

    if raw:
        print(content, end="")
        return

    console.print(f"[bold green]Config file:[/bold green] {config_file}\n")
    syntax = Syntax(content, "yaml", theme="monokai", line_numbers=True, word_wrap=True)
    console.print(syntax)


@config_app.command("edit")
def config_edit(
    path: Path | None = CONFIG_PATH_OPTION,
) -> None:
    """Open config.yaml in your default editor.

    Editor preference: $EDITOR -> $VISUAL -> nano -> vim -> vi.
    """
    config_file = _resolve_config_path(path)

    if not config_file.exists():
        console.print("[yellow]No config file found.[/yellow]")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one first.")
        raise typer.Exit(1)

    editor = _get_editor()
    console.print(f"[dim]Opening {config_file} with {editor}...[/dim]")

    try:
        editor_cmd = shlex.split(editor, posix=os.name != "nt")
    except ValueError:
        console.print("[red]Invalid editor command. Check $EDITOR/$VISUAL.[/red]")
        raise typer.Exit(1) from None

    if not editor_cmd:
        console.print("[red]Editor command is empty.[/red]")
        raise typer.Exit(1)

    try:
        subprocess.run([*editor_cmd, str(config_file)], check=True)
    except FileNotFoundError:
        console.print(f"[red]Editor '{editor_cmd[0]}' not found.[/red]")
        console.print("Set $EDITOR environment variable to your preferred editor.")
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Editor exited with error code {e.returncode}[/red]")
        raise typer.Exit(e.returncode) from None


@config_app.command("validate")
def config_validate(
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        "-p",
        help="Path to the configuration file to validate.",
    ),
) -> None:
    """Validate config.yaml and check for common issues.

    Parses the YAML config using Pydantic and reports errors in a friendly format.
    Also checks whether required API keys are set as environment variables.
    """
    config_path = _resolve_config_path(path)
    console.print(f"Validating configuration: [bold]{config_path}[/bold]\n")

    if not config_path.exists():
        console.print(f"[red]Error:[/red] Configuration file not found: {config_path}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        raise typer.Exit(1)

    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        _format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None
    except (yaml.YAMLError, OSError) as e:
        console.print(f"[red]Error:[/red] Could not load configuration: {e}")
        raise typer.Exit(1) from None

    console.print("[green]Configuration is valid.[/green]\n")
    console.print(f"  Agents: {len(config.agents)} ({', '.join(config.agents.keys()) or 'none'})")
    console.print(f"  Teams:  {len(config.teams)} ({', '.join(config.teams.keys()) or 'none'})")
    console.print(f"  Models: {len(config.models)} ({', '.join(config.models.keys()) or 'none'})")
    rooms = config.get_all_configured_rooms()
    console.print(f"  Rooms:  {len(rooms)} ({', '.join(sorted(rooms)) or 'none'})")

    # Check for missing API keys based on configured providers
    _check_env_keys(config)


@config_app.command("path")
def config_path_cmd(
    path: Path | None = CONFIG_PATH_OPTION,
) -> None:
    """Show the resolved config file path and search locations."""
    resolved = _resolve_config_path(path)
    exists = resolved.exists()
    status = "[green]exists[/green]" if exists else "[red]not found[/red]"
    console.print(f"Resolved config path: {resolved} ({status})")

    console.print("\nSearch locations (first match wins):")
    for i, loc in enumerate(config_search_locations(), 1):
        loc_status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
        console.print(f"  {i}. {loc} ({loc_status})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config_quiet(path: Path) -> Config:
    """Load config while temporarily suppressing structlog output.

    structlog's default PrintLogger bypasses stdlib log levels, so we
    route it through stdlib with the root level at WARNING for the
    duration of the load then reset so later callers (e.g. the bot)
    can configure structlog themselves.
    """
    import structlog  # noqa: PLC0415

    was_configured = structlog.is_configured()
    if not was_configured:
        logging.basicConfig(format="%(message)s", level=logging.WARNING)
        structlog.configure(
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
    try:
        return Config.from_yaml(path)
    finally:
        if not was_configured:
            structlog.reset_defaults()


def _find_missing_env_keys(config: Config) -> list[tuple[str, str]]:
    """Return (provider, env_key) pairs for configured providers missing env vars."""
    providers_used: set[str] = {model.provider for model in config.models.values()}
    missing: list[tuple[str, str]] = []
    for provider in sorted(providers_used):
        env_key = env_key_for_provider(provider)
        if env_key and not os.getenv(env_key):
            missing.append((provider, env_key))
    return missing


def _check_env_keys(config: Config) -> None:
    """Warn about missing environment variables for configured providers."""
    missing = _find_missing_env_keys(config)
    if missing:
        console.print("\n[yellow]Warning:[/yellow] Missing API key environment variables:\n")
        for provider, env_key in missing:
            console.print(f"  [yellow]*[/yellow] {provider}: Set {env_key}")
        console.print("\nYou can set these in a .env file or export them in your shell.")


def _normalize_provider_preset(provider: str) -> ProviderPreset | None:
    """Normalize provider preset values used by prompts and CLI flags."""
    normalized = provider.strip().lower()
    aliases: dict[str, ProviderPreset] = {
        "openai": "openai",
        "o": "openai",
        "openrouter": "openrouter",
        "or": "openrouter",
        "r": "openrouter",
    }
    return aliases.get(normalized)


def _prompt_provider_preset() -> ProviderPreset:
    """Prompt the user for a starter provider preset."""
    while True:
        raw_value = typer.prompt(
            "Choose provider preset [openai/openrouter]",
            default="openai",
            show_default=True,
        )
        provider_preset = _normalize_provider_preset(raw_value)
        if provider_preset is not None:
            return provider_preset
        console.print("[red]Invalid choice.[/red] Enter openai or openrouter.")


def _full_template(provider_preset: ProviderPreset) -> str:
    """Return a provider-aware starter config."""
    provider, model_id = _DEFAULT_MODEL_PRESETS[provider_preset]
    return f"""\
# MindRoom Configuration
# Generated by: mindroom config init
# Docs: https://docs.mindroom.chat/

models:
  default:
    provider: {provider}
    id: {model_id}

agents:
  assistant:
    display_name: Assistant
    role: A helpful general-purpose assistant
    model: default
    rooms:
      - lobby
    tools: []
    instructions:
      - Be helpful and conversational

router:
  model: default

# Set username before first run; once created, it cannot be changed.
# You can still change display_name later.
mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser

matrix_room_access:
  mode: single_user_private
  multi_user_join_rule: public
  publish_to_room_directory: false
  invite_only_rooms: []
  reconcile_existing_rooms: false

# File-based memory requires no external embedder or LLM.
memory:
  backend: file

authorization:
  default_room_access: false
  global_users:
    # Replace with your Matrix user ID (example: @alice:mindroom.chat).
    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}
  agent_reply_permissions:
    "*":
      # Replace with your Matrix user ID (example: @alice:mindroom.chat).
      - {OWNER_MATRIX_USER_ID_PLACEHOLDER}

defaults:
  tools:
    - scheduler
  markdown: true
"""


def _env_template(profile: str, provider_preset: ProviderPreset) -> str:
    """Return a starter .env file for standalone deployments.

    Generates a random dashboard API key.
    """
    api_key = secrets.token_urlsafe(32)
    if profile == "public":
        matrix_homeserver = "https://mindroom.chat"
        extra_matrix = (
            "# Matrix server_name override (needed when federation hostname differs)\n"
            "MATRIX_SERVER_NAME=mindroom.chat\n\n"
            "# Hosted pairing/provisioning API for `mindroom connect` and token issuance\n"
            "MINDROOM_PROVISIONING_URL=https://mindroom.chat\n\n"
            "# Required for homeservers that gate bot registration (recommended in public mode)\n"
            "# Keep this secret; do not commit real values.\n"
            "MATRIX_REGISTRATION_TOKEN="
        )
    else:
        matrix_homeserver = "https://matrix.example.com"
        extra_matrix = (
            "# Matrix registration token (only needed if your homeserver requires it)\n# MATRIX_REGISTRATION_TOKEN="
        )

    required_env_keys = set(_REQUIRED_ENV_KEYS[provider_preset])

    key_placeholders = {
        "OPENAI_API_KEY": "your-openai-key-here",
        "OPENROUTER_API_KEY": "your-openrouter-key-here",
    }

    provider_lines: list[str] = []
    for env_key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        prefix = "" if env_key in required_env_keys else "# "
        line = f"{prefix}{env_key}={key_placeholders[env_key]}"
        provider_lines.append(line)

    provider_lines_text = "\n".join(provider_lines)

    return f"""\
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER={matrix_homeserver}
# MATRIX_SSL_VERIFY=false
{extra_matrix.rstrip()}

# AI provider API keys (set the uncommented keys for this preset)
{provider_lines_text}

# Dashboard API key — protects the /api/* dashboard endpoints.
# When set, all dashboard requests require: Authorization: Bearer <key>
# The auth header is injected at the proxy layer (nginx / Vite dev server),
# so the key never appears in the browser JS bundle.
# Remove or comment out to allow open access (fine for localhost).
MINDROOM_API_KEY={api_key}

# OpenAI-compatible API authentication (separate from dashboard auth)
# OPENAI_COMPAT_API_KEYS=sk-my-secret-key
# OPENAI_COMPAT_ALLOW_UNAUTHENTICATED=true

# Backend port (default 8765)
# MINDROOM_PORT=8765
"""


def _minimal_template(provider_preset: ProviderPreset = "openai") -> str:
    """Return a bare-minimum inline config."""
    provider, model_id = _DEFAULT_MODEL_PRESETS[provider_preset]
    return f"""\
# MindRoom Configuration (minimal)

models:
  default:
    provider: {provider}
    id: {model_id}

agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant
    model: default
    rooms:
      - lobby

router:
  model: default

# Set username before first run; once created, it cannot be changed.
# You can still change display_name later.
mindroom_user:
  username: mindroom_user
  display_name: MindRoomUser

authorization:
  default_room_access: false
  global_users:
    # Replace with your Matrix user ID (example: @alice:mindroom.chat).
    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}
  agent_reply_permissions:
    "*":
      # Replace with your Matrix user ID (example: @alice:mindroom.chat).
      - {OWNER_MATRIX_USER_ID_PLACEHOLDER}

defaults:
  tools:
    - scheduler
  markdown: true
"""
