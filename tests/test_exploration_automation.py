"""The exploration automation runs when there are leads or a revisit is due, and lists what conversations touched."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.automations import exploration
from mindroom.automations.exploration import _collect_leads, check_exploration
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

DAY = 86_400


def _config(**entry: object) -> Config:
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=True,
                tools=["github", "duckduckgo"],
                automations=[{"name": "exploration", "room": "lab", **entry}],
            ),
        },
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra"),
            "luna": ModelConfig(provider="openai", id="gpt-6-luna"),
        },
        router=RouterConfig(model="default"),
        knowledge_graph={"model": "luna"},
    )


def _context(config: Config, paths: RuntimePaths, tmp_path: Path) -> AutomationContext:
    (entry,) = config.resolve_entity("mind").automations
    return AutomationContext(
        agent_name="mind",
        config=config,
        runtime_paths=paths,
        entry=entry,
        options={},
        settings={},
        requester_id=None,
        execution_identity=None,
        workspace=None,
        state_dir=tmp_path / "state",
    )


def _tool(name: str, result: str = "ok", **args: object) -> ToolExecution:
    return ToolExecution(tool_call_id=f"call-{name}", tool_name=name, tool_args=args, result=result)


def _seed(
    config: Config,
    paths: RuntimePaths,
    session_id: str,
    tools: list[ToolExecution],
    *,
    created_at: int,
    hook_source: str | None = None,
    updated_at: int | None = None,
) -> None:
    metadata: dict[str, object] = {"room_id": "!room:x", "thread_id": session_id.rsplit(":", 1)[-1]}
    if hook_source is not None:
        metadata[HOOK_SOURCE_RUN_METADATA_KEY] = hook_source
    run = RunOutput(
        run_id=f"run-{session_id}",
        session_id=session_id,
        agent_id="mind",
        created_at=created_at,
        status=RunStatus.completed,
        metadata=metadata,
        messages=[Message(role="user", content="q"), Message(role="assistant", content="a")],
        tools=tools,
    )
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    try:
        seed_session(storage, AgentSession(session_id=session_id, agent_id="mind", runs=[run]))
        # The conversation was last active when its turn was made, unless a later reply updated it.
        with storage.db_engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {storage.session_table_name} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
                (updated_at or created_at, session_id),
            )
    finally:
        storage.close()


def _finish(ask: Ask | None, config: Config) -> None:
    """End the run the prompt started, as the runner does once its answer is final."""
    assert ask is not None
    assert ask.then is not None
    ask.then(config, "$thread", False)


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)


def test_first_check_explores_even_without_leads(tmp_path: Path) -> None:
    """A newly enabled exploration of an agent people talk to starts by exploring what its tools reach."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [], created_at=int(time.time()) - 60)

    ask = check_exploration(_context(config, paths, tmp_path))

    assert isinstance(ask, Ask)
    assert ask.new_thread
    assert "at most 40 tool calls" in ask.text
    assert "No tool calls" in ask.text


def test_no_leads_and_a_recent_run_post_nothing(tmp_path: Path) -> None:
    """Between revisits, a check without new leads costs nothing, even while people keep talking to the agent."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$talk", [], created_at=int(time.time()) - 60)
    context = _context(config, paths, tmp_path)
    _finish(check_exploration(context), config)
    _seed(config, paths, "!room:x:$more", [], created_at=int(time.time()) + 60)

    assert check_exploration(context) is None


def test_revisit_runs_without_leads_once_due(tmp_path: Path) -> None:
    """After revisit_days, exploration runs again to catch changes in known sources while people use the agent."""
    config, paths = _config(revisit_days=2), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    _seed(config, paths, "!room:x:$a", [], created_at=int(time.time()) - 60)
    _finish(check_exploration(context), config)
    _seed(config, paths, "!room:x:$b", [], created_at=int(time.time()) + DAY)

    with patch.object(exploration.time, "time", return_value=time.time() + 3 * DAY):
        assert check_exploration(context) is not None


def test_an_agent_nobody_talks_to_is_not_explored(tmp_path: Path) -> None:
    """Without any conversation, or none since the last run, a revisit costs nothing, such as for an idle requester."""
    config, paths = _config(revisit_days=2), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    assert check_exploration(context) is None

    _seed(config, paths, "!room:x:$a", [], created_at=int(time.time()) - 60)
    _finish(check_exploration(context), config)

    with patch.object(exploration.time, "time", return_value=time.time() + 3 * DAY):
        assert check_exploration(context) is None


def test_new_leads_start_a_run_before_the_revisit(tmp_path: Path) -> None:
    """A conversation that used a tool since the last run is a lead worth following."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$talk", [], created_at=int(time.time()) - 60)
    context = _context(config, paths, tmp_path)
    _finish(check_exploration(context), config)
    _seed(config, paths, "!room:x:$t1", [_tool("get_repository", repo="acme/atlas")], created_at=int(time.time()) + 5)

    with patch.object(exploration.time, "time", return_value=time.time() + 10):
        ask = check_exploration(context)

    assert ask is not None
    assert 'get_repository({"repo": "acme/atlas"})' in ask.text


