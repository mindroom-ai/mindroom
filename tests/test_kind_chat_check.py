"""Tests for the kind quick-start Matrix chat check helper."""

from __future__ import annotations

import subprocess
import sys

import pytest

from cluster.k8s.kind.chat_check import extract_latest_reply, is_terminal_reply, port_forward

AGENT = "@mindroom_helper:mindroom.local"


def test_extract_latest_reply_follows_streaming_replace_edits() -> None:
    """Streaming placeholder and partial edits stay nonterminal until the final m.replace; m.notice is ignored."""
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
    text, status = extract_latest_reply(events, AGENT, "$prompt")
    assert (text, status) == ("po", "streaming")
    assert not is_terminal_reply(text, status)

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
    text, status = extract_latest_reply(events, AGENT, "$prompt")
    assert (text, status) == ("pong", "completed")
    assert is_terminal_reply(text, status)


def test_extract_latest_reply_ignores_unrelated_messages_and_orphan_edits() -> None:
    """Only thread replies rooted at prompt_id (and edits of them) match."""
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
    ]
    assert extract_latest_reply(events, AGENT, "$prompt") == (None, None)
    assert not is_terminal_reply("", "completed")
    assert not is_terminal_reply("waiting", "approval_pending")
    assert is_terminal_reply("boom", "error")


def test_port_forward_reports_stderr_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Port-forward surfaces kubectl stderr if the subprocess exits before becoming ready."""
    real_popen = subprocess.Popen

    def fake_popen(_cmd: list[str], **_kwargs: object) -> subprocess.Popen[str]:
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('svc not found\\n'); sys.exit(1)"]
        return real_popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="svc not found"), port_forward():
        pass
