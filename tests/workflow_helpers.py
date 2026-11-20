"""Shared helpers for the workflow tests.

`FakeWorkflowGateway` answers the SDK over real HTTP (pure level).
`WorkflowStack` is the engine harness: the real workflow store, script store, broker and engine, with a fake script
manager, so a test plays the script by calling the engine's gateway methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

import httpx
from fastapi import FastAPI

import mindroom.workflow as sdk
from mindroom.api.workflow_gateway import bind_workflow_engine
from mindroom.api.workflow_gateway import router as workflow_router
from mindroom.approval.tool_approval import ToolApprovalDecision
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.message_target import MessageTarget
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker
from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptRunRecord, ScriptRunState
from mindroom.script_runs.reasons import PROCESS_EXIT_OBSERVED
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import read_result_payload
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.models import (
    NodeReceipt,
    NodeRequest,
    WorkflowRun,
    new_workflow_run_id,
)
from mindroom.workflow_runs.store import WorkflowRunStore
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
)
from tests.scripted_agent_helpers import registered_toolkit
from tests.tool_job_helpers import call_job_tool, job_actor, tool_job_runtime, toolkit_job_tools

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    import pytest

    from mindroom.approval.tool_approval import BackgroundScriptToolOrigin
    from mindroom.script_runs.manager import ScriptRunLimits
    from mindroom.script_runs.models import ScriptToolGrant
    from mindroom.tool_jobs.runtime import BackgroundJob, JobGrant
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from tests.tool_job_helpers import ProcessRuntime

SCRIPT_RUN_ID = "script-" + "a" * 32
TOKEN = "invented-capability"  # noqa: S105 - an invented placeholder, not a credential.


def completed(result: object, **extra: object) -> dict[str, object]:
    """Return a receipt for a node that completed with *result*."""
    return {"state": "completed", "cached": False, "result": result, "error": None, **extra}


def failed(kind: str, message: str, *, state: str = "failed") -> dict[str, object]:
    """Return a receipt for a node that ended without a result."""
    return {"state": state, "cached": False, "result": None, "error": {"kind": kind, "message": message}}


class FakeWorkflowGateway:
    """A threaded HTTP server that records the SDK's requests and answers nodes from `respond`."""

    def __init__(self, token_path: Path) -> None:
        self.token_path = token_path
        token_path.write_text(TOKEN + "\n", encoding="utf-8")
        self.args: object = {}
        self.respond: Callable[[dict[str, object]], dict[str, object]] = lambda _request: completed(None)
        self.requests: list[tuple[str, str]] = []
        self.nodes: list[dict[str, object]] = []
        self.logs: list[str] = []
        self.finished: dict[str, object] | None = None
        self.results: dict[str, object] = {}
        self.polls_before_settled = 0
        """How many `GET nodes/{id}` answers say `running` before the receipt of `respond` is returned."""
        self.drop_connections = 0
        """How many of the next requests are cut off without an answer, as a restarting gateway does."""
        self.refuse: tuple[int, object] | None = None
        """A status and JSON body answered to every request instead of the normal answer."""
        self._receipts: dict[str, dict[str, object]] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}/api/script-gateway"

    def __enter__(self) -> Self:
        """Start serving."""
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _answer(self, method: str, route: str, body: dict[str, object]) -> object:  # noqa: PLR0911
        if (method, route) == ("GET", "run"):
            return {"run_id": "wf-" + "b" * 32, "attempt": 1, "args": self.args}
        if (method, route) == ("POST", "nodes"):
            self.nodes.append(body)
            node_id = f"n{len(self.nodes) - 1}"
            self._receipts[node_id] = {"id": node_id, **self.respond(body)}
            if self.polls_before_settled:
                return {"id": node_id, "state": "running", "cached": False, "result": None, "error": None}
            return self._receipts[node_id]
        if method == "GET" and route.startswith("nodes/"):
            node_id = route.removeprefix("nodes/").partition("?")[0]
            self.polls_before_settled -= 1
            if self.polls_before_settled > 0:
                return {"id": node_id, "state": "running", "cached": False, "result": None, "error": None}
            return self._receipts[node_id]
        if method == "GET" and route.startswith("results/"):
            return self.results[route.removeprefix("results/")]
        if (method, route) == ("POST", "log"):
            self.logs.append(str(body["text"]))
            return {}
        if (method, route) == ("POST", "finish"):
            self.finished = body
            return {}
        msg = f"Unexpected request {method} {route}"
        raise AssertionError(msg)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        gateway = self
        prefix = f"/api/script-gateway/workflow/{SCRIPT_RUN_ID}/"

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                gateway.requests.append((self.command, self.path))
                if gateway.drop_connections:
                    gateway.drop_connections -= 1
                    self.close_connection = True
                    return
                if self.headers.get("Authorization") != f"Bearer {TOKEN}" or not self.path.startswith(prefix):
                    status, answer = 404, {"detail": "Workflow run not found."}
                elif gateway.refuse is not None:
                    status, answer = gateway.refuse
                else:
                    try:
                        status, answer = 200, gateway._answer(self.command, self.path.removeprefix(prefix), body)
                    except AssertionError as exc:
                        status, answer = 400, {"detail": str(exc)}
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _handle  # noqa: N815 - the names `http.server` dispatches to.

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        return Handler


