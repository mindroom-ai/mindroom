"""Low-level plugin manifest and import helpers for the tool system."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from importlib import util
from importlib.abc import MetaPathFinder
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.logging_config import get_logger
from mindroom.tool_system.plugin_identity import validate_plugin_name

logger = get_logger(__name__)
_MISSING_MODULE_ATTRIBUTE = object()
_PLUGIN_IMPORT_TRANSACTION_LOCK = threading.RLock()
_PLUGIN_IMPORT_TRACKING = threading.local()

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from importlib.machinery import ModuleSpec


class _PluginImportTrackingFinder(MetaPathFinder):
    """Observe imports without claiming responsibility for loading them."""

    def find_spec(
        self,
        fullname: str,
        path: Iterable[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        tracked_imports = getattr(_PLUGIN_IMPORT_TRACKING, "tracked_imports", None)
        if tracked_imports is not None:
            tracked_imports.record(fullname)
        return None


_PLUGIN_IMPORT_TRACKING_FINDER = _PluginImportTrackingFinder()


def _ensure_plugin_import_tracking_finder() -> None:
    """Install the process-wide observing finder once."""
    if _PLUGIN_IMPORT_TRACKING_FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _PLUGIN_IMPORT_TRACKING_FINDER)


@contextmanager
def tracked_module_imports() -> Iterator[TrackedModuleImports]:
    """Track modules imported by one serialized MindRoom plugin transaction."""
    with _PLUGIN_IMPORT_TRANSACTION_LOCK:
        _ensure_plugin_import_tracking_finder()
        sentinel = object()
        previous_imports = getattr(_PLUGIN_IMPORT_TRACKING, "tracked_imports", sentinel)
        tracked_imports = TrackedModuleImports()
        _PLUGIN_IMPORT_TRACKING.tracked_imports = tracked_imports
        try:
            yield tracked_imports
        finally:
            if previous_imports is sentinel:
                delattr(_PLUGIN_IMPORT_TRACKING, "tracked_imports")
            else:
                assert isinstance(previous_imports, TrackedModuleImports)
                previous_imports.merge(tracked_imports)
                _PLUGIN_IMPORT_TRACKING.tracked_imports = previous_imports


def running_asyncio_tasks() -> frozenset[asyncio.Task[object]]:
    """Snapshot pending tasks when plugin code is executing on an event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return frozenset()
    return frozenset(task for task in asyncio.all_tasks(loop) if not task.cancelling())


def cancel_asyncio_tasks_created_since(
    existing_tasks: frozenset[asyncio.Task[object]],
) -> frozenset[asyncio.Task[object]]:
    """Cancel and return pending tasks created after one plugin import boundary."""
    created_tasks = running_asyncio_tasks() - existing_tasks
    for task in created_tasks:
        if not task.done():
            task.cancel()
    return created_tasks


def reject_asyncio_tasks_created_since(
    existing_tasks: frozenset[asyncio.Task[object]],
    *,
    message: str,
) -> None:
    """Cancel and reject background tasks created inside one plugin import boundary."""
    if cancel_asyncio_tasks_created_since(existing_tasks):
        raise PluginValidationError(message)


@dataclass(frozen=True, slots=True)
class ModuleImportState:
    """Loaded modules and importlib-managed parent bindings before plugin execution."""

    modules: dict[str, ModuleType]
    parent_bindings: dict[str, tuple[ModuleType, str, object]]


@dataclass(slots=True)
class TrackedModuleImports:
    """Module names and parent attributes observed before importlib loads them."""

    module_names: set[str] = dataclass_field(default_factory=set)
    parent_bindings: dict[str, tuple[ModuleType, str, object]] = dataclass_field(default_factory=dict)
    owned_package_roots: set[str] = dataclass_field(default_factory=set)

    def record(self, module_name: str) -> None:
        """Record one unresolved import and its pre-import parent attribute."""
        self.module_names.add(module_name)
        if module_name in self.parent_bindings:
            return
        parent_name, separator, child_name = module_name.rpartition(".")
        parent_module = sys.modules.get(parent_name) if separator else None
        if parent_module is not None:
            self.parent_bindings[module_name] = (
                parent_module,
                child_name,
                vars(parent_module).get(child_name, _MISSING_MODULE_ATTRIBUTE),
            )

    def merge(self, other: TrackedModuleImports) -> None:
        """Merge a nested transaction without losing the earliest parent value."""
        self.module_names.update(other.module_names)
        for module_name, binding in other.parent_bindings.items():
            self.parent_bindings.setdefault(module_name, binding)
        self.owned_package_roots.update(other.owned_package_roots)


