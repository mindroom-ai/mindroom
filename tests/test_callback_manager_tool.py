"""Tests for callback script minting."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.callback_manager import CallbackManagerTools
from mindroom.external_triggers.store import ExternalTriggerStore
from mindroom.message_target import MessageTarget
from mindroom.runtime_state import reset_runtime_state, set_api_server_address
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import resolve_unscoped_worker_key, resolve_worker_key, visible_workspace_roots
from mindroom.workers.backends._dedicated_worker_common import plan_scoped_workspace_mounts
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import make_conversation_reader_mock, make_relation_lookup


class _Client:
    user_id = "@mindroom_coder:example.org"


def _runtime_paths(tmp_path: Path, process_env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )


def _config(*, enabled: bool = True) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {
                "coder": {
                    "display_name": "Coder",
                    "model": "default",
                    "rooms": ["lobby"],
                    "access": {"users": ["@owner:example.org"]},
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "external_trigger_policy": {"enabled": enabled},
            "administrators": ["@owner:example.org"],
        },
    )


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@owner:example.org",
    process_env: dict[str, str] | None = None,
) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="coder",
        target=MessageTarget.resolve(room_id="lobby", thread_id="$thread", reply_to_event_id=None),
        requester_id=requester_id,
        client=cast("Any", _Client()),
        config=_config(),
        runtime_paths=_runtime_paths(tmp_path, process_env),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


def test_mint_callback_writes_one_bound_script(tmp_path: Path) -> None:
    """Minting returns one script bound to the live conversation."""
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    script_path = Path(payload["script_path"])
    assert script_path.is_relative_to(_workspace(tmp_path))
    assert payload["instruction"] == f'When finished, run: bash {script_path} "<short result summary>"'
    assert stat.S_IMODE(script_path.stat().st_mode) == 0o700
    assert script_path.parent.joinpath(".gitignore").read_text(encoding="utf-8") == "*\n"

    callback_id = script_path.stem
    store = ExternalTriggerStore(_runtime_paths(tmp_path))
    [record] = store.list_records()
    assert record.trigger_id == callback_id
    assert record.owner_user_id == "@owner:example.org"
    assert record.created_in_room_id == "lobby"
    assert record.created_in_thread_id == "$thread"
    assert record.target.room_id == "lobby"
    assert record.target.thread_id == "$thread"
    assert record.target.agent == "coder"
    assert record.description == "issue-042 implementer"
    assert record.auth == "capability"
    assert record.delivery_mode == "single_use"
    assert record.allowed_kinds == ("mindroom.callback.completed",)

    script_text = script_path.read_text(encoding="utf-8")
    assert f"CALLBACK_URL=http://127.0.0.1:8765/api/triggers/{callback_id}" in script_text
    assert "CALLBACK_TOKEN=mrt_" in script_text
    assert "mrt_" not in store.store_path.read_text(encoding="utf-8")


def test_a_private_agent_writes_its_callback_script_into_the_requester_workspace(tmp_path: Path) -> None:
    """A private agent's script lands in its requester's workspace, which that requester's worker mounts."""
    context = _context(tmp_path)
    data = context.config.model_dump(mode="json", exclude_unset=True)
    data["agents"]["coder"]["private"] = {"per": "user"}
    context = replace(context, config=Config.model_validate(data))

    with tool_runtime_context(context):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    # The requester's user-scope worker mounts exactly the workspace the script landed in.
    identity = build_execution_identity_from_runtime_context(context)
    worker_key = resolve_worker_key("user", identity, agent_name="coder")
    assert worker_key is not None
    mounted = visible_workspace_roots(
        context.runtime_paths.storage_root,
        worker_key,
        context.config.get_agent_policies(),
        private_agent_names=frozenset({"coder"}),
    )
    script_path = Path(payload["script_path"])
    assert any(script_path.is_relative_to(root) for root in mounted)
    assert not script_path.is_relative_to(_workspace(tmp_path))
    assert script_path.is_file()


