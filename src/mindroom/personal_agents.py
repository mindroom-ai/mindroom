"""Provision personal agents, and create, update and delete generated agents for their owners."""

from __future__ import annotations

import re
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from mindroom.background_tasks import run_blocking_or_compensate, run_blocking_until_complete
from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.config.agent import AgentConfig
from mindroom.config.main import config_validation_summary
from mindroom.config.personal_agents import (
    CreatedAgentEntry,
    GeneratedAgentEdits,
    PersonalAgentRoster,
    PersonalAgentRosterEntry,
    personal_agent_data,
    personal_agent_display_name,
    personal_agent_roster_lock_path,
    personal_agent_roster_path,
    personal_agent_username,
    read_personal_agent_roster,
    read_personal_agent_roster_file,
)
from mindroom.config.resolution import authored_tool_entries
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import RESERVED_ENTITY_NAMES, runtime_matrix_homeserver
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.managed_avatars import normalized_avatar_png
from mindroom.matrix.avatar import set_user_avatar_from_png
from mindroom.matrix.identity import MatrixID, managed_account_key, matrix_account_user_id
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.matrix.users import (
    ManagedAccountProvisioningRequest,
    create_agent_user,
    preflight_managed_account_provisioning,
)
from mindroom.matrix_identifiers import agent_username_localpart, namespaced_username_localpart
from mindroom.requester_identity import resolve_human_requester_alias, runtime_matrix_domain
from mindroom.startup_errors import PermanentStartupError

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixAccount

type _RosterEntry = PersonalAgentRosterEntry | CreatedAgentEntry

_EDIT_FIELDS = tuple(GeneratedAgentEdits.model_fields)
# Created agents this runtime is registering, changed only under the roster lock.
# The primary runtime is the roster's only writer, so any other pending created entry was left by a crash.
_REGISTERING: set[str] = set()


class _GeneratedAgentError(ValueError):
    """Why a change to a generated agent was refused; the message is shown to the requester as is."""


def _write_personal_agent_roster(runtime_paths: RuntimePaths, roster: PersonalAgentRoster) -> None:
    payload = roster.model_dump(mode="json")
    write_json_file_durable(personal_agent_roster_path(runtime_paths), payload, strict_atomic_replace=True)


def _refuse_unloadable(config: Config, runtime_paths: RuntimePaths, roster: PersonalAgentRoster) -> None:
    """Refuse a roster the next load would reject, because that load would leave out every generated agent."""
    try:
        authored = config.authored_model_dump()
        config.validate_with_runtime(authored, runtime_paths, tolerate_plugin_load_errors=True, roster=roster)
    except ValueError as exc:  # Pydantic and runtime validation errors are both ValueErrors.
        msg = f"The agent would not load with this change, so nothing was stored: {config_validation_summary(exc)}"
        raise _GeneratedAgentError(msg) from exc


def _replace_roster_entry(
    runtime_paths: RuntimePaths,
    entry: _RosterEntry,
    replacement: _RosterEntry | None,
    config: Config | None = None,
) -> None:
    """Write the roster with one entry swapped for its replacement, or removed; the caller holds the lock.

    Given the live config, it first refuses a roster the next load would reject.
    """
    roster = read_personal_agent_roster_file(runtime_paths)
    field = "created" if isinstance(entry, CreatedAgentEntry) else "entries"
    kept = [replacement if other == entry else other for other in getattr(roster, field)]
    roster = roster.model_copy(update={field: [other for other in kept if other is not None]})
    if config is not None:
        _refuse_unloadable(config, runtime_paths, roster)
    _write_personal_agent_roster(runtime_paths, roster)


def _append_personal_agent_roster_entry(runtime_paths: RuntimePaths, entry: PersonalAgentRosterEntry) -> None:
    """Append one entry durably in place of the user's earlier one from that template; the caller holds the lock."""
    key = (entry.template, entry.user_id)
    roster = read_personal_agent_roster_file(runtime_paths)
    entries = [other for other in roster.entries if (other.template, other.user_id) != key]
    _write_personal_agent_roster(runtime_paths, roster.model_copy(update={"entries": [*entries, entry]}))


def _settle_roster_entry(runtime_paths: RuntimePaths, entry: _RosterEntry, replacement: _RosterEntry | None) -> None:
    """Rewrite the pending entry a provisioning reserved durably as its active copy, or remove it, under the lock."""
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        try:
            _replace_roster_entry(runtime_paths, entry, replacement)
        finally:
            # A created entry this write failed to settle stays pending, and the next creation removes it.
            _REGISTERING.discard(entry.agent_name)


def _refuse_bracketed_owner(user_id: str) -> None:
    if "[" in user_id or "]" in user_id:
        # access.users entries are globs, so brackets would keep the owner out of their own agent.
        msg = f"{user_id} cannot own an agent: brackets in a Matrix ID are not supported"
        raise _GeneratedAgentError(msg)


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


