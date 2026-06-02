"""Root-anchored glob helpers for config-relative file sets."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path


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
    path_parts = _split_posix_parts(relative_path)
    pattern_parts = _split_posix_parts(pattern)
    if not path_parts or not pattern_parts:
        return False

    cache: dict[tuple[int, int], bool] = {}

    def _match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in cache:
            return cache[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        else:
            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                next_index = pattern_index
                while next_index < len(pattern_parts) and pattern_parts[next_index] == "**":
                    next_index += 1
                if next_index == len(pattern_parts):
                    result = True
                else:
                    result = any(_match(next_path, next_index) for next_path in range(path_index, len(path_parts) + 1))
            elif path_index < len(path_parts) and fnmatchcase(path_parts[path_index], pattern_part):
                result = _match(path_index + 1, pattern_index + 1)
            else:
                result = False
        cache[key] = result
        return result

    return _match(0, 0)