def snapshot_module_import_state() -> ModuleImportState:
    """Snapshot loaded modules before one transactional plugin import boundary."""
    modules = sys.modules.copy()
    parent_bindings: dict[str, tuple[ModuleType, str, object]] = {}
    for module_name in modules:
        parent_name, separator, child_name = module_name.rpartition(".")
        if not separator:
            continue
        parent_module = modules.get(parent_name)
        if parent_module is None:
            continue
        parent_bindings[module_name] = (
            parent_module,
            child_name,
            vars(parent_module).get(child_name, _MISSING_MODULE_ATTRIBUTE),
        )
    return ModuleImportState(modules=modules, parent_bindings=parent_bindings)


def restore_module_import_state(
    previous_state: ModuleImportState,
    tracked_imports: TrackedModuleImports,
) -> None:
    """Restore only modules attributable to one failed plugin transaction."""
    previous_modules = previous_state.modules
    imported_module_names = frozenset(tracked_imports.module_names)

    def is_plugin_import(module_name: str) -> bool:
        return module_name in imported_module_names or (
            module_name not in previous_modules
            and any(
                module_name == root or module_name.startswith(f"{root}.")
                for root in tracked_imports.owned_package_roots
            )
        )

    current_modules = sys.modules.copy()
    candidate_names = {
        module_name for module_name in set(previous_modules) | set(current_modules) if is_plugin_import(module_name)
    }
    new_module_names = sorted(
        candidate_names - set(previous_modules),
        key=lambda name: name.count("."),
        reverse=True,
    )
    for module_name in new_module_names:
        current_module = current_modules.get(module_name)
        parent_name, separator, child_name = module_name.rpartition(".")
        parent_module = sys.modules.get(parent_name) if separator else None
        previous_binding = tracked_imports.parent_bindings.get(module_name)
        if previous_binding is not None:
            binding_parent, binding_child, previous_value = previous_binding
            if previous_value is _MISSING_MODULE_ATTRIBUTE:
                vars(binding_parent).pop(binding_child, None)
            else:
                vars(binding_parent)[binding_child] = previous_value
        elif parent_module is not None and vars(parent_module).get(child_name) is current_module:
            vars(parent_module).pop(child_name, None)
        sys.modules.pop(module_name, None)
    for module_name in candidate_names & set(previous_modules):
        sys.modules[module_name] = previous_modules[module_name]
        previous_binding = tracked_imports.parent_bindings.get(module_name) or previous_state.parent_bindings.get(
            module_name,
        )
        if previous_binding is None:
            continue
        parent_module, child_name, previous_value = previous_binding
        if previous_value is _MISSING_MODULE_ATTRIBUTE:
            vars(parent_module).pop(child_name, None)
        else:
            vars(parent_module)[child_name] = previous_value


_PLUGIN_MANIFEST = "mindroom.plugin.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WARNED_PLUGIN_MESSAGES: set[tuple[str, Path]] = set()
_PreparedPluginModule = tuple[ModuleType, Any, dict[str, ModuleType | None]] | None


class PluginValidationError(ValueError):
    """Raised when plugin config or plugin runtime validation fails for authored config."""


@dataclass(frozen=True)
class _SkippedPluginToolSource:
    """Tool-source details retained when a parsed plugin manifest cannot load."""

    source: str
    plugin_name: str | None = None
    root: Path | None = None
    manifest_path: Path | None = None
    tools_module_path: Path | None = None


class _PluginResolvedManifestValidationError(PluginValidationError):
    """A plugin resource failed after its manifest name was resolved."""

    def __init__(self, message: str, tool_source: _SkippedPluginToolSource) -> None:
        super().__init__(message)
        self.tool_source = tool_source


@dataclass(frozen=True)
class _PluginManifest:
    """Validated plugin manifest data."""

    name: str
    tools_module: str | None
    hooks_module: str | None
    oauth_module: str | None
    skills: list[str]


@dataclass(frozen=True)
class _PluginBase:
    """Loaded plugin details that depend only on the manifest."""

    name: str
    root: Path
    manifest_path: Path
    tools_module_path: Path | None
    hooks_module_path: Path | None
    oauth_module_path: Path | None
    skill_dirs: list[Path]


