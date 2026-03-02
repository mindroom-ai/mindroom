# ruff: noqa: INP001
"""Doctor command implementation for MindRoom CLI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import typer
import yaml
from pydantic import ValidationError

from mindroom.constants import (
    CONFIG_PATH,
    MATRIX_HOMESERVER,
    MATRIX_SSL_VERIFY,
    STORAGE_PATH,
    env_key_for_provider,
)

from .config import _load_config_quiet, console

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config


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
    if not config.uses_mem0_memory():
        console.print("[green]✓[/green] Memory backend: file (markdown)")
        return 1, 0, 0

    if config.uses_file_memory():
        console.print("[green]✓[/green] Memory backend: mixed (per-agent mem0/file)")

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
