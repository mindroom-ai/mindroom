"""Tests for the contact:before_send policy hook event."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_CONTACT_BEFORE_SEND,
    ContactBeforeSendContext,
    HookRegistry,
    emit_policy,
    hook,
)
from mindroom.hooks.execution import _EMIT_DEPTH, _MAX_EMIT_DEPTH
from mindroom.hooks.types import default_timeout_ms_for_event, validate_event_name
from tests.conftest import runtime_paths_for
from tests.test_tool_hooks import _config, _plugin

if TYPE_CHECKING:
    from pathlib import Path


def _contact_context(
    tmp_path: Path,
    *,
    agent_name: str = "code",
    room_id: str = "!room:localhost",
    text: str = "Hello Bob",
    message_received_depth: int = 0,
) -> ContactBeforeSendContext:
    config = _config(tmp_path)
    return ContactBeforeSendContext(
        agent_name=agent_name,
        room_id=room_id,
        thread_id="$thread",
        requester_id="@alice:localhost",
        contact_id="contact-1",
        counterparty_id="@bob:localhost",
        counterparty_name="Bob",
        rules="Share nothing about the budget.",
        text=text,
        message_received_depth=message_received_depth,
        config=config,
        runtime_paths=runtime_paths_for(config),
        correlation_id="corr-contact",
    )


def test_contact_event_is_registered_with_10s_timeout() -> None:
    """The contact event is a built-in with a 10 second default, and plugins cannot add contact events."""
    assert EVENT_CONTACT_BEFORE_SEND == "contact:before_send"
    assert EVENT_CONTACT_BEFORE_SEND in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_CONTACT_BEFORE_SEND) == EVENT_CONTACT_BEFORE_SEND
    assert default_timeout_ms_for_event(EVENT_CONTACT_BEFORE_SEND) == 10000
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("contact:other")


def test_contact_context_verdict_helpers(tmp_path: Path) -> None:
    """The context starts as a send and its helpers record a rewrite, a revise, or a deny."""
    context = _contact_context(tmp_path)
    assert context.event_name == EVENT_CONTACT_BEFORE_SEND
    assert context.verdict == "send"
    assert context.feedback == ""

    context.rewrite("Hello")
    assert (context.text, context.verdict) == ("Hello", "send")

    context.revise("Leave out the budget.")
    assert (context.verdict, context.feedback) == ("revise", "Leave out the budget.")

    context.deny("Not allowed.")
    assert (context.verdict, context.feedback) == ("deny", "Not allowed.")


@pytest.mark.asyncio
async def test_emit_policy_without_hooks_sends_the_text_unchanged(tmp_path: Path) -> None:
    """With no hook registered the verdict stays send and the text stays as written."""
    context = _contact_context(tmp_path)

    await emit_policy(HookRegistry.empty(), EVENT_CONTACT_BEFORE_SEND, context)

    assert (context.text, context.verdict, context.feedback) == ("Hello Bob", "send", "")


@pytest.mark.asyncio
async def test_emit_policy_rewrites_then_next_hook_sees_new_text(tmp_path: Path) -> None:
    """A rewrite reaches the next hook in priority order and the caller's context."""
    seen: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=20)
    async def second(ctx: ContactBeforeSendContext) -> None:
        seen.append(ctx.text)
        ctx.rewrite(f"{ctx.text}!")

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=10)
    async def first(ctx: ContactBeforeSendContext) -> None:
        seen.append(ctx.text)
        ctx.rewrite("Hi Bob")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [second, first])])
    context = _contact_context(tmp_path)

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert seen == ["Hello Bob", "Hi Bob"]
    assert context.text == "Hi Bob!"
    assert context.verdict == "send"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "feedback"),
    [("revise", "Say less."), ("deny", "Blocked.")],
)
async def test_emit_policy_stops_at_first_revise_or_deny(tmp_path: Path, verdict: str, feedback: str) -> None:
    """The first non-send verdict ends the chain, and its text and feedback reach the caller."""
    seen: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=10)
    async def rewriter(ctx: ContactBeforeSendContext) -> None:
        seen.append("rewriter")
        ctx.rewrite("Rewritten")

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=20)
    async def stopper(ctx: ContactBeforeSendContext) -> None:
        seen.append("stopper")
        if verdict == "revise":
            ctx.revise(feedback)
        else:
            ctx.deny(feedback)

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=30)
    async def later(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("later")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [later, stopper, rewriter])])
    context = _contact_context(tmp_path)

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert seen == ["rewriter", "stopper"]
    assert context.text == "Rewritten"
    assert context.verdict == verdict
    assert context.feedback == feedback


