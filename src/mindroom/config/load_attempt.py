"""The one config-file load attempt that the runtime and the dashboard API share."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    ConfigLoadUserError,
    config_validation_issues,
    validate_loaded_config_source,
)
from mindroom.config.yaml_includes import (
    load_yaml_config_source_with_digests,
    partial_source_files,
    partial_source_uses_includes,
    source_files_fingerprint,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConfigLoad:
    """Everything one read of the config files produced, whether or not it validated.

    ``uses_includes`` is None when the load failed before it could tell.
    ``error`` is the exception that failed the load, kept so the reload owner
    reports a failure exactly as a raising loader would. ``superseded`` marks a
    load the dashboard API discarded because a newer commit won.
    """

    config: Config | None
    source_fingerprint: str | None
    source_files: frozenset[Path] | None
    uses_includes: bool | None
    error: Exception | None = None
    superseded: bool = False

    @property
    def loaded(self) -> bool:
        """Return whether the load validated and became the published config."""
        return not self.superseded and self.config is not None

    def loaded_config(self) -> Config:
        """Return the validated config, or raise the error that failed this load."""
        if self.error is not None:
            raise self.error
        assert self.config is not None
        return self.config


def config_error_detail(exc: ConfigLoadUserError) -> list[dict[str, object]]:
    """Return the API error payload for an invalid config, with the messages every config surface shows."""
    return [
        {"loc": location, "msg": message, "type": "value_error"} for location, message in config_validation_issues(exc)
    ]


def raw_source_fingerprint(source: bytes | str) -> str:
    """Return the stable identity used for raw config stale-write protection."""
    source_bytes = source.encode("utf-8") if isinstance(source, str) else source
    return hashlib.sha256(source_bytes).hexdigest()


def load_config_attempt(runtime_paths: RuntimePaths) -> ConfigLoad:
    """Load and validate the config files once, reporting a failure instead of raising it."""
    source_fingerprint: str | None = None
    source_files: frozenset[Path] | None = None
    uses_includes: bool | None = None
    try:
        source_bytes = runtime_paths.config_path.read_bytes()
        source_fingerprint = raw_source_fingerprint(source_bytes)
        # Parse the bytes already read so a mid-load file edit can never publish
        # config under a fingerprint computed from different content.
        data, source_digests, uses_includes = load_yaml_config_source_with_digests(
            runtime_paths.config_path,
            source=source_bytes,
        )
        source_files = frozenset(source_digests)
        source_fingerprint = source_files_fingerprint(runtime_paths.config_path, source_digests)
        runtime_config, source_digests = validate_loaded_config_source(
            data,
            source_digests,
            source_bytes,
            runtime_paths,
            uses_includes=uses_includes,
            tolerate_plugin_load_errors=True,
        )
        source_files = frozenset(source_digests)
        source_fingerprint = source_files_fingerprint(runtime_paths.config_path, source_digests)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        detail = config_error_detail(exc)
        logger.warning(
            "Failed to load config due to validation",
            config_path=str(runtime_paths.config_path),
            errors=detail,
        )
        if source_files is None:
            # A parse-time failure never reached the full set; the files read
            # so far keep the watcher covering a broken new include file.
            source_files = partial_source_files(exc)
        if uses_includes is None:
            uses_includes = partial_source_uses_includes(exc)
        return ConfigLoad(
            config=None,
            source_fingerprint=source_fingerprint,
            source_files=source_files,
            uses_includes=uses_includes,
            error=exc,
        )
    except Exception as exc:
        logger.exception("Failed to load config", config_path=str(runtime_paths.config_path))
        return ConfigLoad(
            config=None,
            source_fingerprint=source_fingerprint,
            source_files=source_files,
            uses_includes=uses_includes,
            error=exc,
        )
    logger.info(
        "loaded_agent_configuration",
        path=str(runtime_paths.config_path),
        source_file_count=len(source_files),
    )
    logger.info("loaded_agent_configuration_count", agent_count=len(runtime_config.agents))
    return ConfigLoad(
        config=runtime_config,
        source_fingerprint=source_fingerprint,
        source_files=source_files,
        uses_includes=uses_includes,
    )
