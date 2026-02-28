"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import typer
import yaml
from pydantic import ValidationError

from mindroom import __version__
from mindroom.cli_banner import make_banner

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config import Config
from mindroom import cli_connect
from mindroom.cli_config import (
    _check_env_keys,
    _format_validation_errors,
    _load_config_quiet,
    config_app,
    console,
)
from mindroom.constants import (
    CONFIG_PATH,
    MATRIX_HOMESERVER,
    MATRIX_SSL_VERIFY,
    STORAGE_PATH,
    config_search_locations,
    env_key_for_provider,
)

_HELP = """\
AI agents that live in Matrix and work everywhere via bridges.

[bold]Quick start:[/bold]
  [cyan]mindroom config init[/cyan]   Create a starter config
  [cyan]mindroom run[/cyan]           Start the system\
"""

app = typer.Typer(
    help=_HELP,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
app.add_typer(config_app, name="config")

_CINNY_DEFAULT_IMAGE = "ghcr.io/mindroom-ai/mindroom-cinny:latest"
_CINNY_DEFAULT_CONTAINER = "mindroom-cinny-local"


@app.command()
def version() -> None:
    """Show the current version of Mindroom."""
    console.print(f"Mindroom version: [bold]{__version__}[/bold]")
    console.print("AI agents that live in Matrix")


@app.command()
def run(
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR)",
        case_sensitive=False,
        envvar="LOG_LEVEL",
    ),
    storage_path: Path = typer.Option(  # noqa: B008
        Path(STORAGE_PATH),
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data (state, sessions, tracking)",
    ),
    api: bool = typer.Option(
        True,
        "--api/--no-api",
        help="Start the dashboard API server alongside the bot",
    ),
    api_port: int = typer.Option(
        8765,
        "--api-port",
        help="Port for the dashboard API server",
    ),
    api_host: str = typer.Option(
        "0.0.0.0",  # noqa: S104
        "--api-host",
        help="Host for the dashboard API server",
    ),
) -> None:
    """Run the mindroom multi-agent system.

    This command starts the multi-agent bot system which automatically:
    - Creates all necessary user and agent accounts
    - Creates all rooms defined in config.yaml
    - Manages agent room memberships
    - Starts the dashboard API server (disable with --no-api)
    """
    asyncio.run(
        _run(
            log_level=log_level.upper(),
            storage_path=storage_path,
            api=api,
            api_port=api_port,
            api_host=api_host,
        ),
    )


async def _run(
    log_level: str,
    storage_path: Path,
    *,
    api: bool,
    api_port: int,
    api_host: str,
) -> None:
    """Run the multi-agent system with friendly error handling."""
    # Check config exists before starting
    config_path = Path(CONFIG_PATH)
    if not config_path.exists():
        _print_missing_config_error()
        raise typer.Exit(1)

    # Validate config early so users get a clear message instead of a traceback
    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        _format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None
    except (yaml.YAMLError, OSError) as exc:
        console.print(f"[red]Error:[/red] Could not load configuration: {exc}")
        console.print("\n  [cyan]mindroom config validate[/cyan]  Check your config")
        raise typer.Exit(1) from None

    # Check for missing API keys
    _check_env_keys(config)

    console.print(make_banner())
    console.print()
    console.print(f"Starting Mindroom (log level: {log_level})...")
    if api:
        console.print(f"Dashboard API: http://{api_host}:{api_port}")
    console.print("Press Ctrl+C to stop\n")

    try:
        from mindroom.bot import main as bot_main  # noqa: PLC0415  # lazy: heavy import

        await bot_main(
            log_level=log_level,
            storage_path=storage_path,
            api=api,
            api_port=api_port,
            api_host=api_host,
        )
    except KeyboardInterrupt:
        console.print("\nStopped")
    except ConnectionError as exc:
        _print_connection_error(exc)
        raise typer.Exit(1) from None
    except OSError as exc:
        if "connect" in str(exc).lower() or "refused" in str(exc).lower():
            _print_connection_error(exc)
            raise typer.Exit(1) from None
        raise


