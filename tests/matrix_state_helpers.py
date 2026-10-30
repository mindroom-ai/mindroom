"""Seed ``matrix_state.yaml`` with a whole state for test setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.matrix.state import _write_matrix_state_file

if TYPE_CHECKING:
    from mindroom.matrix.state import MatrixState


def save_matrix_state(state: MatrixState, runtime_paths: constants.RuntimePaths) -> None:
    """Publish ``state`` as the whole persisted file, the way a test sets up a runtime it then starts."""
    _write_matrix_state_file(constants.matrix_state_file(runtime_paths=runtime_paths), state.model_dump(mode="json"))
