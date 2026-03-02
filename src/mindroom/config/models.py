"""Shared model provider and defaults configuration models."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

AgentLearningMode = Literal["always", "agentic"]
_DEFAULT_DEFAULT_TOOLS = ("scheduler",)


class DefaultsConfig(BaseModel):
    """Default configuration values for agents."""

    tools: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_DEFAULT_TOOLS),
        description="Tool names automatically added to every agent",
    )
    markdown: bool = Field(default=True, description="Default markdown setting")
    enable_streaming: bool = Field(
        default=True,
        description="Enable streaming responses via progressive message edits",
    )
    show_stop_button: bool = Field(default=False, description="Whether to automatically show stop button on messages")
    learning: bool = Field(default=True, description="Default Agno Learning setting")
    learning_mode: AgentLearningMode = Field(default="always", description="Default Agno Learning mode")
    num_history_runs: int | None = Field(
        default=None,
        description="Default number of prior Agno runs to include as history context (None = all)",
    )
    num_history_messages: int | None = Field(
        default=None,
        description="Default max messages from history (mutually exclusive with num_history_runs)",
    )
    compress_tool_results: bool = Field(
        default=True,
        description="Compress tool results in history to save context",
    )
    enable_session_summaries: bool = Field(
        default=False,
        description="Enable Agno session summaries for conversation compaction",
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
    sandbox_tools: list[str] | None = Field(
        default=None,
        description="Tool names to sandbox by default for all agents (None = use env var config)",
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

    @model_validator(mode="after")
    def _check_history_config(self) -> Self:
        if self.num_history_runs is not None and self.num_history_messages is not None:
            msg = "num_history_runs and num_history_messages are mutually exclusive"
            raise ValueError(msg)
        return self

    @field_validator("tools")
    @classmethod
    def validate_unique_tools(cls, tools: list[str]) -> list[str]:
        """Ensure each default tool appears at most once."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for tool_name in tools:
            if tool_name in seen and tool_name not in duplicates:
                duplicates.append(tool_name)
            seen.add(tool_name)

        if duplicates:
            msg = f"Duplicate default tools are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return tools


class EmbedderConfig(BaseModel):
    """Configuration for memory embedder."""

    model: str = Field(default="text-embedding-3-small", description="Model name for embeddings")
    api_key: str | None = Field(default=None, description="API key (usually from environment variable)")
    host: str | None = Field(default=None, description="Host URL for self-hosted models (Ollama, llama.cpp, etc.)")


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
        description="Context window size in tokens; when set, history is dynamically reduced toward an 80% target of this limit",
    )


class RouterConfig(BaseModel):
    """Configuration for the router system."""

    model: str = Field(default="default", description="Model to use for routing decisions")
