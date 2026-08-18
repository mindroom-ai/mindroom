"""Tests for background-script capability and approval policy."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.message_target import MessageTarget
from mindroom.script_runs.models import ScriptToolGrant
from mindroom.script_runs.policy import (
    effective_script_grants,
    resolve_current_script_grants,
    resolve_script_launch_grants,
)
from mindroom.tool_approval import _matching_tool_approval_rule, tool_may_require_approval
from mindroom.tool_system.automation_approval import build_automation_approval_config
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _context_for_config(tmp_path: Path, config: Config) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    bound_config = bind_runtime_paths(config, runtime_paths)
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


def _toolkit(name: str, *function_names: str) -> Toolkit:
    toolkit = Toolkit(name=name)
    for function_name in function_names:
        toolkit.functions[function_name] = SimpleNamespace(name=function_name)
    return toolkit


def test_effective_grants_never_expand_after_launch() -> None:
    """A live tool addition cannot enlarge an existing run's launch snapshot."""
    launched = (
        ScriptToolGrant("website", "read_url"),
        ScriptToolGrant("matrix_message", "matrix_message"),
    )
    current = frozenset(
        {
            ScriptToolGrant("website", "read_url"),
            ScriptToolGrant("shell", "run_shell_command"),
        },
    )

    assert effective_script_grants(launched, current) == frozenset(
        {ScriptToolGrant("website", "read_url")},
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
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
    )
    context = replace(
        context,
        tool_function_filter=lambda function: function.name in {"matrix_message", "get_attachment", "add"},
    )

    grants = resolve_script_launch_grants(context)

    assert grants == (
        ScriptToolGrant("matrix_message", "matrix_message"),
        ScriptToolGrant("attachments", "get_attachment"),
        ScriptToolGrant("calculator", "add"),
    )
    assert all(grant.toolkit_name != "dynamic_workflow" for grant in grants)


def test_current_grants_use_live_config_and_agent_removal_revokes_surface(tmp_path: Path) -> None:
    """Hot reload removals revoke grants without consulting the launch config again."""
    context = _context_for_config(
        tmp_path,
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
    )
    removed = bind_runtime_paths(
        Config(
            agents={},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config_provider=lambda: removed)

    assert resolve_current_script_grants(context) == frozenset()


def test_background_approval_overlay_never_preapproves_system_mutation() -> None:
    """Wildcard automation preapproval excludes system-mutating toolkit owners."""
    config = Config.model_validate(
        {
            "tool_approval": {
                "rules": [{"match": "update_config", "action": "require_approval"}],
            },
        },
    )
    toolkits = {
        "website": _toolkit("website", "read_url"),
        "config_manager": _toolkit("config_manager", "update_config"),
    }

    resolved = build_automation_approval_config(
        config,
        toolkits_by_name=toolkits,
        preapproved_toolkits=frozenset({"*"}),
        never_preapprove_toolkits=frozenset({"config_manager", "scheduler", "subagents", "claude_agent"}),
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
    toolkits = {
        "python": _toolkit("python", "read_file", "run_python_code"),
        "file": _toolkit("file", "read_file"),
    }

    resolved = build_automation_approval_config(
        config,
        toolkits_by_name=toolkits,
        preapproved_toolkits=frozenset({"python"}),
        never_preapprove_toolkits=frozenset(),
    )

    assert tool_may_require_approval(resolved, "read_file") is True
    assert tool_may_require_approval(resolved, "run_python_code") is False
