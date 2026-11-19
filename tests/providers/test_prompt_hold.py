"""The one rule of a held system prompt: it and its updates are sent only while every stored anchor matches."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.messages import (
    PROMPT_UPDATE_KEY,
    QUEUED_MESSAGE_NOTICE_MARKER_KEY,
    REWRITTEN_ATTEMPT_KEY,
    Message,
)
from mindroom.llm.provider import (
    Capabilities,
    DeferredTools,
    ModelIdentity,
    ModelRequest,
    RequestOverrides,
    ToolSpec,
    TurnCompleted,
)
from mindroom.llm.request_identity import REQUEST_IDENTITY_KEY, request_prefixes
from mindroom.providers.pipeline import prompt_hold
from mindroom.providers.pipeline.prompt_hold import held_request, prompt_hold_path, prompt_hold_stream
from mindroom.providers.pipeline.wrapped import RequestProjection, WrappedProvider
from tests.fakes.request_sizing import PlainRequestSizing
from tests.fakes.token_counter import fake_token_counter
from tests.providers.golden_support import pipeline_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, StreamEvent, TokenCounter

_A = "# Rules\n\n## Replies\n- End every reply with AMBER.\n\n## Tools\n- Prefer the calculator."
_B = _A.replace("AMBER", "VIOLET")
_C = _A.replace("AMBER", "GREEN")
_TURN = "Today is Wednesday."
_OPUS = ModelIdentity(id="anthropic/claude-opus-5.5", provider="OpenRouter", endpoint="https://openrouter.ai/api/v1")


def _takes_updates(_request: ModelRequest) -> bool:
    return True


_UPDATES = Capabilities(system_updates=_takes_updates)
_SETTINGS = ModelRequest(messages=())


@dataclass
class _Model(PlainRequestSizing):
    """A route that takes system prompt updates and records the requests it is sent."""

    identity: ModelIdentity = _OPUS
    capabilities: Capabilities = _UPDATES
    native_history_route: None = None
    token_counter: TokenCounter = field(default_factory=fake_token_counter)
    model_params: dict[str, object] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)
    answer_data: dict[str, Any] | None = None

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        answer = Message(role="assistant", content="ok", provider_data=self.answer_data)
        yield TurnCompleted(message=answer, stop_reason="end")


def _held(model: ModelProvider) -> WrappedProvider:
    return WrappedProvider(model, prompt_hold_stream, RequestProjection(held_request))


def _system(stable: str, turn: str = _TURN) -> list[Message]:
    return [Message(role="system", content=stable), Message(role="system", content=turn)]


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _answer(text: str = "ok") -> Message:
    return Message(role="assistant", content=text)


def _replayed(messages: Sequence[Message]) -> list[Message]:
    """Return ``messages`` as a later turn replays them from storage: decoded again, under new ids and times."""
    return [
        replace(Message.from_dict(message.to_dict()), id=f"replayed-{index}", created_at=1, from_history=True)
        for index, message in enumerate(messages)
    ]


def _shape(messages: Sequence[Message]) -> list[str]:
    """Return each message's role, an update as ``update``."""
    return ["update" if PROMPT_UPDATE_KEY in (message.provider_data or {}) else message.role for message in messages]


def _updates(messages: Sequence[Message]) -> list[str]:
    return [str(message.content) for message in messages if PROMPT_UPDATE_KEY in (message.provider_data or {})]


