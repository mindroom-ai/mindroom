"""Doctor command implementation for MindRoom CLI."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import typer
from rich.markup import escape

from mindroom import constants
from mindroom.config.resolution import memory_backend
from mindroom.constants import RuntimePaths, env_key_for_provider
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.credentials.sync import provider_key_with_source
from mindroom.embeddings.embedder_health import probe_embedder, semantic_embedder_configured
from mindroom.embeddings.errors import EMBEDDER_UNREACHABLE_DETAIL
from mindroom.embeddings.factory import resolve_embedder_settings
from mindroom.embeddings.sentence_transformers_embedder import create_sentence_transformers_embedder
from mindroom.llm.errors import AuthenticationFailed, ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, complete
from mindroom.matrix.health import (
    MSC4186_UNSTABLE_FEATURE,
    matrix_versions_url,
    response_advertises_sliding_sync,
    response_has_matrix_versions,
)
from mindroom.matrix.provisioning_env import (
    local_pairing_required,
    local_provisioning_client_credentials_from_env,
    provisioning_url_from_env,
    registration_token_from_env,
)
from mindroom.providers.credentials import model_uses_own_credential
from mindroom.providers.key_checks import PROVIDER_KEY_CHECKS
from mindroom.providers.names import provider_route
from mindroom.providers.ollama.settings import ollama_client_options
from mindroom.runtime_env_policy import VERTEXAI_CLAUDE_ENV_BY_KEY
from mindroom.startup_errors import PermanentStartupError

from .config import (
    activate_cli_runtime,
    automation_reference_issues,
    console,
    find_missing_env_variables,
    load_config_quiet,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from google.auth.exceptions import DefaultCredentialsError

    from mindroom.config.models import ModelConfig

from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES, Config, iter_config_validation_messages


def doctor(config_path: Path | None = None, storage_path: Path | None = None) -> None:
    """Check your environment for common issues.

    Runs connectivity, configuration, and credential checks in a single pass
    so you can fix everything before running `mindroom run`.
    """
    console.print("[bold]MindRoom Doctor[/bold]\n")

    totals = [0, 0, 0]  # passed, failed, warnings

    def add(counts: Sequence[int]) -> None:
        totals[:] = [total + count for total, count in zip(totals, counts, strict=True)]

    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    config_path = runtime_paths.config_path
    console.print(f"[dim]Config directory: {runtime_paths.config_dir}[/dim]")

    # Open the credential store before the checks so a failure to open it is reported once, here.
    # Credential-store failures must not abort the remaining diagnostics, but
    # they are counted because the general storage check may target another path.
    try:
        get_runtime_credentials_manager(runtime_paths)
    except (OSError, ValueError) as exc:
        console.print(f"[yellow]![/yellow] Could not open the credential store ({exc})")
        add((0, 0, 1))

    # 1. Config file exists
    add(_run_doctor_step("Checking config file...", lambda: _check_config_exists(config_path)))

    # 2+. Config validity + provider API key validation (skip if file missing)
    config: Config | None = None
    if config_path.is_file():
        config, *counts = _run_doctor_step("Validating configuration...", lambda: _check_config_valid(runtime_paths))
        add(counts)
        if config is not None:
            # 3-4. Providers, then the memory LLM & embedder
            for message, check in (
                ("Checking providers...", lambda: _check_providers(config, runtime_paths=runtime_paths)),
                ("Checking memory config...", lambda: _check_memory_config(config, runtime_paths=runtime_paths)),
            ):
                add(_run_doctor_step(message, check))

    # 5+. Matrix homeserver, hosted pairing, storage, and encryption stores
    for message, check in (
        ("Checking Matrix homeserver...", lambda: _check_matrix_homeserver(runtime_paths=runtime_paths, config=config)),
        ("Checking pairing...", lambda: _check_pairing(runtime_paths)),
        ("Checking storage...", lambda: _check_storage_writable(runtime_paths)),
        ("Checking encryption stores...", lambda: _check_e2ee_stores(runtime_paths)),
    ):
        add(_run_doctor_step(message, check))

    # Summary
    passed, failed, warnings = totals
    console.print(f"\n{passed} passed, {failed} failed, {warnings} warning{'s' if warnings != 1 else ''}")

    if failed > 0:
        raise typer.Exit(1)


def _check_e2ee_stores(runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check bots' persisted Matrix devices have their encryption stores on disk.

    The internal user joins rooms through an HTTP-only client, so its device never has a store.
    """
    from mindroom.matrix.client_session import olm_store_exists  # noqa: PLC0415
    from mindroom.matrix.state import matrix_state_for_runtime  # noqa: PLC0415
    from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY  # noqa: PLC0415

    state = matrix_state_for_runtime(runtime_paths)
    accounts_with_devices = [
        (key, account)
        for key, account in state.accounts.items()
        if key != INTERNAL_USER_ACCOUNT_KEY and account.device_id and account.domain
    ]
    if not accounts_with_devices:
        console.print("[green]✓[/green] Encryption stores: no persisted Matrix devices yet")
        return 1, 0, 0

    missing = [
        (key, account)
        for key, account in accounts_with_devices
        if not olm_store_exists(f"@{account.username}:{account.domain}", account.device_id or "", runtime_paths)
    ]
    if not missing:
        count = len(accounts_with_devices)
        console.print(f"[green]✓[/green] Encryption stores: {count} device store{'s' if count != 1 else ''} present")
        return 1, 0, 0

    for key, account in missing:
        console.print(
            f"[yellow]⚠[/yellow] Encryption store missing for {key} "
            f"(@{account.username}:{account.domain}, device {account.device_id}); "
            "that bot does not start until its store is restored from a backup",
        )
    return 0, 0, len(missing)


