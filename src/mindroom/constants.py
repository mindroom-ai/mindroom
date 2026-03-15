"""Shared constants for the mindroom package.

This module contains constants that are used across multiple modules
to avoid circular imports. It does not import anything from the internal
codebase.
"""

import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

from dotenv import dotenv_values

# Agent names
ROUTER_AGENT_NAME = "router"

# Search order for existing files: env var > ./config.yaml > ~/.mindroom/config.yaml
_CONFIG_SEARCH_PATHS = [Path("config.yaml"), Path.home() / ".mindroom" / "config.yaml"]
_RUNTIME_PATH_ENV_KEYS = frozenset({"MINDROOM_CONFIG_PATH", "MINDROOM_STORAGE_PATH"})
_CONFIG_PATH_PLACEHOLDER_PATTERN = re.compile(r"\$(?:\{(?P<braced>[A-Z0-9_]+)\}|(?P<bare>[A-Z0-9_]+))")


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved runtime context shared across the process.

    `RuntimePaths` is the source of truth for:
    - active config path and config dir
    - config-adjacent `.env`
    - shared storage root
    - the true exported process env seen during resolution
    - the sibling `.env` values seen during resolution

    Runtime env precedence is:
    1. Explicit runtime arguments passed to `resolve_runtime_paths()`
    2. Exported process env values
    3. The config-adjacent `.env`
    4. Code defaults in the caller
    """

    config_path: Path
    config_dir: Path
    env_path: Path
    storage_root: Path
    process_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    env_file_values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def env_value(self, name: str, *, default: str | None = None) -> str | None:
        """Resolve one env value against this runtime context."""
        if name == "MINDROOM_CONFIG_PATH":
            return str(self.config_path)
        if name == "MINDROOM_STORAGE_PATH":
            return str(self.storage_root)
        if name in self.process_env:
            return self.process_env[name]
        if name in self.env_file_values:
            return self.env_file_values[name]
        return default

    def env_flag(self, name: str, *, default: bool = False) -> bool:
        """Resolve one boolean env value against this runtime context."""
        value = self.env_value(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}


def _copy_process_env(process_env: dict[str, str] | None = None) -> dict[str, str]:
    if process_env is not None:
        return dict(process_env)
    return dict(os.environ)


def _runtime_env_file_values_for_path(env_path: Path) -> dict[str, str]:
    """Read string env values from one runtime-adjacent `.env` file."""
    if not env_path.is_file():
        return {}
    return {key: value for key, value in dotenv_values(env_path).items() if isinstance(value, str)}


def _configured_config_path(process_env: Mapping[str, str]) -> Path | None:
    configured_path = process_env.get("MINDROOM_CONFIG_PATH", "").strip()
    if not configured_path:
        return None
    return Path(configured_path).expanduser()


def config_search_locations(process_env: Mapping[str, str]) -> list[Path]:
    """Return the ordered list of locations where MindRoom looks for config.

    This is the single source of truth for config file discovery.
    """
    seen: set[Path] = set()
    locations: list[Path] = []
    if configured_path := _configured_config_path(process_env):
        resolved = configured_path.resolve()
        seen.add(resolved)
        locations.append(resolved)
    for p in _CONFIG_SEARCH_PATHS:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            locations.append(resolved)
    return locations


def _storage_root_from_env_values(env_file_values: dict[str, str]) -> Path | None:
    value = env_file_values.get("MINDROOM_STORAGE_PATH")
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _storage_root_from_env_path(env_path: Path) -> Path | None:
    """Read MINDROOM_STORAGE_PATH from one env file when present."""
    return _storage_root_from_env_values(_runtime_env_file_values_for_path(env_path))


def resolve_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
    process_env: dict[str, str] | None = None,
) -> RuntimePaths:
    """Resolve the runtime config/env/storage paths for one execution context.

    This is a pure resolver. It does not mutate `os.environ` or any module globals.
    """
    resolved_config_arg = Path(config_path).expanduser().resolve() if config_path is not None else None
    resolved_process_env = _copy_process_env(process_env)
    resolved_config_path = (
        Path(resolved_config_arg or find_config(process_env=resolved_process_env)).expanduser().resolve()
    )
    config_dir = resolved_config_path.parent
    env_path = config_dir / ".env"
    env_file_values = _runtime_env_file_values_for_path(env_path)

    configured_storage_root = resolved_process_env.get("MINDROOM_STORAGE_PATH", "").strip()
    configured_storage_path = Path(configured_storage_root).expanduser().resolve() if configured_storage_root else None

    if storage_path is not None:
        resolved_storage_root = Path(storage_path).expanduser().resolve()
    elif configured_storage_path is not None:
        resolved_storage_root = configured_storage_path
    elif env_storage_root := _storage_root_from_env_values(env_file_values):
        resolved_storage_root = env_storage_root
    else:
        resolved_storage_root = (config_dir / "mindroom_data").resolve()

    return RuntimePaths(
        config_path=resolved_config_path,
        config_dir=config_dir,
        env_path=env_path,
        storage_root=resolved_storage_root,
        process_env=cast("Mapping[str, str]", MappingProxyType(resolved_process_env)),
        env_file_values=cast("Mapping[str, str]", MappingProxyType(env_file_values)),
    )


def _with_primary_runtime_env(paths: RuntimePaths) -> RuntimePaths:
    """Return one runtime context whose process env snapshot carries its path contract."""
    normalized_process_env = dict(paths.process_env)
    normalized_process_env["MINDROOM_CONFIG_PATH"] = str(paths.config_path)
    normalized_process_env["MINDROOM_STORAGE_PATH"] = str(paths.storage_root)
    if normalized_process_env == dict(paths.process_env):
        return paths
    return RuntimePaths(
        config_path=paths.config_path,
        config_dir=paths.config_dir,
        env_path=paths.env_path,
        storage_root=paths.storage_root,
        process_env=cast("Mapping[str, str]", MappingProxyType(normalized_process_env)),
        env_file_values=paths.env_file_values,
    )


def resolve_primary_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
    process_env: dict[str, str] | None = None,
) -> RuntimePaths:
    """Resolve the primary runtime context for one top-level execution boundary."""
    return _with_primary_runtime_env(
        resolve_runtime_paths(
            config_path=config_path,
            storage_path=storage_path,
            process_env=process_env,
        ),
    )


def serialize_runtime_paths(runtime_paths: RuntimePaths) -> dict[str, object]:
    """Return a JSON-compatible payload for explicit cross-process runtime handoff."""
    return {
        "config_path": str(runtime_paths.config_path),
        "storage_root": str(runtime_paths.storage_root),
        "process_env": dict(runtime_paths.process_env),
        "env_file_values": dict(runtime_paths.env_file_values),
    }


def deserialize_runtime_paths(payload: Mapping[str, object]) -> RuntimePaths:
    """Build one RuntimePaths object from an explicit serialized payload."""
    raw_config_path = payload.get("config_path")
    raw_storage_root = payload.get("storage_root")
    raw_process_env = payload.get("process_env")
    raw_env_file_values = payload.get("env_file_values")
    if not isinstance(raw_config_path, str) or not raw_config_path.strip():
        msg = "Serialized runtime payload is missing config_path"
        raise TypeError(msg)
    if not isinstance(raw_storage_root, str) or not raw_storage_root.strip():
        msg = "Serialized runtime payload is missing storage_root"
        raise TypeError(msg)
    if not isinstance(raw_process_env, Mapping):
        msg = "Serialized runtime payload is missing process_env"
        raise TypeError(msg)
    if not isinstance(raw_env_file_values, Mapping):
        msg = "Serialized runtime payload is missing env_file_values"
        raise TypeError(msg)

    process_env = {
        key: value for key, value in raw_process_env.items() if isinstance(key, str) and isinstance(value, str)
    }
    env_file_values = {
        key: value for key, value in raw_env_file_values.items() if isinstance(key, str) and isinstance(value, str)
    }
    config_path = Path(raw_config_path).expanduser().resolve()
    return RuntimePaths(
        config_path=config_path,
        config_dir=config_path.parent,
        env_path=config_path.parent / ".env",
        storage_root=Path(raw_storage_root).expanduser().resolve(),
        process_env=cast("Mapping[str, str]", MappingProxyType(process_env)),
        env_file_values=cast("Mapping[str, str]", MappingProxyType(env_file_values)),
    )


def _expand_runtime_path_vars(value: str, paths: RuntimePaths) -> str:
    """Expand the allowed config-path placeholders for one runtime context."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare") or ""
        if name == "MINDROOM_CONFIG_PATH":
            return str(paths.config_path)
        if name == "MINDROOM_STORAGE_PATH":
            return str(paths.storage_root)
        msg = (
            "Config-relative paths only support ${MINDROOM_CONFIG_PATH} and "
            f"${{MINDROOM_STORAGE_PATH}} placeholders (got: {name})"
        )
        raise ValueError(msg)

    return _CONFIG_PATH_PLACEHOLDER_PATTERN.sub(_replace, value)


