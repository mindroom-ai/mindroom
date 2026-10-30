"""What one dashboard API app holds: its own state, and what the runtime it serves lends it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.api.computers import ComputerRuntime
    from mindroom.api.config_lifecycle import ApiState
    from mindroom.api.mcp_gateway import GatewayRuntime
    from mindroom.api.usage_export import UsageExportRunner
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.reload_status import ConfigReloadStatus
    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
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
    """Single typed namespace for FastAPI ``app.state`` attributes used across the API."""

    api_state: ApiState | None = None
    api_auth_account_id: str | None = None
    # What `mindroom run` lends this app while it serves it; read through `runtime`.
    embedded: EmbeddedRuntime | None = None
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
    def runtime(self) -> EmbeddedRuntime:
        """Return what `mindroom run` lent this app; the app serves only inside `mindroom run`, which binds it first."""
        if self.embedded is None:
            msg = "The dashboard API serves only inside `mindroom run`"
            raise RuntimeError(msg)
        return self.embedded


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
