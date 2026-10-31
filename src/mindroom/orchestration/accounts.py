"""Managed Matrix accounts: the internal user's and each configured entity's, prepared before the runtime uses them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
    ManagedAccountProvisioningRequest,
    create_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.orchestration.runtime import is_permanent_startup_error, run_with_retry
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config
    from mindroom.config.matrix import MindRoomUserConfig

logger = get_logger(__name__)


def validate_entity_accounts(config: Config, runtime_paths: constants.RuntimePaths) -> None:
    """Validate the persisted Matrix identities of every entity ``config`` configures."""
    try:
        entity_identity_registry(config, runtime_paths)
    except (DuplicateManagedEntityIdentityError, MissingManagedEntityAccountError) as exc:
        raise PermanentStartupError(str(exc)) from exc


def _entity_request(config: Config, entity_name: str) -> ManagedAccountProvisioningRequest:
    """Request a generated agent's stored Matrix username, so a wiped matrix_state recreates the same ID."""
    instance = config.personal_agent_instance(entity_name)
    return ManagedAccountProvisioningRequest(
        entity_name,
        username=None if instance is None else instance.matrix_username,
    )


async def _prepare_internal_user(
    mindroom_user: MindRoomUserConfig,
    runtime_paths: constants.RuntimePaths,
    *,
    update_runtime_state: bool,
) -> None:
    """Create or load the internal user's account through the managed-account lifecycle, retrying transient failures."""

    async def prepare() -> None:
        user_account = await create_agent_user(
            constants.runtime_matrix_homeserver(runtime_paths=runtime_paths),
            INTERNAL_USER_AGENT_NAME,
            mindroom_user.display_name,
            username=mindroom_user.username,
            runtime_paths=runtime_paths,
        )
        logger.info("user_account_ready", user_id=user_account.user_id)

    await run_with_retry(
        "Preparing MindRoom user account",
        prepare,
        permanent_error_check=is_permanent_startup_error,
        update_runtime_state=update_runtime_state,
    )


async def prepare_managed_accounts(
    config: Config,
    runtime_paths: constants.RuntimePaths,
    entity_names: Iterable[str],
    *,
    include_internal_user: bool,
    update_runtime_state: bool,
) -> dict[str, AgentMatrixUser]:
    """Create or load the accounts of ``entity_names``, and the internal user's when asked, then validate them all.

    Every requested localpart is checked for collisions before any missing account is created. The entity requests
    are checked again after the internal user's account exists, since the server may have given it another localpart.
    Without entities, the configured identities are validated once; otherwise creation and validation retry together,
    so a transient failure validates again.
    """
    requests = [_entity_request(config, entity_name) for entity_name in entity_names]
    display_names = {request.entity_name: config.entity_display_name(request.entity_name) for request in requests}
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)
    mindroom_user = config.mindroom_user if include_internal_user else None
    if include_internal_user and mindroom_user is None:
        # Not configured in hosted and public profiles.
        logger.debug("mindroom_user not configured, skipping user account creation")
    if mindroom_user is None:
        preflight_managed_account_provisioning(requests, runtime_paths)
    else:
        internal_request = ManagedAccountProvisioningRequest(INTERNAL_USER_AGENT_NAME, username=mindroom_user.username)
        preflight_managed_account_provisioning([internal_request, *requests], runtime_paths)
        await _prepare_internal_user(mindroom_user, runtime_paths, update_runtime_state=update_runtime_state)
        preflight_managed_account_provisioning(requests, runtime_paths)
    if not requests:
        validate_entity_accounts(config, runtime_paths)
        return {}
    users: dict[str, AgentMatrixUser] = {}

    async def prepare() -> None:
        nonlocal users
        prepared: dict[str, AgentMatrixUser] = {}
        for request in requests:
            prepared[request.entity_name] = await create_agent_user(
                homeserver,
                request.entity_name,
                display_names[request.entity_name],
                runtime_paths=runtime_paths,
                username=request.username,
            )
        validate_entity_accounts(config, runtime_paths)
        users = prepared

    await run_with_retry(
        "Preparing managed Matrix accounts",
        prepare,
        permanent_error_check=is_permanent_startup_error,
        update_runtime_state=update_runtime_state,
    )
    return users
