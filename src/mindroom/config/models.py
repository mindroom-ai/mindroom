"""Shared model provider and defaults configuration models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from mindroom.credentials import validate_service_name
from mindroom.tool_system.worker_routing import WorkerScope  # noqa: TC001


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved authored tool config after defaults and per-agent overrides merge."""

    name: str
    tool_config_overrides: dict[str, object]


AgentLearningMode = Literal["always", "agentic"]
_DEFAULT_DEFAULT_TOOLS = ("scheduler",)


class StreamingConfig(BaseModel):
    """Timing parameters for streaming response edits."""

    update_interval: float = Field(
        default=5.0,
        gt=0,
        description="Steady-state seconds between message edits during LLM streaming",
    )
    min_update_interval: float = Field(default=0.5, gt=0, description="Fast edit interval at stream start")
    interval_ramp_seconds: float = Field(
        default=15.0,
        ge=0,
        description="Seconds to ramp from min to steady-state interval (0 disables ramp)",
    )


class CoalescingConfig(BaseModel):
    """Live dispatch coalescing configuration."""

    model_config = ConfigDict(extra="forbid")

    debounce_ms: int = Field(
        default=300,
        ge=0,
        description="Sliding debounce window in milliseconds for live message coalescing",
    )
    upload_grace_ms: int = Field(
        default=500,
        ge=0,
        description="Upload grace window in milliseconds for late media joining a text-first live batch",
    )


class DebugConfig(BaseModel):
    """Debug and diagnostic settings."""

    log_llm_requests: bool = False
    llm_request_log_dir: str | None = None


def _normalize_tool_entry_overrides(
    overrides: object,
    *,
    error_message: str,
) -> dict[str, object]:
    """Normalize one inline tool override mapping."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError(error_message)  # noqa: TRY004 - keep Pydantic validation errors structured
    return cast("dict[str, object]", dict(overrides))


def _coerce_named_tool_entry(data: dict[object, object]) -> dict[str, object]:
    """Normalize the explicit ``{name: ..., overrides: ...}`` form."""
    normalized = cast("dict[str, object]", dict(data))
    normalized["overrides"] = _normalize_tool_entry_overrides(
        normalized.get("overrides"),
        error_message="Tool entry overrides must be a mapping",
    )
    return normalized


def _coerce_single_key_tool_entry(data: dict[object, object]) -> dict[str, object]:
    """Normalize the compact single-key YAML form."""
    if len(data) != 1:
        msg = (
            "Tool entries must be either a string name or a single-key mapping like "
            "{shell: {extra_env_passthrough: 'DAWARICH_*'}}"
        )
        raise ValueError(msg)

    name, overrides = next(iter(data.items()))
    if not isinstance(name, str):
        msg = "Tool entry names must be strings"
        raise ValueError(msg)  # noqa: TRY004 - keep Pydantic validation errors structured

    return {
        "name": name,
        "overrides": _normalize_tool_entry_overrides(
            overrides,
            error_message=f"Tool '{name}' overrides must be a mapping",
        ),
    }


class ToolConfigEntry(BaseModel):
    """One authored tool entry with optional inline overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    overrides: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def coerce_entry(cls, data: object) -> object:
        """Normalize string and single-key YAML forms into the model shape."""
        if isinstance(data, cls):
            return data
        if isinstance(data, str):
            return {"name": data}
        if isinstance(data, dict):
            entry_dict = cast("dict[object, object]", data)
            return (
                _coerce_named_tool_entry(entry_dict)
                if "name" in entry_dict or "overrides" in entry_dict
                else _coerce_single_key_tool_entry(entry_dict)
            )
        msg = "Tool entries must be strings or single-key mappings"
        raise ValueError(msg)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Strip surrounding whitespace and reject empty tool names."""
        stripped = value.strip()
        if not stripped:
            msg = "Tool name must not be empty"
            raise ValueError(msg)
        return stripped

    @model_serializer(mode="plain")
    def serialize(self) -> object:
        """Preserve the compact YAML form when no overrides are set."""
        return self.name if not self.overrides else {self.name: self.overrides}


class ToolkitDefinition(BaseModel):
    """One dynamically loadable toolkit definition."""

    model_config = ConfigDict(validate_assignment=True)

    description: str = Field(default="", description="Short description shown to agents for this toolkit")
    tools: list[ToolConfigEntry] = Field(
        default_factory=list,
        description="Tool entries dynamically loadable together as one toolkit",
    )

    @property
    def tool_names(self) -> list[str]:
        """Return authored toolkit tool names without inline override details."""
        return [entry.name for entry in self.tools]

    @field_validator("tools")
    @classmethod
    def validate_unique_tools(cls, tools: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Ensure each toolkit tool appears at most once."""
        return validate_unique_tool_entries(tools, scope_name="toolkit")