def test_mint_callback_needs_no_hard_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspaces on SMB, gcsfuse, and similar mounts lack hard links, so publication never needs one."""

    def no_hard_links(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_hard_links)
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    assert stat.S_IMODE(Path(payload["script_path"]).stat().st_mode) == 0o700


def test_mint_callback_uses_current_trigger_policy(tmp_path: Path) -> None:
    """A long-lived tool context must honor a current trigger disablement."""
    context = replace(
        _context(tmp_path),
        config_provider=lambda: _config(enabled=False),
    )

    with tool_runtime_context(context):
        payload = _payload(CallbackManagerTools().mint_callback("revoked callback"))

    assert payload == {
        "message": "Callback manager requires external triggers to be enabled.",
        "status": "error",
        "tool": "callback_manager",
    }


def test_mint_callback_targets_bound_api_server_port(tmp_path: Path) -> None:
    """The script URL follows the port the running API server actually bound."""
    set_api_server_address("0.0.0.0", 8766)  # noqa: S104
    try:
        with tool_runtime_context(_context(tmp_path)):
            payload = _payload(CallbackManagerTools().mint_callback("non-default port"))
    finally:
        reset_runtime_state()

    script_text = Path(payload["script_path"]).read_text(encoding="utf-8")
    assert "CALLBACK_URL=http://127.0.0.1:8766/api/triggers/callback_" in script_text
    assert ":8765" not in script_text


@pytest.mark.parametrize(
    ("host", "expected_base_url"),
    [
        ("::", "http://[::1]:8766"),
        ("::1", "http://[::1]:8766"),
    ],
)
def test_mint_callback_formats_ipv6_api_server_address(
    tmp_path: Path,
    host: str,
    expected_base_url: str,
) -> None:
    """IPv6 bind addresses produce reachable, bracketed callback URLs."""
    set_api_server_address(host, 8766)
    try:
        with tool_runtime_context(_context(tmp_path)):
            payload = _payload(CallbackManagerTools().mint_callback("IPv6 address"))
    finally:
        reset_runtime_state()

    script_text = Path(payload["script_path"]).read_text(encoding="utf-8")
    assert f"{expected_base_url}/api/triggers/callback_" in script_text


def test_mint_callback_prefers_explicit_mindroom_url(tmp_path: Path) -> None:
    """An explicit MINDROOM_URL wins over the bound API server address."""
    set_api_server_address("0.0.0.0", 8766)  # noqa: S104
    try:
        env = {"MINDROOM_URL": "https://hub.example.org/"}
        with tool_runtime_context(_context(tmp_path, process_env=env)):
            payload = _payload(CallbackManagerTools().mint_callback("explicit url"))
    finally:
        reset_runtime_state()

    script_text = Path(payload["script_path"]).read_text(encoding="utf-8")
    assert "CALLBACK_URL=https://hub.example.org/api/triggers/callback_" in script_text


def test_generated_script_posts_summary_and_deletes_itself(tmp_path: Path) -> None:
    """The Bash-only script posts a safe summary then removes itself."""
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("quote-safe callback"))
    script_path = Path(payload["script_path"])
    script_text = script_path.read_text(encoding="utf-8")
    # Whole words only. The script embeds a minted callback token, and a random
    # token containing the letters "jq" is not the script reaching for `jq`.
    assert not re.search(r"\bpython3\b", script_text)
    assert not re.search(r"\bjq\b", script_text)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    capture_path = tmp_path / "curl-args.txt"
    capture_stdin_path = tmp_path / "curl-stdin.txt"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\ncat > "$CAPTURE_STDIN"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    message = 'quoted "summary" with \\ slash'

    completed = subprocess.run(
        [script_path, message],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CAPTURE_ARGS": str(capture_path),
            "CAPTURE_STDIN": str(capture_stdin_path),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )

    curl_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert curl_args[curl_args.index("--connect-timeout") + 1] == "10"
    assert curl_args[curl_args.index("--max-time") + 1] == "60"
    assert curl_args[curl_args.index("-H") + 1] == "@-"
    assert "mrt_" not in "\n".join(curl_args)
    assert capture_stdin_path.read_text(encoding="utf-8").startswith("Authorization: Bearer mrt_")
    assert json.loads(curl_args[curl_args.index("--data") + 1]) == {
        "kind": "mindroom.callback.completed",
        "title": "✅ quote-safe callback",
        "message": message,
    }
    assert completed.stdout.strip() == "MindRoom notified."
    assert not script_path.exists()


def _run_instruction(tmp_path: Path, instruction: str, env: dict[str, str]) -> str:
    """Run the instruction's command as a background task would, with curl faked, and return its output."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text("#!/usr/bin/env bash\ncat > /dev/null\n", encoding="utf-8")
    (fake_bin / "curl").chmod(0o700)
    command = instruction.removeprefix("When finished, run: ").replace("<short result summary>", "done")
    completed = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env={**env, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("execution_mode", "worker_backend", "shell_in_worker"),
    [
        ("off", "static_runner", False),
        ("off", "docker", False),
        ("all", "docker", True),
    ],
    ids=["local_shell", "local_shell_beside_dedicated_workers", "dedicated_worker_shell"],
)
def test_the_instruction_runs_without_the_agent_shell_environment(
    tmp_path: Path,
    execution_mode: str,
    worker_backend: str,
    shell_in_worker: bool,
) -> None:
    """The instruction names the script where the agent's shell finds it, so a task in a fresh tmux session runs it."""
    worker_storage = tmp_path / "worker-storage"
    process_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": execution_mode,
        "MINDROOM_WORKER_BACKEND": worker_backend,
        "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test",
        "MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH": str(worker_storage),
    }
    context = _context(tmp_path, process_env=process_env)
    with tool_runtime_context(context):
        payload = _payload(CallbackManagerTools().mint_callback("background callback"))
    script_path = Path(payload["script_path"])
    assert script_path.is_relative_to(worker_storage) == shell_in_worker
    if shell_in_worker:
        # The instruction names the script where the worker's mount planner puts the workspace.
        [mount] = plan_scoped_workspace_mounts(
            worker_key=resolve_unscoped_worker_key("coder"),
            local_shared_storage_root=context.runtime_paths.storage_root,
            worker_visible_shared_storage_root=worker_storage,
            private_agent_names=frozenset(),
            resolved_agent_policies=context.config.get_agent_policies(),
        )
        assert script_path.parent.parent.parent == mount.worker_visible_path
        # The worker sees the workspace only below its own storage mount, never at the primary's path.
        shutil.copytree(context.runtime_paths.storage_root, worker_storage)
        shutil.rmtree(context.runtime_paths.storage_root)
    # Like a fresh tmux session, the task inherits none of the agent shell's variables, which tests may run inside.
    env = {name: value for name, value in os.environ.items() if name != "MINDROOM_AGENT_WORKSPACE"}

    assert _run_instruction(tmp_path, payload["instruction"], env) == "MindRoom notified."
    assert not script_path.exists()


