"""Real filesystem and persisted-session tests for automatic skill learning."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, cast

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

import mindroom.skill_learning.store as store_module
import mindroom.skill_learning.worker as worker_module
import mindroom.tool_system.skills as skills_module
from mindroom.agent_modes import set_agent_mode
from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentPrivateConfig
from mindroom.provider_tool_policy import provider_tools_disabled
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.skill_learning.worker import SkillReview

import pytest
from agno.agent import Agent
from agno.metrics import RunMetrics

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.skills import build_agent_skills

MARKDOWN = "---\nname: learned-task\ndescription: Verify a repaired task\n---\nRun the check and inspect its result.\n"


def test_learning_is_opt_in() -> None:
    """Learning is opt in."""
    agent = AgentConfig(display_name="Mind", role="")
    assert getattr(agent, "skill_learning", None) is not None
    assert agent.skill_learning.enabled is False


def test_publish_is_discovered_and_replay_is_idempotent(tmp_path: Path) -> None:
    """Publish is discovered and replay is idempotent."""
    module = store_module
    store = module.SkillStore(tmp_path / "owner")
    revision = store.snapshot()
    assert store.publish("learned-task", MARKDOWN, action="create", expected=revision, source="run-1")
    assert not store.publish("learned-task", MARKDOWN, action="create", expected=revision, source="run-1")
    assert (tmp_path / "owner/skills/learned-task/SKILL.md").read_text() == MARKDOWN
    assert not (tmp_path / "other/skills/learned-task/SKILL.md").exists()
    config = Config(agents={"mind": AgentConfig(display_name="Mind", role="")})
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    skills = build_agent_skills("mind", config, paths, workspace_skills_root=tmp_path / "owner/skills")
    assert skills is not None
    assert "learned-task" in skills.get_skill_names()


def test_update_preserves_previous_version_and_rejects_manual_edits(tmp_path: Path) -> None:
    """Update preserves previous version and rejects manual edits."""
    module = store_module
    store = module.SkillStore(tmp_path)
    store.publish("learned-task", MARKDOWN, action="create", expected=store.snapshot(), source="run-1")
    revision = store.snapshot()
    changed = MARKDOWN + "Repeat the verification.\n"
    assert store.publish("learned-task", changed, action="update", expected=revision, source="run-2")
    assert MARKDOWN in json.loads((store.workspace / ".skill-learning.json").read_text())["learned-task"]["previous"]
    revision = store.snapshot()
    path = tmp_path / "skills/learned-task/SKILL.md"
    path.write_text("User edit")
    with pytest.raises(ValueError, match=r"changed|owned"):
        store.publish("learned-task", changed, action="update", expected=revision, source="run-3")
    assert path.read_text() == "User edit"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "UPPER", "a/b", "mindroom-docs"])
def test_rejects_invalid_and_protected_names(tmp_path: Path, name: str) -> None:
    """Rejects invalid and protected names."""
    module = store_module
    store = module.SkillStore(tmp_path)
    with pytest.raises(ValueError, match=r"Invalid|Protected|owned|Unsafe"):
        store.publish(name, MARKDOWN.replace("learned-task", name), action="create", expected={}, source="run")


def test_rejects_manual_skill_and_symlink(tmp_path: Path) -> None:
    """Rejects manual skill and symlink."""
    module = store_module
    store = module.SkillStore(tmp_path / "owner")
    path = tmp_path / "owner/skills/learned-task"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(MARKDOWN)
    with pytest.raises(ValueError, match=r"owned|Unsafe"):
        store.publish("learned-task", MARKDOWN, action="update", expected=store.snapshot(), source="run")
    (path / "SKILL.md").unlink()
    (path / "SKILL.md").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match=r"owned|Unsafe"):
        store.publish("learned-task", MARKDOWN, action="create", expected={}, source="run")
    assert not (tmp_path / "outside").exists()


def test_replay_recovers_interrupted_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay recovers interrupted update."""
    module = store_module
    store = module.SkillStore(tmp_path)
    store.publish("learned-task", MARKDOWN, action="create", expected={}, source="run-1")
    expected = store.snapshot()
    changed = MARKDOWN + "Verify again.\n"
    original = module.atomic_write_bytes_at

    def fail_skill(descriptor: int, filename: str, payload: bytes, **kwargs: object) -> None:
        if filename == "SKILL.md":
            message = "Simulated crash before publication"
            raise OSError(message)
        original(descriptor, filename, payload, **kwargs)

    monkeypatch.setattr(module, "atomic_write_bytes_at", fail_skill)
    with pytest.raises(OSError, match="Simulated crash"):
        store.publish("learned-task", changed, action="update", expected=expected, source="run-2")
    monkeypatch.setattr(module, "atomic_write_bytes_at", original)
    assert store.publish("learned-task", changed, action="update", expected=expected, source="run-2")
    assert json.loads((store.workspace / ".skill-learning.json").read_text())["learned-task"]["previous"] == [MARKDOWN]


