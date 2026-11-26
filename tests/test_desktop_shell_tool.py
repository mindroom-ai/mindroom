"""Tests for the unified run_desktop_shell tool lifecycle and managed-job integration."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools import desktop_shell as desktop_shell_mod
from mindroom.custom_tools.desktop import DesktopTools
from mindroom.desktop.client import DesktopRequestError
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.protocol import DesktopCommand, DesktopProtocolError, DesktopResponse, EncryptedDesktopMedia
from mindroom.desktop.shell import DesktopShell
from mindroom.desktop.shell_actions import execute_shell
from mindroom.matrix.olm_to_device import OlmToDeviceError
from mindroom.shell.execution import shell_job_wait_seconds
from mindroom.tool_jobs.control import JobControl, current_job_id, job_control_context
from mindroom.tool_jobs.execution import _default_wait, project_definitions
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.output_files import ToolOutputFilePolicy, with_output_files
from mindroom.tool_system.progress import structured_tool_result, tool_progress_reporter
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolSpec
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity
from mindroom.tools.desktop import desktop_tools
from tests.desktop_helpers import ALICE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


def _target() -> ResolvedWorkerTarget:
    return ResolvedWorkerTarget(
        worker_scope="user_agent",
        routing_agent_name="computer",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="computer",
            requester_id=ALICE,
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id="session-1",
        ),
        tenant_id=None,
        account_id=None,
        worker_key="worker-1",
    )


def _shell_reply(
    request_id: str = "r1",
    session_id: str = "s1",
    *,
    legacy: bool = False,
    **fields: object,
) -> DesktopResponse:
    base: dict[str, object] = {
        "state": "completed",
        "handle": None,
        "exit_code": 0,
        "output": "",
        "output_bytes": 0,
        "output_truncated": False,
        "output_attachment": None,
        "output_start": 0,
        "next_offset": 0,
    }
    if not legacy:
        base.update(
            {
                "stderr": "",
                "stderr_bytes": 0,
                "stderr_truncated": False,
                "stderr_attachment": None,
                "stderr_start": 0,
                "stderr_next_offset": 0,
            },
        )
    base.update(fields)
    if "output" in fields and "output_bytes" not in fields:
        out_len = len(str(fields["output"]).encode())
        base["output_bytes"] = out_len
    if "next_offset" not in fields:
        base["next_offset"] = base["output_bytes"]
    if not legacy:
        if "stderr" in fields and "stderr_bytes" not in fields:
            err_len = len(str(fields["stderr"]).encode())
            base["stderr_bytes"] = err_len
        if "stderr_next_offset" not in fields:
            base["stderr_next_offset"] = base["stderr_bytes"]
    return DesktopResponse(request_id=request_id, session_id=session_id, ok=True, result=base)


def _media(data: bytes, *, url: str = "mxc://example.org/out") -> EncryptedDesktopMedia:
    return EncryptedDesktopMedia(
        url=url,
        key="k",
        iv="i",
        sha256="h",
        mime_type="text/plain",
        size=len(data),
    )


def _setup_tool(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[Any, DesktopCommand, float], Any],
    *,
    timeout_seconds: float = 30.0,
    runtime_paths: object | None = None,
) -> tuple[DesktopTools, list[tuple[DesktopCommand, float]]]:
    recorded: list[tuple[DesktopCommand, float]] = []

    async def fake_request(target: object, command: DesktopCommand, *, timeout_seconds: float) -> DesktopResponse:
        recorded.append((command, timeout_seconds))
        result = handler(target, command, timeout_seconds)
        if asyncio.iscoroutine(result):
            return await result
        return result

    router = SimpleNamespace(request=fake_request)
    context = SimpleNamespace(
        requester_id=ALICE,
        agent_name="computer",
        client=object(),
        runtime_paths=runtime_paths or SimpleNamespace(),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.desktop_shell.desktop_response_router", lambda _client: router)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DESKTOP",
            "device_ed25519": "fingerprint",
        },
    )
    tool = DesktopTools(
        timeout_seconds=timeout_seconds,
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_target(),
    )
    return tool, recorded


def _fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_shell_mod, "_POLL_BACKOFF_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(desktop_shell_mod, "_TIMEOUT_KILL_GRACE_DELAYS", (0.0, 0.01, 0.01))
    monkeypatch.setattr(desktop_shell_mod, "_BOUNDED_KILL_GRACE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(desktop_shell_mod, "_FORCE_KILL_DRAIN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(desktop_shell_mod, "_RECOVERY_DELAYS_SECONDS", (0.01, 0.01, 0.01, 0.01))
    monkeypatch.setattr(desktop_shell_mod, "_CANCEL_STATUS_RETRY_DELAY_SECONDS", 0.01)


def test_shell_job_wait_seconds_covers_both_shell_and_desktop_shell() -> None:
    """Both native shell and desktop shell default to a 120-second foreground wait inside managed jobs."""
    assert shell_job_wait_seconds("shell", "run_shell_command") == 120.0
    assert shell_job_wait_seconds("desktop", "run_desktop_shell") == 120.0
    assert shell_job_wait_seconds("desktop", "desktop") is None
    assert desktop_tools() is DesktopTools
    assert builtin_tool_registry().entries["desktop"].function_names == ("desktop", "run_desktop_shell")

    shell_fn = next(fn for fn in adapt_toolkit(DesktopTools()).functions if fn.name == "run_desktop_shell")
    registered_fn = replace(shell_fn, registered_toolkit="desktop")
    wait = _default_wait(registered_fn)
    assert wait == 120.0
    spec = ToolSpec(
        name=registered_fn.name,
        description=registered_fn.description or "",
        parameters=registered_fn.parameters,
        strict=False,
    )
    projected = project_definitions((spec,), {registered_fn.name: wait})[0]
    assert "background_after" in projected.parameters["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "kwargs", "expected_error"),
    [
        ("", {}, "'args' must be a shell command string"),
        ("   ", {}, "'args' must be a shell command string"),
        ([], {}, "'args' must be a shell command string"),
        ([""], {}, "'args' must be a shell command string"),
        (["   "], {}, "'args' must be a shell command string"),
        ('{"cmd": "ls"}', {}, "'args' must be a shell command string"),
        ("x" * 8193, {}, "8192"),
        ("ls", {"tail": -1}, "tail must be a non-negative integer"),
        ("ls", {"timeout": 0}, "timeout must be a positive number"),
        ("ls", {"timeout": -5}, "timeout must be a positive number"),
        ("ls", {"cwd": ""}, "cwd must be a non-empty string"),
        ("ls", {"cwd": "/" + "a" * 4096}, "4096"),
    ],
)
async def test_pre_start_validation_refuses_with_not_started_metadata(
    monkeypatch: pytest.MonkeyPatch,
    args: object,
    kwargs: dict[str, Any],
    expected_error: str,
) -> None:
    """Invalid arguments fail before sending anything and report not_started=True."""
    tool, recorded = _setup_tool(monkeypatch, lambda *_args: _shell_reply())
    with structured_tool_result() as metadata:
        result = await tool.run_desktop_shell(args, **kwargs)
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert expected_error in result
    assert metadata == {"not_started": True}
    assert recorded == []


@pytest.mark.asyncio
async def test_unconfigured_desktop_refuses_with_setup_hint_and_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpaired desktop setup returns an Error pointing to /desktop setup with not_started=True."""
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(requester_id=ALICE, agent_name="computer", client=object()),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.load_desktop_credentials", lambda *_a, **_kw: None)
    tool = DesktopTools(credentials_manager=MagicMock(spec=CredentialsManager), worker_target=_target())
    with structured_tool_result() as metadata:
        result = await tool.run_desktop_shell("ls")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "/desktop setup" in result
    assert metadata == {"not_started": True}


