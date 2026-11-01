"""Credentials the runtime environment declares: provider keys, bootstrap values, and credential seeds.

The primary reads them as a layer under the credentials saved in its store, so a variable removed from the
environment stops supplying its credential at the next start, and nothing is copied into the store.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import PROVIDER_ENV_KEYS, read_secret_file, runtime_env_path
from mindroom.credentials.manager import validate_service_name
from mindroom.credentials.policy import is_oauth_token_service
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import CREDENTIAL_SEEDS_FILE_ENV, CREDENTIAL_SEEDS_JSON_ENV

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Dedicated credential service for the semantic-search embedder, declared by EMBEDDER_API_KEY.
EMBEDDER_CREDENTIAL_SERVICE = "embedder"


@dataclass(frozen=True, slots=True)
class DeclaredCredential:
    """One service's credentials as the environment declares them, and the variable that declares them."""

    credentials: Mapping[str, Any]
    variable: str


@dataclass(frozen=True)
class _CredentialSeedDeclaration:
    source_env_var: str
    seed: Mapping[str, Any]


def get_secret_from_env(name: str, runtime_paths: RuntimePaths) -> str | None:
    """Read a secret from NAME or NAME_FILE; None when neither supplies a usable (non-blank, non-placeholder) value."""
    secret = runtime_paths.first_env_value((name,))
    return secret[0] if secret else None


def _read_text_file(path: Path) -> str | None:
    try:
        return read_secret_file(path)
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
    path_context = {} if source_path is None else {"path": str(source_path)}
    try:
        raw_value = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning(
            "credential_seed_declaration_json_invalid",
            env_var=source_env_var,
            error=str(exc),
            **path_context,
        )
        return []
    try:
        return _coerce_seed_entries(raw_value, source=source_env_var)
    except TypeError as exc:
        logger.warning("credential_seed_declaration_invalid", env_var=source_env_var, error=str(exc), **path_context)
        return []


def _load_declared_credential_seeds(runtime_paths: RuntimePaths) -> list[_CredentialSeedDeclaration]:
    """Load explicit credential seed declarations from runtime env or file."""
    seed_entries: list[_CredentialSeedDeclaration] = []

    seed_file = runtime_env_path(runtime_paths, CREDENTIAL_SEEDS_FILE_ENV)
    if seed_file is not None:
        try:
            raw_file_json = read_secret_file(seed_file)
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


def _declared_seed_credentials(runtime_paths: RuntimePaths) -> list[tuple[str, dict[str, Any], str]]:
    """Return the credentials each valid seed declaration supplies, in declaration order."""
    seeded: list[tuple[str, dict[str, Any], str]] = []
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
        seeded.append((service, credentials, declaration.source_env_var))
    return seeded


def declared_credentials(runtime_paths: RuntimePaths) -> dict[str, DeclaredCredential]:
    """Return every credential the environment declares, by service.

    Provider keys come first, then ``GOOGLE_APPLICATION_CREDENTIALS``, ``GITHUB_TOKEN``, ``EMBEDDER_API_KEY``, and the
    credential seeds in declaration order; a later declaration of a service replaces an earlier one.
    OAuth token services cannot be declared; they are connected through OAuth.
    """
    declared: dict[str, DeclaredCredential] = {}

    def declare(service: str, credentials: dict[str, Any], variable: str) -> None:
        if is_oauth_token_service(service):
            logger.warning("credential_env_declaration_rejected_lifecycle_owned_service", service=service)
            return
        declared[service] = DeclaredCredential(credentials, variable)

    for service, env_var in PROVIDER_ENV_KEYS.items():
        secret = runtime_paths.first_env_value((env_var,))
        if secret is not None:
            value, variable = secret
            declare(service, {"host": value} if service == "ollama" else {"api_key": value}, variable)
    adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if adc_path is not None:
        declare("google_vertex_adc", {"application_credentials_path": str(adc_path)}, "GOOGLE_APPLICATION_CREDENTIALS")
    if (secret := runtime_paths.first_env_value(("GITHUB_TOKEN",))) is not None:
        declare("github_private", {"username": "x-access-token", "token": secret[0]}, secret[1])
    if (secret := runtime_paths.first_env_value(("EMBEDDER_API_KEY",))) is not None:
        declare(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": secret[0]}, secret[1])
    for service, credentials, variable in _declared_seed_credentials(runtime_paths):
        declare(service, credentials, variable)
    return declared