@pytest.mark.asyncio
async def test_emit_policy_fails_closed_on_error_and_timeout(tmp_path: Path) -> None:
    """A hook that raises or times out denies the send and stops the chain, dropping its own changes."""
    seen: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=10)
    async def broken(ctx: ContactBeforeSendContext) -> None:
        seen.append("broken")
        ctx.rewrite("Half-finished")
        msg = "boom"
        raise RuntimeError(msg)

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=20)
    async def later(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("later")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [broken, later])])
    context = _contact_context(tmp_path)

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert seen == ["broken"]
    assert context.verdict == "deny"
    assert context.feedback == "A contact policy hook failed."
    assert context.text == "Hello Bob"

    @hook(EVENT_CONTACT_BEFORE_SEND, priority=10, timeout_ms=10)
    async def slow(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("slow")
        await asyncio.sleep(0.05)

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [slow, later])])
    seen.clear()
    context = _contact_context(tmp_path)

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert seen == ["slow"]
    assert (context.verdict, context.feedback) == ("deny", "A contact policy hook failed.")


@pytest.mark.asyncio
async def test_emit_policy_denies_when_hooks_nest_too_deeply(tmp_path: Path) -> None:
    """A policy run started from inside nested hook emissions denies instead of skipping the policy."""
    seen: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def never_runs(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("never")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [never_runs])])
    context = _contact_context(tmp_path)
    token = _EMIT_DEPTH.set(_MAX_EMIT_DEPTH)
    try:
        await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)
    finally:
        _EMIT_DEPTH.reset(token)

    assert seen == []
    assert context.verdict == "deny"
    assert context.feedback == "Contact policy hooks nested too deeply."


@pytest.mark.asyncio
async def test_emit_policy_respects_agents_and_rooms_scope(tmp_path: Path) -> None:
    """Hooks scoped to other agents or rooms are skipped, and in-scope hooks run."""
    seen: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND, name="other-agent", priority=5, agents=["research"])
    async def other_agent(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("other-agent")

    @hook(EVENT_CONTACT_BEFORE_SEND, name="other-room", priority=6, rooms=["!elsewhere:localhost"])
    async def other_room(ctx: ContactBeforeSendContext) -> None:
        del ctx
        seen.append("other-room")

    @hook(EVENT_CONTACT_BEFORE_SEND, name="in-scope", priority=10, agents=["code"], rooms=["!room:localhost"])
    async def in_scope(ctx: ContactBeforeSendContext) -> None:
        seen.append("in-scope")
        ctx.deny("Scoped policy blocked it.")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [other_agent, other_room, in_scope])])
    context = _contact_context(tmp_path)

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert seen == ["in-scope"]
    assert (context.verdict, context.feedback) == ("deny", "Scoped policy blocked it.")


@pytest.mark.asyncio
async def test_contact_hook_sends_carry_the_requester(tmp_path: Path) -> None:
    """A hook that sends a message from this event preserves the context's requester as the original sender."""
    sent: list[tuple[str, dict[str, object] | None]] = []

    async def message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str:
        del room_id, body, thread_id, trigger_dispatch
        sent.append((source_hook, extra_content))
        return "$event"

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def notify(ctx: ContactBeforeSendContext) -> None:
        await ctx.send_message("!room:localhost", "FYI")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [notify])])
    context = _contact_context(tmp_path)
    context.message_sender = message_sender

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert len(sent) == 1
    source_hook, extra_content = sent[0]
    assert source_hook == "contact-policy:contact:before_send"
    assert extra_content is not None
    assert extra_content[ORIGINAL_SENDER_KEY] == "@alice:localhost"


@pytest.mark.asyncio
async def test_contact_hook_sends_continue_the_inbound_hook_chain_depth(tmp_path: Path) -> None:
    """A hook send from a turn that a hook message started is one hop deeper than that message."""
    sent: list[dict[str, object] | None] = []

    async def message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str:
        del room_id, body, thread_id, source_hook, trigger_dispatch
        sent.append(extra_content)
        return "$event"

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def notify(ctx: ContactBeforeSendContext) -> None:
        await ctx.send_message("!room:localhost", "FYI")

    registry = HookRegistry.from_plugins([_plugin("contact-policy", [notify])])
    context = _contact_context(tmp_path, message_received_depth=2)
    context.message_sender = message_sender

    await emit_policy(registry, EVENT_CONTACT_BEFORE_SEND, context)

    assert [content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] for content in sent if content is not None] == [3]
