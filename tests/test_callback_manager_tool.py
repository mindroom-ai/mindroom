"""Tests for callback script minting."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.callback_manager import CallbackManagerTools
from mindroom.external_triggers.store import ExternalTriggerStore
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    import pytest


class _Client:
    user_id = "@mindroom_coder:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _config() -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
            "agents": {"coder": {"display_name": "Coder", "model": "default", "rooms": ["lobby"]}},
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "authorization": {
                "global_users": ["@owner:example.org"],
                "agent_reply_permissions": {"*": ["@owner:example.org"]},
            },
        },
    )


def _context(tmp_path: Path, *, requester_id: str = "@owner:example.org") -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="coder",
        target=MessageTarget.resolve(room_id="lobby", thread_id="$thread", reply_to_event_id=None),
        requester_id=requester_id,
        client=cast("Any", _Client()),
        config=_config(),
        runtime_paths=_runtime_paths(tmp_path),
        event_cache=cast("Any", object()),
        conversation_cache=cast("Any", object()),
    )


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


def test_mint_callback_writes_one_bound_script(tmp_path: Path) -> None:
    """Minting returns one script bound to the live conversation."""
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    script_path = Path(payload["script_path"])
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
    assert f"/api/triggers/{callback_id}" in script_text
    assert "CALLBACK_TOKEN=mrt_" in script_text
    assert "mrt_" not in store.store_path.read_text(encoding="utf-8")


def test_generated_script_posts_summary_and_deletes_itself(tmp_path: Path) -> None:
    """The Bash-only script posts a safe summary then removes itself."""
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(CallbackManagerTools().mint_callback("quote-safe callback"))
    script_path = Path(payload["script_path"])
    script_text = script_path.read_text(encoding="utf-8")
    assert "python3" not in script_text
    assert "jq" not in script_text

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
