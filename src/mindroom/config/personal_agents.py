"""Personal agent templates, the roster of generated agents, and their pure expansion into raw config data."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.config.agent import AgentConfig
from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_USERNAME_PLACEHOLDER = "{username}"
_PLACEHOLDER_PATTERN = re.compile(r"\{(user|username|user_display_name)\}")
# Keep synchronized with config.main._AGENT_NAME_PATTERN; this module cannot import config.main.
_AGENT_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_]+")
_MATRIX_LOCALPART_PATTERN = re.compile(r"[a-z0-9._=/-]+")
_MAX_USERNAME_LENGTH = 40
_MAX_DISPLAY_NAME_LENGTH = 100
_PROBE_USER_ID = "@personal_agent_probe:example.invalid"


def _name_pattern(name: str | None, key: str) -> str:
    return name or f"{key}_{_USERNAME_PLACEHOLDER}"


def _reject_unsupported_fields(*, private: object, allow_self_config: object, where: str) -> None:
    if private is not None:
        msg = f"{where}: private is not supported, a personal agent already belongs to one user"
        raise ValueError(msg)
    # Raw overrides are not coerced yet, and pydantic would accept "true" or 1 as true.
    if allow_self_config not in (None, False):
        msg = f"{where}: allow_self_config is not supported, edit the template's users overrides instead"
        raise ValueError(msg)


class PersonalAgentTemplateConfig(BaseModel):
    """Defaults for the agent that every onboarded user gets of their own."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        description="Agent key pattern with one {username}; defaults to '<template key>_{username}'",
    )
    matrix_username: str | None = Field(
        default=None,
        description=(
            "Matrix localpart pattern with one {username} and at least one other character; "
            "defaults to the usual mindroom_<agent key> account"
        ),
    )
    agent: AgentConfig = Field(description="Agent fields shared by every user's agent")
    users: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-user agent field overrides keyed by concrete Matrix user ID; each replaces the template field",
    )

    def name_pattern(self, key: str) -> str:
        """Return the agent key pattern, defaulting to '<template key>_{username}'."""
        return _name_pattern(self.name, key)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.count(_USERNAME_PLACEHOLDER) != 1:
            msg = f"name must contain {_USERNAME_PLACEHOLDER} exactly once"
            raise ValueError(msg)
        if not _AGENT_NAME_PATTERN.fullmatch(value.replace(_USERNAME_PLACEHOLDER, "x")):
            msg = "name must render to letters, digits, and underscores only"
            raise ValueError(msg)
        return value

    @field_validator("matrix_username")
    @classmethod
    def _validate_matrix_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.count(_USERNAME_PLACEHOLDER) != 1 or value == _USERNAME_PLACEHOLDER:
            msg = f"matrix_username must contain {_USERNAME_PLACEHOLDER} exactly once plus at least one other character"
            raise ValueError(msg)
        if not _MATRIX_LOCALPART_PATTERN.fullmatch(value.replace(_USERNAME_PLACEHOLDER, "x")):
            msg = "matrix_username must render to a Matrix localpart of lowercase letters, digits, and ._=/-"
            raise ValueError(msg)
        return value

    @field_validator("users")
    @classmethod
    def _validate_users(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        validate_concrete_matrix_user_ids(list(value), field_name="users")
        for user_id, overrides in value.items():
            if unknown := sorted(set(overrides) - set(AgentConfig.model_fields)):
                msg = f"users[{user_id!r}] has unknown agent fields: {', '.join(unknown)}"
                raise ValueError(msg)
            _reject_unsupported_fields(
                private=overrides.get("private"),
                allow_self_config=overrides.get("allow_self_config"),
                where=f"users[{user_id!r}]",
            )
        return value

    @model_validator(mode="after")
    def _validate_agent(self) -> Self:
        _reject_unsupported_fields(
            private=self.agent.private,
            allow_self_config=self.agent.allow_self_config,
            where="agent",
        )
        return self


class PersonalAgentRosterEntry(BaseModel):
    """One generated agent: the template, its owner, and the names fixed at onboarding."""

    model_config = ConfigDict(extra="forbid")

    template: str
    user_id: str
    username: str
    agent_name: str
    matrix_username: str | None
    user_display_name: str
    source_room_id: str


@dataclass(frozen=True)
class PersonalAgentInstance:
    """What the effective config remembers about one generated agent."""

    template: str
    user_id: str
    matrix_username: str | None


@dataclass(frozen=True)
class _PersonalAgentExpansion:
    """Raw config data with generated agents added, plus a probe copy covering users without agents yet."""

    data: object
    instances: dict[str, PersonalAgentInstance]
    probe_data: object | None


class _PersonalAgentRosterFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PersonalAgentRosterEntry] = Field(default_factory=list)


