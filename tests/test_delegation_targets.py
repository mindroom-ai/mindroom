"""Both delegation adapters read a call's arguments into its target through one resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.delegation.state import SubagentPersona
from mindroom.delegation.targets import resolve_follow_up_target, resolve_fresh_target
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild


def _config(tools: tuple[str, ...]) -> Config:
    return Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=list(tools), delegate_to=["leader"])},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        models={"default": ModelConfig(provider="openai", id="default-model")},
    )


def _retained(paths: RuntimePaths) -> DelegationChild:
    """A started self-copy whose persona names the `file` toolkit."""
    return prepare_child_turn(
        "leader",
        "leader",
        "Read.",
        owner=_identity(),
        config=_config(("file",)),
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        persona=SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",)),
        model=None,
        agent_mode="standard",
        previous=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"subagent_id": 7, "message": "Again"}, {"subagent_id": "a" * 32, "message": None}],
)
async def test_a_follow_up_with_arguments_of_the_wrong_type_is_refused(
    tmp_path: Path,
    args: dict[str, object],
) -> None:
    """A stored call whose arguments are not strings resolves to a refusal, never to a session lookup."""
    result = await resolve_follow_up_target(
        args,
        owner=_identity(),
        config=_config(("file",)),
        runtime_paths=_runtime_paths(tmp_path),
        depth=0,
    )

    assert result == "Cannot continue: subagent_id and message must be strings."


@pytest.mark.asyncio
async def test_a_resumed_follow_up_keeps_its_child_after_the_caller_lost_a_persona_tool(tmp_path: Path) -> None:
    """The follow-up refusal is for a fresh follow-up; a resumed call continues the child it already started."""
    paths = _runtime_paths(tmp_path)
    retained = _retained(paths)
    without_file = _config(())

    with tool_runtime_context(_delegate_runtime_context(without_file, paths, execution_identity=_identity())):
        target = await resolve_follow_up_target(
            {"subagent_id": retained.subagent_id, "message": "Again"},
            retained=retained,
            owner=_identity(),
            config=without_file,
            runtime_paths=paths,
            depth=0,
        )

    assert not isinstance(target, str), target
    assert (target.agent_name, target.task, target.previous_child) == ("leader", "Again", retained)


@pytest.mark.asyncio
async def test_a_resumed_follow_up_naming_another_subagent_fails(tmp_path: Path) -> None:
    """A stored call that names another subagent than the child it started is a corrupted wait, not a refusal."""
    paths = _runtime_paths(tmp_path)

    with pytest.raises(RuntimeError, match="no longer matches its retained requirement"):
        await resolve_follow_up_target(
            {"subagent_id": "b" * 32, "message": "Again"},
            retained=_retained(paths),
            owner=_identity(),
            config=_config(("file",)),
            runtime_paths=paths,
            depth=0,
        )


@pytest.mark.parametrize(
    ("args", "refusal"),
    [
        ({"task": "Read.", "minimal": "yes"}, "Cannot delegate: minimal must be a boolean or null."),
        ({"task": 3}, "Cannot delegate: task must be a string and agent_name must be a string or null."),
    ],
)
def test_a_fresh_call_with_arguments_of_the_wrong_type_is_refused(
    tmp_path: Path,
    args: dict[str, object],
    refusal: str,
) -> None:
    """A stored call whose arguments have the wrong types resolves to a refusal before any persona is read."""
    result = resolve_fresh_target(
        args,
        caller_name="leader",
        config=_config(("file",)),
        runtime_paths=_runtime_paths(tmp_path),
        depth=0,
        workspace_root=lambda: tmp_path,
        cap=None,
    )

    assert result == refusal
