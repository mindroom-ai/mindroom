"""Materialized immutable values returned by runtime config resolution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mindroom.config.agent import CultureConfig
    from mindroom.config.memory import MemoryBackend, MemorySearchConfig
    from mindroom.config.models import CompactionConfig, EffectiveToolConfig
    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.tool_system.worker_routing import WorkerScope

_EntityKind = Literal["defaults", "agent", "team", "router"]


@dataclass(frozen=True)
class ResolvedRuntimeModel:
    """Resolved active runtime model and context window for one execution context."""

    model_name: str
    context_window: int | None


def _copy_tool_config(entry: EffectiveToolConfig) -> EffectiveToolConfig:
    return deepcopy(entry)


@dataclass(frozen=True)
class ResolvedEntityView:
    """Fully materialized effective values for one validated runtime entity scope."""

    name: str | None
    _kind: _EntityKind
    _history_settings: ResolvedHistorySettings | None
    _compaction_config: CompactionConfig | None
    _has_authored_compaction_config: bool | None
    memory_backend: MemoryBackend
    _memory_search: MemorySearchConfig
    _model_name: str | None
    _available_tools: tuple[str, ...] | None
    _tool_configs: tuple[EffectiveToolConfig, ...] | None
    _authored_tool_configs: tuple[EffectiveToolConfig, ...] | None
    _authored_deferred_tool_configs: tuple[EffectiveToolConfig, ...] | None
    _tool_runtime_overrides: tuple[tuple[str, tuple[tuple[str, object], ...]], ...] | None
    _deferred_scope_incompatible_tools: tuple[tuple[str, tuple[str, ...]], ...] | None
    _culture: tuple[str, CultureConfig] | None
    _knowledge_base_ids: tuple[str, ...] | None
    _private_knowledge_base_id: str | None
    _execution_scope: WorkerScope | None
    _scope_label: str | None

    def _agent_name(self) -> str:
        if self._kind == "defaults":
            msg = "The defaults-only scope has no per-agent config"
            raise ValueError(msg)
        if self._kind != "agent":
            msg = f"Unknown agent: {self.name}"
            raise ValueError(msg)
        assert self.name is not None
        return self.name

    @property
    def history_settings(self) -> ResolvedHistorySettings:
        """Effective history replay settings for this scope."""
        if self._history_settings is None:
            msg = f"Unknown entity: {self.name}"
            raise ValueError(msg)
        return self._history_settings

    @property
    def compaction_config(self) -> CompactionConfig:
        """Effective destructive compaction config for this scope."""
        if self._compaction_config is None:
            msg = f"Unknown entity: {self.name}"
            raise ValueError(msg)
        return self._compaction_config.model_copy(deep=True)

    @property
    def has_authored_compaction_config(self) -> bool:
        """Whether destructive compaction was explicitly configured for this scope."""
        if self._has_authored_compaction_config is None:
            msg = f"Unknown entity: {self.name}"
            raise ValueError(msg)
        return self._has_authored_compaction_config

    @property
    def memory_search(self) -> MemorySearchConfig:
        """Effective file-memory search settings for this scope."""
        return self._memory_search.model_copy(deep=True)

    @property
    def model_name(self) -> str:
        """Authored model name for this agent, team, or router."""
        if self._kind == "defaults":
            msg = "The defaults-only scope has no authored model"
            raise ValueError(msg)
        if self._kind == "team" and self._model_name is None:
            msg = f"Team {self.name} has no model configured"
            raise ValueError(msg)
        assert self._model_name is not None
        return self._model_name

    @property
    def available_tools(self) -> list[str]:
        """All tools this agent may use after dynamic loading."""
        self._agent_name()
        assert self._available_tools is not None
        return list(self._available_tools)

    @property
    def tool_configs(self) -> list[EffectiveToolConfig]:
        """Effective runtime tool config entries for each authored owner."""
        self._agent_name()
        assert self._tool_configs is not None
        return [_copy_tool_config(entry) for entry in self._tool_configs]

    @property
    def authored_tool_configs(self) -> list[EffectiveToolConfig]:
        """Effective authored tool config entries before preset/implied expansion."""
        self._agent_name()
        assert self._authored_tool_configs is not None
        return [_copy_tool_config(entry) for entry in self._authored_tool_configs]

    @property
    def authored_deferred_tool_configs(self) -> list[EffectiveToolConfig]:
        """One entry per authored deferred tool in effective order."""
        self._agent_name()
        assert self._authored_deferred_tool_configs is not None
        return [_copy_tool_config(entry) for entry in self._authored_deferred_tool_configs]

    def authored_deferred_tool_config(self, authored_tool_name: str) -> EffectiveToolConfig | None:
        """Return one materialized authored deferred tool config by authored name."""
        for entry in self.authored_deferred_tool_configs:
            if entry.name == authored_tool_name:
                return entry
        return None

    def tool_runtime_overrides(self, tool_name: str) -> dict[str, object] | None:
        """Return materialized runtime kwargs for one tool."""
        self._agent_name()
        assert self._tool_runtime_overrides is not None
        for configured_name, overrides in self._tool_runtime_overrides:
            if configured_name == tool_name:
                return deepcopy(dict(overrides))
        return None

    def deferred_tool_scope_incompatible_tools(self, authored_tool_name: str) -> list[str]:
        """Return materialized expanded deferred tools invalid for this agent's scope."""
        self._agent_name()
        assert self._deferred_scope_incompatible_tools is not None
        for configured_name, tool_names in self._deferred_scope_incompatible_tools:
            if configured_name == authored_tool_name:
                return list(tool_names)
        return []

    @property
    def culture(self) -> tuple[str, CultureConfig] | None:
        """Configured culture assignment for this agent, if any."""
        if self._kind == "defaults":
            self._agent_name()
        if self._culture is None:
            return None
        culture_name, culture_config = self._culture
        return culture_name, culture_config.model_copy(deep=True)

    @property
    def knowledge_base_ids(self) -> list[str]:
        """Shared and private knowledge base IDs assigned to this agent."""
        self._agent_name()
        assert self._knowledge_base_ids is not None
        return list(self._knowledge_base_ids)

    @property
    def private_knowledge_base_id(self) -> str | None:
        """Synthetic knowledge base ID for this agent's private knowledge, if enabled."""
        self._agent_name()
        return self._private_knowledge_base_id

    @property
    def execution_scope(self) -> WorkerScope | None:
        """Internal derived execution scope for this agent."""
        self._agent_name()
        return self._execution_scope

    @property
    def scope_label(self) -> str:
        """User-facing authored scope label for this agent."""
        self._agent_name()
        assert self._scope_label is not None
        return self._scope_label