@pytest.mark.asyncio
async def test_fast_completion_argv_normalization_cwd_split_stderr_and_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arguments normalize like native shell, cwd forwards, and stdout/stderr format with native tail notices."""
    replies = iter(
        [
            _shell_reply(output="a\nb\nc\nd\n", stderr="w1\nw2\nw3\n", exit_code=0),
            _shell_reply(output="partial\n", stderr="boom\ndetail\n", exit_code=2),
            _shell_reply(state="killed", output="cleaned up\n", stderr="", exit_code=0),
            _shell_reply(output="ok\n", exit_code=0),
        ],
    )
    tool, recorded = _setup_tool(monkeypatch, lambda _t, _c, _to: next(replies))

    with structured_tool_result() as meta1:
        res1 = await tool.run_desktop_shell(["git", "commit", "-m", "hello world"], tail=2, cwd="/Users/me/repo")
    assert res1 == (
        "[Showing the last 2 of 4 lines. Pass a larger tail, or narrow the command.]\n"
        "[Showing the last 2 of 3 stderr lines. Pass a larger tail, or narrow the command.]\n"
        "c\nd\nStderr:\nw2\nw3"
    )
    assert meta1 == {"exit_code": 0}
    assert recorded[0][0].action == "run_shell"
    assert recorded[0][0].parameters == {
        "command": "git commit -m 'hello world'",
        "cwd": "/Users/me/repo",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 120,
    }
    assert recorded[0][1] == 120.0

    # Non-zero exit includes exit code first and preserves ALL stderr regardless of tail.
    with structured_tool_result() as meta2:
        res2 = await tool.run_desktop_shell("make test", tail=1, timeout=1)
    assert res2 == "Exit code 2\nOutput:\npartial\nStderr:\nboom\ndetail"
    assert meta2 == {"exit_code": 2}
    assert recorded[1][0].parameters == {
        "command": "make test",
        "timeout_seconds": 1,
        "separate_stderr": True,
        "max_runtime_seconds": 1.0,
    }

    # Exit 0 with state="killed" (killed locally on Mac) prefixes killed notice.
    res3 = await tool.run_desktop_shell(["echo hi && pwd"])
    assert res3 == "Command was killed on the desktop.\ncleaned up"
    assert recorded[2][0].parameters == {
        "command": "echo hi && pwd",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 120,
    }

    # JSON-encoded argv string is also normalized.
    res4 = await tool.run_desktop_shell('["ls", "-la"]')
    assert res4 == "ok"
    assert recorded[3][0].parameters == {
        "command": "ls -la",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 120,
    }


@pytest.mark.asyncio
async def test_slow_command_polling_tail_reset_progress_and_finished_page_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running tail slices reset offset to 0, progress reports live, and finished multi-page fallback drains cleanly."""
    _fast_timers(monkeypatch)
    replies = iter(
        [
            # Initial running reply skipped bytes [0, 5) so output_start > 0 -> must pull from offset 0 next.
            _shell_reply(
                state="running",
                handle="shell:1",
                exit_code=None,
                output="tail!",
                output_start=5,
                next_offset=10,
                output_bytes=10,
                stderr="e0",
                stderr_start=0,
                stderr_next_offset=2,
                stderr_bytes=2,
            ),
            # Poll 1 pulls stdout from 0 and stderr from 2.
            _shell_reply(
                state="running",
                handle="shell:1",
                exit_code=None,
                output="01234tail!",
                output_start=0,
                next_offset=10,
                output_bytes=10,
                stderr="",
                stderr_start=2,
                stderr_next_offset=2,
                stderr_bytes=2,
            ),
            # Poll 2: command finished, upload failed so bridge returns page 1 with handle still open.
            _shell_reply(
                state="completed",
                handle="shell:1",
                exit_code=0,
                output="page1-",
                output_start=10,
                next_offset=16,
                output_bytes=21,
                output_truncated=True,
                stderr="err-end",
                stderr_start=2,
                stderr_next_offset=9,
                stderr_bytes=9,
                warning="upload failed",
            ),
            # Poll 3: final page finishes draining with output_truncated=False.
            _shell_reply(
                state="completed",
                handle="shell:1",
                exit_code=0,
                output="page2",
                output_start=16,
                next_offset=21,
                output_bytes=21,
                output_truncated=False,
                stderr="",
                stderr_start=9,
                stderr_next_offset=9,
                stderr_bytes=9,
            ),
        ],
    )
    tool, recorded = _setup_tool(monkeypatch, lambda _t, _c, _to: next(replies))
    reports: list[str] = []
    monkeypatch.setattr("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0)
    with tool_progress_reporter(reports.append):
        result = await tool.run_desktop_shell("long-cmd")

    assert result == "01234tail!page1-page2\nStderr:\ne0err-end"
    assert [cmd.action for cmd, _ in recorded] == ["run_shell", "check_shell", "check_shell", "check_shell"]
    assert recorded[1][0].parameters == {"handle": "shell:1", "offset": 0, "stderr_offset": 2}
    assert recorded[2][0].parameters == {"handle": "shell:1", "offset": 10, "stderr_offset": 2}
    assert recorded[3][0].parameters == {"handle": "shell:1", "offset": 16, "stderr_offset": 9}
    assert any("01234tail!" in report for report in reports)


@pytest.mark.asyncio
async def test_legacy_combined_output_bridge_never_receives_stderr_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older helpers rejecting separate_stderr/max_runtime_seconds retry cleanly without caching stale legacy state."""
    _fast_timers(monkeypatch)
    upgraded = False

    def handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if (
            not upgraded
            and cmd.action == "run_shell"
            and ({"separate_stderr", "max_runtime_seconds"} & set(cmd.parameters))
        ):
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                False,
                error="Unexpected desktop parameters: max_runtime_seconds, separate_stderr.",
            )
        if cmd.action == "run_shell" and cmd.parameters["command"] == "legacy-cmd":
            return _shell_reply(
                request_id=cmd.request_id,
                session_id=cmd.session_id,
                legacy=True,
                state="running",
                handle="shell:legacy",
                exit_code=None,
                output="part1\n",
                output_start=0,
                next_offset=6,
                output_bytes=6,
            )
        if cmd.action == "check_shell":
            return _shell_reply(
                request_id=cmd.request_id,
                session_id=cmd.session_id,
                legacy=True,
                state="completed",
                handle="shell:legacy",
                exit_code=0,
                output="part2\n",
                output_start=6,
                next_offset=12,
                output_bytes=12,
            )
        return _shell_reply(
            request_id=cmd.request_id,
            session_id=cmd.session_id,
            output="out\n",
            stderr="err\n",
        )

    tool, recorded = _setup_tool(monkeypatch, handler)
    result = await tool.run_desktop_shell("legacy-cmd")

    assert result == "part1\npart2"
    assert recorded[0][0].parameters == {
        "command": "legacy-cmd",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 120,
    }
    assert recorded[1][0].parameters == {"command": "legacy-cmd", "timeout_seconds": 2}
    assert recorded[1][0].request_id != recorded[0][0].request_id
    assert recorded[2][0].parameters == {"handle": "shell:legacy", "offset": 6}
    assert "stderr_offset" not in recorded[2][0].parameters

    # Once the helper is upgraded, the next call immediately sends separate_stderr and max_runtime_seconds again.
    upgraded = True
    result2 = await tool.run_desktop_shell("upgraded-cmd", timeout=15)
    assert result2 == "out\nStderr:\nerr"
    assert recorded[3][0].parameters == {
        "command": "upgraded-cmd",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 15.0,
    }


@pytest.mark.asyncio
async def test_mindroom_output_path_success_failure_timeout_and_truncation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """mindroom_output_path saves full output on completion/failure, and refuses to save on timeout or truncation."""
    _fast_timers(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)

    def _wrapped(tool: DesktopTools) -> Callable[..., Any]:
        fn = next(fn for fn in adapt_toolkit(tool).functions if fn.name == "run_desktop_shell")
        return with_output_files(fn, policy).entrypoint

    # 1. Success with split attachments -> saves stdout, receipt reports stderr_bytes.
    out_bytes = b"full-stdout-line\n" * 3000
    err_bytes = b"some-warning\n"
    tool1, _ = _setup_tool(
        monkeypatch,
        lambda _t, _c, _to: _shell_reply(
            output_bytes=len(out_bytes),
            output_attachment=_media(out_bytes, url="mxc://example.org/out").to_content(),
            stderr_bytes=len(err_bytes),
            stderr_attachment=_media(err_bytes, url="mxc://example.org/err").to_content(),
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop_shell.download_encrypted_media",
        AsyncMock(side_effect=[out_bytes, err_bytes]),
    )
    res_ok = json.loads(await _wrapped(tool1)(args="big", mindroom_output_path="out/ok.txt"))["mindroom_tool_output"]
    assert res_ok["status"] == "saved_to_file"
    assert res_ok["stderr_bytes"] == len(err_bytes)
    assert (workspace / "out" / "ok.txt").read_text() == out_bytes.decode()

    # 2. Non-zero exit -> saves formatted failure with exit_code in receipt.
    tool2, _ = _setup_tool(
        monkeypatch,
        lambda _t, _c, _to: _shell_reply(output="out\n", stderr="err\n", exit_code=7),
    )
    res_fail = json.loads(await _wrapped(tool2)(args="fail", mindroom_output_path="out/fail.txt"))[
        "mindroom_tool_output"
    ]
    assert res_fail["status"] == "saved_to_file"
    assert res_fail["exit_code"] == 7
    assert (workspace / "out" / "fail.txt").read_text() == "Exit code 7\nOutput:\nout\n\nStderr:\nerr\n"

    # 3. Bridge-truncated output -> does not save or overwrite file.
    existing = workspace / "out" / "trunc.txt"
    existing.write_text("keep")
    tool3, _ = _setup_tool(
        monkeypatch,
        lambda _t, _c, _to: _shell_reply(output="cut", output_truncated=True, exit_code=0),
    )
    res_trunc = json.loads(await _wrapped(tool3)(args="huge", mindroom_output_path="out/trunc.txt"))[
        "mindroom_tool_output"
    ]
    assert res_trunc["status"] == "error"
    assert existing.read_text() == "keep"

    # 4. Timeout -> does not save or overwrite file, error receipt says No output file was saved.
    existing_to = workspace / "out" / "timeout.txt"
    existing_to.write_text("keep-timeout")

    killed = False

    def timeout_handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal killed
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:t", exit_code=None, output="partial\n")
        if cmd.action == "kill_shell":
            killed = True
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"state": "killed", "handle": "shell:t"},
            )
        if not killed:
            return _shell_reply(
                state="running",
                handle="shell:t",
                exit_code=None,
                output="",
                output_start=8,
                next_offset=8,
                output_bytes=8,
            )
        return _shell_reply(
            state="killed",
            handle="shell:t",
            exit_code=-15,
            output="",
            output_start=8,
            next_offset=8,
            output_bytes=8,
        )

    monkeypatch.setattr(desktop_shell_mod, "shell_call_timeout", lambda _t: 0.05)
    tool4, _ = _setup_tool(monkeypatch, timeout_handler)
    res_to = json.loads(await _wrapped(tool4)(args="sleep 30", timeout=1, mindroom_output_path="out/timeout.txt"))[
        "mindroom_tool_output"
    ]
    assert res_to["status"] == "error"
    assert "No output file was saved." in res_to["error"]
    assert existing_to.read_text() == "keep-timeout"


@pytest.mark.asyncio
async def test_timeout_escalates_from_sigterm_to_sigkill_and_drains_final_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a timed-out command ignores SIGTERM during grace, run_desktop_shell sends force=True and drains."""
    _fast_timers(monkeypatch)
    killed_force = False

    def handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal killed_force
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:stubborn", exit_code=None, output="started\n")
        if cmd.action == "kill_shell":
            if cmd.parameters.get("force"):
                killed_force = True
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"state": "killed", "handle": "shell:stubborn"},
            )
        if not killed_force:
            return _shell_reply(
                state="running",
                handle="shell:stubborn",
                exit_code=None,
                output="",
                output_start=8,
                next_offset=8,
                output_bytes=8,
            )
        return _shell_reply(
            state="killed",
            handle="shell:stubborn",
            exit_code=-9,
            output="final\n",
            output_start=8,
            next_offset=14,
            output_bytes=14,
        )

    tool, recorded = _setup_tool(monkeypatch, handler)
    with structured_tool_result() as meta:
        result = await tool.run_desktop_shell("trap '' TERM; sleep 60", timeout=0.05)

    assert isinstance(result, str)
    assert result.startswith("Stopped after 0.05s.")
    assert "started\nfinal" in result
    assert meta == {"exit_code": -9}
    kills = [cmd.parameters for cmd, _ in recorded if cmd.action == "kill_shell"]
    assert kills == [{"handle": "shell:stubborn"}, {"handle": "shell:stubborn", "force": True}]


