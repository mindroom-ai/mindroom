"""Managed entity recognition in requester identity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix import state as matrix_state
from mindroom.matrix.state import MatrixState
from mindroom.requester_identity import is_managed_entity_id
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_managed_entity_check_reads_matrix_state_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One check reads Matrix state once for all entities, and still knows persisted and generated accounts.

    API requests run this check hundreds of times, so a state read per entity made the Connections page slow.
    """
    config = bind_runtime_paths(
        Config(agents={name: AgentConfig(display_name=name.title()) for name in ("helper", "coder", "writer")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_coder", "renamed_coder", "pw", domain="localhost")
    state.save(runtime_paths=runtime_paths)
    reads = 0
    cache_key = matrix_state._matrix_state_cache_key

    def counting_cache_key(state_file: Path) -> tuple[Path, int, int | None, int | None]:
        nonlocal reads
        reads += 1
        return cache_key(state_file)

    monkeypatch.setattr(matrix_state, "_matrix_state_cache_key", counting_cache_key)

    assert is_managed_entity_id("@renamed_coder:localhost", config, runtime_paths)
    assert is_managed_entity_id("@mindroom_writer:localhost", config, runtime_paths)
    assert not is_managed_entity_id("@alice:localhost", config, runtime_paths)
    assert reads == 3
