"""Tests for the contact message policy: the conversation limit, the hooks, and the judge."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from mindroom.config.main import Config
from mindroom.contacts.policy import (
    _CONTACT_RULES_QUESTION_ID,
    ContactVerdict,
    _contact_rules_question,
    check_contact_message,
    effective_rules,
)
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide
from mindroom.hooks import EVENT_CONTACT_BEFORE_SEND, ContactBeforeSendContext, HookBindings, HookRegistry, hook
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    install_model_providers,
    make_conversation_reader_mock,
    make_relation_lookup,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

ALICE = "@alice:localhost"
BOB = "@bob:localhost"
ROOM = "!contact:localhost"
RULES = "- Never share the budget.\n- Do not name Carol."
NO_RULES = "No extra rules; share only what the owner would expect."
LLM_JUDGE = {"provider": "llm", "model": "cheap"}
LEAKED_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


def _config(
    tmp_path: Path,
    *,
    rules: list[str] | None = None,
    judge: dict[str, Any] | None = None,
    process_env: dict[str, str] | None = None,
) -> tuple[Config, RuntimePaths]:
    """Alice's agent is bound by one config rule and Bob's agent by none; only Alice's agent may carry a judge."""
    contacts: dict[str, Any] = {"rules": ["Never share the budget."] if rules is None else rules}
    if judge is not None:
        contacts["judge"] = judge
    paths = test_runtime_paths(tmp_path)
    paths = replace(paths, process_env={**paths.process_env, **(process_env or {})})
    config = Config.validate_with_runtime(
        {
            "models": {
                "default": {"provider": "openai", "id": "m"},
                "cheap": {"provider": "openai", "id": "cheap-model"},
            },
            "agents": {
                "mind_alice": {"display_name": "Alice's Mind", "contacts": contacts},
                "mind_bob": {"display_name": "Bob's Mind", "contacts": {}},
            },
            "defaults": {"max_consecutive_agent_replies": 3},
        },
        paths,
    )
    return config, paths


def _record(**kwargs: object) -> ContactRecord:
    fields: dict[str, object] = {
        "id": "contact-1",
        "status": "active",
        "requester": ContactParty(kind="agent", id="mind_alice", owner=ALICE),
        "target": ContactParty(kind="agent", id="mind_bob", owner=BOB),
        "reason": "Plan a dinner",
        "requester_side": ContactSide(rules="Do not name Carol."),
        "target_side": ContactSide(),
        "origin_room_id": "!alice:localhost",
        "origin_thread_id": "$thread",
        "room_id": ROOM,
        "created_at": 1.0,
    }
    return ContactRecord.model_validate(fields | kwargs)


def _tool_context(
    config: Config,
    paths: RuntimePaths,
    *callbacks: object,
    **fields: object,
) -> ToolRuntimeContext:
    """Alice's agent in its turn, with the hooks of one plugin when callbacks are given."""
    bindings = HookBindings(registry=HookRegistry.from_plugins([_plugin("contact-policy", list(callbacks))]))
    return make_test_tool_runtime_context(
        agent_name="mind_alice",
        target=MessageTarget.resolve(room_id=ROOM, thread_id="$thread", reply_to_event_id="$request"),
        requester_id=ALICE,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=bindings if callbacks else HookBindings(),
        **fields,
    )


async def _check(
    context: ToolRuntimeContext,
    *,
    record: ContactRecord | None = None,
    text: str = "Dinner at eight?",
    consecutive_agent_messages: int = 0,
) -> ContactVerdict:
    return await check_contact_message(
        context,
        record or _record(),
        "requester",
        room_id=ROOM,
        thread_id="$thread",
        text=text,
        consecutive_agent_messages=consecutive_agent_messages,
    )


def _judge_model(monkeypatch: pytest.MonkeyPatch, reply: str) -> ScriptedModel:
    model = ScriptedModel([answer(reply)])
    install_model_providers(monkeypatch, {"cheap": model})
    return model


def _judged(model: ScriptedModel) -> dict[str, Any]:
    """Return the request the judge model received: its question, its guidance, and the one message it judged."""
    (request,) = model.requests
    body = json.loads(request.messages[1].content)
    assert body["question"]["id"] == _CONTACT_RULES_QUESTION_ID
    (message,) = body["state"]["conversation"]
    return {"guidance": body["guidance"], "role": message["role"], "text": message["text"]}


