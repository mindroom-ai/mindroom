"""Shared constants for the mindroom package.

This module contains constants that are used across multiple modules
to avoid circular imports. It does not import anything from the internal
codebase.
"""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import dotenv_values, load_dotenv

load_dotenv()

# Agent names
ROUTER_AGENT_NAME = "router"

# Default path to agents configuration file. Allow overriding via environment
# variable so deployments can place the writable configuration file on a
# persistent volume instead of the package directory (which may be read-only).
_CONFIG_PATH_ENV = os.getenv("MINDROOM_CONFIG_PATH")

# Search order for existing files: env var > ./config.yaml > ~/.mindroom/config.yaml
_CONFIG_SEARCH_PATHS = [Path("config.yaml"), Path.home() / ".mindroom" / "config.yaml"]
_RUNTIME_PATH_ENV_KEYS = frozenset({"MINDROOM_CONFIG_PATH", "MINDROOM_STORAGE_PATH"})


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved runtime path contract shared across the process."""

    config_path: Path
    config_dir: Path
    env_path: Path
    storage_root: Path


CONFIG_PATH: Path
STORAGE_PATH: str
STORAGE_PATH_OBJ: Path
MATRIX_STATE_FILE: Path
TRACKING_DIR: Path
_MEMORY_DIR: Path
CREDENTIALS_DIR: Path
ENCRYPTION_KEYS_DIR: Path


def config_search_locations() -> list[Path]:
    """Return the ordered list of locations where MindRoom looks for config.

    This is the single source of truth for config file discovery.
    """
    seen: set[Path] = set()
    locations: list[Path] = []
    if _CONFIG_PATH_ENV:
        resolved = Path(_CONFIG_PATH_ENV).expanduser().resolve()
        seen.add(resolved)
        locations.append(resolved)
    for p in _CONFIG_SEARCH_PATHS:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            locations.append(resolved)
    return locations


def _storage_root_from_env_path(env_path: Path) -> Path | None:
    """Read MINDROOM_STORAGE_PATH from one env file when present."""
    if not env_path.is_file():
        return None
    value = dotenv_values(env_path).get("MINDROOM_STORAGE_PATH")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _runtime_env_file_values(paths: RuntimePaths) -> dict[str, str]:
    """Read string env values from one runtime-adjacent `.env` file."""
    if not paths.env_path.is_file():
        return {}
    return {key: value for key, value in dotenv_values(paths.env_path).items() if isinstance(value, str)}


def resolve_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
) -> RuntimePaths:
    """Resolve the runtime config/env/storage paths for one execution context."""
    resolved_config_path = Path(config_path or find_config()).expanduser().resolve()
    config_dir = resolved_config_path.parent
    env_path = config_dir / ".env"

    if storage_path is not None:
        resolved_storage_root = Path(storage_path).expanduser().resolve()
    elif configured_storage_root := os.getenv("MINDROOM_STORAGE_PATH", "").strip():
        resolved_storage_root = Path(configured_storage_root).expanduser().resolve()
    elif env_storage_root := _storage_root_from_env_path(env_path):
        resolved_storage_root = env_storage_root
    else:
        resolved_storage_root = (config_dir / "mindroom_data").resolve()

    return RuntimePaths(
        config_path=resolved_config_path,
        config_dir=config_dir,
        env_path=env_path,
        storage_root=resolved_storage_root,
    )


def load_runtime_env(
    paths: RuntimePaths,
    *,
    sync_path_env: bool = True,
    override_existing: bool = False,
) -> None:
    """Load a runtime-adjacent env file without giving it ambient path authority."""
    for key, value in _runtime_env_file_values(paths).items():
        if key in _RUNTIME_PATH_ENV_KEYS:
            continue
        if override_existing or key not in os.environ:
            os.environ[key] = value
    if sync_path_env:
        os.environ["MINDROOM_CONFIG_PATH"] = str(paths.config_path)
        os.environ["MINDROOM_STORAGE_PATH"] = str(paths.storage_root)


def _expand_runtime_path_vars(value: str, paths: RuntimePaths) -> str:
    """Expand runtime path placeholders against explicit runtime paths first."""
    expanded = value.replace("${MINDROOM_CONFIG_PATH}", str(paths.config_path))
    expanded = expanded.replace("$MINDROOM_CONFIG_PATH", str(paths.config_path))
    expanded = expanded.replace("${MINDROOM_STORAGE_PATH}", str(paths.storage_root))
    expanded = expanded.replace("$MINDROOM_STORAGE_PATH", str(paths.storage_root))
    return os.path.expandvars(expanded)


def runtime_config_path(config_path: Path | None = None) -> Path:
    """Return the active runtime config path or one explicit config path."""
    return get_runtime_paths(config_path=config_path).config_path


def runtime_env_value(
    name: str,
    *,
    runtime_paths: RuntimePaths | None = None,
    default: str | None = None,
) -> str | None:
    """Resolve one runtime env value from explicit process env, then config-adjacent `.env`."""
    paths = runtime_paths or get_runtime_paths()

    if name == "MINDROOM_CONFIG_PATH":
        return str(paths.config_path)
    if name == "MINDROOM_STORAGE_PATH":
        return str(paths.storage_root)

    configured_value = os.getenv(name)
    if configured_value is not None:
        return configured_value

    file_value = _runtime_env_file_values(paths).get(name)
    if file_value is not None:
        return file_value
    return default


def runtime_env_flag(
    name: str,
    *,
    default: bool = False,
    runtime_paths: RuntimePaths | None = None,
) -> bool:
    """Read a boolean runtime env flag with config-adjacent `.env` fallback."""
    value = runtime_env_value(name, runtime_paths=runtime_paths)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def runtime_matrix_homeserver(*, runtime_paths: RuntimePaths | None = None) -> str:
    """Return the effective Matrix homeserver for one runtime context."""
    return (
        runtime_env_value(
            "MATRIX_HOMESERVER",
            runtime_paths=runtime_paths,
            default="http://localhost:8008",
        )
        or "http://localhost:8008"
    )


def get_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
) -> RuntimePaths:
    """Return the active runtime paths or resolve an explicit temporary context."""
    if config_path is None and storage_path is None:
        return _ACTIVE_RUNTIME_PATHS
    return resolve_runtime_paths(config_path=config_path, storage_path=storage_path)


def _set_active_runtime_paths(paths: RuntimePaths) -> RuntimePaths:
    """Commit one runtime path context as the process-wide source of truth."""
    global _ACTIVE_RUNTIME_PATHS
    global CONFIG_PATH, STORAGE_PATH, STORAGE_PATH_OBJ, MATRIX_STATE_FILE, TRACKING_DIR, _MEMORY_DIR, CREDENTIALS_DIR
    global ENCRYPTION_KEYS_DIR

    _ACTIVE_RUNTIME_PATHS = paths
    CONFIG_PATH = paths.config_path
    STORAGE_PATH = str(paths.storage_root)
    STORAGE_PATH_OBJ = paths.storage_root
    MATRIX_STATE_FILE = STORAGE_PATH_OBJ / "matrix_state.yaml"
    TRACKING_DIR = STORAGE_PATH_OBJ / "tracking"
    _MEMORY_DIR = STORAGE_PATH_OBJ / "memory"
    CREDENTIALS_DIR = STORAGE_PATH_OBJ / "credentials"
    ENCRYPTION_KEYS_DIR = STORAGE_PATH_OBJ / "encryption_keys"
    return _ACTIVE_RUNTIME_PATHS


def set_runtime_paths(
    *,
    config_path: Path | None = None,
    storage_path: Path | None = None,
) -> RuntimePaths:
    """Resolve, load, and commit one runtime path context for the process."""
    paths = resolve_runtime_paths(config_path=config_path, storage_path=storage_path)
    load_runtime_env(paths, sync_path_env=True)
    return _set_active_runtime_paths(paths)


def resolve_config_relative_path(raw_path: str | Path, *, config_path: Path | None = None) -> Path:
    """Resolve a configured path, treating relative values as config-directory-relative.

    Environment variables are expanded first so configs can anchor paths to the
    active runtime storage root via values such as `${MINDROOM_STORAGE_PATH}`.
    """
    paths = get_runtime_paths(config_path=config_path)
    unresolved = Path(_expand_runtime_path_vars(os.fspath(raw_path), paths)).expanduser()
    if unresolved.is_absolute():
        return unresolved.resolve()
    return (paths.config_dir / unresolved).resolve()


def _docker_container_enabled() -> bool:
    """Return whether MindRoom is running from the packaged container image."""
    return os.getenv("DOCKER_CONTAINER", "").strip().lower() in {"1", "true", "yes", "on"}


def _use_storage_path_for_workspace_assets(config_path: Path | None = None) -> bool:
    """Return whether writable workspace assets should live under persistent storage."""
    if not _docker_container_enabled():
        return False
    if config_path is None:
        return True
    return config_path.expanduser().resolve() == get_runtime_paths().config_path


def avatars_dir(*, config_path: Path | None = None) -> Path:
    """Return the writable avatars directory for the active workspace.

    Source checkouts keep avatars next to the active config file so generated
    assets can be committed with the workspace.
    Containerized deployments usually mount `config.yaml` as a single file, so
    config-adjacent writes would be ephemeral; in that case, store writable
    overrides under the persistent MindRoom storage root instead.
    """
    paths = get_runtime_paths(config_path=config_path)
    if _use_storage_path_for_workspace_assets(config_path):
        return paths.storage_root / "avatars"
    return paths.config_dir / "avatars"


def bundled_avatars_dir() -> Path:
    """Return the bundled avatar directory shipped with a source checkout or runtime image."""
    return Path(__file__).resolve().parents[2] / "avatars"


def workspace_avatar_path(
    entity_type: str,
    entity_name: str,
    *,
    config_path: Path | None = None,
) -> Path:
    """Return the writable workspace avatar path for a managed entity."""
    return avatars_dir(config_path=config_path) / entity_type / f"{entity_name}.png"


def resolve_avatar_path(
    entity_type: str,
    entity_name: str,
    *,
    config_path: Path | None = None,
) -> Path:
    """Return the best available avatar path for a managed entity.

    Prefer a writable workspace override.
    Fall back to the bundled runtime assets when no workspace file exists yet.
    If neither exists, return the intended workspace path so callers that write
    new avatars know where to place them.
    """
    workspace_path = workspace_avatar_path(entity_type, entity_name, config_path=config_path)
    if workspace_path.exists():
        return workspace_path

    bundled_path = bundled_avatars_dir() / entity_type / f"{entity_name}.png"
    if bundled_path.exists():
        return bundled_path

    return workspace_path


def find_config() -> Path:
    """Find the first existing config file, or fall back to ~/.mindroom/config.yaml.

    Returns the original (possibly relative) path, not a resolved one,
    so that derived paths like STORAGE_PATH stay relative and display
    cleanly in CLI help text.
    """
    if _CONFIG_PATH_ENV:
        return Path(_CONFIG_PATH_ENV).expanduser()
    for path in _CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return _CONFIG_SEARCH_PATHS[-1]  # default to ~/.mindroom/config.yaml for creation


_ACTIVE_RUNTIME_PATHS = resolve_runtime_paths()


# Optional template path used to seed the writable config file if it does not
# exist yet. Defaults to the same location as CONFIG_PATH so the
# behaviour is unchanged when no overrides are provided.
_CONFIG_TEMPLATE_ENV = os.getenv("MINDROOM_CONFIG_TEMPLATE")
_set_active_runtime_paths(_ACTIVE_RUNTIME_PATHS)
load_runtime_env(get_runtime_paths(), sync_path_env=False)


def set_runtime_storage_path(storage_path: Path) -> Path:
    """Update the process-wide runtime storage root.

    `mindroom run --storage-path ...` should behave the same as setting
    `MINDROOM_STORAGE_PATH` before startup, so runtime code only has one
    storage-root contract to reason about.
    """
    return set_runtime_paths(storage_path=storage_path).storage_root


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
ENABLE_AI_CACHE = env_flag("MINDROOM_ENABLE_AI_CACHE", default=True)

# Matrix
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
# (for federation setups where hostname != server_name)
MATRIX_SERVER_NAME = os.getenv("MATRIX_SERVER_NAME", None)
# Optional installation namespace suffix used to avoid collisions on shared homeservers.
# When set, managed users/rooms are namespaced as "<name>_<namespace>".
MINDROOM_NAMESPACE = os.getenv("MINDROOM_NAMESPACE", "").strip().lower() or None

# Placeholder used in starter config templates. `mindroom connect` can
# automatically replace this token with the owner Matrix user ID returned
# by the provisioning service.
OWNER_MATRIX_USER_ID_PLACEHOLDER = "__MINDROOM_OWNER_USER_ID_FROM_PAIRING__"
MATRIX_SSL_VERIFY = env_flag("MATRIX_SSL_VERIFY", default=True)


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


def ensure_writable_config_path(*, create_minimal: bool = False) -> bool:
    """Ensure the writable config path exists when running from a managed template.

    Returns whether a config file exists after the call.
    """
    paths = get_runtime_paths()
    config_path = paths.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        return True

    template_path = Path(_CONFIG_TEMPLATE_ENV).expanduser().resolve() if _CONFIG_TEMPLATE_ENV else config_path
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
