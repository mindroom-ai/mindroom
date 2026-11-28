"""End-to-end invariant and security-negative tests for Phase 1 K8s sandboxed subagents & workflows."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mindroom import workflow as workflow_sdk
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.minimal_agent import minimal_attempt, minimal_turn, presented_build
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_loop.tools import PreparedTools
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import create_agent
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths, write_startup_manifest
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation import lifecycle as delegation_lifecycle
from mindroom.delegation import workers as delegation_workers
from mindroom.delegation.background import delegation_child
from mindroom.delegation.continuation import continued_subagent_job
from mindroom.delegation.lifecycle import (
    child_execution_identity,
    child_run_context,
    finish_child_turn,
    prepare_child_turn,
    reserve_child_turn,
    start_child_turn,
)
from mindroom.delegation.state import DelegationChild
from mindroom.delegation.targets import DelegationTarget, resolve_delegation_target, resolve_fresh_target
from mindroom.delegation.workers import (
    SandboxedSubagentUnavailableError,
    child_placement,
    parse_delegation_sandbox,
    subagent_sandbox_worker_key,
    validate_sandbox_execution,
)
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import system_text
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_turn import ResponseTurnContext
from mindroom.run_storage import RunStorage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.execution import agent_tool_middleware, offered_definitions, wait_mode
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system import sandbox_proxy as sandbox_proxy_module
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import (
    DetachedRequesterContext,
    detached_requester_context,
    detached_tool_scope,
    tool_runtime_context,
)
from mindroom.tool_system.sandbox_proxy import (
    _build_execute_request,
    _ensure_route_worker,
    _placed_worker_spec,
    _resolve_call_placement,
    _worker_route_with_sandbox,
    save_attachment_to_worker,
    view_file_from_worker,
    worker_stand_ins,
)
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    ToolExecutionIdentity,
    agent_workspace_root_path,
    serialize_tool_execution_identity,
    subagent_worker_key,
    worker_root_path,
)
from mindroom.turn_model import TurnModel
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.backends.local import _LocalWorkerBackend
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerSandboxConfig, WorkerSpec
from mindroom.workers.named import NamedWorkerError, named_worker_call, resolve_locked_worker_options
from mindroom.workers.placements import DedicatedWorkerPlacement, dedicated_worker_placement
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION
from mindroom.workers.runtime import idle_worker_cleanup_owner
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowRunState
from mindroom.workflow_runs.peers import NodeRef, send
from mindroom.workflow_runs.store import SupersededAttemptError
from tests.access_schema_support import with_responder_access
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, default_delegation_executor
from tests.fakes.scripted_model import Await, ScriptedModel, answer
from tests.test_kubernetes_worker_backend import _named_backend, _named_spec
from tests.test_kubernetes_worker_backend import _spec as _k8s_spec
from tests.tool_job_helpers import tool_job_journal
from tests.workflow_helpers import NodeFailed, workflow_stack

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from tests.workflow_helpers import WorkflowStack

_K8S_ENV = {
    "MINDROOM_WORKER_BACKEND": "kubernetes",
    "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "60",
    "MINDROOM_KUBERNETES_NAMESPACE": "mindroom-test",
    "MINDROOM_KUBERNETES_IMAGE": "ghcr.io/mindroom-ai/mindroom:test",
    "MINDROOM_KUBERNETES_STORAGE_PVC_NAME": "mindroom-storage",
    "MINDROOM_KUBERNETES_NAMED_WORKERS_MAX": "4",
}
_DOCKER_ENV = {
    "MINDROOM_WORKER_BACKEND": "docker",
    "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "60",
}
_RUNNER_TOKEN = "sandbox-subagent-test-token"  # noqa: S105
_NORMAL_KEY = "v1:default:user_agent:@alice:example.org:leader"


@pytest.fixture(autouse=True)
def _idle_cleanup() -> Iterator[None]:
    with idle_worker_cleanup_owner():
        yield


def _paths(tmp_path: Path, env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", **(env or {})},
    )


def _identity(
    agent_name: str = "leader",
    *,
    session_id: str = "sess-1",
    requester_id: str = "@alice:example.org",
) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
    )


def _config(paths: RuntimePaths, **overrides: object) -> Config:
    agents = {
        "leader": AgentConfig(
            display_name="Leader",
            role="TOP_SECRET_AGENT_ROLE",
            instructions=["TOP_SECRET_AGENT_INSTRUCTION"],
            tools=["shell", "filesystem", "memory", "delegate", "matrix_message"],
            worker_tools=["shell", "filesystem"],
            worker_scope="user_agent",
            file_access="unrestricted",
            delegate_to=["leader", "helper"],
            dedicated_subagent_workers=True,
            named_workers=2,
            knowledge_bases=["kb1"],
            context_files=["EXTRA_SECRET.md"],
        ),
        "helper": AgentConfig(
            display_name="Helper",
            role="HELPER_SECRET_ROLE",
            tools=["shell", "filesystem"],
            worker_tools=["shell", "filesystem"],
            worker_scope="user_agent",
        ),
        "no_tools": AgentConfig(
            display_name="NoTools",
            tools=["memory"],
            worker_scope="user_agent",
        ),
        "shared_agent": AgentConfig(
            display_name="SharedAgent",
            tools=["shell"],
            worker_tools=["shell"],
            worker_scope="user",
            dedicated_subagent_workers=True,
            delegate_to=["shared_agent"],
        ),
        "shared_agent_2": AgentConfig(
            display_name="SharedAgent2",
            tools=["shell"],
            worker_tools=["shell"],
            worker_scope="user",
        ),
    }
    cfg = Config(
        agents=agents,
        models={"default": ModelConfig(provider="openai", id="gpt-test")},
        knowledge_bases={"kb1": KnowledgeBaseConfig(path="knowledge_docs")},
        memory=MemoryConfig(backend="file"),
        defaults=DefaultsConfig(tools=[], allow_shell_without_worker=False),
        **overrides,
    )
    bound = bind_runtime_paths(cfg, paths)
    for name in bound.agents:
        bound = with_responder_access(bound, name, users=("@alice:example.org",))
    return bound


# ---------------------------------------------------------------------------
# 1. Validation matrix, backend defense-in-depth, auth-before-FS & persona checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env_extra", "expected_error"),
    [
        ({"sandbox": False, "mount_dir": "repo"}, {}, "mount_dir always runs in sandbox mode"),
        ({"mount_read_only": True}, {}, "mount_read_only requires mount_dir"),
        ({"dedicated_worker": False, "sandbox": True}, {}, "sandbox requires dedicated_worker=true"),
        ({"dedicated_worker": False, "mount_dir": "repo"}, {}, "sandbox requires dedicated_worker=true"),
        ({"mount_dir": ".mindroom"}, {}, "hidden or dot-prefixed"),
        ({"mount_dir": ".git/hooks"}, {}, "hidden or dot-prefixed"),
        ({"mount_dir": "../escape"}, {}, "Invalid mount_dir"),
        ({"mount_dir": "."}, {}, "not the workspace root"),
        ({"mount_dir": "repo"}, {"MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME": "gvisor"}, "runtimeClassName"),
    ],
)
def test_sandbox_option_validation_matrix_refuses_invalid_combinations(
    tmp_path: Path,
    args: dict[str, object],
    env_extra: dict[str, str],
    expected_error: str,
) -> None:
    """Syntax, path-confinement, and RuntimeClass+mount_dir checks fail closed without touching disk."""
    paths = _paths(tmp_path, {**_K8S_ENV, **env_extra})
    parsed = parse_delegation_sandbox(args, runtime_paths=paths)
    assert isinstance(parsed, str)
    assert expected_error in parsed


def test_mount_dir_implies_sandbox_and_dedicated_and_scratch_allows_runtime_class(tmp_path: Path) -> None:
    """mount_dir implies sandbox=True+dedicated_worker=True, and empty scratch stays allowed under runtimeClassName."""
    paths = _paths(tmp_path, {**_K8S_ENV, "MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME": "gvisor"})
    scratch = parse_delegation_sandbox({"sandbox": True, "resource_profile": "small"}, runtime_paths=paths)
    assert scratch == (True, "small", WorkerSandboxConfig(mount_dir=None, mount_read_only=False))

    k8s_paths = _paths(tmp_path, _K8S_ENV)
    mounted = parse_delegation_sandbox({"mount_dir": "work/app", "mount_read_only": True}, runtime_paths=k8s_paths)
    assert mounted == (True, None, WorkerSandboxConfig(mount_dir="work/app", mount_read_only=True))


def test_non_kubernetes_backends_fail_closed_for_sandboxed_specs(tmp_path: Path) -> None:
    """Docker, local, and static_runner backends reject sandboxed specs directly in ensure_worker()."""
    sandbox_spec = WorkerSpec(_NORMAL_KEY, sandbox=WorkerSandboxConfig())

    with pytest.raises(WorkerBackendError, match="kubernetes"):
        _LocalWorkerBackend(
            worker_root=tmp_path / "local-workers",
            api_root="http://127.0.0.1:8766/api/sandbox-runner",
            idle_timeout_seconds=60.0,
        ).ensure_worker(sandbox_spec)
    with pytest.raises(WorkerBackendError, match="kubernetes"):
        StaticSandboxRunnerBackend(
            api_root="http://127.0.0.1:8766/api/sandbox-runner",
            auth_token="tok",  # noqa: S106
        ).ensure_worker(sandbox_spec)
    with pytest.raises(WorkerBackendError, match="kubernetes"):
        object.__new__(DockerWorkerBackend).ensure_worker(sandbox_spec)

    for env, needle in (
        (_DOCKER_ENV, "kubernetes"),
        ({}, "dedicated workers"),
        ({**_K8S_ENV, "MINDROOM_KUBERNETES_NAMED_WORKERS_MAX": "0"}, "named_workers_max is 0"),
    ):
        paths = _paths(tmp_path, env)
        cfg = _config(paths)
        res = resolve_fresh_target(
            {"task": "run", "sandbox": True},
            caller_name="leader",
            config=cfg,
            runtime_paths=paths,
            depth=0,
            workspace_root=lambda: tmp_path,
            cap=None,
        )
        assert isinstance(res, str)
        assert needle in res


@pytest.mark.asyncio
async def test_authorization_runs_before_mount_dir_filesystem_check_and_refuses_cross_agent_mount(
    tmp_path: Path,
) -> None:
    """Unauthorized targets never leak mount_dir existence on disk, and cross-agent mount_dir is refused."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "existing_dir").mkdir(parents=True, exist_ok=True)
    unauth_cfg = bind_runtime_paths(
        cfg.model_copy(
            update={
                "agents": {**cfg.agents, "leader": cfg.agents["leader"].model_copy(update={"delegate_to": ["helper"]})},
            },
        ),
        paths,
    )
    tools = DelegateTools(
        agent_name="leader",
        runtime_paths=paths,
        config=unauth_cfg,
        workspace_root=ws,
        execution_identity=_identity("leader"),
    )
    ctx = _delegate_runtime_context(unauth_cfg, paths, agent_name="leader")
    with tool_runtime_context(ctx):
        existing_res = await tools._prepare("run_subagent", {"task": "t", "mount_dir": "existing_dir"})
        missing_res = await tools._prepare("run_subagent", {"task": "t", "mount_dir": "nonexistent_dir"})
    assert isinstance(existing_res, str)
    assert existing_res == missing_res
    assert "does not exist" not in existing_res

    cross_mount = resolve_fresh_target(
        {"task": "t", "agent_name": "helper", "mount_dir": "existing_dir"},
        caller_name="leader",
        config=cfg,
        runtime_paths=paths,
        depth=0,
        workspace_root=lambda: ws,
        cap=None,
    )
    assert isinstance(cross_mount, str)
    assert "self-copy" in cross_mount

    cross_scratch = resolve_fresh_target(
        {"task": "t", "agent_name": "helper", "sandbox": True},
        caller_name="leader",
        config=cfg,
        runtime_paths=paths,
        depth=0,
        workspace_root=lambda: ws,
        cap=None,
    )
    assert isinstance(cross_scratch, DelegationTarget)
    assert cross_scratch.agent_name == "helper"
    assert cross_scratch.sandbox == WorkerSandboxConfig(mount_dir=None, mount_read_only=False)

    # DelegateTools schema advertises sandbox params only on K8s with dedicated workers + named_workers_max > 0.
    k8s_fn = next(
        fn
        for fn in adapt_toolkit(
            DelegateTools("leader", paths, cfg, ws, _identity("leader"), delegation_depth=0),
            key="delegate",
        ).functions
        if fn.name == "run_subagent"
    )
    assert {"sandbox", "mount_dir", "mount_read_only"} <= set(k8s_fn.parameters["properties"])
    docker_paths = _paths(tmp_path, _DOCKER_ENV)
    docker_cfg = _config(docker_paths)
    docker_fn = next(
        fn
        for fn in adapt_toolkit(
            DelegateTools("leader", docker_paths, docker_cfg, ws, _identity("leader"), delegation_depth=0),
            key="delegate",
        ).functions
        if fn.name == "run_subagent"
    )
    assert not ({"sandbox", "mount_dir", "mount_read_only"} & set(docker_fn.parameters["properties"]))


