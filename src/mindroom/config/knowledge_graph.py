"""Knowledge graph settings: the graph database, the extraction model, and conversation ingestion."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeGraphIngestConfig(BaseModel):
    """How the background worker turns finished conversation turns into graph episodes."""

    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(default=600, ge=30, description="Seconds between ingestion passes")
    idle_seconds: int = Field(
        default=120,
        ge=0,
        description="Seconds a conversation must be idle before its new turns are ingested",
    )
    max_sessions_per_cycle: int = Field(default=10, ge=1, description="Conversations ingested per pass")
    max_episode_chars: int = Field(
        default=24000,
        ge=1000,
        le=60000,
        description="Largest episode sent for extraction, in characters; turns are split into episodes at turn boundaries",
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


class KnowledgeGraphConfig(BaseModel):
    """The Graphiti knowledge graph that graph-enabled agents build from their conversations and exploration."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["falkordb_lite", "falkordb"] = Field(
        default="falkordb_lite",
        description="falkordb_lite runs an embedded database in MindRoom storage; falkordb connects to a FalkorDB server",
    )
    url: str | None = Field(
        default=None,
        description="FalkorDB server URL for the falkordb backend, for example redis://user:password@graph:6379",
    )
    model: str | None = Field(
        default=None,
        description="Model alias that extracts entities and facts; required when any agent enables the graph",
    )
    ingest: KnowledgeGraphIngestConfig = Field(
        default_factory=KnowledgeGraphIngestConfig,
        description="Conversation ingestion settings",
    )