def _run_doctor_step[T](message: str, check: Callable[[], T]) -> T:
    """Run one doctor step with a minimal terminal spinner."""
    with console.status(f"[dim]{message}[/dim]", spinner="dots"):
        return check()


def _check_config_exists(config_path: Path) -> tuple[int, int, int]:
    """Check config file exists. Returns (passed, failed, warnings)."""
    if config_path.is_file():
        console.print(f"[green]✓[/green] Config file: {config_path}")
        return 1, 0, 0
    if config_path.exists():
        console.print(f"[red]✗[/red] Config path is not a file: {config_path}")
        return 0, 1, 0
    console.print(f"[red]✗[/red] Config file not found: {config_path}")
    return 0, 1, 0


def _check_config_valid(runtime_paths: RuntimePaths) -> tuple[Config | None, int, int, int]:
    """Validate the config file. Returns (config, passed, failed, warnings).

    The config is None when it does not load. A config with bad automation references still comes back, with one
    failure, so the remaining checks run against it.
    """
    try:
        config = load_config_quiet(runtime_paths=runtime_paths)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        issues = "; ".join(f"{location}: {message}" for location, message in iter_config_validation_messages(exc))
        # A message can quote a config key, which may contain brackets Rich reads as markup.
        console.print(f"[red]✗[/red] Config invalid: {escape(issues)}")
        return None, 0, 1, 0
    if reference_issues := automation_reference_issues(config, runtime_paths):
        # The config loads, so the remaining checks still run against it.
        console.print(f"[red]✗[/red] Config invalid: {escape('; '.join(reference_issues))}")
        return config, 0, 1, 0
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


def _models_reading_shared_key(config: Config, env_key: str, runtime_paths: RuntimePaths) -> dict[str, ModelConfig]:
    """Return the models, by name, that may read the shared ``env_key``: all of its models if the store is unreadable."""
    models = {name: model for name, model in config.models.items() if env_key_for_provider(model.provider) == env_key}
    try:
        return {
            name: model for name, model in models.items() if not model_uses_own_credential(name, model, runtime_paths)
        }
    except (OSError, ValueError):
        return models