def sdk_env(monkeypatch: pytest.MonkeyPatch, gateway: FakeWorkflowGateway) -> None:
    """Point the SDK at *gateway* as the launcher would, with no pause between retries."""
    monkeypatch.setenv(GATEWAY_URL_ENV, gateway.url)
    monkeypatch.setenv(RUN_ID_ENV, SCRIPT_RUN_ID)
    monkeypatch.setenv(TOKEN_PATH_ENV, str(gateway.token_path))
    monkeypatch.setattr(sdk, "_RETRY_SECONDS", 0.0)
    sdk._gateway.cache_clear()


_SETTLED_STATES = frozenset({"completed", "failed", "cancelled", "indeterminate"})


def tool_args(toolkit: str, function: str, **arguments: object) -> dict[str, object]:
    """Return the arguments of a `tool` node, as the SDK's `tool()` sends them."""
    return {"toolkit": toolkit, "function": function, "arguments": arguments}


class NodeFailed(Exception):  # noqa: N818 - named as the plan's tests name it.
    """A node that ended without a result, as the SDK would raise it."""

    def __init__(self, receipt: NodeReceipt) -> None:
        error = receipt.error or {}
        super().__init__(str(error.get("message")))
        self.kind = str(error.get("kind"))
        self.receipt = receipt


class CallFailed(Exception):  # noqa: N818 - named after what happened to the call.
    """A tool call that failed, with the text the model reads."""


