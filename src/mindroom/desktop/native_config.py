"""Private persisted configuration for the app-owned desktop helper."""

# Validation failures intentionally carry stable inline user-facing wire messages.
# ruff: noqa: EM101

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from mindroom.desktop.input import MINDROOM_APP_IDS
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.device_identity import PinnedMatrixDevice

_V1_TOP_LEVEL_KEYS = frozenset(
    {
        "v",
        "revision",
        "enabled",
        "controller",
        "allowed_requester_ids",
        "allowed_agent_names",
        "allowed_app_ids",
        "capture",
        "browser",
    },
)
_V1_EXTENDED_KEYS = _V1_TOP_LEVEL_KEYS | {"files", "shell"}
_V2_TOP_LEVEL_KEYS = frozenset(
    {
        "v",
        "revision",
        "enabled",
        "controllers",
        "allowed_app_ids",
        "capture",
        "browser",
    },
)
_V2_EXTENDED_KEYS = _V2_TOP_LEVEL_KEYS | {"files", "shell"}
_CONTROLLER_ENTRY_KEYS = frozenset({"controller", "allowed_requester_ids", "allowed_agent_names"})
_MAX_CONTROLLERS = 64
_MAX_ROOTS = 32
_MAX_ROOT_LENGTH = 4_096
# Folder reads confine through POSIX descriptors; shell commands need process groups and the login account.
_LOCAL_ACCESS_SUPPORTED = os.name == "posix"
_POSIX_ONLY_ACCESS = (
    "Read-only folders and shell commands need macOS or Linux; on Windows, MindRoom Desktop supports only "
    "screenshot observation of the `primary-screen` app. Turn them off with "
    "`mindroom desktop access --clear-folders --no-shell`."
)
_MULTI_CONTROLLER_LEGACY_ERROR = (
    "Multiple controllers are configured; use upsert_controller or MindRoom Chat to add another agent "
    "without changing existing saved access."
)


