"""Integration tests for Task 2: trace_audit automation, Run step, and friction-triggered skill learning."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing
from pydantic import ValidationError

import mindroom.automations.trace_audit as trace_audit_automation
import mindroom.skill_learning.library as skill_library
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.automations.registry import AutomationDefinition, compile_automations
from mindroom.automations.runner import AutomationRunner
from mindroom.automations.steps import AutomationContext, CheckStep, Done, Run
from mindroom.automations.threads import record_automation_thread
from mindroom.automations.trace_audit import _resolve_audit_model, check_trace_audit
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import PluginAutomation, TraceAuditAutomation
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.config.skill_learning import SkillLearningConfig
from mindroom.constants import SKIP_MENTIONS_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.assistant_turn import stamp_model
from mindroom.reply_turn.request import PreparedResponseRuntime
from mindroom.response_runner import ResponseRunner
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.session_storage.agent_storage import agent_session_database_path, create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.skill_learning.reviewer import (
    AUDIT_REVIEW_MAX_INPUT_TOKENS,
    AUDIT_REVIEW_MAX_TOOL_CALLS,
    review_audit_cluster,
)
from mindroom.skill_learning.runner import SkillReviewRunner
from mindroom.skill_learning.tools import ReviewProgress
from mindroom.tool_system.skills import agent_workspace_skills_root
from mindroom.trace_audit.models import AuditFinding, EvidenceCard, FailureCluster
from mindroom.turn_origin import SenderKind, TurnIntent
from mindroom.usage.stats import collect_admin_usage
from tests.conftest import install_model_providers, seed_session
from tests.fakes.request_sizing import PlainRequestSizing
from tests.fakes.token_counter import fake_token_counter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.automations import Automation

ROOM, OTHER_ROOM = "!lobby:example.test", "!other:example.test"
LEARNED_SKILL = (
    "---\nname: deploy-checks\ndescription: Checks before running deployment commands\n"
    "metadata:\n  mindroom:\n    learned: true\n---\n1. Check target environment.\n"
)
USER_SKILL = "---\nname: user-runbook\ndescription: User-owned runbook\n---\n1. Keep user steps intact.\n"
type _ToolStep = tuple[str, dict[str, Any]]


@dataclass
class _ScriptedModel(PlainRequestSizing):
    """Minimal scripted ModelProvider for bounded evidence-only review tests."""

    script: list[_ToolStep] = field(default_factory=list)
    fixed_input_tokens: int | None = None
    final_answer: str = "Done."
    identity: ModelIdentity = field(default_factory=lambda: ModelIdentity(provider="openai", id="gpt-test"))
    capabilities: Capabilities = field(default_factory=Capabilities)
    native_history_route: None = None
    token_counter: Any = field(default_factory=fake_token_counter)
    model_params: dict[str, Any] = field(default_factory=dict)
    requests: list[list[str]] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        contents = [message.get_content_string() for message in request.messages]
        self.requests.append(contents)
        in_tok = (
            self.fixed_input_tokens if self.fixed_input_tokens is not None else max(10, sum(map(len, contents)) // 4)
        )
        usage = MessageMetrics(tokens=TokenCounts(input_tokens=in_tok, output_tokens=5, total_tokens=in_tok + 5))
        yield UsageReported(usage)
        if not self.script:
            done = Message(role="assistant", content=self.final_answer, metrics=usage)
            stamp_model(done, self.identity)
            yield TurnCompleted(message=done, stop_reason="end")
            return
        name, arguments = self.script.pop(0)
        call = {
            "id": f"call-{len(self.requests)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        msg = Message(role="assistant", content="", tool_calls=[call], metrics=usage)
        stamp_model(msg, self.identity)
        yield TurnCompleted(message=msg, stop_reason="tool_use")


@dataclass
class _Bot:
    """Test double for both AutomationRunner and SkillReviewRunner bot surfaces."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    running: bool = True
    client: Any = field(default_factory=lambda: MagicMock(user_id="@mindroom_mind:example.test"))
    conversation_reader: Any = field(default_factory=MagicMock)

    @property
    def hook_context(self) -> _Bot:
        return self

    async def send_message(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None = None,
        *,
        trigger_dispatch: bool = False,
    ) -> str:
        self.sent.append(
            {
                "room_id": room_id,
                "body": body,
                "thread_id": thread_id,
                "source_hook": source_hook,
                "extra_content": extra_content,
                "trigger_dispatch": trigger_dispatch,
            },
        )
        return f"$event{len(self.sent)}"

    async def latest_thread_event_id_if_needed(self, _room_id: str, thread_id: str) -> str | None:
        return thread_id


