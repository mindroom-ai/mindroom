"""Tests for the generation-keyed cache around the Matrix state YAML."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.matrix import state as matrix_state
from mindroom.matrix.state import MatrixState, _load_matrix_state_file_cached, matrix_state_for_runtime
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    import os


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


def test_matrix_state_cache_invalidates_after_write(tmp_path: Path) -> None:
    """Saving state should bypass the cache and make the next read fresh."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    first = matrix_state_for_runtime(runtime_paths)
    assert first.get_room("dev") is not None
    assert first.get_room("research") is None

    fresh = MatrixState.load(runtime_paths=runtime_paths)
    fresh.add_room("research", room_id="!research:localhost", alias="#research:localhost", name="research")
    fresh.save(runtime_paths=runtime_paths)

    second = matrix_state_for_runtime(runtime_paths)
    assert second is not first
    assert second.get_room("research") is not None


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

    rooms = matrix_state.load_rooms(runtime_paths)
    assert "dev" in rooms
    rooms.clear()
    rooms_after_mutation = matrix_state.load_rooms(runtime_paths)
    assert rooms_after_mutation.get("dev") is not None
    assert rooms_after_mutation["dev"].room_id == "!dev:localhost"


def test_load_rooms_room_value_is_isolated(tmp_path: Path) -> None:
    """Mutating a ``MatrixRoom`` returned by ``load_rooms`` must not leak into cached state."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    rooms = matrix_state.load_rooms(runtime_paths)
    rooms["dev"].room_id = "!corrupted:localhost"
    rooms_after_mutation = matrix_state.load_rooms(runtime_paths)
    assert rooms_after_mutation["dev"].room_id == "!dev:localhost"


def test_resolve_room_aliases_accepts_room_key_and_full_alias(tmp_path: Path) -> None:
    """Room target resolution should accept both managed room keys and full aliases."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    assert matrix_state.resolve_room_aliases(["dev", "#dev:localhost", "!external:localhost"], runtime_paths) == [
        "!dev:localhost",
        "!dev:localhost",
        "!external:localhost",
    ]


def test_resolve_room_aliases_does_not_reparse_yaml(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Repeated alias resolution must not re-invoke ``yaml.safe_load`` per call."""
    runtime_paths = test_runtime_paths(tmp_path)
    _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()

    safe_load_calls = 0
    real_safe_load = matrix_state.yaml_io.safe_load

    def _counting_safe_load(stream: object) -> object:
        nonlocal safe_load_calls
        safe_load_calls += 1
        return real_safe_load(stream)

    monkeypatch.setattr(matrix_state.yaml_io, "safe_load", _counting_safe_load)

    matrix_state.resolve_room_aliases(["dev", "#external:localhost"], runtime_paths)
    matrix_state.resolve_room_aliases(["dev", "#external:localhost"], runtime_paths)
    matrix_state.resolve_room_aliases(["dev"], runtime_paths)

    assert safe_load_calls == 1


def test_matrix_state_cached_reads_do_not_stat_the_file(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Repeated reads of cached state must not stat the file."""
    runtime_paths = test_runtime_paths(tmp_path)
    state_file = _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()
    monkeypatch.setattr(matrix_state, "monotonic", lambda: 0.0)
    first = matrix_state_for_runtime(runtime_paths)
    stat_calls = 0
    original_stat = Path.stat

    def counting_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal stat_calls
        if self == state_file:
            stat_calls += 1
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(matrix_state.Path, "stat", counting_stat)

    second = matrix_state_for_runtime(runtime_paths)
    third = matrix_state_for_runtime(runtime_paths)

    assert second is first
    assert third is first
    assert stat_calls == 0


def test_matrix_state_cache_observes_first_write_after_missing_read(tmp_path: Path) -> None:
    """A write must invalidate a cached empty state for a previously missing file."""
    runtime_paths = test_runtime_paths(tmp_path)
    _load_matrix_state_file_cached.cache_clear()

    missing = matrix_state_for_runtime(runtime_paths)
    assert missing.rooms == {}

    _seed_state(runtime_paths, "dev", "!dev:localhost")

    written = matrix_state_for_runtime(runtime_paths)
    assert written is not missing
    assert written.get_room("dev") is not None


def test_matrix_state_cache_observes_external_write_after_stat_ttl(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A write from another process must become visible after the stat TTL."""
    runtime_paths = test_runtime_paths(tmp_path)
    state_file = _seed_state(runtime_paths, "dev", "!dev:localhost")
    _load_matrix_state_file_cached.cache_clear()
    now = 0.0
    monkeypatch.setattr(matrix_state, "monotonic", lambda: now)
    monkeypatch.setattr(matrix_state, "_MATRIX_STATE_STAT_TTL_SECONDS", 1.0)

    first = matrix_state_for_runtime(runtime_paths)
    external = first.model_copy(deep=True)
    external.add_room("research", room_id="!research:localhost", alias="#research:localhost", name="research")
    state_file.write_text(
        matrix_state.yaml_io.safe_dump(
            external.model_dump(mode="json"),
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    now = 1.0

    second = matrix_state_for_runtime(runtime_paths)

    assert second is not first
    assert second.get_room("research") is not None
