"""Public knowledge package interface."""

# ruff: noqa: RUF022

from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.refresh_owner import (
    OrchestratorKnowledgeRefreshOwner,
    StandaloneKnowledgeRefreshOwner,
)
from mindroom.knowledge.shared_managers import (
    ensure_shared_knowledge_manager,
    get_shared_knowledge_manager_for_config,
    initialize_shared_knowledge_managers,
    referenced_shared_knowledge_base_ids,
    shutdown_shared_knowledge_managers,
)
from mindroom.knowledge.utils import (
    KnowledgeAccessSupport,
    KnowledgeAvailability,
    ensure_request_knowledge_managers,
    format_knowledge_availability_notice,
    get_agent_knowledge,
)

__all__ = [
    "KnowledgeManager",
    "initialize_shared_knowledge_managers",
    "shutdown_shared_knowledge_managers",
    "ensure_shared_knowledge_manager",
    "get_shared_knowledge_manager_for_config",
    "referenced_shared_knowledge_base_ids",
    "ensure_request_knowledge_managers",
    "get_agent_knowledge",
    "KnowledgeAccessSupport",
    "KnowledgeAvailability",
    "format_knowledge_availability_notice",
    "OrchestratorKnowledgeRefreshOwner",
    "StandaloneKnowledgeRefreshOwner",
]