def _config_and_paths(
    tmp_path: Path,
    *,
    automations: list[Automation] | None = None,
    skill_learning: SkillLearningConfig | None = None,
    cheap_model: str | None = None,
) -> tuple[Config, RuntimePaths]:
    models = {
        k: ModelConfig(provider="openai", id=f"gpt-{k}", context_window=128_000)
        for k in ("default", "cheap", "review", "audit")
    }
    agent = AgentConfig(
        display_name="Mind",
        model="default",
        cheap_model=cheap_model,
        memory_backend="mem0",
        rooms=[ROOM],
        automations=automations,
        skill_learning=skill_learning or SkillLearningConfig(),
    )
    config = Config(agents={"mind": agent}, models=models, router=RouterConfig(model="default"))
    return config, resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)


def _seed_failing_session(
    config: Config,
    paths: RuntimePaths,
    *,
    session_id: str,
    run_id: str,
    room_id: str,
    created_at: int,
) -> None:
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    tools = [
        ToolExecution(
            tool_call_id=f"{run_id}-{i}",
            tool_name="run_shell_command",
            tool_args={"command": "pytest tests/"},
            is_error=False,
            result=f"Exit code 1\nAssertionError: broken {i}",
        )
        for i in (1, 2)
    ]
    run = RunRecord(
        run_id=run_id,
        agent_id="mind",
        session_id=session_id,
        status=RunStatus.completed,
        created_at=created_at,
        content="Deployment failed twice.",
        metadata={"room_id": room_id},
        tools=tools,
        messages=[
            Message(role="user", content="Deploy app", created_at=created_at),
            Message(role="assistant", content="Failed.", created_at=created_at + 1),
        ],
    )
    seed_session(
        storage,
        ConversationSession(
            session_id=session_id,
            kind="agent",
            owner_id="mind",
            runs=[run],
            created_at=created_at,
            metadata={"room_id": room_id},
        ),
    )


async def _tick(runner: AutomationRunner, now: datetime) -> None:
    await runner._tick(now)
    assert await wait_for_background_tasks(5.0)


def _actionable_finding(*, target_skill: str | None = None) -> AuditFinding:
    return AuditFinding(
        verdict="actionable",
        teachable=True,
        category="verification_gap",
        remedy="verify_outcome",
        root_cause="Repeated failing pytest runs without checking prerequisites",
        proposed_rule="Verify pytest prerequisites before retrying failing runs.",
        has_verified_fix=True,
        target_skill=target_skill,
    )


def test_trace_audit_config_defaults_and_friction_trigger_validation() -> None:
    """TraceAuditAutomation defaults, backward-compatible interval trigger, and friction validation."""
    default_cfg = Config(
        agents={"mind": AgentConfig(display_name="Mind", skill_learning=SkillLearningConfig(enabled=True))},
        router=RouterConfig(model="default"),
    )
    assert default_cfg.agents["mind"].skill_learning.trigger == "interval"

    ta_cfg = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="mem0",
                automations=["trace_audit"],
                skill_learning=SkillLearningConfig(enabled=True, trigger="friction"),
            ),
        },
        router=RouterConfig(model="default"),
    )
    (entry,) = ta_cfg.resolve_agent("mind").automations
    assert isinstance(entry, TraceAuditAutomation)
    assert (
        entry.name,
        entry.requires_file_memory,
        entry.cron,
        entry.min_idle_seconds,
        entry.sample_rate,
        entry.max_audits_per_run,
        entry.min_cluster_sessions,
        entry.cooldown_days,
        entry.max_nominations_per_run,
        entry.notify,
    ) == (
        "trace_audit",
        False,
        "0 * * * *",
        600,
        0.02,
        12,
        2,
        14,
        1,
        True,
    )

    inherited = Config(
        defaults={"automations": ["trace_audit"]},
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                memory_backend="mem0",
                skill_learning=SkillLearningConfig(enabled=True, trigger="friction"),
            ),
        },
        router=RouterConfig(model="default"),
    )
    assert [a.name for a in inherited.resolve_agent("mind").automations] == ["trace_audit"]

    disabled = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                skill_learning=SkillLearningConfig(enabled=False, trigger="friction"),
            ),
        },
        router=RouterConfig(model="default"),
    )
    assert disabled.agents["mind"].skill_learning.trigger == "friction"

    for defaults, agent_auto in (([], None), (["trace_audit"], [])):
        with pytest.raises(ValidationError, match="trace_audit"):
            Config(
                defaults={"automations": defaults},
                agents={
                    "mind": AgentConfig(
                        display_name="Mind",
                        automations=agent_auto,
                        skill_learning=SkillLearningConfig(enabled=True, trigger="friction"),
                    ),
                },
                router=RouterConfig(model="default"),
            )

    definition = compile_automations([]).get("trace_audit")
    assert definition is not None
    assert definition.requires_file_memory is False


