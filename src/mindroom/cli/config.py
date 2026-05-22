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
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Literal

import typer
from rich.console import Console
from rich.syntax import Syntax

from mindroom import constants
from mindroom.model_defaults import (
    CONFIG_INIT_MODEL_ALTERNATIVES,
    CONFIG_INIT_MODEL_PRESETS,
    LLAMA_CPP_API_KEY_DEFAULT,
    LLAMA_CPP_BASE_URL_DEFAULT,
    LLAMA_CPP_GEMMA,
    LLAMA_CPP_QWEN,
    LOCAL_QWEN_CONTEXT_WINDOW,
    LOCAL_QWEN_PRESET_NAME,
    OLLAMA_GEMMA,
    OLLAMA_HOST_DEFAULT,
    OLLAMA_QWEN,
    SENTENCE_TRANSFORMERS_DEFAULT,
    llama_cpp_server_command,
)
from mindroom.runtime_env_policy import AZURE_OPENAI_ENV_BY_KEY, VERTEXAI_CLAUDE_ENV_BY_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping

    import yaml  # type: ignore[import-untyped]
    from pydantic import ValidationError

    from mindroom.config.main import Config, ConfigRuntimeValidationError
    from mindroom.constants import RuntimePaths

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

_MatrixServerPreset = Literal["self-hosted", "mindroom.chat"]
_ProviderPreset = Literal[
    "anthropic",
    "azure",
    "codex",
    "llama_cpp",
    "ollama",
    "openai",
    "openrouter",
    "vertexai_claude",
]

_OLLAMA_HOST = OLLAMA_HOST_DEFAULT
_LLAMA_CPP_BASE_URL = LLAMA_CPP_BASE_URL_DEFAULT
_LLAMA_CPP_API_KEY = LLAMA_CPP_API_KEY_DEFAULT
_PROVIDER_PRESET_ALIASES: dict[str, _ProviderPreset] = {
    "anthropic": "anthropic",
    "azure": "azure",
    "azure_openai": "azure",
    "azure-openai": "azure",
    "claude": "anthropic",
    "codex": "codex",
    "llama.cpp": "llama_cpp",
    "llama-cpp": "llama_cpp",
    "llama_cpp": "llama_cpp",
    "ollama": "ollama",
    "openai": "openai",
    "openrouter": "openrouter",
    "vertexai_claude": "vertexai_claude",
    "vertexai": "vertexai_claude",
    "vertex": "vertexai_claude",
    "vertexai-anthropic": "vertexai_claude",
    "vertex-anthropic": "vertexai_claude",
}

_PUBLIC_HOSTED_ENV_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("MATRIX_HOMESERVER", "https://mindroom.chat"),
    ("MATRIX_SERVER_NAME", "mindroom.chat"),
    ("MINDROOM_PROVISIONING_URL", "https://mindroom.chat"),
    ("MATRIX_REGISTRATION_TOKEN", ""),
)
_MATRIX_SERVER_CHOICES_TEXT = "self-hosted or mindroom.chat"
_MATRIX_SERVER_HELP = (
    "Matrix defaults: self-hosted = configure your own homeserver; mindroom.chat = hosted Matrix pairing defaults."
)
_PROVIDER_HELP = "Default model provider for the generated config."
_PROVIDER_CHOICES_TEXT = "anthropic, azure, codex, llama.cpp, ollama, openai, openrouter, or vertexai_claude"
_MATRIX_DELIVERY_TEMPLATE_BLOCK = """\
matrix_delivery:
  ignore_unverified_devices: false"""


def _config_init_storage_plan(
    config_dir: Path,
    env_path: Path,
    *,
    replace_env_file: bool,
) -> tuple[Path, bool]:
    """Return the storage root and whether the starter config can use the env placeholder."""
    runtime_paths = constants.resolve_runtime_paths(config_path=config_dir / "config.yaml")
    if replace_env_file:
        return runtime_paths.storage_root, True
    if "MINDROOM_STORAGE_PATH" in runtime_paths.env_file_values and env_path.is_file():
        return runtime_paths.storage_root, True
    return runtime_paths.storage_root, False


