"""Every config-driven runtime service has the one lifecycle the orchestrator syncs and stops."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.automations.runner import AutomationRunner
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.knowledge_graph.runtime import KnowledgeGraphRuntime
from mindroom.memory import MemoryAutoFlushService
from mindroom.orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.thread_export.workspace_sync import WorkspaceThreadExportDeps, WorkspaceThreadExportRunner

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.orchestration.services import RuntimeService


async def _no_redactions(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
    del agent_name, room_id, event_ids
    return frozenset()


@pytest.mark.asyncio
async def test_runtime_services_share_the_sync_and_stop_lifecycle(tmp_path: Path) -> None:
    """Each service fits ``RuntimeService``, names its own shutdown step, and stops before it ever started."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = Config()
    memberships = AgentReplyMembershipIndex()
    services: tuple[RuntimeService, ...] = (
        MemoryAutoFlushService(
            runtime_paths=runtime_paths,
            config_provider=lambda: config,
            redacted_event_ids=_no_redactions,
        ),
        KnowledgeSourceWatcher(KnowledgeRefreshScheduler(), runtime_paths),
        KnowledgeGraphRuntime(
            runtime_paths=runtime_paths,
            config_provider=lambda: config,
            redacted_event_ids=_no_redactions,
        ),
        TodoPokeRuntimeCoordinator(
            runtime_paths=runtime_paths,
            config_provider=lambda: config,
            bot_provider=lambda _name: None,
            agent_reply_memberships=memberships,
        ),
        AutomationRunner(
            runtime_paths=runtime_paths,
            config_provider=lambda: config,
            bot_provider=lambda _name: None,
            definition_provider=lambda _name: None,
            agent_reply_memberships=memberships,
        ),
        BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: config),
        WorkspaceThreadExportRunner(
            WorkspaceThreadExportDeps(
                runtime_paths=runtime_paths,
                config_provider=lambda: config,
                bot_provider=lambda _name: None,
                response_admission_gate=ResponseAdmissionGate(),
                agent_reply_memberships=memberships,
            ),
        ),
    )

    for service in services:
        await service.stop()

    assert [service.name for service in services] == [
        "memory_auto_flush",
        "knowledge_source_watchers",
        "knowledge_graph",
        "todo_poke",
        "automations",
        "budgets",
        "thread_exports",
    ]
