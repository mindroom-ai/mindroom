"""Named workers: who owns them, how many may run, and how a shell call reaches the one it names."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextvars import copy_context
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from urllib3.exceptions import ReadTimeoutError

from mindroom import agents
from mindroom.agent_cli.minimal_agent import minimal_turn
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.llm.messages import system_text
from mindroom.message_target import MessageTarget
from mindroom.response_turn import AttemptResolved, CompletedAttempt
from mindroom.tool_jobs.resources import ExecutionResourceReference, current_execution_resources, execution_resources
from mindroom.tool_system import sandbox_proxy as sandbox_proxy_module
from mindroom.tool_system.agno_toolkits import adapt_toolkit, toolkit_function
from mindroom.tool_system.catalog import builtin_tool_registry
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    pinned_worker_kind,
    pinned_worker_state_scope_key,
    resolve_worker_target,
)
from mindroom.workers import named as named_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends import kubernetes as kubernetes_backend_module
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workers.named import (
    NamedWorkerCall,
    NamedWorkerError,
    ensure_named_worker,
    named_worker_call,
    named_workers_refusal,
)
from mindroom.workers.runtime import idle_worker_cleanup_owner
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import SETTLE_TIMEOUT_SECONDS, make_conversation_reader_mock, make_relation_lookup
from tests.identity_helpers import persist_entity_accounts
from tests.test_kubernetes_worker_backend import _FakeAppsApi, _named_backend
from tests.test_response_turn import _collect_turn, _ctx
from tests.test_sandbox_proxy import _offered_entrypoint, _recording_client_class, _static_worker_manager_lease
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import WorkerStatus

_TURN = "turn-1"
_CONFIG = Config(agents={"code": AgentConfig(display_name="Code", named_workers=2, worker_scope="user_agent")})
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
        # Called once the next listing has read the workers, before it answers with what it read.
        self.after_listing: Callable[[], None] | None = None
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
                    # A replacement is a new worker, which counts its starts from one and is told apart by what
                    # the backend runs it as.
                    startup_count=1,
                    incarnation=f"uid-{len(self.specs)}",
                )
                self.workers[spec.worker_key] = existing
            return existing

    def has_named_worker(self, worker_key: str) -> bool:
        with self._lock:
            return worker_key in self.workers

    def named_worker_keys(self) -> frozenset[str]:
        with self._lock:
            listed = frozenset(self.workers)
        after_listing, self.after_listing = self.after_listing, None
        if after_listing is not None:
            after_listing()
        return listed

    def retire_worker(self, worker_key: str) -> None:
        with self._lock:
            self.retired.append(worker_key)
            self.workers.pop(worker_key, None)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        return self.workers.get(worker_key)

    def record_failure(
        self,
        worker_key: str,
        failure_reason: str,
        *,
        incarnation: str | None = None,
        **_kwargs: object,
    ) -> WorkerHandle:
        """Stop the worker and keep it as failed, unless the failure is from one it replaced, as Kubernetes does."""
        if incarnation is not None and self.workers[worker_key].incarnation != incarnation:
            return self.workers[worker_key]
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


def _run(backend: object, call: NamedWorkerCall, normal_key: str = _ALICE) -> WorkerHandle:
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
    async with execution_resources(response=True):
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
    async with execution_resources(response=True):
        _run(backend, _call(runtime_paths, "build", "small"))

        with pytest.raises(NamedWorkerError, match="was started with resource_profile small in this reply"):
            _run(backend, _call(runtime_paths, "build", "large"))
        assert [spec.resource_profile for spec in backend.specs] == ["small"]


async def _name_at_the_same_time(
    backend: object,
    runtime_paths: RuntimePaths,
    profiles: tuple[str | None, ...],
) -> list[WorkerHandle | BaseException]:
    """Make one call per profile that names ``e2e-0``, none of which has started the worker when the others name it."""
    named = threading.Barrier(len(profiles))

    def run(profile: str | None) -> WorkerHandle:
        call = _call(runtime_paths, "e2e-0", profile)
        spec = call.worker_spec(WorkerSpec(_ALICE, private_agent_names=frozenset()))
        named.wait(SETTLE_TIMEOUT_SECONDS)
        return ensure_named_worker(cast("WorkerBackend", backend), spec, call)

    async with execution_resources(response=True):
        return await asyncio.gather(*(asyncio.to_thread(run, profile) for profile in profiles), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("profiles", [("small", "large"), ("large", None), (None, "small", "large", None)])
async def test_calls_that_name_a_new_worker_at_the_same_time_start_it_with_one_size(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    profiles: tuple[str | None, ...],
) -> None:
    """The first call to be admitted sizes the worker: one that names no size takes it, and another size is refused."""
    results = await _name_at_the_same_time(backend, runtime_paths, profiles)

    [size] = {spec.resource_profile for spec in backend.specs}
    refused = [result for result in results if isinstance(result, NamedWorkerError)]
    started = [result for result in results if isinstance(result, WorkerHandle)]
    assert len(refused) + len(started) == len(profiles)
    assert len(refused) == sum(profile not in (None, size) for profile in profiles)
    assert all("was started with" in str(error) for error in refused)
    [worker_key] = {spec.worker_key for spec in backend.specs}
    assert await _retired(backend) == [worker_key]


@pytest.mark.asyncio
async def test_the_workers_of_a_reply_are_removed_when_it_ends(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A reply that ends with no background job removes every worker its calls started."""
    async with execution_resources(response=True):
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
    async with execution_resources(response=True) as resources:
        job = resources.acquire()
        # A job can be the first caller to name the worker, and borrows the reply's lifetime.
        async with execution_resources():
            started = _run(backend, _call(runtime_paths, "shard"))
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
    async with execution_resources(response=True) as resources:
        started = _run(backend, _call(runtime_paths, "shard"))
        job = resources.acquire()
    async with execution_resources(response=True):
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
    async with execution_resources(response=True):
        left_behind = _run(backend, _call(runtime_paths, "e2e-0"))
        async with execution_resources(response=True):
            concurrent = _run(backend, _call(runtime_paths, "e2e-0", turn_id="turn-2"), normal_key=_BOB)
    assert await _retired(backend) == []

    # The same turn resumes after the restart and names the same worker.
    async with execution_resources(response=True):
        resumed = _run(backend, _call(runtime_paths, "e2e-0"))

        pins = {key.split(":")[-2] for key in (left_behind.worker_key, concurrent.worker_key, resumed.worker_key)}
        assert len(pins) == 3
        # What the restart left behind still runs, so it counts against the agent's limit of two until cleanup
        # removes it.
        with pytest.raises(NamedWorkerError, match="already has 2 of the 2 named workers"):
            _run(backend, _call(runtime_paths, "e2e-1"))


