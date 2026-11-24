"""How a team's leader runs in one turn or resume: the team, its storage, hooks, request settings, and model.

A live turn and a saved approval's resume each build one ``LeaderRun``; a turn starts the leader's run from it, and a
resume continues a paused one, so both run the leader the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.spec import RunContext
from mindroom.approval.resume import resume_inputs
from mindroom.run_storage import persist_into
from mindroom.teams.runtime import continue_team, run_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.history.types import ResolvedReplayPlan
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import TeamRunRecord
    from mindroom.run_storage import RunStorage, TerminalRecord
    from mindroom.teams.members import BuiltTeam
    from mindroom.teams.runtime import TeamDecisions, TeamRunEvent


@dataclass(frozen=True)
class LeaderRun:
    """How the leader of ``built`` runs: where its run stores, its hooks, its request settings, and its model.

    ``model_name`` is the configured model the team run uses, which its tool calls carry. Every run shares the
    stored session's ``session_state`` with its tools, which the store writes back.
    """

    built: BuiltTeam
    storage: RunStorage
    hooks: LoopHooks
    request_template: ModelRequest
    model_name: str

    def start(
        self,
        run_input: Sequence[Message],
        *,
        run_id: str,
        user_id: str | None,
        metadata: dict[str, Any] | None,
        records: TerminalRecord[TeamRunRecord] | None,
    ) -> AsyncIterator[TeamRunEvent]:
        """Start the leader's run over ``run_input``."""
        return run_team(
            self.built.spec,
            run_input,
            ctx=RunContext(
                run_id=run_id,
                session=self.storage.session,
                user_id=user_id,
                metadata=metadata,
                history_scope=self.storage.scope,
                model_name=self.model_name,
            ),
            hooks=self.hooks,
            persist=persist_into(records, self.storage),
            request_template=self.request_template,
        )

    def resume(
        self,
        paused: TeamRunRecord,
        decisions: TeamDecisions,
        *,
        plan: ResolvedReplayPlan | None,
        user_id: str | None,
        records: TerminalRecord[TeamRunRecord],
    ) -> AsyncIterator[TeamRunEvent]:
        """Continue ``paused`` with ``decisions``, its leader's request replaying the stored history ``plan`` selects."""
        request, context = resume_inputs(
            paused,
            self.storage,
            self.built.history_settings,
            plan,
            user_id=user_id,
            model_name=self.model_name,
            history_scope=self.storage.scope,
        )
        return continue_team(
            self.built.spec,
            paused,
            request,
            decisions=decisions,
            context=context,
            hooks=self.hooks,
            persist=persist_into(records, self.storage),
            request_template=self.request_template,
        )


__all__ = ["LeaderRun"]
