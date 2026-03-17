"""Sandbox runner execution env and subprocess context helpers."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.tool_system.worker_routing import worker_dir_name

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.workers.backends.local import LocalWorkerStatePaths

DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0
RUNNER_EXECUTION_MODE_ENV = "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"
RUNNER_SUBPROCESS_TIMEOUT_ENV = "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS"
DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"
SHARED_STORAGE_ROOT_ENV = "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"
KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV = "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX"
DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX = "workers"
EXECUTION_ENV_TOOL_NAMES = frozenset({"python", "shell"})
SUBPROCESS_ENV_PASSTHROUGH_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)


def runner_execution_mode(runtime_paths: RuntimePaths) -> str:
    """Return the configured sandbox runner execution mode."""
    return (runtime_paths.env_value(RUNNER_EXECUTION_MODE_ENV, default="inprocess") or "inprocess").strip().lower()


def runner_uses_subprocess(runtime_paths: RuntimePaths) -> bool:
    """Return whether the runner should dispatch through a subprocess."""
    return runner_execution_mode(runtime_paths) == "subprocess"


def runner_subprocess_timeout_seconds(runtime_paths: RuntimePaths) -> float:
    """Return the bounded subprocess timeout for sandbox execution."""
    raw_timeout = runtime_paths.env_value(
        RUNNER_SUBPROCESS_TIMEOUT_ENV,
        default=str(DEFAULT_SUBPROCESS_TIMEOUT_SECONDS),
    )
    try:
        timeout = float(raw_timeout or DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)
    except ValueError:
        timeout = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    return max(1.0, timeout)


def runner_dedicated_worker_key(runtime_paths: RuntimePaths) -> str | None:
    """Return the pinned dedicated worker key when configured."""
    raw = (runtime_paths.env_value(DEDICATED_WORKER_KEY_ENV, default="") or "").strip()
    return raw or None


def runner_dedicated_worker_root(runtime_paths: RuntimePaths) -> Path | None:
    """Return the dedicated worker root visible to this runner."""
    dedicated_root = (runtime_paths.env_value(DEDICATED_WORKER_ROOT_ENV, default="") or "").strip()
    if dedicated_root:
        return Path(dedicated_root).expanduser().resolve()
    return runtime_paths.storage_root.resolve()


def shared_root_from_dedicated_worker_root(
    *,
    dedicated_root: Path,
    worker_key: str,
    storage_subpath_prefix: str,
) -> Path | None:
    """Recover the shared storage root from `<shared>/<prefix>/<worker-dir>`."""
    resolved_dedicated_root = dedicated_root.expanduser().resolve()
    if resolved_dedicated_root.name != worker_dir_name(worker_key):
        return None

    prefix_parts = tuple(Path(storage_subpath_prefix.strip("/")).parts)
    parent = resolved_dedicated_root.parent
    for expected_part in reversed(prefix_parts):
        if parent.name != expected_part:
            return None
        parent = parent.parent
    return parent.resolve()


def runner_shared_storage_root(runtime_paths: RuntimePaths) -> Path | None:
    """Return the shared storage root for worker-visible agent paths."""
    shared_root = (runtime_paths.env_value(SHARED_STORAGE_ROOT_ENV, default="") or "").strip()
    if shared_root:
        return Path(shared_root).expanduser().resolve()

    dedicated_root = runner_dedicated_worker_root(runtime_paths)
    worker_key = runner_dedicated_worker_key(runtime_paths)
    if dedicated_root is None or worker_key is None:
        return None

    raw_storage_subpath_prefix = runtime_paths.env_value(
        KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV,
        default=DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX,
    )
    storage_subpath_prefix = (raw_storage_subpath_prefix or DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX).strip() or (
        DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX
    )
    return shared_root_from_dedicated_worker_root(
        dedicated_root=dedicated_root,
        worker_key=worker_key,
        storage_subpath_prefix=storage_subpath_prefix,
    )


def runner_storage_root(runtime_paths: RuntimePaths) -> Path:
    """Return the storage root used for worker path validation."""
    if shared_root := runner_shared_storage_root(runtime_paths):
        return shared_root
    return runtime_paths.storage_root.resolve()


def runner_uses_dedicated_worker(runtime_paths: RuntimePaths) -> bool:
    """Return whether this runner is pinned to one dedicated worker."""
    return runner_dedicated_worker_key(runtime_paths) is not None


def request_execution_env(
    tool_name: str,
    execution_env: dict[str, str] | None,
    runtime_paths: RuntimePaths,
) -> dict[str, str]:
    """Return the effective runtime-scoped execution env for one request."""
    if execution_env:
        return dict(execution_env)
    if tool_name not in EXECUTION_ENV_TOOL_NAMES:
        return {}
    return dict(constants.execution_runtime_env_values(runtime_paths))


def runtime_paths_with_execution_env(
    runtime_paths: RuntimePaths,
    execution_env: dict[str, str],
) -> RuntimePaths:
    """Return runtime paths overlaid with one execution env snapshot."""
    if not execution_env:
        return runtime_paths

    process_env = dict(runtime_paths.process_env)
    process_env.update(execution_env)
    return constants.RuntimePaths(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env=MappingProxyType(process_env),
        env_file_values=runtime_paths.env_file_values,
    )


def project_src_path() -> Path:
    """Return the repository `src/` root used in worker subprocesses."""
    return Path(__file__).resolve().parents[2]


def current_runtime_site_packages() -> list[str]:
    """Return site-packages paths visible to the current Python runtime."""
    site_package_paths = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        site_package_paths.append(user_site)

    discovered_paths: list[str] = []
    for path_text in site_package_paths:
        path = Path(path_text).expanduser()
        if path.is_dir():
            discovered_paths.append(str(path.resolve()))

    return list(dict.fromkeys(discovered_paths))


def subprocess_passthrough_env() -> dict[str, str]:
    """Return the small set of host env vars forwarded to subprocesses."""
    return {key: value for key, value in os.environ.items() if key in SUBPROCESS_ENV_PASSTHROUGH_KEYS}


def generic_subprocess_env() -> dict[str, str]:
    """Build the baseline subprocess env for non-worker execution."""
    env = subprocess_passthrough_env()
    for key in ("HOME", "PATH", "PYTHONPATH", "VIRTUAL_ENV"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def worker_subprocess_env(paths: LocalWorkerStatePaths) -> dict[str, str]:
    """Build the subprocess env for one prepared local worker."""
    env = generic_subprocess_env()
    env["HOME"] = str(paths.root)
    env["XDG_CACHE_HOME"] = str(paths.cache_dir)
    env["PIP_CACHE_DIR"] = str(paths.cache_dir / "pip")
    env["UV_CACHE_DIR"] = str(paths.cache_dir / "uv")
    env["PYTHONPYCACHEPREFIX"] = str(paths.cache_dir / "pycache")
    env["VIRTUAL_ENV"] = str(paths.venv_dir)

    current_path = env.get("PATH", "")
    env["PATH"] = f"{paths.venv_dir / 'bin'}:{current_path}" if current_path else str(paths.venv_dir / "bin")

    python_path_parts = [str(project_src_path()), *current_runtime_site_packages()]
    existing_python_path = env.get("PYTHONPATH", "")
    if existing_python_path:
        python_path_parts.append(existing_python_path)
    env["PYTHONPATH"] = ":".join(python_path_parts)
    return env


def resolve_subprocess_worker_context(
    paths: LocalWorkerStatePaths | None,
) -> tuple[str | None, dict[str, str] | None, str | None]:
    """Return the python executable, env, and cwd for subprocess dispatch."""
    if paths is None:
        return sys.executable, generic_subprocess_env(), str(Path.cwd())

    return (
        str(paths.venv_dir / "bin" / "python"),
        worker_subprocess_env(paths),
        str(paths.workspace),
    )


def subprocess_env_for_request(
    base_env: dict[str, str] | None,
    execution_env: dict[str, str],
) -> dict[str, str] | None:
    """Overlay request execution env onto one subprocess env snapshot."""
    if base_env is None:
        return None
    if not execution_env:
        return base_env

    env = dict(base_env)
    env.update(execution_env)
    return env


def subprocess_worker_command(
    subprocess_worker_arg: str,
    *,
    python_executable: str | None = None,
) -> list[str]:
    """Build the sandbox subprocess worker command line."""
    return [python_executable or sys.executable, "-m", "mindroom.api.sandbox_runner", subprocess_worker_arg]