@pytest.mark.asyncio
async def test_a_new_worker_past_the_agent_limit_is_refused_not_queued(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The limit counts one requester's running workers of the agent; a name that already runs is always reached."""
    async with execution_resources(response=True):
        first = _run(backend, _call(runtime_paths, "a"))
        _run(backend, _call(runtime_paths, "b"))
        # Another requester's workers of the same agent do not count against this one.
        _run(backend, _call(runtime_paths, "a"), normal_key=_BOB)

        with pytest.raises(NamedWorkerError, match="Cannot start worker 'c': this agent already has 2 of the 2"):
            _run(backend, _call(runtime_paths, "c"))
        assert len(backend.workers) == 3
        assert _run(backend, _call(runtime_paths, "a")).worker_key == first.worker_key

        # A stopped worker holds its place until it is removed.
        backend.set_status(first.worker_key, "idle")
        with pytest.raises(NamedWorkerError, match="already has 2 of the 2"):
            _run(backend, _call(runtime_paths, "c"))
        backend.retire_worker(first.worker_key)
        _run(backend, _call(runtime_paths, "c"))


@pytest.mark.asyncio
async def test_a_new_worker_past_the_deployment_limit_is_refused(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The deployment's limit counts every requester's and agent's named workers together."""
    backend.named_workers_max = 2
    async with execution_resources(response=True):
        _run(backend, _call(runtime_paths, "a"))
        _run(backend, _call(runtime_paths, "a"), normal_key=_BOB)

        with pytest.raises(NamedWorkerError, match="this deployment already has its limit of 2 named workers"):
            _run(backend, _call(runtime_paths, "b"))


@pytest.mark.asyncio
async def test_workers_that_are_starting_count_against_the_limit(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """Calls that start workers at the same time cannot pass the limit together."""
    backend.hold_starts = threading.Event()
    async with execution_resources(response=True):
        starting = [asyncio.create_task(asyncio.to_thread(_run, backend, _call(runtime_paths, name))) for name in "ab"]
        try:
            # Both starts are admitted and wait in the backend, which lists neither worker yet.
            for _ in starting:
                assert await asyncio.to_thread(backend.entered.acquire, timeout=SETTLE_TIMEOUT_SECONDS)
            assert backend.workers == {}

            with pytest.raises(NamedWorkerError, match="already has 2 of the 2 named workers"):
                await asyncio.to_thread(_run, backend, _call(runtime_paths, "c"))
        finally:
            backend.hold_starts.set()
            await asyncio.gather(*starting)
        assert len(backend.workers) == 2
        assert not named_module._starting


@pytest.mark.asyncio
async def test_a_start_that_finishes_while_the_workers_are_counted_still_counts(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """A worker whose start ends between the count of the workers that exist and of those starting counts once."""
    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=1, worker_scope="user_agent")})
    backend.hold_starts = threading.Event()
    first_started = threading.Event()

    def start_first() -> None:
        _run(backend, _call(runtime_paths, "a", config=config))
        first_started.set()

    def finish_the_first_start() -> None:
        # The listing has read no worker yet; the first worker now starts and leaves the starts in flight.
        assert backend.hold_starts is not None
        backend.hold_starts.set()
        assert first_started.wait(SETTLE_TIMEOUT_SECONDS)

    async with execution_resources(response=True):
        first = asyncio.create_task(asyncio.to_thread(start_first))
        assert await asyncio.to_thread(backend.entered.acquire, timeout=SETTLE_TIMEOUT_SECONDS)
        backend.after_listing = finish_the_first_start

        with pytest.raises(NamedWorkerError, match="already has 1 of the 1 named workers"):
            await asyncio.to_thread(_run, backend, _call(runtime_paths, "b", config=config))
        await first
        assert len(backend.workers) == 1


@pytest.mark.asyncio
async def test_reusing_a_named_worker_does_not_depend_on_the_deployment_list(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace-wide list failure need not block a call to a healthy, already admitted worker."""
    async with execution_resources(response=True):
        call = _call(runtime_paths, "a")
        worker = _run(backend, call)

        def list_unavailable() -> frozenset[str]:
            msg = "cannot list Deployments"
            raise WorkerBackendError(msg)

        monkeypatch.setattr(backend, "named_worker_keys", list_unavailable)
        assert _run(backend, call) == worker
        with pytest.raises(WorkerBackendError, match="cannot list Deployments"):
            _run(backend, _call(runtime_paths, "new"))
        backend.workers.pop(worker.worker_key)
        with pytest.raises(WorkerBackendError, match="cannot list Deployments"):
            _run(backend, call)


@pytest.mark.parametrize("method", ["list_namespaced_deployment", "read_namespaced_deployment"])
@pytest.mark.asyncio
async def test_kubernetes_admission_read_timeout_releases_the_lock(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Each admission read has a bounded request timeout, and a stall cannot wedge future calls."""
    kubernetes, apps, _core = _named_backend(tmp_path)
    monkeypatch.setattr(kubernetes_backend_module, "_NAMED_WORKER_REQUEST_TIMEOUT_SECONDS", 0.02, raising=False)
    stall = threading.Event()
    timeouts: list[float | None] = []

    def stalled(*_args: object, _request_timeout: float | None = None, **_kwargs: object) -> object:
        timeouts.append(_request_timeout)
        assert not stall.wait(_request_timeout if _request_timeout is not None else 0.1)
        msg = "Deployment read timed out"
        raise ReadTimeoutError(None, "/apis/apps/v1", msg)

    async with execution_resources(response=True):
        warm = _call(runtime_paths, "warm")
        first = _run(kubernetes, warm)
        request = _call(runtime_paths, "new") if method.startswith("list_") else warm
        with monkeypatch.context() as patch:
            patch.setattr(apps, method, stalled)
            with pytest.raises(WorkerBackendError, match=r"named worker admission.*read timed out"):
                await asyncio.wait_for(asyncio.to_thread(_run, kubernetes, request), SETTLE_TIMEOUT_SECONDS)
        assert timeouts == [0.02]
        reused = await asyncio.wait_for(asyncio.to_thread(_run, kubernetes, warm), SETTLE_TIMEOUT_SECONDS)
        assert (reused.worker_key, reused.incarnation) == (first.worker_key, first.incarnation)
        assert reused.status == "ready"


@pytest.mark.asyncio
async def test_a_worker_that_crashed_is_replaced_by_a_new_one(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """The next call with the name of a failed worker removes what it left and starts again, and says so."""
    async with execution_resources(response=True):
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
async def test_cancelling_a_reply_retires_a_named_worker_whose_start_finishes_later(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
) -> None:
    """Retirement must wait for a blocking start, even after its awaiting reply was cancelled."""
    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=1, worker_scope="user_agent")})
    pending: list[asyncio.Task[WorkerHandle]] = []
    release_start = threading.Event()
    backend.hold_starts = release_start

    async def reply() -> None:
        async with execution_resources(response=True):
            call = _call(runtime_paths, "build", config=config)
            pending.append(asyncio.create_task(asyncio.to_thread(_run, backend, call)))
            await asyncio.shield(pending[0])

    cancelled = asyncio.create_task(reply())
    try:
        assert await asyncio.to_thread(backend.entered.acquire, timeout=SETTLE_TIMEOUT_SECONDS)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        # Drain any retirement the cancelled reply tried before its Deployment existed.
        await _retired(backend)
    finally:
        release_start.set()
    worker = await pending[0]

    assert await _retired(backend) == [worker.worker_key]
    assert backend.workers == {}
    async with execution_resources(response=True):
        _run(backend, _call(runtime_paths, "retry", config=config))
    assert len(await _retired(backend)) == 2