def _shared_key(provider: str, env_key: str, runtime_paths: RuntimePaths) -> tuple[str | None, str]:
    """Return the shared key the runtime reads for ``provider``, if any, and the name doctor reports it by.

    The runtime reads it from the credential store, where a key saved in the dashboard wins over the environment. The
    name is the variable that supplies the key, which may be a credential seed's, or says that the key was saved in the
    dashboard. Without the store, doctor reads ``env_key`` itself.
    """
    try:
        api_key, variable = provider_key_with_source(provider, runtime_paths)
    except (OSError, ValueError):
        return get_secret_from_env(env_key, runtime_paths=runtime_paths), env_key
    return api_key, variable or "key saved in the dashboard"


def _endpoint_settings(value: object, path: str) -> list[str]:
    """Return the path of each setting under ``value`` that can point a client at another server or credential.

    Any set key whose name contains ``base``, ``host``, ``endpoint``, or ``header`` counts, at any depth, so doctor
    never has to know which of them a provider's client reads.
    """
    if not isinstance(value, Mapping):
        return []
    found: list[str] = []
    for key, item in value.items():
        if item and any(part in str(key).lower() for part in ("base", "host", "endpoint", "header")):
            found.append(f"{path}.{key}")
        else:
            found.extend(_endpoint_settings(item, f"{path}.{key}"))
    return found


def _endpoint_variables(env_key: str, runtime_paths: RuntimePaths) -> list[str]:
    """Return the set endpoint variables of ``env_key``'s provider family, such as ``OPENAI_BASE_URL``.

    Each is read as providers read it, so its ``<NAME>_FILE`` variable sets it and a blank export does not hide ``.env``.
    """
    family = env_key.removesuffix("API_KEY")
    names = {name.removesuffix("_FILE") for name in (*runtime_paths.process_env, *runtime_paths.env_file_values)}
    return sorted(
        name
        for name in names
        if name.startswith(family)
        and name.endswith(("_URL", "_HOST", "_ENDPOINT", "_API_BASE"))
        and runtime_paths.first_env_value((name,))
    )


def _report_key_not_checked(subject: str, provider: str, key_name: str, reasons: Sequence[str]) -> bool:
    """Print that ``key_name`` is not checked, and return True, when ``reasons`` may redirect or replace it.

    A provider without a key check is never probed, so it reports nothing here and gets the probe's "no key check".
    """
    if not reasons or provider_route(provider) not in PROVIDER_KEY_CHECKS:
        return False
    # A setting path holds the model's settings' keys, which may contain brackets Rich reads as markup.
    listed = escape(", ".join(reasons))
    effect = "may send it elsewhere or send another key in its place"
    console.print(f"[yellow]![/yellow] {subject}: {key_name} not checked: {listed} {effect}")
    return True


def _http_check(url: str, headers: dict[str, str] | None = None) -> tuple[bool | None, str]:
    """Make a lightweight GET request and return (True, ""), (False, reason), or (None, reason)."""
    try:
        resp = httpx.get(url, headers=headers or {}, timeout=5)
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
        EMBEDDER_UNREACHABLE_DETAIL,
    )
    if not any(signal in lowered for signal in route_signals):
        return detail

    return (
        f"{detail}; local host '{host}' may be unreachable from this Python runtime"
        " (try a reachable LAN IP instead of .local)"
    )


def _validate_provider_key(provider: str, api_key: str) -> tuple[bool | None, str]:
    """Validate an API key with the provider's key check, which goes to the provider's own API.

    A provider without a key check is never probed: its key style and a model list are unknown, so a probe could
    report a valid key as invalid.

    Returns (True, "") if valid, (False, reason) if invalid,
    (None, reason) if inconclusive (e.g. connection error, or a provider without a key check).
    """
    check = PROVIDER_KEY_CHECKS.get(provider_route(provider))
    if check is None:
        return None, "no key check for this provider"
    url, headers = check.request(api_key)
    return _http_check(url, headers)


