"""Give one user their personal agent: register its Matrix account, then record it in the roster."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import TYPE_CHECKING

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
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    ManagedAccountProvisioningRequest,
    create_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.matrix_identifiers import namespaced_username_localpart
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


def _localpart_taken(agent_name: str, matrix_username: str | None, runtime_paths: RuntimePaths) -> bool:
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
    for username in (slug, f"{slug}_{sha256(user_id.encode()).hexdigest()[:6]}"):
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
    async with async_exclusive_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        roster = read_personal_agent_roster(runtime_paths)
        for entry in roster:
            if entry.template == template and entry.user_id == user_id:
                return entry.agent_name
        entry = _new_roster_entry(
            config,
            runtime_paths,
            roster,
            template=template,
            user_id=user_id,
            user_display_name=user_display_name,
            source_room_id=source_room_id,
        )
        template_data = config.personal_agents[template].model_dump(exclude_unset=True)
        await create_agent_user(
            runtime_matrix_homeserver(runtime_paths),
            entry.agent_name,
            personal_agent_data(template_data, entry)["display_name"],
            runtime_paths,
            username=entry.matrix_username,
        )
        await asyncio.to_thread(append_personal_agent_roster_entry, runtime_paths, entry)
        return entry.agent_name