@pytest.mark.asyncio
async def test_a_call_whose_reply_ended_starts_no_worker(runtime_paths: RuntimePaths, backend: _FakeBackend) -> None:
    """A call that outlives its reply and that reply's jobs cannot start a worker nothing would remove."""
    async with execution_resources(response=True):
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
    retire = backend.retire_worker

    def fail_one(worker_key: str) -> None:
        if worker_key == stuck:
            msg = "cluster unavailable"
            raise WorkerBackendError(msg)
        retire(worker_key)

    async with execution_resources(response=True):
        stuck, removed = (_run(backend, _call(runtime_paths, name)).worker_key for name in ("a", "b"))
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
        *(
            (
                "code",
                Config(agents={"code": AgentConfig(display_name="Code", named_workers=2, **scope)}),
                {},
                True,
                message,
            )
            for scope, message in (
                # Only this scope's worker key names whose limit a named worker counts against.
                ({}, "per requester and agent"),
                ({"worker_scope": "shared"}, "per requester and agent"),
                ({"worker_scope": "user"}, "per requester and agent"),
                ({"private": AgentPrivateConfig(per="user")}, "per requester and agent"),
                ({"private": AgentPrivateConfig(per="user_agent")}, None),
            )
        ),
        ("code", _CONFIG, {"MINDROOM_WORKER_BACKEND": "docker"}, True, "need the kubernetes worker backend"),
        (
            "code",
            _CONFIG,
            {"MINDROOM_KUBERNETES_NAMED_WORKERS_MAX": "0"},
            True,
            "MINDROOM_KUBERNETES_NAMED_WORKERS_MAX",
        ),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normal_key",
    ["v1:default:shared:code", "v1:default:user:@alice:example.org", "v1:default:unscoped:code"],
)
async def test_a_worker_beside_a_normal_worker_that_is_not_per_requester_and_agent_is_refused(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    normal_key: str,
) -> None:
    """Whatever the config said when the call was read, the key the call is routed by decides whose limit applies."""
    async with execution_resources(response=True):
        with pytest.raises(NamedWorkerError, match="per requester and agent"):
            _run(backend, _call(runtime_paths, "a"), normal_key=normal_key)
    assert backend.specs == []


