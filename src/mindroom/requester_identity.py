"""Canonical human requester identity resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver, runtime_mindroom_namespace
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID, managed_account_key, managed_account_user_id, matrix_account_user_id
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix_identifiers import agent_username_localpart, extract_server_name_from_homeserver
from mindroom.private_storage.private_instance_identity import private_instances_for_agent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixState

logger = get_logger(__name__)
INTERNAL_USER_ENTITY_NAME = "user"
# How many input combinations one identity memo keeps, enough for the runtime's and the API's configs.
_IDENTITY_MEMO_LIMIT = 8
type IdentityMemo[T] = dict[tuple[object, ...], tuple[MatrixState, T]]
_MANAGED_ENTITY_IDS: IdentityMemo[frozenset[str]] = {}


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
    return user_id in _managed_entity_ids(config, runtime_paths)


def memoized_entity_identities[T](
    memo: IdentityMemo[T],
    config: Config,
    runtime_paths: RuntimePaths,
    derive: Callable[[str, MatrixState], T],
) -> T:
    """Return ``derive(domain, state)`` for the configured entities, deriving it again only when an input changes.

    Every agent authorizes each message once per responder in the room, so deriving the entities' Matrix IDs on
    each check made a burst of messages cost the fourth power of the agent count.
    The inputs are the entity names, the domain, the namespace, whether an internal user is configured, and the
    persisted state, which ``matrix_state_for_runtime`` returns as one shared object until its file changes.
    """
    domain = runtime_matrix_domain(runtime_paths)
    state = matrix_state_for_runtime(runtime_paths)
    key = (
        domain,
        runtime_mindroom_namespace(runtime_paths),
        config.mindroom_user is not None,
        ROUTER_AGENT_NAME,
        *config.agents,
        *config.teams,
    )
    cached = memo.get(key)
    if cached is not None and cached[0] is state:
        return cached[1]
    value = derive(domain, state)
    if len(memo) >= _IDENTITY_MEMO_LIMIT:
        memo.clear()
    memo[key] = (state, value)
    return value


def _managed_entity_ids(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return the persisted and the generated Matrix ID of every configured router, agent, and team."""

    def derive(domain: str, state: MatrixState) -> frozenset[str]:
        user_ids: set[str] = set()
        for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]:
            account = state.get_account(managed_account_key(entity_name))
            if account is not None:
                user_ids.add(matrix_account_user_id(account, domain))
            user_ids.add(MatrixID.from_username(agent_username_localpart(entity_name, runtime_paths), domain).full_id)
        return frozenset(user_ids)

    return memoized_entity_identities(_MANAGED_ENTITY_IDS, config, runtime_paths, derive)


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


def is_platform_administrator(user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether a requester has platform-wide administrative authority."""
    return resolve_human_requester_alias(user_id, config, runtime_paths) in config.administrators


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
