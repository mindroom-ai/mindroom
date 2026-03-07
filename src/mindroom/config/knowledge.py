"""Knowledge base configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class KnowledgeGitConfig(BaseModel):
    """Git repository synchronization settings for a knowledge base."""

    repo_url: str = Field(description="Git repository URL used as the knowledge source")
    branch: str = Field(default="main", description="Git branch to track")
    poll_interval_seconds: int = Field(
        default=300,
        ge=5,
        description="How often to poll the remote repository for updates",
    )
    credentials_service: str | None = Field(
        default=None,
        description="Optional CredentialsManager service name used for private HTTPS repos",
    )
    skip_hidden: bool = Field(
        default=True,
        description="Skip hidden files/folders (paths with components starting with '.') during indexing",
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description="Optional root-anchored glob patterns to include (e.g. 'content/post/*/index.md')",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        description="Optional root-anchored glob patterns to exclude after include filtering",
    )


class KnowledgeBaseConfig(BaseModel):
    """Knowledge base configuration."""

    path: str = Field(default="./knowledge_docs", description="Path to knowledge documents folder")
    watch: bool = Field(default=True, description="Watch folder for changes")
    chunk_size: int = Field(
        default=5000,
        ge=128,
        description="Maximum number of characters per indexed chunk for text-like knowledge files",
    )
    chunk_overlap: int = Field(
        default=0,
        ge=0,
        description="Number of overlapping characters between adjacent chunks",
    )
    embedding_max_in_flight_requests: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum concurrent embedding requests during knowledge indexing for this base. "
            "Set to 0 to disable indexing backpressure and keep unconstrained concurrency."
        ),
    )
    git: KnowledgeGitConfig | None = Field(
        default=None,
        description="Optional Git sync configuration for this knowledge base",
    )

    @model_validator(mode="after")
    def validate_chunking(self) -> KnowledgeBaseConfig:
        """Ensure chunk overlap is always smaller than chunk size."""
        if self.chunk_overlap >= self.chunk_size:
            msg = "chunk_overlap must be smaller than chunk_size"
            raise ValueError(msg)
        return self
