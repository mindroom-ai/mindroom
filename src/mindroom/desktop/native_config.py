"""Private persisted configuration for the app-owned desktop helper."""

# Validation failures intentionally carry stable inline user-facing wire messages.
# ruff: noqa: EM101

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.device_identity import PinnedMatrixDevice

_TOP_LEVEL_KEYS = frozenset(
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


class NativeConfigError(ValueError):
    """Native helper configuration is missing, stale, exposed, or invalid."""

    def __init__(self, code: str, message: str, *, revision: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.revision = revision


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
class NativeDesktopConfig:
    """Complete non-secret configuration for one helper."""

    revision: int
    enabled: bool
    controller: PinnedMatrixDevice
    allowed_requester_ids: tuple[str, ...]
    allowed_agent_names: tuple[str, ...]
    allowed_app_ids: tuple[str, ...]
    capture: NativeCaptureConfig
    browser: NativeBrowserConfig

    @classmethod
    def from_payload(cls, raw: object, *, validate_browser_paths: bool = True) -> NativeDesktopConfig:
        """Parse a strict version-one configuration."""
        payload = _mapping(raw, "configuration")
        version = payload.get("v")
        if set(payload) != _TOP_LEVEL_KEYS or type(version) is not int or version != 1:
            raise NativeConfigError(
                "invalid_request",
                "Native desktop configuration has unsupported fields or version.",
            )
        revision = _integer(payload.get("revision"), "revision", minimum=0)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise NativeConfigError("invalid_request", "Native desktop enabled must be a boolean.")
        controller_raw = _mapping(payload.get("controller"), "controller")
        if set(controller_raw) != {"user_id", "device_id", "ed25519"}:
            raise NativeConfigError("invalid_request", "Native desktop controller must contain its exact identity.")
        try:
            controller = PinnedMatrixDevice(
                user_id=_text(controller_raw.get("user_id"), "controller user_id"),
                device_id=_text(controller_raw.get("device_id"), "controller device_id"),
                ed25519=_text(controller_raw.get("ed25519"), "controller ed25519"),
            )
        except ValueError as exc:
            raise NativeConfigError("invalid_request", str(exc)) from exc
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
        return cls(
            revision=revision,
            enabled=enabled,
            controller=controller,
            allowed_requester_ids=_text_tuple(payload.get("allowed_requester_ids"), "allowed requester"),
            allowed_agent_names=_text_tuple(payload.get("allowed_agent_names"), "allowed agent"),
            allowed_app_ids=_text_tuple(payload.get("allowed_app_ids"), "allowed application", allow_empty=True),
            capture=capture,
            browser=browser,
        )

    def with_allowed_apps(self, raw: object) -> NativeDesktopConfig:
        """Validate an app-only edit without revalidating unrelated browser paths."""
        return replace(self, allowed_app_ids=_text_tuple(raw, "allowed application", allow_empty=True))

    def to_payload(self) -> dict[str, object]:
        """Serialize the complete non-secret configuration."""
        return {
            "v": 1,
            "revision": self.revision,
            "enabled": self.enabled,
            "controller": {
                "user_id": self.controller.user_id,
                "device_id": self.controller.device_id,
                "ed25519": self.controller.ed25519,
            },
            "allowed_requester_ids": list(self.allowed_requester_ids),
            "allowed_agent_names": list(self.allowed_agent_names),
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
        }


def native_config_path(storage_root: Path) -> Path:
    """Return the native helper configuration path."""
    return storage_root / "desktop_bridge" / "native_config.json"


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


def _mapping(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be a JSON object.")
    return cast("dict[str, object]", raw)


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be non-empty text.")
    return raw


def _text_tuple(raw: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > 256:
        raise NativeConfigError("invalid_request", f"Native desktop {label} must be a list of at most 256 entries.")
    if not raw and not allow_empty:
        raise NativeConfigError("invalid_request", f"Native desktop {label} list must not be empty.")
    values = tuple(_text(value, label) for value in raw)
    if len(set(values)) != len(values):
        raise NativeConfigError("invalid_request", f"Native desktop {label} list must not contain duplicates.")
    return values


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


__all__ = [
    "NativeBrowserConfig",
    "NativeCaptureConfig",
    "NativeConfigError",
    "NativeDesktopConfig",
    "load_native_config",
    "native_config_path",
    "save_native_config",
]