@app.command()
def doctor() -> None:
    """Check your environment for common issues.

    Runs connectivity, configuration, and credential checks in a single pass
    so you can fix everything before running `mindroom run`.
    """
    console.print("[bold]MindRoom Doctor[/bold]\n")

    passed = 0
    failed = 0
    warnings = 0

    config_path = Path(CONFIG_PATH)

    # 1. Config file exists
    p, f, w = _run_doctor_step("Checking config file...", lambda: _check_config_exists(config_path))
    passed += p
    failed += f
    warnings += w

    # 2+. Config validity + provider API key validation (skip if file missing)
    if config_path.exists():
        config, p, f, w = _run_doctor_step(
            "Validating configuration...",
            lambda: _check_config_valid(config_path),
        )
        passed += p
        failed += f
        warnings += w
        if config is not None:
            p, f, w = _run_doctor_step("Checking providers...", lambda: _check_providers(config))
            passed += p
            failed += f
            warnings += w

            # 4. Memory LLM & embedder
            p, f, w = _run_doctor_step(
                "Checking memory config...",
                lambda: _check_memory_config(config),
            )
            passed += p
            failed += f
            warnings += w

    # 5. Matrix homeserver reachable
    p, f, w = _run_doctor_step("Checking Matrix homeserver...", _check_matrix_homeserver)
    passed += p
    failed += f
    warnings += w

    # 6. Storage directory writable
    p, f, w = _run_doctor_step("Checking storage...", _check_storage_writable)
    passed += p
    failed += f
    warnings += w

    # Summary
    console.print(f"\n{passed} passed, {failed} failed, {warnings} warning{'s' if warnings != 1 else ''}")

    if failed > 0:
        raise typer.Exit(1)


@app.command()
def connect(
    pair_code: str = typer.Option(
        ...,
        "--pair-code",
        help="Pair code shown in chat UI (format: ABCD-EFGH).",
    ),
    provisioning_url: str | None = typer.Option(
        None,
        "--provisioning-url",
        help="Base URL for the MindRoom provisioning API.",
    ),
    client_name: str = typer.Option(
        socket.gethostname(),
        "--client-name",
        help="Human-readable name for this local machine.",
    ),
    persist_env: bool = typer.Option(
        True,
        "--persist-env/--no-persist-env",
        help="Persist local provisioning credentials to .env next to config.yaml.",
    ),
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        "-p",
        help="Override auto-detection and use this config file path for .env persistence.",
    ),
) -> None:
    """Pair this local MindRoom install with the hosted provisioning service."""
    normalized_pair_code = pair_code.strip().upper()
    if not cli_connect.is_valid_pair_code(normalized_pair_code):
        console.print("[red]Error:[/red] Invalid pair code format. Expected ABCD-EFGH.")
        raise typer.Exit(1)

    resolved_provisioning_url = (
        provisioning_url or os.getenv("MINDROOM_PROVISIONING_URL", "https://mindroom.chat")
    ).strip()
    if not resolved_provisioning_url:
        console.print("[red]Error:[/red] Invalid provisioning URL.")
        raise typer.Exit(1)

    resolved_config_path = (path or Path(CONFIG_PATH)).expanduser().resolve()
    normalized_client_name = client_name.strip() or socket.gethostname()
    try:
        credentials = cli_connect.complete_local_pairing(
            provisioning_url=resolved_provisioning_url,
            pair_code=normalized_pair_code,
            client_name=normalized_client_name,
            client_fingerprint=_local_client_fingerprint(config_path=resolved_config_path),
            matrix_ssl_verify=MATRIX_SSL_VERIFY,
            post_request=httpx.post,
        )
    except (TypeError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None

    if credentials.owner_user_id_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed owner_user_id; skipping config owner autofill.",
        )

    if persist_env:
        env_path = cli_connect.persist_local_provisioning_env(
            provisioning_url=resolved_provisioning_url,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            config_path=resolved_config_path,
        )
        console.print("[green]Paired successfully.[/green]")
        console.print(f"  Saved credentials to: {env_path}")
        if credentials.owner_user_id and cli_connect.replace_owner_placeholders_in_config(
            config_path=resolved_config_path,
            owner_user_id=credentials.owner_user_id,
        ):
            console.print(f"  Updated owner placeholder(s) in: {resolved_config_path}")
        console.print("\nNext step:")
        console.print("  uv run mindroom run")
        return

    _print_pairing_success_with_exports(
        provisioning_url=resolved_provisioning_url,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        owner_user_id=credentials.owner_user_id,
    )


@app.command("local-stack-setup")
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


def _run_doctor_step[T](message: str, check: Callable[[], T]) -> T:
    """Run one doctor step with a minimal terminal spinner."""
    with console.status(f"[dim]{message}[/dim]", spinner="dots"):
        return check()