def _taken_agent_names(config: Config, runtime_paths: RuntimePaths, roster: PersonalAgentRoster) -> set[str]:
    """Return the names a new generated agent would collide with at the next load, including those saved but not loaded."""
    saved, _ = load_yaml_config_source(runtime_paths.config_path)
    sections = (saved.get("agents"), saved.get("teams"), saved.get("personal_agents"))
    return {
        *config.agents,
        *config.teams,
        *config.personal_agents,
        *RESERVED_ENTITY_NAMES,
        *(name for section in sections if isinstance(section, dict) for name in section),
        *(entry.agent_name for entry in [*roster.entries, *roster.created]),
    }


def _new_roster_entry(
    config: Config,
    runtime_paths: RuntimePaths,
    roster: PersonalAgentRoster,
    *,
    template: str,
    user_id: str,
    user_display_name: str | None,
    source_user_id: str,
    source_room_id: str,
) -> PersonalAgentRosterEntry:
    """Pick the first free name, the plain slug and then the hashed one, for a user's new pending agent."""
    template_config = config.personal_agents[template]
    taken_names = _taken_agent_names(config, runtime_paths, roster)
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
        roster = read_personal_agent_roster_file(runtime_paths)
        key = (template, user_id)
        existing = next((entry for entry in roster.entries if (entry.template, entry.user_id) == key), None)
        if existing is not None and not existing.pending:
            return existing, None
        entry = existing
        accounts = matrix_state_for_runtime(runtime_paths).accounts
        if entry is None or managed_account_key(entry.agent_name) not in accounts:
            # Only a registration that saved its account resumes; a failed one picks names from the current template.
            roster = roster.model_copy(update={"entries": [other for other in roster.entries if other is not existing]})
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
            msg = f"{user_id} cannot own a personal agent: {config_validation_summary(exc)}"
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
    _refuse_bracketed_owner(user_id)
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
    active = entry.model_copy(update={"pending": False})
    await run_blocking_until_complete(_settle_roster_entry, runtime_paths, entry, active)
    return entry.agent_name, True


def _refuse_owners(owners: list[str], runtime_paths: RuntimePaths) -> None:
    """Refuse owners that would make the next load invalid, or that are agents rather than people."""
    validate_concrete_matrix_user_ids(owners, field_name="owners")
    for owner in owners:
        _refuse_bracketed_owner(owner)
        if is_mindroom_account_id(owner, runtime_paths):
            msg = f"{owner} is a MindRoom account and cannot own an agent"
            raise _GeneratedAgentError(msg)


def _refuse_tools(tools: list[str] | None, allowed: tuple[str, ...]) -> None:
    if outside := [name for name in tools or () if name not in allowed]:
        msg = f"Not among the tools this agent may use: {', '.join(outside)}; allowed: {', '.join(allowed)}"
        raise _GeneratedAgentError(msg)


def _owned_entry(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    requester_id: str,
) -> tuple[_RosterEntry, str]:
    """Return a loaded generated agent's roster entry and the resolved requester, refusing anyone but its owners."""
    roster = read_personal_agent_roster_file(runtime_paths)
    entry = next((other for other in [*roster.entries, *roster.created] if other.agent_name == agent_name), None)
    if entry is None or config.generated_agent(agent_name) is None:
        msg = f"{agent_name} is not a generated agent"
        raise _GeneratedAgentError(msg)
    owners = entry.owners
    requester = resolve_human_requester_alias(requester_id, config, runtime_paths)
    if requester not in owners:
        msg = f"Only {' or '.join(owners)} may change {agent_name}"
        raise _GeneratedAgentError(msg)
    return entry, requester


def generated_agent_settings(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    *,
    requester_id: str,
) -> dict[str, Any]:
    """Return the current values an owner may edit, the tools they may choose from and, if created, the owners."""
    entry, _ = _owned_entry(config, runtime_paths, agent_name, requester_id)
    current = config.agents[agent_name].model_dump(include={"display_name", "role", "instructions"})
    current["tools"] = [tool.name for tool in authored_tool_entries(config, agent_name)]
    settings = {"agent_name": agent_name, "edits": current, "allowed_tools": list(config.tool_ceiling(agent_name))}
    if isinstance(entry, CreatedAgentEntry):
        settings |= {"owner": entry.owner, "co_owners": entry.co_owners}
    return settings


def update_generated_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    requester_id: str,
    changes: Mapping[str, object],
) -> None:
    """Store an owner's changes to a generated agent, None restoring the base's value; blocking, under the lock."""
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        entry, requester = _owned_entry(config, runtime_paths, agent_name, requester_id)
        editable = [*_EDIT_FIELDS, "co_owners"] if isinstance(entry, CreatedAgentEntry) else list(_EDIT_FIELDS)
        if unknown := [field for field in changes if field not in editable]:
            msg = f"{', '.join(unknown)} cannot be changed; allowed: {', '.join(editable)}"
            raise _GeneratedAgentError(msg)
        if isinstance(entry, CreatedAgentEntry) and "co_owners" in changes and requester != entry.owner:
            msg = f"Only {entry.owner} may change co_owners of {agent_name}"
            raise _GeneratedAgentError(msg)
        data = entry.model_dump()
        data["edits"] = (data["edits"] or {}) | {field: changes[field] for field in _EDIT_FIELDS if field in changes}
        if "co_owners" in changes:
            data["co_owners"] = changes["co_owners"] or []
        updated = type(entry).model_validate(data)
        if updated.edits is not None and "tools" in changes:
            _refuse_tools(updated.edits.tools, config.tool_ceiling(agent_name))
        if isinstance(updated, CreatedAgentEntry):
            # A bridge alias is stored as its person, so it matches the resolved requester.
            co_owners = [resolve_human_requester_alias(user_id, config, runtime_paths) for user_id in updated.co_owners]
            updated = updated.model_copy(update={"co_owners": co_owners})
            _refuse_owners(updated.owners, runtime_paths)
        _replace_roster_entry(runtime_paths, entry, updated, config)


