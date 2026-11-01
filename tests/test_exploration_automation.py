"""The exploration automation runs when there are leads or a revisit is due, and lists what conversations touched."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from mindroom.automations import exploration
from mindroom.automations.exploration import _collect_leads, check_exploration
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.contacts.store import update_contacts
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session, test_runtime_paths
from tests.contacts.test_store import _record as _contact_record
from tests.test_generated_agents import CO_OWNER, _load, _personal, _with_template

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.automations.exploration import _Lead
    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore, LoggedRun

DAY = 86_400


def _config(*, graph_edits: bool = False, **entry: object) -> Config:
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=True,
                knowledge_graph_edits=graph_edits,
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


def _context(config: Config, paths: RuntimePaths, tmp_path: Path, *, agent: str = "mind") -> AutomationContext:
    (entry,) = config.resolve_agent(agent).automations
    return AutomationContext(
        agent_name=agent,
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
    agent: str = "mind",
) -> None:
    metadata: dict[str, object] = {"room_id": "!room:x", "thread_id": session_id.rsplit(":", 1)[-1]}
    if hook_source is not None:
        metadata[HOOK_SOURCE_RUN_METADATA_KEY] = hook_source
    run = RunRecord(
        run_id=f"run-{session_id}",
        session_id=session_id,
        agent_id=agent,
        created_at=created_at,
        status=RunStatus.completed,
        metadata=metadata,
        messages=[Message(role="user", content="q"), Message(role="assistant", content="a")],
        tools=tools,
    )
    storage = create_session_storage(agent, config, paths, execution_identity=None)
    seed_session(storage, ConversationSession(kind="agent", session_id=session_id, owner_id=agent, runs=[run]))
    # The conversation was last active when its turn was made, unless a later reply updated it.
    _set_updated_at(storage, session_id, updated_at or created_at)


def _set_updated_at(storage: ConversationStore, session_id: str, updated_at: int) -> None:
    with storage.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE {storage.names.sessions} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
            (updated_at, session_id),
        )


def _leads(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    *,
    since: int = 0,
    limit: int = 10,
) -> tuple[list[_Lead], bool, int]:
    return _collect_leads(_context(config, paths, tmp_path), since=since, read_through={}, log_end=None, limit=limit)


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

    leads, _, _ = _leads(config, paths, tmp_path, limit=2)

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

    assert _leads(config, paths, tmp_path)[0] == []


def test_tool_calls_in_a_contact_room_are_not_leads(tmp_path: Path) -> None:
    """The other side of a contact drove the tool calls in its room, so they never steer exploration."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"] |= {
        "knowledge_graph": True,
        "contacts": {},
        "automations": [{"name": "exploration", "room": "lobby"}],
    }
    data["knowledge_graph"] = {"model": "default"}
    config = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER)])
    paths = test_runtime_paths(tmp_path)
    contact = _contact_record(status="active", room_id="!contact:x")
    update_contacts(paths, lambda book: book.contacts.append(contact))
    contact_call = _tool("get_repository", repo="acme/secret")
    _seed(config, paths, "!contact:x:$talk", [contact_call], created_at=100, agent="mind_bob")
    _seed(config, paths, "!room:x:$t1", [_tool("get_repository", repo="acme/atlas")], created_at=200, agent="mind_bob")
    context = _context(config, paths, tmp_path, agent="mind_bob")

    leads, _, _ = _collect_leads(context, since=0, read_through={}, log_end=None, limit=10)

    assert [lead.arguments for lead in leads] == ['{"repo": "acme/atlas"}']


def test_lead_arguments_are_redacted(tmp_path: Path) -> None:
    """Credential-like arguments never reach the exploration room."""
    config, paths = _config(), _paths(tmp_path)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    _seed(config, paths, "!room:x:$a", [_tool("call_api", token=token)], created_at=100)

    [lead], _, _ = _leads(config, paths, tmp_path)

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


def test_an_agent_that_may_edit_its_graph_is_asked_to_fix_it(tmp_path: Path) -> None:
    """Only an agent allowed to edit its graph is asked to fix what it finds wrong there, and nothing else changes."""
    reader_paths, editor_paths = _paths(tmp_path / "reader"), _paths(tmp_path / "editor")
    reader, editor = _config(), _config(graph_edits=True)
    for config, paths in ((reader, reader_paths), (editor, editor_paths)):
        _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=int(time.time()))

    read_only = check_exploration(_context(reader, reader_paths, tmp_path / "reader"))
    fixing = check_exploration(_context(editor, editor_paths, tmp_path / "editor"))

    assert read_only is not None
    assert fixing is not None
    assert "4. " not in read_only.text
    assert "\nOnly read: never create, change, send, or delete anything.\n" in read_only.text
    assert "\n4. Fix the mistakes step 1 showed in your graph: " in fixing.text
    assert "\nOnly read: never create, change, send, or delete anything, apart from fixing your graph.\n" in fixing.text