def test_response_skill_review_skips_capture_and_counting_in_friction_mode(tmp_path: Path) -> None:
    """In friction mode _response_skill_review skips cancel, capture, and reply counting; interval keeps them."""
    for trigger, should_wire in (("friction", False), ("interval", True)):
        config, _ = _config_and_paths(
            tmp_path / trigger,
            automations=[TraceAuditAutomation()],
            skill_learning=SkillLearningConfig(enabled=True, trigger=trigger),
        )
        reviews = MagicMock()
        deps = MagicMock(agent_name="mind")
        deps.runtime.config = config
        deps.runtime.orchestrator.skill_reviews = reviews
        runner = ResponseRunner.__new__(ResponseRunner)
        runner.locked = MagicMock(deps=deps)

        request = MagicMock(contact=None)
        request.response_envelope.origin = MagicMock(
            requester_kind=SenderKind.USER,
            intent=TurnIntent.USER_MESSAGE,
            source_kind="matrix",
        )
        runtime = PreparedResponseRuntime(
            media_inputs=MagicMock(),
            model_prompt="hi",
            active_model_name="default",
            show_tool_calls=False,
            tool_context=MagicMock(),
        )

        queue_fn, next_runtime = runner._response_skill_review(
            request,
            runtime,
            session_id=f"{ROOM}:$t1",
            execution_identity=None,
        )
        assert (queue_fn is not None) is should_wire
        assert (next_runtime.skill_review_capture is not None) is should_wire
        assert reviews.cancel.called is should_wire


@pytest.mark.asyncio
async def test_runner_executes_builtin_run_under_lock_and_rejects_plugin_run(tmp_path: Path) -> None:
    """Built-in Run executes under the per-instance lock and posts Done.notice; plugin Run is rejected."""
    config, paths = _config_and_paths(
        tmp_path,
        automations=[TraceAuditAutomation(cron="* * * * *"), PluginAutomation(name="custom_plugin", cron="* * * * *")],
    )
    bot, started, release, plugin_executed = _Bot(), asyncio.Event(), asyncio.Event(), False
    seen_reviewers: dict[str, object] = {}

    def builtin_check(ctx: AutomationContext) -> CheckStep:
        seen_reviewers["builtin"] = ctx._review_audit_cluster

        async def _exec() -> Done:
            started.set()
            await release.wait()
            return Done(notice="🔍 Audit summary posted.")

        return Run(execute=_exec)

    def plugin_check(ctx: AutomationContext) -> CheckStep:
        seen_reviewers["plugin"] = ctx._review_audit_cluster

        async def _exec() -> Done:
            nonlocal plugin_executed
            plugin_executed = True
            return Done(notice="never")

        return Run(execute=_exec)

    defs = {
        "trace_audit": AutomationDefinition("trace_audit", builtin_check, requires_file_memory=False),
        "custom_plugin": AutomationDefinition("custom_plugin", plugin_check, requires_file_memory=False),
    }
    dummy_reviewer = AsyncMock()
    runner = AutomationRunner(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda n: cast("AgentBot", bot) if n == "mind" else None,
        definition_provider=defs.get,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        review_audit_cluster=dummy_reviewer,
    )

    t0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    await _tick(runner, t0)
    await runner._tick(t0 + timedelta(minutes=1))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert ("mind", None) in runner._active
    assert seen_reviewers["builtin"] is dummy_reviewer

    await runner._tick(t0 + timedelta(minutes=2))  # blocked while built-in Run holds per-instance lock
    assert "plugin" not in seen_reviewers

    release.set()
    assert await wait_for_background_tasks(5.0)
    assert ("mind", None) not in runner._active
    assert len(bot.sent) == 1
    assert bot.sent[0]["body"] == "🔍 Audit summary posted."
    assert bot.sent[0]["extra_content"] == {SKIP_MENTIONS_KEY: True}

    config.agents["mind"].automations = [PluginAutomation(name="custom_plugin", cron="* * * * *")]
    with structlog.testing.capture_logs() as logs:
        await _tick(runner, t0 + timedelta(minutes=3))
    assert not plugin_executed
    assert seen_reviewers["plugin"] is None
    assert ("mind", None) not in runner._active
    assert len(bot.sent) == 1
    assert any("Plugin automation returned built-in-only Run step" in str(log.get("event", "")) for log in logs)


