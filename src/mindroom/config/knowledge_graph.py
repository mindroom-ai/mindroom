"""Knowledge graph settings: the extraction model and conversation ingestion."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeGraphIngestConfig(BaseModel):
    """How the background worker turns finished conversation turns into graph episodes."""

    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(default=600, ge=30, description="Seconds between ingestion passes")
    # At least a second, so a conversation updated after a pass always carries a later update time than it had then.
    idle_seconds: int = Field(
        default=120,
        ge=1,
        description="Seconds a conversation must be idle before its new turns are ingested",
    )
    max_sessions_per_cycle: int = Field(default=10, ge=1, description="Conversations ingested per pass")
    max_episode_chars: int = Field(
        default=24000,
        ge=1000,
        le=60000,
        description="Largest episode sent for extraction, in characters; a longer turn is split into several episodes",
    )
    max_tool_result_chars: int = Field(
        default=4000,
        ge=100,
        description="Characters kept from each tool result",
    )
    backfill_days: int = Field(
        default=7,
        ge=0,
        description="A new graph starts from conversations active within this many days",
    )

    @model_validator(mode="after")
    def fit_tool_results_in_episodes(self) -> Self:
        """Keep room for a whole tool result in an episode, so the episode limit never cuts one further."""
        if self.max_tool_result_chars * 2 > self.max_episode_chars:
            msg = "max_tool_result_chars must be at most half of max_episode_chars"
            raise ValueError(msg)
        return self


class KnowledgeGraphConfig(BaseModel):
    """The Graphiti knowledge graph that graph-enabled agents build from their conversations and exploration."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description="Model alias that extracts entities and facts; required when any agent enables the graph",
    )
    extraction_instructions: str | None = Field(
        default=None,
        description=(
            "Instructions added to the built-in ones when extracting entities and facts, such as which topics, systems, "
            "or kinds of facts matter most"
        ),
    )
    ingest: KnowledgeGraphIngestConfig = Field(
        default_factory=KnowledgeGraphIngestConfig,
        description="Conversation ingestion settings",
    )
