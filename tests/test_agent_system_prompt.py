"""Focused unit tests for default agent system prompt cleanup, gating, and cache stability."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from mindroom.agent_cli.minimal_agent import minimal_context_documents
from mindroom.agent_loop.prompt import render_system_messages
from mindroom.agents import create_agent
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.delegation.state import SubagentPersona
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.provider import Capabilities
from mindroom.prompts import (
    OUTPUT_REDIRECT_PROMPT,
    PROMPT_DEFAULTS,
    SKILLS_TOOL_USAGE_PROMPT,
    WORKSPACE_SKILL_AUTHORING_PROMPT,
)
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, install_model_providers
from tests.fakes.scripted_model import ScriptedModel
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_modes import AgentMode
    from mindroom.tool_system.tool_function import ToolFunction

_LISTEN = {"tools": ["memory"], "listen": {"rooms": ["!lobby:localhost"], "tools": ["memory"]}}


def _runtime_paths(tmp_path: Path, process_env: dict[str, str] | None = None) -> RuntimePaths:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (config_path := tmp_path / "config.yaml").write_text("", encoding="utf-8")
    env = {"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": "", **(process_env or {})}
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)


def _dummy_knowledge() -> AgentKnowledge:
    return AgentKnowledge(
        sources=(KnowledgeSource(KnowledgeSourceDescription(base_id="docs", description="Docs"), MagicMock()),),
    )


def _build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_cfg: dict[str, Any] | None = None,
    prompts: dict[str, str] | None = None,
    approval_matches: tuple[str, ...] = (),
    process_env: dict[str, str] | None = None,
    native_tool_search: bool = False,
    execution_identity: ToolExecutionIdentity | None = None,
    knowledge: AgentKnowledge | None = None,
    outside_matrix: bool = False,
    supports_native_tool_approval: bool = True,
    tool_function_filter: Callable[[ToolFunction], bool] | None = None,
    agent_mode: AgentMode = "standard",
    persona: SubagentPersona | None = None,
) -> tuple[AgentBuild, RuntimePaths]:
    rp = _runtime_paths(tmp_path, process_env=process_env)
    payload: dict[str, Any] = {
        "agents": {"assistant": {"display_name": "Assistant", "role": "A helpful assistant.", **(agent_cfg or {})}},
        "defaults": {"tools": []},
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "timezone": "UTC",
    }
    if prompts is not None:
        payload["prompts"] = prompts
    if approval_matches:
        rules = [ApprovalRuleConfig(match=m, action="require_approval") for m in approval_matches]
        payload["tool_approval"] = ToolApprovalConfig(rules=rules).model_dump(exclude_none=True)
    config = bind_runtime_paths(Config.model_validate(payload), rp)
    caps = Capabilities(native_tool_search=native_tool_search)
    install_model_providers(monkeypatch, {"default": ScriptedModel([], capabilities=caps)})
    build = create_agent(
        "assistant",
        config,
        rp,
        execution_identity=execution_identity,
        knowledge=knowledge,
        model=turn_model(config, rp, "assistant"),
        plugins=config_plugins(config, rp),
        outside_matrix=outside_matrix,
        supports_native_tool_approval=supports_native_tool_approval,
        tool_function_filter=tool_function_filter,
        agent_mode=agent_mode,
        persona=persona,
    )
    return build, rp


def _write_skill(rp: RuntimePaths, name: str, description: str = "Test skill.") -> Path:
    skill_dir = agent_workspace_root_path(rp.storage_root, "assistant") / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n\nFollow {name}.\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


@pytest.mark.parametrize(
    ("agent_cfg", "expect_participation", "expect_listen"),
    [
        ({}, False, False),
        ({"participation": {}}, True, False),
        (_LISTEN, False, True),
        ({"participation": {}, **_LISTEN}, True, True),
    ],
)
def test_matrix_automation_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_cfg: dict[str, Any],
    expect_participation: bool,
    expect_listen: bool,
) -> None:
    """Dynamic participation and listen lines render only when configured on a Matrix agent."""
    build, _ = _build(tmp_path, monkeypatch, agent_cfg=agent_cfg)
    role = build.prompt.role or ""
    assert ("judges unmentioned messages" in role) is expect_participation
    assert ("silent listening turns" in role) is expect_listen
    assert "config.yaml" not in role


@pytest.mark.parametrize(
    ("template", "expected_snippet"),
    [
        pytest.param(
            "Custom {display_name} ({matrix_id}).",
            "Custom Assistant (@mindroom_assistant:localhost).",
            id="custom-template",
        ),
        pytest.param("", None, id="empty-override"),
    ],
)
def test_matrix_automation_facts_suppressed_by_explicit_identity_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
    expected_snippet: str | None,
) -> None:
    """Explicit AGENT_IDENTITY_CONTEXT_TEMPLATE overrides (including empty) suppress default automation lines."""
    build, _ = _build(
        tmp_path,
        monkeypatch,
        agent_cfg={"participation": {}, **_LISTEN},
        prompts={"AGENT_IDENTITY_CONTEXT_TEMPLATE": template},
    )
    role = build.prompt.role or ""
    assert "judges unmentioned messages" not in role
    assert "silent listening turns" not in role
    if expected_snippet is None:
        assert "## Your Identity" not in role
        assert "A helpful assistant." in role
    else:
        assert expected_snippet in role


@pytest.mark.parametrize(
    ("channel", "prompts", "persona"),
    [
        ("openai_compat", None, None),
        ("headless", None, None),
        ("openai_compat", {"AGENT_IDENTITY_CONTEXT_TEMPLATE": "Custom {display_name} ({matrix_id})."}, None),
        (None, None, SubagentPersona(source_kind="profile", source_name="helper", system_prompt="Subagent.")),
    ],
)
def test_matrix_automation_facts_absent_outside_matrix_and_for_personas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str | None,
    prompts: dict[str, str] | None,
    persona: SubagentPersona | None,
) -> None:
    """Non-Matrix runs and personas omit Matrix automation lines while keeping Matrix-override fallback."""
    identity = (
        ToolExecutionIdentity(channel, "assistant", "@alice:localhost", None, None, None, "sess-1") if channel else None
    )
    build, _ = _build(
        tmp_path,
        monkeypatch,
        agent_cfg={"participation": {}, **_LISTEN},
        prompts=prompts,
        outside_matrix=channel is not None,
        execution_identity=identity,
        persona=persona,
    )
    role = build.prompt.role or ""
    assert "judges unmentioned messages" not in role
    assert "silent listening turns" not in role
    if prompts:
        assert "Custom Assistant (not available in OpenAI-compatible API)." in role


@pytest.mark.parametrize(
    ("agent_cfg", "native_search", "mode", "expected"),
    [
        ({"tools": []}, False, "standard", False),
        ({"tools": ["calculator"]}, False, "standard", False),
        ({"tools": [{"filesystem": {"include_tools": ["read_file", "ls", "grep"]}}]}, False, "standard", False),
        ({"tools": [{"filesystem": {"exclude_tools": ["write_file", "edit_file"]}}]}, False, "standard", False),
        ({"tools": [{"python": {"include_tools": ["read_file", "list_files"]}}]}, False, "standard", False),
        ({"tools": [{"shell": {"enable_run_shell_command": False}}]}, False, "standard", False),
        ({"tools": [{"shell": {"defer": True, "enable_run_shell_command": False}}]}, False, "minimal", False),
        ({"tools": ["filesystem"]}, False, "standard", True),
        ({"tools": ["python"]}, False, "standard", True),
        ({"tools": ["shell"]}, False, "standard", True),
        ({"tools": [{"filesystem": {"defer": True}}]}, True, "standard", True),
        ({"tools": [{"filesystem": {"defer": True}}]}, False, "standard", True),
        ({"tools": [{"python": {"defer": True}}]}, True, "standard", True),
        ({"tools": [{"shell": {"defer": True}}]}, False, "minimal", True),
        ({"tools": ["filesystem", "skill_manage"]}, False, "standard", False),
        ({"tools": ["filesystem"], "skill_learning": {"enabled": True}}, False, "standard", False),
        ({"tools": [{"shell": {"defer": True}}, "skill_manage"]}, False, "minimal", False),
    ],
)
def test_workspace_skill_authoring_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_cfg: dict[str, Any],
    native_search: bool,
    mode: AgentMode,
    expected: bool,
) -> None:
    """WORKSPACE_SKILL_AUTHORING_PROMPT follows registry workspace writers across eager, deferred, and minimal modes."""
    build, _ = _build(tmp_path, monkeypatch, agent_cfg=agent_cfg, native_tool_search=native_search, agent_mode=mode)
    assert (WORKSPACE_SKILL_AUTHORING_PROMPT in build.prompt.instructions) is expected


@pytest.mark.parametrize(
    ("tools", "blocked_fn", "approval", "mode", "expected"),
    [
        (["filesystem"], {"write_file", "edit_file"}, (), "standard", False),
        (["filesystem"], set(), ("write_file", "edit_file"), "standard", False),
        ([{"shell": {"defer": True}}], set(), ("run_shell_command",), "minimal", False),
        (["filesystem", "skill_manage"], set(), ("skill_manage",), "standard", True),
        ([{"filesystem": {"defer": True}}], {"tool_search", "call_tool"}, (), "standard", False),
    ],
)
def test_workspace_skill_authoring_filters_approval_and_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[Any],
    blocked_fn: set[str],
    approval: tuple[str, ...],
    mode: AgentMode,
    expected: bool,
) -> None:
    """Function filters, non-resumable approval, and unreachable portable search gate skill authoring accurately."""
    build, _ = _build(
        tmp_path,
        monkeypatch,
        agent_cfg={"tools": tools},
        approval_matches=approval,
        supports_native_tool_approval=not approval,
        tool_function_filter=(lambda fn: fn.name not in blocked_fn) if blocked_fn else None,
        agent_mode=mode,
    )
    assert (WORKSPACE_SKILL_AUTHORING_PROMPT in build.prompt.instructions) is expected


@pytest.mark.parametrize(
    ("tools", "native_search", "use_kb", "hide_search", "expected"),
    [
        ([], False, False, False, False),
        (["skill_manage"], False, False, False, False),
        ([], False, True, False, False),
        (["calculator"], False, False, False, True),
        (["shell"], False, False, False, True),
        ([{"calculator": {"defer": True}}], True, False, False, True),
        ([{"calculator": {"defer": True}}], False, False, False, True),
        ([{"calculator": {"defer": True}}], False, False, True, False),
    ],
)
def test_output_redirect_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[Any],
    native_search: bool,
    use_kb: bool,
    hide_search: bool,
    expected: bool,
) -> None:
    """Default OUTPUT_REDIRECT_PROMPT is omitted; custom overrides still follow tool gating."""
    assert OUTPUT_REDIRECT_PROMPT == ""
    default_build, _ = _build(
        tmp_path / "default",
        monkeypatch,
        agent_cfg={"tools": tools},
        native_tool_search=native_search,
        knowledge=_dummy_knowledge() if use_kb else None,
        tool_function_filter=(lambda fn: fn.name not in {"tool_search", "call_tool"}) if hide_search else None,
    )
    assert all(instruction.strip() for instruction in default_build.prompt.instructions)
    assert "mindroom_output_path" not in "\n".join(default_build.prompt.instructions)

    override = "CUSTOM_OUTPUT_REDIRECT"
    custom_build, _ = _build(
        tmp_path / "custom",
        monkeypatch,
        agent_cfg={"tools": tools},
        prompts={"OUTPUT_REDIRECT_PROMPT": override},
        native_tool_search=native_search,
        knowledge=_dummy_knowledge() if use_kb else None,
        tool_function_filter=(lambda fn: fn.name not in {"tool_search", "call_tool"}) if hide_search else None,
    )
    assert (override in custom_build.prompt.instructions) is expected


def test_tool_execution_environment_modes_and_generated_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool execution environment is omitted when primary-only has no actionable lines and detailed for workers."""
    primary, _ = _build(tmp_path / "primary", monkeypatch, agent_cfg={"tools": ["calculator", "scheduler"]})
    role = primary.prompt.role or ""
    assert "## Tool Execution Environment" not in role
    assert "- All tools run in the primary MindRoom runtime." not in role

    path_only, _ = _build(tmp_path / "path_only", monkeypatch, agent_cfg={"tools": ["filesystem"]})
    path_role = path_only.prompt.role or ""
    assert "## Tool Execution Environment" in path_role
    assert "- All tools run in the primary MindRoom runtime." not in path_role
    assert "- File access for path tools: `workspace` (agent workspace and attachments only)." in path_role

    unrestricted_shell, _ = _build(
        tmp_path / "unrestricted_shell",
        monkeypatch,
        agent_cfg={"tools": ["shell"], "file_access": "unrestricted"},
    )
    assert "## Tool Execution Environment" not in (unrestricted_shell.prompt.role or "")

    mixed, _ = _build(
        tmp_path / "mixed",
        monkeypatch,
        agent_cfg={"tools": ["calculator", "shell"], "worker_tools": ["shell"], "worker_scope": "user_agent"},
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes", "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token"},
    )
    mixed_role = mixed.prompt.role or ""
    assert "- Local tools (primary MindRoom runtime): `calculator`." in mixed_role
    assert "- Worker-routed tools: `shell`." in mixed_role
    assert "- Worker backend: `kubernetes`." in mixed_role
    assert (
        "- Worker state persists across turns for that scope (stopped/scaled down when idle, files kept)." in mixed_role
    )
    assert "- Execution location is determined per tool; this agent is not sandboxed as a whole." in mixed_role

    kb_only, _ = _build(tmp_path / "kb", monkeypatch, agent_cfg={"tools": []}, knowledge=_dummy_knowledge())
    assert "## Tool Execution Environment" not in (kb_only.prompt.role or "")

    _write_skill(_runtime_paths(tmp_path / "skill"), "demo", "Demo skill.")
    skill_only, _ = _build(tmp_path / "skill", monkeypatch, agent_cfg={"tools": []})
    assert "## Tool Execution Environment" not in (skill_only.prompt.role or "")

    empty, _ = _build(tmp_path / "empty", monkeypatch, agent_cfg={"tools": []})
    assert "## Tool Execution Environment" not in (empty.prompt.role or "")


