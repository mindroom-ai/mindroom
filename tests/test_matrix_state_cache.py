"""Tests for the file-mtime-keyed cache around the Matrix state YAML."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix import state as matrix_state
from mindroom.matrix.state import (
    MatrixState,
    _load_matrix_state_file_cached,
    matrix_state_for_runtime,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _seed_state(runtime_paths: constants.RuntimePaths, room_key: str, room_id: str) -> Path:
    """Persist one MatrixState and return the on-disk path used by the cache."""
    state = MatrixState()
    state.add_room(room_key, room_id=room_id, alias=f"#{room_key}:localhost", name=room_key)
    state.save(runtime_paths=runtime_paths)
    return constants.matrix_state_file(runtime_paths=runtime_paths)


def test_matrix_state_cache_hits_when_file_unchanged(tmp_path: Path) -> None:
    """Two reads of an unmodified state file should return the same cached object."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    first = matrix_state_for_runtime(runtime_paths)
    second = matrix_state_for_runtime(runtime_paths)

    assert first is second
    info = _load_matrix_state_file_cached.cache_info()
    assert info.hits == 1
    assert info.misses == 1


def test_matrix_state_cache_invalidates_when_file_mtime_changes(tmp_path: Path) -> None:
    """Touching the state file should bypass the cache and produce a fresh state."""
    runtime_paths = test_runtime_paths(tmp_path)
    state_file = _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    first = matrix_state_for_runtime(runtime_paths)
    assert first.get_room("dev") is not None
    assert first.get_room("research") is None

    fresh = MatrixState.load(runtime_paths=runtime_paths)
    fresh.add_room("research", room_id="!research:localhost", alias="#research:localhost", name="research")
    fresh.save(runtime_paths=runtime_paths)

    stat = state_file.stat()
    os.utime(state_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    second = matrix_state_for_runtime(runtime_paths)
    assert second is not first
    assert second.get_room("research") is not None


def test_get_room_aliases_uses_cached_state(tmp_path: Path) -> None:
    """The per-message ``_get_room_aliases`` path should hit the shared cache."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    aliases_first = matrix_rooms._get_room_aliases(runtime_paths)
    aliases_second = matrix_rooms._get_room_aliases(runtime_paths)

    assert aliases_first == {"dev": "!dev:localhost"}
    assert aliases_second == aliases_first
    info = _load_matrix_state_file_cached.cache_info()
    assert info.misses == 1
    assert info.hits >= 1


def test_matrix_state_load_returns_isolated_deep_copy(tmp_path: Path) -> None:
    """``MatrixState.load`` must return a fresh copy that mutators cannot leak through the cache."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    mutated = MatrixState.load(runtime_paths=runtime_paths)
    mutated.add_room("scratch", room_id="!scratch:localhost", alias="#scratch:localhost", name="scratch")

    cached = matrix_state_for_runtime(runtime_paths)
    assert cached.get_room("scratch") is None
    assert mutated is not cached


def test_load_rooms_returns_isolated_dict(tmp_path: Path) -> None:
    """``load_rooms`` must hand back an isolated copy that mutators cannot leak via the cache."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    rooms = matrix_rooms.load_rooms(runtime_paths)
    assert "dev" in rooms
    rooms.clear()
    rooms_after_mutation = matrix_rooms.load_rooms(runtime_paths)
    assert rooms_after_mutation.get("dev") is not None
    assert rooms_after_mutation["dev"].room_id == "!dev:localhost"


def test_load_rooms_room_value_is_isolated(tmp_path: Path) -> None:
    """Mutating a ``MatrixRoom`` returned by ``load_rooms`` must not leak into cached state."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    rooms = matrix_rooms.load_rooms(runtime_paths)
    rooms["dev"].room_id = "!corrupted:localhost"
    rooms_after_mutation = matrix_rooms.load_rooms(runtime_paths)
    assert rooms_after_mutation["dev"].room_id == "!dev:localhost"


def test_resolve_room_aliases_does_not_reparse_yaml(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Repeated alias resolution must not re-invoke ``yaml.safe_load`` per call."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    safe_load_calls = 0
    real_safe_load = matrix_state.yaml.safe_load

    def _counting_safe_load(stream: object) -> object:
        nonlocal safe_load_calls
        safe_load_calls += 1
        return real_safe_load(stream)

    monkeypatch.setattr(matrix_state.yaml, "safe_load", _counting_safe_load)

    matrix_rooms.resolve_room_aliases(["dev", "#external:localhost"], runtime_paths)
    matrix_rooms.resolve_room_aliases(["dev", "#external:localhost"], runtime_paths)
    matrix_rooms.resolve_room_aliases(["dev"], runtime_paths)

    assert safe_load_calls == 1