def _config_init_owner_user_id(config_path: Path) -> str | None:
    """Return the paired owner MXID available to config init, if one was persisted."""
    from mindroom.cli.owner import parse_owner_matrix_user_id  # noqa: PLC0415

    runtime_paths = constants.resolve_runtime_paths(config_path=config_path)
    return parse_owner_matrix_user_id(runtime_paths.env_value(constants.OWNER_MATRIX_USER_ID_ENV))


def _default_mind_workspace(storage_root: Path) -> Path:
    """Return the shared single-user starter Mind workspace inside the canonical agent workspace."""
    from mindroom.tool_system.worker_routing import agent_workspace_root_path  # noqa: PLC0415

    return agent_workspace_root_path(storage_root, "mind")


def _path_string_for_config(path: Path, config_dir: Path) -> str:
    """Render one filesystem path for config.yaml, preferring config-relative paths when possible."""
    resolved_path = path.expanduser().resolve()
    try:
        relative_path = resolved_path.relative_to(config_dir.resolve())
    except ValueError:
        return str(resolved_path)
    return f"./{relative_path.as_posix()}"


def _default_mind_knowledge_base_path(
    config_dir: Path,
    *,
    storage_root: Path,
    use_storage_env_placeholder: bool,
) -> str:
    """Return the starter knowledge-base path anchored to the chosen runtime storage root."""
    if use_storage_env_placeholder:
        return "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/memory"
    return _path_string_for_config(_default_mind_workspace(storage_root) / "memory", config_dir)


def _ensure_mind_workspace(workspace_path: Path, *, force: bool) -> None:
    """Create the default Mind workspace files used by starter configs."""
    from mindroom.workspaces import ensure_workspace_template  # noqa: PLC0415

    ensure_workspace_template(workspace_path, template="mind", force=force)


def _write_env_file(
    env_path: Path,
    matrix_server: _MatrixServerPreset,
    selected_preset: _ProviderPreset,
    *,
    storage_root: Path,
    replace_existing: bool,
) -> bool:
    """Create or update .env and return whether the file changed."""
    if not env_path.exists():
        env_path.write_text(_env_template(matrix_server, selected_preset, storage_root), encoding="utf-8")
        console.print(f"[green]Env file created:[/green] {env_path}")
        return True

    if not replace_existing:
        # `connect` can create .env before `config init`; mindroom.chat still
        # need hosted Matrix defaults, so preserve user-owned values and append
        # only the missing hosted keys.
        if matrix_server == "mindroom.chat":
            return _append_missing_env_defaults(
                env_path,
                _PUBLIC_HOSTED_ENV_DEFAULTS,
                title="Hosted Matrix defaults for mindroom.chat",
            )
        return False

    env_path.write_text(_env_template(matrix_server, selected_preset, storage_root), encoding="utf-8")
    console.print(f"[green]Env file overwritten:[/green] {env_path}")
    return True


def _append_missing_env_defaults(
    env_path: Path,
    defaults: tuple[tuple[str, str], ...],
    *,
    title: str,
) -> bool:
    """Append missing env defaults without changing existing user-owned values."""
    from dotenv import dotenv_values  # noqa: PLC0415

    existing_values = dotenv_values(env_path)
    missing_defaults = [(key, value) for key, value in defaults if key not in existing_values]
    if not missing_defaults:
        return False

    current_content = env_path.read_text(encoding="utf-8")
    separator = ""
    if current_content:
        separator = "" if current_content.endswith("\n") else "\n"
        if current_content.strip():
            separator += "\n"

    appended_lines = [f"# {title}", *(f"{key}={value}" for key, value in missing_defaults)]
    appended_content = "\n".join(appended_lines)
    env_path.write_text(f"{current_content}{separator}{appended_content}\n", encoding="utf-8")
    console.print(f"[green]Env file updated:[/green] {env_path}")
    return True


def _should_replace_env_file(env_path: Path, *, force: bool) -> bool:
    """Return whether config init should create or overwrite the full env template."""
    if not env_path.exists():
        return True
    return force or typer.confirm(f"Overwrite existing .env file ({env_path})?", default=False)


