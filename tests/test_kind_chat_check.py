"""Tests for the kind quick-start Matrix chat check helper."""

from __future__ import annotations

import subprocess
import sys

import pytest

from cluster.k8s.kind.chat_check import extract_completed_reply, port_forward

AGENT = "@mindroom_helper:mindroom.local"


def test_extract_completed_reply_follows_streaming_replace_edits() -> None:
    """Streaming placeholder and partial edits return None until the final m.replace; m.notice is ignored."""
    events = [
        {
            "type": "m.room.message",
            "event_id": "$root",
            "sender": AGENT,
            "content": {
                "msgtype": "m.text",
                "body": "Thinking...",
                "io.mindroom.stream_status": "pending",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$prompt"},
            },
        },
        {
            "type": "m.room.message",
            "event_id": "$edit1",
            "sender": AGENT,
            "content": {
                "msgtype": "m.text",
                "body": "* po",
                "m.new_content": {"msgtype": "m.text", "body": "po", "io.mindroom.stream_status": "streaming"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$root"},
            },
        },
    ]
    assert extract_completed_reply(events, AGENT, "$prompt") is None

    events.extend(
        [
            {
                "type": "m.room.message",
                "event_id": "$edit2",
                "sender": AGENT,
                "content": {
                    "msgtype": "m.text",
                    "body": "* pong",
                    "m.new_content": {"msgtype": "m.text", "body": "pong", "io.mindroom.stream_status": "completed"},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$root"},
                },
            },
            {
                "type": "m.room.message",
                "event_id": "$summary",
                "sender": AGENT,
                "content": {
                    "msgtype": "m.notice",
                    "body": "🔁 Pong reply test",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$prompt"},
                },
            },
        ],
    )
    assert extract_completed_reply(events, AGENT, "$prompt") == "pong"


def test_extract_completed_reply_ignores_unrelated_and_raises_on_error() -> None:
    """Unrelated/nonterminal messages return None; error statuses raise immediately."""
    events = [
        {"type": "m.room.message", "event_id": "$other", "sender": "@tester:mindroom.local", "content": {"body": "hi"}},
        {"type": "m.room.member", "event_id": "$join", "sender": AGENT, "content": {"membership": "join"}},
        {"type": "m.room.message", "event_id": "$unrelated", "sender": AGENT, "content": {"body": "earlier notice"}},
        {
            "type": "m.room.message",
            "event_id": "$other_thread",
            "sender": AGENT,
            "content": {"body": "wrong thread", "m.relates_to": {"rel_type": "m.thread", "event_id": "$old_prompt"}},
        },
        {
            "type": "m.room.message",
            "event_id": "$orphan_edit",
            "sender": AGENT,
            "content": {
                "body": "* pong",
                "m.new_content": {"body": "pong", "io.mindroom.stream_status": "completed"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing_root"},
            },
        },
        {
            "type": "m.room.message",
            "event_id": "$waiting",
            "sender": AGENT,
            "content": {
                "body": "waiting",
                "io.mindroom.stream_status": "approval_pending",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$prompt"},
            },
        },
    ]
    assert extract_completed_reply(events, AGENT, "$prompt") is None

    events.append(
        {
            "type": "m.room.message",
            "event_id": "$err",
            "sender": AGENT,
            "content": {
                "body": "",
                "io.mindroom.stream_status": "error",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$prompt"},
            },
        },
    )
    with pytest.raises(RuntimeError, match="status error"):
        extract_completed_reply(events, AGENT, "$prompt")


def test_port_forward_parses_ephemeral_port_and_handles_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Port-forward reads the assigned port from stdout and reports exit status without leaking stderr."""
    real_popen = subprocess.Popen

    def spawn_script(code: str) -> None:
        def fake_popen(_cmd: list[str], **_kwargs: object) -> subprocess.Popen[str]:
            return real_popen(
                [sys.executable, "-c", code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_script("import time; print('Forwarding from 127.0.0.1:43210 -> 8008', flush=True); time.sleep(30)")
    with port_forward() as base:
        assert base == "http://127.0.0.1:43210"

    spawn_script("import sys; sys.stderr.write('secret-stderr\\n'); sys.exit(1)")
    with pytest.raises(RuntimeError, match=r"exit 1") as exc_info, port_forward():
        pass
    assert "secret-stderr" not in str(exc_info.value)
