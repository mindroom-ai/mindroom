"""Prove what a room actually shows after a streamed answer and a hard restart.

Two things about a streamed answer are invisible to a mocked client and to the
fuzz oracle, which reads ``m.new_content`` when an edit carries one:

- the **top-level fallback body**, which is what every client that does not
  understand ``m.replace`` renders, and
- the **count** of visible messages a turn leaves behind once the process is
  killed mid-turn and the frozen outbox row is re-sent verbatim.

A real homeserver deduplicates on transaction ID, so a verbatim re-send
collapses onto the same event; a fake one happily creates a second message.
Only a live server can tell those apart, which is why this lives here rather
than in the unit suite.

It reuses the live fuzzer's disposable stack, so run it from the project root
with that package importable::

    PYTHONPATH=. uv run python tests/manual/streamed_edit_live_proof.py

Every run uses a throwaway Tuwunel, throwaway accounts, and removes the stack
afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from scripts.testing.fuzz_live_matrix import (
    LiveMatrixClient,
    ManagedTuwunelStack,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class Findings:
    """Every observation this proof made, in the order it made them."""

    lines: list[tuple[bool, str]] = field(default_factory=list)

    def record(self, *, passed: bool, claim: str, detail: str = "") -> None:
        """Record one observation."""
        self.lines.append((passed, f"{claim}{f' — {detail}' if detail else ''}"))

    @property
    def failed(self) -> bool:
        """Return whether any observation failed."""
        return any(not passed for passed, _ in self.lines)

    def render(self) -> str:
        """Return one line per observation."""
        return "\n".join(f"[{'PASS' if passed else 'FAIL'}] {text}" for passed, text in self.lines)


def _revisions(events: Sequence[dict[str, Any]], response_event_id: str) -> list[dict[str, Any]]:
    """Return the original response and every edit that replaces it, oldest first."""
    revisions = [
        event
        for event in events
        if event.get("event_id") == response_event_id
        or (
            isinstance(event.get("content"), dict)
            and isinstance(event["content"].get("m.relates_to"), dict)
            and event["content"]["m.relates_to"].get("rel_type") == "m.replace"
            and event["content"]["m.relates_to"].get("event_id") == response_event_id
        )
    ]
    return sorted(revisions, key=lambda event: event.get("origin_server_ts") or 0)


def _agent_replies(events: Sequence[dict[str, Any]], agent_id: str, source_event_id: str) -> list[str]:
    """Return every distinct agent message that answers one source event."""
    replies = []
    for event in events:
        content = event.get("content")
        if event.get("sender") != agent_id or not isinstance(content, dict):
            continue
        relation = content.get("m.relates_to")
        if not isinstance(relation, dict) or relation.get("rel_type") == "m.replace":
            continue
        in_reply_to = relation.get("m.in_reply_to")
        replied_to = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
        if replied_to == source_event_id or relation.get("event_id") == source_event_id:
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                replies.append(event_id)
    return replies


async def _drain(client: LiveMatrixClient, *, seconds: float) -> None:
    """Sync until the room has been quiet for the given window."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await client.sync_incremental(timeout_ms=250, allow_limited=True)


async def _await_reply(
    client: LiveMatrixClient,
    agent_id: str,
    source_event_id: str,
    *,
    budget_seconds: float,
) -> str | None:
    """Wait for one agent reply to a source event and return its event ID."""
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        await client.sync_incremental(timeout_ms=500, allow_limited=True)
        replies = _agent_replies(client.seen_events.values(), agent_id, source_event_id)
        if replies:
            return replies[0]
    return None


