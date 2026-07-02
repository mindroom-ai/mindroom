"""Resolved per-entity config view produced by `Config.resolve_entity`."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.history.types import ResolvedHistorySettings


@dataclass(frozen=True, eq=False)
class ResolvedEntityView:
    """Resolved config values for one agent or team, or the defaults-only scope when `name` is None.

    Every field is a resolved value: defaults applied and entity-vs-default fallbacks already collapsed.
    Construction never validates `name`; each field raises the same error the underlying resolution
    raises for unknown entities.
    Views are cheap per-call snapshots over one loaded ``Config``; config hot-reload replaces the
    ``Config`` object, so never store a view beyond the current operation.
    Views compare by identity (``eq=False``): each `resolve_entity` call returns a fresh view.
    """

    _config: Config = field(repr=False)
    name: str | None

    @cached_property
    def history_settings(self) -> ResolvedHistorySettings:
        """Effective history replay settings for this scope."""
        if self.name is None:
            return self._config.get_default_history_settings()
        return self._config.get_entity_history_settings(self.name)

    @cached_property
    def compaction_config(self) -> CompactionConfig:
        """Effective destructive compaction config for this scope."""
        if self.name is None:
            return self._config.get_default_compaction_config()
        return self._config.get_entity_compaction_config(self.name)

    @cached_property
    def has_authored_compaction_config(self) -> bool:
        """Whether destructive compaction was explicitly configured for this scope."""
        if self.name is None:
            return self._config.has_authored_default_compaction_config()
        return self._config.has_authored_entity_compaction_config(self.name)
