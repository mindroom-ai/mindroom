"""Focused unit tests for default agent system prompt cleanup, gating, and cache stability."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

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
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
)
from tests.fakes.scripted_model import ScriptedModel
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_modes import AgentMode
    from mindroom.tool_system.tool_function import ToolFunction


def _runtime_paths(tmp_path: Path, process_env: dict[str, str] | None = None) -> RuntimePaths:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            **(process_env or {}),
        },
    )


def _dummy_knowledge() -> AgentKnowledge:
    return AgentKnowledge(
        sources=(
            KnowledgeSource(
                description=KnowledgeSourceDescription(base_id="docs", description="Docs"),
                index=MagicMock(),
            ),
        ),
    )


def _build_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_cfg: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    prompts: dict[str, str] | None = None,
    tool_approval: ToolApprovalConfig | None = None,
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
    runtime_paths = _runtime_paths(tmp_path, process_env=process_env)
    payload: dict[str, Any] = {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "A helpful assistant.",
                **(agent_cfg or {}),
            },
        },
        "defaults": {"tools": [], **(defaults or {})},
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "timezone": "UTC",
    }
    if prompts:
        payload["prompts"] = prompts
    if tool_approval is not None:
        payload["tool_approval"] = tool_approval.model_dump(exclude_none=True)
    config = bind_runtime_paths(Config.model_validate(payload), runtime_paths)
    install_model_providers(
        monkeypatch,
        {
            "default": ScriptedModel(
                [],
                capabilities=Capabilities(native_tool_search=native_tool_search),
            ),
        },
    )
    build = create_agent(
        "assistant",
        config,
        runtime_paths,
        execution_identity=execution_identity,
        knowledge=knowledge,
        model=turn_model(config, runtime_paths, "assistant"),
        plugins=config_plugins(config, runtime_paths),
        outside_matrix=outside_matrix,
        supports_native_tool_approval=supports_native_tool_approval,
        tool_function_filter=tool_function_filter,
        agent_mode=agent_mode,
        persona=persona,
    )
    return build, runtime_paths


def _write_workspace_skill(
    runtime_paths: RuntimePaths,
    name: str,
    description: str = "Test skill.",
) -> Path:
    skill_dir = agent_workspace_root_path(runtime_paths.storage_root, "assistant") / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nFollow the {name} steps.\n",
        encoding="utf-8",
    )
    return skill_dir


class TestMatrixAutomationFacts:
    """Dynamic participation and listen facts appear only for enabled Matrix agents."""

    def test_participation_and_listen_absent_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither participation nor listening prose appears when both features are unset."""
        build, _ = _build_agent(tmp_path, monkeypatch)
        role = build.prompt.role or ""

        assert "judges unmentioned messages" not in role
        assert "silent listening turns" not in role

    def test_empty_participation_enables_matrix_fact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty participation block enables the participation dispatch line without leaking config paths."""
        build, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"participation": {}})
        role = build.prompt.role or ""

        assert "In multi-human threads where you already replied, MindRoom judges unmentioned messages" in role
        assert "silent listening turns" not in role
        assert "config.yaml" not in role

    def test_listen_enables_matrix_fact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Configuring listen adds the silent listening fact without leaking config paths."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": ["memory"], "listen": {"rooms": ["!lobby:localhost"], "tools": ["memory"]}},
        )
        role = build.prompt.role or ""

        assert "You also run separate silent listening turns in configured rooms" in role
        assert "judges unmentioned messages" not in role
        assert "config.yaml" not in role

    @pytest.mark.parametrize(
        ("outside_matrix", "channel", "prompts"),
        [
            (True, "openai_compat", None),
            (True, "headless", None),
            (True, "openai_compat", {"AGENT_IDENTITY_CONTEXT_TEMPLATE": "Custom {display_name} ({matrix_id})."}),
        ],
        ids=["openai-compat", "headless", "openai-compat-matrix-override"],
    )
    def test_automation_facts_absent_outside_matrix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outside_matrix: bool,
        channel: str,
        prompts: dict[str, str] | None,
    ) -> None:
        """Non-Matrix runs omit Matrix automation facts while preserving Matrix-override identity fallback."""
        identity = ToolExecutionIdentity(
            channel=channel,
            agent_name="assistant",
            requester_id="@alice:localhost",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id="sess-1",
        )
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={
                "tools": ["memory"],
                "participation": {},
                "listen": {"rooms": ["!lobby:localhost"], "tools": ["memory"]},
            },
            prompts=prompts,
            outside_matrix=outside_matrix,
            execution_identity=identity,
        )
        role = build.prompt.role or ""

        assert "judges unmentioned messages" not in role
        assert "silent listening turns" not in role
        if prompts:
            assert "Custom Assistant (not available in OpenAI-compatible API)." in role

    def test_automation_facts_absent_for_persona(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Authored subagent personas do not inherit Matrix participation or listening facts."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={
                "participation": {},
                "tools": ["memory"],
                "listen": {"rooms": ["!lobby:localhost"], "tools": ["memory"]},
            },
            persona=SubagentPersona(
                source_kind="profile",
                source_name="helper",
                system_prompt="You are a subagent.",
            ),
        )
        role = build.prompt.role or ""

        assert "judges unmentioned messages" not in role
        assert "silent listening turns" not in role


class TestWorkspaceSkillAuthoringGating:
    """WORKSPACE_SKILL_AUTHORING_PROMPT follows the final offered/discoverable tool surface."""

    @pytest.mark.parametrize(
        "tools",
        [
            [],
            ["calculator"],
            [{"filesystem": {"include_tools": ["read_file", "ls", "grep"]}}],
            [{"filesystem": {"exclude_tools": ["write_file", "edit_file"]}}],
            [{"shell": {"enable_run_shell_command": False}}],
        ],
        ids=["toolless", "non-writer", "fs-read-only-include", "fs-read-only-exclude", "shell-disabled"],
    )
    def test_omitted_without_workspace_writer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tools: list[Any],
    ) -> None:
        """Read-only and tool-less agents omit manual workspace skill authoring instructions."""
        build, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"tools": tools})
        assert WORKSPACE_SKILL_AUTHORING_PROMPT not in build.prompt.instructions

    def test_omitted_when_writers_hidden_by_filter_or_approval(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removing workspace writers via function filters or approval gating omits authoring guidance."""
        filtered_build, _ = _build_agent(
            tmp_path / "filtered",
            monkeypatch,
            agent_cfg={"tools": ["filesystem"]},
            tool_function_filter=lambda fn: fn.name not in {"write_file", "edit_file"},
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT not in filtered_build.prompt.instructions

        gated_build, _ = _build_agent(
            tmp_path / "gated",
            monkeypatch,
            agent_cfg={"tools": ["filesystem"]},
            tool_approval=ToolApprovalConfig(
                rules=[
                    ApprovalRuleConfig(match="write_file", action="require_approval"),
                    ApprovalRuleConfig(match="edit_file", action="require_approval"),
                ],
            ),
            supports_native_tool_approval=False,
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT not in gated_build.prompt.instructions

    @pytest.mark.parametrize(
        ("tools", "native_tool_search"),
        [
            (["filesystem"], False),
            (["shell"], False),
            ([{"filesystem": {"defer": True}}], True),
            ([{"filesystem": {"defer": True}}], False),
            ([{"shell": {"defer": True}}], True),
        ],
        ids=["eager-fs", "eager-shell", "native-deferred-fs", "portable-deferred-fs", "native-deferred-shell"],
    )
    def test_included_for_offered_or_discoverable_workspace_writer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tools: list[Any],
        native_tool_search: bool,
    ) -> None:
        """Eager or discoverable deferred filesystem/shell writers include workspace skill authoring guidance."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": tools},
            native_tool_search=native_tool_search,
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT in build.prompt.instructions

    @pytest.mark.parametrize(
        ("agent_cfg", "agent_mode"),
        [
            ({"tools": ["filesystem", "skill_manage"]}, "standard"),
            ({"tools": ["filesystem"], "skill_learning": {"enabled": True}}, "standard"),
            ({"tools": ["shell", "skill_manage"]}, "minimal"),
        ],
        ids=["eager-skill-manage", "skill-learning-enabled", "minimal-cli-deferred-skill-manage"],
    )
    def test_omitted_when_skill_manage_is_offered_or_deferred(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        agent_cfg: dict[str, Any],
        agent_mode: AgentMode,
    ) -> None:
        """Offering skill_manage eagerly, via skill_learning, or via minimal CLI suppresses manual authoring prose."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg=agent_cfg,
            agent_mode=agent_mode,
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT not in build.prompt.instructions

    def test_included_when_skill_manage_is_hidden_by_approval_but_filesystem_remains(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When approval hides skill_manage, a surviving filesystem writer still gets manual authoring guidance."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": ["filesystem", "skill_manage"]},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="skill_manage", action="require_approval")],
            ),
            supports_native_tool_approval=False,
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT in build.prompt.instructions

    def test_omitted_when_portable_deferred_writer_cannot_be_discovered(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A portable-deferred filesystem writer without tool_search/call_tool is not discoverable."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": [{"filesystem": {"defer": True}}]},
            native_tool_search=False,
            tool_function_filter=lambda fn: fn.name not in {"tool_search", "call_tool"},
        )
        assert WORKSPACE_SKILL_AUTHORING_PROMPT not in build.prompt.instructions


