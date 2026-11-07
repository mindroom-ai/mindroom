"""The policy every message of an agent passes before it reaches the other side of a contact.

The conversation limit comes first, then the plugin hooks on ``contact:before_send``, then the judge when the agent has one.
The limit is checked again last, because other messages can reach it while the hooks and the judge run.
Everything fails closed: a message that cannot be checked is never sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom.contacts.parties import party_name, party_reference
from mindroom.hooks import EVENT_CONTACT_BEFORE_SEND, ContactBeforeSendContext, emit_policy
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.matrix.conversation_reads import consecutive_agent_messages

if TYPE_CHECKING:
    from mindroom.config.judgment import JudgmentConfig
    from mindroom.config.main import Config
    from mindroom.contacts.store import ContactRecord, ContactSide, SideName
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_CONTACT_RULES_QUESTION_ID = "contact_rules"

NO_CONTACT_RULES = "No extra rules; share only what the owner would expect."
_HOOK_GAVE_NO_REASON = "A contact policy hook stopped this message without saying why."
_HOOK_LEFT_IT_EMPTY = "A contact policy hook left the message empty."
_UNCHECKED = "The message could not be checked; try again later."
_BULLET = re.compile(r"^[-*]\s+")


@dataclass(frozen=True)
class ContactVerdict:
    """What to do with one message; ``text`` is what to send, or the agent's own text when nothing is sent."""

    action: Literal["send", "revise", "deny"]
    text: str
    feedback: str = ""
    rewritten: bool = False


def effective_rules(config: Config, agent_name: str, side: ContactSide) -> str:
    """Return the agent's configured rules followed by its owner's rules for this contact, one bullet per rule.

    Each nonblank line of either is one rule, with its own bullet removed.
    """
    contacts = config.agents[agent_name].contacts
    texts = [*(contacts.rules if contacts is not None else ()), side.rules or ""]
    rules = [rule for text in texts for line in text.splitlines() if (rule := _BULLET.sub("", line.strip()))]
    return "\n".join(f"- {rule}" for rule in rules)


def _contact_rules_question(config: Config, record: ContactRecord, side: SideName) -> JudgmentQuestion:
    """Return the question the judge answers about each message of ``side``'s agent: does it follow every rule.

    Owners write rules about themselves as "I" and "my", while their agent writes about them in the third person, so
    the judge learns who writes the message, for whom, and to whom.
    The question is the rubric the judge trusts, so it names the other side by ID only: that side chooses its agent's
    display name, which could otherwise rewrite the rubric.
    """
    sender = record.party(side)
    owner = sender.owner
    recipient = party_reference(record.counterparty(side))
    return JudgmentQuestion(
        id=_CONTACT_RULES_QUESTION_ID,
        instructions=(
            f"{party_name(config, sender)} ({sender.id}) is an assistant acting for its owner {owner}. "
            f"It wants to send the message below, written on behalf of {owner}, to {recipient}. "
            f"In the rules, I, me, and my mean {owner}, and the message may name {owner} in the third person. "
            "Decide whether the message follows every rule."
        ),
        when_true="The message follows every rule.",
        when_false="The message breaks or may break a rule.",
    )