def _check_config_exists(config_path: Path) -> tuple[int, int, int]:
    """Check config file exists. Returns (passed, failed, warnings)."""
    if config_path.exists():
        console.print(f"[green]✓[/green] Config file: {config_path}")
        return 1, 0, 0
    console.print(f"[red]✗[/red] Config file not found: {config_path}")
    return 0, 1, 0


def _check_config_valid(config_path: Path) -> tuple[Config | None, int, int, int]:
    """Validate config file. Returns (config_or_none, passed, failed, warnings)."""
    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        n = len(exc.errors())
        console.print(f"[red]✗[/red] Config invalid ({n} validation error{'s' if n != 1 else ''})")
        return None, 0, 1, 0
    except (yaml.YAMLError, OSError) as exc:
        console.print(f"[red]✗[/red] Config invalid: {exc}")
        return None, 0, 1, 0
    agents = len(config.agents)
    teams = len(config.teams)
    models = len(config.models)
    rooms = len(config.get_all_configured_rooms())
    console.print(
        f"[green]✓[/green] Config valid"
        f" ({agents} agent{'s' if agents != 1 else ''},"
        f" {teams} team{'s' if teams != 1 else ''},"
        f" {models} model{'s' if models != 1 else ''},"
        f" {rooms} room{'s' if rooms != 1 else ''})",
    )
    return config, 1, 0, 0


_PROVIDER_VALIDATE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "deepseek": "https://api.deepseek.com/v1/models",
    "cerebras": "https://api.cerebras.ai/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
}


def _get_custom_base_url(config: Config, provider: str) -> str | None:
    """Get custom base_url for a provider from model extra_kwargs, if any."""
    for model in config.models.values():
        if model.provider == provider and model.extra_kwargs:
            base_url = model.extra_kwargs.get("base_url")
            if base_url:
                return base_url
    return None


def _http_check(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    verify: bool = True,
) -> tuple[bool | None, str]:
    """Make a lightweight GET request and return (True, ""), (False, reason), or (None, reason)."""
    try:
        resp = httpx.get(url, headers=headers or {}, timeout=5, verify=verify)
    except httpx.HTTPError as exc:
        return None, str(exc)
    if resp.is_success:
        return True, ""
    return False, f"HTTP {resp.status_code}"


def _validate_provider_key(
    provider: str,
    api_key: str,
    base_url: str | None = None,
) -> tuple[bool | None, str]:
    """Validate an API key with a lightweight models-list request.

    Returns (True, "") if valid, (False, reason) if invalid,
    (None, reason) if inconclusive (e.g. connection error).
    """
    # Normalize aliases so we look up a single URL and auth style
    canonical = "google" if provider == "gemini" else provider

    if base_url:
        url = base_url.rstrip("/") + "/models"
    elif canonical in _PROVIDER_VALIDATE_URLS:
        url = _PROVIDER_VALIDATE_URLS[canonical]
    else:
        return None, "unknown provider"

    headers: dict[str, str] = {}
    if canonical == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif canonical == "google":
        url = f"{url}?key={api_key}"
    else:
        headers = {"Authorization": f"Bearer {api_key}"}

    return _http_check(url, headers)


def _get_ollama_host(config: Config) -> str:
    """Get the Ollama host from config or environment."""
    for model in config.models.values():
        if model.provider == "ollama" and model.host:
            return model.host
    return os.getenv("OLLAMA_HOST", "http://localhost:11434")


def _check_providers(config: Config) -> tuple[int, int, int]:
    """Print provider summary and validate API keys. Returns (passed, failed, warnings)."""
    provider_models: dict[str, list[str]] = {}
    for name, model in config.models.items():
        provider_models.setdefault(model.provider, []).append(name)

    if not provider_models:
        return 0, 0, 0

    # Print provider summary
    parts = []
    for provider in sorted(provider_models):
        n = len(provider_models[provider])
        parts.append(f"{provider} ({n} model{'s' if n != 1 else ''})")
    console.print(f"  Providers: {', '.join(parts)}")

    passed = 0
    failed = 0
    warnings = 0
    validated_keys: set[str] = set()

    for provider in sorted(provider_models):
        p, f, w = _check_single_provider(provider, config, validated_keys)
        passed += p
        failed += f
        warnings += w

    return passed, failed, warnings


def _print_validation(
    valid: bool | None,
    detail: str,
    pass_msg: str,
    fail_msg: str,
    warn_msg: str,
) -> tuple[int, int, int]:
    """Print a tri-state validation result. Returns (passed, failed, warnings)."""
    if valid is True:
        console.print(f"[green]✓[/green] {pass_msg}")
        return 1, 0, 0
    if valid is False:
        console.print(f"[red]✗[/red] {fail_msg} ({detail})")
        return 0, 1, 0
    console.print(f"[yellow]![/yellow] {warn_msg} ({detail})")
    return 0, 0, 1


