"""Tests for Phase 2 uniform K8s sandboxing of workflow.shell() and raw shell tool calls."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom import workflow as sdk
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.progress import report_tool_metadata
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.sandbox_proxy import WorkerStandIns, _named_worker_call
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    resolve_worker_key,
    shared_storage_root,
    visible_workspace_roots,
)
from mindroom.tools.shell import shell_tools as build_shell_tools
from mindroom.workers import named as named_module
from mindroom.workers.backends.kubernetes_config import KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY
from mindroom.workers.models import WorkerSandboxConfig
from mindroom.workers.named import named_worker_owner
from mindroom.workers.runtime import idle_worker_cleanup_owner
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.replay import node_digest
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.test_named_workers import _FakeBackend
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """Engine harness with one started run."""
    with workflow_stack(tmp_path) as harness:
        await harness.start()
        yield harness
        await harness.engine.close()


@contextmanager
def _k8s_shell_harness(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    *,
    named_workers: int = 4,
    backend_kind: str = "kubernetes",
    runtime_class_name: str | None = None,
    proxy_handler: Callable[..., object] | None = None,
) -> Iterator[tuple[_FakeBackend, list[dict[str, Any]], Path]]:
    """Configure *stack* for K8s named/sandboxed shell proxy routing backed by _FakeBackend."""
    backend = _FakeBackend()
    requests: list[dict[str, Any]] = []
    env = {
        **stack.context.runtime_paths.process_env,
        "MINDROOM_WORKER_BACKEND": backend_kind,
        "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "60",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
        "MINDROOM_SANDBOX_PROXY_URL": "http://127.0.0.1:8766",
    }
    if runtime_class_name is not None:
        env[KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["runtime_class_name"]] = runtime_class_name
    paths = replace(stack.context.runtime_paths, process_env=MappingProxyType(env))
    watcher = stack.context.config.agents["watcher"].model_copy(
        update={"worker_scope": "user_agent", "named_workers": named_workers},
    )
    cfg = stack.context.config.model_copy(update={"agents": {**stack.context.config.agents, "watcher": watcher}})
    ctx = replace(stack.context, config=cfg, runtime_paths=paths)
    stack.context = stack.resolver.context = ctx

    root = shared_storage_root(paths.storage_root)
    owner_key = resolve_worker_key("user_agent", ctx.execution_identity, agent_name="watcher")
    assert owner_key is not None
    workspace = visible_workspace_roots(root, owner_key, cfg.get_agent_policies(), private_agent_names=frozenset())[0]
    (workspace / "repo").mkdir(parents=True, exist_ok=True)
    (workspace / "repo2").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lease(*_args: object, **_kwargs: object) -> Iterator[_FakeBackend]:
        yield backend

    def _fake_execute(**kwargs: object) -> object:
        req = kwargs["request"]
        requests.append({"request": req, "handle": kwargs["worker_handle"]})
        if proxy_handler is not None:
            return proxy_handler(req, kwargs["worker_handle"])
        report_tool_metadata(not_started=False, exit_code=0)
        return f"ran:{req.kwargs.get('args')}@{req.kwargs.get('worker')}"

    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.lease_worker_manager", _lease)
    monkeypatch.setattr("mindroom.workers.named.lease_configured_primary_worker_manager", _lease)
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.execute_worker_proxy_request", _fake_execute)
    with idle_worker_cleanup_owner():
        yield backend, requests, workspace


async def _wait_retired(backend: _FakeBackend, expected: set[str]) -> None:
    deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_SECONDS
    while not expected.issubset(backend.retired):
        assert asyncio.get_running_loop().time() < deadline, f"Retired {backend.retired}, expected {expected}"
        await asyncio.sleep(0.02)


def _shell_call(cmd: str, **kwargs: object) -> dict[str, object]:
    return tool_args("shell", "run_shell_command", args=cmd, **kwargs)


def test_sdk_shell_forwarding_and_legacy_digest_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """workflow.shell omits None options (preserving legacy digests) and forwards explicit values including False."""
    captured: list[tuple[str, str, dict[str, object]]] = []

    def _fake_tool(toolkit: str, function: str, /, *, cached: bool = True, **arguments: object) -> dict[str, object]:
        del cached
        captured.append((toolkit, function, dict(arguments)))
        return {"exit_code": 0, "output": "ok"}

    monkeypatch.setattr(sdk, "tool", _fake_tool)
    assert sdk.shell("echo hi") == sdk.ShellResult(ok=True, exit_code=0, output="ok")
    legacy = captured[-1][2]
    assert legacy == {"args": "echo hi", "timeout": 3600, "tail": 2000}
    assert node_digest("tool", _shell_call("echo hi", timeout=3600, tail=2000)) == node_digest(
        "tool",
        {"toolkit": "shell", "function": "run_shell_command", "arguments": legacy},
    )

    sdk.shell("pytest", worker="box", resource_profile="small", sandbox=False, mount_dir="repo", mount_read_only=True)
    assert captured[-1] == (
        "shell",
        "run_shell_command",
        {
            "args": "pytest",
            "timeout": 3600,
            "tail": 2000,
            "worker": "box",
            "resource_profile": "small",
            "sandbox": False,
            "mount_dir": "repo",
            "mount_read_only": True,
        },
    )


@pytest.mark.asyncio
async def test_named_worker_reuse_inheritance_pure_output_conflict_and_retirement(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named workers reuse one pod, inherit locked specs, omit headers, refuse conflicts/quota as FAILED, and retire."""
    with _k8s_shell_harness(stack, monkeypatch, named_workers=1) as (backend, requests, workspace):
        r1 = await stack.result(
            "@0",
            "tool",
            _shell_call("setup", worker="box", sandbox=True, mount_dir="repo", resource_profile="small"),
        )
        assert r1 == {"output": "ran:setup@box", "exit_code": 0}
        assert not str(r1["output"]).startswith("[worker:")
        assert backend.specs[0].resource_profile == "small"
        assert backend.specs[0].sandbox == WorkerSandboxConfig(mount_dir="repo", mount_read_only=False)
        assert backend.specs[0].mirror_grantable_credentials is False
        assert requests[-1]["request"].sandbox is True

        # Flagless follow-up reuses the exact pod and inherits locked sandbox & resource_profile.
        r2 = await stack.result("@1", "tool", _shell_call("pytest", worker="box"))
        assert r2 == {"output": "ran:pytest@box", "exit_code": 0}
        assert backend.specs[-1].worker_key == backend.specs[0].worker_key
        assert backend.specs[-1].sandbox == WorkerSandboxConfig(mount_dir="repo", mount_read_only=False)
        assert backend.specs[-1].resource_profile == "small"

        # Conflicts & mindroom_output_path on flagless follow-up fail pre-dispatch with zero new pods.
        before = len(backend.specs)
        for idx, bad in enumerate(
            [
                {"worker": "box", "sandbox": False},
                {"worker": "box", "mount_dir": "repo2"},
                {"worker": "box", "mount_dir": "repo", "mount_read_only": True},
                {"worker": "box", "resource_profile": "large"},
                {"worker": "box", "mindroom_output_path": "out.txt"},
            ],
            start=2,
        ):
            with pytest.raises(NodeFailed) as exc_info:
                await stack.result(f"@{idx}", "tool", _shell_call("nope", **bad))
            node = stack.store.get_node(stack.run_id, f"@{idx}")
            assert exc_info.value.kind == "failed"
            assert node is not None
            assert (node.state, node.handle) == (WorkflowNodeState.FAILED, None)
        assert len(backend.specs) == before

        # Quota refusal inside sandbox_proxy sets not_started=True -> FAILED (never INDETERMINATE).
        with pytest.raises(NodeFailed) as exc_info:
            await stack.result("@7", "tool", _shell_call("over", worker="box2", sandbox=True))
        assert exc_info.value.kind == "failed"
        assert stack.store.get_node(stack.run_id, "@7").state is WorkflowNodeState.FAILED  # type: ignore[union-attr]

        # Deleted mount_dir on flagless follow-up fails pre-dispatch (mount_dir checked on effective lock).
        (workspace / "repo").rmdir()
        with pytest.raises(NodeFailed) as exc_info:
            await stack.result("@8", "tool", _shell_call("ls", worker="box"))
        assert exc_info.value.kind == "failed"
        assert len(backend.specs) == before

        worker_key = backend.specs[0].worker_key
        assert worker_key not in backend.retired
        await stack.engine.end_run(stack.store.get_run(stack.run_id), WorkflowRunState.COMPLETED, None)
        await _wait_retired(backend, {worker_key})


