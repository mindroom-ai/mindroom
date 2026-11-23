"""Tests for the kind quick-start Matrix chat check helper."""

from __future__ import annotations

from cluster.k8s.kind.chat_check import extract_latest_reply, is_terminal_reply

AGENT = "@mindroom_helper:mindroom.local"


def test_extract_latest_reply_follows_streaming_replace_edits() -> None:
    """Streaming placeholder and partial edits stay nonterminal until the final m.replace."""
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
    text, status = extract_latest_reply(events, AGENT, prompt_event_id="$prompt")
    assert (text, status) == ("po", "streaming")
    assert not is_terminal_reply(text, status)

    events.append(
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
    )
    text, status = extract_latest_reply(events, AGENT, prompt_event_id="$prompt")
    assert (text, status) == ("pong", "completed")
    assert is_terminal_reply(text, status)


def test_extract_latest_reply_prefers_prompt_thread_and_handles_compact_edits() -> None:
    """Prefer the reply to prompt_event_id over unrelated chatter and accept standalone m.replace edits."""
    events = [
        {
            "type": "m.room.message",
            "event_id": "$other_user",
            "sender": "@tester:mindroom.local",
            "content": {"body": "hi"},
        },
        {"type": "m.room.member", "event_id": "$join", "sender": AGENT, "content": {"membership": "join"}},
        {
            "type": "m.room.message",
            "event_id": "$matched",
            "sender": AGENT,
            "content": {
                "body": "pong",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$prompt"}},
            },
        },
        {
            "type": "m.room.message",
            "event_id": "$unrelated",
            "sender": AGENT,
            "content": {"body": "earlier notice"},
        },
    ]
    text, status = extract_latest_reply(events, AGENT, prompt_event_id="$prompt")
    assert (text, status) == ("pong", None)
    assert is_terminal_reply(text, status)

    compact_only = [
        {
            "type": "m.room.message",
            "event_id": "$final_edit",
            "sender": AGENT,
            "content": {
                "body": "* pong",
                "m.new_content": {"body": "pong", "io.mindroom.stream_status": "completed"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing_root"},
            },
        },
    ]
    assert extract_latest_reply(compact_only, AGENT, prompt_event_id="$prompt") == ("pong", "completed")
    assert not is_terminal_reply("", "completed")
    assert not is_terminal_reply("waiting", "approval_pending")
    assert is_terminal_reply("boom", "error")