def test_the_fix_step_follows_a_prompt_override(tmp_path: Path) -> None:
    """Operators can reword the fix step through the prompts block, as any other built-in prompt."""
    config = _config(graph_edits=True).model_copy(update={"prompts": {"EXPLORATION_FIX_STEP": "\n4. Tidy the graph."}})
    paths = _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=int(time.time()))

    ask = check_exploration(_context(config, paths, tmp_path))

    assert ask is not None
    assert "lacks.\n4. Tidy the graph.\n" in ask.text


def test_old_runs_of_an_updated_conversation_are_not_leads(tmp_path: Path) -> None:
    """A text-only reply in an old thread does not bring back the thread's earlier tool calls."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$a", [_tool("get_repository", repo="acme/atlas")], created_at=100, updated_at=200)

    assert _leads(config, paths, tmp_path, since=150)[:2] == ([], True)


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
    real = exploration.matrix_turns

    def runs(storage: ConversationStore, session_id: str) -> list[LoggedRun]:
        if session_id == "!room:x:$bad":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    with patch.object(exploration, "matrix_turns", runs):
        leads, _, _ = _leads(config, paths, tmp_path)

    assert [lead.arguments for lead in leads] == ['{"repo": "acme/atlas"}']


def test_a_turn_approved_after_a_check_is_a_lead_at_the_next(tmp_path: Path) -> None:
    """A turn that completes after its approval offers its tool calls once, though it was created before a check ran.

    It keeps its creation time, which is older than the check, so reading turns created since the check misses it.
    """
    config, paths = _config(), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    created_at = int(time.time()) - 50
    session_id = "!room:x:$a"
    run = RunRecord(
        run_id="run-asks",
        session_id=session_id,
        agent_id="mind",
        created_at=created_at,
        status=RunStatus.paused,
        metadata={"room_id": "!room:x", "thread_id": "$a"},
        messages=[Message(role="user", content="q"), Message(role="assistant", content="a")],
        tools=[_tool("get_repository", repo="acme/late")],
    )
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    session = seed_session(
        storage,
        ConversationSession(kind="agent", session_id=session_id, owner_id="mind", runs=[run]),
    )
    _finish(check_exploration(context), config)

    run.status = RunStatus.completed
    storage.save_run(session, run, record_usage=False)
    _set_updated_at(storage, session_id, int(time.time()) + 1)
    with patch.object(exploration.time, "time", return_value=time.time() + 5):
        ask = check_exploration(context)
        assert ask is not None
        assert "acme/late" in ask.text
        _finish(ask, config)
    with patch.object(exploration.time, "time", return_value=time.time() + 10):
        again = check_exploration(context)

    assert again is None or "acme/late" not in again.text


def test_a_first_turn_stored_after_a_check_is_a_lead_at_the_next(tmp_path: Path) -> None:
    """A new conversation whose first turn was created before a check and stored after it offers that turn's calls.

    A turn is stored when it completes, so the check that ran meanwhile never listed its conversation.
    """
    config, paths = _config(), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    now = int(time.time())
    _seed(config, paths, "!room:x:$seen", [_tool("get_repository", repo="acme/seen")], created_at=now - 60)
    _finish(check_exploration(context), config)
    _seed(
        config,
        paths,
        "!room:x:$late",
        [_tool("get_repository", repo="acme/late")],
        created_at=now - 30,
        updated_at=now + 1,
    )

    with patch.object(exploration.time, "time", return_value=time.time() + 5):
        ask = check_exploration(context)

    assert ask is not None
    assert "acme/late" in ask.text
    assert "acme/seen" not in ask.text


def test_a_state_file_without_places_offers_turns_created_since_the_check(tmp_path: Path) -> None:
    """A conversation the state names no place for starts with its turns created after the last check."""
    config, paths = _config(), _paths(tmp_path)
    context = _context(config, paths, tmp_path)
    checked_at = int(time.time()) - 100
    context.state_dir.mkdir(parents=True)
    (context.state_dir / "exploration.json").write_text(json.dumps({"last_run_at": checked_at}), encoding="utf-8")
    _seed(
        config,
        paths,
        "!room:x:$old",
        [_tool("get_repository", repo="acme/old")],
        created_at=checked_at - 50,
        updated_at=checked_at + 10,
    )
    _seed(config, paths, "!room:x:$new", [_tool("get_repository", repo="acme/new")], created_at=checked_at + 20)

    ask = check_exploration(context)

    assert ask is not None
    assert "acme/new" in ask.text
    assert "acme/old" not in ask.text
    _finish(ask, config)
    state = json.loads((context.state_dir / "exploration.json").read_text(encoding="utf-8"))
    assert state["sessions"] == {"!room:x:$old": 1, "!room:x:$new": 2}