def test_effective_rules_puts_config_rules_first(tmp_path: Path) -> None:
    """The agent's configured rules come first, then the lines its owner set, as one bulleted list."""
    config, _ = _config(tmp_path, rules=["Never share the budget.", "Be brief."])

    assert (
        effective_rules(config, "mind_alice", ContactSide(rules="- Do not name Carol.\n\n  No prices  \n"))
        == "- Never share the budget.\n- Be brief.\n- Do not name Carol.\n- No prices"
    )
    assert effective_rules(config, "mind_alice", ContactSide()) == "- Never share the budget.\n- Be brief."


def test_effective_rules_normalize_config_rules_like_owner_rules(tmp_path: Path) -> None:
    """A configured rule loses its own bullet and surrounding space, and a blank configured rule is left out."""
    config, _ = _config(tmp_path, rules=["- Be brief.", "  ", "* No prices  ", "First.\n- Second."])

    assert effective_rules(config, "mind_alice", ContactSide()) == "- Be brief.\n- No prices\n- First.\n- Second."


def test_effective_rules_are_empty_without_any_rule(tmp_path: Path) -> None:
    """No configured rule and no rule of the owner give an empty string, not an empty bullet."""
    config, _ = _config(tmp_path, rules=[])

    assert effective_rules(config, "mind_alice", ContactSide()) == ""
    assert effective_rules(config, "mind_alice", ContactSide(rules="  \n")) == ""


def test_contact_rules_question_asks_whether_the_message_follows_every_rule() -> None:
    """The judge answers true only for a message that follows every rule."""
    question = _contact_rules_question()

    assert question.id == _CONTACT_RULES_QUESTION_ID == "contact_rules"
    assert question.instructions == (
        "An assistant acting for its owner wants to send the message below to an outside party. "
        "Decide whether the message follows every rule."
    )
    assert question.when_true == "The message follows every rule."
    assert question.when_false == "The message breaks or may break a rule."


@pytest.mark.asyncio
async def test_consecutive_limit_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit denies before any hook or judge runs, and one message less still sends."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')
    ran: list[str] = []

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def watcher(ctx: ContactBeforeSendContext) -> None:
        ran.append(ctx.text)

    context = _tool_context(config, paths, watcher)

    verdict = await _check(context, consecutive_agent_messages=3)

    assert verdict == ContactVerdict(
        "deny",
        "Dinner at eight?",
        "The conversation already has 3 agent messages in a row; wait for a person to write.",
    )
    assert ran == []
    assert model.requests == []

    assert (await _check(context, consecutive_agent_messages=2)).action == "send"
    assert ran == ["Dinner at eight?"]


@pytest.mark.asyncio
async def test_no_hooks_no_judge_sends(tmp_path: Path) -> None:
    """Without hooks or a judge the text goes out as written."""
    config, paths = _config(tmp_path)

    verdict = await _check(_tool_context(config, paths))

    assert verdict == ContactVerdict("send", "Dinner at eight?")
    assert verdict.feedback == ""
    assert verdict.rewritten is False


@pytest.mark.asyncio
async def test_hook_that_leaves_the_text_alone_sends_it_unrewritten(tmp_path: Path) -> None:
    """A hook that only looks at the message does not mark it as rewritten."""
    config, paths = _config(tmp_path)

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def looker(ctx: ContactBeforeSendContext) -> None:
        del ctx

    assert await _check(_tool_context(config, paths, looker)) == ContactVerdict("send", "Dinner at eight?")


@pytest.mark.asyncio
async def test_hook_rewrite_is_judged_and_sent_with_rewritten_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge sees the rewritten text and not the original, and the verdict sends the rewrite and says so."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def shorten(ctx: ContactBeforeSendContext) -> None:
        ctx.rewrite("Dinner?")

    verdict = await _check(_tool_context(config, paths, shorten), text="Dinner at eight, budget 500?")

    assert verdict == ContactVerdict("send", "Dinner?", rewritten=True)
    assert _judged(model) == {"guidance": RULES, "role": "user", "text": "Dinner?"}


