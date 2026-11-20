"""Named workers: who owns them, how many may run, and how a shell call reaches the one it names."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.response_turn import AttemptResolved, CompletedAttempt
from mindroom.tool_jobs.resources import ExecutionResourceReference, current_execution_resources, execution_resources
from mindroom.tool_system import sandbox_proxy as sandbox_proxy_module
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.catalog import builtin_tool_registry
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    pinned_worker_kind,
    pinned_worker_state_scope_key,
    resolve_worker_target,
)
from mindroom.workers import named as named_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workers.named import (
    NamedWorkerCall,
    NamedWorkerError,
    ensure_named_worker,
    named_worker_call,
    named_workers_refusal,
    reply_named_workers,
)
from mindroom.workers.runtime import idle_worker_cleanup_owner
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import SETTLE_TIMEOUT_SECONDS, make_conversation_reader_mock, make_relation_lookup
from tests.test_response_turn import _collect_turn, _ctx
from tests.test_sandbox_proxy import _offered_entrypoint, _recording_client_class, _static_worker_manager_lease

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import WorkerStatus

_TURN = "turn-1"
_CONFIG = Config(agents={"code": AgentConfig(display_name="Code", extra_workers=2)})
_ALICE = "v1:default:user_agent:@alice:example.org:code"
_BOB = "v1:default:user_agent:@bob:example.org:code"


class _FakeBackend:
    """A backend that runs named workers in memory, recording what it starts and removes."""

    backend_name = "kubernetes"
    cleanup_locator = "test"
    idle_timeout_seconds = 1800.0

    def __init__(self, *, named_workers_max: int = 16) -> None:
        self.named_workers_max = named_workers_max
        self.workers: dict[str, WorkerHandle] = {}
        self.specs: list[WorkerSpec] = []
        self.retired: list[str] = []
        self.replaced: list[str] = []
        # Set to hold every start until the test releases it; each held start first releases ``entered``.
        self.hold_starts: threading.Event | None = None
        self.entered = threading.Semaphore(0)
        self._lock = threading.Lock()

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object = None,
    ) -> WorkerHandle:
        del now, progress_sink
        if self.hold_starts is not None:
            self.entered.release()
            assert self.hold_starts.wait(SETTLE_TIMEOUT_SECONDS)
        with self._lock:
            self.specs.append(spec)
            existing = self.workers.get(spec.worker_key)
            if existing is not None and existing.status != "ready":
                # A stopped named worker is replaced by a new one, as the Kubernetes backend does.
                self.replaced.append(spec.worker_key)
            if existing is None or existing.status != "ready":
                existing = WorkerHandle(
                    worker_id=f"worker-{len(self.specs)}",
                    worker_key=spec.worker_key,
                    endpoint=f"http://worker-{len(self.specs)}:8766/api/sandbox-runner/execute",
                    auth_token="test-token",  # noqa: S106
                    status="ready",
                    backend_name=self.backend_name,
                    last_used_at=0.0,
                    created_at=0.0,
                    startup_count=1 if existing is None else existing.startup_count + 1,
                )
                self.workers[spec.worker_key] = existing
            return existing

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        with self._lock:
            return list(self.workers.values())

    def retire_worker(self, worker_key: str) -> None:
        with self._lock:
            self.retired.append(worker_key)
            self.workers.pop(worker_key, None)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        return self.workers.get(worker_key)

    def record_failure(self, worker_key: str, failure_reason: str, **_kwargs: object) -> WorkerHandle:
        """Stop the worker and keep it as failed, as the Kubernetes backend does."""
        return self.set_status(worker_key, "failed", failure_reason=failure_reason)

    def set_status(self, worker_key: str, status: WorkerStatus, **changes: Any) -> WorkerHandle:  # noqa: ANN401
        with self._lock:
            self.workers[worker_key] = replace(self.workers[worker_key], status=status, **changes)
            return self.workers[worker_key]


@pytest.fixture
def runtime_paths(tmp_path: Path) -> Iterator[RuntimePaths]:
    """Return a runtime on the kubernetes backend whose idle-worker cleanup runs."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "30",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
        },
    )
    with idle_worker_cleanup_owner():
        yield paths


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    """Return the backend a reply's named workers are removed through when it ends."""
    fake = _FakeBackend()
    monkeypatch.setattr(
        named_module,
        "lease_configured_primary_worker_manager",
        lambda _runtime_paths: _static_worker_manager_lease(fake),
    )
    return fake