def test_sandbox_persona_and_profile_tool_skill_knowledge_enforcement(tmp_path: Path) -> None:
    """Persona & saved profiles enforce SANDBOX_ALLOWED_TOOLKITS, minimal shell-only, and zero skills/knowledge."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nbody\n")
    (ws / "subagents").mkdir(parents=True, exist_ok=True)
    (ws / "subagents" / "min_fs.md").write_text(
        "---\ndescription: minimal with fs\nmode: minimal\ntools: [shell, filesystem]\n---\nDo work.\n",
    )
    (ws / "subagents" / "with_skill.md").write_text(
        "---\ndescription: has skill\nskills: [demo]\n---\nDo work.\n",
    )

    def resolve(args: dict[str, object]) -> DelegationTarget | str:
        return resolve_fresh_target(
            {"task": "work", "sandbox": True, **args},
            caller_name="leader",
            config=cfg,
            runtime_paths=paths,
            depth=0,
            workspace_root=lambda: ws,
            cap=None,
        )

    assert isinstance(
        resolve({"system_prompt": "narrow", "tools": ["shell", "filesystem.read_file"]}),
        DelegationTarget,
    )
    for bad_tool in ("memory", "delegate", "matrix_message"):
        err = resolve({"system_prompt": "bad", "tools": ["shell", bad_tool]})
        assert isinstance(err, str)
        assert "not allowed in sandbox mode" in err
    assert "skills are not available" in str(resolve({"system_prompt": "s", "skills": ["demo"]}))
    assert "knowledge is not available" in str(resolve({"system_prompt": "k", "knowledge": ["kb1"]}))
    assert "skills are not available" in str(resolve({"profile": "with_skill"}))
    assert "not allowed in sandbox mode" in str(resolve({"profile": "min_fs"}))

    no_tools_err = validate_sandbox_execution(
        caller_name="leader",
        child_name="no_tools",
        config=cfg,
        runtime_paths=paths,
        execution_identity=_identity("no_tools"),
        sandbox=WorkerSandboxConfig(),
    )
    assert no_tools_err is not None
    assert "no worker-routed sandbox tools" in no_tools_err

    shared_err = validate_sandbox_execution(
        caller_name="shared_agent",
        child_name="shared_agent",
        config=cfg,
        runtime_paths=paths,
        execution_identity=_identity("shared_agent"),
        sandbox=WorkerSandboxConfig(mount_dir="repo"),
    )
    assert shared_err is not None
    assert "single agent workspace" in shared_err


# ---------------------------------------------------------------------------
# 2. Fail-closed dispatch, placement tamper refusal, & continuation refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_sandbox_proxy_dispatch_and_named_worker_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandboxed placement refuses named-worker overrides, mismatches, missing scope, invalid mount_dir, and output_path."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    identity = _identity("leader", session_id="sb-sess")
    sandbox = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
    placement = DedicatedWorkerPlacement("a" * 32, "small", sandbox)
    target = ResolvedWorkerTarget(
        worker_scope="user_agent",
        routing_agent_name="leader",
        execution_identity=identity,
        tenant_id=None,
        account_id=None,
        worker_key=_NORMAL_KEY,
        private_agent_names=frozenset(),
        placement=placement,
    )
    normal = WorkerSpec(_NORMAL_KEY, private_agent_names=frozenset(), mirror_grantable_credentials=True)

    spec = _placed_worker_spec(normal, placement=placement, named=None)
    assert spec.sandbox == sandbox
    assert spec.mirror_grantable_credentials is False
    assert spec.worker_key == subagent_worker_key(_NORMAL_KEY, "a" * 32)

    async with execution_resources(response=True, jobs_enabled=False):
        named = named_worker_call(
            "box",
            None,
            agent_name="leader",
            config=cfg,
            runtime_paths=paths,
            turn_id="turn-1",
        )
        assert named is not None
        with pytest.raises(NamedWorkerError, match="Named workers cannot be used inside a sandboxed subagent"):
            _placed_worker_spec(normal, placement=placement, named=named)

    with (
        dedicated_worker_placement("sb-sess", replace(placement, sandbox=replace(sandbox, mount_read_only=False))),
        pytest.raises(
            RuntimeError,
            match="does not match active session placement",
        ),
    ):
        _resolve_call_placement(target)

    with (
        dedicated_worker_placement("sb-sess", placement),
        pytest.raises(
            RuntimeError,
            match="session does not match active sandbox placement",
        ),
    ):
        _resolve_call_placement(
            replace(target, execution_identity=_identity("leader", session_id="other-sess"), placement=None),
        )

    with pytest.raises(RuntimeError, match="worker-scoped tool route"):
        _worker_route_with_sandbox(
            runtime_paths=paths,
            runtime_config=cfg,
            tool_name="shell",
            function_name="run_shell_command",
            worker_target=replace(target, worker_scope=None, worker_key=None),
            worker_manager=MagicMock(),
            base_dir=None,
            placement=placement,
        )

    backend = MagicMock()
    with pytest.raises(NamedWorkerError, match="does not exist"):
        _ensure_route_worker(
            spec,
            named=None,
            worker_manager=backend,
            runtime_paths=paths,
            runtime_config=cfg,
            private_agent_names=frozenset(),
            progress_sink=None,
            start=True,
        )
    backend.ensure_worker.assert_not_called()

    stand_ins = worker_stand_ins(
        "shell",
        object(),
        runtime_paths=paths,
        credentials_manager=None,
        worker_target=target,
        runtime_config=cfg,
        tools=builtin_tool_registry(),
    )
    with pytest.raises(NamedWorkerError, match="mindroom_output_path is not supported in a sandboxed worker"):
        _build_execute_request(
            stand_ins,
            worker_manager=backend,
            function_name="run_shell_command",
            wire_args=[],
            wire_kwargs={"args": "echo hi", "mindroom_output_path": "out.txt"},
        )
    backend.ensure_worker.assert_not_called()

    # Follow-up named worker call (omitting sandbox=) checks locked sandbox for mount validation + output_path refusal.
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    repo_dir = ws / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    unsandboxed_stand_ins = worker_stand_ins(
        "shell",
        object(),
        runtime_paths=paths,
        credentials_manager=None,
        worker_target=replace(target, placement=None),
        runtime_config=cfg,
        tools=builtin_tool_registry(),
    )
    async with execution_resources(response=True, jobs_enabled=False):
        call1 = named_worker_call(
            "box",
            None,
            sandbox=True,
            mount_dir="repo",
            agent_name="leader",
            config=cfg,
            runtime_paths=paths,
            turn_id="turn-locked",
        )
        assert call1 is not None
        call1._fleet.admit(call1, call1._fleet.worker_key(_NORMAL_KEY, "box"))
        call2 = named_worker_call(
            "box",
            None,
            agent_name="leader",
            config=cfg,
            runtime_paths=paths,
            turn_id="turn-locked",
        )
        assert call2 is not None
        with pytest.raises(NamedWorkerError, match="mindroom_output_path is not supported in a sandboxed worker"):
            _build_execute_request(
                unsandboxed_stand_ins,
                worker_manager=backend,
                function_name="run_shell_command",
                wire_args=[],
                wire_kwargs={"args": "echo hi", "mindroom_output_path": "out.txt"},
                named=call2,
            )
        repo_dir.rmdir()
        with pytest.raises(NamedWorkerError, match="does not exist"):
            _ensure_route_worker(
                call2.worker_spec(normal),
                named=call2,
                worker_manager=backend,
                runtime_paths=paths,
                runtime_config=cfg,
                private_agent_names=frozenset(),
                progress_sink=None,
                start=True,
            )
        backend.ensure_worker.assert_not_called()

    monkeypatch.setattr(
        sandbox_proxy_module,
        "_ensure_route_worker",
        MagicMock(return_value=(None, replace(spec, mirror_grantable_credentials=True))),
    )
    with pytest.raises(
        RuntimeError,
        match=r"^Sandboxed subagent worker spec does not match its required sandbox placement\.$",
    ):
        _build_execute_request(
            replace(stand_ins, expected_sandbox=sandbox),
            worker_manager=backend,
            function_name="run_shell_command",
            wire_args=[],
            wire_kwargs={"args": "echo hi"},
        )


@pytest.mark.asyncio
async def test_child_placement_reserve_scope_and_continuation_fail_closed_on_sandbox_tamper_or_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child_placement raises (never None), tampering fails reserve/scope checks, and continuation fails closed."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "repo").mkdir(parents=True, exist_ok=True)
    owner = _identity("leader")
    ro_sandbox = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
    child = prepare_child_turn(
        "leader",
        "leader",
        "audit",
        owner=owner,
        config=cfg,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
        dedicated_worker=True,
        worker_resource_profile="small",
        worker_sandbox=ro_sandbox,
    )
    assert child_placement(child, cfg, paths) == DedicatedWorkerPlacement(child.subagent_id, "small", ro_sandbox)

    disabled_paths = _paths(tmp_path, {**_K8S_ENV, "MINDROOM_KUBERNETES_NAMED_WORKERS_MAX": "0"})
    with pytest.raises(SandboxedSubagentUnavailableError, match="named_workers_max is 0"):
        child_placement(child, cfg, disabled_paths)

    await reserve_child_turn(child, owner=owner, runtime_paths=paths)
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        parent_delegation_id=None,
        config=cfg,
        runtime_paths=paths,
        caller_execution_identity=owner,
    )
    tampered_rw = replace(child, worker_sandbox=WorkerSandboxConfig(mount_dir="repo", mount_read_only=False))
    with pytest.raises(RuntimeError, match="placement no longer matches its retained session"):
        await reserve_child_turn(tampered_rw, owner=owner, runtime_paths=paths)

    target = DelegationTarget("leader", "audit", dedicated_worker=True, resource_profile="small", sandbox=ro_sandbox)
    with pytest.raises(RuntimeError, match="worker placement no longer matches its parent requirement"):
        delegation_execution._validate_child_scope(tampered_rw, target, caller_identity=owner, config=cfg, depth=0)

    assert DelegationChild.from_dict(asdict(child)).worker_sandbox == ro_sandbox
    with pytest.raises(TypeError, match="Invalid worker sandbox snapshot"):
        DelegationChild.from_dict({**asdict(child), "worker_sandbox": {"mount_dir": None, "mount_read_only": True}})

    # Background continuation after restart fails closed if mount_dir is removed.
    (ws / "repo").rmdir()
    fake_job = MagicMock(job_id="job-1", kind="delegation", owner=owner, depth=0, adapter={"child": asdict(child)})
    ctx = _delegate_runtime_context(cfg, paths, agent_name="leader", execution_identity=_identity("leader"))
    with tool_runtime_context(ctx):
        res = await continued_subagent_job(fake_job, runtime=MagicMock())
    assert isinstance(res, str)
    assert "does not exist" in res

    # continue_subagent fails closed on a completed sandboxed child when mount_dir is gone.
    assert ctx.execution_identity is not None
    ctx_owner = ctx.execution_identity
    (ws / "repo").mkdir(parents=True, exist_ok=True)
    done_child = prepare_child_turn(
        "leader",
        "leader",
        "audit-2",
        owner=ctx_owner,
        config=cfg,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
        dedicated_worker=True,
        worker_resource_profile="small",
        worker_sandbox=ro_sandbox,
    )
    await reserve_child_turn(done_child, owner=ctx_owner, runtime_paths=paths)
    await start_child_turn(
        done_child,
        parent_run_id="parent-run",
        parent_delegation_id=None,
        config=cfg,
        runtime_paths=paths,
        caller_execution_identity=ctx_owner,
    )
    await finish_child_turn(done_child, config=cfg, runtime_paths=paths, status="completed", reason="done")
    tools = DelegateTools("leader", paths, cfg, ws, ctx_owner, delegation_depth=0)
    # Revoking the self-copy grant refuses the follow-up without checking the retained mount.
    mount_resolver = MagicMock(wraps=delegation_workers.resolve_worker_sandbox_mount)
    with monkeypatch.context() as patch, tool_runtime_context(ctx):
        patch.setattr(cfg.agents["leader"], "delegate_to", ["helper"])
        patch.setattr(delegation_workers, "resolve_worker_sandbox_mount", mount_resolver)
        denied = await tools.continue_subagent(subagent_id=done_child.subagent_id, message="follow up")
    assert denied == "Cannot delegate to 'leader'. Allowed subagents: helper."
    mount_resolver.assert_not_called()

    (ws / "repo").rmdir()
    with tool_runtime_context(ctx):
        cont_res = await tools.continue_subagent(subagent_id=done_child.subagent_id, message="follow up")
    assert "does not exist" in cont_res


@pytest.mark.asyncio
async def test_execute_child_approval_resume_verifies_sandbox_placement_and_excludes_workflow_peers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_execute_child refuses mismatched/missing placement on approval resume and excludes workflow_peers + knowledge."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "repo").mkdir(parents=True, exist_ok=True)
    owner = _identity("leader")
    ro_sandbox = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
    child = prepare_child_turn(
        "leader",
        "leader",
        "resume-me",
        owner=owner,
        config=cfg,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
        dedicated_worker=True,
        worker_resource_profile="small",
        worker_sandbox=ro_sandbox,
    )
    child.workflow_run_id = "wf-run-1"
    child.workflow_node_key = "@0"
    child.workflow_generation = 1
    paused = RunRecord(run_id="paused-run", session_id=child.session_id, status=RunStatus.paused)
    observation = MagicMock(turn_id="child-turn-1")
    observation.subagent_cost = MagicMock()

    async def _fake_read(*_a: object, **_kw: object) -> RunRecord:
        return paused

    monkeypatch.setattr(delegation_execution, "read_child_run", _fake_read)
    called_toolkits: list[tuple[str, ...]] = []

    async def _capture_required(*_a: object, **kw: object) -> frozenset[str]:
        toolkits = kw.get("runtime_toolkits", ())
        called_toolkits.append(tuple(toolkits) if isinstance(toolkits, tuple) else ())
        msg = "sentinel-stop-after-required-check"
        raise StopAsyncIteration(msg)

    monkeypatch.setattr(delegation_execution, "required_approval_tool_names", _capture_required)
    ctx = _delegate_runtime_context(cfg, paths, agent_name="leader", execution_identity=owner)

    token = delegation_lifecycle._CHILD_RUN.set(observation)
    try:
        # 1. Missing placement refuses BEFORE required_approval_tool_names or tool rebuild runs.
        with (
            tool_runtime_context(ctx),
            pytest.raises(RuntimeError, match="does not match its retained child before approval resume"),
        ):
            await delegation_execution._execute_child(
                child,
                observation=observation,
                run_child=MagicMock(),
                config=cfg,
                runtime_paths=paths,
                refresh_scheduler=None,
                approval_calls=(),
                fresh=False,
                prompt=None,
            )
        assert called_toolkits == []

        # 2. Mismatched RW vs RO placement refuses BEFORE required_approval_tool_names runs.
        rw_placement = DedicatedWorkerPlacement(
            child.subagent_id,
            "small",
            WorkerSandboxConfig(mount_dir="repo", mount_read_only=False),
        )
        with (
            tool_runtime_context(ctx),
            dedicated_worker_placement(child.session_id, rw_placement),
            pytest.raises(RuntimeError, match="does not match its retained child before approval resume"),
        ):
            await delegation_execution._execute_child(
                child,
                observation=observation,
                run_child=MagicMock(),
                config=cfg,
                runtime_paths=paths,
                refresh_scheduler=None,
                approval_calls=(),
                fresh=False,
                prompt=None,
            )
        assert called_toolkits == []

        # 3. Matching RO placement passes gate and excludes workflow_peers even on a workflow child.
        ro_placement = DedicatedWorkerPlacement(child.subagent_id, "small", ro_sandbox)
        with (
            tool_runtime_context(ctx),
            dedicated_worker_placement(child.session_id, ro_placement),
            pytest.raises(StopAsyncIteration, match="sentinel-stop-after-required-check"),
        ):
            await delegation_execution._execute_child(
                child,
                observation=observation,
                run_child=MagicMock(),
                config=cfg,
                runtime_paths=paths,
                refresh_scheduler=None,
                approval_calls=(),
                fresh=False,
                prompt=None,
            )
        assert called_toolkits == [()]

    finally:
        delegation_lifecycle._CHILD_RUN.reset(token)


# ---------------------------------------------------------------------------
# 3. Allowlist prompt, trust boundary, runner filesystem confinement & minimal+sandbox
# ---------------------------------------------------------------------------


def test_sandbox_create_agent_uses_allowlist_prompt_and_strips_ambient_context(tmp_path: Path) -> None:
    """create_agent(..., worker_sandbox=...) exposes only shell/filesystem and strips all ambient prompt/memory/skills/KB."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("SECRET_AGENTS_MD_CONTENT")
    (ws / "SOUL.md").write_text("SECRET_SOUL_MD_CONTENT")
    (ws / "EXTRA_SECRET.md").write_text("SECRET_EXTRA_CONTEXT_FILE")
    (ws / "MEMORY.md").write_text("SECRET_MEMORY_MD_CONTENT")
    (ws / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: SECRET_SKILL_DESC\n---\nbody\n")
    (ws / "repo").mkdir(parents=True, exist_ok=True)

    build = create_agent(
        "leader",
        cfg,
        paths,
        _identity("leader"),
        knowledge=None,
        model=TurnModel("default", "default", None, False),
        provider=ScriptedModel(answer("ok")),
        plugins=config_plugins(cfg, paths),
        delegation_depth=1,
        worker_sandbox=WorkerSandboxConfig(mount_dir="repo", mount_read_only=True),
    )

    assert {fn.registered_toolkit for fn in build.spec.functions.values()} == {"shell", "filesystem"}
    assert build.tool_jobs is not None
    assert build.tool_jobs.inline_only is True
    assert set(build.tool_jobs.tools) == set(build.spec.functions)
    disabled_cfg = _config(paths, background_tool_jobs=BackgroundToolJobsConfig(enabled=False))
    disabled_build = create_agent(
        "leader",
        disabled_cfg,
        paths,
        _identity("leader"),
        knowledge=None,
        model=TurnModel("default", "default", None, False),
        provider=ScriptedModel(answer("ok")),
        plugins=config_plugins(disabled_cfg, paths),
        delegation_depth=1,
        worker_sandbox=WorkerSandboxConfig(mount_dir="repo", mount_read_only=True),
    )
    assert disabled_build.tool_jobs is None
    assert build.surface.cli_shell is False
    assert build.surface.named_workers is False
    assert build.surface.deferred == ()
    assert build.surface.skills == ()
    assert build.surface.knowledge_sources == ()
    assert build.surface.context_documents == ()
    assert build.surface.memory_root is None
    assert "worker" not in build.spec.functions["run_shell_command"].parameters["properties"]
    for fn in build.spec.functions.values():
        assert "mindroom_output_path" not in fn.parameters["properties"]

    prompt_text = system_text(prompt_for_turn(build))
    assert "mounted subdirectory `repo` (read-only)" in prompt_text
    assert "No credentials, memory, knowledge bases" in prompt_text
    assert build.surface.output_file_policy is None
    for leaked in (
        "TOP_SECRET_AGENT_ROLE",
        "TOP_SECRET_AGENT_INSTRUCTION",
        "SECRET_AGENTS_MD_CONTENT",
        "SECRET_SOUL_MD_CONTENT",
        "SECRET_EXTRA_CONTEXT_FILE",
        "SECRET_MEMORY_MD_CONTENT",
        "SECRET_SKILL_DESC",
        "http://localhost:8008",
        str(ws),
    ):
        assert leaked not in prompt_text


def _write_sandbox_runner_fixture(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    storage = tmp_path / "storage"
    config_path = tmp_path / "config.yaml"
    shared_dir = tmp_path / "outside_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "secret.txt").write_text("OUTSIDE_SHARED_SECRET", encoding="utf-8")
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-test\n"
        "defaults:\n"
        "  tool_output_auto_save_threshold_bytes: 32\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code\n"
        "    tools: [shell, filesystem]\n"
        "    worker_tools: [shell, filesystem]\n"
        "    worker_scope: user_agent\n"
        "    file_access: unrestricted\n"
        "router:\n"
        "  model: default\n",
        encoding="utf-8",
    )
    worker_key = subagent_worker_key("v1:default:user_agent:@alice:example.org:code", "b" * 32)
    ws_root = storage / "agents" / "code" / "workspace"
    repo_dir = ws_root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (ws_root / "parent_secret.txt").write_text("PARENT_WORKSPACE_SECRET", encoding="utf-8")
    (repo_dir / "inside.txt").write_text("INSIDE_REPO_OK", encoding="utf-8")
    worker_root = worker_root_path(storage, worker_key)
    worker_root.mkdir(parents=True, exist_ok=True)
    startup_env = {
        "PATH": os.environ.get("PATH", ""),
        "MINDROOM_CONFIG_PATH": str(config_path),
        "MINDROOM_STORAGE_PATH": str(worker_root),
        "MINDROOM_SANDBOX_PROXY_TOKEN": _RUNNER_TOKEN,
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_USE_SUBPROCESS": "false",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key,
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(worker_root),
        "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": str(storage),
    }
    runner_paths = resolve_runtime_paths(config_path=config_path, storage_path=worker_root, process_env=startup_env)
    manifest_path = write_startup_manifest(
        worker_root,
        runner_paths,
        sandbox_mount={"path": str(repo_dir), "ino": os.lstat(repo_dir).st_ino},
    )
    return worker_key, repo_dir, shared_dir, manifest_path


def test_sandbox_runner_confines_filesystem_tool_autosave_unprefixed_and_enforces_protocol_14(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner with sandbox=True enforces file_access='workspace', empty shared roots, unprefixed auto-save, and protocol 14."""
    worker_key, repo_dir, shared_dir, manifest_path = _write_sandbox_runner_fixture(tmp_path)

    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", _RUNNER_TOKEN)
    monkeypatch.setenv("MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("MINDROOM_WORKER_COMPUTER_ENABLED", raising=False)
    monkeypatch.setattr(sandbox_runner_app.state, "sandbox_runner_context", None, raising=False)

    base_payload = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "worker_key": worker_key,
        "worker_scope": "user_agent",
        "routing_agent_name": "code",
        "execution_identity": serialize_tool_execution_identity(_identity("code")),
        "private_agent_names": [],
        "base_dir": str(repo_dir),
        "tool_name": "filesystem",
        "function_name": "read_file",
        "sandbox": True,
    }
    headers = {"x-mindroom-sandbox-token": _RUNNER_TOKEN}
    try:
        with TestClient(sandbox_runner_app) as client:

            def execute(**overrides: object) -> dict[str, object]:
                response = client.post(
                    "/api/sandbox-runner/execute",
                    headers=headers,
                    json={**base_payload, **overrides},
                )
                assert response.status_code == 200
                return json.loads([line for line in response.text.splitlines() if line.strip()][-1])

            # Protocol 13 is refused by protocol 14 runner.
            stale = execute(protocol=13, kwargs={"path": "inside.txt"})
            assert stale["ok"] is False
            assert "protocol 13" in str(stale["error"])

            # Inside mount_dir succeeds.
            ok = execute(kwargs={"path": "inside.txt"})
            assert ok["ok"] is True
            assert "INSIDE_REPO_OK" in str(ok["result"])

            # Parent workspace and host paths outside mount_dir are REFUSED despite file_access: unrestricted.
            for escape_path in ("../parent_secret.txt", str(shared_dir / "secret.txt")):
                blocked = execute(kwargs={"path": escape_path})
                result_text = str(blocked.get("result") or blocked.get("error") or "")
                assert "PARENT_WORKSPACE_SECRET" not in result_text
                assert "OUTSIDE_SHARED_SECRET" not in result_text
                assert "Error" in result_text or blocked["ok"] is False

            # Auto-save inside writable mount_dir returns path relative to mount_dir (unprefixed) readable by filesystem!
            saved_meta = execute(
                tool_name="shell",
                function_name="run_shell_command",
                kwargs={"args": "python3 -c 'print(\"X\" * 120)'"},
            )["result"]["mindroom_tool_output"]  # type: ignore[index]
            assert saved_meta["status"] == "saved_to_file"
            assert not saved_meta["path"].startswith("repo/")
            read_saved = execute(kwargs={"path": saved_meta["path"]})
            assert read_saved["ok"] is True
            assert "XXXX" in str(read_saved["result"])
    finally:
        sandbox_runner_app.state.sandbox_runner_context = None


@pytest.mark.asyncio
async def test_minimal_sandbox_mints_zero_cli_token_and_omits_mindroom_agent(tmp_path: Path) -> None:
    """minimal=True + sandbox=True runs with orchestrator=None, zero CLI token, Bash-only prompt, while non-sandbox still fails closed."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    identity = _identity("leader", session_id="min-sb-sess")
    sandbox = WorkerSandboxConfig(mount_dir=None, mount_read_only=False)
    build = create_agent(
        "leader",
        cfg,
        paths,
        identity,
        knowledge=None,
        model=TurnModel("default", "default", None, False),
        provider=ScriptedModel(answer("ok")),
        plugins=config_plugins(cfg, paths),
        delegation_depth=1,
        agent_mode="minimal",
        worker_sandbox=sandbox,
    )

    assert build.surface.output_file_policy is None
    presented = presented_build(build)
    assert list(presented.spec.functions) == ["bash"]
    assert "mindroom-agent" not in (presented.spec.functions["bash"].description or "")

    turn = minimal_turn(
        build,
        session_preamble="LEAKED_MEMORY",
        transient_memory="LEAKED_TRANSIENT",
        system_enrichment=(),
        transient_enrichment=(),
    )
    sys_text = system_text(turn.system_messages)
    assert "isolated scratch directory" in sys_text
    assert "Bash is your only tool" in sys_text
    assert "mindroom-agent" not in sys_text
    assert "TOP_SECRET_AGENT_ROLE" not in sys_text
    assert "LEAKED_MEMORY" not in sys_text
    assert turn.context_documents == {}
    assert turn.transient_context == ""

    ctx = replace(
        _delegate_runtime_context(cfg, paths, agent_name="leader", execution_identity=identity),
        turn_id="turn-1",
        correlation_id="corr-1",
    )
    assert ctx.orchestrator is None
    session = ConversationSession(session_id=identity.session_id, kind="agent", owner_id="leader")
    storage = RunStorage(None, session, HistoryScope(kind="agent", scope_id="leader"))
    response_ctx = ResponseTurnContext(
        entity_label="leader",
        session_id=identity.session_id,
        run_id="run-1",
        correlation_id="corr-1",
        reply_to_event_id=None,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        matrix_run_metadata=None,
        execution_identity=identity,
        agent_mode="minimal",
        worker_sandbox=sandbox,
    )
    placement = DedicatedWorkerPlacement("c" * 32, None, sandbox)
    with tool_runtime_context(ctx), dedicated_worker_placement(identity.session_id, placement):
        async with response_cli_lifetime() as lifetime:
            spec, _hooks = await minimal_attempt(
                build,
                build.spec,
                LoopHooks(),
                run=RunContext(run_id="run-1", session=session, user_id=identity.requester_id),
                storage=storage,
                response_context=response_ctx,
                context_documents=turn.context_documents,
                delegation_depth=1,
                refresh_scheduler=None,
            )
            assert lifetime.owner is not None
            assert lifetime.owner.shell_env is None
            assert lifetime.grant_expires_at_ns is None
            assert "mindroom-agent" not in (spec.functions["bash"].description or "")

            ordinary = LiveTurnTools(
                lifetime.owner.owner,
                catalog=lifetime.owner.catalog,
                authorize=lifetime.owner._authorize,
                context={},
                output_file_policy=build.surface.output_file_policy,
                delegation_depth=1,
                refresh_scheduler=None,
                sandbox_only=False,
            )
            with pytest.raises(RuntimeError, match="Minimal Bash has no CLI environment"):
                await ordinary._invoke_shell(MagicMock(), {"args": "echo hi"}, window="w1")

            drifted = DedicatedWorkerPlacement(
                "d" * 32,
                None,
                WorkerSandboxConfig(mount_dir="repo", mount_read_only=True),
            )
            with (
                dedicated_worker_placement(identity.session_id, drifted),
                pytest.raises(PermissionError, match="Current authorization no longer permits"),
            ):
                await lifetime.owner._authorize(lifetime.owner, ToolKey("shell", "run_shell_command"), {})


# ---------------------------------------------------------------------------
# 4. K8s combined ephemeral pod cap & shorter idle expiry
# ---------------------------------------------------------------------------


def test_kubernetes_combined_ephemeral_sandbox_pod_cap_and_shorter_idle_expiry(tmp_path: Path) -> None:
    """Combined named + sandboxed subagent deployments enforce named_workers_max and use shorter idle timeouts."""
    backend, apps, _core = _named_backend(
        tmp_path,
        named_worker_idle_timeout_seconds=120.0,
        named_worker_max_age_seconds=600.0,
    )
    backend.config = replace(backend.config, named_workers_max=2)
    backend.named_workers_max = 2
    named1 = _named_spec("1")
    scope_key = named1.state_scope_worker_key
    assert scope_key is not None
    sb_key1 = subagent_worker_key(scope_key, "a" * 32)
    sb_key2 = subagent_worker_key(scope_key, "b" * 32)
    plain_sub_key = subagent_worker_key(scope_key, "c" * 32)
    sb_spec1 = replace(_k8s_spec(sb_key1, state_scope_worker_key=scope_key), sandbox=WorkerSandboxConfig())
    sb_spec2 = replace(_k8s_spec(sb_key2, state_scope_worker_key=scope_key), sandbox=WorkerSandboxConfig())
    plain_sub = _k8s_spec(plain_sub_key, state_scope_worker_key=scope_key)

    backend.ensure_worker(named1, now=10.0)
    backend.ensure_worker(sb_spec1, now=10.0)
    with pytest.raises(WorkerBackendError, match=r"Cannot start sandboxed worker.*limit of 2 ephemeral"):
        backend.ensure_worker(sb_spec2, now=10.0)
    with pytest.raises(WorkerBackendError, match=r"Cannot start named worker.*limit of 2 ephemeral"):
        backend.ensure_worker(_named_spec("2"), now=10.0)
    backend.ensure_worker(plain_sub, now=10.0)

    # In-flight sandboxed start also counts toward combined cap.
    backend.retire_worker(named1.worker_key)
    entered, release = threading.Event(), threading.Event()
    orig_apply = backend._resources.apply_deployment

    def blocking_apply(*args: object, **kwargs: object) -> object:
        if kwargs.get("worker_key") == sb_key2:
            entered.set()
            assert release.wait(timeout=5.0)
        return orig_apply(*args, **kwargs)

    backend._resources.apply_deployment = blocking_apply  # type: ignore[method-assign]
    t = threading.Thread(target=lambda: backend.ensure_worker(sb_spec2, now=20.0))
    t.start()
    assert entered.wait(timeout=5.0)
    try:
        with pytest.raises(WorkerBackendError, match="limit of 2 ephemeral"):
            backend.ensure_worker(_named_spec("3"), now=20.0)
    finally:
        release.set()
        t.join(timeout=5.0)
    backend._resources.apply_deployment = orig_apply  # type: ignore[method-assign]

    # Shorter idle expiry stops & retires sandboxed subagents while leaving unsandboxed subagent alive.
    cleaned_keys = {h.worker_key for h in backend.cleanup_idle_workers(now=140.0)}
    assert {sb_key1, sb_key2} <= cleaned_keys
    assert plain_sub_key not in cleaned_keys
    backend.cleanup_idle_workers(now=141.0)
    remaining = {(dep.metadata.annotations or {}).get("mindroom.ai/worker-key") for dep in apps.deployments.values()}
    assert sb_key1 not in remaining
    assert sb_key2 not in remaining
    assert plain_sub_key in remaining


# ---------------------------------------------------------------------------
# 5. Workflows: SDK digests, peer/inbox closure, load_follow_up_child, fenced retirement
# ---------------------------------------------------------------------------


def test_workflow_sdk_agent_options_preserve_digests_and_forward_contradictions() -> None:
    """Default options stay omitted (preserving replay digests), while invalid combinations travel to the engine."""
    blank = dict.fromkeys(workflow_sdk._SHARED_OPTIONS) | {"minimal": False}
    assert workflow_sdk._agent_options("agent", {**blank, "name": None}, {}, extra=frozenset({"name"})) == {}
    assert workflow_sdk._agent_options("spawn", {**blank, "dedicated_worker": False}, {}) == {}
    assert workflow_sdk._agent_options("agent", {**blank, "dedicated_worker": False, "sandbox": True}, {}) == {
        "dedicated_worker": False,
        "sandbox": True,
    }
    assert workflow_sdk._agent_options("agent", {**blank, "sandbox": False, "mount_dir": "repo"}, {}) == {
        "sandbox": False,
        "mount_dir": "repo",
    }


def _enable_sandbox_on_stack(stack: WorkflowStack) -> None:
    """Configure workflow stack's watcher agent for Kubernetes sandboxed subagents."""
    new_paths = replace(
        stack.context.runtime_paths,
        process_env={**stack.context.runtime_paths.process_env, **_K8S_ENV},
    )
    watcher = AgentConfig.model_validate(
        {
            **stack.context.config.agents["watcher"].model_dump(),
            "dedicated_subagent_workers": True,
            "worker_scope": "user_agent",
            "tools": ["counter", "shell", "filesystem", "workflow"],
            "worker_tools": ["shell", "filesystem"],
        },
    )
    new_cfg = bind_runtime_paths(
        stack.context.config.model_copy(update={"agents": {**stack.context.config.agents, "watcher": watcher}}),
        new_paths,
    )
    stack.context = replace(stack.context, config=new_cfg, runtime_paths=new_paths)
    stack.runtime_paths = new_paths
    stack.engine.agents = lambda: agent_nodes.AgentRuntime(stack.jobs, new_cfg, new_paths)
    ws = agent_workspace_root_path(new_paths.storage_root, "watcher")
    (ws / "repo").mkdir(parents=True, exist_ok=True)


@pytest.mark.asyncio
async def test_workflow_sandboxed_agent_fenced_retirement_and_schema_retry_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot workflow.agent persists worker_sandbox, strips peer tools, fences retirement, and fails closed on schema retry."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    retired: list[str] = []
    monkeypatch.setattr(
        agent_nodes,
        "schedule_subagent_worker_retirement",
        lambda _paths, worker_key: retired.append(worker_key),
    )

    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        _enable_sandbox_on_stack(stack)
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()

        # 1. One-shot workflow.agent(..., sandbox=True): persists worker_sandbox, strips WorkflowPeerTools, retires on settle.
        model1 = stack.child_script(answer("one-shot-done"))
        assert (
            await stack.result("@0", "agent", {"prompt": "Run checks", "sandbox": True}, meta={"name": "box1"})
            == "one-shot-done"
        )
        child_a1 = delegation_child(stack.node_job("@0"))
        assert child_a1.worker_sandbox == WorkerSandboxConfig(mount_dir=None, mount_read_only=False)
        assert not any(
            tool.name in {"send_peer_message", "read_peer_messages", "get_shared_state", "set_shared_state", "ask_user"}
            for tool in model1.requests[0].tools
        )
        expected_a1_key = subagent_sandbox_worker_key(child_a1, stack.context.config, stack.context.runtime_paths)
        assert expected_a1_key is not None
        await stack.until(lambda: retired == [expected_a1_key])

        # 2. Fenced retirement: when settle_node raises SupersededAttemptError, pod is NOT retired.
        retired.clear()
        orig_settle = stack.engine.settle_node

        async def superseded_settle(*_a: object, **_kw: object) -> bool:
            msg = "stale epoch"
            raise SupersededAttemptError(msg)

        monkeypatch.setattr(stack.engine, "settle_node", superseded_settle)
        node_a1 = stack.engine.store.get_node(stack.run_id, "@0")
        assert node_a1 is not None
        await agent_nodes._settle_turn(stack.engine, stack.engine.store.get_run(stack.run_id), node_a1, 1, block=False)
        assert retired == []
        monkeypatch.setattr(stack.engine, "settle_node", orig_settle)

        # 3. Schema retry on sandboxed workflow.agent fails closed if mount_dir is deleted before retry.
        repo_dir = agent_workspace_root_path(stack.context.runtime_paths.storage_root, "watcher") / "repo"
        model_schema = stack.child_script(answer("not-json"))
        orig_correct = agent_nodes._correct

        async def _remove_repo_before_retry(*a: object, **kw: object) -> object:
            if repo_dir.exists():
                repo_dir.rmdir()
            return await orig_correct(*a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(agent_nodes, "_correct", _remove_repo_before_retry)
        with pytest.raises(NodeFailed) as failed_schema:
            await stack.result(
                "@1",
                "agent",
                {
                    "prompt": "Return JSON",
                    "sandbox": True,
                    "mount_dir": "repo",
                    "schema": {"type": "object", "required": ["ok"]},
                },
                meta={"name": "schema-box"},
            )
        assert failed_schema.value.kind == "refused"
        assert "does not exist" in str(failed_schema.value)
        assert len(model_schema.requests) == 1
        monkeypatch.setattr(agent_nodes, "_correct", orig_correct)
        await stack.until(lambda: len(retired) == 1)
        await stack.close()


@pytest.mark.asyncio
async def test_workflow_sandboxed_spawn_peer_closure_refusal_and_end_run_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workflow.spawn sandboxed sessions skip direct & broadcast peer mail, refuse rebuild on lost mount, and retire once on end_run."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    retired: list[str] = []
    monkeypatch.setattr(
        agent_nodes,
        "schedule_subagent_worker_retirement",
        lambda _paths, worker_key: retired.append(worker_key),
    )

    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        _enable_sandbox_on_stack(stack)
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()

        model_s = stack.child_script(answer("s1"), answer("s2"))
        await stack.result("@0", "session", {"name": "sb", "sandbox": True, "mount_dir": "repo"}, meta={"name": "sb"})
        assert (
            await stack.result("@1", "turn", {"session": "@0", "turn": 1, "message": "Turn one"}, meta={"name": "sb"})
            == "s1"
        )
        send(
            stack.engine.store,
            stack.run_id,
            op_id="m1",
            sender=NodeRef("script", 1, "script"),
            to="sb",
            text="SECRET_DIRECT_MAIL",
        )
        send(
            stack.engine.store,
            stack.run_id,
            op_id="m2",
            sender=NodeRef("script", 1, "script"),
            to="*",
            text="SECRET_BROADCAST_MAIL",
        )
        assert (
            await stack.result("@2", "turn", {"session": "@0", "turn": 2, "message": "Turn two"}, meta={"name": "sb"})
            == "s2"
        )
        turn2_messages = json.dumps([m.content for m in model_s.requests[1].messages])
        assert "SECRET_DIRECT_MAIL" not in turn2_messages
        assert "SECRET_BROADCAST_MAIL" not in turn2_messages
        assert retired == []
        child_s1 = delegation_child(stack.node_job("@1"))
        expected_s_key = subagent_sandbox_worker_key(child_s1, stack.context.config, stack.context.runtime_paths)
        assert expected_s_key is not None

        # load_follow_up_child refuses rebuild when storage scope changes (NEVER silently switches workspaces!).
        original_scope = stack.context.config.agents["watcher"].worker_scope
        stack.context.config.agents["watcher"].worker_scope = "user"
        try:
            with pytest.raises(NodeFailed) as failed_scope:
                await stack.result(
                    "@3",
                    "turn",
                    {"session": "@0", "turn": 3, "message": "Turn scope"},
                    meta={"name": "sb"},
                )
            assert failed_scope.value.kind == "refused"
            assert "storage scope changed" in str(failed_scope.value)
            assert len(model_s.requests) == 2
        finally:
            stack.context.config.agents["watcher"].worker_scope = original_scope

        # Authorized turn preparation refuses a deleted mount rather than silently rebuilding fresh.
        repo_dir = agent_workspace_root_path(stack.context.runtime_paths.storage_root, "watcher") / "repo"
        repo_dir.rmdir()
        with pytest.raises(NodeFailed) as failed_turn3:
            await stack.result("@4", "turn", {"session": "@0", "turn": 3, "message": "Turn three"}, meta={"name": "sb"})
        assert failed_turn3.value.kind == "refused"
        assert "does not exist" in str(failed_turn3.value)
        assert len(model_s.requests) == 2

        # Terminal end_run retires workflow.spawn pod ONCE (deduped across turns 1 & 2).
        await stack.engine.end_run(stack.engine.store.get_run(stack.run_id), WorkflowRunState.COMPLETED, None)
        assert retired == [expected_s_key]
        await stack.close()


@pytest.mark.asyncio
async def test_workflow_engine_close_retires_spawn_sandbox_pod_surviving_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workflow.spawn sandboxed worker resources survive _gc between turns and retire on engine.close()."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    retired: list[str] = []
    monkeypatch.setattr(
        agent_nodes,
        "schedule_subagent_worker_retirement",
        lambda _paths, worker_key: retired.append(worker_key),
    )
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        _enable_sandbox_on_stack(stack)
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        stack.child_script(answer("ok"))
        await stack.result("@0", "session", {"name": "sb", "sandbox": True}, meta={"name": "sb"})
        assert (
            await stack.result("@1", "turn", {"session": "@0", "turn": 1, "message": "hi"}, meta={"name": "sb"}) == "ok"
        )
        child = delegation_child(stack.node_job("@1"))
        expected_key = subagent_sandbox_worker_key(child, stack.context.config, stack.context.runtime_paths)
        assert expected_key is not None
        stack.engine._gc(stack.run_id)
        assert stack.run_id in stack.engine._runs
        assert retired == []
        await stack.close()
        assert retired == [expected_key]


@pytest.mark.asyncio
async def test_non_matrix_and_detached_delegate_hides_and_refuses_minimal_while_allowing_standard_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Matrix/detached DelegateTools hides minimal, refuses minimal=True before creating a child, and allows standard sandbox."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "subagents").mkdir(parents=True, exist_ok=True)
    (ws / "subagents" / "min_only.md").write_text(
        "---\ndescription: minimal sandbox profile\nmode: minimal\ntools: [shell]\n---\nRun bash.\n",
    )

    def offered_fn(identity: ToolExecutionIdentity | None) -> object:
        toolkit = DelegateTools("leader", paths, cfg, ws, identity, delegation_depth=0)
        return next(fn for fn in adapt_toolkit(toolkit, key="delegate").functions if fn.name == "run_subagent")

    # Matrix caller with K8s sandbox workers still sees minimal + sandbox options.
    matrix_fn = offered_fn(_identity("leader"))
    assert {"minimal", "sandbox", "mount_dir", "mount_read_only"} <= set(matrix_fn.parameters["properties"])  # type: ignore[attr-defined]
    assert "minimal=true" in (matrix_fn.description or "")  # type: ignore[attr-defined]

    # Non-Matrix (OpenAI-compat) and identity-less DelegateTools hide minimal, while still advertising sandbox.
    openai_identity = replace(
        _identity("leader"),
        channel="openai_compat",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )
    for caller_identity in (openai_identity, None):
        fn = offered_fn(caller_identity)
        assert "minimal" not in fn.parameters["properties"]  # type: ignore[attr-defined]
        assert "minimal=true" not in (fn.description or "")  # type: ignore[attr-defined]
        assert {"sandbox", "mount_dir", "mount_read_only"} <= set(fn.parameters["properties"])  # type: ignore[attr-defined]

    # Detached / OpenAI-compat execution refuses minimal=True and mode:minimal profiles BEFORE creating any child.
    helper_model = ScriptedModel([answer("standard sandbox ok")])
    ScriptedAgents({"leader": ScriptedModel([]), "helper": helper_model}).install(monkeypatch)
    detached_tools = DelegateTools(
        "leader",
        paths,
        cfg,
        ws,
        openai_identity,
        delegation_depth=0,
        delegation_executor=default_delegation_executor(),
    )
    sessions_dir = paths.storage_root / "subagent_sessions"

    authority = DetachedRequesterContext(
        requester_id="@alice:example.org",
        config=cfg,
        runtime_paths=paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        config_provider=lambda: cfg,
        budget_monitor=None,
    )
    with detached_requester_context(authority), detached_tool_scope(openai_identity):
        refused_explicit = await detached_tools.run_subagent(
            task="Try minimal",
            agent_name="helper",
            sandbox=True,
            minimal=True,
        )
        assert "Cannot delegate: minimal mode requires an active Matrix conversation context" in refused_explicit

        refused_profile = await detached_tools.run_subagent(
            task="Try minimal profile",
            sandbox=True,
            profile="min_only",
        )
        assert "Cannot delegate: minimal mode requires an active Matrix conversation context" in refused_profile

        native_refused = await resolve_delegation_target(
            ToolExecution(
                tool_call_id="c1",
                tool_name="run_subagent",
                tool_args={"task": "Try minimal", "agent_name": "helper", "sandbox": True, "minimal": True},
            ),
            retained=None,
            caller_identity=openai_identity,
            config=cfg,
            runtime_paths=paths,
            depth=0,
        )
        assert isinstance(native_refused, str)
        assert "minimal mode requires an active Matrix conversation context" in native_refused

        # Zero child sessions or model turns were created by any minimal refusal.
        assert not sessions_dir.exists() or list(sessions_dir.glob("*.json")) == []
        assert helper_model.requests == []

        # Standard (minimal=False) sandbox=True subagent STILL works cleanly on OpenAI-compat!
        ok_result = await detached_tools.run_subagent(
            task="Run standard sandbox",
            agent_name="helper",
            sandbox=True,
        )
        assert "standard sandbox ok" in ok_result
        assert len(helper_model.requests) == 1

    # Even a Matrix-channel identity running in detached_tool_scope (get_tool_runtime_context() is None) is refused.
    matrix_identity = _identity("leader")
    with detached_tool_scope(matrix_identity):
        refused_detached_matrix = await resolve_delegation_target(
            ToolExecution(
                tool_call_id="c2",
                tool_name="run_subagent",
                tool_args={"task": "Try minimal detached", "agent_name": "helper", "sandbox": True, "minimal": True},
            ),
            retained=None,
            caller_identity=matrix_identity,
            config=cfg,
            runtime_paths=paths,
            depth=0,
        )
        assert isinstance(refused_detached_matrix, str)
        assert "minimal mode requires an active Matrix conversation context" in refused_detached_matrix


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settlement",
    [
        WorkflowRunState.COMPLETED,
        WorkflowRunState.FAILED,
        WorkflowRunState.CANCELLED,
        WorkflowRunState.INTERRUPTED,
        "close",
    ],
    ids=["completed", "failed", "cancelled", "interrupted", "engine-close"],
)
async def test_workflow_spawn_replay_to_starting_preserves_live_sandbox_pod_until_terminal_or_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settlement: WorkflowRunState | str,
) -> None:
    """Replaying an attempt to STARTING while a spawn() job is live preserves its sandbox pod until terminal settlement or close."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    retired: list[str] = []
    monkeypatch.setattr(
        agent_nodes,
        "schedule_subagent_worker_retirement",
        lambda _paths, worker_key: retired.append(worker_key),
    )

    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        _enable_sandbox_on_stack(stack)
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()

        started = asyncio.Event()
        gate = asyncio.Event()

        async def _hold_turn() -> None:
            started.set()
            await gate.wait()

        stack.child_script([Await(_hold_turn), *answer("survived replay")])
        await stack.result("@0", "session", {"name": "sb", "sandbox": True}, meta={"name": "sb"})
        await stack.submit("@1", "turn", {"session": "@0", "turn": 1, "message": "Long turn"}, meta={"name": "sb"})
        await asyncio.wait_for(started.wait(), timeout=5)

        child = delegation_child(stack.node_job("@1"))
        expected_key = subagent_sandbox_worker_key(child, stack.context.config, stack.context.runtime_paths)
        assert expected_key is not None
        assert stack.node_job("@1").status == "running"

        # Replay attempt to STARTING while the spawn() job is still live: sandbox pod MUST NOT retire!
        run = stack.engine.store.get_run(stack.run_id)
        replayed = await stack.engine.end_run(
            run,
            WorkflowRunState.STARTING,
            None,
            finished_at=None,
            previous_source_digest=run.source_digest,
        )
        assert replayed.state is WorkflowRunState.STARTING
        assert retired == []
        assert stack.run_id in stack.engine._runs
        assert stack.node_job("@1").status == "running"

        if settlement == "close":
            await stack.close()
            assert retired == [expected_key]
            assert stack.run_id not in stack.engine._runs
            return

        if settlement is WorkflowRunState.COMPLETED:
            # Relaunch next attempt, let the surviving spawn() job finish, then settle COMPLETED.
            launched = await stack.engine.launch(stack.run_id, stack.context)
            assert launched.state is WorkflowRunState.RUNNING
            gate.set()
            assert (
                await stack.result(
                    "@1",
                    "turn",
                    {"session": "@0", "turn": 1, "message": "Long turn"},
                    meta={"name": "sb"},
                )
                == "survived replay"
            )
            assert retired == []
            await stack.engine.end_run(stack.engine.store.get_run(stack.run_id), WorkflowRunState.COMPLETED, None)
        else:
            assert isinstance(settlement, WorkflowRunState)
            await stack.engine.end_run(stack.engine.store.get_run(stack.run_id), settlement, f"ended:{settlement}")

        assert retired == [expected_key]
        assert stack.run_id not in stack.engine._runs
        await stack.close()


# ---------------------------------------------------------------------------
# 6. Round 3 regressions: sandbox tool_jobs live revocation, fail-closed toolkit
#    dispatch without active placement, and locked-worker option helper
# ---------------------------------------------------------------------------


def _record_sandbox_proxy_requests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Patch sandbox_proxy dispatch to record worker requests without touching Kubernetes."""
    worker_requests: list[tuple[str, str]] = []
    backend = MagicMock()
    backend.ensure_worker.side_effect = lambda spec, **_kwargs: _k8s_spec(spec.worker_key)

    class _Lease:
        def __enter__(self) -> MagicMock:
            return backend

        def __exit__(self, *_exc: object) -> None:
            return None

    def _fake_execute(**kwargs: object) -> str:
        req = kwargs["request"]
        worker_requests.append((req.tool_name, req.function_name))
        return f"ran:{req.function_name}"

    monkeypatch.setattr(sandbox_proxy_module, "lease_worker_manager", lambda *_args, **_kwargs: _Lease())
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", _fake_execute)
    return worker_requests


async def _start_sandbox_child_job_coordinator(
    tmp_path: Path,
) -> tuple[
    RuntimePaths,
    list[Config],
    ToolJobRuntimeCoordinator,
    ToolExecutionIdentity,
    DelegationChild,
    ToolExecutionIdentity,
]:
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths, background_tool_jobs=BackgroundToolJobsConfig(enabled=True))
    config_box = [cfg]
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config_box[0],
        bot_provider=lambda _name: None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=lambda: tool_job_journal(paths.storage_root),
    )
    await coordinator.initialize()
    register_background_runtime(paths, coordinator.runtime)
    caller_identity = replace(_identity("leader", session_id="parent-sess-1"), transport_agent_name="leader")
    child = prepare_child_turn(
        "leader",
        "helper",
        "Run sandboxed check",
        owner=caller_identity,
        config=cfg,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
        dedicated_worker=True,
        worker_sandbox=WorkerSandboxConfig(),
    )
    await start_child_turn(
        child,
        parent_run_id="parent-run-1",
        config=cfg,
        runtime_paths=paths,
        caller_execution_identity=caller_identity,
    )
    return paths, config_box, coordinator, caller_identity, child, child_execution_identity(child)


