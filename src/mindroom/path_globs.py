"""Root-anchored glob helpers for config-relative file sets."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def _split_posix_parts(value: str) -> tuple[str, ...]:
    """Split one slash-separated path or glob into normalized POSIX parts."""
    normalized = value.replace("\\", "/").strip()
    normalized = normalized.removeprefix("./")
    normalized = normalized.strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part and part != ".")


def validate_safe_relative_pattern(value: str, *, field_name: str) -> str:
    """Validate a root-relative glob pattern that cannot escape its root."""
    parts = _split_posix_parts(value)
    if not parts or any(part == ".." for part in parts) or Path(value).is_absolute():
        msg = f"{field_name} must be a non-empty relative pattern inside the memory root"
        raise ValueError(msg)
    return "/".join(parts)


def matches_root_glob(relative_path: str, pattern: str) -> bool:
    """Return whether a root-relative POSIX path matches a root-anchored glob."""
    normalized_path = "/".join(_split_posix_parts(relative_path))
    normalized_pattern = "/".join(_split_posix_parts(pattern))
    if not normalized_path or not normalized_pattern:
        return False
    return PurePosixPath(normalized_path).full_match(normalized_pattern)