def _config_init_env_hint(matrix_server: _MatrixServerPreset, selected_preset: _ProviderPreset) -> str:
    """Return the env setup hint shown after `mindroom config init`."""
    hosted_hints: dict[_ProviderPreset, str] = {
        "azure": "Set your Azure OpenAI key/endpoint and confirm the config model deployment name",
        "codex": "Run `codex login` before starting MindRoom (Matrix homeserver is prefilled)",
        "vertexai_claude": "Set your Vertex AI project/region and Google auth (Matrix homeserver is prefilled)",
        "ollama": "Start Ollama and pull the local models (Matrix homeserver is prefilled)",
        "llama_cpp": "Start llama.cpp server with the local model (Matrix homeserver is prefilled)",
    }
    standalone_hints: dict[_ProviderPreset, str] = {
        "azure": "Set your Matrix homeserver, Azure OpenAI key/endpoint, and config model deployment name",
        "codex": "Set your Matrix homeserver and run `codex login` before starting MindRoom",
        "vertexai_claude": "Set your Matrix homeserver, Vertex AI project/region, and Google auth",
        "ollama": "Set your Matrix homeserver, start Ollama, and pull the local models",
        "llama_cpp": "Set your Matrix homeserver and start llama.cpp server with the local model",
    }
    if matrix_server == "mindroom.chat":
        return hosted_hints.get(selected_preset, "Set your API keys (Matrix homeserver is prefilled)")
    return standalone_hints.get(selected_preset, "Set your API keys and Matrix homeserver")


def _print_config_init_next_steps(
    env_path: Path,
    *,
    env_changed: bool,
    matrix_server: _MatrixServerPreset,
    selected_preset: _ProviderPreset,
) -> None:
    """Print post-init guidance for the selected Matrix server and provider."""
    console.print("\nNext steps:")
    if env_changed:
        env_hint = _config_init_env_hint(matrix_server, selected_preset)
        console.print(f"  [cyan]Edit {env_path}[/cyan]  {env_hint}")
    if matrix_server == "mindroom.chat":
        console.print(
            "  [cyan]mindroom connect --pair-code XXXX[/cyan]  "
            "Pair with hosted Matrix (get code from chat.mindroom.chat)",
        )
    if selected_preset == "ollama":
        console.print(f"  [cyan]ollama pull {OLLAMA_GEMMA}[/cyan]         Pull the default local model")
        console.print(f"  [cyan]ollama pull {OLLAMA_QWEN}[/cyan]   Pull the larger local model option")
    if selected_preset == "llama_cpp":
        console.print(
            f"  [cyan]{llama_cpp_server_command(LLAMA_CPP_GEMMA)}[/cyan]  Start the default local model server",
        )
        console.print(
            f"  [cyan]{llama_cpp_server_command(LLAMA_CPP_QWEN)}[/cyan]  Start the larger local model option",
        )
    console.print("  [cyan]mindroom config edit[/cyan]      Customize your config")
    console.print("  [cyan]mindroom config validate[/cyan]  Verify it's valid")
    console.print("  [cyan]mindroom run[/cyan]              Start the system")


def _config_discovery_env(path: Path | None = None) -> dict[str, str]:
    """Return the exported env snapshot used for config discovery and display."""
    process_env = constants.exported_process_env()
    if path is not None:
        process_env["MINDROOM_CONFIG_PATH"] = str(path.expanduser().resolve())
    return process_env


def _format_config_search_locations(process_env: Mapping[str, str]) -> list[str]:
    """Return rendered config search locations with existence labels."""
    return [
        f"  {i}. {loc} ({'[green]exists[/green]' if loc.exists() else '[dim]not found[/dim]'})"
        for i, loc in enumerate(constants.config_search_locations(process_env), 1)
    ]


def print_config_search_locations(process_env: Mapping[str, str], *, title: str) -> None:
    """Print the config search locations used by CLI commands."""
    console.print(title)
    for line in _format_config_search_locations(process_env):
        console.print(line)


def _yaml_syntax(content: str, *, line_numbers: bool, word_wrap: bool) -> Syntax:
    """Return the shared Rich YAML renderer used by config display commands."""
    return Syntax(content, "yaml", theme="monokai", line_numbers=line_numbers, word_wrap=word_wrap)