@pytest.mark.parametrize(
    ("tools", "expected_local"),
    [
        pytest.param(["calculator", {"shell": {"defer": True}}], "`calculator`", id="eager-local-and-deferred-worker"),
        pytest.param(
            [{"calculator": {"defer": True}}, {"shell": {"defer": True}}],
            "`calculator`",
            id="deferred-local-and-deferred-worker",
        ),
        pytest.param([{"shell": {"defer": True}}], "none", id="deferred-worker-only"),
    ],
)
def test_minimal_mode_deferred_worker_routed_shell_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[Any],
    expected_local: str,
) -> None:
    """Minimal mode role context reports worker routing when a deferred toolkit runs in a worker."""
    build, _ = _build(
        tmp_path / "worker",
        monkeypatch,
        agent_cfg={"tools": tools, "worker_tools": ["shell"], "worker_scope": "user_agent"},
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes", "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token"},
        agent_mode="minimal",
    )
    role = minimal_context_documents(build)["role"]
    assert "- All tools run in the primary MindRoom runtime." not in role
    assert f"- Local tools (primary MindRoom runtime): {expected_local}." in role
    assert "- Worker-routed tools: `shell`." in role
    assert "- Worker backend: `kubernetes`." in role
    assert "- Not confined by file_access (only a worker isolates them): `shell`." in role

    local_build, _ = _build(
        tmp_path / "local",
        monkeypatch,
        agent_cfg={"tools": [{"shell": {"defer": True}}]},
        agent_mode="minimal",
    )
    local_role = minimal_context_documents(local_build)["role"]
    assert "- All tools run in the primary MindRoom runtime." not in local_role
    assert "- File access for path tools:" not in local_role
    assert "- Not confined by file_access (only a worker isolates them): `shell`." in local_role