@pytest.mark.asyncio
@pytest.mark.parametrize("teardown", ["cancel", "close"])
async def test_named_worker_retires_on_cancel_and_engine_close(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    teardown: str,
) -> None:
    """Both engine.cancel() and engine.close() retire live run-scoped named worker pods."""
    with _k8s_shell_harness(stack, monkeypatch) as (backend, _requests, _workspace):
        await stack.result("@0", "tool", _shell_call("hi", worker="box", sandbox=True))
        worker_key = backend.specs[0].worker_key
        assert worker_key not in backend.retired
        if teardown == "cancel":
            await stack.engine.cancel(stack.run_id, reason="stop")
        else:
            await stack.engine.close()
        await _wait_retired(backend, {worker_key})


@pytest.mark.asyncio
async def test_restart_resume_persisted_worker_lock_order_independence_and_tamper_guards(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted workflow_run_workers prevents silent downgrade on resume, ignores seq order, and fails closed on tamper."""
    first_call = _shell_call(
        "init",
        worker="box",
        sandbox=True,
        mount_dir="repo",
        mount_read_only=True,
        resource_profile="small",
    )
    with _k8s_shell_harness(stack, monkeypatch) as (backend1, requests1, _workspace):
        await stack.result("@0", "tool", first_call)
        lock = stack.store.get_run_worker(stack.run_id, "box")
        assert lock is not None
        assert lock.resource_profile == "small"
        assert lock.sandbox_json == json.dumps({"mount_dir": "repo", "mount_read_only": True}, separators=(",", ":"))
        assert requests1[-1]["request"].sandbox is True

        await stack.engine.end_run(stack.store.get_run(stack.run_id), WorkflowRunState.INTERRUPTED, "restart")
        await _wait_retired(backend1, {backend1.specs[0].worker_key})
        assert not named_module._fleets

    await stack.restart()
    with _k8s_shell_harness(stack, monkeypatch) as (backend2, requests2, _workspace):
        await stack.resume(stack.run_id)
        assert (await stack.submit("@0", "tool", first_call)).cached is True
        assert backend2.specs == []

        # Flagless follow-up after restart MUST inherit persisted sandbox + resource_profile (never downgrade!).
        assert await stack.result("@1", "tool", _shell_call("check", worker="box")) == {
            "output": "ran:check@box",
            "exit_code": 0,
        }
        assert backend2.specs[0].sandbox == WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
        assert backend2.specs[0].resource_profile == "small"
        assert backend2.specs[0].mirror_grantable_credentials is False
        assert requests2[-1]["request"].sandbox is True

        # Swap seqs (flagless @1 -> seq=1, sandboxed @0 -> seq=2): immutable lock still governs (no seq-scan bug!).
        with stack.store.write_transaction() as conn:
            conn.execute("UPDATE workflow_nodes SET seq = 99 WHERE run_id = ? AND key = '@0'", (stack.run_id,))
            conn.execute("UPDATE workflow_nodes SET seq = 1 WHERE run_id = ? AND key = '@1'", (stack.run_id,))
            conn.execute("UPDATE workflow_nodes SET seq = 2 WHERE run_id = ? AND key = '@0'", (stack.run_id,))
        assert await stack.result("@2", "tool", _shell_call("order", worker="box")) == {
            "output": "ran:order@box",
            "exit_code": 0,
        }
        assert backend2.specs[-1].sandbox == WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)

        # Parallel foreach-style calls to the same named worker remain order-independent and enforce the lock.
        par_ok = await asyncio.gather(
            stack.result("/0:a@0", "tool", _shell_call("par-a", worker="box")),
            stack.result(
                "/0:b@0",
                "tool",
                _shell_call("par-b", worker="box", sandbox=True, mount_dir="repo", mount_read_only=True),
            ),
        )
        assert par_ok == [
            {"output": "ran:par-a@box", "exit_code": 0},
            {"output": "ran:par-b@box", "exit_code": 0},
        ]
        assert all(
            spec.sandbox == WorkerSandboxConfig(mount_dir="repo", mount_read_only=True) for spec in backend2.specs
        )

        # Persisted lock corruption & owner_scope tamper guards all fail closed before dispatch with zero extra pods.
        before = len(backend2.specs)
        for idx, sql in enumerate(
            [
                (
                    "UPDATE workflow_run_workers SET owner_scope = 'v1:other:user_agent:bob:watcher' "
                    "WHERE run_id = ? AND worker_name = 'box'",
                    (stack.run_id,),
                ),
                (
                    "UPDATE workflow_run_workers SET owner_scope = ?, sandbox_json = '{bad' "
                    "WHERE run_id = ? AND worker_name = 'box'",
                    (lock.owner_scope, stack.run_id),
                ),
                (
                    "UPDATE workflow_run_workers SET sandbox_json = ?, resource_profile = 'huge' "
                    "WHERE run_id = ? AND worker_name = 'box'",
                    (lock.sandbox_json, stack.run_id),
                ),
            ],
            start=3,
        ):
            with stack.store.write_transaction() as conn:
                conn.execute(*sql)
            with pytest.raises(NodeFailed) as exc_info:
                await stack.result(f"@{idx}", "tool", _shell_call("x", worker="box"))
            assert exc_info.value.kind == "failed"
        assert len(backend2.specs) == before

        # Verify store.prune() cascades deletion of workflow_run_workers via ON DELETE CASCADE.
        await stack.engine.end_run(stack.store.get_run(stack.run_id), WorkflowRunState.COMPLETED, None)
        assert stack.store.prune("2999-01-01T00:00:00Z") == 1
        assert stack.store.get_run_worker(stack.run_id, "box") is None


@pytest.mark.asyncio
async def test_unnamed_sandbox_call_scoped_parallel_isolation_and_immediate_retirement(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unnamed sandboxed calls synthesize per-call wf-* pods, never collide in parallel, and retire immediately."""
    with _k8s_shell_harness(stack, monkeypatch) as (backend, requests, _workspace):
        r_a, r_b = await asyncio.gather(
            stack.result("/0:a@0", "tool", _shell_call("job-a", sandbox=True, resource_profile="small")),
            stack.result(
                "/0:b@0",
                "tool",
                _shell_call("job-b", mount_dir="repo", mount_read_only=True, resource_profile="large"),
            ),
        )
        assert (r_a["exit_code"], str(r_a["output"]).startswith("[worker:")) == (0, False)
        assert (r_b["exit_code"], str(r_b["output"]).startswith("[worker:")) == (0, False)
        assert len(backend.specs) == 2
        assert backend.specs[0].worker_key != backend.specs[1].worker_key
        by_cmd = {req["request"].kwargs["args"]: req["request"].kwargs["worker"] for req in requests}
        assert all(isinstance(w, str) and w.startswith("wf-") and len(w) == 27 for w in by_cmd.values())
        assert by_cmd["job-a"] != by_cmd["job-b"]

        # Synthetic worker name is NOT persisted in node.request or workflow_run_workers.
        assert "worker" not in stack.store.get_node(stack.run_id, "/0:a@0").request["arguments"]  # type: ignore[union-attr,index]
        assert "worker" not in stack.store.get_node(stack.run_id, "/0:b@0").request["arguments"]  # type: ignore[union-attr,index]
        assert stack.store.get_run_worker(stack.run_id, str(by_cmd["job-a"])) is None

        # Both ephemeral pods retire immediately while the run is still RUNNING.
        assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
        await _wait_retired(backend, {spec.worker_key for spec in backend.specs})


@pytest.mark.asyncio
async def test_broker_resource_ref_fences_in_flight_call_on_run_end(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending a run while a shielded broker call is in-flight defers pod retirement until that call settles."""
    entered, release = threading.Event(), threading.Event()

    def _blocking_proxy(_req: object, _handle: object) -> str:
        entered.set()
        assert release.wait(SETTLE_TIMEOUT_SECONDS)
        report_tool_metadata(not_started=False, exit_code=0)
        return "finished"

    with _k8s_shell_harness(stack, monkeypatch, proxy_handler=_blocking_proxy) as (backend, _requests, _workspace):
        await stack.submit("@0", "tool", _shell_call("slow", worker="box", sandbox=True))
        await asyncio.to_thread(entered.wait, SETTLE_TIMEOUT_SECONDS)
        assert entered.is_set()
        worker_key = backend.specs[0].worker_key

        await stack.engine.end_run(stack.store.get_run(stack.run_id), WorkflowRunState.CANCELLED, "stop")
        await asyncio.sleep(0.05)
        assert worker_key not in backend.retired

        release.set()
        await _wait_retired(backend, {worker_key})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "harness_opts", "expected_msg"),
    [
        ({"sandbox": False, "mount_dir": "repo"}, {}, "mount_dir always runs in sandbox mode"),
        ({"mount_read_only": True}, {}, "mount_read_only requires mount_dir"),
        ({"resource_profile": "large"}, {}, "resource_profile requires worker"),
        ({"worker": "box"}, {"named_workers": 0}, "Named workers are off"),
        ({"sandbox": True}, {"backend_kind": "docker"}, "kubernetes worker backend"),
        ({"mount_dir": "repo"}, {"runtime_class_name": "gvisor"}, "runtimeClassName"),
        ({"worker": "BAD_NAME"}, {}, "worker must be 1 to 32"),
        ({"mount_dir": "does-not-exist"}, {}, "does not exist"),
    ],
)
async def test_pre_dispatch_refusals_settle_failed_with_zero_pods(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    harness_opts: dict[str, Any],
    expected_msg: str,
) -> None:
    """Invalid options, disabled named_workers, non-K8s, runtimeClassName+mount_dir, and missing dirs settle FAILED."""
    with _k8s_shell_harness(stack, monkeypatch, **harness_opts) as (backend, _requests, _workspace):
        with pytest.raises(NodeFailed) as exc_info:
            await stack.result("@0", "tool", _shell_call("echo hi", **kwargs))
        node = stack.store.get_node(stack.run_id, "@0")
        assert exc_info.value.kind == "failed"
        assert expected_msg in str(exc_info.value)
        assert node is not None
        assert (node.state, node.handle) == (WorkflowNodeState.FAILED, None)
        assert backend.specs == []


@pytest.mark.asyncio
async def test_post_dispatch_lost_response_remains_indeterminate(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once dispatched (not_started=False), a lost worker response MUST settle INDETERMINATE."""

    def _dropping_proxy(_req: object, _handle: object) -> object:
        report_tool_metadata(not_started=False)
        msg = "connection dropped after dispatch"
        raise RuntimeError(msg)

    with _k8s_shell_harness(stack, monkeypatch, proxy_handler=_dropping_proxy) as (backend, _requests, _workspace):
        with pytest.raises(NodeFailed) as exc_info:
            await stack.result("@0", "tool", _shell_call("rm -rf tmp", sandbox=True))
        assert exc_info.value.kind == "indeterminate"
        assert stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.INDETERMINATE  # type: ignore[union-attr]
        assert len(backend.specs) == 1
        await _wait_retired(backend, {backend.specs[0].worker_key})


@pytest.mark.asyncio
async def test_named_worker_owner_takes_precedence_over_ambient_turn_id(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit named_worker_owner() beats ambient context.turn_id and exposes prelocked sandbox to locked_sandbox()."""
    with _k8s_shell_harness(stack, monkeypatch):
        ambient_ctx = replace(stack.context, turn_id="ambient-turn-id")
        target = build_agent_toolkit_worker_target(
            "user_agent",
            "watcher",
            is_private=False,
            execution_identity=ambient_ctx.execution_identity,
            runtime_paths=ambient_ctx.runtime_paths,
        )
        shell_toolkit = build_shell_tools()(runtime_paths=ambient_ctx.runtime_paths, named_workers=True)
        stand_ins = WorkerStandIns(
            tool_name="shell",
            runtime_paths=ambient_ctx.runtime_paths,
            credentials_manager=None,
            tools=ambient_ctx.plugins.tools,
            worker_target=target,
            runtime_config=ambient_ctx.config,
        )
        locked = WorkerSandboxConfig(mount_dir="repo", mount_read_only=True)
        async with execution_resources(response=True, jobs_enabled=False):
            with (
                tool_runtime_context(ambient_ctx),
                named_worker_owner(
                    "workflow:explicit-owner",
                    prelocked={"box": ("small", locked)},
                ),
            ):
                call = _named_worker_call(stand_ins, shell_toolkit.run_shell_command, ("echo hi",), {"worker": "box"})
                assert call is not None
                assert call._fleet.owner_id == "workflow:explicit-owner"
                assert call.locked_sandbox(target.worker_key) == locked


@pytest.mark.asyncio
async def test_interrupted_workflow_run_replay_preserves_pods_until_terminal_settlement(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay to STARTING preserves worker pods and _RunState; terminal INTERRUPTED releases pods and evicts _RunState."""
    with _k8s_shell_harness(stack, monkeypatch) as (backend, _requests, _workspace):
        assert await stack.result("@0", "tool", _shell_call("step1", worker="box", sandbox=True)) == {
            "output": "ran:step1@box",
            "exit_code": 0,
        }
        assert len(backend.specs) == 1
        worker_key = backend.specs[0].worker_key
        assert stack.run_id in stack.engine._runs

        # Attempt replay (STARTING) must keep run-scoped worker resources alive.
        run = stack.store.get_run(stack.run_id)
        replayed = await stack.engine.end_run(
            run,
            WorkflowRunState.STARTING,
            None,
            finished_at=None,
            previous_source_digest=run.source_digest,
        )
        assert replayed.state is WorkflowRunState.STARTING
        assert worker_key not in backend.retired
        assert named_module._fleets
        assert stack.run_id in stack.engine._runs

        # Terminal INTERRUPTED settlement releases worker pods, clears fleets, and evicts _RunState.
        ended = await stack.engine.end_run(replayed, WorkflowRunState.INTERRUPTED, "interrupted")
        assert ended.state is WorkflowRunState.INTERRUPTED
        await _wait_retired(backend, {worker_key})
        assert not named_module._fleets
        assert stack.run_id not in stack.engine._runs