@dataclass
class _Conversation:
    """One conversation's requests to a model through the hold stage, each naming the same hold file."""

    path: Path
    model: _Model = field(default_factory=_Model)
    tools: tuple[ToolSpec, ...] = ()
    settings: ModelRequest = _SETTINGS
    answers: list[StreamEvent] = field(default_factory=list)

    def request(self, messages: Sequence[Message], *, pin: list[dict[str, Any] | None] | None = None) -> ModelRequest:
        return replace(
            self.settings,
            messages=tuple(messages),
            tools=self.tools,
            prompt_hold=self.path,
            prompt_hold_pin=pin,
        )

    async def send(
        self,
        messages: Sequence[Message],
        *,
        read_only: bool = False,
        pin: list[dict[str, Any] | None] | None = None,
    ) -> list[Message]:
        """Send ``messages`` and return the messages the model received; ``read_only`` as a run of that one request."""
        request = self.request(messages, pin=[] if read_only else pin)
        self.answers = [event async for event in _held(self.model).stream(request)]
        sent = self.model.requests[-1]
        assert sent.prompt_hold is None
        return list(sent.messages)

    def state(self) -> dict[str, object]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def answer(self) -> Message:
        """Return the answer to the last request, which records the identity of that request as it was sent."""
        [answer] = self.answers
        assert isinstance(answer, TurnCompleted)
        return answer.message

    async def told(self, *, change: str = _B) -> tuple[list[Message], list[Message]]:
        """Open a conversation under ``_A``, edit its prompt to ``change``, and return the second turn and its send."""
        first = [*_system(_A), _user("one")]
        assert await self.send(first) == first
        second = [*_system(change), *_replayed([_user("one"), _answer()]), _user("two")]
        return second, await self.send(second)


@pytest.fixture
def conversation(tmp_path: Path) -> _Conversation:
    """A conversation whose hold file lies in the test's state root."""
    return _Conversation(prompt_hold_path(tmp_path / "sessions" / "agent.db", "session-1"))


@pytest.mark.asyncio
async def test_a_request_without_a_hold_file_or_to_a_route_without_updates_is_sent_untouched(tmp_path: Path) -> None:
    """Nothing but the field and the route's capability opts a request in, so every other one is main's."""
    model = _Model()
    plain = ModelRequest(messages=(*_system(_A), _user("one")))
    assert held_request(model, plain) is plain
    [_ async for _ in _held(model).stream(plain)]
    assert model.requests[-1] is plain

    unproven = _Conversation(tmp_path / "holds" / "x.json", _Model(capabilities=Capabilities()))
    first = [*_system(_A), _user("one")]
    assert await unproven.send(first) == first
    edited = [*_system(_B), *first[2:], _answer(), _user("two")]
    assert await unproven.send(edited) == edited
    assert not unproven.path.exists()
    # Through the whole pipeline, the adapter of such a route receives the same request with and without the field.
    piped = _Model(capabilities=Capabilities())
    for request in (unproven.request(edited), replace(unproven.request(edited), prompt_hold=None)):
        [_ async for _ in pipeline_provider(piped).stream(request)]
    with_field, without = piped.requests
    assert replace(with_field, on_send=without.on_send) == without


@pytest.mark.asyncio
async def test_a_changed_prompt_is_sent_as_the_held_one_and_one_update_after_the_newest_user_message(
    conversation: _Conversation,
) -> None:
    """The first request starts the hold; an edit keeps its system messages and adds the update, in place from then on."""
    second, sent = await conversation.told()
    assert [message.content for message in sent[:2]] == [_A, _TURN]
    assert _shape(sent) == ["system", "system", "user", "assistant", "user", "update"]
    assert sent[2:5] == second[2:5]
    [update] = _updates(sent)
    assert update.splitlines()[1:] == [
        "Change the system prompt as this diff shows:",
        "```diff",
        " ",
        " ## Replies",
        "-- End every reply with AMBER.",
        "+- End every reply with VIOLET.",
        " ",
        " ## Tools",
        "```",
    ]

    # The tool loop and the next turn extend the request around the update's one place, and tell nothing new.
    third = [*_system(_B), *_replayed(second[2:]), _answer(), _user("three")]
    sent = await conversation.send(third)
    assert _shape(sent) == ["system", "system", "user", "assistant", "user", "update", "assistant", "user"]
    assert _updates(sent) == [update]
    assert sent[0].content == _A