@pytest.mark.asyncio
async def test_cancel_kills_running_handle_and_recovers_handle_before_start_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation kills a known handle, or queries request_status(start_request_id) to kill a newly created one."""
    _fast_timers(monkeypatch)

    # Case 1: Cancelled while polling a known handle.
    started_polling = asyncio.Event()

    async def handler_known(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:c1", exit_code=None)
        if cmd.action == "check_shell":
            started_polling.set()
            await asyncio.sleep(0.05)
            return _shell_reply(state="running", handle="shell:c1", exit_code=None)
        return DesktopResponse(cmd.request_id, cmd.session_id, True, result={"state": "killed", "handle": "shell:c1"})

    tool1, recorded1 = _setup_tool(monkeypatch, handler_known)
    task1 = asyncio.create_task(tool1.run_desktop_shell("sleep 60"))
    await asyncio.wait_for(started_polling.wait(), timeout=2)
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1
    assert any(cmd.action == "kill_shell" and cmd.parameters.get("handle") == "shell:c1" for cmd, _ in recorded1)

    # Case 2: Cancelled before run_shell returned its handle -> recovers handle via request_status and force-kills.
    run_sent = asyncio.Event()
    start_req_id: list[str] = []

    async def handler_early(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            start_req_id.append(cmd.request_id)
            run_sent.set()
            await asyncio.Event().wait()
        if cmd.action == "kill_shell" and "request_id" in cmd.parameters:
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                False,
                error="Unexpected desktop parameters: request_id.",
            )
        if cmd.action == "request_status":
            recorded_resp = _shell_reply(
                request_id=start_req_id[0],
                session_id=cmd.session_id,
                state="running",
                handle="shell:early",
                exit_code=None,
            )
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"request_id": start_req_id[0], "state": "completed", "response": recorded_resp.to_content()},
            )
        return DesktopResponse(
            cmd.request_id,
            cmd.session_id,
            True,
            result={"state": "killed", "handle": "shell:early"},
        )

    tool2, recorded2 = _setup_tool(monkeypatch, handler_early)
    task2 = asyncio.create_task(tool2.run_desktop_shell("sleep 60"))
    await asyncio.wait_for(run_sent.wait(), timeout=2)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2
    assert any(
        cmd.action == "request_status" and cmd.parameters == {"request_id": start_req_id[0]} for cmd, _ in recorded2
    )
    assert any(
        cmd.action == "kill_shell" and cmd.parameters == {"handle": "shell:early", "force": True}
        for cmd, _ in recorded2
    )


@pytest.mark.asyncio
async def test_request_status_auto_recovers_start_and_check_without_replaying(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport timeouts recover automatically via request_status without ever replaying run_shell or check_shell."""
    _fast_timers(monkeypatch)
    status_calls = 0

    def handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal status_calls
        if cmd.action == "run_shell":
            msg = "timeout on start"
            raise DesktopRequestError(msg, request_id=cmd.request_id)
        if cmd.action == "check_shell":
            msg = "timeout on check"
            raise DesktopRequestError(msg, request_id=cmd.request_id)
        assert cmd.action == "request_status"
        status_calls += 1
        req_id = str(cmd.parameters["request_id"])
        if status_calls == 1:
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"request_id": req_id, "state": "queued"},
            )
        if status_calls == 2:
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"request_id": req_id, "state": "running"},
            )
        if status_calls == 3:
            rec = _shell_reply(
                request_id=req_id,
                session_id=cmd.session_id,
                state="running",
                handle="shell:rec",
                exit_code=None,
                output="a\n",
            )
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"request_id": req_id, "state": "completed", "response": rec.to_content()},
            )
        rec_done = _shell_reply(
            request_id=req_id,
            session_id=cmd.session_id,
            state="completed",
            handle="shell:rec",
            exit_code=0,
            output="b\n",
            output_start=2,
            next_offset=4,
            output_bytes=4,
        )
        return DesktopResponse(
            cmd.request_id,
            cmd.session_id,
            True,
            result={"request_id": req_id, "state": "completed", "response": rec_done.to_content()},
        )

    tool, recorded = _setup_tool(monkeypatch, handler)
    result = await tool.run_desktop_shell("flaky-link")

    assert result == "a\nb"
    assert [cmd.action for cmd, _ in recorded].count("run_shell") == 1
    assert [cmd.action for cmd, _ in recorded].count("check_shell") == 1
    assert [cmd.action for cmd, _ in recorded].count("request_status") == 4