def _localpart(user_id: str) -> str:
    return user_id.removeprefix("@").partition(":")[0]


def personal_agent_username(user_id: str) -> str:
    """Return the `{username}` slug of one Matrix user ID."""
    slug = re.sub(r"[^a-z0-9_]", "_", _localpart(user_id).lower())[:_MAX_USERNAME_LENGTH]
    return slug or "user"


def personal_agent_display_name(user_id: str, display_name: str | None) -> str:
    """Return the `{user_display_name}` value: the sanitized display name, or the localpart when unset."""
    cleaned = "".join(char for char in display_name or "" if unicodedata.category(char) != "Cc")
    return (cleaned or _localpart(user_id))[:_MAX_DISPLAY_NAME_LENGTH]


def _render_placeholders(text: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(1)], text)


def _with_owner(users: object, owner: str) -> object:
    """Append the owner to a user list, leaving malformed values for validation to report."""
    if users is None:
        return [owner]
    if isinstance(users, list) and owner not in users:
        return [*users, owner]
    return users


def personal_agent_data(template: Mapping[str, Any], entry: PersonalAgentRosterEntry) -> dict[str, Any]:
    """Build the raw `agents` entry of one generated agent from its raw template."""
    agent: dict[str, Any] = deepcopy(dict(template["agent"]))
    users = template.get("users")
    overrides = users.get(entry.user_id) if isinstance(users, dict) else None
    if isinstance(overrides, dict):
        agent.update(deepcopy(overrides))

    values = {"user": entry.user_id, "username": entry.username, "user_display_name": entry.user_display_name}
    for field in ("display_name", "role"):
        if isinstance(agent.get(field), str):
            agent[field] = _render_placeholders(agent[field], values)
    if isinstance(agent.get("instructions"), list):
        agent["instructions"] = [
            _render_placeholders(item, values) if isinstance(item, str) else item for item in agent["instructions"]
        ]

    access = {} if agent.get("access") is None else agent["access"]
    if isinstance(access, dict):
        if access.get("members_of_rooms") is None:
            access["members_of_rooms"] = []
        access["users"] = _with_owner(access.get("users"), entry.user_id)
        agent["access"] = access
    agent["credential_managers"] = _with_owner(agent.get("credential_managers"), entry.user_id)
    # self_config edits agents.<name>, which a generated agent does not have; defaults.allow_self_config must not apply.
    agent["allow_self_config"] = False
    return agent


