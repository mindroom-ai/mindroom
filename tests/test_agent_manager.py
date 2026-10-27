"""Tests for the agent_manager toolkit, which creates agents and lets owners change the agents generated for them."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import yaml

from mindroom.config.personal_agents import read_personal_agent_roster_file
from mindroom.custom_tools.agent_manager import AgentManagerTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, test_runtime_paths
from tests.test_generated_agents import CO_OWNER, OWNER, STRANGER, _config, _created, _load, _register

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_CONFIRMED = {"create_agent", "update_agent", "delete_agent"}
_MANAGED = {"agent_settings", "update_agent", "delete_agent"}


@contextmanager
def _as_requester(
    tool: AgentManagerTools,
    config: Config,
    paths: RuntimePaths,
    *,
    requester_id: str = OWNER,
    orchestrator: object | None = None,
) -> Iterator[None]:
    """Bind one live requester, and optionally a running orchestrator, for the toolkit's calls."""
    context = make_test_tool_runtime_context(
        agent_name=tool.agent_name,
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id="$request"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=orchestrator,
    )
    with tool_runtime_context(context):
        yield


def _created_agent_tool() -> AgentManagerTools:
    return AgentManagerTools("creator_research", can_create=False, generated=True)


@pytest.mark.parametrize(
    ("can_create", "generated", "offered"),
    [
        (True, False, {"create_agent"}),
        (False, True, _MANAGED),
        (True, True, _MANAGED | {"create_agent"}),
    ],
)
def test_functions_follow_the_flags_and_changes_need_confirmation(
    can_create: bool,
    generated: bool,
    offered: set[str],
) -> None:
    """A creator is offered create_agent and a generated agent its settings, update and delete; changes ask the owner."""
    functions = adapt_toolkit(AgentManagerTools("agent", can_create=can_create, generated=generated)).functions

    assert {function.name for function in functions} == offered
    assert {function.name for function in functions if function.requires_confirmation} == offered & _CONFIRMED


def test_update_agent_schema_lets_a_model_send_text_and_name_lists() -> None:
    """The offered schema types each change value, so a model is not led to send empty objects for text or lists."""
    function = next(
        function for function in adapt_toolkit(_created_agent_tool()).functions if function.name == "update_agent"
    )

    assert function.parameters["required"] == ["changes"]
    value_types = function.parameters["properties"]["changes"]["additionalProperties"]["anyOf"]
    assert {"type": "string"} in value_types
    assert {"type": "array", "items": {"type": "string"}} in value_types
    assert {"type": "null"} in value_types


@pytest.mark.asyncio
async def test_create_agent_registers_it_for_the_owner_and_requests_a_reload(tmp_path: Path) -> None:
    """A creator's call stores an agent for the requester, names its Matrix ID and asks the orchestrator to reload."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    tool = AgentManagerTools("creator", can_create=True, generated=False)
    orchestrator = MagicMock()

    with (
        _as_requester(tool, config, paths, orchestrator=orchestrator),
        patch(
            "mindroom.personal_agents.create_agent_user",
            new=_register(),
        ),
    ):
        result = await tool.create_agent("Research", "Research", "Finds things", tools=["shell"])

    user_id = "@mindroom_creator_research:localhost"
    assert user_id in result
    assert f"Invite {user_id} to a room to talk to it; it accepts invites from its owners." in result
    (entry,) = read_personal_agent_roster_file(paths).created
    assert (entry.agent_name, entry.owner, entry.edits.tools) == ("creator_research", OWNER, ["shell"])
    orchestrator.config_reload.request_reload.assert_called_once_with()


@pytest.mark.asyncio
async def test_create_agent_refusals_come_back_as_errors_without_a_reload(tmp_path: Path) -> None:
    """A tool the creator lacks is refused with the allowed names, nothing is stored and no reload is asked for."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    tool = AgentManagerTools("creator", can_create=True, generated=False)
    orchestrator = MagicMock()

    with _as_requester(tool, config, paths, orchestrator=orchestrator):
        result = await tool.create_agent("Research", "Research", "Finds things", tools=["browser"])

    assert result == "Error: Not among the tools this agent may use: browser; allowed: calculator, shell"
    assert read_personal_agent_roster_file(paths).created == []
    orchestrator.config_reload.request_reload.assert_not_called()


