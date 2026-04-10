"""Doctor command implementation for MindRoom CLI."""

from __future__ import annotations

import ipaddress
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import typer
from agno.models.vertexai.claude import Claude as VertexAIClaude
from anthropic import APIStatusError
from google.auth.exceptions import DefaultCredentialsError, RefreshError

from mindroom import constants
from mindroom.connections import (
    ResolvedConnection,
    connection_api_key,
    connection_google_application_credentials_path,
    resolve_connection,
)
from mindroom.credentials_sync import sync_env_to_credentials
from mindroom.embeddings import create_sentence_transformers_embedder
from mindroom.matrix.health import matrix_versions_url, response_has_matrix_versions

from .config import _activate_cli_runtime, _load_config_quiet, console

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES, Config, iter_config_validation_messages


def doctor() -> None:
    """Check your environment for common issues.

    Runs connectivity, configuration, and credential checks in a single pass
    so you can fix everything before running `mindroom run`.
    """
    console.print("[bold]MindRoom Doctor[/bold]\n")

    passed = 0
    failed = 0
    warnings = 0

    runtime_paths = _activate_cli_runtime()
    with suppress(OSError):
        sync_env_to_credentials(runtime_paths)
    config_path = runtime_paths.config_path

    # 1. Config file exists
    p, f, w = _run_doctor_step("Checking config file...", lambda: _check_config_exists(config_path))
    passed += p
    failed += f
    warnings += w

    # 2. Config validity (skip if file missing)
    if config_path.exists():
        config, p, f, w = _run_doctor_step(
            "Validating configuration...",
            lambda: _check_config_valid(runtime_paths),
        )
        passed += p
        failed += f
        warnings += w

    # 3. Storage directory writable
    storage_ok = False
    p, f, w = _run_doctor_step("Checking storage...", lambda: _check_storage_writable(runtime_paths))
    passed += p
    failed += f
    warnings += w
    storage_ok = f == 0

    # 4+. Credential-backed config checks only work once storage is available.
    if config_path.exists() and storage_ok and config is not None:
        p, f, w = _run_doctor_step(
            "Checking providers...",
            lambda: _check_providers(config, runtime_paths=runtime_paths),
        )
        passed += p
        failed += f
        warnings += w

        # 4. Memory LLM & embedder
        p, f, w = _run_doctor_step(
            "Checking memory config...",
            lambda: _check_memory_config(config, runtime_paths=runtime_paths),
        )
        passed += p
        failed += f
        warnings += w

        p, f, w = _run_doctor_step(
            "Checking voice config...",
            lambda: _check_voice_config(config, runtime_paths=runtime_paths),
        )
        passed += p
        failed += f
        warnings += w

    # 5. Matrix homeserver reachable
    p, f, w = _run_doctor_step(
        "Checking Matrix homeserver...",
        lambda: _check_matrix_homeserver(runtime_paths=runtime_paths),
    )
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


def _check_config_valid(runtime_paths: RuntimePaths) -> tuple[Config | None, int, int, int]:
    """Validate config file. Returns (config_or_none, passed, failed, warnings)."""
    try:
        config = _load_config_quiet(runtime_paths=runtime_paths)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        issues = "; ".join(f"{location}: {message}" for location, message in iter_config_validation_messages(exc))
        console.print(f"[red]✗[/red] Config invalid: {issues}")
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
_OPENAI_TRANSCRIPTION_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"


def _model_base_url(model_config: ModelConfig) -> str | None:
    """Get one model's custom base_url override, if any."""
    extra_kwargs = model_config.extra_kwargs or {}
    base_url = extra_kwargs.get("base_url")
    if not isinstance(base_url, str):
        return None
    normalized = base_url.strip()
    return normalized or None


def _doctor_connection_api_key(
    connection: ResolvedConnection,
    *,
    label: str,
) -> tuple[str | None, tuple[int, int, int] | None]:
    """Return one connection API key, or the terminal result for auth-free/missing-key cases."""
    if connection.auth_kind == "none":
        console.print(
            f"[green]✓[/green] {label} connection '{connection.connection_id}' auth-free (no key required)",
        )
        return None, (1, 0, 0)

    api_key = connection_api_key(connection)
    if api_key is None:
        console.print(f"[red]✗[/red] {label} connection '{connection.connection_id}' has no API key")
        return None, (0, 1, 0)

    return api_key, None


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


