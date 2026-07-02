"""Resolved per-entity config view produced by `Config.resolve_entity`.

This module is the public read API for per-entity config resolution.
The resolution bodies live as private ``_``-prefixed methods on ``Config``; this view is their
only external consumer, so the entity-keyed surface stays one documented seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.agent import CultureConfig
    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryBackend, MemorySearchConfig
    from mindroom.config.models import CompactionConfig, EffectiveToolConfig
    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.tool_system.worker_routing import WorkerScope


@dataclass(frozen=True, eq=False)
class ResolvedEntityView:
    """Resolved config values for one agent or team, or the defaults-only scope when `name` is None.

    Every field is a resolved value: defaults applied and entity-vs-default fallbacks already collapsed.
    Construction never validates `name`; each field raises the same error the underlying resolution
    raises for unknown entities.
    Views are cheap per-call snapshots over one loaded ``Config``; config hot-reload replaces the
    ``Config`` object, so never store a view beyond the current operation.
    Views compare by identity (``eq=False``): each `resolve_entity` call returns a fresh view.
    List-valued fields are cached on first access; treat them as read-only.
    """

    _config: Config = field(repr=False)
    name: str | None

    @cached_property
    def history_settings(self) -> ResolvedHistorySettings:
        """Effective history replay settings for this scope."""
        if self.name is None:
            return self._config._default_history_settings()
        return self._config._entity_history_settings(self.name)

    @cached_property
    def compaction_config(self) -> CompactionConfig:
        """Effective destructive compaction config for this scope."""
        if self.name is None:
            return self._config._default_compaction_config()
        return self._config._entity_compaction_config(self.name)

    @cached_property
    def has_authored_compaction_config(self) -> bool:
        """Whether destructive compaction was explicitly configured for this scope."""
        if self.name is None:
            return self._config._has_authored_default_compaction_config()
        return self._config._has_authored_entity_compaction_config(self.name)

    @cached_property
    def memory_backend(self) -> MemoryBackend:
        """Effective memory backend; every non-agent scope (team, router, defaults) inherits the global backend."""
        if self.name is None:
            return self._config.memory.backend
        return self._config._agent_memory_backend(self.name)

    @cached_property
    def memory_search(self) -> MemorySearchConfig:
        """Effective file-memory search settings; every non-agent scope inherits the global settings."""
        if self.name is None:
            return self._config.memory.search
        return self._config._agent_memory_search(self.name)

    @cached_property
    def model_name(self) -> str:
        """Authored model name for this agent, team, or router."""
        if self.name is None:
            msg = "The defaults-only scope has no authored model"
            raise ValueError(msg)
        return self._config._entity_model_name(self.name)

    def _agent_name(self) -> str:
        if self.name is None:
            msg = "The defaults-only scope has no per-agent config"
            raise ValueError(msg)
        return self.name

    @cached_property
    def available_tools(self) -> list[str]:
        """All tools this agent may use after dynamic loading."""
        return self._config._agent_available_tools(self._agent_name())

    @cached_property
    def tool_configs(self) -> list[EffectiveToolConfig]:
        """Effective runtime tool config entries for each authored owner."""
        return self._config._agent_tool_configs(self._agent_name())

    @cached_property
    def authored_deferred_tool_configs(self) -> list[EffectiveToolConfig]:
        """One entry per authored deferred tool in effective order."""
        return self._config._agent_authored_deferred_tool_configs(self._agent_name())

    def authored_deferred_tool_config(self, authored_tool_name: str) -> EffectiveToolConfig | None:
        """Return one authored deferred tool config by authored name."""
        return self._config._agent_authored_deferred_tool_config(self._agent_name(), authored_tool_name)

    def tool_runtime_overrides(self, tool_name: str) -> dict[str, object] | None:
        """Return runtime kwargs derived from this agent's authored overrides for one tool."""
        return self._config._agent_tool_runtime_overrides(self._agent_name(), tool_name)

    def deferred_tool_scope_incompatible_tools(self, authored_tool_name: str) -> list[str]:
        """Return expanded deferred tools invalid for this agent's effective execution scope."""
        return self._config._deferred_tool_scope_incompatible_tools(self._agent_name(), authored_tool_name)

    @cached_property
    def culture(self) -> tuple[str, CultureConfig] | None:
        """Configured culture assignment for this agent, if any.

        Unlike the other agent-only fields, non-agent names do not raise: culture assignment
        is a membership scan, so unknown or team names resolve to None.
        """
        return self._config._agent_culture(self._agent_name())

    @cached_property
    def knowledge_base_ids(self) -> list[str]:
        """Shared and private knowledge base IDs assigned to this agent."""
        return self._config._agent_knowledge_base_ids(self._agent_name())

    @cached_property
    def private_knowledge_base_id(self) -> str | None:
        """Synthetic knowledge base ID for this agent's private knowledge, if enabled."""
        return self._config._agent_private_knowledge_base_id(self._agent_name())

    @cached_property
    def execution_scope(self) -> WorkerScope | None:
        """Internal derived execution scope for this agent."""
        return self._config._agent_execution_scope(self._agent_name())

    @cached_property
    def scope_label(self) -> str:
        """User-facing authored scope label for this agent."""
        return self._config._agent_scope_label(self._agent_name())
