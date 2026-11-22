"""Seed ``matrix_state.yaml`` with a whole state for test setup."""

from __future__ import annotations

from mindroom import constants
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState, _write_matrix_state_file
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.requester_identity import runtime_matrix_domain


def save_matrix_state(state: MatrixState, runtime_paths: constants.RuntimePaths) -> None:
    """Publish ``state`` as the whole persisted file, the way a test sets up a runtime it then starts."""
    _write_matrix_state_file(constants.matrix_state_file(runtime_paths=runtime_paths), state.model_dump(mode="json"))


def persist_owner_account(runtime_paths: constants.RuntimePaths) -> None:
    """Persist the owner's account under its generated ID, which is anyone's to register until then."""
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(
        managed_account_key(constants.ROUTER_AGENT_NAME),
        agent_username_localpart(constants.ROUTER_AGENT_NAME, runtime_paths),
        "pw",
        domain=runtime_matrix_domain(runtime_paths),
    )
    save_matrix_state(state, runtime_paths)