@dataclass
class _PluginCacheEntry:
    manifest_mtime: float
    manifest: _PluginManifest


@dataclass
class _ModuleCacheEntry:
    mtime: float
    module_name: str
    module: ModuleType


_PLUGIN_CACHE: dict[Path, _PluginCacheEntry] = {}
_MODULE_IMPORT_CACHE: dict[Path, _ModuleCacheEntry] = {}


def _warn_once(message: str, *, path: Path) -> None:
    """Emit one plugin-path warning once per process for the same message/path pair."""
    warning_key = (message, path)
    if warning_key in _WARNED_PLUGIN_MESSAGES:
        return
    _WARNED_PLUGIN_MESSAGES.add(warning_key)
    logger.warning(message, path=str(path))


def _collect_plugin_bases(
    plugin_entries: list[PluginEntryConfig],
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool,
) -> tuple[list[tuple[_PluginBase, PluginEntryConfig, int]], list[_SkippedPluginToolSource]]:
    """Resolve plugin roots/manifests under the transactional module-import lock."""
    with tracked_module_imports():
        return _collect_plugin_bases_locked(
            plugin_entries,
            runtime_paths,
            skip_broken_plugins=skip_broken_plugins,
        )


def _collect_plugin_bases_locked(
    plugin_entries: list[PluginEntryConfig],
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool,
) -> tuple[list[tuple[_PluginBase, PluginEntryConfig, int]], list[_SkippedPluginToolSource]]:
    """Resolve plugin roots/manifests for one config snapshot.

    Returns resolved plugin bases plus tool-source details for skipped entries.
    """
    plugin_bases: list[tuple[_PluginBase, PluginEntryConfig, int]] = []
    skipped_plugin_sources: list[_SkippedPluginToolSource] = []
    deferred_manifest_error: _PluginResolvedManifestValidationError | None = None
    for plugin_order, plugin_entry in enumerate(plugin_entries):
        if not plugin_entry.enabled:
            continue

        root: Path | None = None
        with tracked_module_imports() as tracked_imports:
            existing_tasks = running_asyncio_tasks()
            module_snapshot = snapshot_module_import_state()
            try:
                root = _resolve_plugin_root(plugin_entry.path, runtime_paths)
                plugin_base = _load_plugin_base(root)
                reject_asyncio_tasks_created_since(
                    existing_tasks,
                    message=(
                        f"Plugin '{plugin_entry.path}' created background tasks while resolving its import; "
                        "start tasks from runtime hooks after activation instead."
                    ),
                )
            except BaseException as exc:
                cancel_asyncio_tasks_created_since(existing_tasks)
                restore_module_import_state(module_snapshot, tracked_imports)
                if not isinstance(exc, (Exception, SystemExit)):
                    raise
                if isinstance(exc, _PluginResolvedManifestValidationError):
                    skipped_plugin_sources.append(
                        _SkippedPluginToolSource(
                            source=plugin_entry.path,
                            plugin_name=exc.tool_source.plugin_name,
                            root=exc.tool_source.root,
                            manifest_path=exc.tool_source.manifest_path,
                            tools_module_path=exc.tool_source.tools_module_path,
                        ),
                    )
                    if not skip_broken_plugins:
                        deferred_manifest_error = deferred_manifest_error or exc
                        continue
                    _log_skipped_plugin_entry(plugin_entry.path, root, exc)
                    continue
                if not skip_broken_plugins:
                    raise
                _log_skipped_plugin_entry(plugin_entry.path, root, exc)
                skipped_plugin_sources.append(_SkippedPluginToolSource(source=plugin_entry.path))
                continue

        plugin_bases.append((plugin_base, plugin_entry, plugin_order))
    _reject_duplicate_plugin_manifest_names(plugin_bases, skipped_plugin_sources)
    if deferred_manifest_error is not None:
        raise deferred_manifest_error
    return plugin_bases, skipped_plugin_sources


def _log_skipped_plugin_entry(
    plugin_path: str,
    root: Path | None,
    exc: BaseException,
) -> None:
    """Log one broken plugin entry without aborting the rest of startup."""
    if root is not None and (not root.exists() or not root.is_dir()):
        _warn_once("Plugin path does not exist, skipping", path=root)
        return

    if isinstance(exc, PluginValidationError) and str(exc).startswith(
        "Configured plugin module could not be resolved:",
    ):
        logger.warning("Plugin module could not be resolved, skipping", spec=plugin_path)
        return

    log_kwargs: dict[str, object] = {"plugin_path": plugin_path, "error": str(exc)}
    if root is not None:
        log_kwargs["path"] = str(root)
    logger.warning("Failed to load plugin, skipping", **log_kwargs)


