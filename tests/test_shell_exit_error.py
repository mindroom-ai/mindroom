"""End-to-end contract tests for failed shell commands returning ToolOutcome(is_error=True)."""

from __future__ import annotations

import json
import sys
import time
from ast import literal_eval
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.hooks import (
    EVENT_TOOL_RESULT_TRANSFORM,
    HookBindings,
    HookRegistry,
    ToolResultTransformContext,
    hook,
)
from mindroom.llm.media import Image
from mindroom.message_target import MessageTarget
from mindroom.redaction import REDACTED
from mindroom.secret_requests import secret_holder, temporary_secrets
from mindroom.shell.background_scope import shell_background_scope
from mindroom.shell.execution import ShellRunResult
from mindroom.shell.supervisor import _parse_supervisor_response
from mindroom.tool_system.agno_toolkits import adapt_toolkit, configure_toolkit_functions
from mindroom.tool_system.invocation import (
    CallResult,
    ToolCallContext,
    client_tool_result,
    execute_tool_call,
    run_tool_call,
)
from mindroom.tool_system.media_transport import encode_media_result
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.sandbox_proxy import _format_named_worker_result
from mindroom.tool_system.tool_function import ToolFunction, ToolOutcome, ToolValue
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from mindroom.tool_system.worker_proxy_client import WorkerProxyClientConfig, execute_worker_proxy_request
from mindroom.workers.named import WorkerSandboxConfig
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest, ExecuteResponse
from tests.api.test_sandbox_runner_api import (
    SANDBOX_HEADERS,
    SANDBOX_TOKEN,
    _refresh_runner_app_from_env,
    _set_sandbox_token,
)
from tests.test_worker_execute_stream import _client

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolMiddleware


def _shell_function(
    tmp_path: Path,
    *,
    allow_without_worker: bool = True,
    auto_save_threshold_bytes: int = 1_000_000,
    process_env: dict[str, str] | None = None,
) -> tuple[ToolFunction, Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env or {},
    )
    config = Config(
        defaults=DefaultsConfig(allow_shell_without_worker=allow_without_worker),
        agents={"code": AgentConfig(display_name="Code", tools=["shell"])},
    )
    toolkit = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        runtime_config=config,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tool_output_workspace_root=tmp_path,
        tool_output_auto_save_threshold_bytes=auto_save_threshold_bytes,
        tools=builtin_tool_registry(),
    )
    configure_toolkit_functions(
        toolkit,
        output_files=ToolOutputFilePolicy(
            workspace_root=tmp_path,
            auto_save_threshold_bytes=auto_save_threshold_bytes,
        ),
    )
    adapted = adapt_toolkit(toolkit, key="shell")
    fn = next(item for item in adapted.functions if item.name == "run_shell_command")
    return fn, config, runtime_paths


def _call_context(call_id: str = "call-1") -> ToolCallContext:
    return ToolCallContext(run_id="run-1", session_id="session-1", user_id="@alice:example.org", call_id=call_id)


def _runtime_context(
    tmp_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    hooks: HookBindings,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id="$msg"),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path / "storage",
        plugins=(),
        conversation_reader=MagicMock(),
        relations=MagicMock(),
        hooks=hooks,
    )


def _registry(*callbacks: object) -> HookRegistry:
    plugin = SimpleNamespace(
        name="guard",
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/guard", settings={}),
        plugin_order=0,
    )
    return HookRegistry.from_plugins([plugin])


def test_shell_run_result_failed_and_supervisor_timed_out_parsing() -> None:
    """ShellRunResult.failed covers non-zero exits, timeouts, and execution errors; supervisor validates timed_out."""
    assert not ShellRunResult(message="ok", return_code=0).failed
    assert not ShellRunResult(message="bg", handle="shell:1234", return_code=None).failed
    assert ShellRunResult(message="Exit code 1", return_code=1).failed
    assert ShellRunResult(message="Exit code -9", return_code=-9).failed
    assert ShellRunResult(message="Stopped after 1s.", timed_out=True).failed
    assert ShellRunResult(message="Error: boom", execution_error=True).failed

    legacy = _parse_supervisor_response(
        json.dumps({"message": "ok", "handle": None, "output_file_handled": False}).encode(),
    )
    assert legacy == ShellRunResult(message="ok", timed_out=False)
    timed = _parse_supervisor_response(
        json.dumps(
            {
                "message": "Stopped after 1s.",
                "handle": None,
                "output_file_handled": False,
                "return_code": -15,
                "timed_out": True,
            },
        ).encode(),
    )
    assert timed.timed_out is True
    assert timed.failed is True
    invalid = _parse_supervisor_response(
        json.dumps({"message": "ok", "handle": None, "output_file_handled": False, "timed_out": "yes"}).encode(),
    )
    assert invalid.execution_error is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_shell_refusal")
