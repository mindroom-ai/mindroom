"""Give one user their personal agent: register its Matrix account, then record it in the roster."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import ValidationError

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.agent import AgentConfig
from mindroom.config.personal_agents import (
    PersonalAgentRosterEntry,
    append_personal_agent_roster_entry,
    personal_agent_data,
    personal_agent_display_name,
    personal_agent_roster_lock_path,
    personal_agent_username,
    read_personal_agent_roster,
)
from mindroom.constants import ROUTER_AGENT_NAME, runtime_matrix_homeserver
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.matrix.identity import MatrixID, managed_account_key, matrix_account_user_id
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    ManagedAccountProvisioningRequest,
    create_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.matrix_identifiers import agent_username_localpart, namespaced_username_localpart
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.personal_agents import PersonalAgentTemplateConfig
    from mindroom.constants import RuntimePaths

_USERNAME_PLACEHOLDER = "{username}"
# Keep synchronized with config.main._RESERVED_ENTITY_NAMES.
_RESERVED_NAMES = frozenset({ROUTER_AGENT_NAME, INTERNAL_USER_AGENT_NAME, "_shared"})


def _matrix_username(
    template_config: PersonalAgentTemplateConfig,
    username: str,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Render a custom Matrix username, or None for the usual mindroom_<agent key> account."""
    if template_config.matrix_username is None:
        return None
    rendered = template_config.matrix_username.replace(_USERNAME_PLACEHOLDER, username)
    return namespaced_username_localpart(rendered, runtime_paths)


def is_mindroom_account_id(user_id: str, runtime_paths: RuntimePaths) -> bool:
    """Whether a Matrix ID is an account MindRoom manages, including agents the live config does not have yet."""
    domain = runtime_matrix_domain(runtime_paths)
    accounts = matrix_state_for_runtime(runtime_paths).accounts.values()
    if any(matrix_account_user_id(account, domain) == user_id for account in accounts):
        return True
    return any(
        MatrixID.from_username(
            entry.matrix_username or agent_username_localpart(entry.agent_name, runtime_paths),
            domain,
        ).full_id
        == user_id
        for entry in read_personal_agent_roster(runtime_paths)
    )


def _localpart_taken(agent_name: str, matrix_username: str | None, runtime_paths: RuntimePaths) -> bool:
    # An account left under this name, such as a removed agent's, would hand its identity and memory to a new owner.
    if matrix_state_for_runtime(runtime_paths).get_account(managed_account_key(agent_name)) is not None:
        return True
    try:
        preflight_managed_account_provisioning(
            [ManagedAccountProvisioningRequest(agent_name, matrix_username)],
            runtime_paths,
        )
    except PermanentStartupError:
        return True
    return False


def _new_roster_entry(
    config: Config,
    runtime_paths: RuntimePaths,
    roster: list[PersonalAgentRosterEntry],
    *,
    template: str,
    user_id: str,
    user_display_name: str | None,
    source_room_id: str,
) -> PersonalAgentRosterEntry:
    """Pick the first free name, the plain slug and then the hashed one, for a user's new agent."""
    template_config = config.personal_agents[template]
    taken_names = {
        *config.agents,
        *config.teams,
        *config.personal_agents,
        *_RESERVED_NAMES,
        *(entry.agent_name for entry in roster),
    }
    slug = personal_agent_username(user_id)
    hashed = f"{slug}_{sha256(user_id.encode()).hexdigest()[:6]}"
    # Someone on another server must not claim a local person's plain name first.
    local = user_id.partition(":")[2] == runtime_matrix_domain(runtime_paths)
    for username in (slug, hashed) if local else (hashed,):
        agent_name = template_config.name_pattern(template).replace(_USERNAME_PLACEHOLDER, username)
        matrix_username = _matrix_username(template_config, username, runtime_paths)
        if agent_name in taken_names or _localpart_taken(agent_name, matrix_username, runtime_paths):
            continue
        return PersonalAgentRosterEntry(
            template=template,
            user_id=user_id,
            username=username,
            agent_name=agent_name,
            matrix_username=matrix_username,
            user_display_name=personal_agent_display_name(user_id, user_display_name),
            source_room_id=source_room_id,
        )
    msg = f"No free personal agent name for {user_id}"
    raise ValueError(msg)


async def provision_personal_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    template: str,
    user_id: str,
    user_display_name: str | None,
    source_room_id: str,
) -> str:
    """Return the name of a user's agent from one template, creating its account and roster entry when missing.

    The Matrix account is created before the roster entry is written, so a failed
    registration leaves no entry for a reload to load. Calling again for the same
    user returns the existing agent's name.
    """
    if "[" in user_id or "]" in user_id:
        # access.users entries are globs, so brackets would keep the owner out of their own agent.
        msg = f"{user_id} cannot own a personal agent: brackets in a Matrix ID are not supported"
        raise ValueError(msg)
    async with async_exclusive_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        roster = read_personal_agent_roster(runtime_paths)
        for entry in roster:
            if entry.template == template and entry.user_id == user_id:
                return entry.agent_name
        # A new agent's account can join an onboarding room before the reload that makes it known.
        if is_mindroom_account_id(user_id, runtime_paths):
            msg = f"{user_id} is a MindRoom account and cannot own a personal agent"
            raise ValueError(msg)
        entry = _new_roster_entry(
            config,
            runtime_paths,
            roster,
            template=template,
            user_id=user_id,
            user_display_name=user_display_name,
            source_room_id=source_room_id,
        )
        agent_data = personal_agent_data(config.personal_agents[template].model_dump(exclude_unset=True), entry)
        try:
            # An owner ID such as one with a space or a wildcard would make every later config load invalid.
            AgentConfig.model_validate(agent_data)
        except ValidationError as exc:
            msg = f"{user_id} cannot own a personal agent: {exc}"
            raise ValueError(msg) from exc
        await create_agent_user(
            runtime_matrix_homeserver(runtime_paths),
            entry.agent_name,
            agent_data["display_name"],
            runtime_paths,
            username=entry.matrix_username,
        )
        await run_blocking_until_complete(append_personal_agent_roster_entry, runtime_paths, entry)
        return entry.agent_name
