"""Tests for the workflow routes of the script gateway: authentication, sizes, polling, both listeners.

Level: engine harness (`WorkflowStack`) behind the real routes; the test is the attempt's script.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import closing
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import mindroom.api.main as api_main
from mindroom.api.script_gateway import serve_script_gateway_listener
from mindroom.api.workflow_gateway import router as workflow_router
from mindroom.constants import RuntimePaths
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs.models import WorkflowRunState
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.workflow_helpers import WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_MEBIBYTE = 1024 * 1024
_NODE = {"key": "@0", "kind": "tool", "args": tool_args("counter", "append", line="a"), "meta": {"label": "First"}}


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The engine harness with one started run."""
    with workflow_stack(tmp_path) as stack:
        await stack.start(args={"topic": "tides"})
        yield stack
        await stack.engine.close()


@pytest_asyncio.fixture
async def client(stack: WorkflowStack) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the attempt's routes that holds the attempt's capability, as its script does."""
    async with stack.http() as client:
        yield client


@pytest.mark.asyncio
async def test_script_reads_its_run_submits_a_node_logs_and_finishes(
    stack: WorkflowStack,
    client: httpx.AsyncClient,
) -> None:
    """The routes carry one whole attempt: its arguments, a node and its receipt, a log line, the report."""
    run = await client.get("run")
    submitted = await client.post("nodes", json=_NODE)
    settled = await client.get(f"nodes/{submitted.json()['id']}", params={"wait": 5})
    result = await client.get(f"results/{submitted.json()['id']}")
    logged = await client.post("log", json={"text": "hi"})
    finished = await client.post("finish", json={"result": {"lines": 1}})
    await stack.end_attempt(ScriptRunState.EXITED)

    assert run.json() == {"run_id": stack.run_id, "attempt": 1, "args": {"topic": "tides"}}
    assert (submitted.status_code, submitted.json()["state"]) == (200, "running")
    assert settled.json() == {
        "id": "n1",
        "state": "completed",
        "cached": False,
        "result": "appended",
        "error": None,
        "result_ref": None,
    }
    assert result.json() == "appended"
    assert (logged.status_code, finished.status_code) == (200, 200)
    assert stack.store.log(stack.run_id) == ["hi"]
    done = stack.store.get_run(stack.run_id)
    assert (done.state, done.result) == (WorkflowRunState.COMPLETED, {"lines": 1})
    saved = stack.store.get_node(stack.run_id, "@0")
    assert saved is not None
    assert saved.label == "First"


@pytest.mark.asyncio
async def test_node_request_over_one_mebibyte_is_413_with_the_byte_count(
    stack: WorkflowStack,
    client: httpx.AsyncClient,
) -> None:
    """The limit is measured on the whole body, and the answer names its size so the SDK can say it."""
    body = json.dumps({**_NODE, "args": tool_args("counter", "append", line="x" * _MEBIBYTE)}).encode()

    refused = await client.post("nodes", content=body, headers={"Content-Type": "application/json"})

    assert (refused.status_code, refused.json()) == (413, {"bytes": len(body), "limit": _MEBIBYTE})
    assert stack.engine.status(stack.run_id)[1] == []


@pytest.mark.asyncio
async def test_finish_accepts_a_report_over_one_mebibyte_and_refuses_one_over_eight(
    stack: WorkflowStack,
    client: httpx.AsyncClient,
) -> None:
    """The report has its own limit of 8 MiB."""
    too_large = json.dumps({"result": "x" * (8 * _MEBIBYTE)}).encode()
    refused = await client.post("finish", content=too_large, headers={"Content-Type": "application/json"})
    accepted = await client.post("finish", json={"result": "x" * (2 * _MEBIBYTE)})

    assert (refused.status_code, refused.json()) == (413, {"bytes": len(too_large), "limit": 8 * _MEBIBYTE})
    assert accepted.status_code == 200
    assert stack.store.get_run(stack.run_id).result == "x" * (2 * _MEBIBYTE)


@pytest.mark.asyncio
async def test_unknown_token_is_404(stack: WorkflowStack) -> None:
    """A wrong capability, a missing one and an unknown attempt all answer like a run that does not exist."""
    async with stack.http(token="invented-capability") as wrong:  # noqa: S106 - an invented placeholder.
        assert (await wrong.get("run")).status_code == 404
        assert (await wrong.post("nodes", json=_NODE)).status_code == 404
        assert (await wrong.get("run", headers={"Authorization": ""})).status_code == 404
    async with stack.http() as client:
        unknown = await client.get(f"/api/script-gateway/workflow/script-{'0' * 32}/run")
        assert unknown.status_code == 404
        assert (await client.get("nodes/n99")).status_code == 404
        assert (await client.get("nodes/not-a-node")).status_code == 404

    assert stack.engine.status(stack.run_id)[1] == []


@pytest.mark.asyncio
async def test_superseded_attempt_is_409(stack: WorkflowStack, client: httpx.AsyncClient) -> None:
    """An attempt the run no longer names is told so on every route, and the SDK exits on that answer."""
    stack.store.update_run(stack.run_id, script_run_id="script-" + "0" * 32)

    answers = [
        await client.get("run"),
        await client.post("nodes", json=_NODE),
        await client.get("nodes/n1"),
        await client.get("results/n1"),
        await client.post("log", json={"text": "late"}),
        await client.post("finish", json={"result": "late"}),
    ]

    assert [answer.status_code for answer in answers] == [409] * 6


@pytest.mark.asyncio
async def test_malformed_requests_are_422_and_save_nothing(stack: WorkflowStack, client: httpx.AsyncClient) -> None:
    """A key that is not `lane@index`, an unknown field, a number JSON cannot carry and a later node kind are refused."""
    bodies = [
        json.dumps({**_NODE, "key": "first"}),
        json.dumps({**_NODE, "priority": 1}),
        json.dumps({**_NODE, "args": tool_args("counter", "append", line="a")})[:-2] + ', "n": NaN}}',
        json.dumps({**_NODE, "kind": "agent"}),
        json.dumps({**_NODE, "args": {"toolkit": "counter"}}),
    ]

    answers = [await client.post("nodes", content=body) for body in bodies]

    assert [answer.status_code for answer in answers] == [422] * 5
    assert "agent" in answers[3].json()["detail"]
    assert stack.engine.status(stack.run_id)[1] == []


@pytest.mark.asyncio
async def test_poll_returns_when_the_node_changes_and_after_wait_otherwise(
    stack: WorkflowStack,
    client: httpx.AsyncClient,
) -> None:
    """A poll is held until the node settles, and comes back unchanged when its wait runs out first."""
    stack.resolver.hold_approvals = asyncio.Event()
    node_id = (await client.post("nodes", json=_NODE)).json()["id"]
    loop = asyncio.get_running_loop()

    started = loop.time()
    unchanged = await client.get(f"nodes/{node_id}", params={"wait": 0.2})
    waited = loop.time() - started
    held = asyncio.create_task(client.get(f"nodes/{node_id}", params={"wait": SETTLE_TIMEOUT_SECONDS}))
    await asyncio.sleep(0.05)
    assert not held.done()
    stack.resolver.hold_approvals.set()
    changed = await held

    assert (unchanged.json()["state"], waited >= 0.2) == ("running", True)
    assert (changed.json()["state"], changed.json()["result"]) == ("completed", "appended")
    assert loop.time() - started < SETTLE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_routes_without_a_bound_engine_are_503() -> None:
    """Before the runtime lends its engine, and after it withdrew it, the routes fail closed."""
    app = FastAPI()
    app.include_router(workflow_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        answer = await client.get(f"/api/script-gateway/workflow/script-{'a' * 32}/run")

    assert answer.status_code == 503


@pytest.mark.asyncio
async def test_routes_are_served_by_the_gateway_only_listener(stack: WorkflowStack, tmp_path: Path) -> None:
    """A Kubernetes worker reaches only the gateway listener, so the workflow routes are mounted there by name."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env={"MINDROOM_SCRIPT_GATEWAY_PORT": str(port)},
    )
    route = f"/api/script-gateway/workflow/{stack.attempt.run_id}/run"

    async with (
        serve_script_gateway_listener(
            runtime_paths,
            host="127.0.0.1",
            broker=stack.broker,
            log_level="INFO",
            agent_cli_registry=None,
            workflow_engine=stack.engine,
        ),
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        answer = await client.get(route, headers={"Authorization": f"Bearer {stack.token}"})

    assert (answer.status_code, answer.json()["run_id"]) == (200, stack.run_id)
    assert any(getattr(route, "path", "").endswith("/workflow/{script_run_id}/run") for route in api_main.app.routes)