def _resolve_config_path(
    path: Path | None,
    *,
    process_env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the config file path from explicit argument or default."""
    if path is not None:
        return path.expanduser().resolve()
    resolved_process_env = dict(process_env) if process_env is not None else constants.exported_process_env()
    return constants.resolve_primary_runtime_paths(process_env=resolved_process_env).config_path.resolve()


def activate_cli_runtime(
    path: Path | None = None,
    *,
    storage_path: Path | None = None,
) -> RuntimePaths:
    """Create the CLI runtime context once and return it for explicit threading."""
    if path is not None:
        return constants.resolve_primary_runtime_paths(
            config_path=path.expanduser().resolve(),
            storage_path=storage_path,
            process_env=constants.exported_process_env(),
        )

    return constants.resolve_primary_runtime_paths(
        storage_path=storage_path,
        process_env=constants.exported_process_env(),
    )


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


def format_validation_errors(
    exc: ValidationError | ConfigRuntimeValidationError | yaml.YAMLError | OSError | UnicodeError,
    config_path: Path | None = None,
) -> None:
    """Print config validation errors in a user-friendly format."""
    from mindroom.config.main import iter_config_validation_messages  # noqa: PLC0415

    if config_path:
        console.print(f"[red]Error:[/red] Invalid configuration in {config_path}\n")
    else:
        console.print("[red]Error:[/red] Invalid configuration\n")
    console.print("Issues found:")
    for location, message in iter_config_validation_messages(exc):
        display_location = location.replace(" → ", " -> ")
        console.print(f"  [red]*[/red] {display_location}: {message}")
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
    matrix_server: str = typer.Option(
        "mindroom.chat",
        "--matrix-server",
        help=f"{_MATRIX_SERVER_HELP} Choices: {_MATRIX_SERVER_CHOICES_TEXT}.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=f"{_PROVIDER_HELP} Choices: {_PROVIDER_CHOICES_TEXT}.",
    ),
    print_config: bool = typer.Option(
        False,
        "--print",
        help="Print generated config YAML with syntax highlighting instead of writing files.",
    ),
) -> None:
    """Create a starter config.yaml with example agents and models.

    Generates a YAML config with starter agents, one model, and sensible defaults.
    """
    target = _resolve_config_path(path)
    env_path = target.parent / ".env"

    if target.exists() and not force and not print_config:
        console.print(f"[yellow]Config file already exists:[/yellow] {target}")
        if not typer.confirm("Overwrite existing config file?"):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    selected_matrix_server, selected_preset = _resolve_config_init_selection(
        matrix_server,
        provider=provider,
        interactive=not print_config,
    )

    replace_env_file = False if print_config else _should_replace_env_file(env_path, force=force)
    storage_root, use_storage_env_placeholder = _config_init_storage_plan(
        target.parent,
        env_path,
        replace_env_file=replace_env_file,
    )

    content = _full_template(
        selected_preset,
        target.parent,
        storage_root=storage_root,
        use_storage_env_placeholder=use_storage_env_placeholder,
        matrix_server=selected_matrix_server,
    )

    # `connect` can run before `config init`, when no config exists to patch.
    # In that order, connect persists the owner MXID in .env so init can render
    # authorization defaults without leaving pairing placeholders behind.
    if owner_user_id := _config_init_owner_user_id(target):
        from mindroom.cli.owner import replace_owner_placeholders_in_text  # noqa: PLC0415

        content = replace_owner_placeholders_in_text(content, owner_user_id)

    if print_config:
        console.print(_yaml_syntax(content, line_numbers=False, word_wrap=False), soft_wrap=True)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    _ensure_mind_workspace(_default_mind_workspace(storage_root), force=force)

    env_changed = _write_env_file(
        env_path,
        selected_matrix_server,
        selected_preset,
        storage_root=storage_root,
        replace_existing=replace_env_file,
    )

    console.print(f"[green]Config created:[/green] {target}")
    _print_config_init_next_steps(
        env_path,
        env_changed=env_changed,
        matrix_server=selected_matrix_server,
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
    process_env = _config_discovery_env(path)
    config_file = _resolve_config_path(path, process_env=process_env)

    if not config_file.exists():
        console.print(f"[yellow]No config file found at:[/yellow] {config_file}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        print_config_search_locations(process_env, title="\nSearch locations (first match wins):")
        raise typer.Exit(1)

    try:
        content = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    if raw:
        print(content, end="")
        return

    console.print(f"[bold green]Config file:[/bold green] {config_file}\n")
    console.print(_yaml_syntax(content, line_numbers=True, word_wrap=True))


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
    runtime_paths = activate_cli_runtime(path)
    config_path = runtime_paths.config_path
    console.print(f"Validating configuration: [bold]{config_path}[/bold]\n")

    if not config_path.exists():
        console.print(f"[red]Error:[/red] Configuration file not found: {config_path}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        raise typer.Exit(1)

    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415

    try:
        config = load_config_quiet(runtime_paths=runtime_paths)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None

    console.print("[green]Configuration is valid.[/green]\n")
    console.print(f"  Agents: {len(config.agents)} ({', '.join(config.agents.keys()) or 'none'})")
    console.print(f"  Teams:  {len(config.teams)} ({', '.join(config.teams.keys()) or 'none'})")
    console.print(f"  Models: {len(config.models)} ({', '.join(config.models.keys()) or 'none'})")
    rooms = config.get_all_configured_rooms()
    console.print(f"  Rooms:  {len(rooms)} ({', '.join(sorted(rooms)) or 'none'})")

    # Check for missing API keys based on configured providers
    check_env_keys(config, runtime_paths=runtime_paths)


@config_app.command("path")
def config_path_cmd(
    path: Path | None = _CONFIG_PATH_OPTION,
) -> None:
    """Show the resolved config file path and search locations."""
    process_env = _config_discovery_env(path)
    resolved = _resolve_config_path(path, process_env=process_env)
    exists = resolved.exists()
    status = "[green]exists[/green]" if exists else "[red]not found[/red]"
    console.print(f"Resolved config path: {resolved} ({status})")

    print_config_search_locations(process_env, title="\nSearch locations (first match wins):")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_config_quiet(
    runtime_paths: RuntimePaths,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> Config:
    """Load config while temporarily suppressing structlog output.

    structlog's default PrintLogger bypasses stdlib log levels, so we
    route it through stdlib with the root level at WARNING for the
    duration of the load then reset so later callers (e.g. the bot)
    can configure structlog themselves.
    """
    import structlog  # noqa: PLC0415

    from mindroom.config.main import load_config  # noqa: PLC0415

    was_configured = structlog.is_configured()
    if not was_configured:
        logging.basicConfig(format="%(message)s", level=logging.WARNING)
        structlog.configure(
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
    try:
        return load_config(
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )
    finally:
        if not was_configured:
            structlog.reset_defaults()


def _find_missing_env_keys(
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[tuple[str, str]]:
    """Return (provider, env_key) pairs for configured providers missing env vars."""
    from mindroom.credentials_sync import get_secret_from_env  # noqa: PLC0415

    providers_used: set[str] = {model.provider for model in config.models.values()}
    missing: list[tuple[str, str]] = []
    for provider in sorted(providers_used):
        if provider == "azure":
            missing.extend(
                (provider, env_key)
                for env_key in (AZURE_OPENAI_ENV_BY_KEY["api_key"], AZURE_OPENAI_ENV_BY_KEY["endpoint"])
                if not get_secret_from_env(env_key, runtime_paths=runtime_paths)
            )
            continue
        if provider == "vertexai_claude":
            missing.extend(
                (provider, env_key)
                for env_key in VERTEXAI_CLAUDE_ENV_BY_KEY.values()
                if not get_secret_from_env(env_key, runtime_paths=runtime_paths)
            )
            continue
        env_key = constants.env_key_for_provider(provider)
        if env_key and not get_secret_from_env(env_key, runtime_paths=runtime_paths):
            missing.append((provider, env_key))
    return missing


def _resolve_config_init_selection(
    matrix_server: str,
    *,
    provider: str | None,
    interactive: bool = True,
) -> tuple[_MatrixServerPreset, _ProviderPreset]:
    """Resolve the requested Matrix server and provider preset."""
    selected_matrix_server = _normalize_matrix_server_preset(matrix_server)
    if selected_matrix_server is None:
        console.print(f"[red]Invalid --matrix-server value.[/red] Use: {_MATRIX_SERVER_CHOICES_TEXT}.")
        raise typer.Exit(1)
    provider_preset = _normalize_provider_preset(provider) if provider else None
    if provider and provider_preset is None:
        console.print(f"[red]Invalid --provider value.[/red] Use: {_PROVIDER_CHOICES_TEXT}.")
        raise typer.Exit(1)

    if provider_preset is not None:
        return selected_matrix_server, provider_preset
    if selected_matrix_server == "mindroom.chat" or not interactive:
        return selected_matrix_server, "openai"
    return selected_matrix_server, _prompt_provider_preset()


def _normalize_matrix_server_preset(matrix_server: str) -> _MatrixServerPreset | None:
    """Normalize Matrix server presets used by config init."""
    value = matrix_server.strip().lower()
    if value == "self-hosted":
        return "self-hosted"
    if value == "mindroom.chat":
        return "mindroom.chat"
    return None


def check_env_keys(config: Config, runtime_paths: RuntimePaths) -> None:
    """Warn about missing environment variables for configured providers."""
    missing = _find_missing_env_keys(config, runtime_paths)
    if missing:
        console.print("\n[yellow]Warning:[/yellow] Missing environment variables:\n")
        for provider, env_key in missing:
            console.print(f"  [yellow]*[/yellow] {provider}: Set {env_key}")
        console.print("\nYou can set these in a .env file or export them in your shell.")


def _normalize_provider_preset(provider: str) -> _ProviderPreset | None:
    """Normalize provider preset values used by prompts and CLI flags."""
    return _PROVIDER_PRESET_ALIASES.get(provider.strip().lower())


def _prompt_provider_preset() -> _ProviderPreset:
    """Prompt the user for a starter provider preset."""
    while True:
        raw_value = typer.prompt(
            "Choose provider preset [anthropic/azure/codex/llama.cpp/ollama/openai/openrouter/vertexai_claude]",
            default="openai",
            show_default=True,
        )
        provider_preset = _normalize_provider_preset(raw_value)
        if provider_preset is not None:
            return provider_preset
        console.print(f"[red]Invalid choice.[/red] Enter {_PROVIDER_CHOICES_TEXT}.")


def _model_template_block(provider_preset: _ProviderPreset) -> str:
    """Render the provider-specific YAML fragment for models.default."""
    model_preset = CONFIG_INIT_MODEL_PRESETS[provider_preset]
    lines = [
        f"provider: {model_preset.provider}",
        f"id: {model_preset.id}",
        f"context_window: {model_preset.context_window}",
    ]
    if provider_preset == "codex":
        lines.extend(
            [
                "# Prompt caching is enabled automatically per active agent session.",
                "extra_kwargs:",
                "  reasoning_effort: medium",
            ],
        )
    if provider_preset == "ollama":
        lines.append(f"host: {_OLLAMA_HOST}")
    if provider_preset == "llama_cpp":
        lines.extend(
            [
                "extra_kwargs:",
                f"  api_key: {_LLAMA_CPP_API_KEY}",
                f"  base_url: {_LLAMA_CPP_BASE_URL}",
            ],
        )
    return textwrap.indent("\n".join(lines), "    ")


def _additional_models_template_block(provider_preset: _ProviderPreset) -> str:
    """Render optional additional named model presets under `models:`."""
    if provider_preset == "ollama":
        return (
            f"\n  {LOCAL_QWEN_PRESET_NAME}:\n"
            "    provider: ollama\n"
            f"    id: {OLLAMA_QWEN}\n"
            f"    context_window: {LOCAL_QWEN_CONTEXT_WINDOW}\n"
            f"    host: {_OLLAMA_HOST}"
        )
    if provider_preset == "llama_cpp":
        return (
            f"\n  {LOCAL_QWEN_PRESET_NAME}:\n"
            "    provider: openai\n"
            f"    id: {LLAMA_CPP_QWEN}\n"
            f"    context_window: {LOCAL_QWEN_CONTEXT_WINDOW}\n"
            "    extra_kwargs:\n"
            f"      api_key: {_LLAMA_CPP_API_KEY}\n"
            f"      base_url: {_LLAMA_CPP_BASE_URL}"
        )
    return ""


def _commented_model_options_template_block(provider_preset: _ProviderPreset) -> str:
    """Render commented model alternatives for the selected provider preset."""
    alternatives = CONFIG_INIT_MODEL_ALTERNATIVES.get(provider_preset)
    if not alternatives:
        return ""

    lines = [
        "",
        "  # Other model options for this provider:",
    ]
    for name, model_preset in alternatives:
        lines.extend(
            [
                f"  # {name}:",
                f"  #   provider: {model_preset.provider}",
                f"  #   id: {model_preset.id}",
            ],
        )
        if model_preset.context_window is not None:
            lines.append(f"  #   context_window: {model_preset.context_window}")
    return "\n".join(lines)


def _full_template(
    provider_preset: _ProviderPreset,
    config_dir: Path,
    *,
    storage_root: Path,
    use_storage_env_placeholder: bool,
    matrix_server: _MatrixServerPreset,
) -> str:
    """Return a provider-aware starter config.

    `config init` intentionally generates the shared single-user starter model.
    Requester-private agents remain an opt-in advanced config surface.
    """
    model_block = _model_template_block(provider_preset)
    additional_models_block = _additional_models_template_block(provider_preset)
    commented_model_options_block = _commented_model_options_template_block(provider_preset)
    mind_memory_knowledge_path = _default_mind_knowledge_base_path(
        config_dir,
        storage_root=storage_root,
        use_storage_env_placeholder=use_storage_env_placeholder,
    )

    if matrix_server == "mindroom.chat":
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
{model_block}{additional_models_block}{commented_model_options_block}

agents:
  assistant:
    display_name: Assistant
    role: A helpful general-purpose assistant
    model: default
    rooms:
      - lobby
    accept_invites: true
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
    rooms:
      - personal
    accept_invites: true
    context_files:
      - SOUL.md
      - AGENTS.md
      - USER.md
      - IDENTITY.md
      - TOOLS.md
      - HEARTBEAT.md
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
      - thread_tags
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
  accept_invites: true
{mindroom_user_block}
matrix_room_access:
  mode: single_user_private

matrix_space:
  enabled: true
  name: MindRoom

{_MATRIX_DELIVERY_TEMPLATE_BLOCK}

knowledge_bases:
  mind_memory:
    path: {mind_memory_knowledge_path}
    watch: true

# File-based memory requires no external LLM, and starter configs use a local embedder for knowledge indexing.
memory:
  backend: file
  embedder:
    provider: sentence_transformers
    config:
      model: {SENTENCE_TRANSFORMERS_DEFAULT}
  file:
    max_entrypoint_lines: 200
  auto_flush:
    enabled: true

authorization:
  default_room_access: false
  global_users:
    # Replace with your Matrix user ID (example: @alice:mindroom.chat).
    - {constants.OWNER_MATRIX_USER_ID_PLACEHOLDER}
  agent_reply_permissions:
    "*":
      # Replace with your Matrix user ID (example: @alice:mindroom.chat).
      - {constants.OWNER_MATRIX_USER_ID_PLACEHOLDER}

defaults:
  tools:
    - scheduler
  markdown: true
  compaction:
    enabled: true
"""


def _env_template(
    matrix_server: _MatrixServerPreset,
    provider_preset: _ProviderPreset,
    storage_root: Path,
) -> str:
    """Return a starter .env file for standalone deployments.

    Generates a random dashboard API key.
    """
    api_key = secrets.token_urlsafe(32)
    if matrix_server == "mindroom.chat":
        matrix_homeserver = "https://mindroom.chat"
        extra_matrix = (
            "# Matrix server_name override (needed when federation hostname differs)\n"
            "MATRIX_SERVER_NAME=mindroom.chat\n\n"
            "# Hosted pairing/provisioning API for `mindroom connect` and token issuance\n"
            "MINDROOM_PROVISIONING_URL=https://mindroom.chat\n\n"
            "# Required for homeservers that gate bot registration (recommended for mindroom.chat)\n"
            "# Keep this secret; do not commit real values.\n"
            "MATRIX_REGISTRATION_TOKEN="
        )
    else:
        matrix_homeserver = "https://matrix.example.com"
        extra_matrix = (
            "# Matrix registration token (only needed if your homeserver requires it)\n# MATRIX_REGISTRATION_TOKEN="
        )

    provider_lines_text = _provider_env_template(provider_preset)
    storage_root_block = (
        "# Runtime storage root for canonical agent state, sessions, logs, and credentials\n"
        f"MINDROOM_STORAGE_PATH={storage_root.expanduser().resolve()}\n\n"
    )

    return f"""\
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER={matrix_homeserver}
# MATRIX_SSL_VERIFY=false
{extra_matrix.rstrip()}

{storage_root_block}{provider_lines_text}

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


def _provider_env_template(provider_preset: _ProviderPreset) -> str:
    """Return the provider-specific section of the starter .env file."""
    if provider_preset == "codex":
        return textwrap.dedent("""\
        # Codex CLI subscription authentication
        # Run `codex login` before starting MindRoom.
        # MindRoom reads ChatGPT OAuth tokens from ~/.codex/auth.json by default.
        # CODEX_HOME=~/.codex
        """).rstrip()

    if provider_preset == "vertexai_claude":
        return textwrap.dedent(f"""\
        # Vertex AI Claude configuration
        {VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"]}=your-gcp-project-id
        {VERTEXAI_CLAUDE_ENV_BY_KEY["region"]}=us-central1

        # Authenticate with Google Application Default Credentials before running:
        # gcloud auth application-default login
        # or set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
        """).rstrip()

    if provider_preset == "azure":
        return textwrap.dedent(f"""\
        # Azure OpenAI configuration
        {AZURE_OPENAI_ENV_BY_KEY["api_key"]}=your-azure-openai-key-here
        {AZURE_OPENAI_ENV_BY_KEY["endpoint"]}=https://your-resource.openai.azure.com

        # Optional: override Agno's Azure OpenAI default API version.
        # {AZURE_OPENAI_ENV_BY_KEY["api_version"]}=2024-10-21

        # In config.yaml, set models.default.id to your Azure OpenAI deployment name.
        """).rstrip()

    if provider_preset == "ollama":
        return textwrap.dedent(f"""\
        # Ollama local model server
        OLLAMA_HOST={_OLLAMA_HOST}

        # Pull the starter local models before running MindRoom:
        # ollama pull {OLLAMA_GEMMA}
        # ollama pull {OLLAMA_QWEN}
        """).rstrip()

    if provider_preset == "llama_cpp":
        return textwrap.dedent(f"""\
        # llama.cpp OpenAI-compatible local server
        OPENAI_BASE_URL={_LLAMA_CPP_BASE_URL}
        OPENAI_API_KEY={_LLAMA_CPP_API_KEY}

        # Start llama.cpp with one of the configured local models before running MindRoom.
        # {llama_cpp_server_command(LLAMA_CPP_GEMMA)}
        # {llama_cpp_server_command(LLAMA_CPP_QWEN)}
        """).rstrip()

    required_env_key = constants.env_key_for_provider(CONFIG_INIT_MODEL_PRESETS[provider_preset].provider)
    key_placeholders = {
        "ANTHROPIC_API_KEY": "your-anthropic-key-here",
        "OPENAI_API_KEY": "your-openai-key-here",
        "OPENROUTER_API_KEY": "your-openrouter-key-here",
    }
    provider_lines: list[str] = ["# AI provider API keys (set the uncommented keys for this preset)"]
    for env_key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        prefix = "" if env_key == required_env_key else "# "
        provider_lines.append(f"{prefix}{env_key}={key_placeholders[env_key]}")
    return "\n".join(provider_lines)