@pytest.mark.asyncio
async def test_each_update_states_the_whole_difference_and_a_revert_withdraws_them(conversation: _Conversation) -> None:
    """A to B to C to A under one held prompt: the newest update alone says what is in force."""
    second, _sent = await conversation.told()
    third = [*_system(_C), *second[2:], _answer(), _user("three")]
    fourth = [*_system(_A), *third[2:], _answer(), _user("four")]
    await conversation.send(third)
    sent = await conversation.send(fourth)
    assert sent[0].content == _A
    told = _updates(sent)
    assert [("AMBER" in text, "VIOLET" in text, "GREEN" in text) for text in told] == [
        (True, True, False),
        (True, False, True),
        (False, False, False),
    ]
    assert "withdrawn" in told[2]
    assert _shape(sent)[-1] == "update"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [
        "tools",
        "deferred_tools",
        "model",
        "idle",
        "first",
        "before_anchor",
        "after_update",
        "unreadable",
        "oversized",
    ],
)
async def test_a_request_whose_anchors_or_route_no_longer_match_is_sent_as_built_and_restarts_the_hold(
    trigger: str,
    conversation: _Conversation,
) -> None:
    """Whatever rewrote the conversation, the request is main's, and the next change is told against it."""
    second, sent = await conversation.told()
    assert len(_updates(sent)) == 1
    third = [*_system(_B), *second[2:], _answer(), _user("three")]
    if trigger == "tools":
        conversation.tools = (ToolSpec(name="add", description=None, parameters={}, strict=None),)
    elif trigger == "deferred_tools":
        # The same definitions, one of them now left off the request until a tool search loads it.
        deferred = DeferredTools(names=frozenset({"add"}), portable=True)
        conversation.settings = replace(conversation.settings, deferred_tools=deferred)
    elif trigger == "model":
        conversation.model = _Model(identity=replace(conversation.model.identity, id="anthropic/claude-opus-6"))
    elif trigger == "idle":
        os.utime(conversation.path, (time.time() - 2 * 3600,) * 2)
    elif trigger == "first":
        # A history limit dropped the oldest message, or a compaction replaced it with a summary.
        third[2] = _user("Summary of the conversation so far.")
    elif trigger == "before_anchor":
        third[3] = _answer("An answer a compaction rewrote.")
    elif trigger == "after_update":
        # A turn stored without its reply leaves the update before a user message, where no route takes it.
        del third[5]
    elif trigger == "unreadable":
        conversation.path.write_text('{"system": 3}', encoding="utf-8")
    else:
        third[0] = Message(role="system", content="\n".join(f"- Rule {number}: {'x' * 40}" for number in range(250)))

    assert await conversation.send(third) == third
    assert conversation.state()["updates"] == []
    green = Message(role="system", content=f"{third[0].content}\n- End every reply with GREEN.")
    fourth = [green, third[1], *third[2:], _answer(), _user("four")]
    sent = await conversation.send(fourth)
    assert [message.content for message in sent[:2]] == [third[0].content, third[1].content]
    assert _shape(sent)[-1] == "update"
    assert "GREEN" in _updates(sent)[0]


@pytest.mark.asyncio
async def test_a_request_that_caches_no_prompt_is_sent_as_built_and_leaves_the_hold(
    conversation: _Conversation,
) -> None:
    """Without a cached prefix there is nothing an update would keep, so the request is main's and writes nothing."""
    second, sent = await conversation.told()
    before = conversation.path.read_bytes()
    conversation.settings = replace(conversation.settings, overrides=RequestOverrides(prompt_cache=False))
    uncached = [*_system(_C), *second[2:], _answer(), _user("three")]
    assert await conversation.send(uncached) == uncached
    assert conversation.path.read_bytes() == before
    conversation.settings = _SETTINGS
    assert _updates(await conversation.send([*_system(_B), *uncached[2:]])) == _updates(sent)


@pytest.mark.asyncio
async def test_a_change_that_cannot_follow_a_user_message_restarts_the_hold(conversation: _Conversation) -> None:
    """An update stands only after a user message at the request's end."""
    first = [*_system(_A), _user("one")]
    await conversation.send(first)
    resumed = [*_system(_B), *first[2:], _answer(), Message(role="tool", content="5", tool_call_id="call_1")]
    assert await conversation.send(resumed) == resumed
    assert conversation.state()["system"] == [_B, _TURN]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", ["", "### /notes/archive\nArchive starts here.\n"])
