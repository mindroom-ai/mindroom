"""Memory-related configuration models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import EmbedderConfig

MemoryBackend = Literal["mem0", "file"]


class _MemoryEmbedderConfig(BaseModel):
    """Memory embedder configuration."""

    provider: str = Field(default="openai", description="Embedder provider (openai, huggingface, etc)")
    config: EmbedderConfig = Field(default_factory=EmbedderConfig, description="Provider-specific config")


class _MemoryLLMConfig(BaseModel):
    """Memory LLM configuration."""

    provider: str = Field(default="ollama", description="LLM provider (ollama, openai, anthropic)")
    config: dict[str, Any] = Field(default_factory=dict, description="Provider-specific LLM config")


class _MemoryFileConfig(BaseModel):
    """File-backed memory configuration."""

    path: str | None = Field(
        default=None,
        description=(
            "Directory for file-backed memory. Relative paths resolve from the config "
            "directory. Defaults to <storage_path>/memory_files when omitted."
        ),
    )
    max_entrypoint_lines: int = Field(
        default=200,
        ge=1,
        description="Maximum number of lines to preload from MEMORY.md",
    )


class _MemoryAutoFlushBatchConfig(BaseModel):
    """Batching controls for background memory auto-flush."""

    max_sessions_per_cycle: int = Field(
        default=10,
        ge=1,
        description="Maximum sessions processed in one auto-flush loop iteration",
    )
    max_sessions_per_agent_per_cycle: int = Field(
        default=3,
        ge=1,
        description="Maximum sessions per agent processed in one auto-flush loop iteration",
    )


class _MemoryAutoFlushContextConfig(BaseModel):
    """Existing-memory context limits injected into extraction runs."""

    memory_snippets: int = Field(
        default=5,
        ge=0,
        description="Maximum number of MEMORY.md snippets included for extraction dedupe context",
    )
    snippet_max_chars: int = Field(
        default=400,
        ge=1,
        description="Maximum characters per included memory snippet",
    )


class _MemoryAutoFlushExtractorConfig(BaseModel):
    """Extraction limits for one background memory flush job."""

    no_reply_token: str = Field(
        default="NO_REPLY",
        description="Token indicating no durable memory should be written",
    )
    max_messages_per_flush: int = Field(
        default=20,
        ge=1,
        description="Maximum session chat messages considered by one extraction job",
    )
    max_chars_per_flush: int = Field(
        default=12000,
        ge=1,
        description="Maximum message characters considered by one extraction job",
    )
    max_extraction_seconds: int = Field(
        default=30,
        ge=1,
        description="Timeout for one extraction job before retrying in a later cycle",
    )
    include_memory_context: _MemoryAutoFlushContextConfig = Field(
        default_factory=_MemoryAutoFlushContextConfig,
        description="Bounds for existing memory context included during extraction",
    )


class MemoryAutoFlushConfig(BaseModel):
    """Background memory auto-flush configuration."""

    enabled: bool = Field(default=False, description="Enable background file-memory auto-flush worker")
    flush_interval_seconds: int = Field(
        default=1800,
        ge=5,
        description="Background auto-flush loop interval",
    )
    idle_seconds: int = Field(
        default=120,
        ge=0,
        description="Session idle time before dirty session becomes flush-eligible",
    )
    max_dirty_age_seconds: int = Field(
        default=600,
        ge=1,
        description="Force flush eligibility once a session remains dirty for this long",
    )
    stale_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        description="Drop stale flush-state entries older than this TTL",
    )
    max_cross_session_reprioritize: int = Field(
        default=5,
        ge=0,
        description="Maximum same-agent dirty sessions reprioritized per incoming prompt",
    )
    retry_cooldown_seconds: int = Field(
        default=30,
        ge=1,
        description="Cooldown before retrying a failed extraction attempt",
    )
    max_retry_cooldown_seconds: int = Field(
        default=300,
        ge=1,
        description="Upper bound for retry cooldown backoff",
    )
    batch: _MemoryAutoFlushBatchConfig = Field(
        default_factory=_MemoryAutoFlushBatchConfig,
        description="Batch sizing controls for each auto-flush cycle",
    )
    extractor: _MemoryAutoFlushExtractorConfig = Field(
        default_factory=_MemoryAutoFlushExtractorConfig,
        description="Extraction-window and timeout controls for auto-flush",
    )


class MemoryConfig(BaseModel):
    """Memory system configuration."""

    backend: MemoryBackend = Field(
        default="mem0",
        description="Memory backend: 'mem0' (vector memory) or 'file' (markdown memory files)",
    )
    team_reads_member_memory: bool = Field(
        default=False,
        description=(
            "When true, team-context memory reads can access member agent memories in addition to the shared team scope"
        ),
    )
    embedder: _MemoryEmbedderConfig = Field(
        default_factory=_MemoryEmbedderConfig,
        description="Embedder configuration for memory",
    )
    llm: _MemoryLLMConfig | None = Field(default=None, description="LLM configuration for memory")
    file: _MemoryFileConfig = Field(default_factory=_MemoryFileConfig, description="File-backed memory configuration")
    auto_flush: MemoryAutoFlushConfig = Field(
        default_factory=MemoryAutoFlushConfig,
        description="Background auto-flush behavior for file-backed memory",
    )