def exported_process_env() -> dict[str, str]:
    """Return the current exported env snapshot."""
    return _copy_process_env()


def runtime_env_values(runtime_paths: RuntimePaths) -> Mapping[str, str]:
    """Return the effective runtime env mapping for one explicit runtime context."""
    merged_env = dict(runtime_paths.env_file_values)
    merged_env.update(runtime_paths.process_env)
    merged_env["MINDROOM_CONFIG_PATH"] = str(runtime_paths.config_path)
    merged_env["MINDROOM_STORAGE_PATH"] = str(runtime_paths.storage_root)
    return cast("Mapping[str, str]", MappingProxyType(merged_env))


def runtime_env_flag(
    name: str,
    runtime_paths: RuntimePaths,
    *,
    default: bool = False,
) -> bool:
    """Read a boolean runtime env flag with config-adjacent `.env` fallback."""
    value = runtime_paths.env_value(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def runtime_matrix_homeserver(runtime_paths: RuntimePaths) -> str:
    """Return the effective Matrix homeserver for one runtime context."""
    return runtime_paths.env_value("MATRIX_HOMESERVER", default="http://localhost:8008") or "http://localhost:8008"


def runtime_matrix_ssl_verify(runtime_paths: RuntimePaths) -> bool:
    """Return whether Matrix HTTPS requests should verify certificates."""
    return runtime_env_flag("MATRIX_SSL_VERIFY", default=True, runtime_paths=runtime_paths)


def runtime_matrix_server_name(runtime_paths: RuntimePaths) -> str | None:
    """Return the optional Matrix server-name override for one runtime context."""
    return runtime_paths.env_value("MATRIX_SERVER_NAME")


def runtime_mindroom_namespace(runtime_paths: RuntimePaths) -> str | None:
    """Return the optional installation namespace for one runtime context."""
    value = runtime_paths.env_value("MINDROOM_NAMESPACE")
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def runtime_ai_cache_enabled(runtime_paths: RuntimePaths) -> bool:
    """Return whether the AI response cache is enabled for one runtime context."""
    return runtime_env_flag("MINDROOM_ENABLE_AI_CACHE", default=True, runtime_paths=runtime_paths)


def matrix_state_file(runtime_paths: RuntimePaths) -> Path:
    """Return the matrix-state file for one runtime context."""
    return runtime_paths.storage_root / "matrix_state.yaml"


def tracking_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the tracking directory for one runtime context."""
    return runtime_paths.storage_root / "tracking"


def memory_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the shared memory directory for one runtime context."""
    return runtime_paths.storage_root / "memory"


def credentials_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the credentials directory for one runtime context."""
    return runtime_paths.storage_root / "credentials"


def encryption_keys_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the encryption-keys directory for one runtime context."""
    return runtime_paths.storage_root / "encryption_keys"


def resolve_config_relative_path(
    raw_path: str | Path,
    runtime_paths: RuntimePaths,
) -> Path:
    """Resolve a configured path, treating relative values as config-directory-relative.

    Config-relative paths may use `${MINDROOM_STORAGE_PATH}` or
    `${MINDROOM_CONFIG_PATH}` placeholders only.
    """
    unresolved = Path(_expand_runtime_path_vars(os.fspath(raw_path), runtime_paths)).expanduser()
    if unresolved.is_absolute():
        return unresolved.resolve()
    return (runtime_paths.config_dir / unresolved).resolve()


def _docker_container_enabled() -> bool:
    """Return whether MindRoom is running from the packaged container image."""
    return os.getenv("DOCKER_CONTAINER", "").strip().lower() in {"1", "true", "yes", "on"}


def _use_storage_path_for_workspace_assets(runtime_paths: RuntimePaths) -> bool:
    """Return whether writable workspace assets should live under persistent storage."""
    if not _docker_container_enabled():
        return False
    configured_config_path = runtime_paths.process_env.get("MINDROOM_CONFIG_PATH")
    configured_storage_path = runtime_paths.process_env.get("MINDROOM_STORAGE_PATH")
    if configured_config_path is None or configured_storage_path is None:
        return False
    return (
        Path(configured_config_path).expanduser().resolve() == runtime_paths.config_path
        and Path(configured_storage_path).expanduser().resolve() == runtime_paths.storage_root
    )


def avatars_dir(runtime_paths: RuntimePaths) -> Path:
    """Return the writable avatars directory for the active workspace.

    Source checkouts keep avatars next to the active config file so generated
    assets can be committed with the workspace.
    Containerized deployments usually mount `config.yaml` as a single file, so
    config-adjacent writes would be ephemeral; in that case, store writable
    overrides under the persistent MindRoom storage root instead.
    """
    if _use_storage_path_for_workspace_assets(runtime_paths):
        return runtime_paths.storage_root / "avatars"
    return runtime_paths.config_dir / "avatars"


def bundled_avatars_dir() -> Path:
    """Return the bundled avatar directory shipped with a source checkout or runtime image."""
    return Path(__file__).resolve().parents[2] / "avatars"


def workspace_avatar_path(
    entity_type: str,
    entity_name: str,
    runtime_paths: RuntimePaths,
) -> Path:
    """Return the writable workspace avatar path for a managed entity."""
    return avatars_dir(runtime_paths) / entity_type / f"{entity_name}.png"


def resolve_avatar_path(
    entity_type: str,
    entity_name: str,
    runtime_paths: RuntimePaths,
) -> Path:
    """Return the best available avatar path for a managed entity.

    Prefer a writable workspace override.
    Fall back to the bundled runtime assets when no workspace file exists yet.
    If neither exists, return the intended workspace path so callers that write
    new avatars know where to place them.
    """
    workspace_path = workspace_avatar_path(
        entity_type,
        entity_name,
        runtime_paths,
    )
    if workspace_path.exists():
        return workspace_path

    bundled_path = bundled_avatars_dir() / entity_type / f"{entity_name}.png"
    if bundled_path.exists():
        return bundled_path

    return workspace_path


def find_config(*, process_env: Mapping[str, str]) -> Path:
    """Find the first existing config file, or fall back to ~/.mindroom/config.yaml.

    Returns the original (possibly relative) path, not a resolved one,
    so CLI-facing defaults still display cleanly.
    """
    if configured_path := _configured_config_path(process_env):
        return configured_path
    for path in _CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return _CONFIG_SEARCH_PATHS[-1]  # default to ~/.mindroom/config.yaml for creation


def set_runtime_storage_path(storage_path: Path, runtime_paths: RuntimePaths) -> Path:
    """Return the storage root for the updated primary runtime context."""
    updated_runtime_paths = resolve_primary_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=storage_path,
        process_env=dict(runtime_paths.process_env),
    )
    return updated_runtime_paths.storage_root


