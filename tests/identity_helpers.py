"""Test helpers for persisted runtime entity Matrix identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry, persist_offline_entity_accounts
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_identifiers import agent_username_localpart

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_DEFAULT_TEST_PASSWORD = "mock_test_password"  # noqa: S105


def persist_entity_accounts(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    usernames: Mapping[str, str] | None = None,
    password: str = _DEFAULT_TEST_PASSWORD,
) -> None:
    """Persist managed Matrix accounts for tests that need prepared runtime identity."""
    persist_offline_entity_accounts(config, runtime_paths, usernames=usernames, password=password)


def actual_entity_usernames(config: Config) -> dict[str, str]:
    """Return non-generated Matrix usernames for runtime identity tests."""
    return {entity_name: f"actual_{entity_name}" for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]}


def persist_actual_entity_accounts(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    password: str = _DEFAULT_TEST_PASSWORD,
) -> None:
    """Persist non-generated managed Matrix accounts for runtime identity tests."""
    persist_entity_accounts(config, runtime_paths, usernames=actual_entity_usernames(config), password=password)


def entity_ids(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    usernames: Mapping[str, str] | None = None,
) -> dict[str, MatrixID]:
    """Return test entity IDs after ensuring persisted account fixtures exist."""
    persist_entity_accounts(config, runtime_paths, usernames=usernames)
    return entity_identity_registry(config, runtime_paths).current_ids


def entity_names_for_ids(ids: list[MatrixID], config: Config, runtime_paths: RuntimePaths) -> list[str | None]:
    """Return configured aliases for Matrix IDs through persisted test identity."""
    registry = entity_identity_registry(config, runtime_paths)
    return [registry.current_entity_name_for_user_id(matrix_id.full_id, include_router=False) for matrix_id in ids]


def entity_name_for_id(matrix_id: MatrixID, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return one configured alias for a Matrix ID through persisted test identity."""
    return entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
        matrix_id.full_id,
        include_router=False,
    )


def fixture_entity_matrix_id(entity_name: str, domain: str, runtime_paths: RuntimePaths) -> MatrixID:
    """Build the default persisted Matrix ID used by test identity fixtures."""
    return MatrixID.from_username(agent_username_localpart(entity_name, runtime_paths), domain)