def _reject_duplicate_plugin_manifest_names(
    plugin_bases: list[tuple[_PluginBase, PluginEntryConfig, int]],
    skipped_plugin_sources: list[_SkippedPluginToolSource] | None = None,
) -> None:
    """Fail plugin loading when configured manifests reuse the same plugin name."""
    manifest_paths_by_name: dict[str, list[Path]] = {}
    for plugin_base, _, _ in plugin_bases:
        manifest_paths_by_name.setdefault(plugin_base.name, []).append(plugin_base.manifest_path)
    for tool_source in skipped_plugin_sources or []:
        if tool_source.plugin_name is not None and tool_source.manifest_path is not None:
            manifest_paths_by_name.setdefault(tool_source.plugin_name, []).append(tool_source.manifest_path)

    duplicates = {name: paths for name, paths in manifest_paths_by_name.items() if len(paths) > 1}
    if not duplicates:
        return

    duplicate_descriptions = ", ".join(
        f"{name}: {', '.join(str(path) for path in paths)}" for name, paths in sorted(duplicates.items())
    )
    logger.error("Duplicate plugin manifest names configured", duplicates=duplicates)
    msg = f"Duplicate plugin manifest names configured: {duplicate_descriptions}"
    raise PluginValidationError(msg)


def _resolve_plugin_root(plugin_path: str, runtime_paths: RuntimePaths) -> Path:
    parsed_python_spec = _parse_python_plugin_spec(plugin_path)
    if parsed_python_spec is not None and parsed_python_spec[2]:
        module_root = _resolve_python_plugin_root(plugin_path)
        if module_root is not None:
            return module_root
        msg = f"Configured plugin module could not be resolved: {plugin_path}"
        raise PluginValidationError(msg)

    relative = resolve_config_relative_path(plugin_path, runtime_paths)
    if relative.exists():
        return relative

    module_root = _resolve_python_plugin_root(plugin_path)
    if module_root is not None:
        return module_root

    return relative


def _resolve_python_plugin_root(plugin_path: str) -> Path | None:
    parsed = _parse_python_plugin_spec(plugin_path)
    if parsed is None:
        return None

    module_name, subpath, _explicit = parsed
    try:
        spec = util.find_spec(module_name)
    except ModuleNotFoundError:
        return None
    except (Exception, SystemExit) as exc:
        msg = f"Failed to resolve plugin module {plugin_path}: {exc}"
        raise PluginValidationError(msg) from exc
    if spec is None:
        return None

    if spec.submodule_search_locations:
        root = Path(next(iter(spec.submodule_search_locations)))
    elif spec.origin:
        root = Path(spec.origin).parent
    else:
        return None

    resolved_root = (root / subpath).resolve() if subpath else root.resolve()
    if not resolved_root.exists() or not resolved_root.is_dir():
        return None

    return resolved_root


def _parse_python_plugin_spec(plugin_path: str) -> tuple[str, str | None, bool] | None:
    prefixes = ("python:", "pkg:", "module:")
    for prefix in prefixes:
        if plugin_path.startswith(prefix):
            spec = plugin_path[len(prefix) :]
            explicit = True
            break
    else:
        spec = plugin_path
        explicit = False
        if "/" in spec or spec.startswith("."):
            return None

    parts = spec.split(":", 1)
    module_name = parts[0].strip()
    if not module_name:
        return None
    subpath = parts[1].strip() if len(parts) > 1 else None
    if subpath == "":
        subpath = None
    return module_name, subpath, explicit


