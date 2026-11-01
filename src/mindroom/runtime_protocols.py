"""Small runtime Protocols for extracted bot collaborators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping

    import nio

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
    from mindroom.automations.runner import AutomationRunner
    from mindroom.bot import AgentBot
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.desktop.identity import DesktopControllerIdentity
    from mindroom.event_journal import EventJournalStore
    from mindroom.hooks import HookContextSupport, HookRegistryState
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.orchestration.config_lifecycle import ConfigReloadLifecycle
    from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.scheduling.runtime import ScheduledTaskRuntime
    from mindroom.skill_learning.runner import SkillReviewRunner
    from mindroom.tool_system.plugins import PluginReloadResult

__all__ = [
    "OrchestratorRuntime",
    "SupportsAgentReplyMemberships",
    "SupportsClientConfig",
    "SupportsClientConfigMemberships",
    "SupportsClientConfigOrchestrator",
    "SupportsConfig",
    "SupportsConfigOrchestrator",
    "SupportsRunningState",
]


class SupportsRunningState(Protocol):
    """Expose whether a managed runtime actor is currently running."""

    running: bool


class OrchestratorRuntime(SupportsRunningState, Protocol):
    """Narrow orchestrator surface used by extracted runtime collaborators."""

    @property
    def config(self) -> Config: ...  # noqa: D102

    @property
    def runtime_paths(self) -> RuntimePaths: ...  # noqa: D102

    @property
    def api_enabled(self) -> bool: ...  # noqa: D102

    @property
    def agent_bots(self) -> Mapping[str, AgentBot]: ...  # noqa: D102

    @property
    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler: ...  # noqa: D102

    @property
    def agent_cli_registry(self) -> TurnToolRegistry: ...  # noqa: D102

    @property
    def skill_reviews(self) -> SkillReviewRunner: ...  # noqa: D102

    @property
    def automations(self) -> AutomationRunner: ...  # noqa: D102

    @property
    def budgets(self) -> BudgetMonitor: ...  # noqa: D102

    @property
    def scheduled_tasks(self) -> ScheduledTaskRuntime: ...  # noqa: D102

    @property
    def script_runtime(self) -> ScriptRuntimeLifecycle: ...  # noqa: D102

    @property
    def agent_reply_memberships(self) -> AgentReplyMembershipIndex: ...  # noqa: D102

    @property
    def agent_reply_membership_sync(self) -> AgentReplyMembershipSync: ...  # noqa: D102

    @property
    def response_admission_gate(self) -> ResponseAdmissionGate: ...  # noqa: D102

    @property
    def hook_registry_state(self) -> HookRegistryState: ...  # noqa: D102

    @property
    def config_reload(self) -> ConfigReloadLifecycle: ...  # noqa: D102

    def event_journal_store(self) -> EventJournalStore:
        """Return the one event-journal store every bot of this runtime borrows."""
        ...

    def observe_room_activity(self, room_id: str) -> None:
        """Note that a bot admitted a conversation event in one room."""
        ...

    def router_hook_context(self) -> HookContextSupport | None:
        """Return the router bot's hook support, whose client backs what another bot's own cannot."""
        ...

    def running_entity_client(self, entity_name: str) -> nio.AsyncClient | None:
        """Return the Matrix client of one running managed entity."""
        ...

    def open_reply_event_id(self, entity_name: str, room_id: str, thread_id: str | None) -> Awaitable[str | None]:
        """Return the event of one running managed entity's unfinished reply in one conversation."""
        ...

    def reload_plugins_now(self, *, source: str) -> Awaitable[PluginReloadResult]: ...  # noqa: D102

    def validate_managed_entity_identities(self) -> None:
        """Validate persisted managed Matrix identities for the live config."""
        ...

    def running_entity_names(self) -> frozenset[str]:
        """Return the entities whose current bot generation is running."""
        ...

    def entity_first_sync_complete(self, entity_name: str) -> bool | None:
        """Return first-sync readiness for the current entity generation."""
        ...

    def desktop_controller_identity(self, entity_name: str) -> DesktopControllerIdentity:
        """Resolve the current running bot's already-owned Matrix device pin."""
        ...

    def handle_bot_ready(self, bot: AgentBot) -> Awaitable[None]:
        """Handle a managed bot completing its first sync."""
        ...

    def refresh_agent_reply_memberships(self) -> Awaitable[None]:
        """Rebuild room-backed reply grants from the router client."""
        ...

    def send_approval_notice(
        self,
        *,
        room_id: str,
        approval_event_id: str,
        thread_id: str | None,
        reason: str,
    ) -> Awaitable[bool]:
        """Send an approval-related notice through the runtime transport."""
        ...


class SupportsConfig(Protocol):
    """Expose the runtime config snapshot."""

    @property
    def config(self) -> Config: ...  # noqa: D102


class SupportsAgentReplyMemberships(Protocol):
    """Expose the shared room-membership reply grant index."""

    @property
    def agent_reply_memberships(self) -> AgentReplyMembershipIndex: ...  # noqa: D102


class SupportsClientConfig(Protocol):
    """Expose the Matrix client plus runtime config."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102


class SupportsClientConfigMemberships(SupportsClientConfig, SupportsAgentReplyMemberships, Protocol):
    """Expose Matrix client/config state plus shared reply membership grants."""


class SupportsConfigOrchestrator(SupportsConfig, SupportsAgentReplyMemberships, Protocol):
    """Expose the config plus the orchestrator handle."""

    @property
    def orchestrator(self) -> OrchestratorRuntime: ...  # noqa: D102


class SupportsClientConfigOrchestrator(SupportsClientConfig, SupportsAgentReplyMemberships, Protocol):
    """Expose client/config access, orchestrator access, and runtime freshness."""

    @property
    def orchestrator(self) -> OrchestratorRuntime: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102