@pytest.mark.asyncio
async def test_parallel_starts_serialize_per_target_and_clean_up_idle_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent run_desktop_shell calls serialize their run_shell starts cleanly and drop the idle lock."""
    _fast_timers(monkeypatch)
    in_flight_starts = 0
    max_concurrent_starts = 0
    release_first = asyncio.Event()
    first_entered = asyncio.Event()

    async def handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal in_flight_starts, max_concurrent_starts
        if cmd.action == "run_shell":
            in_flight_starts += 1
            max_concurrent_starts = max(max_concurrent_starts, in_flight_starts)
            try:
                if cmd.parameters["command"] == "cmd-1":
                    first_entered.set()
                    await release_first.wait()
                return _shell_reply(output=f"{cmd.parameters['command']}\n")
            finally:
                in_flight_starts -= 1
        return _shell_reply()

    tool, _ = _setup_tool(monkeypatch, handler)
    t1 = asyncio.create_task(tool.run_desktop_shell("cmd-1"))
    await asyncio.wait_for(first_entered.wait(), timeout=2)
    t2 = asyncio.create_task(tool.run_desktop_shell("cmd-2"))
    await asyncio.sleep(0.02)
    assert max_concurrent_starts == 1
    release_first.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert (r1, r2) == ("cmd-1", "cmd-2")
    assert max_concurrent_starts == 1
    assert not desktop_shell_mod._START_LOCKS.get(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_human_decision_wait_enters_during_slow_approval_inside_managed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_shell waits past the approval threshold inside a managed job, human_decision_wait pauses work_timeout."""
    monkeypatch.setattr(desktop_shell_mod, "_APPROVAL_WAIT_THRESHOLD_SECONDS", 0.02)
    events: list[str] = []
    allow_approve = asyncio.Event()
    wait_entered = asyncio.Event()

    class FakeJobRuntime:
        @asynccontextmanager
        async def human_decision_wait(self, job_id: str) -> AsyncIterator[None]:
            events.append(f"enter:{job_id}")
            wait_entered.set()
            try:
                yield
            finally:
                events.append(f"exit:{job_id}")

    async def handler(_t: object, _cmd: DesktopCommand, _to: float) -> DesktopResponse:
        await allow_approve.wait()
        return _shell_reply(output="approved!\n")

    fake_paths = SimpleNamespace()
    monkeypatch.setattr("mindroom.custom_tools.desktop_shell.get_background_runtime", lambda _rp: FakeJobRuntime())
    tool, _ = _setup_tool(monkeypatch, handler, runtime_paths=fake_paths)

    with job_control_context(JobControl(job_id="job-42")):
        assert current_job_id() == "job-42"
        task = asyncio.create_task(tool.run_desktop_shell("deploy"))
        await asyncio.wait_for(wait_entered.wait(), timeout=2)
        assert events == ["enter:job-42"]
        allow_approve.set()
        result = await task

    assert result == "approved!"
    assert events == ["enter:job-42", "exit:job-42"]


