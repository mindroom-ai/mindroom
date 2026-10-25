"""Startup opt-in and passive recovery for generic tool jobs."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent, team_background_jobs
from mindroom.ai import _with_background_tool_jobs
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import (
    get_background_runtime,
)
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, pending_background_tool_jobs_restart
from tests.conftest import make_turn_context, test_runtime_paths
from tests.delegation_helpers import ScriptedAgents
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import persist_entity_accounts
from tests.test_config_lifecycle import _make_lifecycle
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_owner,
    run_built_turn,
    tool_job_journal,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.parametrize("exclusions", [None, [], ["shell", "native_plugin"]])
def test_background_job_yaml_round_trip(exclusions: list[str] | None) -> None:
    """The public mapping preserves explicit toolkit exclusions and the empty default."""
    settings: dict[str, object] = {"enabled": True}
    if exclusions is not None:
        settings["exclude_toolkits"] = exclusions
    config = Config.model_validate(yaml.safe_load(yaml.safe_dump({"background_tool_jobs": settings})))
    assert config.background_tool_jobs.enabled
    assert config.background_tool_jobs.exclude_toolkits == ([] if exclusions is None else exclusions)
    assert Config.model_validate(config.model_dump(mode="json")).background_tool_jobs == config.background_tool_jobs


@pytest.mark.parametrize("settings", [True, {"exclude_toolkits": "shell"}, {"exclude_toolkit": ["shell"]}])
def test_background_job_yaml_rejects_invalid_settings(settings: object) -> None:
    """Reject the old scalar and malformed lists instead of silently ignoring policy."""
    with pytest.raises(ValidationError):
        Config.model_validate({"background_tool_jobs": settings})


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_authority_stays_out_of_saved_metadata_with_startup_feature_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """A run's metadata omits job authority; whether calls can be jobs follows the startup setting, not a later toggle."""
    config = Config(
        agents={"lead": AgentConfig(display_name="Lead", tools=[])},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
    )
    config.memory.backend = "none"
    config.defaults.tools = []
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    instance = pin_background_tool_jobs(config, paths)
    config.background_tool_jobs.enabled = not enabled
    ScriptedAgents({"lead": ScriptedModel([answer("done")])}).install(monkeypatch)
    owner = completed_delegation_job().owner
    try:
        build = create_agent("lead", config, paths, owner, include_interactive_questions=False)
        record = await run_built_turn(build, config=config, runtime_paths=paths, prompt="hello")
        assert "mindroom_tool_authority" not in (record.metadata or {})
        assert (build.tool_jobs is not None) is enabled
    finally:
        release_background_tool_jobs(paths, instance)


@pytest.mark.asyncio
async def test_default_startup_does_not_create_job_runtime(tmp_path: Path) -> None:
    """Default startup leaves job persistence and scheduling absent."""
    paths = test_runtime_paths(tmp_path)
    config = Config()
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, paths.storage_root),
    )
    try:
        await coordinator.sync()
        assert get_background_runtime(paths) is None
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_recreated_coordinator_pins_its_own_setting(tmp_path: Path) -> None:
    """A coordinator whose stop never ran, as when shutdown skips it for pending responses, cannot decide the next one's mode.

    Its late stop withdraws only the instance it pinned, never the one that replaced it.
    """
    paths = test_runtime_paths(tmp_path)
    disabled = Config()
    enabled = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=True))
    journal = partial(tool_job_journal, paths.storage_root)
    stale = ToolJobRuntimeCoordinator(paths, lambda: disabled, lambda _: None, AgentReplyMembershipIndex(), journal)
    await stale.initialize()
    recreated = ToolJobRuntimeCoordinator(paths, lambda: enabled, lambda _: None, AgentReplyMembershipIndex(), journal)
    try:
        await recreated.sync()
        # The authored setting is disabled, so only the recreated coordinator's pin can report it enabled.
        assert background_tool_jobs_enabled(disabled, paths)
        assert get_background_runtime(paths) is not None
        await stale.stop()
        assert background_tool_jobs_enabled(disabled, paths)
        assert get_background_runtime(paths) is not None
    finally:
        await recreated.stop()


def test_disabled_delegation_describes_only_available_tools(tmp_path: Path) -> None:
    """Default delegation must not promise a job API or reserved wait argument."""
    config = Config(agents={"lead": AgentConfig(display_name="Lead", delegate_to=["lead"])})
    toolkit = DelegateTools("lead", ["lead"], test_runtime_paths(tmp_path), config)
    instructions = toolkit.instructions or ""
    description = toolkit.async_functions["run_subagent"].description or ""
    assert "background_after" not in instructions + description
    assert "job(action=" not in instructions + description
    assert "continue_subagent" in instructions + description
    # The default prompt keeps main's foreground wording; background semantics live only in the opt-in addendum.
    assert "The caller waits for the child to finish; this is not background work." in instructions
    assert "The caller waits; this does not create a Matrix thread." in description
    assert "child tools that require approval pause both runs" in instructions