def test_a_local_shell_mints_beside_an_unconfigured_worker_backend(tmp_path: Path) -> None:
    """Only a worker shell needs the worker backend's settings to name the script."""
    process_env = {"MINDROOM_SANDBOX_EXECUTION_MODE": "off", "MINDROOM_WORKER_BACKEND": "docker"}
    with tool_runtime_context(_context(tmp_path, process_env=process_env)):
        payload = _payload(CallbackManagerTools().mint_callback("local callback"))

    assert payload["status"] == "ok"
    assert Path(payload["script_path"]).is_relative_to(_workspace(tmp_path))


def test_a_worker_shell_with_an_unconfigured_backend_mints_nothing(tmp_path: Path) -> None:
    """When the worker's path cannot be named, no record or token-bearing script is left behind."""
    process_env = {"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_WORKER_BACKEND": "docker"}
    with tool_runtime_context(_context(tmp_path, process_env=process_env)):
        payload = _payload(CallbackManagerTools().mint_callback("worker callback"))

    assert payload["status"] == "error"
    assert "MINDROOM_DOCKER_WORKER_IMAGE" in payload["message"]
    assert ExternalTriggerStore(_runtime_paths(tmp_path)).list_records() == []
    assert not (_workspace(tmp_path) / ".mindroom" / "callbacks").exists()


def test_script_failure_rolls_back_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script write failure leaves no live callback record."""

    def fail_write(*_args: object, **_kwargs: object) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr("mindroom.custom_tools.callback_manager.write_callback_script", fail_write)
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("rollback"))

    assert payload["status"] == "error"
    store_payload = json.loads(ExternalTriggerStore(_runtime_paths(tmp_path)).store_path.read_text(encoding="utf-8"))
    assert store_payload["triggers"] == {}


def test_manager_requires_live_human_requester(tmp_path: Path) -> None:
    """Callbacks are minted only inside a human-owned Matrix turn."""
    tool = CallbackManagerTools()
    no_context = _payload(tool.mint_callback("no context"))
    with tool_runtime_context(_context(tmp_path, requester_id=_Client.user_id)):
        bot_requester = _payload(tool.mint_callback("bot requester"))

    assert no_context["status"] == "error"
    assert "live Matrix tool context" in no_context["message"]
    assert bot_requester["status"] == "error"
    assert "human Matrix requester" in bot_requester["message"]


def _workspace(tmp_path: Path) -> Path:
    return _runtime_paths(tmp_path).storage_root / "agents" / "coder" / "workspace"


@pytest.mark.parametrize("replaced", ["workspace", "callbacks_dir"])
def test_mint_callback_refuses_a_linked_workspace_directory(tmp_path: Path, replaced: str) -> None:
    """A workspace or callbacks directory swapped for a link never receives the capability script."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    workspace = _workspace(tmp_path)
    if replaced == "workspace":
        workspace.parent.mkdir(parents=True)
        workspace.symlink_to(elsewhere, target_is_directory=True)
    else:
        (workspace / ".mindroom").mkdir(parents=True)
        (workspace / ".mindroom" / "callbacks").symlink_to(elsewhere, target_is_directory=True)

    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "error"
    assert not list(elsewhere.iterdir())
    assert ExternalTriggerStore(_runtime_paths(tmp_path)).list_records() == []


def test_mint_callback_does_not_write_through_a_planted_gitignore_link(tmp_path: Path) -> None:
    """A link planted as the callbacks .gitignore is left alone instead of truncating or creating its target."""
    callbacks_dir = _workspace(tmp_path) / ".mindroom" / "callbacks"
    callbacks_dir.mkdir(parents=True)
    existing_target = tmp_path / "primary-file.json"
    existing_target.write_text('{"secret": "kept"}', encoding="utf-8")
    (callbacks_dir / ".gitignore").symlink_to(existing_target)

    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    assert existing_target.read_text(encoding="utf-8") == '{"secret": "kept"}'
    assert stat.S_IMODE(Path(payload["script_path"]).stat().st_mode) == 0o700
