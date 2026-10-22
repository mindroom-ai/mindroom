"""Canonical human requester identity resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id, matrix_account_user_id
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix_identifiers import agent_username_localpart, extract_server_name_from_homeserver
from mindroom.private_storage.private_instance_identity import private_instances_for_agent

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
INTERNAL_USER_ENTITY_NAME = "user"


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
        managed_account_key(INTERNAL_USER_ENTITY_NAME),
        runtime_matrix_domain(runtime_paths),
        runtime_paths,
    )


def is_managed_entity_id(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether one Matrix user ID is a configured router, agent, or team account."""
    domain = runtime_matrix_domain(runtime_paths)
    # One state read for all entities: an API request can run this check hundreds of times.
    state = matrix_state_for_runtime(runtime_paths)
    for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]:
        account = state.get_account(managed_account_key(entity_name))
        persisted_user_id = matrix_account_user_id(account, domain) if account is not None else None
        generated_user_id = MatrixID.from_username(
            agent_username_localpart(entity_name, runtime_paths),
            domain,
        ).full_id
        if user_id in {persisted_user_id, generated_user_id}:
            return True
    return False


def is_human_requester_id(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether one Matrix user ID represents a human requester."""
    if user_id in config.bot_accounts or is_managed_entity_id(user_id, config, runtime_paths):
        return False
    domain = runtime_matrix_domain(runtime_paths)
    if config.mindroom_user is None:
        return True
    configured_internal_user_id = MatrixID.from_username(config.mindroom_user.username, domain).full_id
    return user_id not in {mindroom_user_id(config, runtime_paths), configured_internal_user_id}


def is_access_checked_requester_id(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether responder access applies to one requester: a human or a configured bot account."""
    return user_id in config.bot_accounts or is_human_requester_id(user_id, config, runtime_paths)


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


def equivalent_requester_ids(
    user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return the resolved requester and its permitted human bridge aliases."""
    canonical_user_id = resolve_human_requester_alias(user_id, config, runtime_paths)
    return {
        canonical_user_id,
        *(
            alias
            for alias in config.authorization.aliases.get(canonical_user_id, ())
            if resolve_human_requester_alias(alias, config, runtime_paths) == canonical_user_id
        ),
    }


def agent_instances(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_names: Iterable[str],
) -> list[tuple[str, str | None]]:
    """Return ``(agent, None)`` for each shared agent and ``(agent, requester)`` for each private instance.

    Only the primary's copy of an owner record names a requester, so an instance worker code created is not listed.
    A requester now aliased to another ID is skipped, because ingress would route that requester to the other ID's
    instance, and so is an instance owned by one of MindRoom's own accounts, which may not own one.
    """
    instances: list[tuple[str, str | None]] = []
    for agent_name in agent_names:
        private = config.get_agent(agent_name).private
        if private is None:
            instances.append((agent_name, None))
            continue
        try:
            records = private_instances_for_agent(runtime_paths.storage_root, agent_name, private.per)
        except OSError as exc:
            # Instances can be created or removed while they are listed; the next pass lists them again.
            logger.warning("Private instances could not be listed", agent=agent_name, error=str(exc))
            continue
        requesters = {
            record.requester_id
            for record in records
            if record.requester_id is not None
            and is_access_checked_requester_id(record.requester_id, config, runtime_paths)
            and resolve_human_requester_alias(record.requester_id, config, runtime_paths) == record.requester_id
        }
        instances.extend((agent_name, requester_id) for requester_id in sorted(requesters))
    return instances
