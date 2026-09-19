"""SDK learning keeps its native owner inside managed response execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import pytest
from agno.learn.stores.user_memory import UserMemoryStore
from agno.models.response import ModelResponse

from mindroom.agent_storage import get_agent_runtime_state_dbs
from mindroom.agents import create_agent
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import tool_execution_identity
from tests.history_helpers import RecordingModel
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_delegation_execution import _call
from tests.test_subagent_runtime import _config, _delivery_coordinator, _job

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.models.message import Message

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@dataclass
class _LearningModel(RecordingModel):
    """Choose real learning tools through the SDK's normal model response loop."""

    before_extract: Callable[[], None] | None = None

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = cast("list[Message]", kwargs["messages"])
        functions = cast("list[dict]", kwargs.get("tools") or [])
        names = {function["function"]["name"] for function in functions}
        if messages[-1].role == "tool":
            return ModelResponse(content="Saved the preference.")
        if "add_memory" in names:
            assert all("wait_timeout" not in function["function"]["parameters"]["properties"] for function in functions)
            if self.before_extract is not None:
                self.before_extract()
            return ModelResponse(tool_calls=[_call("add_memory", "extract", memory="User prefers jasmine tea.")])
        if "update_user_memory" in names:
            return ModelResponse(tool_calls=[_call("update_user_memory", "remember", task="User prefers jasmine tea.")])
        return ModelResponse(content="I will remember your preference.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("managed", "mode", "revoke"),
    [
        (False, "always", False),
        (True, "always", False),
        (False, "agentic", False),
        (True, "agentic", False),
        (True, "agentic", True),
    ],
)
@pytest.mark.parametrize("scope", [None, "user"])
async def test_learning_persists_with_background_jobs_and_scoped_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    mode: Literal["always", "agentic"],
    scope: Literal["user"] | None,
    revoke: bool,
) -> None:
    """Automatic and explicitly requested learning write the real requester-scoped store."""
    config = _config(tmp_path)
    config.background_tool_jobs.enabled = managed
    config.memory.backend = "none"
    config.defaults.tools = []
    config.agents["lead"].delegate_to = []
    config.agents["lead"].learning = True
    config.agents["lead"].learning_mode = mode
    config.agents["lead"].worker_scope = scope
    coordinator = _delivery_coordinator(tmp_path, config)
    paths, owner = coordinator.runtime_paths, _job().owner
    entity_ids(config, paths)

    def withdraw_learning() -> None:
        config.agents["lead"].learning = False

    model = _LearningModel(id="test", before_extract=withdraw_learning if revoke else None)
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    agent = create_agent("lead", config, paths, owner, session_id=owner.session_id)
    try:
        await coordinator.sync()
        async with execution_resources():
            with (
                tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)),
                tool_execution_identity(owner),
            ):
                await agent.arun(
                    "Remember that I prefer jasmine tea.",
                    session_id=owner.session_id,
                    user_id=owner.requester_id,
                )
        machine = agent.learning_machine
        assert machine is not None
        store = machine.user_memory_store
        assert isinstance(store, UserMemoryStore)
        saved = await store.aget(user_id=owner.requester_id)
        if revoke:
            assert saved is None, "Internal extraction must recheck the outer job's current authority"
        else:
            assert saved is not None, "Learning must survive managed model instrumentation"
            assert [memory["content"] for memory in saved.memories] == ["User prefers jasmine tea."]
        assert await store.aget(user_id="@other:localhost") is None
        if managed and not revoke:
            jobs = await coordinator.runtime.list_jobs(owner=owner, depth=0)
            assert [job.tool_name for job in jobs] == (["update_user_memory"] if mode == "agentic" else [])
    finally:
        await coordinator.stop()
        for db in get_agent_runtime_state_dbs(agent):
            if db is not None:
                db.close()
