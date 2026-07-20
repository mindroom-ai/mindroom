"""Conversation-state persistence helpers for bot flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.agent_storage import create_session_storage, get_agent_session, get_team_session
from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.entity_resolution import entity_identity_registry
from mindroom.history.interrupted_replay import interrupted_replay_prose_from_metadata
from mindroom.history.runtime import create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.runtime_protocols import SupportsConfig  # noqa: TC001
from mindroom.streaming import strip_visible_tool_markers_for_trace
from mindroom.team_scope import ad_hoc_team_scope_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog
    from agno.db.base import BaseDb
    from agno.models.message import Message

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ConversationStateWriterDeps:
    """Static collaborators for conversation-state persistence and cache writes."""

    runtime: SupportsConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str


@dataclass
class ConversationStateWriter:
    """Own the persisted conversation state for one bot."""

    deps: ConversationStateWriterDeps

    def history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.deps.agent_name in self.deps.runtime.config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        return HistoryScope(kind="agent", scope_id=self.deps.agent_name)

    def session_type_for_scope(self, scope: HistoryScope) -> SessionType:
        """Return the Agno session type used by one persisted history scope."""
        return SessionType.TEAM if scope.kind == "team" else SessionType.AGENT

    def team_history_scope(
        self,
        team_agents: list[MatrixID],
        *,
        requester_user_id: str | None = None,
    ) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        config = self.deps.runtime.config
        if self.deps.agent_name in config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        registry = entity_identity_registry(config, self.deps.runtime_paths)
        member_names: list[str] = []
        for matrix_id in team_agents:
            member_name = registry.current_entity_name_for_user_id(matrix_id.full_id) or matrix_id.username
            member_names.append(member_name)
        scope_id = (
            ad_hoc_team_scope_id(
                member_names,
                config.agents,
                requester_user_id=requester_user_id,
                missing_requester_message="Private ad hoc team history scope requires requester_user_id",
            )
            or "team_"
        )
        return HistoryScope(kind="team", scope_id=scope_id)

    def create_storage(
        self,
        execution_identity: ToolExecutionIdentity | None,
        *,
        scope: HistoryScope | None = None,
    ) -> BaseDb:
        """Create storage for one exact persisted history scope."""
        config = self.deps.runtime.config
        normalized_scope = (
            self.history_scope() if scope is None else HistoryScope(kind=scope.kind, scope_id=scope.scope_id)
        )
        if (
            normalized_scope == self.history_scope()
            and self.session_type_for_scope(normalized_scope) is SessionType.AGENT
        ):
            return create_session_storage(
                agent_name=self.deps.agent_name,
                config=config,
                runtime_paths=self.deps.runtime_paths,
                execution_identity=execution_identity,
            )
        return create_scope_session_storage(
            agent_name=normalized_scope.scope_id if normalized_scope.kind == "agent" else self.deps.agent_name,
            scope=normalized_scope,
            config=config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    def persist_response_event_id_in_session_run(
        self,
        *,
        storage: BaseDb,
        session_id: str,
        session_type: SessionType,
        run_id: str,
        response_event_id: str,
        response_sender_id: str,
        delivered_visible_body: str | None,
        delivered_body_tool_trace: Sequence[ToolTraceEntry] = (),
    ) -> None:
        """Persist Matrix response linkage onto the run that produced it.

        ``delivered_visible_body`` is the authoritative visible body of
        ``response_event_id`` and is set only when this run's own output was
        actually delivered there; notices, suppressed or failed deliveries, and
        unchanged edit targets persist the metadata linkage without wrapping
        the assistant message with an event that never carried its text. A run
        already linked to the same event is upgraded with the wrap when a
        delivered body arrives later (e.g. an interrupted run reconciled after
        terminal delivery).
        """
        # Remove exactly the display chrome MindRoom injected for this run's
        # own tool trace: the canonical body is what the model said as
        # delivered, so model-authored marker-shaped text is preserved, while
        # a body that is only injected markers (or empty) must never erase the
        # model's reply with an empty tag.
        stripped_body = (
            strip_visible_tool_markers_for_trace(delivered_visible_body, delivered_body_tool_trace)
            if delivered_visible_body
            else ""
        )
        wrap_body = stripped_body if stripped_body.strip() else ""
        session = (
            get_team_session(storage, session_id)
            if session_type is SessionType.TEAM
            else get_agent_session(storage, session_id)
        )
        if session is None or not session.runs:
            return
        for run in session.runs:
            if not isinstance(run, (RunOutput, TeamRunOutput)) or run.run_id != run_id:
                continue
            metadata = dict(run.metadata or {})
            already_linked = metadata.get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY) == response_event_id
            if already_linked and not wrap_body:
                return
            metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = response_event_id
            run.metadata = metadata
            wrapped = wrap_body and _wrap_final_assistant_message(
                run,
                response_sender_id=response_sender_id,
                response_event_id=response_event_id,
                delivered_body=wrap_body,
            )
            if not wrapped and already_linked:
                # Nothing changed: the target already carries this event's tag
                # (or no eligible message exists) and the link was in place.
                return
            storage.upsert_session(session)
            return


def _current_generation_assistant_message(run: RunOutput | TeamRunOutput) -> Message | None:
    """Return the run's own final content-bearing assistant message.

    Scanning stops at the first user- or tool-role message from the end:
    assistant entries before a user turn belong to replayed or fallback
    context, and assistant segments before a tool result are intermediate
    generations — rewriting either would bind older text to the new event.
    Tool-call stubs without string content are never targeted.
    """
    for message in reversed(run.messages or []):
        if message.role in {"user", "tool"}:
            return None
        if message.role == "assistant" and isinstance(message.content, str) and message.content:
            return message
    return None


def _wrap_final_assistant_message(
    run: RunOutput | TeamRunOutput,
    *,
    response_sender_id: str,
    response_event_id: str,
    delivered_body: str,
) -> bool:
    """Wrap the run's own final assistant message with its delivered event body.

    The CDATA body is the delivered visible text with MindRoom display chrome
    stripped, matching how the same event renders through Matrix-fallback
    history. The desired content is rebuilt from the authoritative callback
    inputs on every call and compared whole, so a repeated callback is a no-op,
    a changed body for the same event is refreshed, tags can never nest, and
    model-authored text is never parsed to make the decision. Canonical
    interrupted runs keep their synthetic interruption prose — carried as
    trusted run metadata — outside the tag, since that text was never part of
    the Matrix event. Returns whether a message changed.
    """
    message = _current_generation_assistant_message(run)
    if message is None:
        return False
    wrapped = render_msg_tag(
        sender=response_sender_id,
        body=delivered_body,
        event_id=response_event_id,
    )
    prose = interrupted_replay_prose_from_metadata(run.metadata)
    desired_content = f"{wrapped}\n\n{prose}" if prose else wrapped
    if message.content == desired_content:
        return False
    message.content = desired_content
    return True