def _learner(tmp_path: Path, *, private: bool = False) -> tuple[Config, RuntimePaths]:

    config = Config(agents={"mind": AgentConfig(display_name="Mind", role="")})
    config.agents["mind"].skill_learning.enabled = True
    config.agents["mind"].skill_learning.cooldown_seconds = 0
    if private:
        config.agents["mind"].private = AgentPrivateConfig(per="user")
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    return config, paths


def _persist(
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity | None = None,
    *,
    text: str = "The validation passed",
) -> None:

    storage = create_session_storage("mind", config, paths, execution_identity=identity)

    seed_session(
        storage,
        AgentSession(
            session_id="session",
            agent_id="mind",
            runs=[
                RunOutput(
                    run_id="run-1",
                    agent_id="mind",
                    session_id="session",
                    messages=[
                        Message(role="user", content="Repair and verify"),
                        Message(role="tool", content=text, tool_call_id="tool-1"),
                        Message(role="assistant", content="The test passed after repair"),
                    ],
                ),
            ],
        ),
    )


@pytest.mark.asyncio
async def test_worker_reviews_persisted_trace_and_deduplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker reviews persisted trace and deduplicates."""
    module = worker_module
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    reviews = []

    async def review(**kwargs: object) -> SkillReview:
        reviews.append(cast("str", kwargs["trace"]))
        return module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(module, "_review_session", review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    worker = module.SkillLearningWorker(paths, lambda: config)
    await worker._run_cycle()
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(reviews) == 1
    assert '"role": "tool"' in reviews[0]
    assert "The validation passed" in reviews[0]
    assert (tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md").exists()


@pytest.mark.asyncio
async def test_private_queue_survives_restart_and_rechecks_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private queue survives restart and rechecks config."""
    module = worker_module
    config, paths = _learner(tmp_path, private=True)
    alice = ToolExecutionIdentity(
        channel="matrix",
        requester_id="@alice:example.test",
        agent_name="mind",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    bob = ToolExecutionIdentity(
        channel="matrix",
        requester_id="@bob:example.test",
        agent_name="mind",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    _persist(config, paths, alice)
    _persist(config, paths, bob, text="Bob's private data")
    alice_root = resolve_agent_runtime("mind", config, paths, alice).workspace.root
    bob_root = resolve_agent_runtime("mind", config, paths, bob).workspace.root

    async def review(**kwargs: object) -> SkillReview:
        assert "Bob's private data" not in cast("str", kwargs["trace"])
        return module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(module, "_review_session", review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=alice)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert (alice_root / "skills/learned-task/SKILL.md").read_text() == MARKDOWN
    assert not (bob_root / "skills/learned-task/SKILL.md").exists()
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=bob)
    config.agents["mind"].skill_learning.enabled = False
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert not (bob_root / "skills/learned-task/SKILL.md").exists()


@pytest.mark.asyncio
async def test_disabled_does_no_io_and_no_change_writes_no_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled does no io and no change writes no skill."""
    module = worker_module
    config, paths = _learner(tmp_path)
    config.agents["mind"].skill_learning.enabled = False
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    assert not (tmp_path / "skill_learning.db").exists()
    config.agents["mind"].skill_learning.enabled = True
    _persist(config, paths)

    async def review(**_kwargs: object) -> SkillReview:
        return module.SkillReview(action="no_change")

    monkeypatch.setattr(module, "_review_session", review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_retry_budget_and_current_config_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry budget and current config check."""
    module = worker_module
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    calls = []

    async def failed(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        raise TimeoutError

    monkeypatch.setattr(module, "_review_session", failed)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    for now in [100, 200, 300, 400, 500]:
        monkeypatch.setattr(module.time, "time", lambda now=now: 20000000000 + now)
        await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(calls) == 3
    assert not list(tmp_path.rglob("SKILL.md"))

    async def disabled_during_review(**_kwargs: object) -> SkillReview:
        config.agents["mind"].skill_learning.enabled = False
        return module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(module, "_review_session", disabled_during_review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_minimal_mode_preserves_queue_without_paid_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minimal mode preserves queue without paid review."""
    module = worker_module
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    runtime = resolve_agent_runtime("mind", config, paths, None)
    calls = []

    async def review(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        return module.SkillReview(action="no_change")

    monkeypatch.setattr(module, "_review_session", review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    set_agent_mode(runtime.state_root, "mind", "session", "minimal", "@alice:example.test")
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert calls == []
    set_agent_mode(runtime.state_root, "mind", "session", "standard", "@alice:example.test")
    resumed_at = worker_module.time.time() + 6
    monkeypatch.setattr(worker_module.time, "time", lambda: resumed_at)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_input_and_output_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Input and output budgets."""
    module = worker_module
    config, paths = _learner(tmp_path)
    config.agents["mind"].skill_learning.max_input_chars = 1000
    config.agents["mind"].skill_learning.max_output_chars = 500
    _persist(config, paths, text="Result " * 1000)
    seen = []

    async def review(**kwargs: object) -> SkillReview:
        seen.append(kwargs)
        return module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN + "X" * 1000)

    monkeypatch.setattr(module, "_review_session", review)
    module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(cast("str", seen[0]["trace"])) + len(cast("str", seen[0]["skill_context"])) <= 1000
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_worker_updates_owned_skill_with_labelled_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later verified correction updates the owned skill and preserves rollback content."""
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    changed = MARKDOWN + "Verify the corrected result.\n"
    calls = []

    async def review(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        if len(calls) == 1:
            return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)
        assert "learner-owned" in cast("str", kwargs["skill_context"])
        assert MARKDOWN in cast("str", kwargs["skill_context"])
        return worker_module.SkillReview(action="update", name="learned-task", markdown=changed)

    monkeypatch.setattr(worker_module, "_review_session", review)
    for result in ["first verified", "corrected verified"]:
        _persist(config, paths, text=result)
        worker_module.queue_skill_learning(
            config,
            paths,
            agent_name="mind",
            session_id="session",
            execution_identity=None,
        )
        await worker_module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    path = tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md"
    assert path.read_text() == changed
    assert json.loads((path.parents[2] / ".skill-learning.json").read_text())["learned-task"]["previous"] == [MARKDOWN]


def test_workspace_snapshot_rejects_symlink_content(tmp_path: Path) -> None:
    """Review context must not read an escaped workspace skill."""
    outside = tmp_path / "outside"
    outside.write_text(MARKDOWN)
    workspace = tmp_path / "workspace"
    skill = workspace / "skills/learned-task"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(outside)
    with pytest.raises(ValueError, match="Unsafe"):
        store_module.SkillStore(workspace).snapshot()


@pytest.mark.asyncio
async def test_worker_shutdown_does_not_publish_late_model_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that swallows cancellation cannot publish after worker shutdown."""
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    worker = worker_module.SkillLearningWorker(paths, lambda: config)

    async def review(**_kwargs: object) -> SkillReview:
        worker.stop()
        return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await worker._run_cycle()
    assert not list(tmp_path.rglob("SKILL.md"))
    # The retained validated proposal completes without another paid call after restart.
    await worker_module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert (tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md").exists()


@pytest.mark.asyncio
async def test_minimal_sessions_do_not_starve_ready_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paused sessions yield their queue position to runnable work."""
    config, paths = _learner(tmp_path)
    for index in range(4):
        session_id = f"paused-{index}"
        worker_module.queue_skill_learning(
            config,
            paths,
            agent_name="mind",
            session_id=session_id,
            execution_identity=None,
        )
        set_agent_mode(paths.storage_root / "agents/mind", "mind", session_id, "minimal", "@alice:example.test")
    _persist(config, paths)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    calls = []

    async def review(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        return worker_module.SkillReview(action="no_change")

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker = worker_module.SkillLearningWorker(paths, lambda: config)
    await worker._run_cycle()
    await worker._run_cycle()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reviewer_has_no_tools_and_records_content_free_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structured helper has no tools and attributes paid usage to its source session."""
    config, paths = _learner(tmp_path)
    _persist(config, paths)

    async def inference(self: Agent, prompt: str, **_kwargs: object) -> RunOutput:
        assert provider_tools_disabled()
        assert not self.tools
        assert self.output_schema is worker_module.SkillReview
        assert "private trace" in prompt
        return RunOutput(
            content=worker_module.SkillReview(action="no_change"),
            model="synthetic",
            model_provider="test",
            metrics=RunMetrics(input_tokens=7, output_tokens=3, total_tokens=10),
        )

    monkeypatch.setattr(
        worker_module.model_loading,
        "get_model_instance",
        lambda *_args, **_kwargs: SyntheticModel(id="synthetic"),
    )
    monkeypatch.setattr(Agent, "arun", inference)
    result = await worker_module._review_session(
        config=config,
        runtime_paths=paths,
        scope={"agent": "mind", "session": "session"},
        trace="private trace",
        skill_context="",
        identity=None,
    )
    assert result.action == "no_change"
    with sqlite3.connect(tmp_path / "agents/mind/sessions/mind.db") as connection:
        rows = connection.execute("SELECT usage_data FROM mind_sessions_usage").fetchall()
    helper_rows = [json.loads(row[0]) for row in rows if json.loads(row[0]).get("kind") == "skill_learning"]
    assert len(helper_rows) == 1
    assert helper_rows[0]["metrics"]["total_tokens"] == 10
    assert "private trace" not in json.dumps(helper_rows)


@pytest.mark.asyncio
async def test_persisted_trace_redacts_credentials_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session tool credentials never enter the review prompt or reusable skill files."""
    config, paths = _learner(tmp_path)
    _persist(config, paths, text="password: very-secret-password-value; verified result passed")
    calls = []

    async def review(**kwargs: object) -> SkillReview:
        calls.append(cast("str", kwargs["trace"]))
        return worker_module.SkillReview(action="no_change")

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await worker_module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(calls) == 1
    assert "very-secret-password-value" not in calls[0]


def test_publisher_rejects_url_credentials(tmp_path: Path) -> None:
    """Generated instructions cannot persist credentials embedded in URLs."""
    with pytest.raises(ValueError, match="credential"):
        store_module.SkillStore(tmp_path).publish(
            "learned-task",
            MARKDOWN + "Connect to https://alice:secret@service.test",
            action="create",
            expected={},
            source="source",
        )


@pytest.mark.asyncio
async def test_recovered_proposal_does_not_acknowledge_newer_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved proposal acknowledges only the turn generation it reviewed."""
    config, paths = _learner(tmp_path)
    _persist(config, paths)
    stopped = worker_module.SkillLearningWorker(paths, lambda: config)
    calls = []
    changed = MARKDOWN + "Verify the newer correction.\n"

    async def review(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        if len(calls) == 1:
            stopped.stop()
            return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)
        return worker_module.SkillReview(action="update", name="learned-task", markdown=changed)

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await stopped._run_cycle()
    _persist(config, paths, text="The newer correction passed")
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    worker = worker_module.SkillLearningWorker(paths, lambda: config)
    await worker._run_cycle()
    await worker._run_cycle()
    assert (tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md").read_text() == changed
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_private_scope_change_during_review_discards_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued private lesson cannot move to a newly configured private scope."""
    config, paths = _learner(tmp_path, private=True)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.test",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    _persist(config, paths, identity)

    async def review(**_kwargs: object) -> SkillReview:
        config.agents["mind"].private = AgentPrivateConfig(per="user_agent")
        return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(
        config,
        paths,
        agent_name="mind",
        session_id="session",
        execution_identity=identity,
    )
    await worker_module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert not list(tmp_path.rglob("SKILL.md"))


def test_queue_metadata_is_owner_readable_only(tmp_path: Path) -> None:
    """The shared queue must not expose private proposals to other operating-system users."""
    config, paths = _learner(tmp_path)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    assert (tmp_path / "skill_learning.db").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("allowlist", [[], ["allowed", "ineligible", "overridden"]])
@pytest.mark.asyncio
async def test_review_context_contains_only_effective_accessible_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allowlist: list[str],
) -> None:
    """Review inputs respect configured access, eligibility and workspace precedence."""
    config, paths = _learner(tmp_path)
    config.agents["mind"].skills = allowlist
    _persist(config, paths)
    global_root = tmp_path / "global-skills"
    workspace_root = tmp_path / "agents/mind/workspace/skills"
    for root, name, description, metadata in [
        (global_root, "hidden", "Hidden global procedure", ""),
        (global_root, "allowed", "Allowed global procedure", ""),
        (global_root, "overridden", "Overridden global procedure", ""),
        (global_root, "ineligible", "Ineligible global procedure", "metadata: {openclaw: {os: [unavailable-os]}}\n"),
        (workspace_root, "overridden", "Effective workspace procedure", ""),
        (
            workspace_root,
            "workspace-ineligible",
            "Ineligible workspace procedure",
            "metadata: {openclaw: {os: [unavailable-os]}}\n",
        ),
    ]:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n{metadata}---\n{description} body.\n",
        )
    monkeypatch.setattr(skills_module, "_get_default_skill_roots", lambda: [global_root])
    contexts = []

    async def review(**kwargs: object) -> SkillReview:
        contexts.append(cast("str", kwargs["skill_context"]))
        return worker_module.SkillReview(action="no_change")

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await worker_module.SkillLearningWorker(paths, lambda: config)._run_cycle()
    assert len(contexts) == 1
    assert "Effective workspace procedure body." in contexts[0]
    assert "Hidden global procedure" not in contexts[0]
    assert "Overridden global procedure" not in contexts[0]
    assert "Ineligible" not in contexts[0]
    assert ("Allowed global procedure" in contexts[0]) == bool(allowlist)
    store = store_module.SkillStore(workspace_root.parent)
    with pytest.raises(ValueError, match="Protected"):
        store.publish("hidden", MARKDOWN.replace("learned-task", "hidden"), action="create", expected={}, source="run")


def test_create_rejects_case_insensitive_manual_frontmatter_collision(tmp_path: Path) -> None:
    """A manual name protects its case variants regardless of the directory name."""
    store = store_module.SkillStore(tmp_path)
    manual_path = tmp_path / "skills/different-directory/SKILL.md"
    manual_path.parent.mkdir(parents=True)
    manual = MARKDOWN.replace("learned-task", "Learned-Task")
    manual_path.write_text(manual)
    with pytest.raises(ValueError, match="already exists"):
        store.publish("learned-task", MARKDOWN, action="create", expected=store.snapshot(), source="run")
    assert manual_path.read_text() == manual
    assert not (tmp_path / "skills/learned-task/SKILL.md").exists()


@pytest.mark.parametrize("newer_before_exhaustion", [False, True])
@pytest.mark.asyncio
async def test_exhausted_proposal_leaves_newer_turn_for_fresh_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newer_before_exhaustion: bool,
) -> None:
    """Rejected publications cannot pin a session to an old proposal or consume later turns."""
    config, paths = _learner(tmp_path)
    config.agents["mind"].skill_learning.max_attempts = 1
    _persist(config, paths)
    initial_worker = worker_module.SkillLearningWorker(paths, lambda: config)
    traces = []

    async def review(**kwargs: object) -> SkillReview:
        traces.append(cast("str", kwargs["trace"]))
        if len(traces) == 1:
            if newer_before_exhaustion:
                initial_worker.stop()
            return worker_module.SkillReview(
                action="create",
                name="mindroom-docs",
                markdown=MARKDOWN.replace("learned-task", "mindroom-docs"),
            )
        return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    await initial_worker._run_cycle()
    _persist(config, paths, text="A newer successful verification")
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    restarted = worker_module.SkillLearningWorker(paths, lambda: config)
    if newer_before_exhaustion:
        await restarted._run_cycle()
    with sqlite3.connect(paths.storage_root / "skill_learning.db") as connection:
        generation, processed, attempts, proposal = connection.execute(
            "SELECT generation, processed, attempts, proposal FROM reviews",
        ).fetchone()
    assert (generation, processed, attempts, proposal) == (2, 1, 0, None)
    await restarted._run_cycle()
    assert len(traces) == 2
    assert "A newer successful verification" in traces[1]
    assert (tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md").read_text() == MARKDOWN


@pytest.mark.asyncio
async def test_exhausted_inference_preserves_turn_queued_during_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure before a proposal exists settles only the generation that began review."""
    config, paths = _learner(tmp_path)
    config.agents["mind"].skill_learning.max_attempts = 1
    _persist(config, paths)
    calls = []

    async def review(**kwargs: object) -> SkillReview:
        calls.append(kwargs)
        if len(calls) == 1:
            _persist(config, paths, text="Later completed turn")
            worker_module.queue_skill_learning(
                config,
                paths,
                agent_name="mind",
                session_id="session",
                execution_identity=None,
            )
            raise TimeoutError
        return worker_module.SkillReview(action="create", name="learned-task", markdown=MARKDOWN)

    monkeypatch.setattr(worker_module, "_review_session", review)
    worker_module.queue_skill_learning(config, paths, agent_name="mind", session_id="session", execution_identity=None)
    worker = worker_module.SkillLearningWorker(paths, lambda: config)
    await worker._run_cycle()
    await worker._run_cycle()
    assert len(calls) == 2
    assert (tmp_path / "agents/mind/workspace/skills/learned-task/SKILL.md").exists()