async def set_generated_agent_avatar(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    requester_id: str,
    client: nio.AsyncClient,
    image: bytes,
) -> None:
    """Set a generated agent's Matrix profile picture for one of its owners from an untrusted image."""
    await run_blocking_until_complete(_owned_entry, config, runtime_paths, agent_name, requester_id)
    png = await run_blocking_until_complete(normalized_avatar_png, image)
    if not await set_user_avatar_from_png(client, png):
        msg = f"The homeserver did not accept the new avatar of {agent_name}; try again later"
        raise _GeneratedAgentError(msg)


def delete_created_agent(config: Config, runtime_paths: RuntimePaths, *, agent_name: str, requester_id: str) -> None:
    """Remove a created agent for its owner; its Matrix account stays, so its name stays taken. Blocking."""
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        entry, requester = _owned_entry(config, runtime_paths, agent_name, requester_id)
        if not isinstance(entry, CreatedAgentEntry):
            msg = f"{agent_name} is a personal agent and cannot be deleted"
            raise _GeneratedAgentError(msg)
        if requester != entry.owner:
            msg = f"Only {entry.owner} may delete {agent_name}"
            raise _GeneratedAgentError(msg)
        _replace_roster_entry(runtime_paths, entry, None)


def _reserve_created_agent(config: Config, runtime_paths: RuntimePaths, entry: CreatedAgentEntry) -> None:
    """Record a pending created agent, refusing an owner at the creator's cap or a taken name; runs in a worker thread."""
    cap = config.agents[entry.creator].created_agents_per_owner
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        _refuse_owners([entry.owner], runtime_paths)
        roster = read_personal_agent_roster_file(runtime_paths)
        # A crash left the pending entries nobody here is registering; the others count, so two creations cannot
        # both pass the cap.
        created = [other for other in roster.created if not other.pending or other.agent_name in _REGISTERING]
        roster = roster.model_copy(update={"created": created})
        count = sum((other.creator, other.owner) == (entry.creator, entry.owner) for other in roster.created)
        if count >= cap:
            msg = f"{entry.owner} already has {count} agents from {entry.creator}, which may create {cap} per owner"
            raise _GeneratedAgentError(msg)
        name = entry.agent_name
        if name in _taken_agent_names(config, runtime_paths, roster) or _localpart_taken(name, None, runtime_paths):
            msg = f"{name} is taken; choose another name"
            raise _GeneratedAgentError(msg)
        active = entry.model_copy(update={"pending": False})
        _refuse_unloadable(config, runtime_paths, roster.model_copy(update={"created": [*roster.created, active]}))
        _write_personal_agent_roster(runtime_paths, roster.model_copy(update={"created": [*roster.created, entry]}))
        _REGISTERING.add(name)


async def provision_created_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    creator: str,
    requester_id: str,
    name: str,
    display_name: str,
    role: str,
    instructions: list[str] | None,
    tools: list[str] | None,
) -> str:
    """Create an agent from `creator` for the requester and return its Matrix ID, registering it as personal agents are."""
    slug = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")[:30]
    if not slug:
        msg = f"{name!r} has no letters or digits to name the agent by; choose another name"
        raise _GeneratedAgentError(msg)
    _refuse_tools(tools, config.tool_ceiling(creator))
    owner = resolve_human_requester_alias(requester_id, config, runtime_paths)
    edits = GeneratedAgentEdits(display_name=display_name, role=role, instructions=instructions, tools=tools)
    entry = CreatedAgentEntry(agent_name=f"{creator}_{slug}", creator=creator, owner=owner, edits=edits, pending=True)
    homeserver = runtime_matrix_homeserver(runtime_paths)
    # A failed or cancelled creation must hold neither the name nor a place under the cap.
    remove = partial(_settle_roster_entry, runtime_paths, entry, None)
    await run_blocking_or_compensate(partial(_reserve_created_agent, config, runtime_paths, entry), lambda _: remove())
    try:
        user = await create_agent_user(homeserver, entry.agent_name, display_name, runtime_paths)
    except BaseException as exc:
        await run_blocking_until_complete(remove)
        if isinstance(exc, Exception):
            msg = f"Registering {entry.agent_name} failed: {exc}"
            raise _GeneratedAgentError(msg) from exc
        raise
    active = entry.model_copy(update={"pending": False})
    await run_blocking_until_complete(_settle_roster_entry, runtime_paths, entry, active)
    return user.user_id