class CountingTool:
    """State of the stack's `counter` toolkit, whose effect is one line appended to a file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.before_effect: Callable[[], object] | None = None

    @property
    def lines(self) -> list[str]:
        """The lines the tool has appended so far."""
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []

    def toolkit(self) -> type[Toolkit]:
        """Return the toolkit class to register as `counter`."""
        counter = self

        class CounterTools(Toolkit):
            def __init__(self) -> None:
                super().__init__(name="counter", tools=[self.append, self.fail, self.text])

            def append(self, line: str) -> str:
                """Append one line to the file."""
                counter.calls += 1
                if counter.before_effect is not None:
                    counter.before_effect()
                with counter.path.open("a", encoding="utf-8") as file:
                    file.write(f"{line}\n")
                return "appended"

            def fail(self) -> str:
                """Raise."""
                counter.calls += 1
                msg = "The counter failed."
                raise RuntimeError(msg)

            def text(self, size: int) -> str:
                """Return a text of *size* characters."""
                counter.calls += 1
                return "x" * size

        return CounterTools


class _StackResolver:
    """The live runtime of the stack's one owner; approvals answer at once unless a test holds them."""

    def __init__(self, context: ToolRuntimeContext) -> None:
        self.context = context
        self.asks: list[str] = []
        self.hold_approvals: asyncio.Event | None = None
        self.decision = ToolApprovalDecision(approved=True)

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool | None:
        del run, config
        return True

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        del run
        return replace(self.context, correlation_id=correlation_id)

    def resolve_worker_authority(
        self,
        run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        return ScriptRuntimeWorkerAuthority(
            worker_id=run.worker_id,
            local_unsafe=True,
            worker_target=context.resolve_worker_target(),
        )

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        del origin, context, arguments, timeout_seconds
        self.asks.append(f"{grant.toolkit_name}.{grant.function_name}")
        if self.hold_approvals is not None:
            await self.hold_approvals.wait()
        return self.decision

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        del origin, reason

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason


class FakeScripts:
    """The script manager as the engine uses it, with no process: real attempt records that the test ends."""

    def __init__(self, store: ScriptRunStore, broker: ScriptToolBroker) -> None:
        self.store = store
        self.broker = broker
        self.launched: list[ScriptRunRecord] = []
        self.limits: list[ScriptRunLimits | None] = []
        self.tokens: dict[str, str] = {}
        self.hold_launch: asyncio.Event | None = None
        """While set and not released, `run` has written its attempt's record and waits, as a starting process does."""
        self.refuse_launch: Exception | None = None

    # The real confined read of a script's source, which needs nothing of the manager itself.
    resolve_source = ScriptRunManager.resolve_source

    async def run(
        self,
        context: ToolRuntimeContext,
        *,
        source: str,
        workflow_run_id: str,
        resource_profile: str | None = None,
        limits: ScriptRunLimits | None = None,
    ) -> ScriptRunRecord:
        """Record one running attempt of *workflow_run_id* and keep its capability token."""
        if self.refuse_launch is not None:
            raise self.refuse_launch
        token, token_hash = mint_script_capability()
        attempt = self.store.create_run(
            ScriptRunRecord(
                run_id=f"script-{uuid4().hex}",
                agent_name=context.agent_name,
                owner_user_id=context.requester_id,
                room_id=context.room_id,
                thread_root_event_id=context.resolved_thread_id,
                execution_identity=serialize_tool_execution_identity(context.execution_identity),
                source_digest=hashlib.sha256(source.encode()).hexdigest(),
                # Empty, so a call that runs proves the broker reads the owner's live tools and no launch snapshot.
                grants=(),
                token_hash=token_hash,
                workflow_run_id=workflow_run_id,
                resource_profile=resource_profile,
                local_unsafe=True,
                max_tool_calls_per_minute=600 if limits is None else limits.max_tool_calls_per_minute,
            ),
        )
        self.tokens[attempt.run_id] = token
        self.limits.append(limits)
        self.launched.append(self.store.transition_run(attempt.run_id, state=ScriptRunState.RUNNING))
        if self.hold_launch is not None:
            await self.hold_launch.wait()
        return self.launched[-1]

    async def revoke(self, run_id: str, *, reason: str) -> ScriptRunRecord:
        """Revoke the attempt's capability and the broker's work for it."""
        revoked = self.store.request_cancel(run_id, reason=reason)
        await self.broker.cancel_run(run_id)
        return revoked

    async def reconcile_durable(self, *, run_id: str) -> ScriptRunRecord:
        """End a revoked attempt as cancelled, as the manager does once its process is confirmed dead."""
        attempt = self.store.get_run(run_id)
        if attempt.state in TERMINAL_SCRIPT_RUN_STATES or attempt.cancel_requested_at is None:
            return attempt
        return await self.end(run_id, ScriptRunState.CANCELLED)

    async def end(
        self,
        run_id: str,
        state: ScriptRunState,
        *,
        reason: str = PROCESS_EXIT_OBSERVED,
        output: str = "",
    ) -> ScriptRunRecord:
        """End the attempt's process as the manager records it: the exit, the broker's calls, the terminal state."""
        exit_code = 0 if state is ScriptRunState.EXITED else 1
        self.store.record_process_exit(
            run_id,
            exit_code=exit_code,
            error=None,
            output=output,
            cancellation_reason=reason,
        )
        await self.broker.cancel_run(run_id)
        return self.store.finalize_cleaned_run(run_id, state=state)


def owner_context(
    tmp_path: Path,
    *,
    extra_tools: tuple[str, ...] = (),
    process_env: Mapping[str, str] = MappingProxyType({}),
    require_approval: tuple[str, ...] = ("append",),
) -> ToolRuntimeContext:
    """Return the tool context of the stack's owner: the agent `watcher` with `counter` and `shell`, for Alice.

    The operator allows shell on this machine and approves by default; only the functions in *require_approval* ask.
    *process_env* is the primary's environment, which says for example where its sandbox runner listens.
    """
    defaults = DefaultsConfig(tools=[], allow_shell_without_worker=True)
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=MappingProxyType(dict(process_env)),
    )
    config = bind_runtime_paths(
        Config(
            agents={"watcher": AgentConfig(display_name="Watcher", tools=["counter", "shell", *extra_tools])},
            defaults=defaults,
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "default": "auto_approve",
                "rules": [{"match": name, "action": "require_approval"} for name in require_approval],
            },
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="watcher",
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.test",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        storage_path=runtime_paths.storage_root,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


class WorkflowStack:
    """The engine harness: real stores, broker and engine around a fake script manager (see the module docstring)."""

    def __init__(self, tmp_path: Path, counting_tool: CountingTool, **owner_options: Any) -> None:  # noqa: ANN401
        self.tmp_path = tmp_path
        self.counting_tool = counting_tool
        self.owner_options = owner_options
        self.run_id = ""
        self.tokens: dict[str, str] = {}
        self.admission: JobGrant = "allowed"
        self._open()

    def _open(self) -> None:
        """Build the runtimes on the stack's databases, as a starting primary does."""
        self.context = owner_context(self.tmp_path, **self.owner_options)
        self.script_store = ScriptRunStore(self.context.runtime_paths)
        self.store = WorkflowRunStore(self.tmp_path / "control" / "workflow_runs" / "workflow_runs.sqlite3")
        self.resolver = _StackResolver(self.context)
        self.broker = ScriptToolBroker(store=self.script_store, runtime_resolver=self.resolver)
        self.broker.open_call_admission()
        self.scripts = FakeScripts(self.script_store, self.broker)
        self.scripts.tokens = self.tokens
        self.engine = WorkflowEngine(self.store, self.scripts, self.broker)  # type: ignore[arg-type]
        # What a turn of the owner sees: the context with the orchestrator that holds the engine.
        self.context = replace(self.context, orchestrator=SimpleNamespace(workflow_engine=self.engine, running=True))  # type: ignore[arg-type]

    @property
    def attempt(self) -> ScriptRunRecord:
        """The live record of the run's current attempt."""
        script_run_id = self.store.get_run(self.run_id).script_run_id
        assert script_run_id is not None
        return self.script_store.get_run(script_run_id)

    @property
    def token(self) -> str:
        """The capability token of the run's current attempt."""
        return self.tokens[self.attempt.run_id]

    async def start(self, source: str = "", args: object = None, **options: object) -> WorkflowRun:
        """Reserve a run for the owner and launch its first attempt."""
        context = self.context
        run = WorkflowRun(
            run_id=new_workflow_run_id(),
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            execution_identity=serialize_tool_execution_identity(context.execution_identity),
            persona=None,
            title=None,
            source=source,
            source_digest=hashlib.sha256(source.encode()).hexdigest(),
            args=args,
            max_concurrent=16,
            max_cost_usd=None,
            resource_profile=None,
            runtime_seconds_left=None,
            origin_job_id=None,
        )
        self.store.reserve_run(replace(run, **options))
        self.run_id = run.run_id
        return await self.engine.launch(run.run_id, context)

    async def submit(
        self,
        key: str,
        kind: str,
        args: dict[str, object],
        meta: dict[str, object] | None = None,
    ) -> NodeReceipt:
        """Submit one node as the attempt's script would."""
        return await self.engine.submit_node(self.attempt, NodeRequest(key, kind, args, meta or {}))

    async def result(
        self,
        key: str,
        kind: str,
        args: dict[str, object],
        meta: dict[str, object] | None = None,
    ) -> object:
        """Submit one node, wait until it settles, and return its result or raise `NodeFailed`."""
        receipt = await self.submit(key, kind, args, meta)
        while receipt.state not in _SETTLED_STATES:
            receipt = await self.engine.node_receipt(self.attempt, receipt.id, wait=SETTLE_TIMEOUT_SECONDS)
        if receipt.state != "completed":
            raise NodeFailed(receipt)
        return receipt.result

    async def end_attempt(self, state: ScriptRunState, reason: str | None = None, *, output: str = "") -> None:
        """End the attempt's process in *state* and let the engine see it."""
        await self.scripts.end(self.attempt.run_id, state, reason=reason or PROCESS_EXIT_OBSERVED, output=output)
        await self.engine.reconcile()

    async def open_jobs(self) -> ProcessRuntime:
        """Start the real job runtime on the stack's journal as the instance of the owner's storage root."""
        paths = self.context.runtime_paths
        self.jobs = await tool_job_runtime(paths.storage_root, grant=lambda _job: self.admission)
        self._instance = pin_background_tool_jobs(self.context.config, paths)
        register_background_runtime(paths, self.jobs)
        return self.jobs

    async def close(self) -> None:
        """Stop the job runtime and the engine, and withdraw the instance."""
        await self.jobs.shutdown()
        release_background_tool_jobs(self.context.runtime_paths, self._instance)
        await self.engine.close()

    def refuse_admission(self) -> None:
        """Make the job runtime's grant check refuse every job from now on."""
        self.admission = "denied"

    async def call(
        self,
        name: str,
        *,
        context: ToolRuntimeContext | None = None,
        depth: int = 0,
        call_id: str | None = None,
        **arguments: object,
    ) -> str:
        """Make one model call of the `workflow` toolkit through job admission, as a turn in *context* does.

        A call that fails raises `CallFailed` with the text the model would read.
        """
        context = context or self.context
        toolkit = WorkflowTools(context.config)
        bind_toolkit_construction(toolkit, ToolConstruction("workflow", None))
        tools = toolkit_job_tools(toolkit, authored_name="workflow").values()
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(
                    job_actor(*tools, name=context.agent_name, depth=depth),
                    name,
                    arguments,
                    call_id=call_id or uuid4().hex,
                    session_id=context.execution_identity.session_id or "session",
                )
        if outcome.is_error:
            raise CallFailed(outcome.content)
        return outcome.content

    async def finish_attempt(self, result: object = None, error: str | None = None) -> None:
        """Let the attempt's script report and exit, as `main` returning or raising does."""
        await self.engine.finish(self.attempt, result=result, error=error)
        await self.end_attempt(ScriptRunState.EXITED if error is None else ScriptRunState.FAILED)

    async def collect(self, job: BackgroundJob) -> object:
        """Return the whole result an ended job delivers to the conversation that started it."""
        return (await read_result_payload(self.jobs, job)).value

    async def restart(self) -> WorkflowStack:
        """Close the runtimes and reopen them on the same databases, as a primary restart does."""
        await self.engine.close()
        self._open()
        return self

    async def until(self, predicate: Callable[[], object]) -> None:
        """Wait until *predicate* holds."""
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while not predicate():  # noqa: ASYNC110 - the predicate reads state no event announces.
                await asyncio.sleep(0.01)

    def app(self) -> FastAPI:
        """Return an app that serves the workflow routes from the stack's engine."""
        app = FastAPI()
        app.include_router(workflow_router)
        bind_workflow_engine(app, self.engine)
        return app

    def http(self, token: str | None = None) -> httpx.AsyncClient:
        """Return a client of the current attempt's workflow routes, as the SDK in that attempt's process is."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app()),
            base_url=f"http://gateway.test/api/script-gateway/workflow/{self.attempt.run_id}/",
            headers={"Authorization": f"Bearer {self.token if token is None else token}"},
        )


@contextmanager
def workflow_stack(
    tmp_path: Path,
    stack_type: type[WorkflowStack] = WorkflowStack,
    **owner_options: Any,  # noqa: ANN401 - the options of `owner_context`.
) -> Iterator[WorkflowStack]:
    """Build the engine harness with its `counter` toolkit registered for the block."""
    counting_tool = CountingTool(tmp_path / "counter.txt")
    with registered_toolkit("counter", counting_tool.toolkit()):
        yield stack_type(tmp_path, counting_tool, **owner_options)
