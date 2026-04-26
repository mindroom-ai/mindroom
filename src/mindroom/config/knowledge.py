"""Knowledge base configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class KnowledgeGitConfig(BaseModel):
    """Git repository synchronization settings for a knowledge base."""

    repo_url: str = Field(description="Git repository URL used as the knowledge source")
    branch: str = Field(default="main", description="Git branch to track")
    poll_interval_seconds: int = Field(
        default=300,
        ge=5,
        description="Minimum age before READY Git snapshots schedule a background on-access refresh",
    )
    credentials_service: str | None = Field(
        default=None,
        description="Optional CredentialsManager service name used for private HTTPS repos",
    )
    lfs: bool = Field(
        default=False,
        description="Enable Git LFS support for repositories that require large-file downloads",
    )
    startup_behavior: Literal["blocking", "background"] = Field(
        default="blocking",
        description="Reserved legacy setting; startup no longer blocks on or schedules Git knowledge sync",
    )
    sync_timeout_seconds: int = Field(
        default=3600,
        ge=5,
        description="Maximum time allowed for one Git sync command before it is aborted",
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
    watch: bool = Field(
        default=True,
        description="When true, READY local snapshots schedule a background refresh on agent access; when false, direct external file edits require explicit reindex or dashboard/API mutations",
    )
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
    include_extensions: list[str] | None = Field(
        default=None,
        description="Optional file extensions to include for indexing, for example ['.md', '.py']",
    )
    exclude_extensions: list[str] = Field(
        default_factory=list,
        description="Optional file extensions to exclude from indexing after include filtering",
    )
    git: KnowledgeGitConfig | None = Field(
        default=None,
        description="Optional Git sync configuration for this knowledge base",
    )

    @field_validator("include_extensions", "exclude_extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str] | None) -> list[str] | None:
        """Normalize configured extensions to lowercase dotted suffixes."""
        if value is None:
            return None
        normalized: list[str] = []
        for extension in value:
            stripped = extension.strip().lower()
            if not stripped:
                continue
            normalized.append(stripped if stripped.startswith(".") else f".{stripped}")
        return normalized

    @model_validator(mode="after")
    def validate_chunking(self) -> KnowledgeBaseConfig:
        """Ensure chunk overlap is always smaller than chunk size."""
        if self.chunk_overlap >= self.chunk_size:
            msg = "chunk_overlap must be smaller than chunk_size"
            raise ValueError(msg)
        return self
