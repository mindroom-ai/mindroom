"""Give one user their personal agent: reserve it in the roster, register its Matrix account, then activate it."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import ValidationError

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.agent import AgentConfig
from mindroom.config.personal_agents import (
    PersonalAgentRosterEntry,
    personal_agent_data,
    personal_agent_display_name,
    personal_agent_roster_lock_path,
    personal_agent_roster_path,
    personal_agent_username,
    read_personal_agent_roster,
)
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import RESERVED_ENTITY_NAMES, runtime_matrix_homeserver
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.identity import MatrixID, managed_account_key, matrix_account_user_id
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix.users import (
    ManagedAccountProvisioningRequest,
    create_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.matrix_identifiers import agent_username_localpart, namespaced_username_localpart
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixAccount


def _write_personal_agent_roster(runtime_paths: RuntimePaths, roster: list[PersonalAgentRosterEntry]) -> None:
    payload = {"entries": [entry.model_dump(mode="json") for entry in roster]}
    write_json_file_durable(personal_agent_roster_path(runtime_paths), payload, strict_atomic_replace=True)


def _append_personal_agent_roster_entry(runtime_paths: RuntimePaths, entry: PersonalAgentRosterEntry) -> None:
    """Append one entry durably in place of the user's earlier one from that template; the caller holds the lock."""
    key = (entry.template, entry.user_id)
    roster = [other for other in read_personal_agent_roster(runtime_paths) if (other.template, other.user_id) != key]
    _write_personal_agent_roster(runtime_paths, [*roster, entry])


def _activate_personal_agent_roster_entry(runtime_paths: RuntimePaths, entry: PersonalAgentRosterEntry) -> None:
    """Rewrite the pending entry this provisioning reserved durably as active, under the roster lock."""
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        roster = [
            other.model_copy(update={"pending": False}) if other == entry else other
            for other in read_personal_agent_roster(runtime_paths)
        ]
        _write_personal_agent_roster(runtime_paths, roster)


def _personal_agent_user_id(
    entry: PersonalAgentRosterEntry,
    runtime_paths: RuntimePaths,
    accounts: Mapping[str, MatrixAccount],
) -> str:
    """Return the Matrix ID of one roster entry's agent: its registered account, else the one it will register."""
    domain = runtime_matrix_domain(runtime_paths)
    # A re-paired namespace or a server name unlike the homeserver host changes the computed ID, not the account.
    if (account := accounts.get(managed_account_key(entry.agent_name))) is not None:
        return matrix_account_user_id(account, domain)
    localpart = entry.matrix_username or agent_username_localpart(entry.agent_name, runtime_paths)
    return MatrixID.from_username(localpart, domain).full_id


def read_personal_agent_user_ids(runtime_paths: RuntimePaths) -> list[tuple[PersonalAgentRosterEntry, str]]:
    """Return every roster entry with its agent's Matrix ID, reading the roster and Matrix state once."""
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    return [
        (entry, _personal_agent_user_id(entry, runtime_paths, accounts))
        for entry in read_personal_agent_roster(runtime_paths)
    ]