async def test_a_prompt_that_inlines_a_memory_file_is_told_as_an_update_like_any_other(
    nested: str,
    conversation: _Conversation,
) -> None:
    """A ``MEMORY.md`` preloaded into the prompt is no special case, whatever headings the file holds."""
    fact = "The door code is 4711."
    memory = f"<your_role>\n### /agent/MEMORY.md\n# Memory\n{nested}Fact one.\n</your_role>\n\n- Be brief."
    first = [*_system(memory), _user("one")]
    await conversation.send(first)
    added = [*_system(memory.replace("Fact one.", f"Fact one.\n{fact}")), *first[2:], _answer(), _user("two")]
    sent = await conversation.send(added)
    assert (sent[0].content, _shape(sent)[-1]) == (memory, "update")
    assert f"+{fact}" in _updates(sent)[0].splitlines()
    edited = [*_system(memory.replace("brief", "thorough")), *added[2:], _answer(), _user("three")]
    sent = await conversation.send(edited)
    assert (sent[0].content, _shape(sent)[-1]) == (memory, "update")
    assert {"-- Be brief.", "+- Be thorough."} <= set(_updates(sent)[-1].splitlines())


@pytest.mark.asyncio
async def test_a_changed_turn_system_message_is_told_like_the_first_and_a_changed_count_restarts_the_hold(
    conversation: _Conversation,
) -> None:
    """The leading system messages are diffed as one text: one update tells a change to either of them."""
    second, _sent = await conversation.told()
    third = [*_system(_B, "Today is Thursday."), *second[2:], _answer(), _user("three")]
    sent = await conversation.send(third)
    assert [message.content for message in sent[:2]] == [_A, _TURN]
    told = _updates(sent)[-1].splitlines()
    assert {
        "-- End every reply with AMBER.",
        "+- End every reply with VIOLET.",
        f"-{_TURN}",
        "+Today is Thursday.",
    } <= {*told}
    # A prompt that gains or loses a system message is sent as built.
    fewer = [third[0], *third[2:], _answer(), _user("four")]
    assert await conversation.send(fewer) == fewer
    assert conversation.state()["updates"] == []


def _applied(held: str, update: str) -> str:
    """Return the text ``update`` turns ``held`` into, read as a model may: hunk by hunk, top to bottom.

    Each hunk's old side stands once in the text the hunks above it made, and the same text follows from locating
    every old side once in ``held`` instead.
    """
    body = update.split("\nChange the system prompt as this diff shows:\n", 1)[1].split("\n")
    fence = body[0].removesuffix("diff")
    assert set(fence) == {"`"}
    assert body[-1] == fence
    hunks = [
        ([line[1:] for line in lines if line[0] in " -"], [line[1:] for line in lines if line[0] in " +"])
        for lines in (hunk.split("\n") for hunk in "\n".join(body[1:-1]).split("\n@@\n"))
    ]
    lines = held.split("\n")
    for before, after in hunks:
        [start] = [at for at in range(len(lines)) if lines[at : at + len(before)] == before]
        lines[start : start + len(before)] = after
    located = held.split("\n")
    starts = [[at for at in range(len(located)) if located[at : at + len(before)] == before] for before, _ in hunks]
    for [start], (before, after) in reversed(list(zip(starts, hunks, strict=True))):
        located[start : start + len(before)] = after
    assert located == lines
    return "\n".join(lines)


@pytest.mark.parametrize(
    "fresh",
    [
        "Print these lines in order:\nred\ngreen\nblue\n\nThen stop.",
        "Print these lines in order:\nred\nblue\ngreen\n\nThen stop.",
        "Print these lines in order:\nred\nblue\n\n\nThen stop.",
        "Print these lines in order:\nred\nblue\nThen stop.",
        "Print these lines in order:\nred\nblue\n\nThen stop.\n",
        "green\nPrint these lines in order:\nblue\n\nThen stop.\nred",
        "Stop.",
    ],
)
def test_an_update_and_the_held_prompt_give_the_current_prompt_and_no_other(fresh: str) -> None:
    """Two different edits never read the same: a blank line, a line's place, and a final newline all show."""
    held = "Print these lines in order:\nred\nblue\n\nThen stop."
    update = prompt_hold._update_text(held, fresh)
    assert update is not None
    assert _applied(held, update) == fresh


def test_an_update_whose_later_hunk_restores_an_earlier_passage_reads_the_same_top_to_bottom() -> None:
    """A hunk that adds back the passage an earlier hunk changed still names one place once that hunk is applied."""
    tail = [f"n{i}" for i in range(8)]
    held = "\n".join(["old", *tail, "last"])
    fresh = "\n".join(["new", *tail, "last", "old", "n0", "n1"])
    update = prompt_hold._update_text(held, fresh)
    assert update is not None
    assert update.count("\n@@\n") == 1
    assert _applied(held, update) == fresh


