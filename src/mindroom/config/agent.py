"""Agent, team, and culture configuration models."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from .memory import MemoryBackend  # noqa: TC001
from .models import AgentLearningMode  # noqa: TC001

CultureMode = Literal["automatic", "agentic", "manual"]


class AgentConfig(BaseModel):
    """Configuration for a single agent."""

    display_name: str = Field(description="Human-readable name for the agent")
    role: str = Field(default="", description="Description of the agent's purpose")
    tools: list[str] = Field(default_factory=list, description="List of tool names")
    include_default_tools: bool = Field(
        default=True,
        description="Whether to merge defaults.tools into this agent's tools",
    )
    skills: list[str] = Field(default_factory=list, description="List of skill names")
    instructions: list[str] = Field(default_factory=list, description="Agent instructions")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    markdown: bool | None = Field(default=None, description="Whether to use markdown formatting")
    learning: bool | None = Field(default=None, description="Enable Agno Learning (defaults to true when omitted)")
    learning_mode: AgentLearningMode | None = Field(
        default=None,
        description="Learning mode for Agno Learning: always (automatic) or agentic (tool-driven)",
    )
    model: str = Field(default="default", description="Model name")
    memory_backend: MemoryBackend | None = Field(
        default=None,
        description="Memory backend override for this agent ('mem0' or 'file'); inherits memory.backend when omitted",
    )
    memory_file_path: str | None = Field(
        default=None,
        description="Custom directory to use as the file-memory scope for this agent instead of the default <root>/agent_<name>/",
    )
    knowledge_bases: list[str] = Field(
        default_factory=list,
        description="Knowledge base IDs assigned to this agent",
    )
    context_files: list[str] = Field(
        default_factory=list,
        description="File paths read at agent init and prepended to role context",
    )
    thread_mode: Literal["thread", "room"] = Field(
        default="thread",
        description="Conversation threading mode: 'thread' creates Matrix threads per conversation, 'room' uses a single continuous conversation per room (ideal for bridges/mobile)",
    )
    num_history_runs: int | None = Field(
        default=None,
        description="Number of prior Agno runs to include as history context (per-agent override)",
    )
    num_history_messages: int | None = Field(
        default=None,
        description="Max messages from history (mutually exclusive with num_history_runs)",
    )
    compress_tool_results: bool | None = Field(
        default=None,
        description="Compress tool results in history to save context (per-agent override)",
    )
    enable_session_summaries: bool | None = Field(
        default=None,
        description="Enable Agno session summaries for conversation compaction (per-agent override)",
    )
    max_tool_calls_from_history: int | None = Field(
        default=None,
        ge=0,
        description="Max tool call messages replayed from history (per-agent override)",
    )
    show_tool_calls: bool | None = Field(
        default=None,
        description="Whether to show tool call details inline in responses (per-agent override)",
    )
    sandbox_tools: list[str] | None = Field(
        default=None,
        description="Tool names to execute through sandbox proxy (overrides defaults; None = inherit)",
    )
    allow_self_config: bool | None = Field(
        default=None,
        description="Allow this agent to modify its own configuration via a tool",
    )
    delegate_to: list[str] = Field(
        default_factory=list,
        description="List of agent names this agent can delegate tasks to via tool calls",
    )

    @model_validator(mode="after")
    def _check_history_config(self) -> Self:
        if self.num_history_runs is not None and self.num_history_messages is not None:
            msg = "num_history_runs and num_history_messages are mutually exclusive"
            raise ValueError(msg)
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_agent_fields(cls, data: object) -> object:
        """Reject removed legacy fields to prevent silent misconfiguration."""
        if isinstance(data, dict):
            if "knowledge_base" in data:
                msg = "Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead."
                raise ValueError(msg)
            if "memory_dir" in data:
                msg = "Agent field 'memory_dir' was removed. Use 'context_files' and memory.backend=file instead."
                raise ValueError(msg)
        return data

    @field_validator("knowledge_bases")
    @classmethod
    def validate_unique_knowledge_bases(cls, knowledge_bases: list[str]) -> list[str]:
        """Ensure each knowledge base assignment appears at most once per agent."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for base_id in knowledge_bases:
            if base_id in seen and base_id not in duplicates:
                duplicates.append(base_id)
            seen.add(base_id)

        if duplicates:
            msg = f"Duplicate knowledge bases are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return knowledge_bases


class TeamConfig(BaseModel):
    """Configuration for a team of agents."""

    display_name: str = Field(description="Human-readable name for the team")
    role: str = Field(description="Description of the team's purpose")
    agents: list[str] = Field(description="List of agent names that compose this team")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    model: str | None = Field(default="default", description="Default model for this team (optional)")
    mode: str = Field(default="coordinate", description="Team collaboration mode: coordinate or collaborate")


class CultureConfig(BaseModel):
    """Configuration for a shared culture."""

    description: str = Field(default="", description="Description of shared principles and practices")
    agents: list[str] = Field(default_factory=list, description="List of agent names assigned to this culture")
    mode: CultureMode = Field(
        default="automatic",
        description="Culture update mode: automatic, agentic, or manual",
    )

    @field_validator("agents")
    @classmethod
    def validate_unique_agents(cls, agents: list[str]) -> list[str]:
        """Ensure each agent is assigned at most once per culture."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for agent_name in agents:
            if agent_name in seen and agent_name not in duplicates:
                duplicates.append(agent_name)
            seen.add(agent_name)

        if duplicates:
            msg = f"Duplicate agents are not allowed in a culture: {', '.join(duplicates)}"
            raise ValueError(msg)
        return agents