def test_skills_deduplication_diagnostics_overrides_and_cache_stability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills catalog omits duplicated instructions, diagnostics and overrides work, and cache prefix stays stable."""
    rp = _runtime_paths(tmp_path / "skills")
    skill_dir = _write_skill(rp, "release-check", "Verify release notes.")
    broken = agent_workspace_root_path(rp.storage_root, "assistant") / "skills" / "broken-skill"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "SKILL.md").write_text("---\nname: broken\ndescription: [unterminated\n---\n", encoding="utf-8")

    frozen = datetime(2026, 11, 24, 9, 0, tzinfo=ZoneInfo("UTC"))
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen

        def build_skills() -> tuple[AgentBuild, RuntimePaths]:
            return _build(
                tmp_path / "skills",
                monkeypatch,
                agent_cfg={"tools": ["filesystem"]},
                approval_matches=("get_skill_script",),
            )

        before, _ = build_skills()
        before_stable, before_turn = render_system_messages(before.prompt)
        for folder, filename in (("references", "checklist.md"), ("scripts", "check.sh")):
            (skill_dir / folder).mkdir()
            (skill_dir / folder / filename).write_text("ok\n", encoding="utf-8")
        after, _ = build_skills()
        after_stable, after_turn = render_system_messages(after.prompt)

    assert SKILLS_TOOL_USAGE_PROMPT in before.prompt.instructions
    assert before.prompt.skills_block == (
        "<skills_system>\nAvailable skills (load via skill tools, not callable directly):\n"
        "<skill>\n  <name>release-check</name>\n  <description>Verify release notes.</description>\n</skill>\n"
        "</skills_system>"
    )
    joined = "\n".join(before.prompt.instructions)
    assert "These skill functions are not available to you: get_skill_script." in joined
    assert "Workspace skill `skills/broken-skill/SKILL.md` did not load." in joined
    assert before_stable.content == after_stable.content
    assert "checklist.md" not in str(after_stable.content)
    assert "Current date:" not in str(after_stable.content)
    assert "Current date: Tuesday, November 24, 2026 (UTC, UTC)." in str(after_turn.content)
    assert before_turn.content == after_turn.content

    overrides = {
        "AGENT_IDENTITY_CONTEXT_TEMPLATE": (
            "Custom {display_name} ({matrix_id}) on {model_provider}/{model_id}.{openai_compat_history_guidance}"
        ),
        "SKILLS_TOOL_USAGE_PROMPT": "CUSTOM_SKILLS_USAGE",
        "WORKSPACE_SKILL_AUTHORING_PROMPT": "CUSTOM_SKILL_AUTHORING",
        "OUTPUT_REDIRECT_PROMPT": "CUSTOM_OUTPUT_REDIRECT",
        "INTERACTIVE_QUESTION_PROMPT": "CUSTOM_INTERACTIVE",
        "HIDDEN_TOOL_CALLS_PROMPT": "CUSTOM_HIDDEN",
        "DATETIME_CONTEXT_TEMPLATE": "Date={date_str} TZ={timezone_str}/{timezone_abbrev}",
    }
    assert set(overrides).issubset(PROMPT_DEFAULTS)
    _write_skill(_runtime_paths(tmp_path / "overrides"), "release-check", "Verify release notes.")
    custom, _ = _build(
        tmp_path / "overrides",
        monkeypatch,
        agent_cfg={"tools": ["filesystem"], "show_tool_calls": False},
        prompts=overrides,
    )
    stable_msg, turn_msg = render_system_messages(custom.prompt)
    stable_text = str(stable_msg.content)
    assert "Custom Assistant (@mindroom_assistant:localhost) on Anthropic/claude-sonnet-4-6." in stable_text
    assert all(v in stable_text for k, v in overrides.items() if k.endswith("_PROMPT"))
    assert "Date=" in str(turn_msg.content)
    assert "TZ=UTC/UTC" in str(turn_msg.content)