def test_a_hunk_that_an_earlier_hunk_makes_stand_twice_is_not_told() -> None:
    """Copying a section to the top and editing the original leaves the edit's old side twice in the text read so far."""
    rules = ["- Be kind.", "- Be exact.", "- Cite sources.", "- Say when unsure.", "- Keep notes.", ""]
    tools = ["## Tools", "- Prefer the calculator."]
    held = "\n".join(["# Helper", *rules, *tools, "- Ask first.", "", "## End", "- Sign off."])
    fresh = "\n".join(
        ["# Helper", *tools, "- Ask first.", "", "## End", *rules, *tools, "- Never ask.", "", "## End", "- Sign off."],
    )
    assert prompt_hold._update_text(held, fresh) is None


def test_an_update_splits_its_hunks_by_a_bare_at_line_and_fences_them_past_any_backticks() -> None:
    """Each change is quoted with two lines around it, and no line of the prompt can close the diff early."""
    held = "\n".join(
        ["## Reply rules", "Be terse.", "End with AMBER.", "", *[f"n{i}" for i in range(10)], "Last AMBER."],
    )
    fresh = held.replace("AMBER", "VIOLET")
    update = prompt_hold._update_text(held, fresh)
    assert update is not None
    assert update.split("\n")[2:] == [
        "```diff",
        " ## Reply rules",
        " Be terse.",
        "-End with AMBER.",
        "+End with VIOLET.",
        " ",
        " n0",
        "@@",
        " n8",
        " n9",
        "-Last AMBER.",
        "+Last VIOLET.",
        "```",
    ]
    assert _applied(held, update) == fresh
    fenced = prompt_hold._update_text("a\n```\nb", "a\n```\nc")
    assert fenced is not None
    assert "\n````diff\n" in fenced
    assert fenced.endswith("\n````")
    assert _applied("a\n```\nb", fenced) == "a\n```\nc"
    assert prompt_hold._update_text("a\nb", "a\n" + "c" * 9000) is None


def test_a_change_among_repeated_lines_that_no_passage_names_once_is_not_told() -> None:
    """A passage that stands twice in the held prompt could be replaced in either place, so the prompt is sent anew."""
    held = "\n".join(["- Check the build.", "- Run the tests.", "- Report."] * 4)
    fresh = held.replace("- Run the tests.", "- Run the linter.", 2).replace("- Run the linter.", "- Run the tests.", 1)
    assert fresh != held
    assert prompt_hold._update_text(held, fresh) is None


@pytest.mark.asyncio
async def test_a_queued_notice_before_an_update_counts_by_the_text_it_sends(conversation: _Conversation) -> None:
    """The request sends a notice's text, so a notice that changed before an update's place sends the request as built."""
    notice = Message(
        role="user",
        content="A new message waits.",
        provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: True},
    )
    await conversation.send([*_system(_A), _user("one")])
    changed = [*_system(_B), _user("one"), _answer(), notice, _user("two")]
    assert len(_updates(await conversation.send(changed))) == 1
    same = [*changed, _answer(), _user("three")]
    assert len(_updates(await conversation.send(same))) == 1
    later = [*changed[:4], replace(notice, content="Another message waits."), *changed[5:], _answer(), _user("three")]
    assert await conversation.send(later) == later


