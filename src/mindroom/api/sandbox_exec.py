"""Sandbox runner execution env and subprocess context helpers."""

from __future__ import annotations

import os
import secrets
import shutil
import site
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.tool_system.worker_routing import worker_dir_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.workers.backends.local import LocalWorkerStatePaths

DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0
WORKSPACE_ENV_HOOK_RELATIVE_PATH = Path(".mindroom") / "worker-env.sh"
WORKSPACE_ENV_HOOK_TIMEOUT_SECONDS = 10.0
_WORKSPACE_ENV_HOOK_MAX_SCRIPT_BYTES = 64 * 1024
_WORKSPACE_ENV_HOOK_MAX_OUTPUT_BYTES = 256 * 1024
_WORKSPACE_ENV_HOOK_MAX_VALUE_BYTES = 32 * 1024
_WORKSPACE_ENV_HOOK_MAX_OVERLAY_BYTES = 128 * 1024
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
        "LD_LIBRARY_PATH",
        "LC_ALL",
        "LC_CTYPE",
        "NIX_LD",
        "NIX_LD_LIBRARY_PATH",
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


class WorkspaceEnvHookError(RuntimeError):
    """One `.mindroom/worker-env.sh` request failed sourcing or validation."""


def resolve_workspace_env_hook_path(base_dir: Path | str | None) -> Path | None:
    """Resolve the workspace env hook for one effective base_dir, if any.

    Returns the resolved file path when a regular file exists at
    `<base_dir>/.mindroom/worker-env.sh` and stays inside the resolved
    base_dir. Returns None when the file is absent. Raises
    `WorkspaceEnvHookError` when the candidate escapes the base_dir
    (including symlink escape) or exceeds the size cap.
    """
    if base_dir is None:
        return None
    base_path = Path(base_dir).expanduser()
    try:
        base_resolved = base_path.resolve()
    except OSError as exc:
        msg = f"Failed to resolve base_dir for .mindroom/worker-env.sh: {exc}"
        raise WorkspaceEnvHookError(msg) from exc
    candidate = base_resolved / WORKSPACE_ENV_HOOK_RELATIVE_PATH
    if not candidate.exists():
        return None
    try:
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        msg = f"Failed to resolve .mindroom/worker-env.sh: {exc}"
        raise WorkspaceEnvHookError(msg) from exc
    if not candidate_resolved.is_relative_to(base_resolved):
        msg = (
            f".mindroom/worker-env.sh resolves outside of {base_resolved}; "
            "agent-editable workspace hooks must stay inside the resolved tool workspace."
        )
        raise WorkspaceEnvHookError(msg)
    if not candidate_resolved.is_file():
        return None
    try:
        size = candidate_resolved.stat().st_size
    except OSError as exc:
        msg = f"Failed to stat .mindroom/worker-env.sh: {exc}"
        raise WorkspaceEnvHookError(msg) from exc
    if size > _WORKSPACE_ENV_HOOK_MAX_SCRIPT_BYTES:
        msg = f".mindroom/worker-env.sh is too large ({size} bytes; limit {_WORKSPACE_ENV_HOOK_MAX_SCRIPT_BYTES})."
        raise WorkspaceEnvHookError(msg)
    return candidate_resolved