def _load_plugin_base(root: Path) -> _PluginBase:
    if not root.exists() or not root.is_dir():
        msg = f"Configured plugin path does not exist: {root}"
        raise PluginValidationError(msg)

    manifest_path = root / _PLUGIN_MANIFEST
    if not manifest_path.exists():
        msg = f"Plugin manifest missing: {manifest_path}"
        logger.error("Plugin manifest missing", path=str(manifest_path))
        raise PluginValidationError(msg)

    if not root.is_relative_to(_REPO_ROOT):
        _warn_once("Loading non-bundled plugin", path=root)

    try:
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as exc:
        msg = f"Failed to stat plugin manifest {manifest_path}: {exc}"
        logger.exception("Failed to stat plugin manifest", path=str(manifest_path), error=str(exc))
        raise PluginValidationError(msg) from exc

    cached = _PLUGIN_CACHE.get(manifest_path)
    if cached and cached.manifest_mtime == manifest_mtime:
        manifest = cached.manifest
    else:
        manifest = _parse_manifest(manifest_path)
        _PLUGIN_CACHE[manifest_path] = _PluginCacheEntry(manifest_mtime=manifest_mtime, manifest=manifest)

    tool_source = _SkippedPluginToolSource(
        source=str(root),
        plugin_name=manifest.name,
        root=root,
        manifest_path=manifest_path,
        tools_module_path=(root / manifest.tools_module).resolve() if manifest.tools_module else None,
    )
    try:
        tools_module_path = _resolve_module_path(root, manifest.tools_module, kind="tools")
    except PluginValidationError as exc:
        raise _PluginResolvedManifestValidationError(str(exc), tool_source) from exc
    try:
        hooks_module_path = _resolve_module_path(root, manifest.hooks_module, kind="hooks")
        oauth_module_path = _resolve_module_path(root, manifest.oauth_module, kind="oauth")
        skill_dirs = _resolve_skill_dirs(root, manifest.skills)
    except PluginValidationError as exc:
        raise _PluginResolvedManifestValidationError(str(exc), tool_source) from exc

    return _PluginBase(
        name=manifest.name,
        root=root,
        manifest_path=manifest_path,
        tools_module_path=tools_module_path,
        hooks_module_path=hooks_module_path,
        oauth_module_path=oauth_module_path,
        skill_dirs=skill_dirs,
    )


def _parse_manifest(path: Path) -> _PluginManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to parse plugin manifest {path}: {exc}"
        logger.exception("Failed to parse plugin manifest", path=str(path), error=str(exc))
        raise PluginValidationError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Plugin manifest must be a JSON object: {path}"
        logger.error("Plugin manifest must be a JSON object", path=str(path))
        raise PluginValidationError(msg)

    name = data.get("name")
    if not isinstance(name, str):
        msg = f"Plugin manifest missing valid string name ({path})"
        logger.error("Plugin manifest missing valid string name", path=str(path))
        raise PluginValidationError(msg)
    try:
        normalized_name = validate_plugin_name(name)
    except ValueError as exc:
        logger.error("Invalid plugin manifest name", path=str(path), plugin_name=name, error=str(exc))  # noqa: TRY400
        msg = f"{exc} ({path})"
        raise PluginValidationError(msg) from exc

    tools_module = data.get("tools_module")
    if tools_module is not None and not isinstance(tools_module, str):
        msg = f"Plugin tools_module must be a string: {path}"
        logger.error("Plugin tools_module must be a string", path=str(path))
        raise PluginValidationError(msg)

    hooks_module = data.get("hooks_module")
    if hooks_module is not None and not isinstance(hooks_module, str):
        msg = f"Plugin hooks_module must be a string: {path}"
        logger.error("Plugin hooks_module must be a string", path=str(path))
        raise PluginValidationError(msg)

    oauth_module = data.get("oauth_module")
    if oauth_module is not None and not isinstance(oauth_module, str):
        msg = f"Plugin oauth_module must be a string: {path}"
        logger.error("Plugin oauth_module must be a string", path=str(path))
        raise PluginValidationError(msg)

    raw_skills = data.get("skills", [])
    if raw_skills is None:
        raw_skills = []
    if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
        msg = f"Plugin skills must be a list of strings: {path}"
        logger.error("Plugin skills must be a list of strings", path=str(path))
        raise PluginValidationError(msg)

    return _PluginManifest(
        name=normalized_name,
        tools_module=tools_module,
        hooks_module=hooks_module,
        oauth_module=oauth_module,
        skills=raw_skills,
    )


def _resolve_module_path(root: Path, module_path: str | None, *, kind: str) -> Path | None:
    if not module_path:
        return None
    resolved_path = (root / module_path).resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
        msg = f"Plugin {kind} module not found: {resolved_path}"
        logger.error("Plugin module not found", kind=kind, path=str(resolved_path))
        raise PluginValidationError(msg)
    return resolved_path