@pytest.mark.asyncio
async def test_an_owner_reads_updates_and_deletes_a_created_agent(tmp_path: Path) -> None:
    """The three calls reach the roster for the owner, and each change asks the orchestrator to reload."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    tool = _created_agent_tool()
    orchestrator = MagicMock()

    with _as_requester(tool, config, paths, orchestrator=orchestrator):
        settings = yaml.safe_load(await tool.agent_settings())
        updated = await tool.update_agent({"role": "Mine", "tools": ["calculator"]})
        edits = read_personal_agent_roster_file(paths).created[0].edits
        deleted = await tool.delete_agent()

    assert (settings["agent_name"], settings["allowed_tools"]) == ("creator_research", ["calculator", "shell"])
    assert updated == "Updated creator_research: role, tools."
    assert (edits.role, edits.tools) == ("Mine", ["calculator"])
    assert deleted == "Deleted creator_research."
    assert read_personal_agent_roster_file(paths).created == []
    assert orchestrator.config_reload.request_reload.call_count == 2


@pytest.mark.asyncio
async def test_a_change_without_an_orchestrator_is_still_stored(tmp_path: Path) -> None:
    """A detached context has no orchestrator to reload, and the change is stored all the same."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    tool = _created_agent_tool()

    with _as_requester(tool, config, paths):
        result = await tool.update_agent({"role": "Mine"})

    assert result == "Updated creator_research: role."
    assert read_personal_agent_roster_file(paths).created[0].edits.role == "Mine"


@pytest.mark.asyncio
async def test_an_empty_change_is_an_error_and_requests_no_reload(tmp_path: Path) -> None:
    """Sending no fields stores nothing, so it is not reported as an update and does not reload."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    tool = _created_agent_tool()
    orchestrator = MagicMock()

    with _as_requester(tool, config, paths, orchestrator=orchestrator):
        result = await tool.update_agent({})

    assert result == "Error: no changes given"
    orchestrator.config_reload.request_reload.assert_not_called()


@pytest.mark.asyncio
async def test_refused_and_invalid_changes_come_back_as_errors_without_a_reload(tmp_path: Path) -> None:
    """Anyone but an owner, an unknown field and a value that does not validate each return an error and store nothing."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    tool = _created_agent_tool()
    orchestrator = MagicMock()
    stored = read_personal_agent_roster_file(paths)

    with _as_requester(tool, config, paths, requester_id=STRANGER, orchestrator=orchestrator):
        results = [await tool.agent_settings(), await tool.update_agent({"role": "Spy"}), await tool.delete_agent()]
    with _as_requester(tool, config, paths, requester_id=CO_OWNER, orchestrator=orchestrator):
        results += [await tool.delete_agent(), await tool.update_agent({"model": "opus"})]
        results.append(await tool.update_agent({"display_name": ["not", "text"]}))

    assert all(result.startswith("Error: ") for result in results)
    assert results[3] == f"Error: Only {OWNER} may delete creator_research"
    assert results[4] == "Error: model cannot be changed; allowed: display_name, role, instructions, tools, co_owners"
    assert read_personal_agent_roster_file(paths) == stored
    orchestrator.config_reload.request_reload.assert_not_called()


@pytest.mark.asyncio
async def test_calls_without_a_runtime_context_fail_closed(tmp_path: Path) -> None:
    """With no live requester the toolkit refuses instead of guessing who is asking."""
    _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    tool = AgentManagerTools("creator_research", can_create=True, generated=True)

    results = [
        await tool.agent_settings(),
        await tool.update_agent({"role": "Mine"}),
        await tool.delete_agent(),
        await tool.create_agent("Research", "Research", "Finds things"),
    ]

    assert all(result.startswith("Error: ") for result in results)
    assert len(read_personal_agent_roster_file(paths).created) == 1