@pytest.mark.asyncio
async def test_end_to_end_run_desktop_shell_through_real_desktop_shell_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Full vertical slice: DesktopTools.run_desktop_shell -> execute_shell -> DesktopShell -> split stdout/stderr."""
    _fast_timers(monkeypatch)
    shell = DesktopShell(
        environment={"PATH": os.environ["PATH"]},
        clock=time.time,
        monotonic_clock=time.monotonic,
    )
    shell.grant(60)

    async def bridge_handler(_target: object, command: DesktopCommand, _timeout: float) -> DesktopResponse:
        result = await execute_shell(AsyncMock(), shell, command)
        return DesktopResponse(command.request_id, command.session_id, True, result=result)

    tool, _ = _setup_tool(monkeypatch, bridge_handler)
    try:
        out = await tool.run_desktop_shell(
            "printf 'alpha\\nbeta\\n'; printf 'warn1\\n' >&2",
            cwd=str(tmp_path),
        )
        assert out == "alpha\nbeta\nStderr:\nwarn1"

        fail = await tool.run_desktop_shell(
            "printf 'before\\n'; printf 'err-detail\\n' >&2; exit 5",
            cwd=str(tmp_path),
        )
        assert fail == "Exit code 5\nOutput:\nbefore\nStderr:\nerr-detail"
    finally:
        await shell.close()


@pytest.mark.asyncio
async def test_cancel_while_approval_prompt_is_open_dismisses_prompt_and_never_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancelling run_desktop_shell while Mac approval is open sends kill_shell(request_id=...) and never runs."""
    _fast_timers(monkeypatch)
    shell = DesktopShell(
        environment={"PATH": os.environ["PATH"]},
        clock=time.time,
        monotonic_clock=time.monotonic,
    )
    marker = tmp_path / "should-not-exist"
    pending_visible = asyncio.Event()

    async def bridge_handler(_target: object, command: DesktopCommand, _timeout: float) -> DesktopResponse:
        if command.action == "run_shell":
            task = asyncio.create_task(execute_shell(AsyncMock(), shell, command))
            for _ in range(100):
                if shell.status()["pending"] is not None:
                    pending_visible.set()
                    break
                await asyncio.sleep(0.01)
            result = await task
            return DesktopResponse(command.request_id, command.session_id, True, result=result)
        result = await execute_shell(AsyncMock(), shell, command)
        return DesktopResponse(command.request_id, command.session_id, True, result=result)

    tool, recorded = _setup_tool(monkeypatch, bridge_handler)
    try:
        run_task = asyncio.create_task(tool.run_desktop_shell(f"touch {marker}", cwd=str(tmp_path)))
        await asyncio.wait_for(pending_visible.wait(), timeout=2)
        assert shell.status()["pending"] is not None

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

        assert shell.status()["pending"] is None
        assert not marker.exists()
        start_req_id = recorded[0][0].request_id
        assert any(cmd.action == "kill_shell" and cmd.parameters == {"request_id": start_req_id} for cmd, _ in recorded)
    finally:
        await shell.close()


