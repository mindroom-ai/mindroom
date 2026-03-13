"""Configuration management CLI subcommands for MindRoom."""

from __future__ import annotations

import logging
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import textwrap
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
    VERTEXAI_CLAUDE_ENV_KEYS,
    config_search_locations,
    env_key_for_provider,
)
from mindroom.credentials_sync import get_secret_from_env
from mindroom.tool_system.worker_routing import agent_workspace_root_path

console = Console()

config_app = typer.Typer(
    name="config",
    help="Manage MindRoom configuration files.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

# Reusable option definitions
_CONFIG_PATH_OPTION: Path | None = typer.Option(
    None,
    "--path",
    "-p",
    help="Override auto-detection and use this config file path.",
)

_ConfigInitProfile = Literal["full", "minimal", "public"]
_ProviderPreset = Literal["anthropic", "openai", "openrouter", "vertexai_claude"]

_DEFAULT_MODEL_PRESETS: dict[_ProviderPreset, tuple[str, str]] = {
    "anthropic": ("anthropic", "claude-sonnet-4-6"),
    "openai": ("openai", "gpt-5.4"),
    "openrouter": ("openrouter", "anthropic/claude-sonnet-4.6"),
    "vertexai_claude": ("vertexai_claude", "claude-sonnet-4-6"),
}

_REQUIRED_ENV_KEYS: dict[_ProviderPreset, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "vertexai_claude": (),
}
_CANONICAL_INIT_PROFILES: tuple[str, ...] = ("full", "minimal", "public", "public-vertexai-anthropic")


_MIND_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "mind_data"
_MIND_WORKSPACE_TEMPLATE_FILES: tuple[str, ...] = (
    "SOUL.md",
    "AGENTS.md",
    "USER.md",
    "IDENTITY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
)
_MIND_MEMORY_TEMPLATE = "# Memory\n\n"


def _default_storage_root_for_config(config_dir: Path) -> Path:
    """Return the default runtime storage root implied by one config directory."""
    return config_dir / "mindroom_data"


def _default_mind_workspace(config_dir: Path) -> Path:
    """Return the starter Mind workspace inside the canonical agent workspace."""
    return agent_workspace_root_path(_default_storage_root_for_config(config_dir), "mind") / "mind_data"


def _default_mind_knowledge_base_path(config_dir: Path) -> str:
    """Return the starter knowledge-base path that points at the canonical Mind workspace."""
    knowledge_root = (_default_mind_workspace(config_dir) / "memory").relative_to(config_dir)
    return f"./{knowledge_root.as_posix()}"


def _ensure_mind_workspace(workspace_path: Path, *, force: bool) -> None:
    """Create the default Mind workspace files used by the full/public templates."""
    workspace_path.mkdir(parents=True, exist_ok=True)
    (workspace_path / "memory").mkdir(parents=True, exist_ok=True)

    for filename in _MIND_WORKSPACE_TEMPLATE_FILES:
        source_path = _MIND_TEMPLATE_DIR / filename
        file_path = workspace_path / filename
        if file_path.exists() and not force:
            continue
        file_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")

    memory_path = workspace_path / "MEMORY.md"
    if not memory_path.exists() or force:
        memory_path.write_text(_MIND_MEMORY_TEMPLATE, encoding="utf-8")


def _write_env_file(
    env_path: Path,
    selected_profile: _ConfigInitProfile,
    selected_preset: _ProviderPreset,
    *,
    force: bool,
) -> bool:
    """Create or update .env and return whether the file changed."""
    if not env_path.exists():
        env_path.write_text(_env_template(selected_profile, selected_preset), encoding="utf-8")
        console.print(f"[green]Env file created:[/green] {env_path}")
        return True

    should_overwrite = force or typer.confirm(f"Overwrite existing .env file ({env_path})?", default=False)
    if not should_overwrite:
        return False

    env_path.write_text(_env_template(selected_profile, selected_preset), encoding="utf-8")
    console.print(f"[green]Env file overwritten:[/green] {env_path}")
    return True


def _config_init_env_hint(selected_profile: _ConfigInitProfile, selected_preset: _ProviderPreset) -> str:
    """Return the env setup hint shown after `mindroom config init`."""
    if selected_preset == "vertexai_claude":
        if selected_profile == "public":
            return "Set your Vertex AI project/region and Google auth (Matrix homeserver is prefilled)"
        return "Set your Matrix homeserver, Vertex AI project/region, and Google auth"
    if selected_profile == "public":
        return "Set your API keys (Matrix homeserver is prefilled)"
    return "Set your API keys and Matrix homeserver"


def _print_config_init_next_steps(
    env_path: Path,
    *,
    env_created: bool,
    selected_profile: _ConfigInitProfile,
    selected_preset: _ProviderPreset,
) -> None:
    """Print post-init guidance for the selected profile."""
    console.print("\nNext steps:")
    if env_created:
        env_hint = _config_init_env_hint(selected_profile, selected_preset)
        console.print(f"  [cyan]Edit {env_path}[/cyan]  {env_hint}")
    if selected_profile == "public":
        console.print(
            "  [cyan]mindroom connect --pair-code XXXX[/cyan]  "
            "Pair with hosted Matrix (get code from chat.mindroom.chat)",
        )
    console.print("  [cyan]mindroom config edit[/cyan]      Customize your config")
    console.print("  [cyan]mindroom config validate[/cyan]  Verify it's valid")
    console.print("  [cyan]mindroom run[/cyan]              Start the system")


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
        help=(
            "Template profile: full, minimal, public, or public-vertexai-anthropic "
            "(hosted Matrix with Vertex AI Claude defaults)."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Provider preset for generated config: anthropic, openai, openrouter, or vertexai_claude.",
    ),
) -> None:
    """Create a starter config.yaml with example agents and models.

    Generates a YAML config with starter agents, one model, and sensible defaults.
    """
    target = _resolve_config_path(path)

    if target.exists() and not force:
        console.print(f"[yellow]Config file already exists:[/yellow] {target}")
        if not typer.confirm("Overwrite existing config file?"):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    selected_profile, selected_preset = _resolve_config_init_selection(
        profile,
        minimal=minimal,
        provider=provider,
    )

    if selected_profile == "minimal":
        content = _minimal_template(selected_preset)
    else:
        full_profile: Literal["full", "public"] = "public" if selected_profile == "public" else "full"
        content = _full_template(selected_preset, target.parent, profile=full_profile)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    if selected_profile != "minimal":
        _ensure_mind_workspace(_default_mind_workspace(target.parent), force=force)

    env_path = target.parent / ".env"
    env_created = _write_env_file(env_path, selected_profile, selected_preset, force=force)

    console.print(f"[green]Config created:[/green] {target}")
    _print_config_init_next_steps(
        env_path,
        env_created=env_created,
        selected_profile=selected_profile,
        selected_preset=selected_preset,
    )