def test_leads_are_distinct_counted_newest_first_and_capped(tmp_path: Path) -> None:
    """Repeated calls collapse into one counted lead, the newest lead comes first, and the list is capped."""
    config, paths = _config(max_leads=2), _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=100)
    _seed(config, paths, "!room:x:$b", [_tool("get_repository", repo="acme/atlas")], created_at=200)
    _seed(config, paths, "!room:x:$c", [_tool("list_issues", repo="acme/beacon")], created_at=300)
    _seed(config, paths, "!room:x:$d", [_tool("search", query="atlas")], created_at=50)

    leads, _ = _collect_leads(_context(config, paths, tmp_path), since=0, limit=2)

    assert [(lead.function, lead.count, lead.last_seen) for lead in leads] == [
        ("list_issues", 1, 300),
        ("get_repository", 2, 200),
    ]


def test_leads_skip_graph_lookups_and_exploration_threads(tmp_path: Path) -> None:
    """The graph's own lookups and exploration's own calls are not leads."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("search_graph", query="atlas")], created_at=100)
    _seed(
        config,
        paths,
        "!room:x:$b",
        [_tool("get_repository", repo="acme/atlas")],
        created_at=200,
        hook_source="automation/exploration",
    )

    assert _collect_leads(_context(config, paths, tmp_path), since=0, limit=10)[0] == []


def test_lead_arguments_are_redacted(tmp_path: Path) -> None:
    """Credential-like arguments never reach the exploration room."""
    config, paths = _config(), _paths(tmp_path)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    _seed(config, paths, "!room:x:$a", [_tool("call_api", token=token)], created_at=100)

    [lead], _ = _collect_leads(_context(config, paths, tmp_path), since=0, limit=10)

    assert token not in lead.arguments


def test_follow_up_resolves_the_thread_and_schedules_ingestion(tmp_path: Path) -> None:
    """When the run ends, the thread is resolved and its tool results are ingested soon after."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$talk", [], created_at=int(time.time()) - 60)
    ask = check_exploration(_context(config, paths, tmp_path))
    assert ask is not None
    assert ask.then is not None

    done = ask.then(config, "$thread", False)

    assert isinstance(done, Done)
    assert done.resolve == ("$thread",)
    assert done.on_loop is not None


def test_lead_arguments_mention_nobody(tmp_path: Path) -> None:
    """A Matrix ID in a lead must not ping that person or wake that agent when the prompt is posted."""
    config, paths = _config(), _paths(tmp_path)
    _seed(
        config,
        paths,
        "!room:x:$a",
        [_tool("send_message", text="@research then -@ops .@qa +@dev, cc bob@example.org")],
        created_at=int(time.time()),
    )

    ask = check_exploration(_context(config, paths, tmp_path))

    assert ask is not None
    lead = next(line for line in ask.text.splitlines() if line.startswith("- send_message("))
    assert "@" not in lead
    assert "research then -\uff20ops .\uff20qa +\uff20dev, cc bob\uff20example.org" in lead


def test_old_runs_of_an_updated_conversation_are_not_leads(tmp_path: Path) -> None:
    """A text-only reply in an old thread does not bring back the thread's earlier tool calls."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=100, updated_at=200)

    assert _collect_leads(_context(config, paths, tmp_path), since=150, limit=10) == ([], True)


def test_a_prompt_that_was_never_answered_keeps_its_leads(tmp_path: Path) -> None:
    """The next check offers the same leads again until a run ends."""
    config, paths = _config(), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=int(time.time()) - 5)

    first = check_exploration(context)
    second = check_exploration(context)

    assert first is not None
    assert second is not None
    assert first.text == second.text


def test_an_unreadable_conversation_does_not_stop_exploration(tmp_path: Path) -> None:
    """One conversation whose turns cannot be read is skipped, and the others still give leads."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$bad", [_tool("get_repository", repo="acme/bad")], created_at=100)
    _seed(config, paths, "!room:x:$good", [_tool("get_repository", repo="acme/atlas")], created_at=200)
    real = exploration.session_runs

    def runs(storage: object, session_id: str) -> list[RunOutput]:
        if session_id == "!room:x:$bad":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    with patch.object(exploration, "session_runs", runs):
        leads, _ = _collect_leads(_context(config, paths, tmp_path), since=0, limit=10)

    assert [lead.arguments for lead in leads] == ['{"repo": "acme/atlas"}']