@pytest.mark.asyncio
async def test_unconfirmed_timeout_kill_does_not_report_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When timeout stop cannot be confirmed, report honestly that the remote command may still be running."""
    _fast_timers(monkeypatch)

    def still_running(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:stuck", exit_code=None, output="partial\n")
        if cmd.action == "kill_shell":
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"state": "killed", "handle": "shell:stuck"},
            )
        return _shell_reply(
            state="running",
            handle="shell:stuck",
            exit_code=None,
            output="",
            output_start=8,
            next_offset=8,
            output_bytes=8,
        )

    tool, _ = _setup_tool(monkeypatch, still_running)
    res = await tool.run_desktop_shell("unstoppable", timeout=0.05)
    assert isinstance(res, str)
    assert "Stopped after" not in res
    assert "could not be confirmed" in res
    assert "may still be running" in res
    assert "partial" in res

    # Also verify mindroom_output_path refuses to save and keeps existing file intact.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "out.txt"
    existing.write_text("keep")
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)
    monkeypatch.setattr(desktop_shell_mod, "shell_call_timeout", lambda _t: 0.05)
    tool2, _ = _setup_tool(monkeypatch, still_running)
    fn = next(fn for fn in adapt_toolkit(tool2).functions if fn.name == "run_desktop_shell")
    receipt = json.loads(
        await with_output_files(fn, policy).entrypoint(args="unstoppable", timeout=1, mindroom_output_path="out.txt"),
    )["mindroom_tool_output"]
    assert receipt["status"] == "error"
    assert "Stopped after" not in receipt["error"]
    assert "could not be confirmed" in receipt["error"]
    assert "No output file was saved." in receipt["error"]
    assert existing.read_text() == "keep"


@pytest.mark.asyncio
async def test_failed_attachment_download_preserves_request_id_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed attachment download preserves response.request_id and request_status recovery instruction without re-running."""
    out_bytes = b"big-output" * 100
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop_shell.download_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("download timed out")),
    )
    tool, recorded = _setup_tool(
        monkeypatch,
        lambda _t, cmd, _to: _shell_reply(
            request_id=cmd.request_id,
            session_id=cmd.session_id,
            exit_code=3,
            output_bytes=len(out_bytes),
            output_attachment=_media(out_bytes).to_content(),
        ),
    )
    with structured_tool_result() as meta:
        res = await tool.run_desktop_shell("big-cmd")
    req_id = recorded[0][0].request_id
    assert isinstance(res, str)
    assert res.startswith("Error:")
    assert req_id in res
    assert f'desktop(action="request_status", request_id={req_id!r})' in res
    assert "without re-running" in res
    assert "exit code 3" in res
    assert meta == {"exit_code": 3}

    # Same recovery hint is preserved in error receipt when mindroom_output_path is set.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)
    tool2, recorded2 = _setup_tool(
        monkeypatch,
        lambda _t, cmd, _to: _shell_reply(
            request_id=cmd.request_id,
            session_id=cmd.session_id,
            exit_code=0,
            output_bytes=len(out_bytes),
            output_attachment=_media(out_bytes).to_content(),
        ),
    )
    fn = next(fn for fn in adapt_toolkit(tool2).functions if fn.name == "run_desktop_shell")
    receipt = json.loads(
        await with_output_files(fn, policy).entrypoint(args="big-cmd", mindroom_output_path="out.txt"),
    )["mindroom_tool_output"]
    req_id2 = recorded2[0][0].request_id
    assert receipt["status"] == "error"
    assert f'desktop(action="request_status", request_id={req_id2!r})' in receipt["error"]
    assert not (workspace / "out.txt").exists()


