"""Canonical human requester identity resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id
from mindroom.matrix_identifiers import agent_username_localpart, extract_server_name_from_homeserver

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_INTERNAL_USER_ENTITY_NAME = "user"


def runtime_matrix_domain(runtime_paths: RuntimePaths) -> str:
    """Return the Matrix domain for one runtime context."""
    return extract_server_name_from_homeserver(
        runtime_matrix_homeserver(runtime_paths),
        runtime_paths,
    )


def mindroom_user_id(config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the configured internal user's persisted Matrix ID."""
    if config.mindroom_user is None:
        return None
    return managed_account_user_id(
        managed_account_key(_INTERNAL_USER_ENTITY_NAME),
        runtime_matrix_domain(runtime_paths),
        runtime_paths,
    )


def is_human_requester_id(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether one Matrix user ID represents a human requester."""
    if user_id in config.bot_accounts:
        return False
    domain = runtime_matrix_domain(runtime_paths)
    for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]:
        persisted_user_id = managed_account_user_id(managed_account_key(entity_name), domain, runtime_paths)
        generated_user_id = MatrixID.from_username(
            agent_username_localpart(entity_name, runtime_paths),
            domain,
        ).full_id
        if user_id in {persisted_user_id, generated_user_id}:
            return False
    if config.mindroom_user is None:
        return True
    configured_internal_user_id = MatrixID.from_username(config.mindroom_user.username, domain).full_id
    return user_id not in {mindroom_user_id(config, runtime_paths), configured_internal_user_id}


def resolve_human_requester_alias(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Resolve a bridge alias only when both identities represent humans."""
    if not is_human_requester_id(user_id, config, runtime_paths):
        return user_id
    canonical_user_id = config.authorization.resolve_alias(user_id)
    if not is_human_requester_id(canonical_user_id, config, runtime_paths):
        return user_id
    return canonical_user_id