def validate_unique_tool_entries(
    tools: list[ToolConfigEntry],
    *,
    scope_name: str,
) -> list[ToolConfigEntry]:
    """Ensure each normalized tool name appears at most once within one scope."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in tools:
        if entry.name in seen and entry.name not in duplicates:
            duplicates.append(entry.name)
        seen.add(entry.name)

    if duplicates:
        msg = f"Duplicate {scope_name} tools are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)
    return tools


def _validate_compaction_threshold_choice(
    *,
    threshold_tokens: int | None,
    threshold_percent: float | None,
) -> None:
    if threshold_tokens is not None and threshold_percent is not None:
        msg = "threshold_tokens and threshold_percent are mutually exclusive"
        raise ValueError(msg)


class CompactionOverrideConfig(BaseModel):
    """Optional per-agent overrides for automatic compaction."""

    enabled: bool | None = Field(default=None, description="Whether to auto-compact before a run")
    threshold_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Absolute history trigger threshold in tokens",
    )
    threshold_percent: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description="Context-window trigger threshold as a fraction",
    )
    reserve_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Reserved headroom for output and tool definitions",
    )
    model: str | None = Field(
        default=None,
        description="Optional model config name to use for summary generation",
    )
    notify: bool | None = Field(
        default=None,
        description="Whether to emit a Matrix compaction notice after compaction",
    )

    @model_validator(mode="after")
    def validate_threshold_choice(self) -> Self:
        """Ensure only one compaction threshold knob is authored at a time."""
        _validate_compaction_threshold_choice(
            threshold_tokens=self.threshold_tokens,
            threshold_percent=self.threshold_percent,
        )
        return self


class CompactionConfig(BaseModel):
    """Concrete automatic compaction configuration."""

    enabled: bool = Field(default=True, description="Whether to auto-compact before a run")
    threshold_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Absolute history trigger threshold in tokens (defaults to 80% of context window when both thresholds are None)",
    )
    threshold_percent: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description="Context-window trigger threshold as a fraction",
    )
    reserve_tokens: int = Field(
        default=16384,
        ge=0,
        description="Reserved headroom for output and tool definitions",
    )
    model: str | None = Field(
        default=None,
        description="Optional model config name to use for summary generation",
    )
    notify: bool = Field(
        default=False,
        description="Whether to emit a Matrix compaction notice after compaction",
    )

    @model_validator(mode="after")
    def validate_threshold_choice(self) -> Self:
        """Ensure only one compaction threshold knob is authored at a time."""
        _validate_compaction_threshold_choice(
            threshold_tokens=self.threshold_tokens,
            threshold_percent=self.threshold_percent,
        )
        return self


class DefaultsConfig(BaseModel):
    """Default configuration values for agents."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tools: list[ToolConfigEntry] = Field(
        default_factory=lambda: [ToolConfigEntry(name=name) for name in _DEFAULT_DEFAULT_TOOLS],
        description="Tool entries automatically added to every agent, with optional inline overrides",
    )
    markdown: bool = Field(default=True, description="Default markdown setting")
    enable_streaming: bool = Field(
        default=True,
        description="Enable streaming responses via progressive message edits",
    )
    coalescing: CoalescingConfig = Field(
        default_factory=CoalescingConfig,
        description="Live message coalescing settings for rapid same-sender turns",
    )
    show_stop_button: bool = Field(default=True, description="Whether to automatically show stop button on messages")
    auto_resume_after_restart: bool = Field(
        default=False,
        description="Whether restart cleanup should post a real system message to resume interrupted threaded conversations",
    )
    learning: bool = Field(default=True, description="Default Agno Learning setting")
    learning_mode: AgentLearningMode = Field(default="always", description="Default Agno Learning mode")
    compaction: CompactionConfig | None = Field(
        default=None,
        description="Default automatic compaction policy (materializes defaults when absent)",
    )
    num_history_runs: int | None = Field(
        default=None,
        ge=1,
        description="Default number of prior Agno runs to include as history context (None = all)",
    )
    num_history_messages: int | None = Field(
        default=None,
        ge=1,
        description="Default max messages from history (mutually exclusive with num_history_runs)",
    )
    compress_tool_results: bool = Field(
        default=True,
        description="Compress tool results in history to save context",
    )
    max_tool_calls_from_history: int | None = Field(
        default=None,
        ge=0,
        description="Max tool call messages replayed from history (None = no limit)",
    )
    show_tool_calls: bool = Field(
        default=True,
        description="Whether to show tool call details inline in responses",
    )
    worker_tools: list[str] | None = Field(
        default=None,
        description="Tool names to route through scoped workers by default (None = use the built-in default routing policy)",
    )
    worker_scope: WorkerScope | None = Field(
        default=None,
        description="Default worker runtime reuse mode for routed tools: shared, user, or user_agent. user reuses one runtime per requester across agents and is not an agent-level filesystem isolation boundary",
    )
    worker_grantable_credentials: list[str] | None = Field(
        default=None,
        description=(
            "Credential service names to mirror into isolated workers' shared credential layer "
            "(None = deny by default). Common built-in examples include openai, anthropic, "
            "google, google_oauth_client, github_private, and ollama. "
            "Custom service names are also valid, but they are mirrored only and are not injected as env vars. Sandbox-proxied python and shell execution use a deny-by-default env contract, and some credentials may still require worker-specific tool support beyond mirroring."
        ),
    )
    allow_self_config: bool = Field(
        default=False,
        description="Default setting for allowing agents to modify their own configuration",
    )
    max_preload_chars: int = Field(
        default=50000,
        ge=1,
        description="Hard cap for extra role preload context loaded from context_files",
    )
    streaming: StreamingConfig = Field(
        default_factory=StreamingConfig,
        description="Streaming response timing parameters",
    )
    thread_summary_model: str | None = Field(
        default=None,
        description="Model config name for generating thread summaries (e.g., 'haiku'). Uses 'default' if not set.",
    )
    thread_summary_first_threshold: int = Field(
        default=5,
        ge=1,
        description="Message count required before the first automatic thread summary is generated.",
    )
    thread_summary_subsequent_interval: int = Field(
        default=10,
        ge=1,
        description="Additional message count required between automatic thread summaries after the first one.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_defaults_fields(cls, data: object) -> object:
        """Reject removed legacy fields to prevent silent misconfiguration."""
        if isinstance(data, dict):
            if "sandbox_tools" in data:
                msg = "defaults.sandbox_tools was removed. Use defaults.worker_tools instead."
                raise ValueError(msg)
            if "allowed_toolkits" in data:
                msg = "defaults.allowed_toolkits was removed. Use defaults.tools instead."
                raise ValueError(msg)
            if "initial_toolkits" in data:
                msg = "defaults.initial_toolkits was removed. Use defaults.tools instead."
                raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _check_history_config(self) -> Self:
        if self.num_history_runs is not None and self.num_history_messages is not None:
            msg = "num_history_runs and num_history_messages are mutually exclusive"
            raise ValueError(msg)
        return self

    @property
    def tool_names(self) -> list[str]:
        """Return default tool names without inline override details."""
        return [entry.name for entry in self.tools]

    @field_validator("tools")
    @classmethod
    def validate_unique_tools(cls, tools: list[ToolConfigEntry]) -> list[ToolConfigEntry]:
        """Ensure each default tool appears at most once."""
        return validate_unique_tool_entries(tools, scope_name="default")

    @field_validator("worker_grantable_credentials")
    @classmethod
    def validate_worker_grantable_credentials(
        cls,
        services: list[str] | None,
    ) -> list[str] | None:
        """Normalize configured worker-grantable credential service names."""
        if services is None:
            return None
        return [validate_service_name(service) for service in services]


class EmbedderConfig(BaseModel):
    """Configuration for memory embedder."""

    model: str = Field(default="text-embedding-3-small", description="Model name for embeddings")
    api_key: str | None = Field(default=None, description="API key (usually from environment variable)")
    host: str | None = Field(default=None, description="Host URL for self-hosted models (Ollama, llama.cpp, etc.)")
    dimensions: int | None = Field(
        default=None,
        ge=1,
        description="Optional embedding dimension override for OpenAI-compatible providers",
    )


class ModelConfig(BaseModel):
    """Configuration for an AI model."""

    provider: str = Field(
        description="Model provider (openai, anthropic, vertexai_claude, ollama, etc)",
    )
    id: str = Field(description="Model ID specific to the provider")
    host: str | None = Field(default=None, description="Optional host URL (e.g., for Ollama)")
    api_key: str | None = Field(default=None, description="Optional API key (usually from env vars)")
    extra_kwargs: dict[str, Any] | None = Field(
        default=None,
        description="Additional provider-specific parameters passed directly to the model",
    )
    context_window: int | None = Field(
        default=None,
        ge=1,
        description="Context window size in tokens. MindRoom needs it on the active runtime model to enforce replay budgets, and an explicit compaction.model also needs its own context_window for destructive compaction",
    )


class RouterConfig(BaseModel):
    """Configuration for the router system."""

    model: str = Field(default="default", description="Model to use for routing decisions")
    startup_thread_prewarm: bool = Field(
        default=True,
        description="Whether the router may prewarm recent thread snapshots for rooms already joined when first sync completes",
    )