def _classify_vertexai_claude_error(
    exc: ProviderError | DefaultCredentialsError | RuntimeError | TypeError | ValueError,
) -> tuple[bool | None, str]:
    """Classify one Vertex AI Claude validation failure for doctor output, with an actionable message.

    The Claude adapter types every failure of a request, rejected Google credentials included, so rejected
    credentials fail with the reason their error carries and another rejected request with its status. Credentials
    that cannot be found and a service that cannot be reached leave the check inconclusive.

    Model availability on Vertex is per project and region: a project can have
    the Vertex AI API enabled yet still 404 on a Claude model when Anthropic
    models are not enabled in its Model Garden or when the model id form is
    wrong for Vertex (current-generation ids are bare, e.g. the anthropic
    default; older ones are dated snapshots with an @ separator).
    """
    if isinstance(exc, AuthenticationFailed):
        return False, str(exc)
    if not isinstance(exc, ProviderError) or exc.status_code is None:
        return None, str(exc)
    if exc.status_code == httpx.codes.NOT_FOUND:
        return False, (
            "HTTP 404: model not available in this project/region — verify the Vertex model id form "
            "(bare current-generation id or dated @ snapshot) and that Anthropic models are enabled "
            f"in the project's Model Garden ({exc})"
        )
    if exc.status_code == httpx.codes.FORBIDDEN:
        if "SERVICE_DISABLED" in str(exc):
            return False, "HTTP 403: the Vertex AI API (aiplatform.googleapis.com) is not enabled in this project"
        return False, "HTTP 403: permission denied — check the credentials' IAM access to Vertex AI in this project"
    return False, f"HTTP {exc.status_code}"


