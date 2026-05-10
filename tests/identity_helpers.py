"""Test helpers for persisted runtime entity Matrix identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import MatrixState
from mindroom.matrix_identifiers import agent_username_localpart
from tests.conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import Mapping


class ConfigLike(Protocol):
    """Minimal config surface needed for persisted identity fixtures."""

    agents: Mapping[str, object]
    teams: Mapping[str, object]

    def get_domain(self, runtime_paths: RuntimePaths) -> str:
        """Return the Matrix domain for the runtime paths."""
        ...


def persist_entity_accounts(
    config: ConfigLike,
    runtime_paths: RuntimePaths,
    *,
    usernames: Mapping[str, str] | None = None,
    password: str = TEST_PASSWORD,
) -> None:
    """Persist actual Matrix accounts for configured runtime entities in tests."""
    usernames = usernames or {}
    state = MatrixState.load(runtime_paths=runtime_paths)
    domain = config.get_domain(runtime_paths)
    for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]:
        account_key = managed_account_key(entity_name)
        if account_key in state.accounts and entity_name not in usernames:
            continue
        username = usernames.get(entity_name, agent_username_localpart(entity_name, runtime_paths=runtime_paths))
        state.add_account(account_key, username, password, domain=domain)
    state.save(runtime_paths=runtime_paths)


def entity_ids(
    config: ConfigLike,
    runtime_paths: RuntimePaths,
    *,
    usernames: Mapping[str, str] | None = None,
) -> dict[str, MatrixID]:
    """Return test entity IDs after ensuring persisted account fixtures exist."""
    persist_entity_accounts(config, runtime_paths, usernames=usernames)
    return entity_identity_registry(config, runtime_paths).current_ids


def entity_names_for_ids(ids: list[MatrixID], config: ConfigLike, runtime_paths: RuntimePaths) -> list[str | None]:
    """Return configured aliases for Matrix IDs through persisted test identity."""
    registry = entity_identity_registry(config, runtime_paths)
    return [registry.current_entity_name_for_user_id(matrix_id.full_id, include_router=False) for matrix_id in ids]


def entity_name_for_id(matrix_id: MatrixID, config: ConfigLike, runtime_paths: RuntimePaths) -> str | None:
    """Return one configured alias for a Matrix ID through persisted test identity."""
    return entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
        matrix_id.full_id,
        include_router=False,
    )


def fixture_entity_matrix_id(entity_name: str, domain: str, runtime_paths: RuntimePaths) -> MatrixID:
    """Build a generated-looking Matrix ID only for tests that persist that fixture."""
    return MatrixID.from_username(agent_username_localpart(entity_name, runtime_paths=runtime_paths), domain)