class TestOutputRedirectGating:
    """OUTPUT_REDIRECT_PROMPT appears only when offered/discoverable tools support mindroom_output_path or shell."""

    def test_omitted_for_toolless_or_non_redirectable_tools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool-less, skill_manage-only, and knowledge-only agents omit output redirect instructions."""
        toolless, _ = _build_agent(tmp_path / "toolless", monkeypatch, agent_cfg={"tools": []})
        assert OUTPUT_REDIRECT_PROMPT not in toolless.prompt.instructions

        skill_manage_only, _ = _build_agent(tmp_path / "sm", monkeypatch, agent_cfg={"tools": ["skill_manage"]})
        assert OUTPUT_REDIRECT_PROMPT not in skill_manage_only.prompt.instructions

        knowledge_only, _ = _build_agent(
            tmp_path / "kb",
            monkeypatch,
            agent_cfg={"tools": []},
            knowledge=_dummy_knowledge(),
        )
        assert OUTPUT_REDIRECT_PROMPT not in knowledge_only.prompt.instructions

    @pytest.mark.parametrize(
        ("tools", "native_tool_search"),
        [
            (["calculator"], False),
            (["shell"], False),
            ([{"calculator": {"defer": True}}], True),
            ([{"calculator": {"defer": True}}], False),
        ],
        ids=["eager-calculator", "eager-shell", "native-deferred", "portable-deferred"],
    )
    def test_included_when_redirectable_or_shell_offered(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tools: list[Any],
        native_tool_search: bool,
    ) -> None:
        """Eager or discoverable deferred tools accepting mindroom_output_path or shell include output redirect."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": tools},
            native_tool_search=native_tool_search,
        )
        assert OUTPUT_REDIRECT_PROMPT in build.prompt.instructions

    def test_omitted_when_portable_deferred_tools_are_not_discoverable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unreachable portable-deferred tools do not trigger output redirect instructions."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": [{"calculator": {"defer": True}}]},
            native_tool_search=False,
            tool_function_filter=lambda fn: fn.name not in {"tool_search", "call_tool"},
        )
        assert OUTPUT_REDIRECT_PROMPT not in build.prompt.instructions


