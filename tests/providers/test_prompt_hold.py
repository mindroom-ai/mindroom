"""The one rule of a held system prompt: it and its updates are sent only while every stored anchor matches."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from mindroom.llm.messages import PROMPT_UPDATE_KEY, Message
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, ToolSpec, TurnCompleted
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
_UPDATES = Capabilities(system_updates=True)


@dataclass
class _Model(PlainRequestSizing):
    """A route that takes system prompt updates and records the requests it is sent."""

    identity: ModelIdentity = _OPUS
    capabilities: Capabilities = _UPDATES
    native_history_route: None = None
    token_counter: TokenCounter = field(default_factory=fake_token_counter)
    model_params: dict[str, object] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield TurnCompleted(message=Message(role="assistant", content="ok"), stop_reason="end")


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

    def request(self, messages: Sequence[Message]) -> ModelRequest:
        return ModelRequest(messages=tuple(messages), tools=self.tools, prompt_hold=self.path)

    async def send(self, messages: Sequence[Message]) -> list[Message]:
        """Send ``messages`` and return the messages the model received."""
        async for _event in _held(self.model).stream(self.request(messages)):
            pass
        sent = self.model.requests[-1]
        assert sent.prompt_hold is None
        return list(sent.messages)

    def state(self) -> dict[str, object]:
        return json.loads(self.path.read_text(encoding="utf-8"))

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
        'In "## Replies":',
        "No longer in force (ignore these lines of the system prompt):",
        "- - End every reply with AMBER.",
        "In force now:",
        "+ - End every reply with VIOLET.",
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
    ["turn_context", "tools", "model", "idle", "first", "before_anchor", "after_update", "unreadable", "oversized"],
)
async def test_a_request_whose_anchors_or_route_no_longer_match_is_sent_as_built_and_restarts_the_hold(
    trigger: str,
    conversation: _Conversation,
) -> None:
    """Whatever rewrote the conversation, the request is main's, and the next change is told against it."""
    second, sent = await conversation.told()
    assert len(_updates(sent)) == 1
    third = [*_system(_B), *second[2:], _answer(), _user("three")]
    if trigger == "turn_context":
        third[1] = Message(role="system", content="Today is Thursday.")
    elif trigger == "tools":
        conversation.tools = (ToolSpec(name="add", description=None, parameters={}, strict=None),)
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
        third[0] = Message(role="system", content="\n".join(f"- Rule {number}." for number in range(250)))

    assert await conversation.send(third) == third
    assert conversation.state()["updates"] == []
    green = Message(role="system", content=f"{third[0].content}\n- End every reply with GREEN.")
    fourth = [green, third[1], *third[2:], _answer(), _user("four")]
    sent = await conversation.send(fourth)
    assert [message.content for message in sent[:2]] == [third[0].content, third[1].content]
    assert _shape(sent)[-1] == "update"
    assert "GREEN" in _updates(sent)[0]


@pytest.mark.asyncio
async def test_a_change_that_cannot_follow_a_user_message_or_would_be_one_update_too_many_restarts_the_hold(
    conversation: _Conversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update stands only after a user message at the request's end, and a conversation keeps only a few."""
    first = [*_system(_A), _user("one")]
    await conversation.send(first)
    resumed = [*_system(_B), *first[2:], _answer(), Message(role="tool", content="5", tool_call_id="call_1")]
    assert await conversation.send(resumed) == resumed
    assert conversation.state()["system"] == [_B, _TURN]

    monkeypatch.setattr(prompt_hold, "_MAX_UPDATES", 1)
    second = [*_system(_C), *resumed[2:], _answer(), _user("two")]
    assert len(_updates(await conversation.send(second))) == 1
    third = [*_system(_A), *second[2:], _answer(), _user("three")]
    assert await conversation.send(third) == third


@pytest.mark.asyncio
async def test_an_inlined_memory_file_is_never_quoted_in_an_update(conversation: _Conversation) -> None:
    """A change under a ``MEMORY.md`` heading sends the current prompt, so a forgotten memory is in no later request."""
    memory = "<your_role>\n### /agent/MEMORY.md\n# Memory\nThe door code is 4711.\n</your_role>\n\n- Be brief."
    first = [*_system(memory), _user("one")]
    await conversation.send(first)
    forgotten = [*_system(memory.replace("The door code is 4711.\n", "")), *first[2:], _answer(), _user("two")]
    assert await conversation.send(forgotten) == forgotten
    # A change outside the role is told, named by no heading of the role.
    edited = [*_system(str(forgotten[0].content).replace("brief", "thorough")), *forgotten[2:], _answer(), _user("x")]
    [update] = _updates(await conversation.send(edited))
    assert update.splitlines()[1] == "In the system prompt:"
    assert "4711" not in json.dumps([message.to_dict() for message in conversation.model.requests[-1].messages])


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
    assert provider.context_key(request) == model.context_key(sent)

    # An update before the answer moves the index the count starts from.
    third = [*second, _answer(), _user("three")]
    sent_third = replace(request, messages=tuple(await conversation.send(third)), prompt_hold=None)
    assert _shape(sent_third.messages[7:]) == ["update", "assistant", "user"]
    assert provider.request_tokens(conversation.request(third), start=7) == model.request_tokens(sent_third, start=8)


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
        return _shape(prompt_hold._commit(conversation.model, request, conversation.path).messages)

    with ThreadPoolExecutor(8) as pool:
        shapes = list(pool.map(commit, requests))
    assert not stale.exists()
    assert conversation.state()["system"][0] in {_A, _B, _C}
    assert {tuple(shape) for shape in shapes} <= {("system", "system", "user"), ("system", "system", "user", "update")}

    # A state root that cannot be written sends the request as built.
    monkeypatch.setattr(prompt_hold, "write_text_atomic", _refuse)
    request = conversation.request([*_system(_C), _user("another conversation")])
    assert prompt_hold._commit(conversation.model, request, conversation.path).messages == request.messages


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise PermissionError