def _validate_vertexai_claude_connection(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[bool | None, str]:
    """Validate the configured Vertex AI Claude model with one tiny request through the runtime's Claude adapter.

    A model the adapter finds no project or region for is not checked, and an unreachable endpoint or token service
    leaves the check inconclusive rather than failed.
    """
    # Google auth, the anthropic SDK, and its HTTP client load only when a Vertex Claude model is checked.
    from google.auth.exceptions import DefaultCredentialsError  # noqa: PLC0415

    from mindroom.providers.anthropic.messages import build_provider, vertex_project_and_region  # noqa: PLC0415
    from mindroom.providers.model_http_client import closing_model_connections  # noqa: PLC0415

    probe_kwargs = {"timeout": 10, **(model_config.extra_kwargs or {}), "max_tokens": 1}
    probe_config = model_config.model_copy(update={"extra_kwargs": probe_kwargs})
    request = ModelRequest(
        messages=[Message(role="user", content="Reply with OK.")],
        overrides=RequestOverrides(prompt_cache=False, omit_thinking_setting=True),
    )
    try:
        provider = build_provider(probe_config, runtime_paths)
        project_id, region = vertex_project_and_region(provider.settings)
        missing = [
            env_key
            for env_key, value in (
                (VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"], project_id),
                (VERTEXAI_CLAUDE_ENV_BY_KEY["region"], region),
            )
            if not value
        ]
        if missing:
            return None, f"missing {', '.join(missing)}"
        asyncio.run(closing_model_connections(complete(provider, request)))
    except PermanentStartupError as exc:
        return False, str(exc)
    except (ProviderError, DefaultCredentialsError, RuntimeError, TypeError, ValueError) as exc:
        return _classify_vertexai_claude_error(exc)

    return True, ""


def _ollama_options(subject: str, read: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the Ollama client options the runtime builds, or None after a warning when its settings cannot be read.

    Building them can read the credential store, and an unreadable store must not end the remaining checks.
    """
    try:
        return read()
    except (OSError, ValueError) as exc:
        console.print(f"[yellow]![/yellow] {escape(subject)}: could not read its Ollama settings ({escape(str(exc))})")
        return None


def _ollama_host(options: Mapping[str, Any]) -> str:
    """Return the server ``options`` reach, as doctor prints it; without a host the Ollama SDK reads ``OLLAMA_HOST``."""
    return escape(str(options.get("host") or "OLLAMA_HOST or the Ollama SDK default"))


def _ollama_reachable(options: Mapping[str, Any]) -> tuple[bool | None, str]:
    """List the server's models through an Ollama SDK client built from the runtime's ``options``, as requests go.

    The SDK completes a host such as ``gpu.example:11434`` with its scheme, and the options carry the runtime's key.
    Returns (True, "") when the server answers, (False, reason) when it rejects the request, and (None, reason) when
    it cannot be reached.
    """
    from ollama import Client, ResponseError  # noqa: PLC0415 - the SDK loads only when an Ollama server is checked

    client_options: dict[str, Any] = {**options, "timeout": 5}
    try:
        Client(**client_options).list()
    except ResponseError as exc:
        return False, f"HTTP {exc.status_code}"
    except (ConnectionError, httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return None, str(exc)
    return True, ""


def _ollama_model_options(config: Config, model_name: str, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Return the client options of the Ollama adapter the runtime builds for ``model_name``, its saved key included."""
    from mindroom.providers.ollama.adapter import build_provider  # noqa: PLC0415 - loads the Ollama SDK
    from mindroom.providers.registry import configured_model  # noqa: PLC0415 - loads every provider family

    return build_provider(configured_model(config, runtime_paths, model_name), runtime_paths).client_options()


def _mem0_ollama_options(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Return the client options of Mem0's Ollama memory LLM, which MindRoom gives only the host."""
    from mindroom.memory.config import mem0_ollama_host  # noqa: PLC0415 - memory setup loads Agno and tiktoken

    return ollama_client_options(mem0_ollama_host(config, runtime_paths), None)


def _ollama_embedder_options(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Return the client options of the Ollama embedder the runtime builds."""
    settings = resolve_embedder_settings(config, runtime_paths)
    return ollama_client_options(settings.host, settings.api_key)


def _check_ollama_server(subject: str, options: Mapping[str, Any]) -> tuple[int, int, int]:
    """Check the Ollama server the runtime's client ``options`` reach, reported under ``subject``."""
    host = _ollama_host(options)
    valid, detail = _ollama_reachable(options)
    return _print_validation(
        valid,
        detail,
        f"{subject} reachable ({host})",
        f"{subject} unreachable: {host}",
        f"{subject}: could not reach {host}",
    )


def _check_ollama_models(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check the server each Ollama model reaches through the client the runtime builds for it, once per client.

    The check sets its own timeout, so models whose other client options match, such as the same server and key,
    share one check, reported under their names; models on one server with different keys are each checked.
    """
    probes: list[tuple[list[str], Mapping[str, Any]]] = []
    passed = failed = warnings = 0
    for name, model_config in config.models.items():
        if provider_route(model_config.provider) != "ollama":
            continue
        options = _ollama_options(f"models.{name}", partial(_ollama_model_options, config, name, runtime_paths))
        if options is None:
            warnings += 1
            continue
        probe = {key: value for key, value in options.items() if key != "timeout"}
        names = next((names for names, other in probes if other == probe), None)
        if names is None:
            probes.append(([name], probe))
        else:
            names.append(name)
    for names, options in probes:
        subject = escape(", ".join(f"models.{name}" for name in names))
        p, f, w = _check_ollama_server(f"{subject}: ollama", options)
        passed += p
        failed += f
        warnings += w
    return passed, failed, warnings


def _check_providers(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Print the providers by route and validate their API keys. Returns (passed, failed, warnings)."""
    provider_models = Counter(provider_route(model.provider) for model in config.models.values())
    if not provider_models:
        return 0, 0, 0

    parts = [f"{provider} ({n} model{'s' if n != 1 else ''})" for provider, n in sorted(provider_models.items())]
    console.print(f"  Providers: {', '.join(parts)}")

    # A variable a model reads but nothing sets is reported once, as `config validate` reports it.
    missing = find_missing_env_variables(config, runtime_paths)
    for variable in missing:
        console.print(f"[yellow]![/yellow] {variable.message}")

    passed = 0
    failed = 0
    warnings = len(missing)
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


def _check_single_provider(provider: str, config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Validate the models of one route, as ``provider_route`` names it. Returns (passed, failed, warnings)."""
    if provider == "vertexai_claude":
        passed = 0
        failed = 0
        warnings = 0
        for model_config in config.models.values():
            if provider_route(model_config.provider) != provider:
                continue
            valid, detail = _validate_vertexai_claude_connection(model_config, runtime_paths)
            p, f, w = _print_validation(
                valid,
                detail,
                f"{provider} connection valid for {model_config.id}",
                f"{provider} connection failed for {model_config.id}",
                f"{provider}: could not validate connection for {model_config.id}",
            )
            passed += p
            failed += f
            warnings += w
        return passed, failed, warnings

    if provider == "ollama":
        return _check_ollama_models(config, runtime_paths)

    env_key = env_key_for_provider(provider)
    if not env_key:
        return 0, 0, 0

    return _check_shared_key(provider, env_key, config, runtime_paths)


def _check_shared_key(provider: str, env_key: str, config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check one route's shared key at the provider's own API. Returns (passed, failed, warnings).

    The key is the one the runtime reads, from ``env_key`` or saved in the dashboard. Doctor never works out where the
    runtime sends a key, which a header, a stored credential, or an SDK variable can change. So it skips the check while
    the provider's endpoint variable is set, reports each model that reads the key and sets an endpoint-like setting as
    not checked, and checks the key unless every model that reads it was skipped. A provider without a key check is
    never probed, so it gets one "no key check" warning whatever its settings.
    """
    api_key, key_name = _shared_key(provider, env_key, runtime_paths)
    if not api_key:
        # `_check_providers` already warned about the missing variable.
        return 0, 0, 0
    # Models with their own credential never read the shared key.
    shared_key_models = _models_reading_shared_key(config, env_key, runtime_paths)
    if _report_key_not_checked(provider, provider, key_name, _endpoint_variables(env_key, runtime_paths)):
        return 0, 0, 1
    skipped = 0
    for name, model in shared_key_models.items():
        settings = _endpoint_settings({"host": model.host, "extra_kwargs": model.extra_kwargs}, f"models.{name}")
        if _report_key_not_checked(provider, provider, key_name, settings):
            skipped += 1
    if shared_key_models and skipped == len(shared_key_models):
        return 0, 0, skipped
    valid, detail = _validate_provider_key(provider, api_key)
    passed, failed, warnings = _print_validation(
        valid,
        detail,
        f"{provider}: {key_name} valid",
        f"{provider}: {key_name} invalid",
        f"{provider}: could not validate {key_name}",
    )
    return passed, failed, warnings + skipped


def _check_memory_config(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory LLM and embedder configuration. Returns (passed, failed, warnings)."""
    backends = (
        {config.memory.backend}
        if not config.agents
        else {memory_backend(config, agent_name) for agent_name in config.agents}
    )
    if "mem0" not in backends:
        if backends == {"none"}:
            console.print("[green]✓[/green] Memory backend: disabled")
        elif backends == {"file"}:
            console.print("[green]✓[/green] Memory backend: file (markdown)")
        else:
            labels = "/".join("disabled" if backend == "none" else backend for backend in sorted(backends))
            console.print(f"[green]✓[/green] Memory backend: mixed (per-agent {labels})")
        # Semantic knowledge bases and file-backend semantic memory search
        # need the shared embedder even without mem0.
        if semantic_embedder_configured(config):
            p, f, w = _check_memory_embedder(config, runtime_paths=runtime_paths)
            return 1 + p, f, w
        return 1, 0, 0

    if len(backends) > 1:
        labels = "/".join("disabled" if backend == "none" else backend for backend in sorted(backends))
        console.print(f"[green]✓[/green] Memory backend: mixed (per-agent {labels})")

    p1, f1, w1 = _check_memory_llm(config, runtime_paths=runtime_paths)
    p2, f2, w2 = _check_memory_embedder(config, runtime_paths=runtime_paths)
    return p1 + p2, f1 + f2, w1 + w2


def _check_default_memory_llm(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check the Ollama server the memory LLM falls back to when none is configured. Returns (passed, failed, warnings)."""
    if (options := _ollama_options("Memory LLM", partial(_mem0_ollama_options, config, runtime_paths))) is None:
        return 0, 0, 1
    ollama_host = _ollama_host(options)
    console.print(
        "[yellow]![/yellow] Memory LLM not configured"
        f" (defaults to ollama at {ollama_host};"
        " see memory/config.py fallback)",
    )
    valid, detail = _ollama_reachable(options)
    if valid is not True:
        console.print(
            f"[red]✗[/red] Default Ollama for memory LLM unreachable ({ollama_host}: {detail})",
        )
        return 0, 1, 0
    return 0, 0, 1


def _check_memory_llm(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory LLM configuration. Returns (passed, failed, warnings)."""
    if config.memory.llm is None:
        return _check_default_memory_llm(config, runtime_paths)

    llm_provider = config.memory.llm.provider
    if llm_provider == "ollama":
        options = _ollama_options("Memory LLM", partial(_mem0_ollama_options, config, runtime_paths))
        return (0, 0, 1) if options is None else _check_ollama_server("Memory LLM: ollama", options)

    llm_model = config.memory.llm.config.get("model", "default")
    # The model is free-form config text, which may contain brackets Rich reads as markup.
    subject = escape(f"Memory LLM ({llm_provider}/{llm_model})")
    env_key = env_key_for_provider(llm_provider)
    # Mem0 uses an explicit memory.llm.config.api_key instead of the shared key.
    if configured_key := config.memory.llm.config.get("api_key"):
        api_key, key_name = configured_key, "memory.llm.config.api_key"
    elif env_key:
        api_key, key_name = _shared_key(llm_provider, env_key, runtime_paths)
    else:
        api_key, key_name = None, "API key"
    if env_key and not api_key:
        console.print(f"[yellow]![/yellow] {subject}: {env_key} not set")
        return 0, 0, 1
    # Any endpoint setting or variable skips the check, whichever key it is, and so does an OPENROUTER_API_KEY in the
    # process environment, which Mem0's OpenAI client, for openai and openrouter alike, sends to OpenRouter in place of
    # the key it is given; only an openrouter key equal to it is the key Mem0 sends.
    if env_key:
        reasons = _endpoint_settings(config.memory.llm.config, "memory.llm.config")
        reasons += _endpoint_variables(env_key, runtime_paths)
        route = provider_route(llm_provider)
        mem0_key = runtime_paths.process_env.get("OPENROUTER_API_KEY")
        if route in {"openai", "openrouter"} and mem0_key and not (route == "openrouter" and mem0_key == api_key):
            reasons.append("OPENROUTER_API_KEY")
        if _report_key_not_checked(subject, llm_provider, key_name, reasons):
            return 0, 0, 1
    valid, detail = _validate_provider_key(llm_provider, api_key or "")
    return _print_validation(
        valid,
        detail,
        f"{subject}: {key_name} valid",
        f"{subject}: {key_name} invalid",
        f"{subject}: could not validate {key_name}",
    )


def _check_memory_embedder(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check memory embedder configuration. Returns (passed, failed, warnings)."""
    emb = config.memory.embedder
    if emb.provider == "ollama":
        options = _ollama_options("Memory embedder", partial(_ollama_embedder_options, config, runtime_paths))
        return (0, 0, 1) if options is None else _check_ollama_server("Memory embedder: ollama", options)

    if emb.provider == "sentence_transformers":
        valid, detail = _validate_sentence_transformers_embedder(runtime_paths, emb.config.model)
        return _print_validation(
            valid,
            detail,
            f"Memory embedder: sentence_transformers/{emb.config.model} local model loaded",
            f"Memory embedder: sentence_transformers/{emb.config.model} local model failed",
            f"Memory embedder: sentence_transformers/{emb.config.model} could not validate",
        )

    if emb.provider == "openai":
        # One real embedding round-trip through the shared probe, which builds
        # the configured embedder and resolves the key via get_embedder_api_key
        # (explicit config key > 'embedder' credential > openai fallback).
        error = probe_embedder(config, runtime_paths)
        host_suffix = f" ({emb.config.host})" if emb.config.host else ""
        valid: bool | None = True if error is None else None if error == EMBEDDER_UNREACHABLE_DETAIL else False
        detail = error or ""
        if error is not None and emb.config.host:
            detail = _with_local_network_hint(detail, emb.config.host)
        return _print_validation(
            valid,
            detail,
            f"Memory embedder: openai/{emb.config.model} embedding round-trip succeeded{host_suffix}",
            f"Memory embedder: openai/{emb.config.model} embedding request failed{host_suffix}",
            f"Memory embedder: openai/{emb.config.model} could not reach embeddings endpoint{host_suffix}",
        )

    env_key = env_key_for_provider(emb.provider)
    api_key = get_secret_from_env(env_key, runtime_paths=runtime_paths) if env_key else None
    if env_key and not api_key:
        console.print(
            f"[yellow]![/yellow] Memory embedder ({emb.provider}): {env_key} not set",
        )
        return 0, 0, 1
    variables = _endpoint_variables(env_key, runtime_paths) if env_key else []
    if _report_key_not_checked("Memory embedder", emb.provider, env_key or "", variables):
        return 0, 0, 1

    valid, detail = _validate_provider_key(emb.provider, api_key or "")
    return _print_validation(
        valid,
        detail,
        f"Memory embedder: {emb.provider}/{emb.config.model} API key valid",
        f"Memory embedder: {emb.provider}/{emb.config.model} API key invalid",
        f"Memory embedder: {emb.provider}/{emb.config.model} could not validate",
    )


def _validate_sentence_transformers_embedder(runtime_paths: RuntimePaths, model: str) -> tuple[bool, str]:
    """Validate a local sentence-transformers model with a tiny embedding request."""
    try:
        embedder = create_sentence_transformers_embedder(runtime_paths, model)
        embedding = embedder.embed(["mindroom doctor embedder check"], purpose="query")[0]
    except Exception as exc:
        return False, str(exc)

    if not embedding:
        return False, "empty embedding vector"
    return True, ""


def _check_matrix_homeserver(runtime_paths: RuntimePaths, config: Config | None = None) -> tuple[int, int, int]:
    """Check Matrix homeserver reachability. Returns (passed, failed, warnings)."""
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    url = matrix_versions_url(homeserver)
    try:
        response = httpx.get(url, timeout=5, verify=constants.runtime_matrix_ssl_verify(runtime_paths=runtime_paths))
    except httpx.TransportError as exc:
        console.print(f"[red]✗[/red] Matrix homeserver unreachable: {homeserver} ({exc})")
        return 0, 1, 0
    if response_has_matrix_versions(response):
        sliding_configured = config is not None and config.matrix_sync.mode == "sliding"
        if sliding_configured and not response_advertises_sliding_sync(response):
            console.print(
                f"[red]✗[/red] Matrix homeserver does not advertise MSC4186 Simplified Sliding Sync"
                f" ({MSC4186_UNSTABLE_FEATURE}): {homeserver}."
                " Set matrix_sync.mode: classic or upgrade the homeserver.",
            )
            return 0, 1, 0
        console.print(f"[green]✓[/green] Matrix homeserver: {homeserver}")
        return 1, 0, 0
    detail = f"HTTP {response.status_code}" if not response.is_success else "returned invalid /versions payload"
    console.print(f"[red]✗[/red] Matrix homeserver {detail}: {homeserver}")
    return 0, 1, 0


def _check_pairing(runtime_paths: RuntimePaths) -> tuple[int, int, int]:
    """Check hosted pairing state. Returns (passed, failed, warnings).

    An unpaired hosted install before its first run is normal because `mindroom run` pairs it, so it counts as passed.
    A warning would keep the macOS app's Check step at "Needs attention" on every first run.
    """
    if provisioning_url_from_env(runtime_paths) is None or registration_token_from_env(runtime_paths) is not None:
        # Without hosted provisioning, or with a registration token, agents register without pairing.
        return 0, 0, 0
    try:
        required = local_pairing_required(runtime_paths)
        paired = local_provisioning_client_credentials_from_env(runtime_paths) is not None
    except ValueError as exc:
        console.print(f"[red]✗[/red] Pairing: {escape(str(exc))}")
        return 0, 1, 0
    if required:
        console.print(
            "[green]✓[/green] Not paired yet: `mindroom run` will print a link to approve "
            "with your MindRoom Chat account",
        )
        return 1, 0, 0
    if paired:
        console.print("[green]✓[/green] Paired with MindRoom Chat")
        return 1, 0, 0
    return 0, 0, 0


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
