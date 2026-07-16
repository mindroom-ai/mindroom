"""Tests for the local-only callback manager tool."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.callbacks.store import CallbackStore, CallbackStoreError
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.callback_manager import CallbackManagerTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    from typing import Any

    import pytest


class _Client:
    user_id = "@mindroom_coder:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _config(
    *,
    admin_users: list[str] | None = None,
    callback_policy: dict[str, object] | None = None,
) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
            "agents": {
                "coder": {
                    "display_name": "Coder",
                    "role": "Write code.",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}},
            "callback_policy": callback_policy or {},
            "external_trigger_policy": {"admin_users": admin_users or []},
            "authorization": {
                "global_users": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                "agent_reply_permissions": {
                    "*": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                },
            },
        },
    )


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@owner:example.org",
    config: Config | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="coder",
        target=MessageTarget.resolve(
            room_id="lobby",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=cast("Any", _Client()),
        config=config or _config(),
        runtime_paths=_runtime_paths(tmp_path),
        event_cache=cast("Any", object()),
        conversation_cache=cast("Any", object()),
    )


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


def test_mint_callback_binds_current_context_and_writes_script(tmp_path: Path) -> None:
    """Minting binds to the live room and thread and materializes a runnable script."""
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(tool.mint_callback("issue-042 implementer"))

    assert payload["status"] == "ok"
    callback = payload["callback"]
    assert callback["owner_user_id"] == "@owner:example.org"
    assert callback["target"] == {"room_id": "lobby", "thread_id": "$thread", "agent": "coder"}
    assert callback["uses_left"] == 1

    script_path = tmp_path / "mindroom_data" / "agents" / "coder" / "workspace" / ".mindroom" / "callbacks"
    assert payload["script_path"] == str(script_path / f"{payload['callback_id']}.sh")
    assert (script_path / ".gitignore").read_text(encoding="utf-8") == "*\n"
    script_file = script_path / f"{payload['callback_id']}.sh"
    mode = stat.S_IMODE(script_file.stat().st_mode)
    assert mode == 0o700

    script_text = script_file.read_text(encoding="utf-8")
    assert f"/api/callbacks/{payload['callback_id']}" in script_text
    assert "CALLBACK_TOKEN=mrcb_" in script_text
    assert "Authorization: Bearer $CALLBACK_TOKEN" in script_text
    assert payload["callback_id"] in payload["curl_snippet"]
    assert "Bearer mrcb_" in payload["curl_snippet"]
    assert payload["brief_snippet"].startswith(f"When finished, run: bash {script_file} done ")

    # The raw token lives only in the generated artifacts, never in the store.
    control_state_root = _runtime_paths(tmp_path).control_state_root
    assert control_state_root is not None
    store_text = (control_state_root / "callbacks" / "records.json").read_text(encoding="utf-8")
    raw_token = payload["curl_snippet"].split("Bearer ")[1].split("'")[0]
    assert raw_token not in store_text


def test_generated_script_needs_only_bash_and_curl_and_preserves_json_message(tmp_path: Path) -> None:
    """The generated consumer script safely builds JSON without Python or jq."""
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(tool.mint_callback("quote-safe callback"))
    script_path = Path(payload["script_path"])
    script_text = script_path.read_text(encoding="utf-8")
    assert "python3" not in script_text
    assert "jq" not in script_text

    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    capture_path = tmp_path / "curl-args.txt"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n', encoding="utf-8")
    fake_curl.chmod(0o700)
    message = 'quoted "summary" with \\ slash'
    completed = subprocess.run(
        [script_path, "done", message],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CAPTURE_ARGS": str(capture_path),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )

    curl_args = capture_path.read_text(encoding="utf-8").splitlines()
    body = curl_args[curl_args.index("--data") + 1]
    assert json.loads(body) == {"status": "done", "message": message}
    assert "OK: MindRoom notified (quote-safe callback)" in completed.stdout


def test_mint_callback_uses_policy_default_ttl_when_omitted(tmp_path: Path) -> None:
    """The tool leaves an omitted TTL to callback_policy.default_ttl_seconds."""
    config = _config(callback_policy={"default_ttl_seconds": 120})
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path, config=config)):
        payload = _payload(tool.mint_callback("policy default"))

    assert payload["status"] == "ok"
    [record] = CallbackStore(_runtime_paths(tmp_path)).list_records()
    assert record.expires_at - record.created_at == 120


def test_mint_callback_rolls_back_record_and_script_when_store_link_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script-path persistence failure leaves no usable callback artifact behind."""

    def fail_set_script_path(*_args: object, **_kwargs: object) -> None:
        msg = "store write failed"
        raise CallbackStoreError(msg)

    monkeypatch.setattr(CallbackStore, "set_script_path", fail_set_script_path)
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(tool.mint_callback("rollback"))

    assert payload["status"] == "error"
    assert CallbackStore(_runtime_paths(tmp_path)).list_records() == []
    callbacks_dir = tmp_path / "mindroom_data" / "agents" / "coder" / "workspace" / ".mindroom" / "callbacks"
    assert list(callbacks_dir.glob("*.sh")) == []