@pytest.mark.asyncio
async def test_a_read_only_request_is_sent_the_hold_it_continues_and_never_writes_it(
    conversation: _Conversation,
) -> None:
    """A fork or a resumed run reads the conversation's hold: it starts none, adds no update, and restarts none."""
    first = [*_system(_A), _user("one")]
    assert await conversation.send(first, read_only=True) == first
    assert not conversation.path.exists()

    second, sent = await conversation.told()
    os.utime(conversation.path, (time.time() - 60,) * 2)
    before = (conversation.path.read_bytes(), conversation.path.stat().st_mtime)
    # A resumed run replays what it was sent, and a review forks it: both are sent the held prompt and its update.
    answered = conversation.answer()
    resumed = [*_system(_B), *_replayed([*second[2:], answered]), _user("Review this conversation.")]
    replayed = await conversation.send(resumed, read_only=True)
    assert _shape(replayed) == [*_shape(sent), "assistant", "user"]
    assert (replayed[0].content, _updates(replayed)) == (_A, _updates(sent))
    # A run whose newest answer was sent another prompt than the hold tells goes on with its own: one paused while
    # its route held no prompt, or one whose answer records no request.
    # Nor does an earlier answer sent the hold speak for a newer one that records none, such as one whose request
    # held an image at a URL or was answered as another request.
    as_built = request_prefixes(conversation.model, conversation.request(second))[-1]
    unrecorded = replace(_answer(), provider_data={REWRITTEN_ATTEMPT_KEY: True})
    for newest in (
        [replace(answered, provider_data={REQUEST_IDENTITY_KEY: as_built})],
        [_answer()],
        [answered, _user("three"), _answer()],
        [answered, _user("three"), unrecorded, Message(role="tool", content="5", tool_call_id="call_1")],
    ):
        unheld = [*_system(_B), *second[2:], *newest, _user("Review this conversation.")]
        assert await conversation.send(unheld, read_only=True) == unheld
    # One whose prompt the hold does not tell yet, or whose messages it does not match, is sent as it was built.
    for stale in (
        [*_system(_C), *resumed[2:]],
        [*_system(_B), _user("Summary of the conversation so far."), *resumed[3:]],
        [*_system(_B), *resumed[2:4]],
    ):
        assert await conversation.send(stale, read_only=True) == stale
    assert (conversation.path.read_bytes(), conversation.path.stat().st_mtime) == before
    # The conversation's own next turn goes on from the hold it left.
    third = [*_system(_B), *second[2:], answered, _user("three")]
    assert _updates(await conversation.send(third)) == _updates(sent)


@pytest.mark.asyncio
async def test_a_replay_of_the_same_messages_sends_the_same_prompt_and_updates(conversation: _Conversation) -> None:
    """A retry, a failed fold, and a resumed run replay the messages they were sent, so their anchors match."""
    second, sent = await conversation.told()
    again = await conversation.send([*_system(_B), *_replayed(second[2:])])
    assert _shape(again) == _shape(sent)
    assert ([message.content for message in again[:2]], _updates(again)) == ([_A, _TURN], _updates(sent))

    # A paused run resumes with the system messages it stored, the tool call it paused on, and its result.
    call = Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "add"}}])
    paused = [*_system(_B), *second[2:], call]
    await conversation.send(paused[:-1])
    resumed = await conversation.send([*_replayed(paused), Message(role="tool", content="5", tool_call_id="c1")])
    assert _shape(resumed) == [*_shape(sent), "assistant", "tool"]
    assert _updates(resumed) == _updates(sent)


@pytest.mark.asyncio
async def test_a_run_resumed_after_a_later_turn_told_another_update_is_sent_its_own_prompt(
    conversation: _Conversation,
) -> None:
    """The paused run's messages end before the later update's anchor, so it sends the prompt it stored, as on main."""
    second, _sent = await conversation.told()
    later = [*_system(_C), *second[2:], _answer(), _user("three")]
    assert len(_updates(await conversation.send(later))) == 2
    resumed = [*_system(_B), *second[2:], _answer(), Message(role="tool", content="5", tool_call_id="c1")]
    assert await conversation.send(resumed) == resumed


@pytest.mark.asyncio
async def test_a_fold_that_keeps_the_messages_before_every_anchor_keeps_the_hold(conversation: _Conversation) -> None:
    """A mid-turn fold of the turn's own work leaves the cached prefix, so the held prompt still leads it."""
    second, sent = await conversation.told()
    folded = [*_system(_B), *second[2:], _answer("Summary of this turn's tool calls.")]
    assert _shape(await conversation.send(folded)) == [*_shape(sent), "assistant"]


