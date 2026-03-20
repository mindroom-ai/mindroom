"""Projected config snapshots for Docker-backed workers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import yaml

from mindroom.agent_policy import ResolvedAgentPolicy, build_agent_policy_seeds, resolve_agent_policy_index
from mindroom.constants import config_relative_path, resolve_config_relative_path
from mindroom.tool_system.worker_routing import resolve_agent_owned_path, resolved_worker_key_scope
from mindroom.workers.backend import WorkerBackendError
from mindroom.workspaces import (
    iter_local_copy_source_entries,
    validate_local_copy_source_dir,
    validate_local_copy_source_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import WorkerScope
    from mindroom.workers.backends.docker_config import _DockerWorkerBackendConfig
    from mindroom.workers.backends.local import LocalWorkerStatePaths

_PROJECTED_ASSETS_DIRNAME = ".mindroom-worker-assets"
_PROJECTED_CONFIGS_DIRNAME = ".mindroom-worker-config-projections"
_WORKER_CONFIG_STATE_DIRNAME = ".mindroom-worker-config-state"
_PROJECTION_READY_FILENAME = ".projection-ready"
_REDACTED_CONFIG_VALUE = "__REDACTED__"
_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "client_secret",
        "long_lived_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    },
)
_SENSITIVE_CONFIG_KEY_SUFFIXES = ("_api_key", "_password", "_secret", "_token")
_NON_SECRET_SENSITIVE_SUFFIX_EXCEPTIONS = frozenset({"no_reply_token", "token_uri"})
_SENSITIVE_HEADER_KEYS = frozenset({"authorization", "proxy_authorization"})


def _container_config_dir(config_path: str) -> str:
    return str(PurePosixPath(config_path).parent)


def _projected_config_value(relative_path: PurePosixPath) -> str:
    return f"./{relative_path.as_posix()}"


def _safe_projection_name(raw_value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_value).strip(".-")
    return normalized or "item"


def _projection_display_name(host_path: Path, *, fallback: str) -> str:
    return _safe_projection_name(host_path.name or fallback)


def _projection_hash(raw_value: str, *, length: int = 8) -> str:
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:length]


def _projection_path_with_suffix(relative_path: PurePosixPath, *, suffix: str) -> PurePosixPath:
    if relative_path.suffix:
        name = f"{relative_path.stem}-{suffix}{relative_path.suffix}"
    else:
        name = f"{relative_path.name}-{suffix}"
    return relative_path.with_name(name)


def _ordered_unique_nonempty_strings(values: Iterable[object]) -> tuple[str, ...]:
    ordered_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or value in seen_values:
            continue
        seen_values.add(value)
        ordered_values.append(value)
    return tuple(ordered_values)


def _plugin_uses_filesystem_path(plugin_path: str, *, runtime_paths: RuntimePaths) -> bool:
    if plugin_path.startswith(("python:", "pkg:", "module:")):
        return False
    candidate = resolve_config_relative_path(plugin_path, runtime_paths)
    if candidate.exists():
        return True
    unresolved = Path(plugin_path).expanduser()
    return unresolved.is_absolute() or plugin_path.startswith((".", "~")) or "/" in plugin_path or "\\" in plugin_path


def _normalized_config_key(raw_key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw_key.strip().lower()).strip("_")


def _config_key_is_sensitive(raw_key: str) -> bool:
    normalized_key = _normalized_config_key(raw_key)
    if normalized_key in _NON_SECRET_SENSITIVE_SUFFIX_EXCEPTIONS:
        return False
    if normalized_key in _SENSITIVE_CONFIG_KEYS:
        return True
    return any(normalized_key.endswith(suffix) for suffix in _SENSITIVE_CONFIG_KEY_SUFFIXES)


def _config_key_is_header_container(raw_key: str | None) -> bool:
    if raw_key is None:
        return False
    normalized_key = _normalized_config_key(raw_key)
    return normalized_key == "headers" or normalized_key.endswith("_headers")


def _header_key_is_sensitive(raw_key: str) -> bool:
    normalized_key = _normalized_config_key(raw_key)
    return normalized_key in _SENSITIVE_HEADER_KEYS or _config_key_is_sensitive(raw_key)


def _redact_sensitive_config_values(value: object, *, parent_key: str | None = None) -> object:
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        inside_header_mapping = _config_key_is_header_container(parent_key)
        for key, item in value.items():
            if isinstance(key, str) and (
                _header_key_is_sensitive(key) if inside_header_mapping else _config_key_is_sensitive(key)
            ):
                redacted[key] = _REDACTED_CONFIG_VALUE
                continue
            redacted[key] = _redact_sensitive_config_values(item, parent_key=key if isinstance(key, str) else None)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_config_values(item, parent_key=parent_key) for item in value]
    return value


def _mode_bits(st_mode: int) -> int:
    return stat.S_IMODE(st_mode)


def _file_state_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"file:{stat.st_size}:{stat.st_mtime_ns}:{_mode_bits(stat.st_mode)}"


def _directory_state_fingerprint(path: Path) -> str:
    root_stat = path.stat()
    state_entries = [f"root-dir::{_mode_bits(root_stat.st_mode)}"]
    for asset_path, relative_path in iter_local_copy_source_entries(path):
        stat = asset_path.stat()
        kind = "dir" if asset_path.is_dir() else "file"
        size = 0 if asset_path.is_dir() else stat.st_size
        state_entries.append(
            f"{kind}:{relative_path.as_posix()}:{size}:{stat.st_mtime_ns}:{_mode_bits(stat.st_mode)}",
        )
    return hashlib.sha256("\0".join(state_entries).encode("utf-8")).hexdigest()


def _validated_asset_host_path(host_path: Path) -> Path:
    try:
        return validate_local_copy_source_path(
            host_path,
            field_name="Docker worker asset",
        )
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc


def _path_state_fingerprint(host_path: Path) -> str:
    resolved_host_path = _validated_asset_host_path(host_path)
    if resolved_host_path.is_dir():
        return f"dir:{_directory_state_fingerprint(resolved_host_path)}"
    return _file_state_fingerprint(resolved_host_path)


def _compute_path_contents_hash(host_path: Path) -> str:
    resolved_host_path = _validated_asset_host_path(host_path)
    hasher = hashlib.sha256()
    try:
        if resolved_host_path.is_dir():
            root_stat = resolved_host_path.stat()
            hasher.update(f"root-dir:{_mode_bits(root_stat.st_mode)}\0".encode())
            for asset_path, relative_path in iter_local_copy_source_entries(resolved_host_path):
                asset_stat = asset_path.stat()
                if asset_path.is_dir():
                    hasher.update(f"dir:{relative_path.as_posix()}:{_mode_bits(asset_stat.st_mode)}\0".encode())
                    continue

                hasher.update(f"file:{relative_path.as_posix()}:{_mode_bits(asset_stat.st_mode)}\0".encode())
                with asset_path.open("rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
            return hasher.hexdigest()

        file_stat = resolved_host_path.stat()
        hasher.update(f"file:{_mode_bits(file_stat.st_mode)}\0".encode())
        with resolved_host_path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as exc:
        msg = f"Failed to read Docker worker asset '{resolved_host_path}': {exc}"
        raise WorkerBackendError(msg) from exc


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def _copy_directory_tree(source_dir: Path, destination_dir: Path) -> None:
    entries = iter_local_copy_source_entries(source_dir)
    for source_path, relative_path in entries:
        destination_path = destination_dir.joinpath(*relative_path.parts)
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)

    for source_path, relative_path in reversed(entries):
        if not source_path.is_dir():
            continue
        destination_dir.joinpath(*relative_path.parts).chmod(_mode_bits(source_path.stat().st_mode))
    destination_dir.chmod(_mode_bits(source_dir.stat().st_mode))


@dataclass(frozen=True, slots=True)
class _DockerProjectedConfigAsset:
    host_path: Path
    relative_path: PurePosixPath

    @property
    def is_directory(self) -> bool:
        return self.host_path.is_dir()


@dataclass(frozen=True, slots=True)
class _DockerProjectedConfig:
    root: Path
    projected_yaml: str
    assets: tuple[_DockerProjectedConfigAsset, ...]
    ready: bool


class DockerProjectionManager:
    """Build projected config snapshots for dedicated Docker workers."""

    def __init__(
        self,
        *,
        config: _DockerWorkerBackendConfig,
        projected_configs_root: Path,
        runtime_paths: RuntimePaths,
    ) -> None:
        self.config = config
        self._projected_configs_root = projected_configs_root
        self._runtime_paths = runtime_paths
        self._asset_hash_cache: dict[Path, tuple[str, str]] = {}
        self._config_data_cache: tuple[Path, str, dict[str, object]] | None = None

    def config_mount_specs(
        self,
        paths: LocalWorkerStatePaths,
        *,
        worker_key: str | None = None,
        materialize_projection: bool = True,
    ) -> tuple[list[tuple[Path, str, bool]], _DockerProjectedConfig | None]:
        """Return projected config mount specs plus the selected projection, if any."""
        if self.config.host_config_path is None:
            return [], None

        projection = self.projected_config(
            paths,
            worker_key=worker_key,
            materialize=materialize_projection,
        )
        config_dir = PurePosixPath(_container_config_dir(self.config.config_path))
        return [(projection.root, str(config_dir), True)], projection

    def projected_config(
        self,
        paths: LocalWorkerStatePaths,
        *,
        worker_key: str | None = None,
        materialize: bool = True,
    ) -> _DockerProjectedConfig:
        """Return the projected config snapshot for one worker root."""
        host_config_path = self.config.host_config_path
        if host_config_path is None:
            msg = "Projected Docker worker config requires a host config path."
            raise WorkerBackendError(msg)

        config_data = self._load_host_config_data(host_config_path)
        resolved_agent_policies = self._resolved_agent_policies(config_data)
        asset_paths_by_host: dict[Path, PurePosixPath] = {}
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path] = {}
        assets: list[_DockerProjectedConfigAsset] = []
        projected_agent_names = self._projected_agent_names(
            worker_key=worker_key,
            resolved_agent_policies=resolved_agent_policies,
        )
        projected_knowledge_base_ids = self._projected_knowledge_base_ids(
            config_data,
            agent_names=projected_agent_names,
            resolved_agent_policies=resolved_agent_policies,
        )
        self._rewrite_projected_config_paths(
            config_data,
            worker_key,
            paths,
            projected_agent_names=projected_agent_names,
            projected_knowledge_base_ids=projected_knowledge_base_ids,
            asset_paths_by_host=asset_paths_by_host,
            host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
            assets=assets,
        )
        self._sanitize_projected_config_data(
            config_data,
            projected_agent_names=projected_agent_names,
            projected_knowledge_base_ids=projected_knowledge_base_ids,
        )

        projected_yaml = yaml.safe_dump(config_data, sort_keys=False, allow_unicode=True)
        projection_manifest = {
            "config_yaml": projected_yaml,
            "config_filename": PurePosixPath(self.config.config_path).name,
            "assets": [
                {
                    "host_path": str(asset.host_path),
                    "relative_path": asset.relative_path.as_posix(),
                    "kind": "dir" if asset.is_directory else "file",
                    "content_hash": self._asset_contents_hash(asset.host_path),
                }
                for asset in assets
            ],
        }
        projection_hash = hashlib.sha256(
            json.dumps(projection_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()[:12]
        projection_dir = self.worker_projected_configs_root(paths) / f"config-projection-{projection_hash}"
        projection = _DockerProjectedConfig(
            root=projection_dir,
            projected_yaml=projected_yaml,
            assets=tuple(assets),
            ready=False,
        )
        projection = _DockerProjectedConfig(
            root=projection.root,
            projected_yaml=projection.projected_yaml,
            assets=projection.assets,
            ready=self._projection_ready(projection),
        )
        if materialize:
            self._write_projected_config(projection)
            self.prune_projected_configs(paths, keep=projection.root)
            return _DockerProjectedConfig(
                root=projection.root,
                projected_yaml=projection.projected_yaml,
                assets=projection.assets,
                ready=True,
            )
        return projection

    def worker_projected_configs_root(self, paths: LocalWorkerStatePaths) -> Path:
        """Return the directory containing one worker root's projected config snapshots."""
        return self._projected_configs_root / paths.root.name

    def current_resolved_agent_policies(self) -> dict[str, ResolvedAgentPolicy]:
        """Return the current agent-policy view derived from the host config."""
        host_config_path = self.config.host_config_path
        if host_config_path is None:
            return {}
        return self._resolved_agent_policies(self._load_host_config_data(host_config_path))

    def remove_projected_configs(self, paths: LocalWorkerStatePaths) -> None:
        """Remove every projected config snapshot for one worker root."""
        projection_root = self.worker_projected_configs_root(paths)
        if projection_root.exists():
            shutil.rmtree(projection_root)

    def prune_projected_configs(self, paths: LocalWorkerStatePaths, *, keep: Path) -> None:
        """Remove stale projected config snapshots for one worker root."""
        projection_root = self.worker_projected_configs_root(paths)
        projection_root.mkdir(parents=True, exist_ok=True)
        for sibling in projection_root.iterdir():
            if sibling == keep:
                continue
            _remove_path(sibling)

    def _projection_ready(self, projection: _DockerProjectedConfig) -> bool:
        projection_root = projection.root
        required_paths: list[tuple[Path, bool]] = [
            (projection_root / PurePosixPath(self.config.config_path).name, False),
            (projection_root / ".env", False),
            (projection_root / _PROJECTION_READY_FILENAME, False),
        ]
        required_paths.extend(
            (projection_root.joinpath(*asset.relative_path.parts), asset.is_directory) for asset in projection.assets
        )
        return all(path.is_dir() if require_directory else path.is_file() for path, require_directory in required_paths)

    def _write_projected_config(self, projection: _DockerProjectedConfig) -> None:
        if self._projection_ready(projection):
            return

        if projection.root.exists():
            _remove_path(projection.root)

        temp_root = projection.root.with_name(
            f"{projection.root.name}.tmp-{os.getpid()}-{time.time_ns()}",
        )
        if temp_root.exists():
            _remove_path(temp_root)

        try:
            temp_root.mkdir(parents=True, exist_ok=True)
            (temp_root / PurePosixPath(self.config.config_path).name).write_text(
                projection.projected_yaml,
                encoding="utf-8",
            )
            (temp_root / ".env").write_text("", encoding="utf-8")
            for asset in projection.assets:
                placeholder_path = temp_root.joinpath(*asset.relative_path.parts)
                if asset.is_directory:
                    placeholder_path.mkdir(parents=True, exist_ok=True)
                    resolved_asset_dir = validate_local_copy_source_dir(
                        asset.host_path,
                        field_name="Docker worker asset",
                    )
                    _copy_directory_tree(resolved_asset_dir, placeholder_path)
                    continue
                placeholder_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(
                    validate_local_copy_source_path(
                        asset.host_path,
                        field_name="Docker worker asset",
                    ),
                    placeholder_path,
                )
            (temp_root / _PROJECTION_READY_FILENAME).write_text("ready\n", encoding="utf-8")
            temp_root.replace(projection.root)
        except Exception:
            _remove_path(temp_root)
            raise

    def _load_host_config_data(self, host_config_path: Path) -> dict[str, object]:
        resolved_host_config_path = host_config_path.expanduser().resolve()
        config_fingerprint = _path_state_fingerprint(resolved_host_config_path)
        cached = self._config_data_cache
        if cached is not None and cached[0] == resolved_host_config_path and cached[1] == config_fingerprint:
            return copy.deepcopy(cached[2])

        try:
            data = yaml.safe_load(resolved_host_config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            msg = f"Failed to read Docker worker config file '{resolved_host_config_path}': {exc}"
            raise WorkerBackendError(msg) from exc
        if not isinstance(data, dict):
            msg = f"Docker worker config file '{resolved_host_config_path}' must contain a YAML object."
            raise WorkerBackendError(msg)
        normalized_data = cast("dict[str, object]", data)
        self._config_data_cache = (resolved_host_config_path, config_fingerprint, copy.deepcopy(normalized_data))
        return copy.deepcopy(normalized_data)

    def _asset_contents_hash(self, host_path: Path) -> str:
        resolved_host_path = host_path.expanduser().resolve()
        state_fingerprint = _path_state_fingerprint(resolved_host_path)
        cached = self._asset_hash_cache.get(resolved_host_path)
        if cached is not None and cached[0] == state_fingerprint:
            return cached[1]

        content_hash = _compute_path_contents_hash(resolved_host_path)
        self._asset_hash_cache[resolved_host_path] = (state_fingerprint, content_hash)
        return content_hash

    def _rewrite_projected_config_paths(
        self,
        config_data: dict[str, object],
        worker_key: str | None,
        paths: LocalWorkerStatePaths,
        *,
        projected_agent_names: tuple[str, ...] | None,
        projected_knowledge_base_ids: tuple[str, ...] | None,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        self._rewrite_projected_plugin_paths(
            config_data,
            asset_paths_by_host,
            host_paths_by_relative_asset_path,
            assets,
        )
        self._rewrite_projected_knowledge_paths(
            config_data,
            projected_knowledge_base_ids,
            asset_paths_by_host,
            host_paths_by_relative_asset_path,
            assets,
        )
        self._rewrite_projected_agent_paths(
            config_data,
            projected_agent_names,
            asset_paths_by_host,
            host_paths_by_relative_asset_path,
            assets,
        )
        self._rewrite_projected_memory_paths(config_data, paths)
        if worker_key is not None:
            self._rewrite_unscoped_default_worker_scope(config_data, worker_key)

    def _sanitize_projected_config_data(
        self,
        config_data: dict[str, object],
        *,
        projected_agent_names: tuple[str, ...] | None,
        projected_knowledge_base_ids: tuple[str, ...] | None,
    ) -> None:
        raw_agents = config_data.get("agents")
        if isinstance(raw_agents, dict) and projected_agent_names is not None:
            agents = cast("dict[str, object]", raw_agents)
            filtered_agents: dict[str, dict[str, object]] = {}
            for name in projected_agent_names:
                agent_data = agents.get(name)
                if isinstance(agent_data, dict):
                    filtered_agents[name] = cast("dict[str, object]", agent_data)
            for agent_data in filtered_agents.values():
                raw_delegate_to = agent_data.get("delegate_to")
                if isinstance(raw_delegate_to, list):
                    agent_data["delegate_to"] = [
                        target for target in raw_delegate_to if isinstance(target, str) and target in filtered_agents
                    ]
            config_data["agents"] = filtered_agents

        raw_knowledge_bases = config_data.get("knowledge_bases")
        if isinstance(raw_knowledge_bases, dict) and projected_knowledge_base_ids is not None:
            knowledge_bases = cast("dict[str, object]", raw_knowledge_bases)
            config_data["knowledge_bases"] = {
                base_id: knowledge_bases[base_id]
                for base_id in projected_knowledge_base_ids
                if base_id in knowledge_bases
            }

        config_data["teams"] = {}
        config_data["cultures"] = {}
        config_data["room_models"] = {}
        config_data["bot_accounts"] = []
        config_data["authorization"] = {}
        config_data["matrix_room_access"] = {}
        config_data["matrix_space"] = {}
        config_data["mindroom_user"] = None

        redacted_data = _redact_sensitive_config_values(config_data)
        config_data.clear()
        config_data.update(cast("dict[str, object]", redacted_data))

    def _resolved_agent_policies(self, config_data: dict[str, object]) -> dict[str, ResolvedAgentPolicy]:
        raw_agents = config_data.get("agents")
        if not isinstance(raw_agents, dict):
            return {}

        agent_mappings = {
            agent_name: cast("dict[str, object]", raw_agent)
            for agent_name, raw_agent in raw_agents.items()
            if isinstance(agent_name, str) and isinstance(raw_agent, dict)
        }
        if not agent_mappings:
            return {}

        seeds = build_agent_policy_seeds(
            agent_mappings,
            default_worker_scope=self._default_projected_worker_scope(config_data),
        )
        return resolve_agent_policy_index(seeds).policies

    def _projected_agent_names(
        self,
        *,
        worker_key: str | None,
        resolved_agent_policies: dict[str, ResolvedAgentPolicy],
    ) -> tuple[str, ...] | None:
        if worker_key is None:
            return None

        worker_scope = resolved_worker_key_scope(worker_key)
        if worker_scope is None:
            msg = f"Unsupported worker key for projected agent selection: {worker_key}"
            raise WorkerBackendError(msg)
        if worker_scope == "user":
            user_scoped_agent_names = tuple(
                agent_name
                for agent_name, policy in resolved_agent_policies.items()
                if policy.effective_execution_scope == "user"
            )
            if user_scoped_agent_names:
                return user_scoped_agent_names
            if not resolved_agent_policies:
                return None
            msg = f"Worker key does not match any configured agent policy: {worker_key}"
            raise WorkerBackendError(msg)

        matching_agent_names = tuple(
            agent_name
            for agent_name, policy in resolved_agent_policies.items()
            if self._worker_key_targets_agent(
                worker_key,
                agent_name=agent_name,
                worker_scope=policy.effective_execution_scope,
            )
        )
        if matching_agent_names:
            return (matching_agent_names[0],)
        if not resolved_agent_policies:
            return None
        msg = f"Worker key does not match any configured agent policy: {worker_key}"
        raise WorkerBackendError(msg)

    def _projected_knowledge_base_ids(
        self,
        config_data: dict[str, object],
        *,
        agent_names: tuple[str, ...] | None,
        resolved_agent_policies: dict[str, ResolvedAgentPolicy],
    ) -> tuple[str, ...] | None:
        if agent_names is None:
            return None

        raw_agents = config_data.get("agents")
        if not isinstance(raw_agents, dict):
            return ()

        agents = cast("dict[object, object]", raw_agents)
        projected_knowledge_base_ids: list[str] = []
        for agent_name in agent_names:
            raw_agent = agents.get(agent_name)
            if not isinstance(raw_agent, dict):
                continue
            raw_knowledge_bases = cast("dict[str, object]", raw_agent).get("knowledge_bases")
            if not isinstance(raw_knowledge_bases, list):
                continue
            projected_knowledge_base_ids.extend(
                knowledge_base_id for knowledge_base_id in raw_knowledge_bases if isinstance(knowledge_base_id, str)
            )
            private_knowledge_base_id = (
                resolved_agent_policies[agent_name].private_knowledge_base_id
                if agent_name in resolved_agent_policies
                else None
            )
            if isinstance(private_knowledge_base_id, str):
                projected_knowledge_base_ids.append(private_knowledge_base_id)
        return _ordered_unique_nonempty_strings(projected_knowledge_base_ids)

    def _default_projected_worker_scope(self, config_data: dict[str, object]) -> WorkerScope | None:
        raw_defaults = config_data.get("defaults")
        if not isinstance(raw_defaults, dict):
            return None
        raw_worker_scope = cast("dict[str, object]", raw_defaults).get("worker_scope")
        if isinstance(raw_worker_scope, str) and raw_worker_scope in {
            "shared",
            "user",
            "user_agent",
        }:
            return cast("WorkerScope", raw_worker_scope)
        return None

    def _worker_key_targets_agent(
        self,
        worker_key: str,
        *,
        agent_name: str,
        worker_scope: WorkerScope | None,
    ) -> bool:
        if worker_scope is None:
            return worker_key.endswith(f":unscoped:{agent_name}")
        if worker_scope == "shared":
            return worker_key.endswith(f":shared:{agent_name}")
        if worker_scope == "user_agent":
            return ":user_agent:" in worker_key and worker_key.endswith(f":{agent_name}")
        return False

    def _rewrite_projected_plugin_paths(
        self,
        config_data: dict[str, object],
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        raw_plugins = config_data.get("plugins")
        if not isinstance(raw_plugins, list):
            return

        plugins = cast("list[object]", raw_plugins)
        for index, raw_plugin in enumerate(plugins):
            if not isinstance(raw_plugin, str) or not _plugin_uses_filesystem_path(
                raw_plugin,
                runtime_paths=self._runtime_paths,
            ):
                continue
            host_path = config_relative_path(raw_plugin, self._runtime_paths)
            plugins[index] = self._projected_path_value(
                host_path,
                PurePosixPath(
                    _PROJECTED_ASSETS_DIRNAME,
                    "plugins",
                    f"{index:02d}-{_projection_display_name(host_path, fallback=raw_plugin)}",
                ),
                asset_paths_by_host=asset_paths_by_host,
                host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
                assets=assets,
            )

    def _rewrite_projected_knowledge_paths(
        self,
        config_data: dict[str, object],
        projected_knowledge_base_ids: tuple[str, ...] | None,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        raw_knowledge_bases = config_data.get("knowledge_bases")
        if not isinstance(raw_knowledge_bases, dict):
            return

        knowledge_bases = cast("dict[object, object]", raw_knowledge_bases)
        for base_id, raw_knowledge_base in knowledge_bases.items():
            if not isinstance(base_id, str) or not isinstance(raw_knowledge_base, dict):
                continue
            if projected_knowledge_base_ids is not None and base_id not in projected_knowledge_base_ids:
                continue
            knowledge_base = cast("dict[str, object]", raw_knowledge_base)
            raw_path = knowledge_base.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            host_path = config_relative_path(raw_path, self._runtime_paths)
            knowledge_base["path"] = self._projected_path_value(
                host_path,
                PurePosixPath(_PROJECTED_ASSETS_DIRNAME, "knowledge_bases", _safe_projection_name(base_id)),
                asset_paths_by_host=asset_paths_by_host,
                host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
                assets=assets,
            )

    def _rewrite_projected_agent_paths(
        self,
        config_data: dict[str, object],
        projected_agent_names: tuple[str, ...] | None,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        raw_agents = config_data.get("agents")
        if not isinstance(raw_agents, dict):
            return

        agents = cast("dict[object, object]", raw_agents)
        for agent_name, raw_agent in agents.items():
            if not isinstance(agent_name, str) or not isinstance(raw_agent, dict):
                continue
            if projected_agent_names is not None and agent_name not in projected_agent_names:
                continue
            agent = cast("dict[str, object]", raw_agent)
            safe_agent_name = _safe_projection_name(agent_name)
            agent_dir = PurePosixPath(_PROJECTED_ASSETS_DIRNAME, "agents", safe_agent_name)
            self._rewrite_projected_context_files(
                agent,
                agent_name=agent_name,
                agent_dir=agent_dir,
                asset_paths_by_host=asset_paths_by_host,
                host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
                assets=assets,
            )
            self._rewrite_projected_private_template_dir(
                agent,
                agent_dir,
                asset_paths_by_host=asset_paths_by_host,
                host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
                assets=assets,
            )

    def _rewrite_projected_context_files(
        self,
        raw_agent: dict[str, object],
        *,
        agent_name: str,
        agent_dir: PurePosixPath,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        raw_context_files = raw_agent.get("context_files")
        if not isinstance(raw_context_files, list):
            return

        context_files = cast("list[object]", raw_context_files)
        for index, raw_context_file in enumerate(context_files):
            if not isinstance(raw_context_file, str) or not raw_context_file.strip():
                continue
            host_path = resolve_agent_owned_path(
                raw_context_file,
                agent_name=agent_name,
                base_storage_path=self._runtime_paths.storage_root,
            )
            context_files[index] = self._projected_path_value(
                host_path,
                agent_dir
                / "context_files"
                / f"{index:02d}-{_projection_display_name(host_path, fallback=raw_context_file)}",
                asset_paths_by_host=asset_paths_by_host,
                host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
                assets=assets,
            )

    def _rewrite_projected_private_template_dir(
        self,
        raw_agent: dict[str, object],
        agent_dir: PurePosixPath,
        *,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> None:
        raw_private = raw_agent.get("private")
        if not isinstance(raw_private, dict):
            return

        private_config = cast("dict[str, object]", raw_private)
        raw_template_dir = private_config.get("template_dir")
        if not isinstance(raw_template_dir, str) or not raw_template_dir.strip():
            return

        host_path = config_relative_path(raw_template_dir, self._runtime_paths)
        private_config["template_dir"] = self._projected_path_value(
            host_path,
            agent_dir / "private" / "template_dir",
            asset_paths_by_host=asset_paths_by_host,
            host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
            assets=assets,
        )

    def _rewrite_projected_memory_paths(
        self,
        config_data: dict[str, object],
        paths: LocalWorkerStatePaths,
    ) -> None:
        raw_memory = config_data.get("memory")
        if not isinstance(raw_memory, dict):
            return

        memory = cast("dict[str, object]", raw_memory)
        raw_file_memory = memory.get("file")
        if not isinstance(raw_file_memory, dict):
            return

        file_memory = cast("dict[str, object]", raw_file_memory)
        raw_path = file_memory.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return

        file_memory["path"] = self._worker_config_state_path_value(
            paths,
            PurePosixPath("memory", "file"),
        )

    def _rewrite_unscoped_default_worker_scope(
        self,
        config_data: dict[str, object],
        worker_key: str,
    ) -> None:
        if ":unscoped:" not in worker_key:
            return
        raw_defaults = config_data.get("defaults")
        if not isinstance(raw_defaults, dict):
            return
        defaults = cast("dict[str, object]", raw_defaults)
        if defaults.get("worker_scope") is None:
            return
        defaults["worker_scope"] = None

    def _projected_path_value(
        self,
        host_path: Path,
        suggested_relative_path: PurePosixPath,
        *,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> str:
        relative_path = self._projected_asset_path(
            host_path,
            suggested_relative_path,
            asset_paths_by_host=asset_paths_by_host,
            host_paths_by_relative_asset_path=host_paths_by_relative_asset_path,
            assets=assets,
        )
        return _projected_config_value(relative_path)

    def _projected_asset_path(
        self,
        host_path: Path,
        suggested_relative_path: PurePosixPath,
        *,
        asset_paths_by_host: dict[Path, PurePosixPath],
        host_paths_by_relative_asset_path: dict[PurePosixPath, Path],
        assets: list[_DockerProjectedConfigAsset],
    ) -> PurePosixPath:
        resolved_host_path = (
            _validated_asset_host_path(host_path) if host_path.exists() else host_path.expanduser().resolve()
        )
        existing_relative_path = asset_paths_by_host.get(resolved_host_path)
        if existing_relative_path is not None:
            return existing_relative_path

        relative_path = suggested_relative_path
        suffix_counter = 0
        while True:
            existing_host_path = host_paths_by_relative_asset_path.get(relative_path)
            if existing_host_path is None or existing_host_path == resolved_host_path:
                break
            suffix_source = str(resolved_host_path) if suffix_counter == 0 else f"{resolved_host_path}:{suffix_counter}"
            relative_path = _projection_path_with_suffix(
                suggested_relative_path,
                suffix=_projection_hash(suffix_source),
            )
            suffix_counter += 1

        asset_paths_by_host[resolved_host_path] = relative_path
        host_paths_by_relative_asset_path[relative_path] = resolved_host_path
        if resolved_host_path.exists():
            assets.append(
                _DockerProjectedConfigAsset(
                    host_path=resolved_host_path,
                    relative_path=relative_path,
                ),
            )
        return relative_path

    def _worker_config_state_path_value(
        self,
        paths: LocalWorkerStatePaths,
        relative_path: PurePosixPath,
    ) -> str:
        host_path = (paths.root / _WORKER_CONFIG_STATE_DIRNAME).joinpath(*relative_path.parts)
        host_path.mkdir(parents=True, exist_ok=True)
        container_path = PurePosixPath(self.config.storage_mount_path) / _WORKER_CONFIG_STATE_DIRNAME / relative_path
        return str(container_path)