def _expandable_templates(templates: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    """Keep templates whose shape expansion can read; the schema reports the rest."""
    return {
        key: template
        for key, template in templates.items()
        if isinstance(key, str) and isinstance(template, dict) and isinstance(template.get("agent"), dict)
    }


def _probe_agents(
    templates: dict[str, dict[str, Any]],
    owners: Mapping[str, set[str]],
) -> dict[str, dict[str, Any]]:
    """Probe each template that has no owner without an override, and each override user without an agent."""
    probes: list[tuple[str, str]] = []
    for key, template in templates.items():
        owned = owners.get(key, set())
        overrides = template.get("users")
        override_users = [user for user in overrides if isinstance(user, str)] if isinstance(overrides, dict) else []
        # Overrides on every owner can mask a broken default that the next plain user would get.
        if owned <= set(override_users):
            probes.append((key, _PROBE_USER_ID))
        probes.extend((key, user_id) for user_id in override_users if user_id not in owned)

    agents: dict[str, dict[str, Any]] = {}
    for index, (key, user_id) in enumerate(probes):
        template = templates[key]
        username = f"personal_agent_probe_{index}"
        name = template.get("name")
        pattern = _name_pattern(name if isinstance(name, str) else None, key)
        entry = PersonalAgentRosterEntry(
            template=key,
            user_id=user_id,
            username=username,
            agent_name=pattern.replace(_USERNAME_PLACEHOLDER, username),
            matrix_username=None,
            user_display_name=personal_agent_display_name(user_id, None),
            source_room_id="",
        )
        agents[entry.agent_name] = personal_agent_data(template, entry)
    return agents


def expand_personal_agents(data: object, roster: Sequence[PersonalAgentRosterEntry]) -> _PersonalAgentExpansion:
    """Add one generated agent per roster entry to raw config data without mutating the input."""
    unexpanded = _PersonalAgentExpansion(data, {}, None)
    if not isinstance(data, dict):
        return unexpanded
    config = cast("dict[str, Any]", data)
    raw_templates = config.get("personal_agents")
    authored_agents = config.get("agents", {})
    if not isinstance(raw_templates, dict) or not isinstance(authored_agents, dict):
        return unexpanded
    templates = _expandable_templates(raw_templates)
    teams = config.get("teams")
    taken = {
        "agents": authored_agents,
        "teams": teams if isinstance(teams, dict) else {},
        "personal_agents": raw_templates,
    }

    agents: dict[str, Any] = dict(authored_agents)
    instances: dict[str, PersonalAgentInstance] = {}
    owners: dict[str, set[str]] = {}
    for entry in roster:
        template = templates.get(entry.template)
        if template is None:
            continue
        for section, keys in taken.items():
            if entry.agent_name in keys:
                msg = (
                    f"{section}.{entry.agent_name} collides with the agent generated from "
                    f"personal_agents.{entry.template} for {entry.user_id}"
                )
                raise ValueError(msg)
        agents[entry.agent_name] = personal_agent_data(template, entry)
        instances[entry.agent_name] = PersonalAgentInstance(entry.template, entry.user_id, entry.matrix_username)
        owners.setdefault(entry.template, set()).add(entry.user_id)

    probes = _probe_agents(templates, owners)
    return _PersonalAgentExpansion(
        {**config, "agents": agents} if instances else config,
        instances,
        {**config, "agents": {**agents, **probes}} if probes else None,
    )


def _personal_agent_roster_path(runtime_paths: RuntimePaths) -> Path:
    """Return the primary-only roster file of generated agents."""
    return tracking_dir(runtime_paths) / "personal_agents.json"


def personal_agent_roster_lock_path(runtime_paths: RuntimePaths) -> Path:
    """Return the lock file that serializes roster updates."""
    return tracking_dir(runtime_paths) / "personal_agents.lock"


def read_personal_agent_roster(runtime_paths: RuntimePaths) -> list[PersonalAgentRosterEntry]:
    """Read every roster entry, or an empty list before the first onboarding."""
    path = _personal_agent_roster_path(runtime_paths)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        return _PersonalAgentRosterFile.model_validate(json.loads(text)).entries
    except ValueError as exc:  # JSON and pydantic errors are both ValueErrors.
        msg = f"Personal agent roster {path} is invalid: {exc}"
        raise ValueError(msg) from exc


def append_personal_agent_roster_entry(runtime_paths: RuntimePaths, entry: PersonalAgentRosterEntry) -> None:
    """Append one entry durably; the caller holds the roster lock."""
    roster = [*read_personal_agent_roster(runtime_paths), entry]
    payload = _PersonalAgentRosterFile(entries=roster).model_dump(mode="json")
    write_json_file_durable(_personal_agent_roster_path(runtime_paths), payload, strict_atomic_replace=True)