@pytest.mark.asyncio
async def test_a_request_is_sized_and_identified_as_it_is_sent(conversation: _Conversation) -> None:
    """Sizing reads the hold without changing it, and counts from the answer ``start`` names in the caller's request."""
    first = [*_system(_A), _user("one")]
    await conversation.send(first)
    before = conversation.path.read_bytes()
    second = [*_system(_B + "\n- " + "More rules. " * 200), *first[2:], _answer(), _user("two"), _answer(), _user("x")]
    request = conversation.request(second)
    provider, model = _held(conversation.model), conversation.model
    size = provider.request_tokens(request)
    assert conversation.path.read_bytes() == before
    sent = replace(request, messages=tuple(await conversation.send(second)), prompt_hold=None)
    assert _shape(sent.messages)[-1] == "update"
    assert sent.messages[0].content == _A
    assert provider.request_tokens(request) == size == model.request_tokens(sent)
    assert size != model.request_tokens(replace(request, prompt_hold=None))
    assert provider.request_tokens(request, start=3) == model.request_tokens(sent, start=3)
    # The request's identity names the prompt and update it is sent with, so usage reported for it sizes no request
    # sent another prefix: the same messages once the hold is gone, or under a later update.
    held_key = provider.context_key(request)
    assert held_key == provider.context_key(conversation.request(second)) != model.context_key(sent)
    assert provider.context_key(conversation.request(first)) == model.context_key(conversation.request(first))

    # An update before the answer moves the index the count starts from.
    third = [*second, _answer(), _user("three")]
    sent_third = replace(request, messages=tuple(await conversation.send(third)), prompt_hold=None)
    assert _shape(sent_third.messages[7:]) == ["update", "assistant", "user"]
    assert provider.request_tokens(conversation.request(third), start=7) == model.request_tokens(sent_third, start=8)
    assert provider.context_key(conversation.request(third)) == held_key
    os.utime(conversation.path, (time.time() - 2 * 3600,) * 2)
    assert provider.context_key(conversation.request(third)) == model.context_key(sent_third) != held_key


def _recorded(conversation: _Conversation) -> object:
    """Return the request identity the last answer records."""
    return (conversation.answer().provider_data or {}).get(REQUEST_IDENTITY_KEY)


