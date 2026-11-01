"""Memory-related configuration models."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.legacy_fields import (
    RETIRED_AUTO_FLUSH_FIELDS,
    RETIRED_MEMORY_FIELDS,
    RETIRED_MEMORY_FILE_FIELDS,
    drop_retired_fields,
)
from mindroom.config.models import EmbedderConfig, normalize_api_key_setting
from mindroom.config.schema_hints import dashboard_hint
from mindroom.path_globs import validate_safe_relative_pattern
from mindroom.providers.names import normalized_provider_name

MemoryBackend = Literal["mem0", "file", "none"]
_MemorySearchMode = Literal["keyword", "semantic"]

# The memory.embedder.config fields each provider other than openai, which reads them all, may set.
_EMBEDDER_FIELDS_BY_PROVIDER = {"ollama": frozenset({"model", "host"}), "sentence_transformers": frozenset({"model"})}
# Nothing reads dimensions for a huggingface or other Mem0-only embedder, but its Mem0 collection name includes it,
# so rejecting it would cut such a deployment off from its stored memories.
_MEM0_ONLY_EMBEDDER_FIELDS = frozenset({"model", "dimensions"})


def _sets_a_value(value: object) -> bool:
    """Return whether a config value sets anything: null and blank strings do not, matching credential resolution."""
    return bool(value.strip()) if isinstance(value, str) else value is not None


def _validate_memory_search_includes(value: list[str]) -> list[str]:
    """Validate include patterns stay inside the memory root."""
    normalized = [validate_safe_relative_pattern(pattern, field_name="memory.search.include") for pattern in value]
    if not normalized:
        msg = "memory.search.include must contain at least one pattern"
        raise ValueError(msg)
    return normalized


_MemorySearchIncludes = Annotated[list[str], AfterValidator(_validate_memory_search_includes)]


class MemorySearchConfig(BaseModel):
    """Search behavior for file-backed memory."""

    model_config = ConfigDict(extra="forbid")

    mode: _MemorySearchMode = Field(
        default="keyword",
        description="Search mode for file-backed memory: keyword or semantic",
    )
    include: _MemorySearchIncludes = Field(
        default_factory=lambda: ["memory/**/*.md"],
        description="Root-relative glob patterns included in file-memory semantic search",
    )
    include_entrypoint: bool = Field(
        default=False,
        description="When true, include MEMORY.md in file-memory semantic search",
    )


class AgentMemorySearchConfig(BaseModel):
    """Optional per-agent override for file-backed memory search."""

    model_config = ConfigDict(extra="forbid")

    mode: _MemorySearchMode | None = Field(default=None, description="Per-agent memory search mode override")
    include: _MemorySearchIncludes | None = Field(
        default=None,
        description="Per-agent included memory-search glob patterns",
    )
    include_entrypoint: bool | None = Field(default=None, description="Per-agent MEMORY.md indexing override")


class _MemoryEmbedderConfig(BaseModel):
    """Memory embedder configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        default="openai",
        description="Embedder provider (openai, ollama, huggingface, sentence_transformers, etc)",
    )
    config: EmbedderConfig = Field(default_factory=EmbedderConfig, description="Provider-specific config")

    @field_validator("provider")
    @classmethod
    def _canonical_provider(cls, value: str) -> str:
        """Spell the provider as every embedder consumer compares it, so `OpenAI` selects openai."""
        return normalized_provider_name(value)

    @model_validator(mode="after")
    def _reject_fields_the_provider_never_reads(self) -> Self:
        """Reject a set field that the provider never reads, except a Mem0-only provider's dimensions."""
        if self.provider == "openai":
            return self
        accepted = _EMBEDDER_FIELDS_BY_PROVIDER.get(self.provider, _MEM0_ONLY_EMBEDDER_FIELDS)
        ignored = [
            f"memory.embedder.config.{name}"
            for name, value in self.config
            if name not in accepted and _sets_a_value(value)
        ]
        if ignored:
            listed = ignored[0] if len(ignored) == 1 else f"{', '.join(ignored[:-1])} and {ignored[-1]}"
            verb, pronoun = ("does", "it") if len(ignored) == 1 else ("do", "them")
            msg = f"{listed} {verb} not apply to the {self.provider} provider; remove {pronoun}"
            raise ValueError(msg)
        return self