@config_app.command("show")
def config_show(
    path: Path | None = _CONFIG_PATH_OPTION,
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
    path: Path | None = _CONFIG_PATH_OPTION,
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
    path: Path | None = _CONFIG_PATH_OPTION,
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
        if provider == "vertexai_claude":
            missing.extend(
                (provider, env_key) for env_key in VERTEXAI_CLAUDE_ENV_KEYS if not get_secret_from_env(env_key)
            )
            continue
        env_key = env_key_for_provider(provider)
        if env_key and not get_secret_from_env(env_key):
            missing.append((provider, env_key))
    return missing


def _resolve_config_init_selection(
    profile: str,
    *,
    minimal: bool,
    provider: str | None,
) -> tuple[_ConfigInitProfile, _ProviderPreset]:
    """Resolve the requested `config init` profile and provider preset."""
    profile_value = "minimal" if minimal else profile.strip().lower()
    normalized_profile = _normalize_init_profile(profile_value)
    if normalized_profile is None:
        msg = f"Invalid profile '{profile}'. Expected one of: {', '.join(_CANONICAL_INIT_PROFILES)}"
        raise typer.BadParameter(msg)
    selected_profile, profile_preset = normalized_profile

    provider_preset = _normalize_provider_preset(provider) if provider else None
    if provider and provider_preset is None:
        console.print(
            "[red]Invalid --provider value.[/red] Use: anthropic, openai, openrouter, or vertexai_claude.",
        )
        raise typer.Exit(1)

    if selected_profile == "minimal":
        return selected_profile, provider_preset or "openai"
    if provider_preset is not None:
        return selected_profile, provider_preset
    if profile_preset is not None:
        return selected_profile, profile_preset
    if selected_profile == "public":
        return selected_profile, "openai"
    return selected_profile, _prompt_provider_preset()


def _normalize_init_profile(profile: str) -> tuple[_ConfigInitProfile, _ProviderPreset | None] | None:
    """Normalize `config init --profile` values and profile aliases."""
    aliases: dict[str, tuple[_ConfigInitProfile, _ProviderPreset | None]] = {
        "full": ("full", None),
        "minimal": ("minimal", None),
        "public": ("public", None),
        "public-vertexai-anthropic": ("public", "vertexai_claude"),
        "public-vertexai-claude": ("public", "vertexai_claude"),
        "vertexai-anthropic": ("public", "vertexai_claude"),
        "vertexai-claude": ("public", "vertexai_claude"),
    }
    return aliases.get(profile.strip().lower())


def _check_env_keys(config: Config) -> None:
    """Warn about missing environment variables for configured providers."""
    missing = _find_missing_env_keys(config)
    if missing:
        console.print("\n[yellow]Warning:[/yellow] Missing environment variables:\n")
        for provider, env_key in missing:
            console.print(f"  [yellow]*[/yellow] {provider}: Set {env_key}")
        console.print("\nYou can set these in a .env file or export them in your shell.")


def _normalize_provider_preset(provider: str) -> _ProviderPreset | None:
    """Normalize provider preset values used by prompts and CLI flags."""
    normalized = provider.strip().lower()
    aliases: dict[str, _ProviderPreset] = {
        "anthropic": "anthropic",
        "claude": "anthropic",
        "a": "anthropic",
        "openai": "openai",
        "o": "openai",
        "openrouter": "openrouter",
        "or": "openrouter",
        "r": "openrouter",
        "vertexai_claude": "vertexai_claude",
        "vertexai": "vertexai_claude",
        "vertex": "vertexai_claude",
        "vertexai-anthropic": "vertexai_claude",
        "vertex-anthropic": "vertexai_claude",
    }
    return aliases.get(normalized)


def _prompt_provider_preset() -> _ProviderPreset:
    """Prompt the user for a starter provider preset."""
    while True:
        raw_value = typer.prompt(
            "Choose provider preset [anthropic/openai/openrouter/vertexai_claude]",
            default="openai",
            show_default=True,
        )
        provider_preset = _normalize_provider_preset(raw_value)
        if provider_preset is not None:
            return provider_preset
        console.print("[red]Invalid choice.[/red] Enter anthropic, openai, openrouter, or vertexai_claude.")


def _model_template_block(provider_preset: _ProviderPreset) -> str:
    """Render the provider-specific YAML fragment for models.default."""
    provider, model_id = _DEFAULT_MODEL_PRESETS[provider_preset]
    lines = [f"provider: {provider}", f"id: {model_id}"]
    return textwrap.indent("\n".join(lines), "    ")


def _full_template(
    provider_preset: _ProviderPreset,
    config_dir: Path,
    *,
    profile: Literal["full", "public"] = "full",
) -> str:
    """Return a provider-aware starter config."""
    model_block = _model_template_block(provider_preset)
    mind_memory_knowledge_path = _default_mind_knowledge_base_path(config_dir)

    if profile == "public":
        mindroom_user_block = ""
    else:
        mindroom_user_block = textwrap.dedent("""\

            # Set username before first run; once created, it cannot be changed.
            # You can still change display_name later.
            mindroom_user:
              username: mindroom_user
              display_name: MindRoomUser
        """)

    return f"""\
# MindRoom Configuration
# Generated by: mindroom config init
# Docs: https://docs.mindroom.chat/

models:
  default:
{model_block}

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
  mind:
    display_name: Mind
    role: Personal assistant with persistent file-based identity and memory
    model: default
    include_default_tools: false
    learning: false
    memory_backend: file
    memory_file_path: mind_data
    rooms:
      - personal
    context_files:
      - mind_data/SOUL.md
      - mind_data/AGENTS.md
      - mind_data/USER.md
      - mind_data/IDENTITY.md
      - mind_data/TOOLS.md
      - mind_data/HEARTBEAT.md
    knowledge_bases:
      - mind_memory
    tools:
      - shell
      - coding
      - duckduckgo
      - website
      - browser
      - scheduler
      - subagents
      - matrix_message
    skills:
      - mindroom-docs
    instructions:
      - You wake up fresh each session with no memory of previous conversations. Your context files are already loaded into your system prompt.
      - Important long-term context is persisted by the configured MindRoom memory backend. If something must be preserved exactly, write or update the relevant file directly.
      - MEMORY.md is curated long-term memory; daily files are short-lived notes and logs.
      - Ask before external or destructive actions.
      - Before answering prior-history questions, search memory files first when a knowledge base is configured.

router:
  model: default
{mindroom_user_block}
matrix_room_access:
  mode: single_user_private
  multi_user_join_rule: public
  publish_to_room_directory: false
  invite_only_rooms: []
  reconcile_existing_rooms: false

matrix_space:
  enabled: true
  name: MindRoom

knowledge_bases:
  mind_memory:
    path: {mind_memory_knowledge_path}
    watch: true

# File-based memory avoids external memory LLMs, and starter configs use a local embedder for knowledge indexing.
memory:
  backend: file
  embedder:
    provider: sentence_transformers
    config:
      model: sentence-transformers/all-MiniLM-L6-v2
  file:
    max_entrypoint_lines: 200
  auto_flush:
    enabled: true

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


def _env_template(profile: _ConfigInitProfile, provider_preset: _ProviderPreset) -> str:
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

    provider_lines_text = _provider_env_template(provider_preset)

    return f"""\
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER={matrix_homeserver}
# MATRIX_SSL_VERIFY=false
{extra_matrix.rstrip()}

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

# MindRoom port (default 8765)
# MINDROOM_PORT=8765
"""


def _minimal_template(provider_preset: _ProviderPreset = "openai") -> str:
    """Return a bare-minimum inline config."""
    model_block = _model_template_block(provider_preset)
    return f"""\
# MindRoom Configuration (minimal)

models:
  default:
{model_block}

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

matrix_space:
  enabled: true
  name: MindRoom

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


def _provider_env_template(provider_preset: _ProviderPreset) -> str:
    """Return the provider-specific section of the starter .env file."""
    if provider_preset == "vertexai_claude":
        return textwrap.dedent("""\
        # Vertex AI Claude configuration
        ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id
        CLOUD_ML_REGION=us-central1

        # Authenticate with Google Application Default Credentials before running:
        # gcloud auth application-default login
        # or set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
        """).rstrip()

    required_env_keys = set(_REQUIRED_ENV_KEYS[provider_preset])
    key_placeholders = {
        "ANTHROPIC_API_KEY": "your-anthropic-key-here",
        "OPENAI_API_KEY": "your-openai-key-here",
        "OPENROUTER_API_KEY": "your-openrouter-key-here",
    }
    provider_lines: list[str] = ["# AI provider API keys (set the uncommented keys for this preset)"]
    for env_key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        prefix = "" if env_key in required_env_keys else "# "
        provider_lines.append(f"{prefix}{env_key}={key_placeholders[env_key]}")
    return "\n".join(provider_lines)
