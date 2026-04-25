"""Public knowledge package interface."""

# ruff: noqa: RUF022

from mindroom.knowledge.manager import KnowledgeManager, knowledge_source_signature, list_knowledge_files
from mindroom.knowledge.redaction import (
    credential_free_url_identity,
    redact_credentials_in_text,
    redact_url_credentials,
)
from mindroom.knowledge.refresh_owner import (
    KnowledgeRefreshOwner,
    OrchestratorKnowledgeRefreshOwner,
    PerBindingKnowledgeRefreshOwner,
    StandaloneKnowledgeRefreshOwner,
)
from mindroom.knowledge.refresh_runner import refresh_knowledge_binding
from mindroom.knowledge.registry import (
    KnowledgeRefreshKey,
    KnowledgeSnapshotKey,
    KnowledgeSnapshotLookup,
    PublishedIndexingState,
    PublishedKnowledgeSnapshot,
    clear_published_snapshots,
    get_published_snapshot,
    load_published_indexing_state,
    publish_snapshot,
    remove_source_path_from_published_snapshots,
    resolve_refresh_key,
    resolve_snapshot_key,
    snapshot_indexed_count,
    snapshot_metadata_path,
)
from mindroom.knowledge.utils import (
    KnowledgeAccessSupport,
    KnowledgeAvailability,
    format_knowledge_availability_notice,
    get_agent_knowledge,
)

__all__ = [
    "KnowledgeManager",
    "get_agent_knowledge",
    "KnowledgeAccessSupport",
    "KnowledgeAvailability",
    "format_knowledge_availability_notice",
    "KnowledgeRefreshOwner",
    "OrchestratorKnowledgeRefreshOwner",
    "PerBindingKnowledgeRefreshOwner",
    "StandaloneKnowledgeRefreshOwner",
    "KnowledgeSnapshotKey",
    "KnowledgeRefreshKey",
    "KnowledgeSnapshotLookup",
    "PublishedIndexingState",
    "PublishedKnowledgeSnapshot",
    "get_published_snapshot",
    "load_published_indexing_state",
    "publish_snapshot",
    "resolve_snapshot_key",
    "resolve_refresh_key",
    "snapshot_metadata_path",
    "snapshot_indexed_count",
    "clear_published_snapshots",
    "remove_source_path_from_published_snapshots",
    "refresh_knowledge_binding",
    "list_knowledge_files",
    "knowledge_source_signature",
    "credential_free_url_identity",
    "redact_credentials_in_text",
    "redact_url_credentials",
]
