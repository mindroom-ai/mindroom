"""Daily-cached MindRoom release awareness for agent prompts."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_distribution_version
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from agno.tools import Toolkit
from packaging.version import InvalidVersion, Version

from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_PYPI_PROJECT_URL = "https://pypi.org/project/mindroom/"
_PYPI_RELEASE_API_URL = "https://pypi.org/pypi/mindroom/json"
_RELEASE_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_RELATIVE_PATH = Path("cache") / "update_awareness.json"
_RELEASE_REQUEST_TIMEOUT_SECONDS = 5.0
_CACHE_LOCK = threading.Lock()


class _ReleaseLookupError(RuntimeError):
    """Raised when the latest MindRoom release cannot be read from PyPI."""


@dataclass(frozen=True, slots=True)
class _ReleaseCacheRecord:
    checked_at: float
    latest_version: str | None
    refresh_succeeded: bool


@dataclass(frozen=True, slots=True)
class _MindRoomReleaseStatus:
    """Installed and published MindRoom version state exposed to the agent."""

    current_version: str
    latest_version: str | None
    update_available: bool | None
    release_check_succeeded: bool


_MEMORY_CACHE: dict[Path, _ReleaseCacheRecord] = {}


def _normalize_version(raw_version: str) -> str:
    version_text = raw_version.strip()
    if not version_text:
        msg = "MindRoom release metadata did not include a version"
        raise _ReleaseLookupError(msg)
    try:
        return str(Version(version_text))
    except InvalidVersion as exc:
        msg = f"MindRoom release metadata included an invalid version: {version_text!r}"
        raise _ReleaseLookupError(msg) from exc


def _current_mindroom_version() -> str:
    try:
        return _normalize_version(installed_distribution_version("mindroom"))
    except (PackageNotFoundError, _ReleaseLookupError) as exc:
        logger.warning("installed_mindroom_version_invalid", error=str(exc))
        return "unknown"


def _fetch_latest_mindroom_version() -> str:
    """Return the latest published MindRoom version from PyPI."""
    try:
        response = httpx.get(
            _PYPI_RELEASE_API_URL,
            headers={"Accept": "application/json", "User-Agent": "MindRoom update awareness"},
            timeout=_RELEASE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "PyPI returned a non-object response"
            raise _ReleaseLookupError(msg)
        info = payload.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("version"), str):
            msg = "PyPI response did not include info.version"
            raise _ReleaseLookupError(msg)
        return _normalize_version(info["version"])
    except (httpx.HTTPError, ValueError) as exc:
        msg = "Could not retrieve the latest MindRoom release from PyPI"
        raise _ReleaseLookupError(msg) from exc


def _cache_path(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / _CACHE_RELATIVE_PATH


def _read_cache(path: Path) -> _ReleaseCacheRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    checked_at = payload.get("checked_at")
    latest_version = payload.get("latest_version")
    refresh_succeeded = payload.get("refresh_succeeded")
    if (
        not isinstance(checked_at, (int, float))
        or isinstance(checked_at, bool)
        or (latest_version is not None and not isinstance(latest_version, str))
        or not isinstance(refresh_succeeded, bool)
    ):
        return None
    try:
        normalized_latest = _normalize_version(latest_version) if latest_version is not None else None
    except _ReleaseLookupError:
        return None
    return _ReleaseCacheRecord(
        checked_at=float(checked_at),
        latest_version=normalized_latest,
        refresh_succeeded=refresh_succeeded,
    )


def _cache_is_fresh(record: _ReleaseCacheRecord, now: float) -> bool:
    age_seconds = now - record.checked_at
    return 0 <= age_seconds < _RELEASE_CACHE_TTL_SECONDS


def _write_cache(path: Path, record: _ReleaseCacheRecord) -> None:
    try:
        write_json_file_durable(path, asdict(record), indent=2, sort_keys=True, trailing_newline=True)
    except OSError as exc:
        logger.warning("mindroom_release_cache_write_failed", path=str(path), error=str(exc))


def _refresh_release_cache(
    path: Path,
    previous: _ReleaseCacheRecord | None,
    *,
    now: float,
    fetch_latest_version: Callable[[], str],
) -> _ReleaseCacheRecord:
    try:
        latest_version = _normalize_version(fetch_latest_version())
        record = _ReleaseCacheRecord(
            checked_at=now,
            latest_version=latest_version,
            refresh_succeeded=True,
        )
    except _ReleaseLookupError as exc:
        logger.warning("mindroom_release_check_failed", error=str(exc))
        record = _ReleaseCacheRecord(
            checked_at=now,
            latest_version=previous.latest_version if previous is not None else None,
            refresh_succeeded=False,
        )
    _MEMORY_CACHE[path] = record
    _write_cache(path, record)
    return record


def _release_cache_record(
    runtime_paths: RuntimePaths,
    *,
    now: float,
    fetch_latest_version: Callable[[], str],
) -> _ReleaseCacheRecord:
    path = _cache_path(runtime_paths)
    with _CACHE_LOCK:
        record = _MEMORY_CACHE.get(path)
        if record is not None and _cache_is_fresh(record, now):
            return record

        disk_record = _read_cache(path)
        if disk_record is not None and (record is None or disk_record.checked_at > record.checked_at):
            record = disk_record
            _MEMORY_CACHE[path] = record
        if record is not None and _cache_is_fresh(record, now):
            return record
        return _refresh_release_cache(
            path,
            record,
            now=now,
            fetch_latest_version=fetch_latest_version,
        )


def _update_available(current_version: str, latest_version: str | None) -> bool | None:
    if current_version == "unknown" or latest_version is None:
        return None
    try:
        return Version(current_version) < Version(latest_version)
    except InvalidVersion:
        return None


def _mindroom_release_status(
    runtime_paths: RuntimePaths,
    *,
    now: float | None = None,
    fetch_latest_version: Callable[[], str] = _fetch_latest_mindroom_version,
) -> _MindRoomReleaseStatus:
    """Resolve installed and daily-cached published MindRoom versions."""
    record = _release_cache_record(
        runtime_paths,
        now=time.time() if now is None else now,
        fetch_latest_version=fetch_latest_version,
    )
    current_version = _current_mindroom_version()
    return _MindRoomReleaseStatus(
        current_version=current_version,
        latest_version=record.latest_version,
        update_available=_update_available(current_version, record.latest_version),
        release_check_succeeded=record.refresh_succeeded,
    )


def _release_awareness_instructions(status: _MindRoomReleaseStatus) -> str:
    latest_version = status.latest_version or "unknown"
    lines = [
        "<mindroom_update_awareness>",
        f"This runtime is running MindRoom {status.current_version}.",
    ]
    if status.release_check_succeeded:
        lines.append(f"The latest published MindRoom release on PyPI is {latest_version}.")
    elif status.latest_version is not None:
        lines.append(f"Today's PyPI check failed; the last known published MindRoom release is {latest_version}.")
    else:
        lines.append("Today's PyPI check failed, so the latest published MindRoom release is unknown.")

    if status.update_available:
        lines.append(
            "A newer MindRoom release is available. Briefly notify the user at a natural opportunity and suggest "
            "updating through the same installation method they originally used. Do not derail unrelated work, "
            "repeat the notice in a conversation that already mentions it, or install the update without an "
            "explicit user request.",
        )
        lines.append(f"Release page: {_PYPI_PROJECT_URL}")
    elif status.release_check_succeeded and status.update_available is False:
        lines.append("This runtime is not older than the latest published release, so do not suggest an update.")
    else:
        lines.append("Do not claim this runtime is up to date while the latest release is unknown or stale.")
    lines.append("</mindroom_update_awareness>")
    return "\n".join(lines)


class UpdateAwarenessTools(Toolkit):
    """Expose MindRoom release status and add it to the system prompt."""

    def __init__(self, *, runtime_paths: RuntimePaths) -> None:
        self.status = _mindroom_release_status(runtime_paths)
        super().__init__(
            name="update_awareness",
            tools=[self.get_mindroom_update_status],
            instructions=_release_awareness_instructions(self.status),
            add_instructions=True,
        )

    def get_mindroom_update_status(self) -> str:
        """Return the installed and daily-cached latest MindRoom versions."""
        return json.dumps(asdict(self.status), sort_keys=True)