class _MemoryLLMConfig(BaseModel):
    """Memory LLM configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="ollama", description="LLM provider (ollama, openai, openrouter, anthropic)")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific LLM config; may include credentials",
        json_schema_extra=dashboard_hint(secret=True),
    )

    @field_validator("provider")
    @classmethod
    def _canonical_provider(cls, value: str) -> str:
        """Spell the provider as Mem0 and every memory LLM consumer compare it, so `OpenRouter` selects openrouter."""
        return normalized_provider_name(value)

    @field_validator("config")
    @classmethod
    def _normalize_api_key(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Trim ``api_key`` like model keys so a blank one counts as unset."""
        return normalize_api_key_setting(value, "memory.llm.config.api_key")


class _MemoryFileConfig(BaseModel):
    """File-backed memory configuration."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def drop_retired_memory_file_fields(cls, data: object) -> object:
        """Drop the retired fallback root older dashboards saved, before unknown keys are rejected."""
        return drop_retired_fields(data, RETIRED_MEMORY_FILE_FIELDS)

    max_entrypoint_lines: int = Field(
        default=200,
        ge=1,
        description="Maximum number of lines to preload from MEMORY.md",
    )
    max_entrypoint_tokens: int = Field(
        default=50000,
        ge=1,
        description="Maximum tokens, as the turn's model counts them, of whole lines to preload from MEMORY.md",
    )


class _MemoryAutoFlushBatchConfig(BaseModel):
    """Batching controls for background memory auto-flush."""

    model_config = ConfigDict(extra="forbid")

    max_sessions_per_cycle: int = Field(
        default=10,
        ge=1,
        description="Maximum conversations flushed in one auto-flush pass, across all agents",
    )
    max_sessions_per_agent_per_cycle: int = Field(
        default=3,
        ge=1,
        description="Maximum conversations of one agent or private instance flushed in one auto-flush pass",
    )


class _MemoryAutoFlushContextConfig(BaseModel):
    """Existing-memory context limits injected into extraction runs."""

    model_config = ConfigDict(extra="forbid")

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

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description="Model config that runs extraction instead of the agent's own model",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    no_reply_token: str = Field(
        default="NO_REPLY",
        description="Token indicating no durable memory should be written",
    )
    max_messages_per_flush: int = Field(
        default=20,
        ge=1,
        description="Maximum new messages read by one extraction job; a longer backlog is read over later passes",
    )
    max_chars_per_flush: int = Field(
        default=12000,
        ge=1,
        description="Maximum message characters read by one extraction job",
    )
    max_extraction_seconds: int = Field(
        default=30,
        ge=1,
        description="Timeout for one extraction job before retrying in a later pass",
    )
    include_memory_context: _MemoryAutoFlushContextConfig = Field(
        default_factory=_MemoryAutoFlushContextConfig,
        description="Bounds for existing memory context included during extraction",
    )


class MemoryAutoFlushConfig(BaseModel):
    """Background memory auto-flush configuration."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def drop_retired_auto_flush_fields(cls, data: object) -> object:
        """Drop the retired dirty-queue fields older dashboards saved, before unknown keys are rejected."""
        return drop_retired_fields(data, RETIRED_AUTO_FLUSH_FIELDS)

    enabled: bool = Field(default=False, description="Enable background file-memory auto-flush worker")
    flush_interval_seconds: int = Field(
        default=300,
        ge=5,
        description="Time between auto-flush passes",
    )
    idle_seconds: int = Field(
        default=120,
        ge=0,
        description="How long a conversation must be quiet before its new turns are flushed",
    )
    stale_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        description="Give up on turns whose flush has kept failing for this long",
    )
    retry_cooldown_seconds: int = Field(
        default=30,
        ge=1,
        description="Cooldown before retrying a failed flush",
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

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def normalize_shorthand(cls, data: object) -> object:
        """Normalize memory: none shorthand into the structured config shape, without retired fields."""
        if data == "none":
            return {"backend": "none"}
        return drop_retired_fields(data, RETIRED_MEMORY_FIELDS)

    backend: MemoryBackend = Field(
        default="mem0",
        description=(
            "Memory backend: 'mem0' (vector memory), 'file' (markdown memory files), or 'none' (disabled memory)"
        ),
    )
    embedder: _MemoryEmbedderConfig = Field(
        default_factory=_MemoryEmbedderConfig,
        description="Embedder configuration for memory",
    )
    llm: _MemoryLLMConfig | None = Field(default=None, description="LLM configuration for memory")
    file: _MemoryFileConfig = Field(default_factory=_MemoryFileConfig, description="File-backed memory configuration")
    search: MemorySearchConfig = Field(
        default_factory=MemorySearchConfig,
        description="Search behavior for file-backed memory",
    )
    auto_flush: MemoryAutoFlushConfig = Field(
        default_factory=MemoryAutoFlushConfig,
        description="Background auto-flush behavior for file-backed memory",
    )