@pytest.mark.asyncio
async def test_hook_that_empties_the_text_denies_before_the_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite to blank text leaves nothing to send, so it denies with the agent's own text and skips the judge."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def blank(ctx: ContactBeforeSendContext) -> None:
        ctx.rewrite("  \n ")

    verdict = await _check(_tool_context(config, paths, blank))

    assert verdict == ContactVerdict("deny", "Dinner at eight?", "A contact policy hook left the message empty.")
    assert model.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "feedback"),
    [("revise", "Leave out the budget."), ("deny", "Not allowed.")],
)
async def test_hook_revise_or_deny_stops_before_the_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    feedback: str,
) -> None:
    """A hook's revise or deny reaches the agent with the text it wrote, and the judge never runs."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def stop(ctx: ContactBeforeSendContext) -> None:
        ctx.rewrite("Rewritten")
        if action == "revise":
            ctx.revise(feedback)
        else:
            ctx.deny(feedback)

    verdict = await _check(_tool_context(config, paths, stop))

    assert verdict == ContactVerdict(action, "Dinner at eight?", feedback)  # type: ignore[arg-type]
    assert model.requests == []


@pytest.mark.asyncio
async def test_hook_failure_denies(tmp_path: Path) -> None:
    """A hook that raises blocks the message instead of letting it through."""
    config, paths = _config(tmp_path)

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def broken(ctx: ContactBeforeSendContext) -> None:
        del ctx
        msg = "boom"
        raise RuntimeError(msg)

    verdict = await _check(_tool_context(config, paths, broken))

    assert verdict == ContactVerdict("deny", "Dinner at eight?", "A contact policy hook failed.")


@pytest.mark.asyncio
@pytest.mark.parametrize("assigned", ["maybe", "revise"])
async def test_hook_verdict_assigned_directly_is_not_sent(tmp_path: Path, assigned: str) -> None:
    """A verdict outside send, revise, and deny denies, and a revise without feedback still tells the agent why."""
    config, paths = _config(tmp_path)

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def assign(ctx: ContactBeforeSendContext) -> None:
        ctx.verdict = assigned  # type: ignore[assignment]

    verdict = await _check(_tool_context(config, paths, assign))

    assert verdict.action == ("revise" if assigned == "revise" else "deny")
    assert verdict.text == "Dinner at eight?"
    assert verdict.feedback == "A contact policy hook stopped this message without saying why."


@pytest.mark.asyncio
async def test_hook_context_describes_the_contact(tmp_path: Path) -> None:
    """Hooks get the sender, the room, the other side by its display name, the rules, and the hook chain depth."""
    config, paths = _config(tmp_path)
    seen: list[ContactBeforeSendContext] = []

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def watcher(ctx: ContactBeforeSendContext) -> None:
        seen.append(ctx)

    context = _tool_context(config, paths, watcher, correlation_id="corr-1", message_received_depth=2)

    await _check(context)

    (ctx,) = seen
    assert (ctx.agent_name, ctx.room_id, ctx.thread_id, ctx.requester_id) == ("mind_alice", ROOM, "$thread", ALICE)
    assert (ctx.contact_id, ctx.counterparty_id, ctx.counterparty_name) == ("contact-1", "mind_bob", "Bob's Mind")
    assert (ctx.rules, ctx.text, ctx.message_received_depth) == (RULES, "Dinner at eight?", 2)
    assert (ctx.correlation_id, ctx.config, ctx.runtime_paths) == ("corr-1", config, paths)


@pytest.mark.asyncio
async def test_hook_context_names_a_person_by_matrix_id_and_makes_a_correlation_id(tmp_path: Path) -> None:
    """A person has no display name in the config, and a turn without a correlation ID still gets one."""
    config, paths = _config(tmp_path)
    seen: list[ContactBeforeSendContext] = []

    @hook(EVENT_CONTACT_BEFORE_SEND)
    async def watcher(ctx: ContactBeforeSendContext) -> None:
        seen.append(ctx)

    record = _record(target=ContactParty(kind="person", id=BOB, owner=BOB))

    await _check(_tool_context(config, paths, watcher), record=record)

    (ctx,) = seen
    assert (ctx.counterparty_id, ctx.counterparty_name) == (BOB, BOB)
    assert ctx.correlation_id


@pytest.mark.asyncio
async def test_judge_true_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A text that follows the rules goes out as written, judged with the rules and nothing else."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    verdict = await _check(_tool_context(config, paths))

    assert verdict == ContactVerdict("send", "Dinner at eight?")
    assert _judged(model) == {"guidance": RULES, "role": "user", "text": "Dinner at eight?"}


@pytest.mark.asyncio
async def test_judge_false_revises_with_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A text that may break a rule goes back to the agent with its owner's name and the rules."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    _judge_model(monkeypatch, '{"decision": false}')

    verdict = await _check(_tool_context(config, paths), text="The budget is 500.")

    assert verdict == ContactVerdict(
        "revise",
        "The budget is 500.",
        f"This message may break {ALICE}'s rules for this contact:\n{RULES}\nRewrite it so it follows them.",
    )


@pytest.mark.asyncio
async def test_judge_without_rules_asks_that_the_owner_would_expect_the_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no rule the judge still runs, and its revise names the default expectation instead of an empty list."""
    config, paths = _config(tmp_path, rules=[], judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": false}')

    verdict = await _check(_tool_context(config, paths), record=_record(requester_side=ContactSide()))

    assert _judged(model)["guidance"] == NO_RULES
    assert verdict.action == "revise"
    assert verdict.feedback == (
        f"This message may break {ALICE}'s rules for this contact:\n{NO_RULES}\nRewrite it so it follows them."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ['{"decision": null}', "not json at all"])
async def test_judge_failure_or_abstain_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    """A judge that abstains or whose answer cannot be read blocks the message."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    _judge_model(monkeypatch, reply)

    verdict = await _check(_tool_context(config, paths))

    assert verdict == ContactVerdict("deny", "Dinner at eight?", "The message could not be checked; try again later.")


@pytest.mark.asyncio
async def test_judge_backend_without_credentials_denies(tmp_path: Path) -> None:
    """A probability backend that has no key cannot be bound, which blocks the message."""
    config, paths = _config(tmp_path, judge={"provider": "typesafe"})

    verdict = await _check(_tool_context(config, paths))

    assert verdict == ContactVerdict("deny", "Dinner at eight?", "The message could not be checked; try again later.")


@pytest.mark.asyncio
@pytest.mark.parametrize(("probability", "action"), [(0.85, "send"), (0.79, "revise")])
async def test_probability_judge_threshold_is_applied_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probability: float,
    action: str,
) -> None:
    """A probability backend's own threshold decides, so a probability above it sends and one below it revises."""
    config, paths = _config(
        tmp_path,
        judge={"provider": "typesafe", "threshold": 0.8},
        process_env={"TYPESAFE_API_KEY": "key"},
    )

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        assert json.loads(body)["questions"][_CONTACT_RULES_QUESTION_ID]["type"] == "noul"
        return json.dumps(
            {
                "model": _PINNED_MODEL,
                "answers": {_CONTACT_RULES_QUESTION_ID: {"type": "noul", "noul": probability}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ).encode()

    monkeypatch.setattr(JudgmentClient, "_post", post)

    verdict = await _check(_tool_context(config, paths))

    assert verdict.action == action


@pytest.mark.asyncio
async def test_secret_in_text_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A text with a detected secret is never sent to the judge, and the message is blocked."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    verdict = await _check(_tool_context(config, paths), text=f"The key is {LEAKED_KEY}")

    assert verdict == ContactVerdict(
        "deny",
        f"The key is {LEAKED_KEY}",
        "The message cannot be checked (essential_input_redacted).",
    )
    assert model.requests == []


@pytest.mark.asyncio
async def test_blank_text_denies_when_a_judge_is_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank message gives the judge nothing to check, which blocks it with the reason the request gives."""
    config, paths = _config(tmp_path, judge=LLM_JUDGE)
    model = _judge_model(monkeypatch, '{"decision": true}')

    verdict = await _check(_tool_context(config, paths), text="   ")

    assert verdict == ContactVerdict("deny", "   ", "The message cannot be checked (missing_essential_input).")
    assert model.requests == []
