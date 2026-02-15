"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import typer
import yaml
from pydantic import ValidationError

from mindroom import __version__

if TYPE_CHECKING:
    from mindroom.config import Config
from mindroom.cli_config import (
    _check_env_keys,
    _format_validation_errors,
    _load_config_quiet,
    config_app,
    console,
)
from mindroom.constants import (
    DEFAULT_AGENTS_CONFIG,
    MATRIX_HOMESERVER,
    MATRIX_SSL_VERIFY,
    STORAGE_PATH,
    config_search_locations,
    env_key_for_provider,
)

app = typer.Typer(
    help="MindRoom - AI agents that live in Matrix\n\nQuick start:\n  mindroom config init   Create a starter config\n  mindroom run           Start the system",
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
app.add_typer(config_app, name="config")


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
    config_path = Path(DEFAULT_AGENTS_CONFIG)
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

    config_path = Path(DEFAULT_AGENTS_CONFIG)

    # 1. Config file exists
    p, f, w = _check_config_exists(config_path)
    passed += p
    failed += f
    warnings += w

    # 2+. Config validity + provider API key validation (skip if file missing)
    if config_path.exists():
        config, p, f, w = _check_config_valid(config_path)
        passed += p
        failed += f
        warnings += w
        if config is not None:
            p, f, w = _check_providers(config)
            passed += p
            failed += f
            warnings += w

    # 4. Matrix homeserver reachable
    p, f, w = _check_matrix_homeserver()
    passed += p
    failed += f
    warnings += w

    # 5. Storage directory writable
    p, f, w = _check_storage_writable()
    passed += p
    failed += f
    warnings += w

    # Summary
    console.print(f"\n{passed} passed, {failed} failed, {warnings} warning{'s' if warnings != 1 else ''}")

    if failed > 0:
        raise typer.Exit(1)


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


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error() -> None:
    console.print("[red]Error:[/red] No config.yaml found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a starter config")
    console.print("  [cyan]mindroom config edit[/cyan]    Edit your config\n")
    console.print("Config search locations:")
    for loc in config_search_locations():
        status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
        console.print(f"  - {loc} ({status})")
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
    # Handle -h flag by replacing with --help
    for i, arg in enumerate(sys.argv):
        if arg == "-h":
            sys.argv[i] = "--help"
            break

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append("--help")

    app()


if __name__ == "__main__":
    main()