async def prove_streamed_edit_fallback(
    stack: ManagedTuwunelStack,
    client: LiveMatrixClient,
    findings: Findings,
) -> None:
    """A streamed answer's final revision must carry a usable top-level body."""
    source = await client.send_event(
        "m.room.message",
        "streamed-edit-source",
        {
            "body": f"Live edit proof {stack.agent_id}",
            "m.mentions": {"user_ids": [stack.agent_id]},
            "msgtype": "m.text",
        },
    )
    response_event_id = await _await_reply(client, stack.agent_id, source, budget_seconds=120)
    findings.record(
        passed=response_event_id is not None,
        claim="a mentioned message produces one streamed answer",
        detail=str(response_event_id),
    )
    if response_event_id is None:
        return

    await _drain(client, seconds=5)
    revisions = _revisions(client.seen_events.values(), response_event_id)
    findings.record(
        passed=len(revisions) >= 1,
        claim="the answer is readable as an original plus its edits",
        detail=f"{len(revisions)} revision(s)",
    )

    final = revisions[-1]
    content = final.get("content", {})
    top_level_body = content.get("body")
    new_content = content.get("m.new_content")
    rendered_body = new_content.get("body") if isinstance(new_content, dict) else top_level_body

    findings.record(
        passed=isinstance(top_level_body, str) and bool(top_level_body.strip()),
        claim="the final revision carries a non-empty top-level fallback body",
        detail=repr(top_level_body)[:120],
    )
    findings.record(
        passed=isinstance(rendered_body, str) and "END call=" in rendered_body,
        claim="the rendered body is the complete answer",
        detail=repr(rendered_body)[:120],
    )
    if isinstance(top_level_body, str) and isinstance(rendered_body, str) and final is not revisions[0]:
        # An edit's fallback is conventionally the rendered body with a marker
        # prefix, so the answer must be recoverable from the fallback alone.
        findings.record(
            passed="END call=" in top_level_body,
            claim="a client that ignores m.replace still sees the complete answer",
            detail=repr(top_level_body)[:160],
        )

    replies = _agent_replies(client.seen_events.values(), stack.agent_id, source)
    findings.record(
        passed=len(replies) == 1,
        claim="a streamed answer leaves exactly one visible message",
        detail=f"{len(replies)} message(s): {replies}",
    )


async def prove_restart_delivery_count(
    stack: ManagedTuwunelStack,
    client: LiveMatrixClient,
    findings: Findings,
) -> None:
    """Killing the process mid-turn must not change the room's message count."""
    source = await client.send_event(
        "m.room.message",
        "streamed-edit-restart-source",
        {
            "body": f"Live edit proof restart {stack.agent_id}",
            "m.mentions": {"user_ids": [stack.agent_id]},
            "msgtype": "m.text",
        },
    )
    # Give the turn a moment to reach the model, then kill without draining.
    await _drain(client, seconds=2)
    stack.restart_mindroom_for_recovery(timeout=90)

    response_event_id = await _await_reply(client, stack.agent_id, source, budget_seconds=180)
    findings.record(
        passed=response_event_id is not None,
        claim="a turn interrupted by a hard kill still answers after recovery",
        detail=str(response_event_id),
    )
    await _drain(client, seconds=10)
    replies = _agent_replies(client.seen_events.values(), stack.agent_id, source)
    findings.record(
        passed=len(replies) == 1,
        claim="recovery re-sends the frozen row without adding a second message",
        detail=f"{len(replies)} message(s): {replies}",
    )


async def run_proof(stack: ManagedTuwunelStack) -> Findings:
    """Run every observation against one disposable stack."""
    findings = Findings()
    client = LiveMatrixClient(stack.homeserver, stack.room_id)
    try:
        await client.register()
        await client.join_room()
        await client.sync_incremental(timeout_ms=0, allow_limited=True)
        await prove_streamed_edit_fallback(stack, client, findings)
        await prove_restart_delivery_count(stack, client, findings)
    finally:
        await client.close()
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Boot a disposable stack, run every observation, and report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failure-log", type=str, default=None)
    args = parser.parse_args(argv)

    stack = ManagedTuwunelStack(model_latch_timeout=180)
    try:
        stack.start()
        print(f"Tuwunel ready at {stack.homeserver}, room {stack.room_id}", flush=True)
        findings = asyncio.run(run_proof(stack))
        print(findings.render(), flush=True)
        if findings.failed and args.failure_log is not None:
            from pathlib import Path  # noqa: PLC0415 - only needed on the failure path

            Path(args.failure_log).write_text(stack.read_log(), encoding="utf-8")
        return 1 if findings.failed else 0
    finally:
        stack.close()


if __name__ == "__main__":
    sys.exit(main())