class NativeConfigError(ValueError):
    """Native helper configuration is missing, stale, exposed, or invalid."""

    def __init__(self, code: str, message: str, *, revision: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.revision = revision


@dataclass(frozen=True, slots=True)
class NativeControllerEntry:
    """One pinned controller and its local requester/agent allowlist."""

    controller: PinnedMatrixDevice
    allowed_requester_ids: tuple[str, ...]
    allowed_agent_names: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate non-empty unique requester and agent allowlists."""
        _validate_text_items(self.allowed_requester_ids, "allowed requester")
        _validate_text_items(self.allowed_agent_names, "allowed agent")

    @classmethod
    def from_payload(cls, raw: object) -> NativeControllerEntry:
        """Parse one strict controller entry payload."""
        entry_raw = _mapping(raw, "controller entry")
        if set(entry_raw) != _CONTROLLER_ENTRY_KEYS:
            raise NativeConfigError("invalid_request", "Native desktop controller entry has unsupported fields.")
        return _controller_entry_from_fields(entry_raw)


@dataclass(frozen=True, slots=True)
class NativeCaptureConfig:
    """Bounded capture settings."""

    max_screenshot_width: int = 1568
    jpeg_quality: int = 80


@dataclass(frozen=True, slots=True)
class NativeBrowserConfig:
    """Installed-profile browser settings without its secret extension token."""

    enabled: bool = False
    executable_path: Path | None = None
    user_data_dir: Path | None = None
    timeout_seconds: int = 90


@dataclass(frozen=True, slots=True)
class NativeFilesConfig:
    """Locally selected, canonical read-only folder roots."""

    roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeShellConfig:
    """Account-level local shell capability."""

    enabled: bool = False


@dataclass(frozen=True, slots=True, init=False)
class NativeDesktopConfig:
    """Complete non-secret configuration for one helper."""

    revision: int
    enabled: bool
    allowed_app_ids: tuple[str, ...]
    capture: NativeCaptureConfig
    browser: NativeBrowserConfig
    files: NativeFilesConfig = field(default_factory=NativeFilesConfig)
    shell: NativeShellConfig = field(default_factory=NativeShellConfig)
    controllers: tuple[NativeControllerEntry, ...] = ()

    def __init__(
        self,
        *,
        revision: int,
        enabled: bool,
        allowed_app_ids: tuple[str, ...],
        capture: NativeCaptureConfig,
        browser: NativeBrowserConfig,
        files: NativeFilesConfig | None = None,
        shell: NativeShellConfig | None = None,
        controllers: tuple[NativeControllerEntry, ...] | None = None,
        controller: PinnedMatrixDevice | None = None,
        allowed_requester_ids: tuple[str, ...] | None = None,
        allowed_agent_names: tuple[str, ...] | None = None,
    ) -> None:
        """Accept either multi-controller entries or single-controller fields."""
        resolved_controllers = _resolve_controllers(
            controllers=controllers,
            controller=controller,
            allowed_requester_ids=allowed_requester_ids,
            allowed_agent_names=allowed_agent_names,
            enabled=enabled,
        )
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "allowed_app_ids", allowed_app_ids)
        object.__setattr__(self, "capture", capture)
        object.__setattr__(self, "browser", browser)
        object.__setattr__(self, "files", NativeFilesConfig() if files is None else files)
        object.__setattr__(self, "shell", NativeShellConfig() if shell is None else shell)
        object.__setattr__(self, "controllers", resolved_controllers)

    @property
    def controller(self) -> PinnedMatrixDevice:
        """Primary controller for single-controller callers."""
        if not self.controllers:
            raise NativeConfigError("configuration_missing", "Native desktop configuration has no paired controllers.")
        return self.controllers[0].controller

    @property
    def allowed_requester_ids(self) -> tuple[str, ...]:
        """Allowed requester IDs for the primary controller."""
        return self.controllers[0].allowed_requester_ids if self.controllers else ()

    @property
    def allowed_agent_names(self) -> tuple[str, ...]:
        """Allowed agent names for the primary controller."""
        return self.controllers[0].allowed_agent_names if self.controllers else ()

    @property
    def has_local_capability(self) -> bool:
        """Whether apps, folders, or shell access are allowed; browser control needs an allowed app, so a browser is not enough."""
        return bool(self.allowed_app_ids or self.files.roots or self.shell.enabled)

    @classmethod
    def from_payload(cls, raw: object, *, validate_browser_paths: bool = True) -> NativeDesktopConfig:
        """Parse a strict v1 or v2 configuration without checking saved root availability."""
        payload = _mapping(raw, "configuration")
        version = payload.get("v")
        keys = set(payload)
        if type(version) is int and version == 1 and keys in {_V1_TOP_LEVEL_KEYS, _V1_EXTENDED_KEYS}:
            controllers = (_controller_entry_from_fields(payload),)
        elif type(version) is int and version == 2 and keys in {_V2_TOP_LEVEL_KEYS, _V2_EXTENDED_KEYS}:
            controllers = _controllers_from_payload(payload.get("controllers"))
        else:
            raise NativeConfigError(
                "invalid_request",
                "Native desktop configuration has unsupported fields or version.",
            )
        revision = _integer(payload.get("revision"), "revision", minimum=0)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise NativeConfigError("invalid_request", "Native desktop enabled must be a boolean.")
        capture_raw = _mapping(payload.get("capture"), "capture")
        if set(capture_raw) != {"max_screenshot_width", "jpeg_quality"}:
            raise NativeConfigError("invalid_request", "Native desktop capture has unsupported fields.")
        capture = NativeCaptureConfig(
            max_screenshot_width=_integer(
                capture_raw.get("max_screenshot_width"),
                "max_screenshot_width",
                minimum=320,
                maximum=3840,
            ),
            jpeg_quality=_integer(capture_raw.get("jpeg_quality"), "jpeg_quality", minimum=20, maximum=95),
        )
        browser_raw = _mapping(payload.get("browser"), "browser")
        if set(browser_raw) != {"enabled", "executable_path", "user_data_dir", "timeout_seconds"}:
            raise NativeConfigError("invalid_request", "Native desktop browser has unsupported fields.")
        browser_enabled = browser_raw.get("enabled")
        if not isinstance(browser_enabled, bool):
            raise NativeConfigError("invalid_request", "Native desktop browser enabled must be a boolean.")
        browser = NativeBrowserConfig(
            enabled=browser_enabled,
            executable_path=_optional_absolute_path(browser_raw.get("executable_path"), "browser executable_path"),
            user_data_dir=_optional_absolute_path(browser_raw.get("user_data_dir"), "browser user_data_dir"),
            timeout_seconds=_integer(
                browser_raw.get("timeout_seconds"),
                "browser timeout_seconds",
                minimum=1,
                maximum=120,
            ),
        )
        if validate_browser_paths and browser.executable_path is not None and not browser.executable_path.is_file():
            raise NativeConfigError("invalid_request", "Native desktop browser executable_path must be a file.")
        if validate_browser_paths and browser.user_data_dir is not None and not browser.user_data_dir.is_dir():
            raise NativeConfigError("invalid_request", "Native desktop browser user_data_dir must be a directory.")
        files, shell = _local_access_from_payload(payload)
        return cls(
            revision=revision,
            enabled=enabled,
            controllers=controllers,
            allowed_app_ids=_allowed_app_ids(payload.get("allowed_app_ids")),
            capture=capture,
            browser=browser,
            files=files,
            shell=shell,
        )

    def with_allowed_apps(self, raw: object) -> NativeDesktopConfig:
        """Validate an app-only edit without revalidating unrelated browser paths."""
        return replace(self, allowed_app_ids=_allowed_app_ids(raw))

    def with_local_access(self, files_raw: object, shell_raw: object) -> NativeDesktopConfig:
        """Validate a folder and shell edit without revalidating unrelated settings."""
        payload = self.to_payload()
        payload["files"] = files_raw
        payload["shell"] = shell_raw
        updated = self.from_payload(payload, validate_browser_paths=False)
        return updated.with_canonical_new_roots(self.files.roots)

    def with_canonical_new_roots(self, saved_roots: tuple[Path, ...]) -> NativeDesktopConfig:
        """Canonicalize newly selected roots; saved roots may have disappeared since they were chosen."""
        roots: list[Path] = []
        for root in self.files.roots:
            if root in saved_roots:
                roots.append(root)
                continue
            try:
                canonical: Path | None = root.resolve(strict=True)
            except OSError:
                canonical = None
            if canonical is None or not canonical.is_dir():
                raise NativeConfigError("invalid_request", "Native desktop root must be an existing directory.")
            roots.append(canonical)
        if len(set(roots)) != len(roots):
            raise NativeConfigError("invalid_request", "Native desktop roots must be unique canonical paths.")
        return replace(self, files=NativeFilesConfig(tuple(roots)))

    def with_upserted_controller(self, entry: NativeControllerEntry) -> NativeDesktopConfig:
        """Update an existing matching controller or append a new one; reject key changes for the same device."""
        device_key = (entry.controller.user_id, entry.controller.device_id)
        updated: list[NativeControllerEntry] = []
        matched = False
        for existing in self.controllers:
            if (existing.controller.user_id, existing.controller.device_id) == device_key:
                if existing.controller.ed25519 != entry.controller.ed25519:
                    raise NativeConfigError(
                        "invalid_request",
                        "Native desktop controllers must not have conflicting keys for the same device.",
                    )
                updated.append(entry)
                matched = True
            else:
                updated.append(existing)
        if not matched:
            updated.append(entry)
        return replace(self, controllers=tuple(updated))

    def with_removed_controller(self, controller_key: str) -> NativeDesktopConfig:
        """Remove one controller matching ``controller_journal_key``; disable when removing the last controller."""
        if not isinstance(controller_key, str) or not controller_key.strip():
            raise NativeConfigError("invalid_request", "Native desktop controller key must be non-empty text.")
        remaining = tuple(entry for entry in self.controllers if _controller_key(entry.controller) != controller_key)
        if len(remaining) == len(self.controllers):
            raise NativeConfigError("invalid_request", "Native desktop controller was not found.")
        if not remaining:
            return replace(self, enabled=False, controllers=())
        return replace(self, controllers=remaining)

    def to_payload(self) -> dict[str, object]:
        """Serialize as v1 for a single controller and v2 for zero or multiple controllers."""
        common: dict[str, object] = {
            "revision": self.revision,
            "enabled": self.enabled,
            "allowed_app_ids": list(self.allowed_app_ids),
            "capture": {
                "max_screenshot_width": self.capture.max_screenshot_width,
                "jpeg_quality": self.capture.jpeg_quality,
            },
            "browser": {
                "enabled": self.browser.enabled,
                "executable_path": str(self.browser.executable_path) if self.browser.executable_path else None,
                "user_data_dir": str(self.browser.user_data_dir) if self.browser.user_data_dir else None,
                "timeout_seconds": self.browser.timeout_seconds,
            },
            "files": {"roots": [str(root) for root in self.files.roots]},
            "shell": {"enabled": self.shell.enabled},
        }
        if len(self.controllers) == 1:
            entry = self.controllers[0]
            return {
                "v": 1,
                "controller": _controller_payload(entry.controller),
                "allowed_requester_ids": list(entry.allowed_requester_ids),
                "allowed_agent_names": list(entry.allowed_agent_names),
                **common,
            }
        return {
            "v": 2,
            "controllers": [
                {
                    "controller": _controller_payload(entry.controller),
                    "allowed_requester_ids": list(entry.allowed_requester_ids),
                    "allowed_agent_names": list(entry.allowed_agent_names),
                }
                for entry in self.controllers
            ],
            **common,
        }


def merge_single_controller_update(
    existing: NativeDesktopConfig | None,
    entry: NativeControllerEntry,
    *,
    promote_to_primary: bool = False,
) -> tuple[tuple[NativeControllerEntry, ...], bool]:
    """Apply a legacy single-controller update and return ``(controllers, preserve_local_access)``.

    With ``<= 1`` saved controllers, replace directly and carry over local access only for the same
    ``user_id``. With ``> 1`` saved controllers, replace a matching ``user_id`` (optionally promoted
    to primary) while preserving unrelated controllers and local access, or refuse a brand-new
    ``user_id``.
    """
    if existing is None or len(existing.controllers) <= 1:
        same_user = bool(
            existing
            and existing.controllers
            and existing.controllers[0].controller.user_id == entry.controller.user_id,
        )
        return (entry,), same_user
    if not any(saved.controller.user_id == entry.controller.user_id for saved in existing.controllers):
        raise NativeConfigError("invalid_request", _MULTI_CONTROLLER_LEGACY_ERROR)
    if promote_to_primary:
        others = tuple(saved for saved in existing.controllers if saved.controller.user_id != entry.controller.user_id)
        return (entry, *others), True
    updated: list[NativeControllerEntry] = []
    inserted = False
    for saved in existing.controllers:
        if saved.controller.user_id == entry.controller.user_id:
            if not inserted:
                updated.append(entry)
                inserted = True
        else:
            updated.append(saved)
    return tuple(updated), True


def native_config_path(storage_root: Path) -> Path:
    """Return the native helper configuration path."""
    return storage_root / "desktop_bridge" / "native_config.json"


def require_supported_local_access(config: NativeDesktopConfig) -> None:
    """Refuse read-only folders or shell access on a computer without the POSIX support they need."""
    if not _LOCAL_ACCESS_SUPPORTED and (config.files.roots or config.shell.enabled):
        raise NativeConfigError("invalid_request", _POSIX_ONLY_ACCESS)


def load_native_config(path: Path) -> NativeDesktopConfig:
    """Load private native configuration."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise NativeConfigError("configuration_missing", "Native desktop configuration is missing.") from exc
    except OSError as exc:
        raise NativeConfigError("invalid_request", "Native desktop configuration could not be read.") from exc
    _require_owned_config_file(file_stat)
    try:
        flags = os.O_RDONLY | (os.O_NOFOLLOW | os.O_NONBLOCK if os.name != "nt" else 0)
        with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as source:
            opened_stat = os.fstat(source.fileno())
            _require_owned_config_file(opened_stat)
            if (opened_stat.st_dev, opened_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                raise NativeConfigError("invalid_request", "Native desktop configuration changed while opening it.")
            payload = json.load(source)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeConfigError(
            "configuration_repair_required",
            "Native desktop configuration is malformed; save new settings to repair it.",
        ) from exc
    except OSError as exc:
        raise NativeConfigError("invalid_request", "Native desktop configuration could not be read.") from exc
    # LEGACY_COMPAT: Saved desktop setups that allow MindRoom's own app or desktop helper.
    # Legacy format: `allowed_app_ids` naming `chat.mindroom.menubar` or `chat.mindroom.desktophelper`, which the macOS app offered and app edits and `mindroom desktop` commands saved.
    # Last legacy release: v2026.9.378; replacement: v2026.9.379 refuses both IDs in every edit and run override.
    # Handling: loading drops both IDs, so the rest of the setup stays usable and editable and the next save writes it without them; edits still refuse them.
    # Coverage: tests/test_desktop_native_config.py::test_saved_config_that_allows_mindroom_itself_loads_and_saves_without_it.
    if isinstance(payload, dict) and isinstance(app_ids := payload.get("allowed_app_ids"), list):
        payload["allowed_app_ids"] = [
            app_id for app_id in app_ids if not isinstance(app_id, str) or app_id not in MINDROOM_APP_IDS
        ]
    # Persisted paths may disappear; they must not prevent unrelated settings from being loaded or edited.
    config = NativeDesktopConfig.from_payload(payload, validate_browser_paths=False)
    if os.name != "nt" and stat.S_IMODE(opened_stat.st_mode) & 0o077:
        raise NativeConfigError(
            "configuration_repair_required",
            "Native desktop configuration must not be readable by group or other users; save settings to repair it.",
            revision=config.revision,
        )
    return config


def _require_owned_config_file(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or (os.name != "nt" and file_stat.st_uid != os.getuid()):
        raise NativeConfigError(
            "invalid_request",
            "Native desktop configuration must be a regular file owned by this user.",
        )


def save_native_config(
    path: Path,
    config: NativeDesktopConfig,
    *,
    expected_revision: int,
) -> NativeDesktopConfig:
    """Compare, increment, and durably replace native configuration."""
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise NativeConfigError("invalid_request", "Native desktop expected_revision must be a non-negative integer.")
    create_directory_durable(path.parent, mode=0o700)
    with advisory_file_lock(path.with_suffix(".lock")):
        try:
            current_revision = load_native_config(path).revision
        except NativeConfigError as exc:
            if exc.code not in {"configuration_missing", "configuration_repair_required"}:
                raise
            current_revision = exc.revision
        if current_revision != expected_revision:
            raise NativeConfigError(
                "revision_conflict",
                "Native desktop configuration changed; reload it and try again.",
            )
        if config.revision != expected_revision:
            raise NativeConfigError(
                "revision_conflict",
                "Native desktop configuration revision does not match the edit.",
            )
        saved = replace(config, revision=current_revision + 1)
        write_json_file_durable(
            path,
            saved.to_payload(),
            strict_atomic_replace=True,
            indent=2,
            sort_keys=True,
            trailing_newline=True,
        )
        path.chmod(0o600)
        return saved


def _controller_key(controller: PinnedMatrixDevice) -> str:
    """Return the canonical key matching ``command_journal.controller_journal_key``."""
    return json.dumps([controller.user_id, controller.device_id, controller.ed25519])


def _controller_payload(controller: PinnedMatrixDevice) -> dict[str, str]:
    return {
        "user_id": controller.user_id,
        "device_id": controller.device_id,
        "ed25519": controller.ed25519,
    }


def _controller_from_payload(raw: object) -> PinnedMatrixDevice:
    controller_raw = _mapping(raw, "controller")
    if set(controller_raw) != {"user_id", "device_id", "ed25519"}:
        raise NativeConfigError("invalid_request", "Native desktop controller must contain its exact identity.")
    try:
        return PinnedMatrixDevice(
            user_id=_text(controller_raw.get("user_id"), "controller user_id"),
            device_id=_text(controller_raw.get("device_id"), "controller device_id"),
            ed25519=_text(controller_raw.get("ed25519"), "controller ed25519"),
        )
    except ValueError as exc:
        raise NativeConfigError("invalid_request", str(exc)) from exc


def _controller_entry_from_fields(payload: dict[str, object]) -> NativeControllerEntry:
    return NativeControllerEntry(
        controller=_controller_from_payload(payload.get("controller")),
        allowed_requester_ids=_text_tuple(payload.get("allowed_requester_ids"), "allowed requester"),
        allowed_agent_names=_text_tuple(payload.get("allowed_agent_names"), "allowed agent"),
    )


def _controllers_from_payload(raw: object) -> tuple[NativeControllerEntry, ...]:
    if not isinstance(raw, list) or len(raw) > _MAX_CONTROLLERS:
        raise NativeConfigError(
            "invalid_request",
            f"Native desktop controllers must be a list of at most {_MAX_CONTROLLERS} entries.",
        )
    return tuple(NativeControllerEntry.from_payload(item) for item in raw)


def _resolve_controllers(
    *,
    controllers: tuple[NativeControllerEntry, ...] | None,
    controller: PinnedMatrixDevice | None,
    allowed_requester_ids: tuple[str, ...] | None,
    allowed_agent_names: tuple[str, ...] | None,
    enabled: bool,
) -> tuple[NativeControllerEntry, ...]:
    resolved = () if controllers is None else tuple(controllers)
    if controller is not None or allowed_requester_ids is not None or allowed_agent_names is not None:
        if not resolved:
            if controller is None:
                raise NativeConfigError(
                    "invalid_request",
                    "Native desktop controller must be set when controller allowlists are provided.",
                )
            resolved = (
                NativeControllerEntry(
                    controller=controller,
                    allowed_requester_ids=() if allowed_requester_ids is None else allowed_requester_ids,
                    allowed_agent_names=() if allowed_agent_names is None else allowed_agent_names,
                ),
            )
        else:
            primary = resolved[0]
            resolved = (
                NativeControllerEntry(
                    controller=primary.controller if controller is None else controller,
                    allowed_requester_ids=(
                        primary.allowed_requester_ids if allowed_requester_ids is None else allowed_requester_ids
                    ),
                    allowed_agent_names=(
                        primary.allowed_agent_names if allowed_agent_names is None else allowed_agent_names
                    ),
                ),
                *resolved[1:],
            )
    return _validate_controllers(resolved, enabled=enabled)


def _validate_controllers(
    controllers: tuple[NativeControllerEntry, ...],
    *,
    enabled: bool,
) -> tuple[NativeControllerEntry, ...]:
    if len(controllers) > _MAX_CONTROLLERS:
        raise NativeConfigError(
            "invalid_request",
            f"Native desktop controllers must be a list of at most {_MAX_CONTROLLERS} entries.",
        )
    if enabled and not controllers:
        raise NativeConfigError(
            "invalid_request",
            "Native desktop configuration must have at least one controller when enabled.",
        )
    seen_devices: dict[tuple[str, str], str] = {}
    for entry in controllers:
        device_key = (entry.controller.user_id, entry.controller.device_id)
        existing_ed25519 = seen_devices.get(device_key)
        if existing_ed25519 is not None:
            if existing_ed25519 == entry.controller.ed25519:
                raise NativeConfigError(
                    "invalid_request",
                    "Native desktop controllers must not contain duplicate entries.",
                )
            raise NativeConfigError(
                "invalid_request",
                "Native desktop controllers must not have conflicting keys for the same device.",
            )
        seen_devices[device_key] = entry.controller.ed25519
    return controllers


def _mapping(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be a JSON object.")
    return cast("dict[str, object]", raw)


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be non-empty text.")
    return raw


def _validate_text_items(items: tuple[str, ...], label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if len(items) > 256:
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be a list of at most 256 entries.")
    if not items and not allow_empty:
        raise NativeConfigError("invalid_request", f"Native desktop {label} list must not be empty.")
    values = tuple(_text(value, label) for value in items)
    if len(set(values)) != len(values):
        raise NativeConfigError("invalid_request", f"Native desktop {label} list must not contain duplicates.")
    return values


def _text_tuple(raw: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > 256:
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be a list of at most 256 entries.")
    return _validate_text_items(tuple(raw), label, allow_empty=allow_empty)


def _allowed_app_ids(raw: object) -> tuple[str, ...]:
    app_ids = _text_tuple(raw, "allowed application", allow_empty=True)
    if reserved := sorted(set(app_ids) & MINDROOM_APP_IDS):
        raise NativeConfigError(
            "invalid_request",
            f"MindRoom cannot allow agents to control MindRoom itself ({', '.join(reserved)}); "
            "remove it from the allowed applications.",
        )
    return app_ids


def _integer(raw: object, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum or (maximum is not None and raw > maximum):
        bounds = f"{minimum} through {maximum}" if maximum is not None else f"at least {minimum}"
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be {bounds}.")
    return raw


def _optional_absolute_path(raw: object, label: str) -> Path | None:
    if raw is None:
        return None
    value = Path(_text(raw, label)).expanduser()
    if not value.is_absolute():
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be an absolute path.")
    return value


def _local_access_from_payload(payload: dict[str, object]) -> tuple[NativeFilesConfig, NativeShellConfig]:
    if "files" not in payload:
        return NativeFilesConfig(), NativeShellConfig()
    files_raw = _mapping(payload["files"], "files")
    shell_raw = _mapping(payload["shell"], "shell")
    if set(files_raw) != {"roots"} or set(shell_raw) != {"enabled"}:
        raise NativeConfigError("invalid_request", "Native desktop local access has unsupported fields.")
    roots_raw = files_raw["roots"]
    if not isinstance(roots_raw, list) or len(roots_raw) > _MAX_ROOTS:
        raise NativeConfigError("invalid_request", "Native desktop roots must be a list of at most 32 paths.")
    roots: list[Path] = []
    for raw_root in roots_raw:
        if (
            not isinstance(raw_root, str)
            or not raw_root
            or len(raw_root) > _MAX_ROOT_LENGTH
            or "\x00" in raw_root
            or not Path(raw_root).is_absolute()
            or ".." in Path(raw_root).parts
        ):
            raise NativeConfigError("invalid_request", "Native desktop roots must be canonical absolute paths.")
        roots.append(Path(raw_root))
    if len(set(roots)) != len(roots):
        raise NativeConfigError("invalid_request", "Native desktop roots must be unique canonical paths.")
    if not isinstance(shell_raw["enabled"], bool):
        raise NativeConfigError("invalid_request", "Native desktop shell enabled must be a boolean.")
    return NativeFilesConfig(tuple(roots)), NativeShellConfig(enabled=shell_raw["enabled"])


__all__ = [
    "NativeBrowserConfig",
    "NativeCaptureConfig",
    "NativeConfigError",
    "NativeControllerEntry",
    "NativeDesktopConfig",
    "NativeFilesConfig",
    "NativeShellConfig",
    "load_native_config",
    "merge_single_controller_update",
    "native_config_path",
    "require_supported_local_access",
    "save_native_config",
]