def source_workspace_env_hook(
    *,
    hook_path: Path,
    base_env: Mapping[str, str],
    cwd: Path,
) -> dict[str, str]:
    """Source the hook with `base_env` and return new/changed exported values.

    Bash sources the script without `set -a`, so agents must write
    `export FOO=bar` for values to overlay; bare `FOO=bar` does not persist.
    A high-entropy capture marker separates anything the script printed to
    stdout from the NUL-separated `printenv -0` block we read afterwards. The
    runner keeps only entries whose name passes
    `constants.is_workspace_env_overlay_name_allowed` and whose value differs
    from `base_env`.

    Raises `WorkspaceEnvHookError` on timeout, non-zero exit, missing capture
    marker, missing bash, or oversized output.
    """
    bash_path = _resolve_bash(base_env)
    capture_marker = secrets.token_hex(16)
    bash_script = '. "$1"; printf "%s\\0" "$2"; printenv -0'
    try:
        completed = subprocess.run(
            [bash_path, "-c", bash_script, "bash", str(hook_path), capture_marker],
            input=b"",
            capture_output=True,
            cwd=str(cwd),
            env=dict(base_env),
            timeout=WORKSPACE_ENV_HOOK_TIMEOUT_SECONDS,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f".mindroom/worker-env.sh timed out after {WORKSPACE_ENV_HOOK_TIMEOUT_SECONDS} seconds."
        raise WorkspaceEnvHookError(msg) from exc
    except OSError as exc:
        msg = f"Failed to start bash for .mindroom/worker-env.sh: {exc}"
        raise WorkspaceEnvHookError(msg) from exc
    if completed.returncode != 0:
        excerpt = (completed.stderr or completed.stdout or b"").decode(errors="replace")[:512].strip()
        suffix = f" output: {excerpt}" if excerpt else ""
        msg = f".mindroom/worker-env.sh exited with code {completed.returncode}.{suffix}"
        raise WorkspaceEnvHookError(msg)
    if len(completed.stdout) > _WORKSPACE_ENV_HOOK_MAX_OUTPUT_BYTES:
        msg = (
            f".mindroom/worker-env.sh produced {len(completed.stdout)} bytes of "
            f"capture output (limit {_WORKSPACE_ENV_HOOK_MAX_OUTPUT_BYTES})."
        )
        raise WorkspaceEnvHookError(msg)
    return _parse_workspace_env_hook_output(
        completed.stdout,
        capture_marker,
        base_env=base_env,
    )


def _resolve_bash(base_env: Mapping[str, str]) -> str:
    bash_path = shutil.which("bash", path=base_env.get("PATH")) or shutil.which("bash")
    if bash_path is not None:
        return bash_path
    if os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    msg = "bash is required to source .mindroom/worker-env.sh and was not found."
    raise WorkspaceEnvHookError(msg)


def _parse_workspace_env_hook_output(
    raw: bytes,
    capture_marker: str,
    *,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    text = raw.decode("utf-8", errors="replace")
    marker_chunk = capture_marker + "\0"
    marker_index = text.find(marker_chunk)
    if marker_index < 0:
        msg = ".mindroom/worker-env.sh output did not include the expected env capture marker."
        raise WorkspaceEnvHookError(msg)
    env_block = text[marker_index + len(marker_chunk) :]
    overlay: dict[str, str] = {}
    total_bytes = 0
    for chunk in env_block.split("\0"):
        kv = _accept_overlay_chunk(chunk, base_env=base_env)
        if kv is None:
            continue
        key, value = kv
        overlay[key] = value
        total_bytes += len(key) + 1 + len(value)
        if total_bytes > _WORKSPACE_ENV_HOOK_MAX_OVERLAY_BYTES:
            break
    return overlay


def _accept_overlay_chunk(
    chunk: str,
    *,
    base_env: Mapping[str, str],
) -> tuple[str, str] | None:
    sep_idx = chunk.find("=") if chunk else -1
    if sep_idx <= 0:
        return None
    key = chunk[:sep_idx]
    value = chunk[sep_idx + 1 :]
    if (
        not _is_valid_env_name(key)
        or key in constants.WORKSPACE_ENV_OVERLAY_TRANSIENT_NAMES
        or not constants.is_workspace_env_overlay_name_allowed(key)
        or base_env.get(key) == value
        or len(value.encode("utf-8")) > _WORKSPACE_ENV_HOOK_MAX_VALUE_BYTES
    ):
        return None
    return key, value


def _is_valid_env_name(name: str) -> bool:
    if not name or name[0].isdigit():
        return False
    return all(ch == "_" or (ch.isascii() and ch.isalnum()) for ch in name)