def set_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
) -> RuntimePaths:
    """Resolve one primary runtime context without mutating process-global state."""
    return resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
    )


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment flag."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Other constants
VOICE_PREFIX = "🎤 "
ORIGINAL_SENDER_KEY = "com.mindroom.original_sender"
VOICE_RAW_AUDIO_FALLBACK_KEY = "com.mindroom.voice_raw_audio_fallback"
ATTACHMENT_IDS_KEY = "com.mindroom.attachment_ids"
AI_RUN_METADATA_KEY = "io.mindroom.ai_run"

# Placeholder used in starter config templates. `mindroom connect` can
# automatically replace this token with the owner Matrix user ID returned
# by the provisioning service.
OWNER_MATRIX_USER_ID_PLACEHOLDER = "__MINDROOM_OWNER_USER_ID_FROM_PAIRING__"


# Canonical mapping from provider name to the environment variable it requires.
# Other modules derive their own views from this single source of truth.
PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "OLLAMA_HOST",
}
VERTEXAI_CLAUDE_ENV_KEYS: tuple[str, str] = ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")

_CHROMADB_PY314_PATCHED = False


def env_key_for_provider(provider: str) -> str | None:
    """Get the environment variable name for a provider's API key.

    Handles the gemini→google alias so callers don't need to.
    """
    if provider == "gemini":
        return PROVIDER_ENV_KEYS.get("google")
    return PROVIDER_ENV_KEYS.get(provider)