@pytest.mark.asyncio
async def test_bridge_truncation_notice_on_inline_exit_zero_nonzero_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline exit 0, non-zero exit, and timeout results prepend an explicit notice on 10 MiB bridge truncation."""
    _fast_timers(monkeypatch)
    replies = iter(
        [
            _shell_reply(output="prefix-ok\n", output_truncated=True, exit_code=0),
            _shell_reply(output="prefix-fail\n", stderr="err\n", output_truncated=True, exit_code=2),
        ],
    )
    tool, _ = _setup_tool(monkeypatch, lambda _t, _c, _to: next(replies))

    res_ok = await tool.run_desktop_shell("noisy-ok")
    assert isinstance(res_ok, str)
    assert res_ok.startswith("[Desktop shell output exceeded the 10 MiB capture limit; later output was dropped.]\n")
    assert res_ok.endswith("prefix-ok")

    res_fail = await tool.run_desktop_shell("noisy-fail")
    assert isinstance(res_fail, str)
    assert res_fail.startswith(
        "[Desktop shell output exceeded the 10 MiB capture limit; later output was dropped.]\nExit code 2\n",
    )
    assert "prefix-fail" in res_fail
    assert "Stderr:\nerr" in res_fail

    # Timeout with truncated final drained output also prepends the notice.
    killed = False

    def timeout_truncated(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal killed
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:trunc", exit_code=None, output="part\n")
        if cmd.action == "kill_shell":
            killed = True
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"state": "killed", "handle": "shell:trunc"},
            )
        if not killed:
            return _shell_reply(
                state="running",
                handle="shell:trunc",
                exit_code=None,
                output="",
                output_start=5,
                next_offset=5,
                output_bytes=5,
            )
        return _shell_reply(
            state="killed",
            handle="shell:trunc",
            exit_code=-15,
            output="",
            output_start=5,
            next_offset=5,
            output_bytes=5,
            output_truncated=True,
        )

    tool_to, _ = _setup_tool(monkeypatch, timeout_truncated)
    res_to = await tool_to.run_desktop_shell("noisy-timeout", timeout=0.05)
    assert isinstance(res_to, str)
    assert res_to.startswith(
        "[Desktop shell output exceeded the 10 MiB capture limit; later output was dropped.]\nStopped after 0.05s.",
    )


@pytest.mark.asyncio
async def test_router_conflict_on_run_shell_reports_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-send DesktopRequestError on run_shell reports not_started=True, while one on check_shell does not."""
    _fast_timers(monkeypatch)

    def conflict_start(_t: object, _c: DesktopCommand, _to: float) -> DesktopResponse:
        msg = "A desktop request is already in progress for this device."
        raise DesktopRequestError(msg)

    tool1, _ = _setup_tool(monkeypatch, conflict_start)
    with structured_tool_result() as meta1:
        res1 = await tool1.run_desktop_shell("echo hi")
    assert isinstance(res1, str)
    assert res1.startswith("Error:")
    assert meta1 == {"not_started": True}

    def not_started_start(_t: object, _c: DesktopCommand, _to: float) -> DesktopResponse:
        err = DesktopRequestError("router conflict")
        err.action_outcome = "not_started"
        raise err

    tool2, _ = _setup_tool(monkeypatch, not_started_start)
    with structured_tool_result() as meta2:
        res2 = await tool2.run_desktop_shell("echo hi")
    assert isinstance(res2, str)
    assert res2.startswith("Error:")
    assert meta2 == {"not_started": True}

    def conflict_check(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:running", exit_code=None)
        msg = "A desktop request is already in progress for this device."
        raise DesktopRequestError(msg)

    tool3, _ = _setup_tool(monkeypatch, conflict_check)
    with structured_tool_result() as meta3:
        res3 = await tool3.run_desktop_shell("sleep 10")
    assert isinstance(res3, str)
    assert res3.startswith("Error:")
    assert "not_started" not in meta3


@pytest.mark.asyncio
async def test_pre_start_errors_do_not_overwrite_mindroom_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pre-start errors (unpaired desktop, missing context, invalid args) never overwrite mindroom_output_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "existing.txt"
    existing.write_text("original-contents")
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)

    def _wrapped(tool: DesktopTools) -> Callable[..., Any]:
        fn = next(fn for fn in adapt_toolkit(tool).functions if fn.name == "run_desktop_shell")
        return with_output_files(fn, policy).entrypoint

    # 1. Unpaired desktop leaves existing file untouched and returns the error string (not saved_to_file).
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(requester_id=ALICE, agent_name="computer", client=object()),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.load_desktop_credentials", lambda *_a, **_kw: None)
    unpaired_tool = DesktopTools(credentials_manager=MagicMock(spec=CredentialsManager), worker_target=_target())
    with structured_tool_result() as meta_unpaired:
        res_unpaired = await _wrapped(unpaired_tool)(args="echo hi", mindroom_output_path="existing.txt")
    assert isinstance(res_unpaired, str)
    assert res_unpaired.startswith("Error:")
    assert "/desktop setup" in res_unpaired
    assert "saved_to_file" not in res_unpaired
    assert meta_unpaired == {"not_started": True}
    assert existing.read_text() == "original-contents"

    # 2. Invalid args leave existing file untouched and return the error string (not saved_to_file).
    ready_tool, recorded = _setup_tool(monkeypatch, lambda *_args: _shell_reply())
    with structured_tool_result() as meta_invalid:
        res_invalid = await _wrapped(ready_tool)(args="echo hi", tail=-1, mindroom_output_path="existing.txt")
    assert isinstance(res_invalid, str)
    assert res_invalid.startswith("Error:")
    assert "tail must be a non-negative integer" in res_invalid
    assert "saved_to_file" not in res_invalid
    assert meta_invalid == {"not_started": True}
    assert recorded == []
    assert existing.read_text() == "original-contents"

    # 3. Missing live runtime context leaves existing file untouched and returns the error string.
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: None)
    with structured_tool_result() as meta_no_ctx:
        res_no_ctx = await _wrapped(ready_tool)(args="echo hi", mindroom_output_path="existing.txt")
    assert isinstance(res_no_ctx, str)
    assert res_no_ctx.startswith("Error:")
    assert "saved_to_file" not in res_no_ctx
    assert meta_no_ctx == {"not_started": True}
    assert existing.read_text() == "original-contents"


