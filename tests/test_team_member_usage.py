"""A team member's provider calls are counted once each, however its run ends: finished, failed, or stopped."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.session_storage.store import persist_to
from mindroom.teams.runtime import continue_team, run_team
from mindroom.teams.spec import TeamMember, TeamMode, TeamSpec
from mindroom.usage.stats import collect_admin_usage
from tests.agent_loop.helpers import gated, toolkit
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.team_runtime.squad import approval, collect, scripted

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

pytestmark = pytest.mark.asyncio

_GO = [Message(role="user", content="Go")]


def lookup() -> str:
    """Look the answer up."""
    return "found"


def publish() -> str:
    """Publish the answer."""
    return "published"


def _calling(name: str, call_id: str, input_tokens: int) -> list[Any]:
    return calls(tool_call(name, call_id), metrics=usage(input_tokens, 0))


def _delegation(*, collaborate: bool) -> list[Any]:
    if collaborate:
        return calls(tool_call("delegate_task_to_members", "call-team", task="Go"), metrics=usage(10, 0))
    return calls(tool_call("delegate_task_to_member", "call-team", member_id="worker", task="Go"), metrics=usage(10, 0))


@dataclass
class _Squad:
    config: Config
    runtime_paths: RuntimePaths
    spec: TeamSpec
    store: ConversationStore
    session: ConversationSession
    leader: ScriptedModel
    member: ScriptedModel
    hanging: asyncio.Event

    def model_tokens(self) -> dict[str, int]:
        """Return the tokens the usage export counts for each model, as the admin usage report reads them."""
        report = collect_admin_usage(config=self.config, runtime_paths=self.runtime_paths)
        return {row.model: row.totals.total_tokens for row in report.model_breakdown}

    def ctx(self) -> RunContext:
        return RunContext(run_id="team-run", session_id="session", user_id="@alice:localhost")

    async def run(self) -> TeamRunRecord:
        """Run the team to its end and return the stored team record."""
        _, record = await collect(
            run_team(self.spec, _GO, ctx=self.ctx(), persist=persist_to(self.store, self.session)),
        )
        return record

    async def run_until_stopped(self, events: Any) -> None:  # noqa: ANN401
        """Drive ``events`` until the member hangs, then stop the reply as a Stop does, and wait for its saves."""

        async def drain() -> None:
            async for _ in events:
                pass

        reply = asyncio.create_task(drain())
        await asyncio.wait_for(self.hanging.wait(), timeout=5)
        reply.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reply


def _squad(tmp_path: Path, *, leader: list[list[Any]], member: list[list[Any]], collaborate: bool = False) -> _Squad:
    config = Config(
        agents={"worker": AgentConfig(display_name="Worker")},
        teams={"squad": TeamConfig(display_name="Squad", role="Does work", agents=["worker"])},
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    store = create_scope_session_storage(
        agent_name="worker",
        scope=HistoryScope(kind="team", scope_id="squad"),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
    )
    session = new_scope_session(session_id="session", scope_id="squad", is_team=True)
    store.ensure_session_row(session)
    hanging = asyncio.Event()

    async def hang() -> None:
        hanging.set()
        await asyncio.Event().wait()

    leader_model = scripted("leader-model", *leader)
    member_model = scripted("member-model", *[[Await(hang)] if turn == _HANG else turn for turn in member])
    worker = TeamMember(
        spec=AgentSpec(
            id="worker",
            name="worker",
            model=member_model,
            system_message=Message(role="system", content="You work."),
            toolkits=(toolkit(lookup, name="lookup"), gated(publish, name="publish")),
        ),
        role="Does the work.",
    )
    spec = TeamSpec(
        id="squad",
        name="Squad",
        mode=TeamMode.COLLABORATE if collaborate else TeamMode.COORDINATE,
        model=leader_model,
        system_message=Message(role="system", content="You lead."),
        members=(worker,),
    )
    return _Squad(config, runtime_paths, spec, store, session, leader_model, member_model, hanging)


_HANG: list[Any] = []


def _no_tasks_left() -> bool:
    return not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]


@pytest.mark.parametrize("collaborate", [False, True], ids=["coordinate", "collaborate"])
async def test_stopped_team_member_keeps_its_usage(tmp_path: Path, collaborate: bool) -> None:
    """A Stop while a member works keeps every request the member already made, and stops the member."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(collaborate=collaborate), answer("Done", usage(1, 0))],
        member=[_calling("lookup", "call-1", 100), _calling("lookup", "call-2", 1000), _HANG],
        collaborate=collaborate,
    )

    await squad.run_until_stopped(
        run_team(squad.spec, _GO, ctx=squad.ctx(), persist=persist_to(squad.store, squad.session)),
    )

    assert squad.model_tokens() == {"leader-model": 10, "member-model": 1100}
    assert _no_tasks_left()


async def test_failed_team_member_keeps_its_usage_once(tmp_path: Path) -> None:
    """A member whose provider fails keeps the requests it made, and the team still answers."""

    async def overloaded() -> None:
        msg = "overloaded"
        raise ProviderError(msg, provider="Synthetic", status_code=529)

    squad = _squad(
        tmp_path,
        leader=[_delegation(collaborate=False), answer("Done", usage(1, 0))],
        member=[_calling("lookup", "call-1", 100), [Await(overloaded)]],
    )

    record = await squad.run()

    assert record.status is RunStatus.completed
    assert squad.model_tokens() == {"leader-model": 11, "member-model": 100}


async def test_finished_team_member_counts_once(tmp_path: Path) -> None:
    """A member that finishes is counted exactly once."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(collaborate=False), answer("Done", usage(1, 0))],
        member=[_calling("lookup", "call-1", 100), answer("found", usage(1000, 0))],
    )

    record = await squad.run()

    assert record.status is RunStatus.completed
    assert squad.model_tokens() == {"leader-model": 11, "member-model": 1100}


async def test_member_stopped_after_approval_keeps_its_usage(tmp_path: Path) -> None:
    """A member resumed after an approval keeps the requests it makes before a Stop, and those before it paused."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(collaborate=False), answer("Done", usage(1, 0))],
        member=[_calling("publish", "call-publish", 100), _calling("lookup", "call-2", 1000), _HANG],
    )
    paused = await squad.run()
    assert paused.status is RunStatus.paused
    # MindRoom resumes the paused run it reads back from storage.
    stored = squad.store.get_run("team-run")
    assert isinstance(stored, TeamRunRecord)

    await squad.run_until_stopped(
        continue_team(
            squad.spec,
            stored,
            _GO,
            approved={("worker", "call-publish"): approval("call-publish", "publish", {}, agent="worker")},
            ctx=squad.ctx(),
            persist=persist_to(squad.store, squad.session),
        ),
    )

    assert squad.model_tokens() == {"leader-model": 10, "member-model": 1100}
    assert _no_tasks_left()