def patch_chromadb_for_python314() -> None:
    """Patch pydantic internals so chromadb works on Python 3.14+.

    chromadb currently relies on pydantic v1 `BaseSettings` behavior and defines
    untyped fields in its settings model. This runtime shim can be removed once
    chromadb ships an upstream fix.
    """
    global _CHROMADB_PY314_PATCHED
    if _CHROMADB_PY314_PATCHED or sys.version_info < (3, 14):
        return

    import pydantic  # noqa: PLC0415
    from pydantic._internal import _model_construction  # noqa: PLC0415
    from pydantic_settings import BaseSettings  # noqa: PLC0415

    # pydantic-settings v2 defaults to extra="forbid", but pydantic v1's
    # BaseSettings silently ignored env vars / .env keys that didn't match
    # any field.  chromadb relies on that tolerance, so we must restore it.
    class _PermissiveBaseSettings(BaseSettings):
        model_config = BaseSettings.model_config.copy()
        model_config["extra"] = "ignore"

    pydantic.BaseSettings = _PermissiveBaseSettings

    original_inspect_namespace = _model_construction.inspect_namespace

    def _patched_inspect_namespace(*args: object, **kwargs: object) -> object:
        try:
            return original_inspect_namespace(*args, **kwargs)
        except pydantic.errors.PydanticUserError as exc:
            if "non-annotated attribute" not in str(exc):
                raise

            namespace = args[0] if args else kwargs.get("namespace")
            raw_annotations = args[1] if len(args) > 1 else kwargs.get("raw_annotations")
            if not isinstance(namespace, dict) or not isinstance(raw_annotations, dict):
                raise
            namespace_dict = cast("dict[str, object]", namespace)
            raw_annotations_dict = cast("dict[str, object]", raw_annotations)

            for field in (
                "chroma_coordinator_host",
                "chroma_logservice_host",
                "chroma_logservice_port",
            ):
                if field in namespace_dict and field not in raw_annotations_dict:
                    raw_annotations_dict[field] = type(namespace_dict[field])
            return original_inspect_namespace(*args, **kwargs)

    _model_construction.inspect_namespace = _patched_inspect_namespace
    _CHROMADB_PY314_PATCHED = True