@pytest.mark.asyncio
async def test_sandbox_subagent_tool_jobs_enforces_live_revocation_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard sandboxed children retain JobActor authorization middleware while keeping all calls inline."""
    paths, config_box, coordinator, caller_identity, child, child_identity = await _start_sandbox_child_job_coordinator(
        tmp_path,
    )
    cfg = config_box[0]
    sandbox = WorkerSandboxConfig()
    child_context = replace(
        _delegate_runtime_context(cfg, paths, execution_identity=child_identity),
        agent_name="helper",
        transport_agent_name="leader",
        plugins=config_plugins(cfg, paths),
    )
    build_kwargs = {
        "knowledge": None,
        "model": TurnModel("default", "default", None, False),
        "provider": ScriptedModel(answer("ok")),
        "plugins": config_plugins(cfg, paths),
        "worker_sandbox": sandbox,
    }
    child_build = create_agent("helper", cfg, paths, child_identity, delegation_depth=1, **build_kwargs)
    depth0_build = create_agent("helper", cfg, paths, child_identity, delegation_depth=0, **build_kwargs)
    assert child_build.tool_jobs is not None
    assert child_build.tool_jobs.inline_only is True
    assert depth0_build.tool_jobs is not None
    assert depth0_build.tool_jobs.inline_only is True
    assert {fn.registered_toolkit for fn in child_build.spec.functions.values()} == {"shell", "filesystem"}
    worker_requests = _record_sandbox_proxy_requests(monkeypatch)
    call_ctx = ToolCallContext(run_id="run-1", call_id="call-1", session_id=child_identity.session_id, user_id=None)

    try:
        async with (
            execution_resources(response=True, jobs_enabled=True),
            child_run_context(child, config=cfg, runtime_paths=paths),
        ):
            with tool_runtime_context(child_context):
                shell_tool = child_build.tool_jobs.tools["run_shell_command"]
                assert wait_mode(shell_tool, depth=0, context=child_context, inline_only=True) == "inline"
                projected = offered_definitions(
                    depth0_build.tool_jobs,
                    PreparedTools(functions=depth0_build.spec.functions).specs(),
                )
                assert all("background_after" not in spec.parameters.get("properties", {}) for spec in projected)

                # Authorized calls stay inline (both depth=1 and depth=0) with zero background jobs created.
                for build, call_id in ((child_build, "call-1"), (depth0_build, "call-depth0")):
                    ok = await run_tool_call(
                        build.spec.functions["run_shell_command"],
                        {"args": "echo ok"},
                        replace(call_ctx, call_id=call_id),
                        middleware=agent_tool_middleware(build),
                    )
                    assert ok.succeeded, ok.error
                    assert ok.value == "ran:run_shell_command"
                assert await coordinator.runtime.list_jobs(owner=caller_identity, depth=0) == []

                bad_wait = await run_tool_call(
                    depth0_build.spec.functions["run_shell_command"],
                    {"args": "echo no", "background_after": 1},
                    call_ctx,
                    middleware=agent_tool_middleware(depth0_build),
                )
                assert not bad_wait.succeeded
                assert isinstance(bad_wait.error, ValueError)
                assert len(worker_requests) == 2

                # Revoke shell from helper while leaving leader -> helper delegation authorized.
                revoked_cfg = cfg.model_copy(deep=True)
                revoked_cfg.agents["helper"].tools = ["filesystem"]
                revoked_cfg.agents["helper"].worker_tools = ["filesystem"]
                config_box[0] = revoked_cfg

                refused = await run_tool_call(
                    child_build.spec.functions["run_shell_command"],
                    {"args": "echo revoked"},
                    replace(call_ctx, call_id="call-2"),
                    middleware=agent_tool_middleware(child_build),
                )
                assert not refused.succeeded
                assert isinstance(refused.error, JobAccessError)
                assert "no longer authorized" in str(refused.error)
                assert len(worker_requests) == 2

                # Still-authorized filesystem tool continues to execute inline.
                still_ok = await run_tool_call(
                    child_build.spec.functions["read_file"],
                    {"path": "notes.txt"},
                    replace(call_ctx, call_id="call-3"),
                    middleware=agent_tool_middleware(child_build),
                )
                assert still_ok.succeeded, still_ok.error
                assert still_ok.value == "ran:read_file"
                assert len(worker_requests) == 3
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_sandbox_built_toolkit_fails_closed_without_active_sandbox_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct invocation of sandbox-built tools fails closed without matching active sandbox placement."""
    paths = _paths(tmp_path, _K8S_ENV)
    cfg = _config(paths)
    ws = agent_workspace_root_path(paths.storage_root, "leader")
    (ws / "repo").mkdir(parents=True, exist_ok=True)
    expected_sandbox = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
    identity = _identity("leader", session_id="sb-direct-sess")

    build = create_agent(
        "leader",
        cfg,
        paths,
        identity,
        knowledge=None,
        model=TurnModel("default", "default", None, False),
        provider=ScriptedModel(answer("ok")),
        plugins=config_plugins(cfg, paths),
        delegation_depth=1,
        worker_sandbox=expected_sandbox,
    )
    run_shell = build.spec.functions["run_shell_command"]
    read_file = build.spec.functions["read_file"]

    lease_calls: list[object] = []
    proxy_calls: list[object] = []

    def _forbidden_lease(*args: object, **kwargs: object) -> object:
        lease_calls.append((args, kwargs))
        msg = "Normal worker must never be leased when sandbox placement is missing"
        raise AssertionError(msg)

    def _forbidden_execute(*args: object, **kwargs: object) -> object:
        proxy_calls.append((args, kwargs))
        msg = "Normal worker must never receive a request when sandbox placement is missing"
        raise AssertionError(msg)

    monkeypatch.setattr(sandbox_proxy_module, "lease_worker_manager", _forbidden_lease)
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", _forbidden_execute)

    # 1. No dedicated_worker_placement bound at all -> both shell and filesystem fail closed.
    with pytest.raises(RuntimeError, match="requires an active sandbox placement"):
        await run_shell.entrypoint(args="echo should-not-run")
    with pytest.raises(RuntimeError, match="requires an active sandbox placement"):
        await read_file.entrypoint(path="secret.txt")

    # 2. Dedicated placement without sandbox -> fails closed.
    with (
        dedicated_worker_placement(identity.session_id, DedicatedWorkerPlacement(subagent_id="sub-1", sandbox=None)),
        pytest.raises(RuntimeError, match="requires an active sandbox placement"),
    ):
        await run_shell.entrypoint(args="echo non-sandbox")

    # 3. Active sandbox placement that disagrees with the toolkit's expected WorkerSandboxConfig -> fails closed.
    mismatched_sandbox = WorkerSandboxConfig(mount_dir="other", mount_read_only=False)
    with (
        dedicated_worker_placement(
            identity.session_id,
            DedicatedWorkerPlacement(subagent_id="sub-1", sandbox=mismatched_sandbox),
        ),
        pytest.raises(RuntimeError, match="does not match the toolkit's expected sandbox"),
    ):
        await read_file.entrypoint(path="secret.txt")

    # 4. save_attachment_to_worker / view_file_from_worker also fail closed when sandbox_required=True.
    target = ResolvedWorkerTarget(
        worker_scope="user_agent",
        routing_agent_name="leader",
        execution_identity=identity,
        tenant_id="default",
        account_id=identity.requester_id,
        worker_key=_NORMAL_KEY,
        private_agent_names=frozenset(),
    )
    with pytest.raises(RuntimeError, match="requires an active sandbox placement"):
        save_attachment_to_worker(
            runtime_paths=paths,
            runtime_config=cfg,
            worker_target=target,
            workspace_root=ws,
            attachment_id="att-1",
            mindroom_output_path="out.bin",
            payload_bytes=b"abc",
            sandbox_required=True,
            expected_sandbox=expected_sandbox,
        )
    with pytest.raises(RuntimeError, match="requires an active sandbox placement"):
        view_file_from_worker(
            runtime_paths=paths,
            runtime_config=cfg,
            worker_target=target,
            workspace_root=ws,
            path="img.png",
            sandbox_required=True,
            expected_sandbox=expected_sandbox,
        )

    # 5. Primary-placement toolkit behind sandbox_required=True fails closed without executing locally on primary.
    primary_calls: list[str] = []

    class _PrimaryToolkit(SupportsPrimaryCallPlacement):
        def runs_on_primary(self, function_name: str, arguments: Mapping[str, object]) -> bool:
            del function_name, arguments
            return True

    async def _local_fn() -> str:
        primary_calls.append("ran-on-primary")
        return "leaked"

    primary_toolkit = _PrimaryToolkit()
    stand_ins = worker_stand_ins(
        "shell",
        primary_toolkit,
        runtime_paths=paths,
        credentials_manager=None,
        worker_target=target,
        runtime_config=cfg,
        tools=builtin_tool_registry(),
        sandbox_required=True,
        expected_sandbox=expected_sandbox,
    )
    wrapped: Callable[[], Awaitable[object]] = stand_ins("local_fn", _local_fn)
    with pytest.raises(RuntimeError, match="cannot execute tool calls on the primary"):
        await wrapped()
    assert primary_calls == []

    # 6. get_tool_by_name refuses to build an unrouted sandbox toolkit on the primary.
    with pytest.raises(RuntimeError, match="must be routed to a worker"):
        get_tool_by_name(
            "shell",
            paths,
            tools=builtin_tool_registry(),
            runs_in_worker=False,
            sandbox=True,
            worker_target=target,
            runtime_config=cfg,
        )

    assert lease_calls == []
    assert proxy_calls == []


