"""Sync shared named-service credentials from runtime env into CredentialsManager."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mindroom.constants import RuntimePaths, runtime_env_path
from mindroom.credentials import get_runtime_shared_credentials_manager, validate_service_name
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import (
    CREDENTIAL_SEEDS_FILE_ENV,
    CREDENTIAL_SEEDS_JSON_ENV,
)

logger = get_logger(__name__)

_ENV_TO_SERVICE_MAP = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GOOGLE_API_KEY": "google_gemini",
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "CEREBRAS_API_KEY": "cerebras",
    "GROQ_API_KEY": "groq",
    "GOOGLE_APPLICATION_CREDENTIALS": "google_vertex_adc",
}


@dataclass(frozen=True)
class _CredentialSeedDeclaration:
    source_env_var: str
    seed: Mapping[str, Any]


def get_secret_from_env(name: str, runtime_paths: RuntimePaths) -> str | None:
    """Read a secret from NAME or NAME_FILE.

    If env var `NAME` is set, return it. Otherwise, if `NAME_FILE` points to
    a readable file, return its stripped contents. Else return None.
    """
    val = runtime_paths.env_value(name)
    if val:
        return val
    file_var = f"{name}_FILE"
    file_path = runtime_env_path(runtime_paths, file_var)
    if file_path is not None and file_path.exists():
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except Exception:
            # Avoid noisy logs here; callers can handle None gracefully
            return None
    return None


def _sync_github_private_credentials(runtime_paths: RuntimePaths) -> bool:
    """Seed/update github_private from GITHUB_TOKEN for Git knowledge sync."""
    github_token = get_secret_from_env("GITHUB_TOKEN", runtime_paths=runtime_paths)
    if not github_token:
        logger.debug("No value found for GITHUB_TOKEN or GITHUB_TOKEN_FILE")
        return False

    return _sync_service_credentials(
        service="github_private",
        credentials={
            "username": "x-access-token",
            "token": github_token,
        },
        runtime_paths=runtime_paths,
        env_var="GITHUB_TOKEN",
    )


def _sync_service_credentials(
    *,
    service: str,
    credentials: dict[str, Any],
    runtime_paths: RuntimePaths,
    env_var: str | None = None,
) -> bool:
    """Seed or update one env-backed named service."""
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    credentials_path = creds_manager.get_credentials_path(service)
    credentials_file_exists = credentials_path.exists()
    existing = creds_manager.load_credentials(service)
    if existing is None and credentials_file_exists:
        logger.warning(
            "credential_env_sync_skipped_unreadable_existing_file",
            service=service,
            path=str(credentials_path),
        )
        return False
    if existing is not None:
        source = existing.get("_source")
        if source != "env":
            logger.debug("credential_env_sync_skipped", service=service, source=source)
            return False

    creds_manager.save_credentials(service, {**credentials, "_source": "env"})
    log_context = {"service": service}
    if env_var is not None:
        log_context["env_var"] = env_var
    if existing is None:
        logger.info("credential_seeded_from_env", **log_context)
    else:
        logger.info("credential_updated_from_env", **log_context)
    return True


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _resolve_seed_file_path(raw_path: str, runtime_paths: RuntimePaths) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = runtime_paths.config_dir / path
    return path.resolve()


def _coerce_seed_entries(raw_value: object, *, source: str) -> list[Mapping[str, Any]]:
    """Return validated raw seed entries from a decoded JSON value."""
    if isinstance(raw_value, Mapping) and "seeds" in raw_value:
        raw_value = cast("Mapping[str, Any]", raw_value)["seeds"]
    elif isinstance(raw_value, Mapping):
        raw_value = [cast("Mapping[str, Any]", raw_value)]
    if not isinstance(raw_value, list):
        msg = f"{source} must contain a credential seed object, a list, or an object with a 'seeds' list"
        raise TypeError(msg)

    entries: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw_value):
        if not isinstance(item, Mapping):
            msg = f"{source} credential seed at index {index} must be an object"
            raise TypeError(msg)
        entries.append(cast("Mapping[str, Any]", item))
    return entries


def _decode_seed_entries(
    raw_json: str,
    *,
    source_env_var: str,
    source_path: Path | None = None,
) -> list[Mapping[str, Any]]:
    try:
        raw_value = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        log_context = {"env_var": source_env_var, "error": str(exc)}
        if source_path is not None:
            log_context["path"] = str(source_path)
        logger.warning("credential_seed_declaration_json_invalid", **log_context)
        return []
    try:
        return _coerce_seed_entries(raw_value, source=source_env_var)
    except (TypeError, ValueError) as exc:
        log_context = {"env_var": source_env_var, "error": str(exc)}
        if source_path is not None:
            log_context["path"] = str(source_path)
        logger.warning("credential_seed_declaration_invalid", **log_context)
        return []


def _load_declared_credential_seeds(runtime_paths: RuntimePaths) -> list[_CredentialSeedDeclaration]:
    """Load explicit credential seed declarations from runtime env or file."""
    seed_entries: list[_CredentialSeedDeclaration] = []

    seed_file = runtime_env_path(runtime_paths, CREDENTIAL_SEEDS_FILE_ENV)
    if seed_file is not None:
        try:
            raw_file_json = seed_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "credential_seed_declaration_file_unreadable",
                env_var=CREDENTIAL_SEEDS_FILE_ENV,
                path=str(seed_file),
                error=str(exc),
            )
        else:
            seed_entries.extend(
                _CredentialSeedDeclaration(source_env_var=CREDENTIAL_SEEDS_FILE_ENV, seed=entry)
                for entry in _decode_seed_entries(
                    raw_file_json,
                    source_env_var=CREDENTIAL_SEEDS_FILE_ENV,
                    source_path=seed_file,
                )
            )

    raw_json = runtime_paths.env_value(CREDENTIAL_SEEDS_JSON_ENV)
    if raw_json:
        seed_entries.extend(
            _CredentialSeedDeclaration(source_env_var=CREDENTIAL_SEEDS_JSON_ENV, seed=entry)
            for entry in _decode_seed_entries(raw_json, source_env_var=CREDENTIAL_SEEDS_JSON_ENV)
        )

    return seed_entries


def _resolve_seed_value(value_spec: object, runtime_paths: RuntimePaths) -> object | None:
    """Resolve one seed value from an env ref, file ref, or literal value."""
    if isinstance(value_spec, str | int | float | bool):
        return value_spec
    if not isinstance(value_spec, Mapping):
        msg = "Credential seed field values must be literals or objects with env, file, or value"
        raise TypeError(msg)
    value_mapping = cast("Mapping[str, Any]", value_spec)

    env_name = value_mapping.get("env")
    if isinstance(env_name, str) and env_name.strip():
        return get_secret_from_env(env_name.strip(), runtime_paths=runtime_paths)

    file_path = value_mapping.get("file")
    if isinstance(file_path, str) and file_path.strip():
        return _read_text_file(_resolve_seed_file_path(file_path.strip(), runtime_paths))

    if "value" in value_mapping:
        return value_mapping["value"]

    msg = "Credential seed field object must include env, file, or value"
    raise ValueError(msg)


def _resolve_seed_credentials(
    seed: Mapping[str, Any],
    *,
    service: str,
    runtime_paths: RuntimePaths,
) -> dict[str, Any] | None:
    credentials_spec = seed.get("credentials")
    if not isinstance(credentials_spec, Mapping) or not credentials_spec:
        msg = f"Credential seed for service '{service}' must include a non-empty credentials object"
        raise ValueError(msg)

    credentials: dict[str, Any] = {}
    for raw_field_name, value_spec in credentials_spec.items():
        if not isinstance(raw_field_name, str) or not raw_field_name.strip():
            msg = f"Credential seed for service '{service}' has an invalid field name"
            raise ValueError(msg)
        field_name = raw_field_name.strip()
        if field_name.startswith("_"):
            msg = f"Credential seed for service '{service}' may not set internal field '{field_name}'"
            raise ValueError(msg)

        value = _resolve_seed_value(value_spec, runtime_paths)
        if value is None or (isinstance(value, str) and not value.strip()):
            logger.debug(
                "credential_seed_value_missing",
                service=service,
                field=field_name,
            )
            return None
        credentials[field_name] = value.strip() if isinstance(value, str) else value

    return credentials


def _sync_declared_credential_seeds(runtime_paths: RuntimePaths) -> int:
    """Seed/update explicitly declared credential services."""
    synced_count = 0
    for declaration in _load_declared_credential_seeds(runtime_paths):
        seed = declaration.seed
        raw_service = seed.get("service")
        if not isinstance(raw_service, str):
            logger.warning(
                "credential_seed_declaration_invalid",
                env_var=declaration.source_env_var,
                error="Credential seed must include a string service name",
            )
            continue
        try:
            service = validate_service_name(raw_service)
            credentials = _resolve_seed_credentials(seed, service=service, runtime_paths=runtime_paths)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "credential_seed_declaration_invalid",
                env_var=declaration.source_env_var,
                error=str(exc),
            )
            continue
        if credentials is None:
            continue
        if _sync_service_credentials(
            service=service,
            credentials=credentials,
            runtime_paths=runtime_paths,
            env_var=declaration.source_env_var,
        ):
            synced_count += 1
    return synced_count


def _sync_google_vertex_adc_credentials(runtime_paths: RuntimePaths) -> int:
    """Seed/update the default ADC service from GOOGLE_APPLICATION_CREDENTIALS."""
    adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if adc_path is None:
        logger.debug("No GOOGLE_APPLICATION_CREDENTIALS path found for google_vertex_adc")
        return 0

    if _sync_service_credentials(
        service="google_vertex_adc",
        credentials={"application_credentials_path": str(adc_path)},
        runtime_paths=runtime_paths,
        env_var="GOOGLE_APPLICATION_CREDENTIALS",
    ):
        return 1
    return 0


def _sync_google_oauth_client_credentials(runtime_paths: RuntimePaths) -> int:
    """Seed/update the default Google OAuth client service from GOOGLE_CLIENT_ID/SECRET."""
    client_id = get_secret_from_env("GOOGLE_CLIENT_ID", runtime_paths=runtime_paths)
    client_secret = get_secret_from_env("GOOGLE_CLIENT_SECRET", runtime_paths=runtime_paths)
    if not client_id or not client_secret:
        return 0

    if _sync_service_credentials(
        service="google_oauth_client",
        credentials={"client_id": client_id, "client_secret": client_secret},
        runtime_paths=runtime_paths,
        env_var="GOOGLE_CLIENT_ID,GOOGLE_CLIENT_SECRET",
    ):
        return 1
    return 0


def sync_env_to_credentials(runtime_paths: RuntimePaths) -> None:
    """Sync supported shared named-service env values into CredentialsManager."""
    synced_count = 0

    for env_var, service in _ENV_TO_SERVICE_MAP.items():
        if env_var == "GOOGLE_APPLICATION_CREDENTIALS":
            continue
        env_value = get_secret_from_env(env_var, runtime_paths=runtime_paths)

        if not env_value:
            logger.debug("credential_env_value_missing", env_var=env_var)
            continue

        logger.debug("credential_env_value_found", env_var=env_var, value_length=len(env_value))

        if _sync_service_credentials(
            service=service,
            credentials={"api_key": env_value},
            runtime_paths=runtime_paths,
            env_var=env_var,
        ):
            synced_count += 1

    synced_count += _sync_google_vertex_adc_credentials(runtime_paths=runtime_paths)
    synced_count += _sync_google_oauth_client_credentials(runtime_paths=runtime_paths)

    if _sync_github_private_credentials(runtime_paths=runtime_paths):
        synced_count += 1

    synced_count += _sync_declared_credential_seeds(runtime_paths=runtime_paths)

    if synced_count > 0:
        logger.info("credentials_synced_from_env", synced_count=synced_count)
    else:
        logger.debug("No credentials to sync from environment")


def get_ollama_host(runtime_paths: RuntimePaths) -> str | None:
    """Get Ollama host configuration.

    Returns:
        The Ollama host URL if configured, None otherwise

    """
    value = runtime_paths.env_value("OLLAMA_HOST")
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def get_api_key_for_provider(provider: str, runtime_paths: RuntimePaths) -> str | None:
    """Get API key for a conventional provider credential service."""
    service = "google_gemini" if provider in {"gemini", "google"} else provider
    if service == "ollama":
        return None
    return get_runtime_shared_credentials_manager(runtime_paths).get_api_key(service)
