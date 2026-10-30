"""The one write protocol for the authored config file."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any

import yaml

from mindroom import constants
from mindroom.config.legacy_access import validate_access_migration_source
from mindroom.config.load_attempt import raw_source_fingerprint
from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    Config,
    ConfigFileChangedError,
    ConfigRuntimeValidationError,
)
from mindroom.config.personal_agents import (
    expand_personal_agents,
    personal_agent_roster_lock_path,
    read_personal_agent_roster_file,
)
from mindroom.config.yaml_includes import load_yaml_config_source_with_digests, partial_source_uses_includes
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


class ConfigComposedFromIncludesError(ConfigRuntimeValidationError):
    """A structured write would replace a config composed from include files with one flat file."""

    def __init__(self) -> None:
        super().__init__("configuration uses !include; edit the source files instead")


@dataclass(frozen=True)
class ConfigCommit:
    """One validated config file content, ready to replace the config file."""

    source: str
    # Validated against the runtime, with the source metadata of `source` recorded.
    config: Config
    # Rendered from an authored payload, so it would flatten a config that uses !include.
    structured: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any], runtime_paths: RuntimePaths) -> ConfigCommit:
        """Validate an authored payload and render the file text a structured save writes."""
        config = Config.validate_with_runtime(payload, runtime_paths)
        source = yaml.dump(config.authored_model_dump(), default_flow_style=False, sort_keys=True, allow_unicode=True)
        config_path = runtime_paths.config_path
        config.record_source_metadata(
            config_path,
            {config_path.resolve(): raw_source_fingerprint(source)},
            uses_includes=False,
        )
        return cls(source=source, config=config, structured=True)

    @classmethod
    def from_source(cls, source: str, runtime_paths: RuntimePaths) -> ConfigCommit:
        """Validate raw file text the way the next load reads it, include tags and all."""
        config_path = runtime_paths.config_path
        # Parsing as the live path keeps include resolution and the fingerprint identical to the reload's.
        data, source_digests, uses_includes = load_yaml_config_source_with_digests(
            config_path,
            source=source.encode("utf-8"),
        )
        validate_access_migration_source(data, uses_includes=uses_includes)
        config = Config.validate_with_runtime(data, runtime_paths)
        config.record_source_metadata(config_path, source_digests, uses_includes=uses_includes)
        return cls(source=source, config=config, structured=False)


def write_config_commit(
    runtime_paths: RuntimePaths,
    commit: ConfigCommit,
    *,
    loaded: Config | None,
    committed_uses_includes: bool,
) -> None:
    """Replace the config file with `commit`, refusing a write that would lose another one.

    `loaded` is the config a tool derived its change from: the file must still be the one it loaded.
    `committed_uses_includes` is whether the config the API last published used include files.
    Provisioning reserves personal-agent names under the same lock, so a commit validated before a reservation
    cannot take a name it reserved.
    """
    config_path = runtime_paths.config_path
    with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
        if commit.structured:
            _raise_when_composed_from_includes(runtime_paths, committed_uses_includes)
        _raise_when_roster_names_collide(runtime_paths, commit.config.authored_model_dump())
        if loaded is not None and _current_file_fingerprint(runtime_paths) != loaded.source_fingerprint:
            raise ConfigFileChangedError
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(commit.source, encoding="utf-8")
        constants.safe_replace(tmp_path, config_path)


def _current_file_fingerprint(runtime_paths: RuntimePaths) -> str | None:
    """Return the fingerprint a load of the include-free config file would record, None when it is gone."""
    try:
        return raw_source_fingerprint(runtime_paths.config_path.read_bytes())
    except FileNotFoundError:
        return None


def _raise_when_composed_from_includes(runtime_paths: RuntimePaths, committed_uses_includes: bool) -> None:
    """Reject structured writes that would silently flatten include files.

    Committed include usage remains protective when source becomes unreadable.
    Negative or missing metadata can be stale, so inspect the current source.
    Empty included directories need the same protection as populated ones.
    """
    if committed_uses_includes:
        raise ConfigComposedFromIncludesError
    try:
        _, _, uses_includes = load_yaml_config_source_with_digests(runtime_paths.config_path)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        uses_includes = partial_source_uses_includes(exc)
        if uses_includes is None and not isinstance(exc, FileNotFoundError):
            # An incomplete scan cannot establish that replacing the source is
            # safe. Complete tag-free scans still allow monolith recovery.
            msg = "could not determine whether configuration uses !include; edit the source files instead"
            raise ConfigRuntimeValidationError(msg) from exc
    if uses_includes:
        raise ConfigComposedFromIncludesError


def _raise_when_roster_names_collide(runtime_paths: RuntimePaths, authored_payload: dict[str, Any]) -> None:
    """Refuse an authored name that a personal agent or a created agent in the current roster already holds."""
    try:
        roster = read_personal_agent_roster_file(runtime_paths)
        expand_personal_agents(authored_payload, roster.entries)
    except ValueError as exc:
        raise ConfigRuntimeValidationError(str(exc)) from exc
    # Loading would skip the created agent, so the authored entity would take over its Matrix account.
    for created, section in product(roster.created, ("agents", "teams", "personal_agents")):
        if created.agent_name in (authored_payload.get(section) or {}):
            msg = (
                f"{section}.{created.agent_name} collides with the agent {created.creator} created for {created.owner}"
            )
            raise ConfigRuntimeValidationError(msg)