@pytest.mark.asyncio
async def test_an_answer_records_the_identity_of_its_request_as_it_was_sent(
    conversation: _Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What was sent decides it, when it was sent: the hold may be gone, or stand again, once the answer is there."""
    provider, model = _held(conversation.model), conversation.model
    first = [*_system(_A), _user("one")]
    await conversation.send(first)
    assert _recorded(conversation) == request_prefixes(model, conversation.request(first))[-1]
    second, sent = await conversation.told()
    assert _updates(sent)
    held = _recorded(conversation)
    as_built = request_prefixes(model, conversation.request(second))[-1]
    assert held == request_prefixes(provider, conversation.request(second))[-1] != as_built

    # A request whose hold cannot be stored is sent as it was built, though the file still tells its update.
    monkeypatch.setattr(prompt_hold.os, "utime", _refuse)
    assert await conversation.send(second) == second
    assert _recorded(conversation) == as_built
    monkeypatch.undo()
    assert request_prefixes(provider, conversation.request(second))[-1] == held

    # A run that pinned the hold is sent it after it ran out, when a request that reads the file is sent as built.
    resumed = [*second, replace(_answer(), provider_data={REQUEST_IDENTITY_KEY: held}), _user("three")]
    pin: list[dict[str, Any] | None] = []
    await conversation.send(resumed, pin=pin)
    resumed_as = _recorded(conversation)
    os.utime(conversation.path, (time.time() - 2 * 3600,) * 2)
    assert _updates(await conversation.send(resumed, pin=pin)) == _updates(sent)
    assert _recorded(conversation) == resumed_as
    assert resumed_as != request_prefixes(provider, conversation.request(resumed))[-1]

    # An answer a stage inside got for another request records none: its usage is not this request's.
    model.answer_data = {REWRITTEN_ATTEMPT_KEY: True}
    await conversation.send(resumed, pin=pin)
    assert _recorded(conversation) is None


@pytest.mark.asyncio
async def test_every_request_of_a_run_that_only_reads_the_hold_is_sent_the_prompt_its_first_was(
    conversation: _Conversation,
) -> None:
    """A resumed run's tool loop keeps its prefix when the hold runs out or stands again meanwhile, and writes nothing."""
    second, sent = await conversation.told()
    loop = [*second, conversation.answer(), Message(role="tool", content="5", tool_call_id="call_1")]
    contents = [message.content for message in sent]

    # Only a request the run sends pins it: sizing or identifying one, even another one, decides nothing.
    pin: list[dict[str, Any] | None] = []
    provider = _held(conversation.model)
    for sized in ([*second[:2], _user("Another replay.")], loop):
        provider.request_tokens(conversation.request(sized, pin=pin))
        provider.context_key(conversation.request(sized, pin=pin))
    assert pin == []
    resumed = await conversation.send(loop, pin=pin)
    assert [message.content for message in resumed[: len(sent)]] == contents
    assert len(pin) == 1
    again = [*loop, conversation.answer(), Message(role="tool", content="6", tool_call_id="call_2")]
    later = [*again, _answer(), _user("three")]
    os.utime(conversation.path, (time.time() - 2 * 3600,) * 2)
    before = (conversation.path.read_bytes(), conversation.path.stat().st_mtime)
    for messages in (again, later):
        resumed = await conversation.send(messages, pin=pin)
        assert [message.content for message in resumed[: len(sent)]] == contents
        assert _updates(resumed) == _updates(sent)
    # The size counts what the run is sent, and messages the pinned hold does not match are sent as they are.
    request = conversation.request(later, pin=pin)
    assert _held(conversation.model).request_tokens(request) == conversation.model.request_tokens(
        replace(request, messages=tuple(resumed)),
    )
    folded = [*_system(_B), _user("Summary of the conversation so far."), *later[3:]]
    assert await conversation.send(folded, pin=pin) == folded

    # A run whose first request found no hold to continue sends its own prompt, though the hold counts again later.
    unheld: list[dict[str, Any] | None] = []
    assert await conversation.send(loop, pin=unheld) == loop
    assert (conversation.path.read_bytes(), conversation.path.stat().st_mtime) == before
    os.utime(conversation.path)
    assert await conversation.send(again, pin=unheld) == again
    assert _updates(await conversation.send(again, read_only=True)) == _updates(sent)
    assert conversation.path.read_bytes() == before[0]


@pytest.mark.asyncio
async def test_a_conversation_tells_eight_updates_and_starts_over_at_the_ninth(conversation: _Conversation) -> None:
    """Every request sends each update again, so a prompt edited over and over is sent as it is, as without a hold."""
    messages = [*_system(_A), _user("turn 0")]
    await conversation.send(messages)
    for turn in range(1, 9):
        messages = [*_system(f"{_A}\n- Revision {turn}."), *messages[2:], _answer(), _user(f"turn {turn}")]
        sent = await conversation.send(messages)
        assert (sent[0].content, len(_updates(sent))) == (_A, turn)
    # A turn that changes nothing adds none, so the eight stay; the next change starts the hold over.
    messages = [*messages, _answer(), _user("turn 9")]
    assert len(_updates(await conversation.send(messages))) == 8
    ninth = [*_system(f"{_A}\n- Revision 9."), *messages[2:], _answer(), _user("turn 10")]
    assert await conversation.send(ninth) == ninth
    assert conversation.state()["updates"] == []


@pytest.mark.asyncio
async def test_holds_are_kept_per_conversation_pruned_when_idle_and_survive_concurrent_requests(
    conversation: _Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests of one agent take turns at its hold files, and a hold idle for a day is deleted."""
    assert conversation.path != prompt_hold_path(conversation.path.parent.parent / "sessions" / "a.db", "session-2")
    stale = conversation.path.with_name("stale.json")
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (time.time() - 25 * 3600,) * 2)
    requests = [conversation.request([*_system(text), _user("one")]) for text in (_A, _B, _C) * 5]

    def commit(request: ModelRequest) -> list[str]:
        return _shape(prompt_hold._commit(conversation.model, request).messages)

    with ThreadPoolExecutor(8) as pool:
        shapes = list(pool.map(commit, requests))
    assert not stale.exists()
    assert conversation.state()["system"][0] in {_A, _B, _C}
    assert {tuple(shape) for shape in shapes} <= {("system", "system", "user"), ("system", "system", "user", "update")}

    # A state root that cannot be written sends the request as built.
    monkeypatch.setattr(prompt_hold, "write_text_atomic", _refuse)
    request = conversation.request([*_system(_C), _user("another conversation")])
    assert prompt_hold._commit(conversation.model, request).messages == request.messages


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise PermissionError