def test_mint_callback_requires_enabled_policy(tmp_path: Path) -> None:
    """Disabled callback policy blocks minting with a clear error."""
    config = _config(callback_policy={"enabled": False})
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path, config=config)):
        payload = _payload(tool.mint_callback("disabled mint"))

    assert payload["status"] == "error"
    assert "disabled" in payload["message"]


def test_mint_callback_enforces_owner_quota(tmp_path: Path) -> None:
    """The policy per-owner quota is surfaced as a tool error."""
    config = _config(callback_policy={"max_active_per_owner": 1})
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path, config=config)):
        assert _payload(tool.mint_callback("first"))["status"] == "ok"
        payload = _payload(tool.mint_callback("second"))

    assert payload["status"] == "error"
    assert "quota" in payload["message"]


def test_mint_callback_rejects_bad_on_expiry(tmp_path: Path) -> None:
    """on_expiry accepts only the two documented modes."""
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(tool.mint_callback("bad mode", on_expiry="explode"))

    assert payload["status"] == "error"
    assert "on_expiry" in payload["message"]


def test_manager_requires_live_human_requester_context(tmp_path: Path) -> None:
    """Callback minting is available only to live human Matrix requesters."""
    tool = CallbackManagerTools()

    no_context_payload = _payload(tool.mint_callback("no context"))
    with tool_runtime_context(_context(tmp_path, requester_id=_Client.user_id)):
        bot_payload = _payload(tool.mint_callback("bot requester"))

    assert no_context_payload["status"] == "error"
    assert "live Matrix tool context" in no_context_payload["message"]
    assert bot_payload["status"] == "error"
    assert "human Matrix requester" in bot_payload["message"]


def test_list_callbacks_scopes_to_owner_unless_admin(tmp_path: Path) -> None:
    """Owners see their own callbacks; admins see every owner's."""
    config = _config(admin_users=["@admin:example.org"])
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        assert _payload(tool.mint_callback("mine"))["status"] == "ok"
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        assert _payload(tool.mint_callback("theirs"))["status"] == "ok"

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        owner_payload = _payload(tool.list_callbacks())
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_payload = _payload(tool.list_callbacks())

    assert [callback["label"] for callback in owner_payload["callbacks"]] == ["mine"]
    assert {callback["label"] for callback in admin_payload["callbacks"]} == {"mine", "theirs"}


def test_revoke_callback_deletes_record_and_script(tmp_path: Path) -> None:
    """Revocation removes the record and best-effort deletes the script file."""
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        minted = _payload(tool.mint_callback("to revoke"))
        assert minted["status"] == "ok"
        revoked = _payload(tool.revoke_callback(minted["callback_id"]))

    assert revoked["status"] == "ok"
    runtime_paths = _runtime_paths(tmp_path)
    store = CallbackStore(runtime_paths)
    assert store.list_records() == []
    assert not (
        tmp_path
        / "mindroom_data"
        / "agents"
        / "coder"
        / "workspace"
        / ".mindroom"
        / "callbacks"
        / f"{minted['callback_id']}.sh"
    ).exists()


def test_revoke_callback_rejects_non_owner(tmp_path: Path) -> None:
    """Only the owner or an admin can revoke a callback."""
    config = _config()
    tool = CallbackManagerTools()
    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        minted = _payload(tool.mint_callback("owned"))
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        payload = _payload(tool.revoke_callback(minted["callback_id"]))

    assert payload["status"] == "error"
    assert "owner or an external trigger admin" in payload["message"]