@pytest.mark.asyncio
async def test_runner_builtin_run_quiet_and_failure_releases_lock(tmp_path: Path) -> None:
    """Empty Done.notice stays quiet; failing built-in Run releases the instance lock and posts a failure notice."""
    config, paths = _config_and_paths(tmp_path, automations=[TraceAuditAutomation(cron="* * * * *")])
    bot = _Bot()

    async def _quiet() -> Done:
        return Done(notice="")

    async def _boom() -> Done | None:
        msg = "audit boom"
        raise RuntimeError(msg)

    current = AutomationDefinition("trace_audit", lambda _c: Run(execute=_quiet), requires_file_memory=False)
    runner = AutomationRunner(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda n: cast("AgentBot", bot) if n == "mind" else None,
        definition_provider=lambda _name: current,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    t0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    await _tick(runner, t0)
    await _tick(runner, t0 + timedelta(minutes=1))
    assert bot.sent == []
    assert ("mind", None) not in runner._active

    current = AutomationDefinition("trace_audit", lambda _c: Run(execute=_boom), requires_file_memory=False)
    await _tick(runner, t0 + timedelta(minutes=2))
    assert ("mind", None) not in runner._active
    assert len(bot.sent) == 1
    assert "audit boom" in bot.sent[0]["body"]


def test_trace_audit_missing_db_and_model_resolution_precedence(tmp_path: Path) -> None:
    """check_trace_audit returns None without creating a DB; model resolves entry -> cheap -> skill_learning -> agent."""
    config, paths = _config_and_paths(
        tmp_path,
        automations=[TraceAuditAutomation()],
        cheap_model="cheap",
        skill_learning=SkillLearningConfig(enabled=False, model="review"),
    )
    db_path = agent_session_database_path("mind", config, paths, None)
    ctx = AutomationContext(
        "mind",
        config,
        paths,
        TraceAuditAutomation(),
        {},
        {},
        None,
        None,
        tmp_path / "ws",
        tmp_path / "st",
        _room_id=ROOM,
    )

    assert check_trace_audit(ctx) is None
    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert _resolve_audit_model(ctx, TraceAuditAutomation(model="audit")) == "audit"
    assert _resolve_audit_model(ctx, TraceAuditAutomation()) == "cheap"
    config.agents["mind"].cheap_model = None
    assert _resolve_audit_model(ctx, TraceAuditAutomation()) == "review"
    config.agents["mind"].skill_learning.model = None
    assert _resolve_audit_model(ctx, TraceAuditAutomation()) == "default"


@pytest.mark.asyncio
async def test_trace_audit_report_only_mode_posts_notice_and_respects_cooldown(tmp_path: Path) -> None:
    """Report-only trace_audit isolates rooms, skips automation threads, posts one notice, and writes no skills."""
    config, paths = _config_and_paths(
        tmp_path,
        automations=[TraceAuditAutomation(cron="* * * * *", min_idle_seconds=0, sample_rate=0.0, notify=True)],
    )
    old_ts = int(time.time()) - 3600
    _seed_failing_session(config, paths, session_id=f"{ROOM}:$s1", run_id="r1", room_id=ROOM, created_at=old_ts)
    _seed_failing_session(config, paths, session_id=f"{ROOM}:$s2", run_id="r2", room_id=ROOM, created_at=old_ts + 10)
    _seed_failing_session(
        config,
        paths,
        session_id=f"{OTHER_ROOM}:$s3",
        run_id="r3",
        room_id=OTHER_ROOM,
        created_at=old_ts,
    )
    _seed_failing_session(config, paths, session_id=f"{ROOM}:$auto1", run_id="r4", room_id=ROOM, created_at=old_ts)
    record_automation_thread(paths, "$auto1")

    audited: list[str] = []

    async def _judge(card: EvidenceCard) -> AuditFinding:
        audited.append(card.session_id)
        return _actionable_finding()

    bot = _Bot()
    runner = AutomationRunner(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda n: cast("AgentBot", bot) if n == "mind" else None,
        definition_provider=compile_automations([]).get,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    t0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    with patch.object(trace_audit_automation, "make_llm_audit_judge", return_value=_judge):
        await _tick(runner, t0)
        await _tick(runner, t0 + timedelta(minutes=1))
        await _tick(runner, t0 + timedelta(minutes=2))  # cooldown suppresses duplicate notice

    assert sorted(audited) == sorted([f"{ROOM}:$s1", f"{ROOM}:$s2"])
    assert len(bot.sent) == 1
    assert "run_shell_command" in bot.sent[0]["body"]
    assert bot.sent[0]["extra_content"] == {SKIP_MENTIONS_KEY: True}
    assert not agent_workspace_skills_root(resolve_agent_runtime("mind", config, paths, None).workspace.root).exists()


@pytest.mark.asyncio
async def test_trace_audit_friction_mode_end_to_end_promotes_cluster_to_skill_learning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Friction-mode trace_audit promotes a 2-session cluster into bounded review_audit_cluster with learner guards."""
    config, paths = _config_and_paths(
        tmp_path,
        automations=[TraceAuditAutomation(cron="* * * * *", min_idle_seconds=0, sample_rate=0.0)],
        skill_learning=SkillLearningConfig(enabled=True, trigger="friction", model="review", notify=True),
    )
    skills_root = agent_workspace_skills_root(
        resolve_agent_runtime("mind", config, paths, None, create=True).workspace.root,
    )
    skill_library.create_skill(skills_root, "deploy-checks", LEARNED_SKILL, reserved_names=frozenset(), learner=True)
    skill_library.create_skill(skills_root, "user-runbook", USER_SKILL, reserved_names=frozenset(), learner=False)

    old_ts = int(time.time()) - 3600
    _seed_failing_session(config, paths, session_id=f"{ROOM}:$s1", run_id="r1", room_id=ROOM, created_at=old_ts)
    _seed_failing_session(config, paths, session_id=f"{ROOM}:$s2", run_id="r2", room_id=ROOM, created_at=old_ts + 10)

    review_model = _ScriptedModel(
        script=[
            (
                "skill_manage",
                {"action": "patch", "name": "user-runbook", "old_string": "intact", "new_string": "hacked"},
            ),
            ("get_skill_instructions", {"skill_name": "deploy-checks"}),
            (
                "skill_manage",
                {
                    "action": "patch",
                    "name": "deploy-checks",
                    "old_string": "1. Check target environment.",
                    "new_string": "1. Check target environment.\n2. Verify pytest prerequisites.",
                },
            ),
        ],
    )
    install_model_providers(monkeypatch, {"default": review_model, "review": review_model})

    bot, skill_notices = _Bot(), []

    async def _fake_send(_client: object, room_id: str, content: dict[str, Any]) -> str:
        skill_notices.append((room_id, content))
        return "$notice1"

    monkeypatch.setattr("mindroom.skill_learning.runner.send_message_result", _fake_send)
    skill_reviews = SkillReviewRunner(paths, bot_provider=lambda n: bot if n == "mind" else None)
    runner = AutomationRunner(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda n: cast("AgentBot", bot) if n == "mind" else None,
        definition_provider=compile_automations([]).get,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        review_audit_cluster=skill_reviews.review_audit_cluster,
    )

    t0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    with patch.object(
        trace_audit_automation,
        "make_llm_audit_judge",
        return_value=AsyncMock(return_value=_actionable_finding(target_skill="deploy-checks")),
    ):
        await _tick(runner, t0)
        await _tick(runner, t0 + timedelta(minutes=1))

    prompt = review_model.requests[0][0]
    assert "<failure_cluster>" in prompt
    assert "<conversation>" not in prompt
    assert "Nothing to save." in prompt
    assert json.loads(review_model.requests[1][-1])["success"] is False
    assert (skills_root / "user-runbook" / "SKILL.md").read_text(encoding="utf-8") == USER_SKILL
    assert "2. Verify pytest prerequisites." in (skills_root / "deploy-checks" / "SKILL.md").read_text(encoding="utf-8")
    assert list((skills_root / ".history" / "deploy-checks").glob("*SKILL.md"))
    assert len(skill_notices) == 1
    assert skill_notices[0][0] == ROOM
    assert "updated `deploy-checks`" in skill_notices[0][1]["body"]
    assert bot.sent == []
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.request_breakdown is not None
    assert "skill_learning" in {row.kind for row in report.request_breakdown}


@pytest.mark.asyncio
async def test_review_audit_cluster_enforces_4_tool_call_cap_16k_token_cap_and_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review_audit_cluster enforces 4-tool-call cap, 16,000-input-token cap, and conservative Nothing to save."""
    config, paths = _config_and_paths(
        tmp_path,
        automations=[TraceAuditAutomation()],
        skill_learning=SkillLearningConfig(enabled=True, trigger="friction"),
    )
    skills_root = agent_workspace_skills_root(
        resolve_agent_runtime("mind", config, paths, None, create=True).workspace.root,
    )
    skill_library.create_skill(skills_root, "deploy-checks", LEARNED_SKILL, reserved_names=frozenset(), learner=True)
    cluster = FailureCluster(
        cluster_key="tool|mind::!lobby:example.test|run_shell_command|pytest|nonzero_exit|verify_outcome",
        scope_key="mind::!lobby:example.test",
        tool_name="run_shell_command",
        operation="pytest",
        kind="nonzero_exit",
        diagnostic_family="exit_1",
        category="verification_gap",
        remedy="verify_outcome",
        target_skill="deploy-checks",
        distinct_sessions=2,
        occurrence_count=3,
        session_ids=(f"{ROOM}:$s1", f"{ROOM}:$s2"),
        root_causes=("Missing environment setup",),
        proposed_rules=("Verify environment before retrying tests.",),
        has_verified_fix=True,
        latest_created_at=1_750_000_000.0,
    )

    # 4-tool-call cap: 5th call (patch) is refused and never mutates the skill.
    call_cap_model = _ScriptedModel(
        script=[
            *[("get_skill_instructions", {"skill_name": "deploy-checks"})] * 4,
            (
                "skill_manage",
                {
                    "action": "patch",
                    "name": "deploy-checks",
                    "old_string": "1. Check target environment.",
                    "new_string": "1. Past cap.",
                },
            ),
            ("get_skill_instructions", {"skill_name": "deploy-checks"}),
        ],
    )
    install_model_providers(monkeypatch, {"default": call_cap_model})
    cap_out = await review_audit_cluster(
        config=config,
        runtime_paths=paths,
        agent_name="mind",
        cluster=cluster,
        skills_root=skills_root,
        progress=ReviewProgress(),
        record_usage=False,
    )
    assert cap_out.tool_calls == AUDIT_REVIEW_MAX_TOOL_CALLS == 4
    assert cap_out.changed_skills == {}
    assert "Past cap." not in (skills_root / "deploy-checks" / "SKILL.md").read_text(encoding="utf-8")

    # 16,000 cumulative input-token cap: two 9,000-token turns stop before turn 3.
    tok_model = _ScriptedModel(
        fixed_input_tokens=9_000,
        script=[("get_skill_instructions", {"skill_name": "deploy-checks"})] * 3,
    )
    install_model_providers(monkeypatch, {"default": tok_model})
    tok_out = await review_audit_cluster(
        config=config,
        runtime_paths=paths,
        agent_name="mind",
        cluster=cluster,
        skills_root=skills_root,
        progress=ReviewProgress(),
        record_usage=False,
    )
    assert tok_out.model_requests == 2
    assert tok_out.input_tokens >= AUDIT_REVIEW_MAX_INPUT_TOKENS

    # Conservative noop path: zero edits.
    install_model_providers(monkeypatch, {"default": _ScriptedModel(script=[], final_answer="Nothing to save.")})
    noop_out = await review_audit_cluster(
        config=config,
        runtime_paths=paths,
        agent_name="mind",
        cluster=cluster,
        skills_root=skills_root,
        progress=ReviewProgress(),
        record_usage=False,
    )
    assert noop_out.changed_skills == {}
    assert noop_out.tool_calls == 0
    assert noop_out.final_text == "Nothing to save."