def test_a_backend_without_named_workers_refuses_them(runtime_paths: RuntimePaths) -> None:
    """Only a backend that limits and removes named workers runs them."""

    class _PlainBackend:
        backend_name = "docker"
        named_workers_max = 0

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


def _tool_context(runtime_paths: RuntimePaths, turn_id: str = _TURN, *, config: Config = _CONFIG) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=object(),
        config=config,
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
        async with execution_resources(response=True):
            normal = await shell("echo hi")
            first = await shell("echo hi", worker="e2e-0", resource_profile="large")
            again = await shell(args="echo hi", worker="e2e-0")
            named_endpoint = backend.workers[backend.specs[-1].worker_key].endpoint

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
    assert normal_url != named_url == named_endpoint
    assert (named_post["worker_key"], named_post["worker_scope"]) == (named_spec.worker_key, "user_agent")
    assert named_post["execution_identity"] == normal_post["execution_identity"]
    assert await _retired(backend) == [named_spec.worker_key]


_MISSING = "/missing-program-xyz"
_LONG_MISSING = "/" + "/".join(["d" * 200] * 15)
_NEVER_SAVED = 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "exit_code", "threshold", "printed"),
    [
        (["bash", "-c", "printf '%100s' shard-output"], 0, 32, "shard-output"),
        (["bash", "-c", "printf '%100s' shard-output; exit 17"], 17, 32, "shard-output"),
        # A program the worker cannot start ends as a command that failed with a shell's words, saved or not.
        ([_MISSING], 127, 1, f"Stderr:\n{_MISSING}: No such file or directory\n"),
        ([_MISSING], 127, 32, f"Stderr:\n{_MISSING}: No such file or directory\n"),
        ([_MISSING], 127, _NEVER_SAVED, f"Stderr:\n{_MISSING}: No such file or directory\n"),
        (["missing-program-xyz"], 127, _NEVER_SAVED, "Stderr:\nmissing-program-xyz: command not found\n"),
        ([_LONG_MISSING], 127, _NEVER_SAVED, f"Stderr:\n{_LONG_MISSING}: No such file or directory\n"),
        (["/etc/passwd"], 126, 32, "Stderr:\n/etc/passwd: Permission denied\n"),
    ],
)
async def test_a_cli_call_reads_saved_output_from_its_named_worker(
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    exit_code: int,
    threshold: int,
    printed: str,
) -> None:
    """A CLI call's receipt keeps the real exit status and saved output path, whether or not the runner saves it."""
    from uuid import uuid4  # noqa: PLC0415

    from mindroom.agent_cli.protocol import ToolCallOperation  # noqa: PLC0415
    from mindroom.agent_cli.session import CliTurnOwner  # noqa: PLC0415
    from mindroom.agent_cli.shell_contract import AgentCliShellEnv  # noqa: PLC0415
    from mindroom.tool_system.output_files import ToolOutputFilePolicy  # noqa: PLC0415
    from tests.conftest import live_turn_tools_for_test  # noqa: PLC0415
    from tests.minimal_agent_fixtures import settled_receipt  # noqa: PLC0415
    from tests.test_agent_tool_calls import _catalog, _toolkit  # noqa: PLC0415

    worker_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "runner.yaml",
        storage_path=tmp_path / "runner-storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    runner_shell = get_tool_by_name(
        "shell",
        worker_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        tool_output_workspace_root=tmp_path,
        tool_output_auto_save_threshold_bytes=threshold,
        worker_target=None,
    )
    result = await _offered_entrypoint(runner_shell, "run_shell_command")(command, worker="shard")
    saved_path = None
    if threshold != _NEVER_SAVED:
        receipt = result["mindroom_tool_output"]
        assert receipt["auto_saved"] is True
        saved_path = receipt["path"]
        assert printed.strip() in (tmp_path / saved_path).read_text()
    _route_through(monkeypatch, backend, responder=lambda _url, _payload: {"ok": True, "result": result})
    shell, _target = _shell(runtime_paths)

    async def run_shell_command(args: list[str], worker: str) -> object:
        """Send the command to the named worker through its ordinary shell proxy."""
        return await shell(args, worker=worker)

    async def authorize(_key: object, _args: object) -> None:
        pass

    catalog = await _catalog(tmp_path, [_toolkit("shell", run_shell_command)])
    catalog.runtime_context = _tool_context(runtime_paths)
    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, _TURN, "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(workspace_root=tmp_path, auto_save_threshold_bytes=threshold),
    )
    owner.shell_env = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")
    async with execution_resources(response=True), owner._window("driver"):
        queued = await owner.operation(
            window="driver",
            operation=ToolCallOperation(
                operation="tools.call",
                call_id=uuid4(),
                toolkit="shell",
                function="run_shell_command",
                arguments={"args": command, "worker": "shard"},
            ),
        )
        cli_receipt = await settled_receipt(owner, queued["call_id"])
    await owner.close()

    # `workers run` streams its command's output instead, so only a `tools call` reads a result the runner formatted.
    assert cli_receipt["status"] == "completed"
    outcome = cli_receipt["outcome"]
    if saved_path is not None:
        saved_to = outcome["mindroom_tool_output"]
        assert saved_to["path"] == saved_path
        outcome = saved_to["preview"]
    header, _, body = outcome.partition("\n")
    assert header == "[worker: shard, started]"
    assert body.startswith(f"Exit code {exit_code}\n") is bool(exit_code)
    assert printed.strip() in body