async def test_execute_tool_call_shell_error_and_success_boundaries(tmp_path: Path) -> None:
    """Failed exits, timeouts, refusals, and bad args return is_error=True; exit 0 (with stderr) and auto-bg stay False."""
    fn, _config, _paths = _shell_function(tmp_path, process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"})

    nonzero = await execute_tool_call(fn, {"args": "echo bad >&2; exit 1"}, _call_context("c1"), middleware=())
    assert nonzero.is_error is True
    assert nonzero.content == "Exit code 1\nStderr:\nbad"

    launch_127 = await execute_tool_call(
        fn,
        {"args": ["/definitely-missing-shell-binary-xyz"], "worker": "build"},
        _call_context("c2"),
        middleware=(),
    )
    assert launch_127.is_error is True
    assert launch_127.content.startswith("Exit code 127\nStderr:\n")

    killed = await execute_tool_call(fn, {"args": "kill -KILL $$"}, _call_context("c3"), middleware=())
    assert killed.is_error is True
    assert killed.content.startswith("Exit code -9")

    timed_out = await execute_tool_call(
        fn,
        {"args": "echo hi; sleep 30", "timeout": 1},
        _call_context("c4"),
        middleware=(),
    )
    assert timed_out.is_error is True
    assert timed_out.content == "Stopped after 1s.\nOutput:\nhi"

    bad_timeout = await execute_tool_call(fn, {"args": "echo hi", "timeout": 0}, _call_context("c5"), middleware=())
    assert bad_timeout.is_error is True
    assert "timeout must be a positive number" in bad_timeout.content

    bad_args = await execute_tool_call(fn, {"args": "   "}, _call_context("c6"), middleware=())
    assert bad_args.is_error is True
    assert "'args' must be" in bad_args.content

    refused_fn, _, _ = _shell_function(tmp_path, allow_without_worker=False)
    refused = await execute_tool_call(refused_fn, {"args": "echo hi"}, _call_context("c7"), middleware=())
    assert refused.is_error is True
    assert "Shell commands run only in a worker" in refused.content

    ok_stderr = await execute_tool_call(fn, {"args": "echo ok; echo warn >&2"}, _call_context("c8"), middleware=())
    assert ok_stderr.is_error is False
    assert ok_stderr.content == "ok\nStderr:\nwarn"

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope() as scope:
            bg = await execute_tool_call(fn, {"args": "echo bg; sleep 30"}, _call_context("c9"), middleware=())
            assert bg.is_error is False
            assert scope.had_backgrounded is True
            assert "moved to the background" in bg.content


@pytest.mark.asyncio
async def test_failed_shell_output_redirect_and_auto_save_preserve_is_error_and_saved_output(tmp_path: Path) -> None:
    """Explicit redirect and large auto-save both write the file cleanly while keeping is_error=True and saved_output."""
    fn, _config, _paths = _shell_function(tmp_path, auto_save_threshold_bytes=64)

    redirected = await execute_tool_call(
        fn,
        {"args": "echo partial; echo boom >&2; exit 4", "mindroom_output_path": "failed.txt"},
        _call_context("redirect"),
        middleware=(),
    )
    assert redirected.is_error is True
    receipt = json.loads(redirected.content)["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert receipt["exit_code"] == 4
    assert (tmp_path / "failed.txt").read_text() == "Exit code 4\nOutput:\npartial\n\nStderr:\nboom\n"

    large_cmd = [sys.executable, "-c", "import sys; print('x' * 200); sys.exit(2)"]
    auto_saved = await execute_tool_call(fn, {"args": large_cmd}, _call_context("autosave"), middleware=())
    assert auto_saved.is_error is True
    assert auto_saved.saved_output is not None
    saved_receipt = auto_saved.saved_output["mindroom_tool_output"]
    assert isinstance(saved_receipt, dict)
    assert saved_receipt["status"] == "saved_to_file"
    assert saved_receipt["auto_saved"] is True
    assert literal_eval(auto_saved.content) == auto_saved.saved_output
    saved_path = tmp_path / str(saved_receipt["path"])
    assert saved_path.read_text().startswith("Exit code 2\nOutput:\n" + "x" * 200)
    assert client_tool_result(auto_saved) == auto_saved.saved_output


@pytest.mark.asyncio
async def test_result_transform_and_secret_masking_cover_failed_shell_output(tmp_path: Path) -> None:
    """Failed shell output runs through secret masking (including saved_output) and tool:result_transform."""
    fn, config, runtime_paths = _shell_function(tmp_path, auto_save_threshold_bytes=64)
    secret_value = "tok_super_secret_failed_shell_999"  # noqa: S105

    def run_with_hooks(registry: HookRegistry) -> tuple[ToolRuntimeContext, ToolMiddleware]:
        bindings = HookBindings(registry=registry)
        ctx = _runtime_context(tmp_path, config, runtime_paths, hooks=bindings)
        holder = secret_holder(ctx.execution_identity)
        assert holder is not None
        temporary_secrets().give(
            holder,
            "API_TOKEN",
            secret_value,
            keep="memory",
            now=time.time(),
            storage_root=runtime_paths.storage_root,
        )
        middleware = build_tool_hook_middleware(bindings, config, runtime_paths)
        return ctx, middleware

    seen_pass: list[str] = []

    @hook(EVENT_TOOL_RESULT_TRANSFORM)
    async def observe(ctx: ToolResultTransformContext) -> None:
        seen_pass.append(ctx.result)

    ctx, middleware = run_with_hooks(_registry(observe))
    cmd = [sys.executable, "-c", f"import sys; print('leak={secret_value} ' + 'a' * 120); sys.exit(5)"]
    with tool_runtime_context(ctx):
        passed = await execute_tool_call(fn, {"args": cmd}, _call_context("pass"), middleware=(middleware,))
    assert passed.is_error is True
    assert passed.saved_output is not None
    assert secret_value not in passed.content
    assert secret_value not in json.dumps(passed.saved_output)
    assert REDACTED in passed.content
    assert REDACTED in json.dumps(passed.saved_output)
    assert seen_pass
    assert secret_value not in seen_pass[0]

    @hook(EVENT_TOOL_RESULT_TRANSFORM)
    async def rewrite(_ctx: ToolResultTransformContext) -> str:
        return "sanitized failure output"

    ctx_rw, middleware_rw = run_with_hooks(_registry(rewrite))
    with tool_runtime_context(ctx_rw):
        rewritten = await execute_tool_call(fn, {"args": cmd}, _call_context("rw"), middleware=(middleware_rw,))
    assert rewritten.is_error is True
    assert rewritten.content == "sanitized failure output"
    assert rewritten.saved_output is None

    @hook(EVENT_TOOL_RESULT_TRANSFORM)
    async def withhold(_ctx: ToolResultTransformContext) -> None:
        msg = "prompt injection in failed shell output"
        raise RuntimeError(msg)

    ctx_wh, middleware_wh = run_with_hooks(_registry(withhold))
    with tool_runtime_context(ctx_wh):
        withheld = await run_tool_call(
            fn,
            {"args": "echo evil; exit 1"},
            _call_context("wh"),
            middleware=(middleware_wh,),
        )
    assert withheld.status == "failed"
    assert isinstance(withheld.value, ToolOutcome)
    assert withheld.value.is_error is True
    assert withheld.value.refused_by == "result_transform"
    assert isinstance(withheld, CallResult)


def test_worker_proxy_client_and_named_worker_formatting_preserve_is_error() -> None:
    """ExecuteResponse(is_error=True) reconstructs ToolOutcome across plain text, saved receipts, and media."""
    cfg = WorkerProxyClientConfig(
        proxy_url="http://worker",
        proxy_token="tok",  # noqa: S106
        proxy_timeout_seconds=10.0,
        credential_lease_ttl_seconds=60,
        credential_policy={},
        lease_tool_credentials=False,
    )
    req = ExecuteRequest(
        protocol=WORKER_PROTOCOL_VERSION,
        tool_name="shell",
        function_name="run_shell_command",
        kwargs={"args": "exit 2"},
    )
    worker_manager = MagicMock()

    def roundtrip(answer: ExecuteResponse) -> object:
        line = answer.model_dump_json()
        return execute_worker_proxy_request(
            config=cfg,
            request=req,
            credentials_manager=None,
            worker_target=None,
            worker_handle=None,
            worker_manager=worker_manager,
            client_factory=_client(lambda _payload: iter([line]), []),
        )

    plain = roundtrip(ExecuteResponse(ok=True, result="Exit code 2\nStderr:\nboom", is_error=True))
    assert plain == ToolOutcome(content="Exit code 2\nStderr:\nboom", is_error=True)

    receipt: dict[str, object] = {
        "mindroom_tool_output": {
            "status": "saved_to_file",
            "path": "mindroom_tool_outputs/failed.txt",
            "bytes": 100,
            "format": "text",
            "auto_saved": True,
            "preview": "Exit code 2\nOutput:\nbig",
        },
    }
    saved = roundtrip(ExecuteResponse(ok=True, result=receipt, is_error=True))
    assert saved == ToolOutcome(content=str(receipt), is_error=True, saved_output=receipt)

    media_env = encode_media_result(
        ToolValue(content="Exit code 2", images=(Image(content=b"png", mime_type="image/png"),)),
    )
    media = roundtrip(ExecuteResponse(ok=True, result=media_env, is_error=True))
    assert isinstance(media, ToolOutcome)
    assert media.is_error is True
    assert media.content == "Exit code 2"
    assert [img.content for img in media.images] == [b"png"]

    formatted_plain = _format_named_worker_result(
        plain,
        header="[worker: build, started]",
        admitted_sandbox=None,
    )
    assert formatted_plain == ToolOutcome(content="[worker: build, started]\nExit code 2\nStderr:\nboom", is_error=True)

    formatted_saved = _format_named_worker_result(
        saved,
        header="[worker: sbx, started]",
        admitted_sandbox=WorkerSandboxConfig(mount_dir="scratch/job1", mount_read_only=False),
    )
    assert isinstance(formatted_saved, ToolOutcome)
    assert formatted_saved.is_error is True
    assert formatted_saved.saved_output is not None
    inner = formatted_saved.saved_output["mindroom_tool_output"]
    assert isinstance(inner, dict)
    assert inner["path"] == "scratch/job1/mindroom_tool_outputs/failed.txt"
    assert inner["preview"] == "[worker: sbx, started]\nExit code 2\nOutput:\nbig"
    assert formatted_saved.content == str(formatted_saved.saved_output)


def test_sandbox_runner_streamed_shell_command_sets_is_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Streamed worker shell execution sets ExecuteResponse.is_error according to exit status."""
    _set_sandbox_token(monkeypatch)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "inprocess")
    _refresh_runner_app_from_env()
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_TOKEN": SANDBOX_TOKEN, "MINDROOM_SANDBOX_RUNNER_MODE": "true"},
    )
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=Config(defaults=DefaultsConfig(allow_shell_without_worker=True)),
        runner_token=SANDBOX_TOKEN,
    )
    with TestClient(sandbox_runner_app) as client:
        failed = client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", "echo err >&2; exit 6"]],
                "stream_io": True,
            },
        )
        ok = client.post(
            "/api/sandbox-runner/execute",
            headers=SANDBOX_HEADERS,
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "tool_name": "shell",
                "function_name": "run_shell_command",
                "args": [["bash", "-c", "echo hi"]],
                "stream_io": True,
            },
        )
    failed_answer = ExecuteResponse.model_validate_json(failed.text.splitlines()[-1])
    ok_answer = ExecuteResponse.model_validate_json(ok.text.splitlines()[-1])
    assert failed_answer.ok is True
    assert failed_answer.is_error is True
    assert "Exit code 6" in str(failed_answer.result)
    assert ok_answer.ok is True
    assert ok_answer.is_error is False
