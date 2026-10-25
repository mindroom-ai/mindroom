"""Native subagent results obey the same output-file policy as direct tools."""

from __future__ import annotations

import asyncio
import json
from ast import literal_eval
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.delegation.state import DelegationState
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.delegation_helpers import (
    ScriptedAgents,
    _runtime_paths,
    _saved_approval_calls,
    approval_executor,
    respond,
    resume,
    stored_run,
)
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import JOB_TEST_TIMEOUT, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "execution"),
    [
        (mode, execution)
        for mode in ("explicit", "automatic", "invalid", "resumed", "resumed_invalid")
        for execution in ("inline", "foreground", "detached", "excluded")
        # A managed child asks for its own approvals through job cards, so only unmanaged children resume a parent.
        if not (mode.startswith("resumed") and execution in {"foreground", "detached"})
    ],
)
async def test_native_delegation_obeys_output_file_policy(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    execution: str,
) -> None:
    """Persisted child approvals must not bypass path validation, redirection, or automatic saving."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(
            enabled=execution != "inline",
            exclude_toolkits=["shell", "delegate"] if execution == "excluded" else ["shell"],
        ),
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"], private=AgentPrivateConfig(per="user")),
            "child": AgentConfig(display_name="Child", tools=["calculator"]),
        },
        defaults=DefaultsConfig(
            tools=[],
            tool_output_auto_save_threshold_bytes=16 if mode == "automatic" else 100_000,
        ),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]} if mode.startswith("resumed") else {},
    )
    entity_ids(config, paths)
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    runtime = await tool_job_runtime(tmp_path) if execution != "inline" else None
    pin_background_tool_jobs(config, paths)
    if runtime is not None:
        register_background_runtime(paths, runtime)
    release = asyncio.Event()
    signal = asyncio.Event()
    if execution != "detached":
        release.set()

    async def start_child() -> None:
        # A human message while an excluded child runs must not move its wait into a job.
        if execution == "excluded":
            signal.set()
        await release.wait()

    child = ScriptedModel([[Await(start_child), *calls(tool_call("add", "sum", a=1, b=2))], answer("Child report")])
    output_args: dict[str, object] = (
        {} if mode == "automatic" else {"mindroom_output_path": "../escape.txt" if mode == "invalid" else "report.txt"}
    )
    if execution == "detached":
        output_args["background_after"] = 0
    parent = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name="child", task="Write report", **output_args)),
            answer("Parent done"),
        ],
    )
    ScriptedAgents({"child": child}, by_session={"parent": parent}).install(monkeypatch)
    workspace = resolve_agent_runtime("leader", config, paths, identity).tool_base_dir
    assert workspace is not None
    options = {"config": config, "runtime_paths": paths, "identity": identity}
    result_tool_name = "run_subagent"
    run_id = "parent-run"
    relocated_output = None
    try:
        with pending_human_messages_context(signal):
            reply = await respond("leader", "Delegate", **options, run_id=run_id)
            if execution == "detached" and mode != "invalid":
                assert runtime is not None
                first = next(
                    tool
                    for tool in stored_run("leader", run_id, **options).tools or []
                    if tool.tool_name == "run_subagent"
                )
                handle = json.loads(str(first.result))
                assert "job_id" in handle, "A released wait returns its handle without redirecting it to a file"
                assert not (workspace / "report.txt").exists()
                release.set()
                waited = await asyncio.wait_for(
                    runtime.wait(handle["job_id"], owner=identity, depth=0),
                    JOB_TEST_TIMEOUT,
                )
                await runtime.release_wait(handle["job_id"], waited.claim)
                if mode == "explicit":
                    relocated_output = workspace / "completed_report.txt"
                    (workspace / "report.txt").rename(relocated_output)
                    (workspace / "report.txt").mkdir()
                parent.turns = [
                    calls(tool_call("job", "retrieve", action="wait", job_id=handle["job_id"])),
                    answer("Parent done"),
                ]
                run_id = "retrieve-run"
                reply = await respond("leader", "Retrieve", **options, run_id=run_id)
                result_tool_name = "job"
            if mode.startswith("resumed"):
                assert isinstance(reply, PausedAttempt)
                assert stored_run("leader", run_id, **options).status is RunStatus.paused
                assert not (workspace / "report.txt").exists()
                state = DelegationState.from_metadata(stored_run("leader", run_id, **options).metadata)
                if mode == "resumed_invalid":
                    (workspace / "report.txt").symlink_to(tmp_path / "escape.txt")
                if execution == "excluded" and mode == "resumed":
                    config.background_tool_jobs.exclude_toolkits.remove("delegate")
                approvals = _saved_approval_calls(state)
                resumed = await resume(
                    approval_executor(config, paths),
                    reply,
                    approvals,
                    agent_name="leader",
                    **options,
                    decisions={call.tool_call_id: True for call in approvals},
                )
                assert isinstance(resumed, CompletedApprovalRun)
        result = stored_run("leader", run_id, **options)
        assert result.status is RunStatus.completed
        if execution == "excluded":
            assert runtime is not None
            assert await runtime.list_jobs(owner=identity, depth=0) == []
        delegation = next(tool for tool in result.tools or [] if tool.tool_name == result_tool_name)
        recorded = str(delegation.result)
        if result_tool_name == "job" and mode == "automatic":
            # The job call's own output policy saves the retrieved receipt again, and its receipt reaches the record
            # as the dict's text, as Agno recorded every tool's dict result.
            saved_again = literal_eval(recorded)["mindroom_tool_output"]
            assert saved_again["auto_saved"] is True
            assert saved_again["path"].startswith("mindroom_tool_outputs/job-")
            recorded = (workspace / saved_again["path"]).read_text()
        receipt = json.loads(recorded)["mindroom_tool_output"]
        if mode in {"invalid", "resumed_invalid"}:
            assert receipt["status"] == "error"
            assert not (tmp_path / "escape.txt").exists()
            # An invalid path never starts the child; a resumed one is refused before the child continues.
            assert len(child.requests) == (0 if mode == "invalid" else 1)
        else:
            assert receipt["status"] == "saved_to_file"
            assert "Child report" in (relocated_output or workspace / receipt["path"]).read_text()
            assert receipt.get("auto_saved", False) == (mode == "automatic")
    finally:
        release.set()
        if runtime is not None:
            await runtime.shutdown()