@pytest.mark.asyncio
@pytest.mark.parametrize(("named_workers", "scope"), [(0, "user_agent"), (2, "shared")])
@pytest.mark.parametrize("hidden", [{"worker": "build"}, {"resource_profile": "large"}])
async def test_cli_catalog_explains_why_named_workers_are_not_offered(
    hidden: dict[str, str],
    named_workers: int,
    scope: str,
    tmp_path: Path,
    runtime_paths: RuntimePaths,
    backend: _FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real CLI dispatch refuses a hidden named-worker argument as such a refusal, so `workers run` exits 125."""
    from uuid import uuid4  # noqa: PLC0415

    from mindroom.agent_cli.main import main  # noqa: PLC0415
    from mindroom.agent_cli.protocol import ToolCallOperation  # noqa: PLC0415
    from mindroom.agent_cli.session import CliTurnOwner  # noqa: PLC0415
    from mindroom.agent_cli.shell_contract import AgentCliShellEnv  # noqa: PLC0415
    from mindroom.tool_system.output_files import ToolOutputFilePolicy  # noqa: PLC0415
    from mindroom.tool_system.tool_access import ToolKey  # noqa: PLC0415
    from tests.conftest import live_turn_tools_for_test  # noqa: PLC0415
    from tests.minimal_agent_fixtures import settled_receipt  # noqa: PLC0415
    from tests.test_agent_tool_calls import _catalog  # noqa: PLC0415

    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=named_workers, worker_scope=scope)})
    target = resolve_worker_target(scope, "code", _identity(), private_agent_names=frozenset())
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=True,
        runtime_config=config,
        worker_target=target,
    )
    posts = _route_through(monkeypatch, backend)
    catalog = await _catalog(tmp_path, [replace(adapt_toolkit(shell, key="shell"), name="shell")])
    catalog.runtime_context = _tool_context(runtime_paths, config=config)
    binding = await catalog.bind(ToolKey("shell", "run_shell_command"))
    assert binding.function.parameters["additionalProperties"] is False
    assert {"worker", "resource_profile"}.isdisjoint(binding.function.parameters["properties"])

    async def authorize(_key: object, _args: object) -> None:
        pass

    owner = live_turn_tools_for_test(
        CliTurnOwner(catalog.runtime_context.execution_identity, _TURN, "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=ToolOutputFilePolicy(tmp_path),
    )
    owner.shell_env = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")
    try:
        async with execution_resources(response=True), owner._window("driver"):
            queued = await owner.operation(
                window="driver",
                operation=ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="shell",
                    function="run_shell_command",
                    arguments={"args": ["true"], "timeout": 3600, "tail": 100} | hidden,
                ),
            )
            receipt = await settled_receipt(owner, queued["call_id"])
    finally:
        await owner.close()

    assert receipt["status"] == "failed"
    assert receipt["outcome"] == named_workers_refusal("code", config, runtime_paths)
    assert receipt["error_kind"] == "named_worker_refusal"
    assert not posts

    class Client:
        def operation(self, _payload: object) -> dict[str, object]:
            return receipt

        def stream(self, _call_id: str) -> object:
            return iter([receipt])

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    assert main(["workers", "run", "build", "--", "true"]) == 125
    assert capsys.readouterr().err == f"mindroom-agent: {receipt['outcome']}\n"


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
        async with execution_resources(response=True):
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
        async with execution_resources(response=True):
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


@pytest.mark.parametrize("named_workers", [0, 2])
@pytest.mark.parametrize("deployment_limit", [0, 16])
def test_the_shell_offers_named_workers_only_to_an_agent_that_may_run_them(
    runtime_paths: RuntimePaths,
    named_workers: int,
    deployment_limit: int,
) -> None:
    """The model sees worker arguments only when its deployment and agent may name workers."""
    runtime_paths = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "MINDROOM_KUBERNETES_NAMED_WORKERS_MAX": str(deployment_limit),
        },
    )
    enabled = bool(named_workers and deployment_limit)
    config = Config(
        agents={"code": AgentConfig(display_name="Code", named_workers=named_workers, worker_scope="user_agent")},
    )
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
    assert ({"worker", "resource_profile"} <= offered) is enabled
    assert {"args", "tail", "timeout"} <= offered
    assert ("mindroom-agent workers run" in (function.description or "")) is enabled


@pytest.mark.parametrize("named_workers", [0, 2])
@pytest.mark.parametrize("deferred", [False, True], ids=["eager", "deferred"])
def test_a_minimal_agent_is_told_of_named_workers_only_when_it_may_run_them(
    runtime_paths: RuntimePaths,
    named_workers: int,
    deferred: bool,
) -> None:
    """Minimal Bash has no ``worker`` argument, so its prompt names the command that runs in a named worker."""
    config = Config(
        models={"default": ModelConfig(provider="openai", id="test-model")},
        agents={
            "code": AgentConfig(
                display_name="Code",
                tools=[{"shell": {"defer": True}}] if deferred else ["shell"],
                worker_tools=["shell"],
                named_workers=named_workers,
                worker_scope="user_agent",
            ),
        },
    )
    persist_entity_accounts(config, runtime_paths)
    build = agents.create_agent(
        "code",
        config,
        runtime_paths,
        _identity(),
        agent_mode="minimal",
        knowledge=None,
        model=turn_model(config, runtime_paths, "code"),
        plugins=config_plugins(config, runtime_paths),
    )
    turn = minimal_turn(
        build,
        session_preamble="",
        transient_memory="",
        system_enrichment=(),
        transient_enrichment=(),
    )

    told = "mindroom-agent workers run NAME -- COMMAND" in system_text(turn.system_messages)
    assert told is bool(named_workers)


@pytest.mark.asyncio
async def test_a_shell_outside_a_worker_refuses_to_name_one(tmp_path: Path) -> None:
    """A shell that runs in MindRoom's own process has no worker to route a named call to."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=2)})
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
@pytest.mark.parametrize("supervised", [False, True])
async def test_named_shell_launch_errors_fail_the_tool_without_matching_command_output(
    tmp_path: Path,
    supervised: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named launch errors fail the tool without treating successful command output as a diagnostic."""
    from mindroom.shell.supervisor import SHELL_SUPERVISOR_SOCKET_ENV  # noqa: PLC0415

    process_env = {"MINDROOM_SANDBOX_RUNNER_MODE": "true"}
    if supervised:
        monkeypatch.setenv(SHELL_SUPERVISOR_SOCKET_ENV, str(tmp_path / "missing.sock"))
    else:
        monkeypatch.delenv(SHELL_SUPERVISOR_SOCKET_ENV, raising=False)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )
    shell = get_tool_by_name(
        "shell",
        runtime_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        worker_target=None,
    )
    call = _offered_entrypoint(shell, "run_shell_command")
    command = ["definitely-not-a-binary-xyz", "arg"]
    if supervised:
        diagnostic = "Shell supervisor is unavailable"
        assert diagnostic in await call(command)
        with pytest.raises(ValueError, match=diagnostic):
            await call(command, worker="build")
    else:
        assert "definitely-not-a-binary-xyz" in await call(command)
        # A program that cannot start ends as a command that printed a shell's words and exited with its code.
        said = "definitely-not-a-binary-xyz: command not found"
        failed = await call(command, worker="build")
        assert failed == f"Exit code 127\nStderr:\n{said}"
        assert failed == await call(["bash", "-c", f"echo '{said}' >&2; exit 127"], worker="build")
        assert await call(["printf", "Error: ordinary output"], worker="build") == "Error: ordinary output"


@pytest.mark.asyncio
async def test_a_named_launch_failure_saves_to_an_output_path_as_a_failed_command_does(tmp_path: Path) -> None:
    """A program that cannot start replaces an existing output file with what a command that failed so would save."""
    runner_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    shell = get_tool_by_name(
        "shell",
        runner_paths,
        tools=builtin_tool_registry(),
        runs_in_worker=False,
        tool_output_workspace_root=tmp_path,
        worker_target=None,
    )
    call = _offered_entrypoint(shell, "run_shell_command")
    said = f"{_MISSING}: No such file or directory"
    for name in ("launch.txt", "failed.txt"):
        (tmp_path / name).write_text("previous contents")

    launch = json.loads(await call([_MISSING], worker="build", mindroom_output_path="launch.txt"))
    command = ["bash", "-c", f"echo '{said}' >&2; exit 127"]
    failed = json.loads(await call(command, worker="build", mindroom_output_path="failed.txt"))

    assert launch["mindroom_tool_output"] == {**failed["mindroom_tool_output"], "path": "launch.txt"}
    assert launch["mindroom_tool_output"]["exit_code"] == 127
    assert launch["mindroom_tool_output"]["overwritten"] is True
    saved = (tmp_path / "launch.txt").read_text()
    assert saved == (tmp_path / "failed.txt").read_text() == f"Exit code 127\nStderr:\n{said}\n"


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
    job_context = None

    async def attempt(_run_state: object, _continuation: object) -> AsyncGenerator[str | AttemptResolved]:
        started.append(_run(backend, _call(runtime_paths, "e2e-0")).worker_key)
        resources = current_execution_resources()
        assert resources is not None
        # Accepted jobs keep a reference and the response context while they run independently.
        nonlocal job_context
        jobs.append(resources.acquire())
        job_context = copy_context()
        yield AttemptResolved(CompletedAttempt(replayable_text="done", has_visible_content=True))

    async def reuse_from_job() -> str:
        async with execution_resources():
            return _run(backend, _call(runtime_paths, "e2e-0")).worker_key

    with tool_runtime_context(_tool_context(runtime_paths)):
        await _collect_turn(attempt, ctx=_ctx(background_tool_jobs=True))
    assert await _retired(backend) == []
    assert job_context is not None
    assert await asyncio.create_task(reuse_from_job(), context=job_context) == started[0]

    await jobs[0].release()

    assert await _retired(backend) == started


# Against the Kubernetes backend, which a fake cluster API serves: what it applies, counts, and removes.


def _annotate(apps_api: _FakeAppsApi, handle: WorkerHandle, **annotations: str) -> None:
    apps_api.deployments[handle.worker_id].metadata.annotations.update(
        {f"mindroom.ai/{name.replace('_', '-')}": value for name, value in annotations.items()},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("profiles", [("small", "large"), ("large", None)])
async def test_calls_that_name_a_new_worker_at_the_same_time_apply_one_pod_template(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
    profiles: tuple[str | None, ...],
) -> None:
    """A second size never reaches the cluster, where it would replace the pod under the first call's command."""
    kubernetes, apps_api, _core_api = _named_backend(tmp_path)

    results = await _name_at_the_same_time(kubernetes, runtime_paths, profiles)

    assert any(isinstance(result, WorkerHandle) for result in results)
    assert len(apps_api.created_bodies) == 1
    assert apps_api.deleted_names == []
    [deployment] = apps_api.deployments.values()
    assert deployment.metadata.annotations["mindroom.ai/startup-count"] == "1"


@pytest.mark.asyncio
async def test_a_named_worker_counts_against_the_limit_until_it_is_removed(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """A worker that runs unused, or that stopped, still holds its place; one being deleted does not."""
    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=1, worker_scope="user_agent")})
    kubernetes, apps_api, _core_api = _named_backend(tmp_path)

    async with execution_resources(response=True):
        first = _run(kubernetes, _call(runtime_paths, "a", config=config))
        # Unused for longer than the worker idle timeout, the worker still runs its pod.
        _annotate(apps_api, first, last_used_at="0.0")
        assert apps_api.deployments[first.worker_id].spec.replicas == 1
        with pytest.raises(NamedWorkerError, match="already has 1 of the 1 named workers"):
            _run(kubernetes, _call(runtime_paths, "b", config=config))

        # Stopped after a failure, it stays until cleanup removes it or a call with its name replaces it.
        kubernetes.record_failure(first.worker_key, "pod was evicted")
        assert apps_api.deployments[first.worker_id].spec.replicas == 0
        with pytest.raises(NamedWorkerError, match="already has 1 of the 1 named workers"):
            _run(kubernetes, _call(runtime_paths, "b", config=config))
        replacement = _run(kubernetes, _call(runtime_paths, "a", config=config))
        assert replacement.worker_key == first.worker_key

        # Its removal has begun and waits for the pod to stop.
        apps_api.delete_read_lag_by_name[first.worker_id] = 1000
        apps_api.delete_namespaced_deployment(first.worker_id, "chat", propagation_policy="Foreground")
        assert first.worker_id in apps_api.deployments
        second = _run(kubernetes, _call(runtime_paths, "b", config=config))

    assert second.worker_key != first.worker_key