def _is_local_network_host(host: str) -> bool:
    """Return True for .local, loopback, and private-link hosts."""
    normalized_host = host.strip().strip("[]").lower()
    if normalized_host in {"localhost", "127.0.0.1", "::1"} or normalized_host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _with_local_network_hint(detail: str, base_url: str | None) -> str:
    """Append a targeted hint for local host routing failures."""
    if not detail or not base_url:
        return detail

    parsed = urlparse(base_url)
    host = parsed.hostname
    if host is None or not _is_local_network_host(host):
        return detail

    lowered = detail.lower()
    route_signals = (
        "no route to host",
        "connection refused",
        "name or service not known",
        "nodename nor servname provided",
    )
    if not any(signal in lowered for signal in route_signals):
        return detail

    return (
        f"{detail}; local host '{host}' may be unreachable from this Python runtime"
        " (try a reachable LAN IP instead of .local)"
    )


def _validate_openai_embeddings_endpoint(
    api_key: str,
    base_url: str,
    model: str,
) -> tuple[bool | None, str]:
    """Validate a custom OpenAI-compatible embeddings endpoint with a tiny request."""
    url = f"{base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"model": model, "input": "mindroom doctor embedder check"}

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
    except httpx.HTTPError as exc:
        return None, str(exc)

    if not resp.is_success:
        return False, f"HTTP {resp.status_code}"

    error_detail: str | None = None
    try:
        body = resp.json()
    except ValueError:
        error_detail = "invalid JSON response"
    else:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            error_detail = "missing embeddings data"
        else:
            first_item = data[0]
            if not isinstance(first_item, dict):
                error_detail = "invalid embeddings payload"
            else:
                embedding = first_item.get("embedding")
                if not isinstance(embedding, list) or not embedding:
                    error_detail = "empty embedding vector"

    if error_detail is not None:
        return False, error_detail

    return True, ""