def test_resolve_locked_worker_options_policy() -> None:
    """Locked worker option inheritance and conflict refusal share one pure policy across replies and workflows."""
    sb_repo = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
    sb_scratch = WorkerSandboxConfig()

    assert resolve_locked_worker_options(
        None,
        worker_name="box",
        resource_profile="small",
        sandbox=sb_repo,
        sandbox_specified=True,
    ) == ("small", sb_repo)

    # Omitted options inherit locked values.
    assert resolve_locked_worker_options(
        ("small", sb_repo),
        worker_name="box",
        resource_profile=None,
        sandbox=None,
        sandbox_specified=False,
        scope="reply",
    ) == ("small", sb_repo)
    assert resolve_locked_worker_options(
        ("small", sb_repo),
        worker_name="box",
        resource_profile="small",
        sandbox=sb_repo,
        sandbox_specified=True,
        scope="run",
    ) == ("small", sb_repo)

    # Conflicting profile / sandbox raise NamedWorkerError with scope-appropriate wording.
    with pytest.raises(NamedWorkerError, match="was started with resource_profile small in this reply"):
        resolve_locked_worker_options(
            ("small", sb_repo),
            worker_name="box",
            resource_profile="large",
            sandbox=None,
            sandbox_specified=False,
            scope="reply",
        )
    with pytest.raises(NamedWorkerError, match="was locked with resource_profile small in this run"):
        resolve_locked_worker_options(
            ("small", sb_repo),
            worker_name="box",
            resource_profile="large",
            sandbox=None,
            sandbox_specified=False,
            scope="run",
        )
    with pytest.raises(NamedWorkerError, match=r"was started in sandbox mode .* in this reply"):
        resolve_locked_worker_options(
            ("small", sb_repo),
            worker_name="box",
            resource_profile=None,
            sandbox=sb_scratch,
            sandbox_specified=True,
            scope="reply",
        )
    with pytest.raises(NamedWorkerError, match="was locked without sandbox mode in this run"):
        resolve_locked_worker_options(
            (None, None),
            worker_name="box",
            resource_profile=None,
            sandbox=sb_scratch,
            sandbox_specified=True,
            scope="run",
        )