def resolve_plugin_root(plugin_path: str, runtime_paths: RuntimePaths) -> Path:
    """Resolve one plugin source while rejecting import-time background work."""
    with tracked_module_imports() as tracked_imports:
        existing_tasks = running_asyncio_tasks()
        module_snapshot = snapshot_module_import_state()
        try:
            resolved_path = _resolve_plugin_root(plugin_path, runtime_paths)
            reject_asyncio_tasks_created_since(
                existing_tasks,
                message=(
                    f"Plugin '{plugin_path}' created background tasks while resolving its import; "
                    "start tasks from runtime hooks after activation instead."
                ),
            )
        except BaseException:
            cancel_asyncio_tasks_created_since(existing_tasks)
            restore_module_import_state(module_snapshot, tracked_imports)
            raise
        return resolved_path


def _resolve_skill_dirs(root: Path, skills: list[str]) -> list[Path]:
    skill_dirs: list[Path] = []
    for relative_path in skills:
        path = (root / relative_path).resolve()
        if not path.exists() or not path.is_dir():
            msg = f"Plugin skill path is not a directory: {path}"
            logger.error("Plugin skill path is not a directory", path=str(path))
            raise PluginValidationError(msg)
        skill_dirs.append(path)
    return skill_dirs


def _plugin_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "plugin"


def _plugin_package_name(plugin_name: str, plugin_root: Path) -> str:
    digest = abs(hash(str(plugin_root)))
    return f"mindroom_plugin_{_plugin_slug(plugin_name)}_{digest}"


def _relative_module_name(plugin_root: Path, module_path: Path) -> str:
    relative_path = module_path.relative_to(plugin_root).with_suffix("")
    return ".".join(_plugin_slug(part) for part in relative_path.parts)


def _module_name(plugin_name: str, plugin_root: Path, module_path: Path) -> str:
    return f"{_plugin_package_name(plugin_name, plugin_root)}.{_relative_module_name(plugin_root, module_path)}"


def _package_chain_names(plugin_name: str, plugin_root: Path, module_path: Path) -> list[tuple[str, Path]]:
    package_name = _plugin_package_name(plugin_name, plugin_root)
    chain = [(package_name, plugin_root)]
    package_root = plugin_root
    relative_parent = module_path.relative_to(plugin_root).parent
    parent_package_name = package_name
    for part in relative_parent.parts:
        package_root /= part
        parent_package_name = f"{parent_package_name}.{_plugin_slug(part)}"
        chain.append((parent_package_name, package_root))
    return chain


def _snapshot_plugin_package_chain(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
) -> dict[str, ModuleType | None]:
    return {
        package_name: sys.modules.get(package_name)
        for package_name, _ in _package_chain_names(plugin_name, plugin_root, module_path)
    }


def _install_plugin_package_chain(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
) -> None:
    root_package_name = _plugin_package_name(plugin_name, plugin_root)
    tracked_imports = getattr(_PLUGIN_IMPORT_TRACKING, "tracked_imports", None)
    if tracked_imports is not None:
        tracked_imports.owned_package_roots.add(root_package_name)
    for package_name, package_root in _package_chain_names(plugin_name, plugin_root, module_path):
        if package_name in sys.modules:
            continue
        if tracked_imports is not None:
            tracked_imports.record(package_name)
        package = ModuleType(package_name)
        package.__file__ = str(package_root / "__init__.py")
        package.__package__ = package_name
        package.__path__ = [str(package_root)]
        sys.modules[package_name] = package


def _restore_plugin_package_chain(previous_packages: dict[str, ModuleType | None]) -> None:
    for package_name, previous_module in previous_packages.items():
        if previous_module is None:
            sys.modules.pop(package_name, None)
        else:
            sys.modules[package_name] = previous_module


def _prepare_module(name: str, root: Path, path: Path, module_name: str) -> _PreparedPluginModule:
    previous_packages = _snapshot_plugin_package_chain(name, root, path)
    _install_plugin_package_chain(name, root, path)
    if (spec := util.spec_from_file_location(module_name, path)) is None or spec.loader is None:
        _restore_plugin_package_chain(previous_packages)
        return None

    tracked_imports = getattr(_PLUGIN_IMPORT_TRACKING, "tracked_imports", None)
    if tracked_imports is not None:
        tracked_imports.record(module_name)
    module = sys.modules[module_name] = util.module_from_spec(spec)
    return module, spec.loader, previous_packages