def _check_single_provider(
    provider: str,
    config: Config,
    validated_keys: set[str],
) -> tuple[int, int, int]:
    """Validate a single provider. Returns (passed, failed, warnings)."""
    if provider == "ollama":
        host = _get_ollama_host(config)
        url = f"{host.rstrip('/')}/api/tags"
        valid, detail = _http_check(url)
        return _print_validation(
            valid,
            detail,
            f"{provider} reachable ({host})",
            f"{provider} unreachable: {host}",
            f"{provider}: could not reach {host}",
        )

    env_key = env_key_for_provider(provider)
    if not env_key:
        return 0, 0, 0

    # google and gemini share GOOGLE_API_KEY — validate once
    if env_key in validated_keys:
        return 0, 0, 0
    validated_keys.add(env_key)

    api_key = os.getenv(env_key)
    if not api_key:
        console.print(f"[yellow]![/yellow] {provider}: {env_key} not set")
        return 0, 0, 1

    base_url = _get_custom_base_url(config, provider)
    valid, detail = _validate_provider_key(provider, api_key, base_url)
    return _print_validation(
        valid,
        detail,
        f"{provider} API key valid",
        f"{provider} API key invalid",
        f"{provider}: could not validate key",
    )


def _check_memory_config(config: Config) -> tuple[int, int, int]:
    """Check memory LLM and embedder configuration. Returns (passed, failed, warnings)."""
    if config.memory.backend == "file":
        console.print("[green]✓[/green] Memory backend: file (markdown)")
        return 1, 0, 0

    p1, f1, w1 = _check_memory_llm(config)
    p2, f2, w2 = _check_memory_embedder(config)
    return p1 + p2, f1 + f2, w1 + w2


def _check_memory_llm(config: Config) -> tuple[int, int, int]:
    """Check memory LLM configuration. Returns (passed, failed, warnings)."""
    if config.memory.llm is None:
        ollama_host = _get_ollama_host(config)
        console.print(
            "[yellow]![/yellow] Memory LLM not configured"
            f" (defaults to ollama at {ollama_host};"
            " see memory/config.py fallback)",
        )
        # Check if default Ollama is reachable
        valid, detail = _http_check(f"{ollama_host.rstrip('/')}/api/tags")
        if valid is not True:
            console.print(
                f"[red]✗[/red] Default Ollama for memory LLM unreachable ({ollama_host}: {detail})",
            )
            return 0, 1, 0
        return 0, 0, 1

    llm_provider = config.memory.llm.provider
    llm_host = config.memory.llm.config.get("host")
    if llm_provider == "ollama":
        host = llm_host or _get_ollama_host(config)
        valid, detail = _http_check(f"{host.rstrip('/')}/api/tags")
        return _print_validation(
            valid,
            detail,
            f"Memory LLM: ollama reachable ({host})",
            f"Memory LLM: ollama unreachable ({host})",
            f"Memory LLM: could not reach ollama ({host})",
        )

    llm_model = config.memory.llm.config.get("model", "default")
    env_key = env_key_for_provider(llm_provider)
    api_key = os.getenv(env_key) if env_key else None
    if env_key and not api_key:
        console.print(
            f"[yellow]![/yellow] Memory LLM ({llm_provider}): {env_key} not set",
        )
        return 0, 0, 1
    base_url = llm_host
    valid, detail = _validate_provider_key(llm_provider, api_key or "", base_url)
    return _print_validation(
        valid,
        detail,
        f"Memory LLM: {llm_provider}/{llm_model} API key valid",
        f"Memory LLM: {llm_provider}/{llm_model} API key invalid",
        f"Memory LLM: {llm_provider}/{llm_model} could not validate",
    )