def _validate_openai_transcriptions_endpoint(
    api_key: str,
    base_url: str | None,
    model: str,
) -> tuple[bool | None, str]:
    """Validate an OpenAI-compatible STT endpoint with the runtime request path."""
    url = f"{base_url.rstrip('/')}/v1/audio/transcriptions" if base_url else _OPENAI_TRANSCRIPTION_ENDPOINT
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    files = {"file": ("doctor.wav", b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")}
    form_data = {"model": model}

    try:
        resp = httpx.post(url, headers=headers, files=files, data=form_data, timeout=10)
    except httpx.HTTPError as exc:
        return None, str(exc)

    if resp.status_code in {200, 400, 415, 422}:
        return True, ""
    if resp.status_code in {401, 403, 404, 405}:
        return False, f"HTTP {resp.status_code}"
    return None, f"HTTP {resp.status_code}"


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


def _validate_vertexai_claude_connection(  # noqa: C901, PLR0911, PLR0912
    config: Config,
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[bool | None, str]:
    """Validate the configured Vertex AI Claude model with the runtime request path."""
    try:
        connection = resolve_connection(
            config,
            provider=model_config.provider,
            purpose="chat_model",
            connection_name=model_config.connection,
            runtime_paths=runtime_paths,
        )
    except ValueError as exc:
        return False, str(exc)

    google_application_credentials = connection_google_application_credentials_path(connection)
    extra_kwargs = dict(model_config.extra_kwargs or {})
    project_id = extra_kwargs.get("project_id") or runtime_paths.env_value("ANTHROPIC_VERTEX_PROJECT_ID")
    region = extra_kwargs.get("region") or runtime_paths.env_value("CLOUD_ML_REGION")
    missing = []
    if not project_id:
        missing.append("ANTHROPIC_VERTEX_PROJECT_ID")
    if not region:
        missing.append("CLOUD_ML_REGION")
    if missing:
        return None, f"missing {', '.join(missing)}"

    extra_kwargs.setdefault("project_id", project_id)
    extra_kwargs.setdefault("region", region)
    extra_kwargs.setdefault("timeout", 10)

    previous_google_application_credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google_application_credentials is not None:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = google_application_credentials
    try:
        model = VertexAIClaude(id=model_config.id, **extra_kwargs)
        request_kwargs = model.get_request_params().copy()
        request_kwargs["model"] = model_config.id
        request_kwargs["messages"] = [{"role": "user", "content": "Reply with OK."}]
        request_kwargs.setdefault("max_tokens", 1)
        request_kwargs["timeout"] = request_kwargs.get("timeout", 10)
        client = model.get_client()
        client.messages.create(
            **request_kwargs,
        )
    except APIStatusError as exc:
        return False, f"HTTP {exc.status_code}"
    except DefaultCredentialsError as exc:
        return None, str(exc)
    except RefreshError as exc:
        return False, str(exc)
    except (RuntimeError, TypeError, ValueError, httpx.HTTPError) as exc:
        return None, str(exc)
    finally:
        if google_application_credentials is not None:
            if previous_google_application_credentials is None:
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            else:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = previous_google_application_credentials

    return True, ""


def _get_ollama_host(config: Config, runtime_paths: RuntimePaths) -> str:
    """Get the Ollama host from config or environment."""
    for model in config.models.values():
        if model.provider == "ollama" and model.host:
            return model.host
    return runtime_paths.env_value("OLLAMA_HOST", default="http://localhost:11434") or "http://localhost:11434"


def _check_providers(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
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

    for provider in sorted(provider_models):
        p, f, w = _check_single_provider(provider, config, runtime_paths)
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
    runtime_paths: RuntimePaths,
) -> tuple[int, int, int]:
    """Validate a single provider. Returns (passed, failed, warnings)."""
    provider_model_configs = [
        (model_name, model_config)
        for model_name, model_config in config.models.items()
        if model_config.provider == provider
    ]
    if provider == "vertexai_claude":
        passed = 0
        failed = 0
        warnings = 0
        for model_name, model_config in provider_model_configs:
            valid, detail = _validate_vertexai_claude_connection(config, model_config, runtime_paths)
            p, f, w = _print_validation(
                valid,
                detail,
                f"{provider} connection valid for model '{model_name}' ({model_config.id})",
                f"{provider} connection failed for model '{model_name}' ({model_config.id})",
                f"{provider}: could not validate connection for model '{model_name}' ({model_config.id})",
            )
            passed += p
            failed += f
            warnings += w
        return passed, failed, warnings

    if provider == "ollama":
        host = _get_ollama_host(config, runtime_paths=runtime_paths)
        url = f"{host.rstrip('/')}/api/tags"
        valid, detail = _http_check(url)
        return _print_validation(
            valid,
            detail,
            f"{provider} reachable ({host})",
            f"{provider} unreachable: {host}",
            f"{provider}: could not reach {host}",
        )

    passed = 0
    failed = 0
    warnings = 0
    validated_targets: set[tuple[str, str | None]] = set()

    for model_name, model_config in provider_model_configs:
        try:
            connection = resolve_connection(
                config,
                provider=provider,
                purpose="chat_model",
                connection_name=model_config.connection,
                runtime_paths=runtime_paths,
            )
        except ValueError as exc:
            console.print(f"[red]✗[/red] {provider} model '{model_name}' connection error ({exc})")
            failed += 1
            continue

        base_url = _model_base_url(model_config)
        validation_target = (connection.connection_id, base_url)
        if validation_target in validated_targets:
            continue
        validated_targets.add(validation_target)

        api_key, key_result = _doctor_connection_api_key(connection, label=provider)
        if key_result is not None:
            p, f, w = key_result
            passed += p
            failed += f
            warnings += w
            continue
        assert api_key is not None

        valid, detail = _validate_provider_key(provider, api_key, base_url)
        p, f, w = _print_validation(
            valid,
            detail,
            f"{provider} connection '{connection.connection_id}' valid",
            f"{provider} connection '{connection.connection_id}' invalid",
            f"{provider}: could not validate connection '{connection.connection_id}'",
        )
        passed += p
        failed += f
        warnings += w

    return passed, failed, warnings


def _check_memory_config(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory LLM and embedder configuration. Returns (passed, failed, warnings)."""
    if not config.uses_mem0_memory():
        console.print("[green]✓[/green] Memory backend: file (markdown)")
        return 1, 0, 0

    if config.uses_file_memory():
        console.print("[green]✓[/green] Memory backend: mixed (per-agent mem0/file)")

    p1, f1, w1 = _check_memory_llm(config, runtime_paths=runtime_paths)
    p2, f2, w2 = _check_memory_embedder(config, runtime_paths=runtime_paths)
    return p1 + p2, f1 + f2, w1 + w2


def _check_memory_llm(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory LLM configuration. Returns (passed, failed, warnings)."""
    if config.memory.llm is None:
        ollama_host = _get_ollama_host(config, runtime_paths=runtime_paths)
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
    llm_host = (
        config.memory.llm.config.get("host")
        or config.memory.llm.config.get("openai_base_url")
        or config.memory.llm.config.get("base_url")
    )
    if llm_provider == "ollama":
        host = llm_host or _get_ollama_host(config, runtime_paths=runtime_paths)
        valid, detail = _http_check(f"{host.rstrip('/')}/api/tags")
        return _print_validation(
            valid,
            detail,
            f"Memory LLM: ollama reachable ({host})",
            f"Memory LLM: ollama unreachable ({host})",
            f"Memory LLM: could not reach ollama ({host})",
        )

    llm_model = config.memory.llm.config.get("model", "default")
    try:
        connection = resolve_connection(
            config,
            provider=llm_provider,
            purpose="memory_llm",
            connection_name=config.memory.llm.connection,
            runtime_paths=runtime_paths,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] Memory LLM connection error ({exc})")
        return 0, 1, 0
    api_key, key_result = _doctor_connection_api_key(connection, label=f"Memory LLM ({llm_provider})")
    if key_result is not None:
        return key_result
    base_url = llm_host
    valid, detail = _validate_provider_key(llm_provider, api_key or "", base_url)
    return _print_validation(
        valid,
        detail,
        f"Memory LLM: {llm_provider}/{llm_model} API key valid",
        f"Memory LLM: {llm_provider}/{llm_model} API key invalid",
        f"Memory LLM: {llm_provider}/{llm_model} could not validate",
    )


def _check_memory_embedder(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory embedder configuration. Returns (passed, failed, warnings)."""
    emb = config.memory.embedder
    if emb.provider == "ollama":
        host = emb.config.host or _get_ollama_host(config, runtime_paths=runtime_paths)
        valid, detail = _http_check(f"{host.rstrip('/')}/api/tags")
        return _print_validation(
            valid,
            detail,
            f"Memory embedder: ollama reachable ({host})",
            f"Memory embedder: ollama unreachable ({host})",
            f"Memory embedder: could not reach ollama ({host})",
        )

    if emb.provider == "sentence_transformers":
        valid, detail = _validate_sentence_transformers_embedder(runtime_paths, emb.config.model)
        return _print_validation(
            valid,
            detail,
            f"Memory embedder: sentence_transformers/{emb.config.model} local model loaded",
            f"Memory embedder: sentence_transformers/{emb.config.model} local model failed",
            f"Memory embedder: sentence_transformers/{emb.config.model} could not validate",
        )

    try:
        connection = resolve_connection(
            config,
            provider=emb.provider,
            purpose="embedder",
            connection_name=emb.config.connection,
            runtime_paths=runtime_paths,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] Memory embedder connection error ({exc})")
        return 0, 1, 0
    api_key, key_result = _doctor_connection_api_key(connection, label=f"Memory embedder ({emb.provider})")
    if key_result is not None:
        return key_result

    if emb.provider == "openai" and emb.config.host:
        valid, detail = _validate_openai_embeddings_endpoint(api_key or "", emb.config.host, emb.config.model)
        return _print_validation(
            valid,
            _with_local_network_hint(detail, emb.config.host),
            f"Memory embedder: openai/{emb.config.model} embeddings endpoint reachable ({emb.config.host})",
            f"Memory embedder: openai/{emb.config.model} embeddings endpoint failed ({emb.config.host})",
            f"Memory embedder: openai/{emb.config.model} could not reach embeddings endpoint ({emb.config.host})",
        )

    base_url = emb.config.host
    valid, detail = _validate_provider_key(emb.provider, api_key or "", base_url)
    return _print_validation(
        valid,
        detail,
        f"Memory embedder: {emb.provider}/{emb.config.model} API key valid",
        f"Memory embedder: {emb.provider}/{emb.config.model} API key invalid",
        f"Memory embedder: {emb.provider}/{emb.config.model} could not validate",
    )


def _check_voice_config(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check voice STT configuration. Returns (passed, failed, warnings)."""
    if not config.voice.enabled:
        console.print("[green]✓[/green] Voice: disabled")
        return 1, 0, 0

    stt_provider = config.voice.stt.provider
    if stt_provider == "ollama":
        host = config.voice.stt.host or _get_ollama_host(config, runtime_paths=runtime_paths)
        valid, detail = _http_check(f"{host.rstrip('/')}/api/tags")
        return _print_validation(
            valid,
            detail,
            f"Voice STT: ollama reachable ({host})",
            f"Voice STT: ollama unreachable ({host})",
            f"Voice STT: could not reach ollama ({host})",
        )

    try:
        connection = resolve_connection(
            config,
            provider=stt_provider,
            purpose="voice_stt",
            connection_name=config.voice.stt.connection,
            runtime_paths=runtime_paths,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] Voice STT connection error ({exc})")
        return 0, 1, 0

    api_key, key_result = _doctor_connection_api_key(connection, label=f"Voice STT ({stt_provider})")
    if key_result is not None:
        return key_result
    assert api_key is not None

    base_url = config.voice.stt.host
    valid, detail = _validate_openai_transcriptions_endpoint(api_key, base_url, config.voice.stt.model)
    return _print_validation(
        valid,
        _with_local_network_hint(detail, base_url),
        f"Voice STT: {stt_provider}/{config.voice.stt.model} endpoint reachable",
        f"Voice STT: {stt_provider}/{config.voice.stt.model} endpoint failed",
        f"Voice STT: {stt_provider}/{config.voice.stt.model} could not reach transcription endpoint",
    )


def _validate_sentence_transformers_embedder(runtime_paths: RuntimePaths, model: str) -> tuple[bool, str]:
    """Validate a local sentence-transformers model with a tiny embedding request."""
    try:
        embedder = create_sentence_transformers_embedder(runtime_paths, model)
        embedding = embedder.get_embedding("mindroom doctor embedder check")
    except Exception as exc:
        return False, str(exc)

    if not isinstance(embedding, list) or not embedding:
        return False, "empty embedding vector"
    return True, ""


def _check_matrix_homeserver(runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check Matrix homeserver reachability. Returns (passed, failed, warnings)."""
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    url = matrix_versions_url(homeserver)
    try:
        response = httpx.get(url, timeout=5, verify=constants.runtime_matrix_ssl_verify(runtime_paths=runtime_paths))
    except httpx.TransportError as exc:
        console.print(f"[red]✗[/red] Matrix homeserver unreachable: {homeserver} ({exc})")
        return 0, 1, 0
    if response_has_matrix_versions(response):
        console.print(f"[green]✓[/green] Matrix homeserver: {homeserver}")
        return 1, 0, 0
    detail = f"HTTP {response.status_code}" if not response.is_success else "returned invalid /versions payload"
    console.print(f"[red]✗[/red] Matrix homeserver {detail}: {homeserver}")
    return 0, 1, 0


def _check_storage_writable(runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check storage directory is writable. Returns (passed, failed, warnings)."""
    storage = runtime_paths.storage_root
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
