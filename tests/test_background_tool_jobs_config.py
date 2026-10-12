"""Startup opt-in and passive recovery for generic tool jobs."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from functools import partial
from typing import TYPE_CHECKING

import pytest
import yaml
from agno.models.response import ModelResponse
from pydantic import ValidationError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import (
    get_background_runtime,
)
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, pending_background_tool_jobs_restart
from tests.conftest import test_runtime_paths
from tests.delegation_helpers import DelegationModel
from tests.identity_helpers import persist_entity_accounts
from tests.test_config_lifecycle import _make_lifecycle
from tests.tool_job_helpers import (
    completed_delegation_job,
    tool_job_journal,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.parametrize("exclusions", [None, [], ["shell", "native_plugin"]])
def test_background_job_yaml_round_trip(exclusions: list[str] | None) -> None:
    """The public mapping preserves explicit toolkit exclusions and the shell default."""
    settings: dict[str, object] = {"enabled": True}
    if exclusions is not None:
        settings["exclude_toolkits"] = exclusions
    config = Config.model_validate(yaml.safe_load(yaml.safe_dump({"background_tool_jobs": settings})))
    assert config.background_tool_jobs.enabled
    assert config.background_tool_jobs.exclude_toolkits == (["shell"] if exclusions is None else exclusions)
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
    """Ordinary saved runs omit job authority, including after an ignored config toggle."""
    config = Config(
        agents={"lead": AgentConfig(display_name="Lead", tools=[], learning=False)},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
    )
    config.memory.backend = "none"
    config.defaults.tools = []
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    instance = pin_background_tool_jobs(config, paths)
    config.background_tool_jobs.enabled = not enabled
    model = DelegationModel(id="test", responses=[ModelResponse(content="done")])
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    owner = completed_delegation_job().owner
    storage = create_session_storage("lead", config, paths, owner)
    try:
        agent = create_agent(
            "lead",
            config,
            paths,
            owner,
            session_id=owner.session_id,
            history_storage=storage,
            include_interactive_questions=False,
        )
        response = await agent.arun("hello", session_id=owner.session_id, user_id=owner.requester_id)
        saved = storage.get_run(response.run_id)
        assert "mindroom_tool_authority" not in (saved.metadata or {})
        assert ("mindroom_tool_authority" in vars(agent)) is enabled
    finally:
        storage.close()
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
    assert "wait_timeout" not in instructions + description
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


@pytest.mark.parametrize("enabled", [False, True])
def test_agent_installs_job_adapters_only_when_enabled(tmp_path: Path, enabled: bool) -> None:
    """Each constructed provider follows the effective feature setting."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
        agents={"lead": AgentConfig(display_name="Lead", tools=[])},
        models={"default": ModelConfig(provider="ollama", id="test")},
    )
    config.memory.backend = "none"
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    agent = create_agent("lead", config, paths, execution_identity=None, persist_runtime_state=False)
    assert agent.model is not None
    assert bool(vars(agent.model).get("_mindroom_tool_jobs")) is enabled


def test_disabled_construction_leaves_sdk_bindings_unchanged_in_fresh_process(tmp_path: Path) -> None:
    """An earlier enabled test cannot conceal accidental SDK installation in disabled mode."""
    script = textwrap.dedent("""
        import asyncio
        import sys
        from pathlib import Path
        from agno.agent import _init as agent_init
        from agno.team import _init as team_init

        names = ("connect_connectable_tools", "disconnect_connectable_tools", "connect_mcp_tools", "disconnect_mcp_tools")
        bindings = [(module, prefix + name, vars(module)[prefix + name])
                    for module, prefix in ((agent_init, ""), (team_init, "_")) for name in names]
        from mindroom.agents import create_agent
        from mindroom.config.agent import AgentConfig
        from mindroom.config.main import Config
        from mindroom.config.models import ModelConfig
        from mindroom.tool_jobs import agno_compat_execution
        from tests.conftest import test_runtime_paths
        from tests.identity_helpers import persist_entity_accounts

        config = Config(
            agents={"lead": AgentConfig(display_name="Lead", tools=["calculator"], learning=False)},
            models={"default": ModelConfig(provider="synthetic", id="lorem-ipsum", extra_kwargs={
                "chars_per_second": 0, "tool_call_probability": 0, "min_response_chars": 32, "max_response_chars": 32,
            })},
        )
        config.memory.backend = "none"
        config.defaults.tools = []
        paths = test_runtime_paths(Path(sys.argv[1]))
        persist_entity_accounts(config, paths)
        agent = create_agent("lead", config, paths, execution_identity=None, persist_runtime_state=False)
        response = asyncio.run(agent.arun("Reply normally"))
        assert response.content
        functions = [function for toolkit in agent.tools for function in toolkit.get_async_functions().values()]
        assert any(function.name == "add" for function in functions)
        assert all(function.name != "job" for function in functions)
        assert all("wait_timeout" not in function.parameters.get("properties", {}) for function in functions)
        assert not vars(agent.model).get("_mindroom_tool_jobs")
        assert not agno_compat_execution._SDK_BINDINGS_INSTALLED
        assert all(vars(module)[name] is original for module, name, original in bindings)
    """)
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