def _call(
    runtime_paths: RuntimePaths,
    name: object,
    resource_profile: object = None,
    *,
    turn_id: str | None = _TURN,
    agent_name: str | None = "code",
    config: Config | None = _CONFIG,
) -> NamedWorkerCall:
    call = named_worker_call(
        name,
        resource_profile,
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        turn_id=turn_id,
    )
    assert call is not None
    return call


def _run(backend: _FakeBackend, call: NamedWorkerCall, normal_key: str = _ALICE) -> WorkerHandle:
    """Reach the worker ``call`` names beside ``normal_key``, as one routed shell call does."""
    spec = call.worker_spec(WorkerSpec(normal_key, private_agent_names=frozenset()))
    return ensure_named_worker(cast("WorkerBackend", backend), spec, call)


async def _retired(backend: _FakeBackend) -> list[str]:
    """Return the workers removed once every pending removal has run."""
    assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS)
    return sorted(backend.retired)


@pytest.mark.asyncio
async def test_a_name_starts_one_worker_that_later_calls_of_the_reply_reach(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The first call with a name starts a worker beside the normal one, and the same name reaches it again."""
    async with reply_named_workers(_TURN):
        call = _call(runtime_paths, "e2e-0", "large")
        first = _run(backend, call)
        again = _run(backend, _call(runtime_paths, "e2e-0"))
        other = _run(backend, _call(runtime_paths, "e2e-1"))

        assert first.worker_key == again.worker_key != other.worker_key
        assert pinned_worker_kind(first.worker_key) == "named"
        assert pinned_worker_state_scope_key(first.worker_key) == _ALICE
        assert "e2e-0" not in first.worker_key
        # The first call sizes the worker, and a later call that names no size keeps it.
        assert [spec.resource_profile for spec in backend.specs] == ["large", "large", None]
        assert {spec.state_scope_worker_key for spec in backend.specs} == {_ALICE}
        assert {spec.private_agent_names for spec in backend.specs} == {frozenset()}
        # Only a call that ran in a new start of its worker says so.
        assert call.header(first) == "[worker: e2e-0, started]"
        assert call.header(again) == "[worker: e2e-0]"


@pytest.mark.asyncio
async def test_a_call_that_asks_for_another_size_of_a_running_worker_is_refused(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """Resizing would restart the worker under its commands, so a name keeps the size it started with."""
    async with reply_named_workers(_TURN):
        _run(backend, _call(runtime_paths, "build", "small"))

        with pytest.raises(NamedWorkerError, match="was started with resource_profile small in this reply"):
            _call(runtime_paths, "build", "large").worker_spec(WorkerSpec(_ALICE))


@pytest.mark.asyncio
async def test_the_workers_of_a_reply_are_removed_when_it_ends(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A reply that ends with no background job removes every worker its calls started."""
    async with reply_named_workers(_TURN):
        keys = sorted(_run(backend, _call(runtime_paths, name)).worker_key for name in ("a", "b"))
        assert backend.retired == []

    assert await _retired(backend) == keys
    assert backend.workers == {}
    with pytest.raises(NamedWorkerError, match="belongs to a reply"):
        _call(runtime_paths, "a")


@pytest.mark.asyncio
async def test_a_background_job_keeps_the_workers_of_its_reply_until_it_ends(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A job the reply started still reaches the reply's workers, which are removed when the job has ended too."""
    async with execution_resources() as resources:
        async with reply_named_workers(_TURN):
            started = _run(backend, _call(runtime_paths, "shard"))
            job = resources.acquire()
        # The reply's turn is over, and its job names the worker again.
        assert _run(backend, _call(runtime_paths, "shard")).worker_key == started.worker_key
    assert await _retired(backend) == []

    await job.release()

    assert await _retired(backend) == [started.worker_key]


@pytest.mark.asyncio
async def test_a_reply_that_resumes_while_its_job_runs_keeps_its_workers(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A second turn under the same turn ID, such as after an approval, joins the workers a job still holds."""
    async with execution_resources() as resources, reply_named_workers(_TURN):
        started = _run(backend, _call(runtime_paths, "shard"))
        job = resources.acquire()
    async with reply_named_workers(_TURN):
        assert _run(backend, _call(runtime_paths, "shard")).worker_key == started.worker_key
    assert await _retired(backend) == []

    await job.release()

    assert await _retired(backend) == [started.worker_key]


@pytest.mark.asyncio
async def test_another_reply_and_a_later_reply_get_workers_of_their_own(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name maps to another worker in every reply, so a worker a restart left behind is never reached again."""
    # The first reply ends as a restart ends it: nothing removes its worker.
    monkeypatch.setattr(named_module, "lease_configured_primary_worker_manager", lambda _runtime_paths: None)
    async with reply_named_workers(_TURN):
        left_behind = _run(backend, _call(runtime_paths, "e2e-0"))
        async with reply_named_workers("turn-2"):
            concurrent = _run(backend, _call(runtime_paths, "e2e-0", turn_id="turn-2"), normal_key=_BOB)
    assert await _retired(backend) == []

    # The same turn resumes after the restart and names the same worker.
    async with reply_named_workers(_TURN):
        resumed = _run(backend, _call(runtime_paths, "e2e-0"))

        pins = {key.split(":")[-2] for key in (left_behind.worker_key, concurrent.worker_key, resumed.worker_key)}
        assert len(pins) == 3
        # What the restart left behind still runs, so it counts against the agent's limit of two until cleanup
        # removes it.
        with pytest.raises(NamedWorkerError, match="2 of the 2 named workers"):
            _run(backend, _call(runtime_paths, "e2e-1"))


@pytest.mark.asyncio
async def test_a_new_worker_past_the_agent_limit_is_refused_not_queued(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The limit counts one requester's running workers of the agent; a name that already runs is always reached."""
    async with reply_named_workers(_TURN):
        first = _run(backend, _call(runtime_paths, "a"))
        _run(backend, _call(runtime_paths, "b"))
        # Another requester's workers of the same agent do not count against this one.
        _run(backend, _call(runtime_paths, "a"), normal_key=_BOB)

        with pytest.raises(NamedWorkerError, match="Cannot start worker 'c': 2 of the 2 named workers"):
            _run(backend, _call(runtime_paths, "c"))
        assert len(backend.workers) == 3
        assert _run(backend, _call(runtime_paths, "a")).worker_key == first.worker_key

        # A worker cleanup stopped no longer counts.
        backend.set_status(first.worker_key, "idle")
        _run(backend, _call(runtime_paths, "c"))


@pytest.mark.asyncio
async def test_a_new_worker_past_the_deployment_limit_is_refused(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The deployment's limit counts every requester's and agent's named workers together."""
    backend.named_workers_max = 2
    async with reply_named_workers(_TURN):
        _run(backend, _call(runtime_paths, "a"))
        _run(backend, _call(runtime_paths, "a"), normal_key=_BOB)

        with pytest.raises(NamedWorkerError, match="this deployment runs its limit of 2 named workers"):
            _run(backend, _call(runtime_paths, "b"))


@pytest.mark.asyncio
async def test_workers_that_are_starting_count_against_the_limit(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """Calls that start workers at the same time cannot pass the limit together."""
    backend.hold_starts = threading.Event()
    async with reply_named_workers(_TURN):
        starting = [asyncio.create_task(asyncio.to_thread(_run, backend, _call(runtime_paths, name))) for name in "ab"]
        try:
            # Both starts are admitted and wait in the backend, which lists neither worker yet.
            for _ in starting:
                assert await asyncio.to_thread(backend.entered.acquire, timeout=SETTLE_TIMEOUT_SECONDS)
            assert backend.workers == {}

            with pytest.raises(NamedWorkerError, match="2 of the 2 named workers"):
                await asyncio.to_thread(_run, backend, _call(runtime_paths, "c"))
        finally:
            backend.hold_starts.set()
            await asyncio.gather(*starting)
        assert len(backend.workers) == 2
        assert not named_module._starting


@pytest.mark.asyncio
async def test_a_worker_that_crashed_is_replaced_by_a_new_one(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The next call with the name of a failed worker removes what it left and starts again, and says so."""
    async with reply_named_workers(_TURN):
        call = _call(runtime_paths, "e2e-0")
        crashed = _run(backend, call)
        assert call.header(crashed) == "[worker: e2e-0, started]"
        backend.record_failure(crashed.worker_key, "pod was evicted")

        replacement = _run(backend, call)

        assert backend.replaced == [crashed.worker_key]
        assert replacement.worker_key == crashed.worker_key
        assert replacement.worker_id != crashed.worker_id
        assert call.header(replacement) == "[worker: e2e-0, started]"


@pytest.mark.asyncio
async def test_a_call_whose_reply_ended_starts_no_worker(runtime_paths: RuntimePaths, backend: _FakeBackend) -> None:
    """A call that outlives its reply and that reply's jobs cannot start a worker nothing would remove."""
    async with reply_named_workers(_TURN):
        call = _call(runtime_paths, "late")
        spec = call.worker_spec(WorkerSpec(_ALICE))

    with pytest.raises(NamedWorkerError, match="its reply and that reply's background jobs have ended"):
        ensure_named_worker(cast("WorkerBackend", backend), spec, call)
    assert backend.specs == []


@pytest.mark.asyncio
async def test_a_removal_that_fails_is_left_to_cleanup(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One worker that cannot be removed does not keep the reply's other workers."""
    async with reply_named_workers(_TURN):
        stuck, removed = (_run(backend, _call(runtime_paths, name)).worker_key for name in ("a", "b"))
    retire = backend.retire_worker

    def fail_one(worker_key: str) -> None:
        if worker_key == stuck:
            msg = "cluster unavailable"
            raise WorkerBackendError(msg)
        retire(worker_key)

    monkeypatch.setattr(backend, "retire_worker", fail_one)

    assert await _retired(backend) == [removed]
    assert list(backend.workers) == [stuck]


@pytest.mark.parametrize(
    ("name", "resource_profile", "message"),
    [
        (None, "large", "resource_profile sizes a named worker"),
        ("Upper", None, "worker must be 1 to 32 lowercase letters"),
        ("-leading", None, "worker must be 1 to 32 lowercase letters"),
        ("a" * 33, None, "worker must be 1 to 32 lowercase letters"),
        ("a/b", None, "worker must be 1 to 32 lowercase letters"),
        (3, None, "worker must be 1 to 32 lowercase letters"),
        ("ok", "huge", "resource_profile must be small, standard, or large"),
        ("ok", None, "belongs to a reply"),
    ],
)
def test_a_call_names_a_worker_only_with_usable_arguments_inside_a_reply(
    runtime_paths: RuntimePaths,
    name: object,
    resource_profile: object,
    message: str,
) -> None:
    """The arguments arrive as the model sent them, and no reply owns a worker named outside one."""
    with pytest.raises(NamedWorkerError, match=message):
        _call(runtime_paths, name, resource_profile)


def test_a_call_that_names_no_worker_asks_for_none(runtime_paths: RuntimePaths) -> None:
    """Without the two arguments a call runs in its normal worker, whatever its agent may do."""
    assert (
        named_worker_call(None, None, agent_name="code", config=None, runtime_paths=runtime_paths, turn_id=None) is None
    )


@pytest.mark.parametrize(
    ("agent_name", "config", "env", "cleanup_runs", "message"),
    [
        ("code", _CONFIG, {}, True, None),
        ("other", _CONFIG, {}, True, "Named workers are off for this agent"),
        (None, _CONFIG, {}, True, "Named workers are off for this agent"),
        ("code", None, {}, True, "Named workers are off for this agent"),
        (
            "code",
            Config(agents={"code": AgentConfig(display_name="Code")}),
            {},
            True,
            "Named workers are off for this agent",
        ),
        ("code", _CONFIG, {"MINDROOM_WORKER_BACKEND": "docker"}, True, "need the kubernetes worker backend"),
        ("code", _CONFIG, {"MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "0"}, True, "idle-worker cleanup"),
        ("code", _CONFIG, {}, False, "idle-worker cleanup"),
    ],
)
def test_named_workers_need_the_agent_setting_the_kubernetes_backend_and_cleanup(
    tmp_path: Path,
    agent_name: str | None,
    config: Config | None,
    env: dict[str, str],
    cleanup_runs: bool,
    message: str | None,
) -> None:
    """Named workers are offered only where the agent may run them and cleanup removes what a reply leaves behind."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes", "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "30", **env},
    )

    def refusal() -> str | None:
        return named_workers_refusal(agent_name, config, runtime_paths)

    if cleanup_runs:
        with idle_worker_cleanup_owner():
            result = refusal()
    else:
        result = refusal()

    assert (result is None) if message is None else (result is not None and message in result)


def test_a_backend_without_named_workers_refuses_them(runtime_paths: RuntimePaths) -> None:
    """Only a backend that limits and removes named workers runs them."""

    class _PlainBackend:
        def list_workers(self) -> list[WorkerHandle]:
            return []

    call = NamedWorkerCall("a", None, 2, runtime_paths, named_module._NamedWorkerFleet(_TURN))

    with pytest.raises(NamedWorkerError, match="need the kubernetes worker backend"):
        ensure_named_worker(cast("WorkerBackend", _PlainBackend()), WorkerSpec(_ALICE), call)


# The shell tool: a call that names a worker is routed to it, and the result says which worker ran it.


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )


def _shell(runtime_paths: RuntimePaths) -> tuple[Callable[..., Any], ResolvedWorkerTarget]:
    """Return the routed entrypoint of the agent's shell and the worker target its calls normally reach."""
    target = resolve_worker_target("user_agent", "code", _identity(), private_agent_names=frozenset())
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=True,
        runtime_config=_CONFIG,
        worker_target=target,
    )
    return _offered_entrypoint(shell, "run_shell_command"), target


def _tool_context(runtime_paths: RuntimePaths, turn_id: str = _TURN) -> object:
    return make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=object(),
        config=_CONFIG,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        turn_id=turn_id,
    )


def _route_through(
    monkeypatch: pytest.MonkeyPatch,
    backend: _FakeBackend,
    responder: Callable[[str, dict[str, Any]], object] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Send the shell's worker calls to ``backend`` and a recording client, and return what the client was sent."""
    posts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_worker_manager",
        lambda _runtime_paths, _proxy_config: _static_worker_manager_lease(backend),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured_calls=posts, responder=responder),
    )
    return posts


@pytest.mark.asyncio
async def test_a_shell_call_runs_in_the_worker_it_names(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed shell call reaches the named worker's own endpoint and leads its result with the worker."""
    posts = _route_through(monkeypatch, backend)
    shell, target = _shell(runtime_paths)
    assert target.worker_key is not None

    with tool_runtime_context(_tool_context(runtime_paths)):
        async with reply_named_workers(_TURN):
            normal = await shell("echo hi")
            first = await shell("echo hi", worker="e2e-0", resource_profile="large")
            again = await shell(args="echo hi", worker="e2e-0")

    assert (normal, first, again) == (
        "sandbox-result",
        "[worker: e2e-0, started]\nsandbox-result",
        "[worker: e2e-0]\nsandbox-result",
    )
    normal_spec, named_spec, reused_spec = backend.specs
    assert normal_spec == WorkerSpec(target.worker_key, private_agent_names=frozenset())
    assert named_spec == reused_spec
    assert pinned_worker_state_scope_key(named_spec.worker_key) == target.worker_key
    assert (named_spec.state_scope_worker_key, named_spec.resource_profile) == (target.worker_key, "large")
    # Each request goes to its own worker and names that worker's key, with the agent's own scope and identity.
    (normal_url, normal_post), (named_url, named_post), _ = posts
    assert normal_url != named_url == backend.workers[named_spec.worker_key].endpoint
    assert (named_post["worker_key"], named_post["worker_scope"]) == (named_spec.worker_key, "user_agent")
    assert named_post["execution_identity"] == normal_post["execution_identity"]
    assert await _retired(backend) == [named_spec.worker_key]


@pytest.mark.asyncio
async def test_a_stopped_shell_call_is_stopped_in_its_named_worker(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a call sends the stop to the named worker that runs its command, which kills its process group."""
    running, stopped = threading.Event(), threading.Event()

    def responder(url: str, _payload: dict[str, Any]) -> object:
        if url.endswith("/execute/cancel"):
            stopped.set()
            return {"cancelled": True}
        running.set()
        assert stopped.wait(SETTLE_TIMEOUT_SECONDS)
        return {"ok": False, "error": "Tool call was cancelled.", "failure_kind": "tool"}

    posts = _route_through(monkeypatch, backend, responder)
    shell, _target = _shell(runtime_paths)

    with tool_runtime_context(_tool_context(runtime_paths)):
        async with reply_named_workers(_TURN):
            call = asyncio.create_task(shell("sleep 600", worker="e2e-0"))
            assert await asyncio.to_thread(running.wait, SETTLE_TIMEOUT_SECONDS)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert await asyncio.to_thread(stopped.wait, SETTLE_TIMEOUT_SECONDS)

    [worker] = backend.workers.values()
    (execute_url, execute), (cancel_url, cancel) = posts
    assert execute_url == worker.endpoint
    assert cancel_url == f"{worker.endpoint}/cancel"
    assert cancel["request_id"] == execute["request_id"]
    # The reply ended with the stop, so its worker goes too.
    assert await _retired(backend) == [worker.worker_key]


@pytest.mark.asyncio
async def test_a_shell_call_after_its_worker_crashed_runs_in_a_new_one(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that dies under a command fails that call, and the next call with its name says it started anew."""
    crash = threading.Event()

    def responder(url: str, _payload: dict[str, Any]) -> object:
        if crash.is_set():
            raise httpx.ConnectError(url)
        return {"ok": True, "result": "sandbox-result"}

    _route_through(monkeypatch, backend, responder)
    shell, _target = _shell(runtime_paths)

    with tool_runtime_context(_tool_context(runtime_paths)):
        async with reply_named_workers(_TURN):
            assert await shell("true", worker="e2e-0") == "[worker: e2e-0, started]\nsandbox-result"
            [crashed] = backend.workers.values()
            crash.set()
            with pytest.raises(httpx.ConnectError):
                await shell("true", worker="e2e-0")
            assert backend.workers[crashed.worker_key].status == "failed"
            crash.clear()

            assert await shell("true", worker="e2e-0") == "[worker: e2e-0, started]\nsandbox-result"

    assert backend.replaced == [crashed.worker_key]
    assert await _retired(backend) == [crashed.worker_key]


@pytest.mark.asyncio
async def test_a_refused_shell_call_fails_with_the_reason_and_starts_nothing(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that names a worker outside a reply fails before anything is started or sent."""
    posts = _route_through(monkeypatch, backend)
    shell, _target = _shell(runtime_paths)

    with (
        tool_runtime_context(_tool_context(runtime_paths)),
        pytest.raises(NamedWorkerError, match="belongs to a reply"),
    ):
        await shell("true", worker="e2e-0")

    assert (backend.specs, posts) == ([], [])


@pytest.mark.parametrize("extra_workers", [0, 2])
def test_the_shell_offers_named_workers_only_to_an_agent_that_may_run_them(
    runtime_paths: RuntimePaths,
    extra_workers: int,
) -> None:
    """The model sees ``worker`` and ``resource_profile``, and how to use them, only when its agent may name workers."""
    config = Config(agents={"code": AgentConfig(display_name="Code", extra_workers=extra_workers)})
    target = resolve_worker_target("user_agent", "code", _identity(), private_agent_names=frozenset())
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=True,
        runtime_config=config,
        worker_target=target,
    )
    function = toolkit_function(shell, "run_shell_command")
    assert function is not None

    offered = set(function.parameters["properties"])
    assert ({"worker", "resource_profile"} <= offered) is bool(extra_workers)
    assert {"args", "tail", "timeout"} <= offered
    assert ("mindroom-agent workers run" in (function.description or "")) is bool(extra_workers)


@pytest.mark.asyncio
async def test_a_shell_outside_a_worker_refuses_to_name_one(tmp_path: Path) -> None:
    """A shell that runs in MindRoom's own process has no worker to route a named call to."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", extra_workers=2)})
    config.defaults.allow_shell_without_worker = True
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        runtime_config=config,
        worker_target=None,
    )

    result = await _offered_entrypoint(shell, "run_shell_command")("true", worker="e2e-0")

    assert result.startswith("Error: `worker` and `resource_profile` need a shell that runs in a worker")


@pytest.mark.asyncio
async def test_the_shell_in_a_worker_runs_the_command_of_a_call_that_names_it(tmp_path: Path) -> None:
    """The call reaches its worker with its arguments as sent, and the worker's shell runs it like any other."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        worker_target=None,
    )

    result = await _offered_entrypoint(shell, "run_shell_command")(
        ["echo", "ran here"],
        worker="e2e-0",
        resource_profile="large",
    )

    assert result == "ran here"


@pytest.mark.asyncio
async def test_a_response_turn_owns_the_named_workers_of_its_calls(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A call made during a turn can name a worker of that turn, which goes when the turn ends."""
    started: list[str] = []

    async def attempt(_run_state: object, _continuation: object) -> AsyncGenerator[str | AttemptResolved]:
        started.append(_run(backend, _call(runtime_paths, "e2e-0")).worker_key)
        yield AttemptResolved(CompletedAttempt(replayable_text="done", has_visible_content=True))

    with tool_runtime_context(_tool_context(runtime_paths)):
        with pytest.raises(NamedWorkerError, match="belongs to a reply"):
            _call(runtime_paths, "e2e-0")
        await _collect_turn(attempt)
        with pytest.raises(NamedWorkerError, match="belongs to a reply"):
            _call(runtime_paths, "e2e-0")

    assert len(started) == 1
    assert await _retired(backend) == started


@pytest.mark.asyncio
async def test_a_job_a_response_turn_started_keeps_the_turn_s_named_workers(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A turn that ends while a job it started still runs leaves its workers to that job."""
    started: list[str] = []
    jobs: list[ExecutionResourceReference] = []

    async def attempt(_run_state: object, _continuation: object) -> AsyncGenerator[str | AttemptResolved]:
        started.append(_run(backend, _call(runtime_paths, "e2e-0")).worker_key)
        resources = current_execution_resources()
        assert resources is not None
        # A call the turn accepted as a background job holds the turn's resources until it ends.
        jobs.append(resources.acquire())
        yield AttemptResolved(CompletedAttempt(replayable_text="done", has_visible_content=True))

    with tool_runtime_context(_tool_context(runtime_paths)):
        await _collect_turn(attempt, ctx=_ctx(background_tool_jobs=True))
        assert await _retired(backend) == []
        assert _run(backend, _call(runtime_paths, "e2e-0")).worker_key == started[0]

    await jobs[0].release()

    assert await _retired(backend) == started