async def check_contact_message(
    context: ToolRuntimeContext,
    record: ContactRecord,
    side: SideName,
    *,
    room_id: str,
    thread_id: str | None,
    text: str,
    adds_message: bool,
) -> ContactVerdict:
    """Decide whether the agent on ``side`` of ``record`` may send ``text`` to the other side.

    Only a message that ``adds_message`` to the conversation, unlike an edit or a reaction, counts toward the limit.
    """
    config = context.current_config
    sender = record.party(side)
    if adds_message and (denied := await _limit_refusal(context, room_id=room_id, thread_id=thread_id, text=text)):
        return denied
    rules = effective_rules(config, sender.id, record.side(side))
    final_text = text
    if context.hooks.registry.has_hooks(EVENT_CONTACT_BEFORE_SEND):
        hooked = await _run_hooks(
            context,
            record,
            side,
            rules=rules,
            room_id=room_id,
            thread_id=thread_id,
            text=text,
        )
        if hooked.verdict != "send":
            action = "revise" if hooked.verdict == "revise" else "deny"
            return ContactVerdict(action, text, hooked.feedback or _HOOK_GAVE_NO_REASON)
        final_text = hooked.text
        if not final_text.strip():
            return ContactVerdict("deny", text, _HOOK_LEFT_IT_EMPTY)
    contacts = config.agents[sender.id].contacts
    if contacts is not None and contacts.judgment is not None:
        refusal = await _judge_refusal(
            context,
            record,
            side,
            contacts.judgment,
            rules=rules or NO_CONTACT_RULES,
            text=final_text,
        )
        if refusal is not None:
            return ContactVerdict(refusal[0], text, refusal[1])
    # The hooks and the judge can take seconds, in which other messages can fill the conversation up to the limit.
    if adds_message and (denied := await _limit_refusal(context, room_id=room_id, thread_id=thread_id, text=text)):
        return denied
    return ContactVerdict("send", final_text, rewritten=final_text != text)


async def _limit_refusal(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    text: str,
) -> ContactVerdict | None:
    """Return the denial of a message once the conversation ends with the limit of agent messages, or None."""
    config = context.current_config
    limit = config.defaults.max_consecutive_agent_replies
    consecutive = await consecutive_agent_messages(
        context.conversation_reader,
        config,
        context.runtime_paths,
        room_id=room_id,
        thread_id=thread_id,
        limit=limit,
    )
    if consecutive < limit:
        return None
    return ContactVerdict(
        "deny",
        text,
        f"The conversation already has {consecutive} agent messages in a row; wait for a person to write.",
    )


async def _run_hooks(
    context: ToolRuntimeContext,
    record: ContactRecord,
    side: SideName,
    *,
    rules: str,
    room_id: str,
    thread_id: str | None,
    text: str,
) -> ContactBeforeSendContext:
    """Run the ``contact:before_send`` hooks and return their context with the text and verdict they left."""
    config = context.current_config
    counterparty = record.counterparty(side)
    hook_context = ContactBeforeSendContext(
        **context.hooks.context_kwargs(
            EVENT_CONTACT_BEFORE_SEND,
            config=config,
            runtime_paths=context.runtime_paths,
            correlation_id=context.correlation_id or uuid4().hex,
        ),
        turn_id=context.turn_id,
        parent_turn_id=context.parent_turn_id,
        agent_name=record.party(side).id,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=context.requester_id,
        contact_id=record.id,
        counterparty_id=counterparty.id,
        counterparty_name=party_name(config, counterparty),
        rules=rules,
        text=text,
        message_received_depth=context.message_received_depth,
    )
    await emit_policy(context.hooks.registry, EVENT_CONTACT_BEFORE_SEND, hook_context)
    return hook_context


async def _judge_refusal(
    context: ToolRuntimeContext,
    record: ContactRecord,
    side: SideName,
    judge: JudgmentConfig,
    *,
    rules: str,
    text: str,
) -> tuple[Literal["revise", "deny"], str] | None:
    """Return why the judge keeps ``text`` of ``side``'s agent from being sent, or None when it follows the rules."""
    config, runtime_paths = context.current_config, context.runtime_paths
    sender = record.party(side)
    question = _contact_rules_question(config, record, side)
    request = build_judgment_request(question, (JudgmentMessage("user", text),), instructions=rules)
    if not request.complete:
        return "deny", f"The message cannot be checked ({request.incomplete_reason})."
    evaluate = create_judgment_evaluator(
        judge,
        config,
        runtime_paths,
        owner=f"{runtime_paths.storage_root}:{sender.id}",
        question_id=_CONTACT_RULES_QUESTION_ID,
    )
    if evaluate is None:
        return "deny", _UNCHECKED
    result = await evaluate(request)
    if result.failure is not None or result.decision is None:
        return "deny", _UNCHECKED
    if result.decision:
        return None
    return (
        "revise",
        f"This message may break {sender.owner}'s rules for this contact:\n{rules}\nRewrite it so it follows them.",
    )
