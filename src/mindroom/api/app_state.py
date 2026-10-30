"""What one dashboard API app holds: its own state, and what the runtime it serves lends it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.agent_reply_membership import AgentReplyMembershipIndex

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.api.computers import ComputerRuntime
    from mindroom.api.config_lifecycle import ApiState
    from mindroom.api.mcp_gateway import GatewayRuntime
    from mindroom.api.usage_export import UsageExportRunner
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.reload_status import ConfigReloadStatus
    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.watch import KnowledgeSourceWatcher
    from mindroom.response_activity import ActiveResponseIdentity, ActiveScriptRunInfo
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.scheduling.runtime import ScheduledTaskRuntime
    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.thread_export.workspace_sync import WorkspaceThreadExportRunner
    from mindroom.worker_computer.sessions import ComputerSessionStore
    from mindroom.workers.backend import WorkerBackend


@dataclass(frozen=True)
class EmbeddedRuntime:
    """What `mindroom run` lends the dashboard API it serves: bound once before the server starts, cleared once it stops.

    Values that change while the runtime runs are callables into their owners, never copies.
    """

    knowledge_refresh_scheduler: KnowledgeRefreshScheduler
    agent_reply_memberships: AgentReplyMembershipIndex
    response_admission_gate: ResponseAdmissionGate
    budget_monitor: BudgetMonitor
    # The scheduled-task owner, whose bots back schedule edits the router may not write.
    scheduled_tasks: ScheduledTaskRuntime
    thread_export_runner: WorkspaceThreadExportRunner
    leave_matrix_room: Callable[[str, str], Awaitable[bool]]
    active_calls: Callable[[], list[ActiveResponseIdentity]]
    active_script_runs: Callable[[], Awaitable[list[ActiveScriptRunInfo]]]
    config_reload_status: Callable[[], ConfigReloadStatus]
    agent_cli_registry: TurnToolRegistry
    script_broker: ScriptToolBroker
    script_worker_keepalive: Callable[[WorkerBackend], None]


@dataclass(frozen=True)
class ExternalTriggerRuntime:
    """Runtime objects needed to deliver accepted external triggers, bound per published config generation."""

    client: object
    conversation_reader: object
    config_generation: int
    is_trigger_snapshot_ready: Callable[[TriggerDeliverySnapshot], Awaitable[bool]]
    # The router bot's fence: delivery runs as the router, under its run phase and the runtime's admission gate.
    admission_fence: Callable[[], AbstractAsyncContextManager[None]]


@dataclass
class _MindroomAppState:
    """Single typed namespace for FastAPI ``app.state`` attributes used across the API.

    What the runtime lends is read through ``embedded``; without a runtime those reads find nothing to use, and the
    membership index is empty, so membership-based access fails closed.
    """

    api_state: ApiState | None = None
    api_auth_account_id: str | None = None
    embedded: EmbeddedRuntime | None = None
    # A dashboard API serving without `mindroom run` schedules and watches knowledge refreshes itself.
    standalone_knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    knowledge_source_watcher: KnowledgeSourceWatcher | None = None
    # Rebound per published config generation, or as integrations start and stop.
    external_trigger_runtime: ExternalTriggerRuntime | None = None
    computer_runtime: ComputerRuntime | None = None
    # Each bound integration's runtime by integration name; the integration's own routes read its entry.
    integration_runtimes: dict[str, object] = field(default_factory=dict)
    # The runtime's registry for the primary app, and the gateway listener app's own binding of the same registry.
    agent_cli_registry: TurnToolRegistry | None = None
    openai_responses: set[ActiveResponseIdentity] = field(default_factory=set)
    mcp_gateway_runtime: GatewayRuntime | None = None
    computer_sessions: ComputerSessionStore | None = None
    usage_export_runner: UsageExportRunner | None = None

    @property
    def knowledge_refresh_scheduler(self) -> KnowledgeRefreshScheduler | None:
        """Return the runtime's knowledge refresh scheduler, or a standalone API's own."""
        if self.embedded is not None:
            return self.embedded.knowledge_refresh_scheduler
        return self.standalone_knowledge_refresh_scheduler

    @property
    def agent_reply_memberships(self) -> AgentReplyMembershipIndex:
        """Return the runtime's room-membership reply-grant index, or an empty one that grants nothing."""
        return self.embedded.agent_reply_memberships if self.embedded is not None else AgentReplyMembershipIndex()

    @property
    def response_admission_gate(self) -> ResponseAdmissionGate | None:
        """Return the runtime's response admission gate."""
        return None if self.embedded is None else self.embedded.response_admission_gate

    @property
    def budget_monitor(self) -> BudgetMonitor | None:
        """Return the runtime's spending-budget monitor."""
        return None if self.embedded is None else self.embedded.budget_monitor

    @property
    def scheduled_tasks(self) -> ScheduledTaskRuntime | None:
        """Return the runtime's scheduled-task owner."""
        return None if self.embedded is None else self.embedded.scheduled_tasks

    @property
    def thread_export_runner(self) -> WorkspaceThreadExportRunner | None:
        """Return the runtime's workspace thread-export runner."""
        return None if self.embedded is None else self.embedded.thread_export_runner

    @property
    def leave_matrix_room(self) -> Callable[[str, str], Awaitable[bool]] | None:
        """Return how the runtime makes one of its bots leave a room."""
        return None if self.embedded is None else self.embedded.leave_matrix_room

    @property
    def active_calls(self) -> Callable[[], list[ActiveResponseIdentity]] | None:
        """Return the runtime's reader of active voice calls."""
        return None if self.embedded is None else self.embedded.active_calls

    @property
    def active_script_runs(self) -> Callable[[], Awaitable[list[ActiveScriptRunInfo]]] | None:
        """Return the runtime's reader of active background script runs."""
        return None if self.embedded is None else self.embedded.active_script_runs

    @property
    def config_reload_status(self) -> Callable[[], ConfigReloadStatus] | None:
        """Return the runtime's reader of its config reload status."""
        return None if self.embedded is None else self.embedded.config_reload_status

    @property
    def script_worker_keepalive(self) -> Callable[[WorkerBackend], None] | None:
        """Return how the runtime keeps workers with live background scripts from idle cleanup."""
        return None if self.embedded is None else self.embedded.script_worker_keepalive


def ensure_app_state(api_app: FastAPI) -> _MindroomAppState:
    """Bind (or return) the :class:`_MindroomAppState` for ``api_app``."""
    existing = getattr(api_app.state, "mindroom_app_state", None)
    if isinstance(existing, _MindroomAppState):
        return existing
    state = _MindroomAppState()
    api_app.state.mindroom_app_state = state
    return state


def app_state(api_app: FastAPI) -> _MindroomAppState:
    """Return the :class:`_MindroomAppState` bound to ``api_app``."""
    state = getattr(api_app.state, "mindroom_app_state", None)
    if not isinstance(state, _MindroomAppState):
        msg = "MindRoom app state is not initialized"
        raise TypeError(msg)
    return state