def is_mindroom_account_id(user_id: str, runtime_paths: RuntimePaths) -> bool:
    """Whether a Matrix ID is an account MindRoom manages, including agents the live config does not have yet."""
    domain = runtime_matrix_domain(runtime_paths)
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    if any(matrix_account_user_id(account, domain) == user_id for account in accounts.values()):
        return True
    return any(
        _personal_agent_user_id(entry, runtime_paths, accounts) == user_id
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
    source_user_id: str,
    source_room_id: str,
) -> PersonalAgentRosterEntry:
    """Pick the first free name, the plain slug and then the hashed one, for a user's new pending agent."""
    template_config = config.personal_agents[template]
    # An agent, team, or template saved but not reloaded yet would collide with this name at the next load.
    saved, _ = load_yaml_config_source(runtime_paths.config_path)
    sections = (saved.get("agents"), saved.get("teams"), saved.get("personal_agents"))
    saved_sections = [section for section in sections if isinstance(section, dict)]
    taken_names = {
        *config.agents,
        *config.teams,
        *config.personal_agents,
        *RESERVED_ENTITY_NAMES,
        *(name for section in saved_sections for name in section),
        *(entry.agent_name for entry in roster),
    }
    slug = personal_agent_username(user_id)
    hashed = f"{slug}_{sha256(user_id.encode()).hexdigest()[:6]}"
    # Someone on another server must not claim a local person's plain name first.
    local = user_id.partition(":")[2] == runtime_matrix_domain(runtime_paths)
    for username in (slug, hashed) if local else (hashed,):
        agent_name = template_config.agent_name(template, username)
        rendered = template_config.rendered_matrix_username(username)
        matrix_username = None if rendered is None else namespaced_username_localpart(rendered, runtime_paths)
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
            source_user_id=None if source_user_id == user_id else source_user_id,
            pending=True,
        )
    msg = f"No free personal agent name for {user_id}"
    raise ValueError(msg)


def _reserve_personal_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    template: str,
    user_id: str,
    user_display_name: str | None,
    source_user_id: str,
    source_room_id: str,
) -> tuple[PersonalAgentRosterEntry, str | None]:
    """Return the user's roster entry and, while it still needs its account, the agent's display name.

    Runs in a worker thread under the roster lock, which config writers also take
    to check their names against the roster, so neither can miss the other's write.
    """
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        roster = read_personal_agent_roster(runtime_paths)
        existing = next((entry for entry in roster if entry.template == template and entry.user_id == user_id), None)
        if existing is not None and not existing.pending:
            return existing, None
        entry = existing
        accounts = matrix_state_for_runtime(runtime_paths).accounts
        if entry is None or managed_account_key(entry.agent_name) not in accounts:
            # Only a registration that saved its account resumes; a failed one picks names from the current template.
            roster = [other for other in roster if other is not existing]
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
                source_user_id=source_user_id,
                source_room_id=source_room_id,
            )
        agent_data = personal_agent_data(config.personal_agents[template].model_dump(exclude_unset=True), entry)
        try:
            # An owner ID such as one with a space or a wildcard would make every later config load invalid.
            AgentConfig.model_validate(agent_data)
        except ValidationError as exc:
            msg = f"{user_id} cannot own a personal agent: {exc}"
            raise ValueError(msg) from exc
        if entry is not existing:
            _append_personal_agent_roster_entry(runtime_paths, entry)
        return entry, agent_data["display_name"]


async def provision_personal_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    template: str,
    user_id: str,
    user_display_name: str | None,
    source_user_id: str,
    source_room_id: str,
) -> tuple[str, bool]:
    """Return the name of a user's agent from one template and whether this call created or activated its entry.

    The roster entry is written as pending before the Matrix account is created
    and becomes active after it, so a reload never loads an agent without an
    account. An interrupted registration that saved its account resumes under
    the same names. The roster lock covers only those two roster writes, never
    the registration. Calling again for a user whose entry is active returns
    that agent's name.
    """
    if "[" in user_id or "]" in user_id:
        # access.users entries are globs, so brackets would keep the owner out of their own agent.
        msg = f"{user_id} cannot own a personal agent: brackets in a Matrix ID are not supported"
        raise ValueError(msg)
    entry, display_name = await run_blocking_until_complete(
        _reserve_personal_agent,
        config,
        runtime_paths,
        template,
        user_id,
        user_display_name,
        source_user_id,
        source_room_id,
    )
    if display_name is None:
        return entry.agent_name, False
    # A resumed entry's own account loads its stored credentials instead of counting as taken.
    await create_agent_user(
        runtime_matrix_homeserver(runtime_paths),
        entry.agent_name,
        display_name,
        runtime_paths,
        username=entry.matrix_username,
    )
    await run_blocking_until_complete(_activate_personal_agent_roster_entry, runtime_paths, entry)
    return entry.agent_name, True
