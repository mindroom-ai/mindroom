"""Tests for background-script capability and approval policy."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function

import mindroom.agents as agents_module
import mindroom.tool_system.plugins as plugins_module
import mindroom.tools  # noqa: F401
from mindroom.approval.tool_approval import _matching_tool_approval_rule, tool_may_require_approval
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.message_target import MessageTarget
from mindroom.script_runs.models import ScriptToolGrant
from mindroom.script_runs.policy import (
    resolve_current_script_tool,
    resolve_script_launch_grants,
    resolve_script_launch_toolkit_names,
)
from mindroom.tool_system import agno_toolkits
from mindroom.tool_system.automation_approval import build_automation_approval_config
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    GENERATION_PROBE_TOOL,
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    publish_test_plugins,
    runtime_paths_for,
    test_runtime_paths,
    write_generation_probe_plugin,
)
from tests.test_dynamic_toolkits import _egress_broker_env
from tests.test_dynamic_toolkits import _runtime_paths as _toolkit_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _context_for_config(
    tmp_path: Path,
    config: Config,
    runtime_paths: RuntimePaths | None = None,
) -> ToolRuntimeContext:
    bound_config = bind_runtime_paths(config, runtime_paths or test_runtime_paths(tmp_path))
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=SimpleNamespace(),
        config=bound_config,
        runtime_paths=runtime_paths_for(bound_config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def test_launch_grants_resolve_defaults_implied_tools_and_function_filter(tmp_path: Path) -> None:
    """The snapshot uses the ordinary resolved toolkit surface and its final function filter."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["matrix_message", "dynamic_workflow"],
                ),
            },
            defaults=DefaultsConfig(tools=["calculator"]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )
    context = replace(
        context,
        tool_function_filter=lambda function: function.name in {"matrix_message", "get_attachment", "add"},
    )

    grants = resolve_script_launch_grants(context)

    assert grants == (
        ScriptToolGrant("matrix_message", "matrix_message"),
        ScriptToolGrant("calculator", "add"),
        ScriptToolGrant("attachments", "get_attachment"),
    )
    assert all(grant.toolkit_name != "dynamic_workflow" for grant in grants)


def _echo(text: str) -> str:
    return text


def _context_captured_before_the_probe_broke(tmp_path: Path) -> ToolRuntimeContext:
    """Return a context that captured the generation probe's plugin before a publication in which it no longer loads."""
    paths = test_runtime_paths(tmp_path)
    plugin_root = paths.config_dir / "plugins" / "generation-probe"
    write_generation_probe_plugin(plugin_root, 1)
    config = Config(
        agents={"general": AgentConfig(display_name="General", tools=[GENERATION_PROBE_TOOL])},
        defaults=DefaultsConfig(tools=[]),
        plugins=["./plugins/generation-probe"],
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )
    publish_test_plugins(bind_runtime_paths(config, paths), paths)
    context = _context_for_config(tmp_path, config, paths)
    (plugin_root / "tools.py").write_text(
        "raise RuntimeError('broken after the context captured it')\n",
        encoding="utf-8",
    )
    plugins_module.publish_plugins(plugins_module.prepare_plugins(context.config, paths))
    return context


def test_launch_grants_come_from_the_plugins_the_turn_captured(tmp_path: Path) -> None:
    """A publication after the turn captured its plugins leaves the script's grants on the turn's generation."""
    context = _context_captured_before_the_probe_broke(tmp_path)

    assert resolve_script_launch_grants(context) == (ScriptToolGrant(GENERATION_PROBE_TOOL, "which_generation"),)


def test_launch_and_call_filters_see_the_toolkit_each_function_is_granted_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context's filter reads the authored toolkit of each granted function, not Agno's name for the toolkit."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )
    context = replace(
        context,
        tool_function_filter=lambda function: (function.toolkit, function.name) == ("calculator", "echo"),
    )

    def build_toolkit(_tool_name: str, **_kwargs: object) -> Toolkit:
        # Agno's own name for the toolkit differs from the configured one the grant names.
        return Toolkit(name="arithmetic", tools=[Function(name="echo", entrypoint=_echo)])

    monkeypatch.setattr(agents_module, "build_agent_toolkit", build_toolkit)

    assert resolve_script_launch_grants(context) == (ScriptToolGrant("calculator", "echo"),)
    assert resolve_current_script_tool(context, ScriptToolGrant("calculator", "echo")) is not None