def _check_memory_embedder(config: Config) -> tuple[int, int, int]:
    """Check memory embedder configuration. Returns (passed, failed, warnings)."""
    emb = config.memory.embedder
    if emb.provider == "ollama":
        host = emb.config.host or _get_ollama_host(config)
        valid, detail = _http_check(f"{host.rstrip('/')}/api/tags")
        return _print_validation(
            valid,
            detail,
            f"Memory embedder: ollama reachable ({host})",
            f"Memory embedder: ollama unreachable ({host})",
            f"Memory embedder: could not reach ollama ({host})",
        )

    env_key = env_key_for_provider(emb.provider)
    api_key = os.getenv(env_key) if env_key else None
    if env_key and not api_key:
        console.print(
            f"[yellow]![/yellow] Memory embedder ({emb.provider}): {env_key} not set",
        )
        return 0, 0, 1
    base_url = emb.config.host
    valid, detail = _validate_provider_key(emb.provider, api_key or "", base_url)
    return _print_validation(
        valid,
        detail,
        f"Memory embedder: {emb.provider}/{emb.config.model} API key valid",
        f"Memory embedder: {emb.provider}/{emb.config.model} API key invalid",
        f"Memory embedder: {emb.provider}/{emb.config.model} could not validate",
    )


def _check_matrix_homeserver() -> tuple[int, int, int]:
    """Check Matrix homeserver reachability. Returns (passed, failed, warnings)."""
    url = f"{MATRIX_HOMESERVER}/_matrix/client/versions"
    valid, detail = _http_check(url, verify=MATRIX_SSL_VERIFY)
    if valid is True:
        console.print(f"[green]✓[/green] Matrix homeserver: {MATRIX_HOMESERVER}")
        return 1, 0, 0
    if valid is False:
        console.print(f"[red]✗[/red] Matrix homeserver {detail}: {MATRIX_HOMESERVER}")
        return 0, 1, 0
    console.print(f"[red]✗[/red] Matrix homeserver unreachable: {MATRIX_HOMESERVER} ({detail})")
    return 0, 1, 0


def _check_storage_writable() -> tuple[int, int, int]:
    """Check storage directory is writable. Returns (passed, failed, warnings)."""
    storage = Path(STORAGE_PATH)
    try:
        storage.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=storage)
        os.close(fd)
        Path(tmp).unlink()
    except OSError as exc:
        console.print(f"[red]✗[/red] Storage not writable: {storage} ({exc})")
        return 0, 1, 0
    console.print(f"[green]✓[/green] Storage writable: {storage}/")
    return 1, 0, 0


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


def _print_pairing_success_with_exports(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    owner_user_id: str | None,
) -> None:
    """Print non-persisted exports for local provisioning credentials."""
    console.print("[green]Paired successfully.[/green]")
    console.print("\nExport these variables before running MindRoom:")
    console.print(f"  export MINDROOM_PROVISIONING_URL={provisioning_url}")
    console.print(f"  export MINDROOM_LOCAL_CLIENT_ID={client_id}")
    console.print(f"  export MINDROOM_LOCAL_CLIENT_SECRET={client_secret}")
    if owner_user_id:
        console.print(
            f"\nOwner user ID from pairing: {owner_user_id} (not persisted in --no-persist-env mode).",
        )
        console.print(
            "Update your config.yaml owner placeholder(s) manually if you rely on authorization defaults.",
        )
    console.print("\nThen run:")
    console.print("  uv run mindroom run")


def _local_client_fingerprint(*, config_path: Path | None = None) -> str:
    """Return a stable, non-secret local fingerprint."""
    resolved_config_path = (config_path or Path(CONFIG_PATH)).expanduser().resolve()
    raw = f"{socket.gethostname()}:{resolved_config_path}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error() -> None:
    console.print("[red]Error:[/red] No config.yaml found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a starter config")
    console.print("  [cyan]mindroom config edit[/cyan]    Edit your config\n")
    console.print("Config search locations (first match wins):")
    for i, loc in enumerate(config_search_locations(), 1):
        status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
        console.print(f"  {i}. {loc} ({status})")
    console.print("\nLearn more: https://github.com/mindroom-ai/mindroom")


def _print_connection_error(exc: BaseException) -> None:
    from mindroom.constants import MATRIX_HOMESERVER  # noqa: PLC0415

    console.print("[red]Error:[/red] Could not connect to the Matrix homeserver.\n")
    console.print(f"  Details: {exc}\n")
    console.print("Check that:")
    console.print("  1. Your Matrix homeserver is running")
    console.print(f"  2. MATRIX_HOMESERVER is set correctly (current: {MATRIX_HOMESERVER})")
    console.print("  3. The server is reachable from this machine")


def main() -> None:
    """Main entry point that shows help by default."""
    # Print banner for top-level help (no subcommand given)
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        console.print(
            make_banner(tagline=("💊 What if I told you... ", "AI agents live in Matrix.")),
        )

    app()


if __name__ == "__main__":
    main()