@pytest.mark.asyncio
async def test_helper_enforced_timeout_formats_like_native_and_saves_no_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Helper-enforced max_runtime_seconds timeouts format identically to native timeouts and save no output file."""
    _fast_timers(monkeypatch)

    # Managed job without explicit timeout omits max_runtime_seconds.
    tool_job, recorded_job = _setup_tool(monkeypatch, lambda *_a: _shell_reply(output="done\n"))
    with job_control_context(JobControl(job_id="job-9")):
        assert await tool_job.run_desktop_shell("long-job") == "done"
    assert recorded_job[0][0].parameters == {
        "command": "long-job",
        "timeout_seconds": 2,
        "separate_stderr": True,
    }

    # Helper returns state="killed", timed_out=True across a paged drain -> matches native timeout formatting.
    def helper_timeout_handler(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            return _shell_reply(
                state="running",
                handle="shell:ht",
                exit_code=None,
                output="line1\n",
                stderr="err1\n",
            )
        if cmd.action == "check_shell" and cmd.parameters["offset"] == 6:
            return _shell_reply(
                state="killed",
                handle="shell:ht",
                exit_code=-15,
                timed_out=True,
                output="line2\n",
                output_start=6,
                next_offset=12,
                output_bytes=18,
                output_truncated=True,
                stderr="",
                stderr_start=5,
                stderr_next_offset=5,
                stderr_bytes=5,
            )
        return _shell_reply(
            state="killed",
            handle="shell:ht",
            exit_code=-15,
            timed_out=True,
            output="line3\n",
            output_start=12,
            next_offset=18,
            output_bytes=18,
            output_truncated=False,
            stderr="",
            stderr_start=5,
            stderr_next_offset=5,
            stderr_bytes=5,
        )

    tool, recorded = _setup_tool(monkeypatch, helper_timeout_handler)
    with structured_tool_result() as meta:
        res = await tool.run_desktop_shell("sleep 30", timeout=5)
    assert res == "Stopped after 5s.\nOutput:\nline1\nline2\nline3\nStderr:\nerr1"
    assert meta == {"exit_code": -15}
    assert recorded[0][0].parameters["max_runtime_seconds"] == 5.0
    assert not any(cmd.action == "kill_shell" for cmd, _ in recorded)

    # With mindroom_output_path, helper timeout leaves existing file untouched and returns an error receipt.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "out.txt"
    existing.write_text("keep-me")
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)
    tool_file, _ = _setup_tool(
        monkeypatch,
        lambda _t, cmd, _to: (
            _shell_reply(state="running", handle="shell:ht2", exit_code=None, output="part\n")
            if cmd.action == "run_shell"
            else _shell_reply(
                state="killed",
                handle="shell:ht2",
                exit_code=-15,
                timed_out=True,
                output="",
                output_start=5,
                next_offset=5,
                output_bytes=5,
            )
        ),
    )
    fn = next(fn for fn in adapt_toolkit(tool_file).functions if fn.name == "run_desktop_shell")
    receipt = json.loads(
        await with_output_files(fn, policy).entrypoint(args="sleep 30", timeout=5, mindroom_output_path="out.txt"),
    )["mindroom_tool_output"]
    assert receipt["status"] == "error"
    assert receipt["error"] == "Stopped after 5s. No output file was saved.\nOutput:\npart"
    assert existing.read_text() == "keep-me"

    # Legacy helper that accepts separate_stderr but rejects max_runtime_seconds retries cleanly omitting both.
    def reject_max_runtime(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell" and "max_runtime_seconds" in cmd.parameters:
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                False,
                error="Unexpected desktop parameters: max_runtime_seconds.",
            )
        return _shell_reply(request_id=cmd.request_id, session_id=cmd.session_id, legacy=True, output="ok\n")

    tool_legacy, recorded_legacy = _setup_tool(monkeypatch, reject_max_runtime)
    assert await tool_legacy.run_desktop_shell("echo ok", timeout=5) == "ok"
    assert recorded_legacy[0][0].parameters == {
        "command": "echo ok",
        "timeout_seconds": 2,
        "separate_stderr": True,
        "max_runtime_seconds": 5.0,
    }
    assert recorded_legacy[1][0].parameters == {"command": "echo ok", "timeout_seconds": 2}
    assert recorded_legacy[1][0].request_id != recorded_legacy[0][0].request_id


@pytest.mark.asyncio
async def test_unrecoverable_poll_error_stops_running_handle_and_reports_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When polling fails on a running handle and bounded stop succeeds, report it stopped and save no output file."""
    _fast_timers(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)
    stopped_on_desktop = False

    def poll_fails_stop_succeeds(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        nonlocal stopped_on_desktop
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:err1", exit_code=None)
        if cmd.action == "kill_shell":
            stopped_on_desktop = True
            return DesktopResponse(
                cmd.request_id,
                cmd.session_id,
                True,
                result={"state": "killed", "handle": "shell:err1"},
            )
        if stopped_on_desktop:
            return _shell_reply(state="killed", handle="shell:err1", exit_code=-15)
        msg = "olm session broken"
        raise OlmToDeviceError(msg)

    tool, recorded = _setup_tool(monkeypatch, poll_fails_stop_succeeds)
    result = await tool.run_desktop_shell("sleep 60")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "olm session broken" in result
    assert "The remote command was stopped." in result
    assert any(cmd.action == "kill_shell" and cmd.parameters == {"handle": "shell:err1"} for cmd, _ in recorded)

    existing = workspace / "err1.txt"
    existing.write_text("keep-1")
    stopped_on_desktop = False
    tool_file, _ = _setup_tool(monkeypatch, poll_fails_stop_succeeds)
    fn = next(fn for fn in adapt_toolkit(tool_file).functions if fn.name == "run_desktop_shell")
    receipt = json.loads(
        await with_output_files(fn, policy).entrypoint(args="sleep 60", mindroom_output_path="err1.txt"),
    )["mindroom_tool_output"]
    assert receipt["status"] == "error"
    assert "The remote command was stopped." in receipt["error"]
    assert existing.read_text() == "keep-1"


@pytest.mark.asyncio
async def test_unrecoverable_poll_error_reports_when_stop_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When polling fails and bounded stop cannot be confirmed, report the command may still be running."""
    _fast_timers(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1_000_000)

    def poll_and_stop_fail(_t: object, cmd: DesktopCommand, _to: float) -> DesktopResponse:
        if cmd.action == "run_shell":
            return _shell_reply(state="running", handle="shell:err2", exit_code=None)
        if cmd.action == "kill_shell":
            msg = "device unreachable"
            raise OlmToDeviceError(msg)
        msg = "malformed check payload"
        raise DesktopProtocolError(msg)

    tool, recorded = _setup_tool(monkeypatch, poll_and_stop_fail)
    result = await tool.run_desktop_shell("sleep 60")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "malformed check payload" in result
    assert "Stopping the remote command could not be confirmed; it may still be running on the desktop." in result
    assert [cmd.parameters for cmd, _ in recorded if cmd.action == "kill_shell"] == [
        {"handle": "shell:err2"},
        {"handle": "shell:err2", "force": True},
    ]

    existing = workspace / "err2.txt"
    existing.write_text("keep-2")
    tool_file, _ = _setup_tool(monkeypatch, poll_and_stop_fail)
    fn = next(fn for fn in adapt_toolkit(tool_file).functions if fn.name == "run_desktop_shell")
    receipt = json.loads(
        await with_output_files(fn, policy).entrypoint(args="sleep 60", mindroom_output_path="err2.txt"),
    )["mindroom_tool_output"]
    assert receipt["status"] == "error"
    assert "may still be running on the desktop" in receipt["error"]
    assert existing.read_text() == "keep-2"