def test_script_filters_see_an_implied_tools_function_under_the_authored_toolkit(tmp_path: Path) -> None:
    """An implied tool's functions name the authored tool that implies it, as an agent's build offers them."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["matrix_message"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )
    context = replace(
        context,
        tool_function_filter=lambda function: (function.toolkit, function.name) == ("matrix_message", "get_attachment"),
    )

    assert resolve_script_launch_grants(context) == (ScriptToolGrant("attachments", "get_attachment"),)
    assert resolve_current_script_tool(context, ScriptToolGrant("attachments", "get_attachment")) is not None


def test_launch_grants_skip_a_refused_function_the_filter_leaves_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A function setting a field MindRoom refuses costs the grants nothing when the context's filter drops it."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )
    context = replace(context, tool_function_filter=lambda function: function.name != "hooked")

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        hooked = Function(name="hooked", entrypoint=_echo, pre_hook=_echo)
        return Toolkit(name=tool_name, tools=[hooked, Function(name="echo", entrypoint=_echo)])

    monkeypatch.setattr(agents_module, "build_agent_toolkit", build_toolkit)

    assert resolve_script_launch_grants(context) == (ScriptToolGrant("calculator", "echo"),)


@pytest.mark.parametrize(
    "tools",
    [
        [{"shell": {"enable_run_shell_command": False}}, "openclaw_compat"],
        ["openclaw_compat", {"shell": {"enable_run_shell_command": False}}],
    ],
)
def test_launch_grants_preserve_concrete_overrides_over_preset_in_any_order(
    tmp_path: Path,
    tools: list[object],
) -> None:
    """A directly authored concrete toolkit owns its preset-expanded child in either order."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=tools)},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert ScriptToolGrant("coding", "read_file") in grants
    assert all(grant.toolkit_name != "shell" for grant in grants)


def test_launch_grants_exclude_stateful_browser_toolkit(tmp_path: Path) -> None:
    """Background calls reject browser state that cannot survive per-call toolkit rebuilding."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["browser"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert all(grant.toolkit_name != "browser" for grant in grants)


@pytest.mark.parametrize("broker_on", [False, True])
def test_launch_toolkits_include_egress_credentials_only_while_the_broker_is_on(
    tmp_path: Path,
    *,
    broker_on: bool,
) -> None:
    """With the broker off a background script is not offered the egress credentials tool its agent lists."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator", "egress_credentials"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        _toolkit_runtime_paths(tmp_path, **_egress_broker_env(broker_on=broker_on)),
    )

    names = resolve_script_launch_toolkit_names(context)

    assert "calculator" in names
    assert ("egress_credentials" in names) is broker_on


@pytest.mark.parametrize("restricted", ["agent_manager", "contacts", "skill_manage"])
def test_launch_grants_exclude_chat_only_tools(tmp_path: Path, restricted: str) -> None:
    """Like self_config, these stay with the agent's chat even when the agent lists them."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator", restricted])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert ScriptToolGrant("calculator", "add") in grants
    assert all(grant.toolkit_name != restricted for grant in grants)


def test_launch_grants_exclude_claude_agent(tmp_path: Path) -> None:
    """Script calls have no conversation to charge, so claude_agent stays with the agent's chat."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator", "claude_agent"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert ScriptToolGrant("calculator", "add") in grants
    assert all(grant.toolkit_name != "claude_agent" for grant in grants)


def test_launch_grants_exclude_slack(tmp_path: Path) -> None:
    """A script's calls rebuild the tool context without the turn's hook source, so Slack writes stay in chat."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator", "slack"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert ScriptToolGrant("calculator", "add") in grants
    assert all(grant.toolkit_name != "slack" for grant in grants)
    assert "slack" not in resolve_script_launch_toolkit_names(context)
    assert resolve_current_script_tool(context, ScriptToolGrant("slack", "slack_post")) is None


def test_launch_grants_leave_out_a_function_that_can_stop_the_run(tmp_path: Path) -> None:
    """A script's call answers the script, never the run, so the model switch is no call a script can make."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["thread_model"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )

    grants = resolve_script_launch_grants(context)

    assert ScriptToolGrant("thread_model", "get_thread_model") in grants
    assert ScriptToolGrant("thread_model", "switch_thread_model") not in grants
    assert resolve_current_script_tool(context, ScriptToolGrant("thread_model", "switch_thread_model")) is None