class TestToolExecutionEnvironment:
    """Tool Execution Environment is compact for primary-only and accurate for generated-only tools."""

    def test_primary_only_is_compact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Primary-only tool surfaces emit one concise line without listing every toolkit."""
        build, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"tools": ["calculator", "scheduler"]})
        role = build.prompt.role or ""

        assert "## Tool Execution Environment\n- All tools run in the primary MindRoom runtime." in role
        assert "No tools use a worker runtime." not in role
        assert "`calculator`" not in role

    def test_mixed_primary_and_worker_keeps_routing_details(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mixed primary/worker tool surfaces retain explicit routing, backend, and isolation notes."""
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": ["calculator", "shell"], "worker_tools": ["shell"], "worker_scope": "user_agent"},
            process_env={
                "MINDROOM_WORKER_BACKEND": "kubernetes",
                "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
            },
        )
        role = build.prompt.role or ""

        assert "- Local tools (primary MindRoom runtime): `calculator`." in role
        assert "- Worker-routed tools: `shell`." in role
        assert "- Worker backend: `kubernetes`." in role
        assert "- Execution location is determined per tool; this agent is not sandboxed as a whole." in role

    def test_generated_only_tools_do_not_claim_no_tools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generated-only tools (knowledge search or skills) report primary runtime instead of no tools."""
        kb_build, _ = _build_agent(
            tmp_path / "kb",
            monkeypatch,
            agent_cfg={"tools": []},
            knowledge=_dummy_knowledge(),
        )
        assert "No tools are available in this runtime." not in (kb_build.prompt.role or "")
        assert "- All tools run in the primary MindRoom runtime." in (kb_build.prompt.role or "")

        skill_rp = _runtime_paths(tmp_path / "skill")
        _write_workspace_skill(skill_rp, "demo", "Demo skill.")
        skill_build, _ = _build_agent(tmp_path / "skill", monkeypatch, agent_cfg={"tools": []})
        assert "No tools are available in this runtime." not in (skill_build.prompt.role or "")
        assert "- All tools run in the primary MindRoom runtime." in (skill_build.prompt.role or "")

        toolless_build, _ = _build_agent(tmp_path / "empty", monkeypatch, agent_cfg={"tools": []})
        assert "- No tools are available in this runtime." in (toolless_build.prompt.role or "")


class TestSkillsDeduplicationAndPromptStability:
    """Skills instructions are never duplicated, overrides keep working, and prompt cache prefix stays stable."""

    def test_no_duplication_between_skill_usage_prompt_and_catalog(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The skills catalog lists only skill summaries without repeating SKILLS_TOOL_USAGE_PROMPT."""
        rp = _runtime_paths(tmp_path)
        _write_workspace_skill(rp, "release-check", "Verify release notes.")
        build, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"tools": []})

        assert SKILLS_TOOL_USAGE_PROMPT in build.prompt.instructions
        skills_block = build.prompt.skills_block or ""
        assert skills_block == (
            "<skills_system>\n"
            "Available skills (load via skill tools, not callable directly):\n"
            "<skill>\n"
            "  <name>release-check</name>\n"
            "  <description>Verify release notes.</description>\n"
            "</skill>\n"
            "</skills_system>"
        )
        for duplicated in ("What are Skills", "get_skill_instructions", "get_skill_reference", "get_skill_script"):
            assert duplicated not in skills_block

    def test_hidden_skill_function_and_refused_workspace_skill_diagnostics_remain(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hidden skill function notices and refused workspace skill diagnostics still render."""
        rp = _runtime_paths(tmp_path)
        _write_workspace_skill(rp, "good-skill", "Good skill.")
        broken = agent_workspace_root_path(rp.storage_root, "assistant") / "skills" / "broken-skill"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "SKILL.md").write_text("---\nname: broken\ndescription: [unterminated\n---\n", encoding="utf-8")

        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": []},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="get_skill_script", action="require_approval")],
            ),
        )
        joined = "\n".join(build.prompt.instructions)
        assert "These skill functions are not available to you: get_skill_script." in joined
        assert "Workspace skill `skills/broken-skill/SKILL.md` did not load." in joined

    def test_custom_prompt_overrides_render_cleanly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All preserved PROMPT_DEFAULTS keys and placeholders render custom user overrides cleanly."""
        rp = _runtime_paths(tmp_path)
        _write_workspace_skill(rp, "release-check", "Verify release notes.")
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
        build, _ = _build_agent(
            tmp_path,
            monkeypatch,
            agent_cfg={"tools": ["filesystem"], "show_tool_calls": False},
            prompts=overrides,
        )
        stable, turn = render_system_messages(build.prompt)
        stable_text = str(stable.content)
        turn_text = str(turn.content)

        assert "Custom Assistant (@mindroom_assistant:localhost) on Anthropic/claude-sonnet-4-6." in stable_text
        for marker in (
            "CUSTOM_SKILLS_USAGE",
            "CUSTOM_SKILL_AUTHORING",
            "CUSTOM_OUTPUT_REDIRECT",
            "CUSTOM_INTERACTIVE",
            "CUSTOM_HIDDEN",
        ):
            assert marker in stable_text
        assert "Date=" in turn_text
        assert "TZ=UTC/UTC" in turn_text

    def test_adding_skill_support_file_preserves_stable_system_message_and_date_stays_in_turn_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adding skill references/scripts does not mutate system message 1, and date stays in system message 2."""
        rp = _runtime_paths(tmp_path)
        skill_dir = _write_workspace_skill(rp, "release-check", "Verify release notes.")
        frozen = datetime(2026, 11, 24, 9, 0, tzinfo=ZoneInfo("UTC"))

        with patch("mindroom.system_prompt.datetime") as mock_datetime:
            mock_datetime.now.return_value = frozen
            before, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"tools": ["filesystem"]})
            before_stable, before_turn = render_system_messages(before.prompt)

            refs = skill_dir / "references"
            refs.mkdir()
            (refs / "checklist.md").write_text("# Checklist\n", encoding="utf-8")
            scripts = skill_dir / "scripts"
            scripts.mkdir()
            (scripts / "check.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")

            after, _ = _build_agent(tmp_path, monkeypatch, agent_cfg={"tools": ["filesystem"]})
            after_stable, after_turn = render_system_messages(after.prompt)

        assert before_stable.content == after_stable.content
        assert "checklist.md" not in str(after_stable.content)
        assert "check.sh" not in str(after_stable.content)
        assert "Current date:" not in str(after_stable.content)
        assert "Current date: Tuesday, November 24, 2026 (UTC, UTC)." in str(after_turn.content)
        assert before_turn.content == after_turn.content
