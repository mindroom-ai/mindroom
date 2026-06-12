"""Indexing settings and storage-key identity for knowledge indexes.

The values produced here are persisted in index metadata and embedded in
on-disk storage paths and collection names, so they must stay byte-identical
across refactors.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agno.knowledge.embedder.base import Embedder
from agno.vectordb.chroma import ChromaDb

from mindroom.embeddings import effective_knowledge_embedder_signature
from mindroom.knowledge.redaction import credential_free_url_identity

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeBaseMode
    from mindroom.config.main import Config

_INDEXING_MODES: set[str] = {"semantic", "files"}


@dataclass(frozen=True)
class IndexingSettings:
    """Typed schema for settings that determine knowledge index compatibility."""

    base_id: str
    storage_root: str
    knowledge_path: str
    mode: KnowledgeBaseMode
    embedder_provider: str
    embedder_model: str
    embedder_host: str
    embedder_dimensions: str
    chunk_size: str
    chunk_overlap: str
    repo_identity: str
    git_branch: str
    git_lfs: str
    git_skip_hidden: str
    git_include_patterns: str
    git_exclude_patterns: str
    include_patterns: str
    exclude_patterns: str
    include_extensions: str
    exclude_extensions: str

    @classmethod
    def from_metadata(cls, settings: Mapping[str, str]) -> IndexingSettings | None:
        """Build typed settings from the persisted JSON object."""
        required_keys = {
            "base_id",
            "storage_root",
            "knowledge_path",
            "mode",
            "embedder_provider",
            "embedder_model",
            "embedder_host",
            "embedder_dimensions",
            "chunk_size",
            "chunk_overlap",
            "repo_identity",
            "git_branch",
            "git_lfs",
            "git_skip_hidden",
            "git_include_patterns",
            "git_exclude_patterns",
            "include_extensions",
            "exclude_extensions",
        }
        optional_keys = {"include_patterns", "exclude_patterns"}
        if not required_keys.issubset(settings) or set(settings) - required_keys - optional_keys:
            return None
        mode = settings["mode"]
        if mode not in _INDEXING_MODES:
            return None
        return cls(
            base_id=settings["base_id"],
            storage_root=settings["storage_root"],
            knowledge_path=settings["knowledge_path"],
            mode=cast("KnowledgeBaseMode", mode),
            embedder_provider=settings["embedder_provider"],
            embedder_model=settings["embedder_model"],
            embedder_host=settings["embedder_host"],
            embedder_dimensions=settings["embedder_dimensions"],
            chunk_size=settings["chunk_size"],
            chunk_overlap=settings["chunk_overlap"],
            repo_identity=settings["repo_identity"],
            git_branch=settings["git_branch"],
            git_lfs=settings["git_lfs"],
            git_skip_hidden=settings["git_skip_hidden"],
            git_include_patterns=settings["git_include_patterns"],
            git_exclude_patterns=settings["git_exclude_patterns"],
            include_patterns=settings.get("include_patterns", ""),
            exclude_patterns=settings.get("exclude_patterns", ""),
            include_extensions=settings["include_extensions"],
            exclude_extensions=settings["exclude_extensions"],
        )

    def to_metadata(self) -> dict[str, str]:
        """Return the JSON object persisted in index metadata."""
        return {
            "base_id": self.base_id,
            "storage_root": self.storage_root,
            "knowledge_path": self.knowledge_path,
            "mode": self.mode,
            "embedder_provider": self.embedder_provider,
            "embedder_model": self.embedder_model,
            "embedder_host": self.embedder_host,
            "embedder_dimensions": self.embedder_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "repo_identity": self.repo_identity,
            "git_branch": self.git_branch,
            "git_lfs": self.git_lfs,
            "git_skip_hidden": self.git_skip_hidden,
            "git_include_patterns": self.git_include_patterns,
            "git_exclude_patterns": self.git_exclude_patterns,
            "include_patterns": self.include_patterns,
            "exclude_patterns": self.exclude_patterns,
            "include_extensions": self.include_extensions,
            "exclude_extensions": self.exclude_extensions,
        }

    def query_compatibility_key(self) -> tuple[str, str, str, str, str, str, str, str]:
        """Return fields that must match for safe vector queries."""
        return (
            self.base_id,
            self.storage_root,
            self.knowledge_path,
            self.mode,
            self.embedder_provider,
            self.embedder_model,
            self.embedder_host,
            self.embedder_dimensions,
        )

    def corpus_compatibility_key(
        self,
    ) -> tuple[str, str, str, str, str, str, str, str, str, str, str, str, str, str]:
        """Return fields that must match for safe source-corpus reuse."""
        return (
            self.base_id,
            self.storage_root,
            self.knowledge_path,
            self.mode,
            self.repo_identity,
            self.git_branch,
            self.git_lfs,
            self.git_skip_hidden,
            self.git_include_patterns,
            self.git_exclude_patterns,
            self.include_patterns,
            self.exclude_patterns,
            self.include_extensions,
            self.exclude_extensions,
        )


class _CollectionExistenceEmbedder(Embedder):
    """Minimal embedder for collection probes that must never embed content."""

    def get_embedding(self, text: str) -> list[float]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, object] | None]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    async def async_get_embedding(self, text: str) -> list[float]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, object] | None]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)


def chroma_collection_exists(storage_path: Path, collection_name: str) -> bool:
    """Check collection existence without constructing Agno Knowledge."""
    vector_db = ChromaDb(
        collection=collection_name,
        path=str(storage_path),
        persistent_client=True,
        embedder=_CollectionExistenceEmbedder(),
    )
    return vector_db.exists()


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def storage_key_for_base(base_id: str, knowledge_path: Path) -> str:
    """Return the persisted storage-directory key for one knowledge base binding."""
    digest_source = f"{base_id}:{knowledge_path.resolve()}"
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:8]
    return f"{_safe_identifier(base_id)}_{digest}"


def _filter_settings_key(values: Iterable[str]) -> str:
    return str(tuple(sorted(values)))


def indexing_settings_key(config: Config, storage_path: Path, base_id: str, knowledge_path: Path) -> IndexingSettings:
    """Derive the indexing-compatibility settings for one knowledge base binding."""
    base_config = config.get_knowledge_base_config(base_id)
    git_config = base_config.git
    if base_config.mode == "semantic":
        embedder_config = config.memory.embedder.config
        embedder_provider, embedder_model, embedder_host, embedder_dimensions = effective_knowledge_embedder_signature(
            config.memory.embedder.provider,
            embedder_config.model,
            host=embedder_config.host,
            dimensions=embedder_config.dimensions,
        )
        chunk_size = str(base_config.chunk_size)
        chunk_overlap = str(base_config.chunk_overlap)
        include_extensions = (
            _filter_settings_key(base_config.include_extensions) if base_config.include_extensions is not None else ""
        )
        exclude_extensions = _filter_settings_key(base_config.exclude_extensions)
    else:
        embedder_provider = ""
        embedder_model = ""
        embedder_host = ""
        embedder_dimensions = ""
        chunk_size = ""
        chunk_overlap = ""
        include_extensions = ""
        exclude_extensions = ""
    return IndexingSettings(
        base_id=base_id,
        storage_root=str(storage_path.resolve()),
        knowledge_path=str(knowledge_path.resolve()),
        mode=base_config.mode,
        embedder_provider=embedder_provider,
        embedder_model=embedder_model,
        embedder_host=embedder_host,
        embedder_dimensions=embedder_dimensions,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        repo_identity=credential_free_url_identity(git_config.repo_url) if git_config is not None else "",
        git_branch=git_config.branch if git_config is not None else "",
        git_lfs=str(git_config.lfs) if git_config is not None else "",
        git_skip_hidden=str(git_config.skip_hidden) if git_config is not None else "",
        git_include_patterns=_filter_settings_key(git_config.include_patterns) if git_config is not None else "",
        git_exclude_patterns=_filter_settings_key(git_config.exclude_patterns) if git_config is not None else "",
        include_patterns=_filter_settings_key(base_config.include_patterns),
        exclude_patterns=_filter_settings_key(base_config.exclude_patterns),
        include_extensions=include_extensions,
        exclude_extensions=exclude_extensions,
    )