def test_requested_toolkit_is_revoked_when_the_agent_is_removed(tmp_path: Path) -> None:
    """Hot reload removals revoke grants without consulting the launch config again."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )
    removed = bind_runtime_paths(
        Config(
            agents={},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config_provider=lambda: removed)

    assert resolve_current_script_tool(context, ScriptToolGrant("calculator", "add")) is None


def test_requested_toolkit_returns_the_granted_live_function(tmp_path: Path) -> None:
    """The broker executes the one live toolkit that still exposes its launch grant."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )
    context = replace(context, tool_function_filter=lambda function: function.name == "add")

    resolved = resolve_current_script_tool(context, ScriptToolGrant("calculator", "add"))

    assert resolved is not None
    assert resolved.function.name == "add"


def test_a_requested_toolkit_comes_from_the_plugins_the_call_s_context_captured(tmp_path: Path) -> None:
    """A publication after the call's context captured its plugins leaves its toolkit on the generation of its hooks."""
    context = _context_captured_before_the_probe_broke(tmp_path)

    resolved = resolve_current_script_tool(context, ScriptToolGrant(GENERATION_PROBE_TOOL, "which_generation"))

    assert resolved is not None
    assert resolved.toolkit.name == GENERATION_PROBE_TOOL


def test_requested_toolkit_builds_only_the_granted_toolkit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatching one grant must not construct unrelated eligible toolkits."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator", "website"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
    )
    built: list[str] = []
    original_build = agents_module.build_agent_toolkit

    def recording_build(tool_name: str, **kwargs: object) -> Toolkit | None:
        built.append(tool_name)
        return original_build(tool_name, **kwargs)

    monkeypatch.setattr(agents_module, "build_agent_toolkit", recording_build)

    assert resolve_current_script_tool(context, ScriptToolGrant("calculator", "add")) is not None
    assert built == ["calculator"]


def test_a_call_adapts_only_its_granted_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call checks its one function, so a large toolkit costs a call no more than a small one."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )
    adapted: list[str] = []
    adapt = agno_toolkits._adapt

    def counting_adapt(function: Function, finishing: object) -> object:
        adapted.append(function.name)
        return adapt(function, finishing)

    monkeypatch.setattr(agno_toolkits, "_adapt", counting_adapt)

    resolved = resolve_current_script_tool(context, ScriptToolGrant("calculator", "add"))

    assert resolved is not None
    assert adapted == ["add"]
    assert (resolved.function.name, resolved.function.toolkit, resolved.function.registered_toolkit) == (
        "add",
        "calculator",
        "calculator",
    )


def test_background_approval_overlay_never_preapproves_system_mutation() -> None:
    """Wildcard automation preapproval excludes system-mutating toolkit owners."""
    config = Config.model_validate(
        {
            "tool_approval": {
                "rules": [{"match": "update_config", "action": "require_approval"}],
            },
        },
    )
    function_owners = {
        "read_url": frozenset({"website"}),
        "update_config": frozenset({"config_manager"}),
    }

    resolved = build_automation_approval_config(
        config,
        function_owners=function_owners,
        preapproved_toolkits=frozenset({"*"}),
        never_preapprove_toolkits=frozenset({"claude_agent", "config_manager", "scheduler"}),
    )

    read_rule = _matching_tool_approval_rule(resolved, "read_url")
    update_rule = _matching_tool_approval_rule(resolved, "update_config")
    assert read_rule is not None
    assert update_rule is not None
    assert read_rule.action == "auto_approve"
    assert update_rule.action == "require_approval"


def test_background_approval_overlay_keeps_colliding_owner_gated() -> None:
    """A bare function collision cannot leak one toolkit's preapproval to another owner."""
    config = Config()
    function_owners = {
        "read_file": frozenset({"python", "file"}),
        "run_python_code": frozenset({"python"}),
    }

    resolved = build_automation_approval_config(
        config,
        function_owners=function_owners,
        preapproved_toolkits=frozenset({"python"}),
        never_preapprove_toolkits=frozenset(),
    )

    assert tool_may_require_approval(resolved, "read_file") is True
    assert tool_may_require_approval(resolved, "run_python_code") is False