@pytest.mark.asyncio
async def test_named_workers_a_restart_left_behind_count_until_the_first_maintenance_pass_removes_them(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """No reply can reach a named worker an earlier process started, so the first pass removes it, whatever its age."""
    config = Config(agents={"code": AgentConfig(display_name="Code", named_workers=1, worker_scope="user_agent")})
    before_restart, apps_api, core_api = _named_backend(tmp_path)
    async with execution_resources(response=True):
        left_behind = _run(before_restart, _call(runtime_paths, "a", config=config))
    # Long unused, as after a primary that was down for a while.
    _annotate(apps_api, left_behind, last_used_at="0.0")
    left_behind_root = before_restart.storage_root / before_restart._state_subpath(left_behind.worker_key)
    assert left_behind_root.is_dir()

    restarted, _apps_api, _core_api = _named_backend(tmp_path, process_started_at=time.time())
    restarted._resources.apps_api, restarted._resources.core_api = apps_api, core_api
    async with execution_resources(response=True):
        with pytest.raises(NamedWorkerError, match="already has 1 of the 1 named workers"):
            _run(restarted, _call(runtime_paths, "a", config=config, turn_id="turn-after-restart"))

        restarted.maintain_workers()

        assert left_behind.worker_id not in apps_api.deployments
        assert left_behind_root.exists() is False
        started = _run(restarted, _call(runtime_paths, "a", config=config, turn_id="turn-after-restart"))
        # A worker this process started stays through its passes.
        restarted.maintain_workers()
        assert apps_api.deployments[started.worker_id].spec.replicas == 1