@pytest.mark.parametrize("initial", [False, True])
def test_reload_reports_restart_and_keeps_effective_mode(tmp_path: Path, initial: bool) -> None:
    """Reload can publish other settings without switching execution ownership."""
    paths = test_runtime_paths(tmp_path)
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=initial))
    instance = pin_background_tool_jobs(config, paths)
    try:
        changed = Config(
            background_tool_jobs=BackgroundToolJobsConfig(enabled=not initial),
            timezone="Europe/Amsterdam",
        )
        lifecycle = _make_lifecycle(tmp_path, current_config=config)
        lifecycle.record_applied(changed)
        assert background_tool_jobs_enabled(changed, paths) is initial
        assert lifecycle.status.status == "restart_required"
        lifecycle.record_applied(config)
        assert lifecycle.status.status == "applied"
    finally:
        release_background_tool_jobs(paths, instance)
    restarted = pin_background_tool_jobs(changed, paths)
    assert restarted.settings.enabled is not initial
    release_background_tool_jobs(paths, restarted)


def test_exclusion_order_is_not_an_execution_policy_change(tmp_path: Path) -> None:
    """Reordering toolkit exclusions does not claim a process restart is needed."""
    paths = test_runtime_paths(tmp_path)
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["shell", "plugin"]))
    instance = pin_background_tool_jobs(config, paths)
    try:
        config.background_tool_jobs.exclude_toolkits = ["plugin", "shell", "shell"]
        assert not pending_background_tool_jobs_restart(config, paths)
        config.background_tool_jobs.exclude_toolkits = ["plugin"]
        assert pending_background_tool_jobs_restart(config, paths)
    finally:
        release_background_tool_jobs(paths, instance)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_execution_scope_bypasses_owners_when_disabled(enabled: bool) -> None:
    """Both driver shapes run ordinary work without feature owners while disabled."""

    @owned_tool_execution(enabled=lambda: enabled)
    async def blocking() -> bool:
        return current_execution_resources() is not None

    @owned_tool_execution(enabled=lambda: enabled)
    async def streaming() -> AsyncIterator[bool]:
        yield current_execution_resources() is not None

    assert await blocking() is enabled
    assert [value async for value in streaming()] == [enabled]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_an_agent_admits_its_calls_as_jobs_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Each built agent follows the effective setting; while it is off, no definition offers a wait budget."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
        agents={"lead": AgentConfig(display_name="Lead", tools=["calculator"])},
        models={"default": ModelConfig(provider="ollama", id="test")},
    )
    config.memory.backend = "none"
    config.defaults.tools = []
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    model = ScriptedModel([answer("done")])
    # Without an execution identity, the agent's model is served by its configured model name.
    ScriptedAgents({"default": model}).install(monkeypatch)
    build = create_agent("lead", config, paths, execution_identity=None, persist_runtime_state=False)
    assert (build.tool_jobs is not None) is enabled
    if build.tool_jobs is not None:
        assert "add" in build.tool_jobs.tools
    await run_built_turn(build, config=config, runtime_paths=paths)
    [request] = model.requests
    assert "add" in {tool.name for tool in request.tools}
    assert all(tool.name != "job" for tool in request.tools)
    assert all("background_after" not in tool.parameters.get("properties", {}) for tool in request.tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_a_team_leader_gets_job_controls_only_when_enabled(tmp_path: Path, enabled: bool) -> None:
    """A team leader's job controls, like an agent's, follow the effective feature setting while the runtime runs."""
    paths = test_runtime_paths(tmp_path)
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled))
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda _: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, paths.storage_root),
    )
    try:
        await coordinator.sync()
        jobs = team_background_jobs(config, paths, job_owner())
        assert (jobs is not None) is enabled
        if jobs is not None:
            offered = {function.name: function for function in jobs.controls.functions}
            assert "job" in offered
            assert jobs.actor.team
            # The team describes each control it offers, by that function and the job toolkit, and nothing else.
            assert set(jobs.actor.tools) == set(offered)
            assert all(
                tool.function is offered[name] and tool.toolkit_name == "job" for name, tool in jobs.actor.tools.items()
            )
        # A team without a Matrix owner has no jobs to collect.
        assert team_background_jobs(config, paths, None) is None
    finally:
        await coordinator.stop()


@pytest.mark.parametrize(
    ("enabled", "voice_call", "expected"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_a_turn_runs_its_calls_as_jobs_only_with_jobs_on_and_outside_a_voice_call(
    tmp_path: Path,
    *,
    enabled: bool,
    voice_call: bool,
    expected: bool,
) -> None:
    """A voice call's tools finish inside its turn, since no reply owns their outcomes afterwards."""
    config = Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled))
    turn = replace(make_turn_context(entity_label="lead"), inline_tools=voice_call)
    assert _with_background_tool_jobs(turn, config, test_runtime_paths(tmp_path)).background_tool_jobs is expected