def safe_replace(tmp_path: Path, target_path: Path) -> None:
    """Replace *target_path* with *tmp_path*, with a fallback for bind mounts.

    ``Path.replace`` performs an atomic rename which fails on some filesystems
    (e.g. Docker bind mounts) with ``OSError: [Errno 16] Device or resource
    busy``.  When that happens we fall back to a non-atomic copy.
    """
    try:
        tmp_path.replace(target_path)
    except OSError:
        shutil.copy2(tmp_path, target_path)
        tmp_path.unlink(missing_ok=True)


def ensure_writable_config_path(
    *,
    create_minimal: bool = False,
    runtime_paths: RuntimePaths,
) -> bool:
    """Ensure the writable config path exists when running from a managed template.

    Returns whether a config file exists after the call.
    """
    config_path = runtime_paths.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        return True

    template_env = runtime_paths.env_value("MINDROOM_CONFIG_TEMPLATE")
    template_path = Path(template_env).expanduser().resolve() if template_env else config_path
    if template_path != config_path and template_path.exists():
        shutil.copyfile(template_path, config_path)
        config_path.chmod(0o600)
        print(f"Seeded config from template {template_path} -> {config_path}")
        return True

    if not create_minimal:
        return False

    config_path.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
    config_path.chmod(0o600)
    print(f"Created new config file at {config_path}")
    return True
