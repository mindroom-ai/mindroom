"""What the ``claude_code`` route gives Claude Code: tool names, a rebuilt conversation, and a parked turn's results."""
# ruff: noqa: D103

from __future__ import annotations

import base64
import json

import pytest

from mindroom.llm.media import File, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ToolSpec
from mindroom.providers.claude_code.prompt import (
    CONTINUE_TEXT,
    Emitted,
    decide,
    rebuild_input,
    short_name,
    tool_names,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
)
_PROVIDER = "Claude subscription (Claude Code)"


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=None, parameters={"type": "object"}, strict=None)


def _call(call_id: str, name: str = "lookup", arguments: str = '{"key": "a"}') -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _text(blocks: list[dict[str, object]]) -> str:
    return "\n".join(str(block["text"]) for block in blocks if block["type"] == "text")


# --- names -------------------------------------------------------------------------------------------------------------


def test_a_plain_tool_name_is_offered_as_written() -> None:
    assert short_name("matrix_message") == "matrix_message"


def test_a_name_claude_code_cannot_take_gets_a_stable_hash_suffix() -> None:
    long_name = "a" * 80
    shortened = short_name(long_name)

    assert len("mcp__mindroom__" + shortened) <= 64
    assert shortened == short_name(long_name)
    assert shortened != short_name("a" * 81)
    assert short_name("web.search") != short_name("web_search")
    assert short_name("web.search").startswith("web_search_")


def test_tool_names_map_each_offered_name_back_to_its_tool() -> None:
    names = tool_names([_spec("lookup"), _spec("b" * 90)])

    assert names["lookup"] == "lookup"
    assert names[short_name("b" * 90)] == "b" * 90


# --- rebuilding a conversation ----------------------------------------------------------------------------------------


def test_a_first_request_sends_its_system_prompt_and_user_text_as_they_are() -> None:
    system, blocks = rebuild_input(
        [Message(role="system", content="Be brief."), Message(role="user", content="Hello @/etc/passwd")],
        _PROVIDER,
    )

    assert system == "Be brief."
    assert blocks == [{"type": "text", "text": "Hello @/etc/passwd"}]


def test_earlier_turns_become_tagged_history_before_the_new_user_message() -> None:
    _system, blocks = rebuild_input(
        [
            Message(role="user", content="What is the marker for a?"),
            Message(role="assistant", content="Looking.", tool_calls=[_call("call-1")]),
            Message(role="tool", content="LILAC-HERON", tool_call_id="call-1", tool_name="lookup"),
            Message(role="assistant", content="It is LILAC-HERON."),
            Message(role="user", content="And for b?"),
        ],
        _PROVIDER,
    )

    text = _text(blocks)
    history, _, current = text.partition("</conversation-checkpoint>")
    assert history.startswith("<conversation-checkpoint>")
    assert "<user>\nWhat is the marker for a?\n</user>" in history
    assert '<tool-call>\n{"id": "call-1", "name": "lookup", "input": {"key": "a"}}\n</tool-call>' in history
    assert '<tool-result>\n{"id": "call-1", "name": "lookup", "output": "LILAC-HERON"}\n</tool-result>' in history
    assert "<assistant>\nIt is LILAC-HERON.\n</assistant>" in history
    assert current.strip() == "And for b?"


def test_history_that_ends_on_tool_results_asks_claude_to_continue_from_them() -> None:
    _system, blocks = rebuild_input(
        [
            Message(role="user", content="Look up a."),
            Message(role="assistant", tool_calls=[_call("call-1")]),
            Message(role="tool", content="boom", tool_call_id="call-1", tool_name="lookup", tool_call_error=True),
        ],
        _PROVIDER,
    )

    text = _text(blocks)
    assert json.dumps({"id": "call-1", "name": "lookup", "output": "boom", "is_error": True}) in text
    assert text.endswith(CONTINUE_TEXT)


def test_images_stay_real_blocks_in_a_rebuilt_conversation() -> None:
    _system, blocks = rebuild_input(
        [
            Message(role="user", content="See this.", images=[Image(content=_PNG, format="png")]),
            Message(role="assistant", content="A dot."),
            Message(role="user", content="Again?"),
        ],
        _PROVIDER,
    )

    images = [block for block in blocks if block["type"] == "image"]
    assert images == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(_PNG).decode()},
        },
    ]
    assert blocks.index(images[0]) < len(blocks) - 1


# --- continuing a live session ----------------------------------------------------------------------------------------


def _parked(*ids: str) -> Emitted:
    return Emitted(content=None, tool_call_ids=ids)


def test_a_parked_turn_continues_with_one_result_per_waiting_call() -> None:
    resume = decide(
        _parked("t1", "t2"),
        [
            Message(role="assistant", tool_calls=[_call("t1"), _call("t2")]),
            Message(role="tool", content="LEMON-IBIS", tool_call_id="t2", tool_name="lookup"),
            Message(role="tool", content="nope", tool_call_id="t1", tool_name="lookup", tool_call_error=True),
        ],
        _PROVIDER,
    )

    assert resume is not None
    assert resume.results == {
        "t1": {"content": [{"type": "text", "text": "nope"}], "isError": True},
        "t2": {"content": [{"type": "text", "text": "LEMON-IBIS"}], "isError": False},
    }
    assert resume.blocks == []


def test_trailing_user_text_and_images_ride_on_the_last_result() -> None:
    resume = decide(
        _parked("t1", "t2"),
        [
            Message(role="assistant", tool_calls=[_call("t1"), _call("t2")]),
            Message(role="tool", content="one", tool_call_id="t1"),
            Message(role="tool", content="two", tool_call_id="t2"),
            Message(role="user", content="Here is the picture.", images=[Image(content=_PNG, format="png")]),
        ],
        _PROVIDER,
    )

    assert resume is not None
    assert resume.results["t1"]["content"] == [{"type": "text", "text": "one"}]
    assert resume.results["t2"]["content"] == [
        {"type": "text", "text": "two"},
        {"type": "text", "text": "Here is the picture."},
        {"type": "image", "data": base64.b64encode(_PNG).decode(), "mimeType": "image/png"},
    ]


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param([Message(role="tool", content="one", tool_call_id="t1")], id="a result is missing"),
        pytest.param(
            [
                Message(role="tool", content="one", tool_call_id="t1"),
                Message(role="tool", content="two", tool_call_id="t2"),
                Message(role="tool", content="again", tool_call_id="t2"),
            ],
            id="a call is answered twice",
        ),
        pytest.param(
            [
                Message(role="tool", content="one", tool_call_id="t1"),
                Message(role="tool", content="two", tool_call_id="t2"),
                Message(role="tool", content="three", tool_call_id="t3"),
            ],
            id="a result answers no waiting call",
        ),
        pytest.param(
            [
                Message(role="tool", content="one", tool_call_id="t1"),
                Message(role="user", content="wait"),
                Message(role="tool", content="two", tool_call_id="t2"),
            ],
            id="a user message sits between the results",
        ),
        pytest.param(
            [
                Message(role="tool", content="one", tool_call_id="t1"),
                Message(role="tool", content="two", tool_call_id="t2"),
                Message(role="user", content="read", files=[File(content=b"%PDF-1.7", mime_type="application/pdf")]),
            ],
            id="a file follows the results",
        ),
        pytest.param(
            [
                Message(role="tool", content="one", tool_call_id="t1"),
                Message(role="tool", content="two", tool_call_id="t2"),
                Message(role="system", content="New rules."),
            ],
            id="a system message follows the results",
        ),
    ],
)
def test_a_parked_turn_rebuilds_when_the_request_does_not_answer_it_exactly(tail: list[Message]) -> None:
    answered = Message(role="assistant", tool_calls=[_call("t1"), _call("t2")])

    assert decide(_parked("t1", "t2"), [answered, *tail], _PROVIDER) is None


def test_a_parked_turn_rebuilds_when_its_assistant_message_was_rewritten() -> None:
    tail = [
        Message(role="assistant", tool_calls=[_call("t1"), _call("other")]),
        Message(role="tool", content="one", tool_call_id="t1"),
        Message(role="tool", content="two", tool_call_id="other"),
    ]

    assert decide(_parked("t1", "t2"), tail, _PROVIDER) is None


def test_an_idle_session_continues_with_the_new_user_messages() -> None:
    resume = decide(
        Emitted(content="It is LILAC-HERON.", tool_call_ids=()),
        [Message(role="assistant", content="It is LILAC-HERON."), Message(role="user", content="And for b?")],
        _PROVIDER,
    )

    assert resume is not None
    assert resume.results == {}
    assert resume.blocks == [{"type": "text", "text": "And for b?"}]


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param([Message(role="assistant", content="It is LILAC-HERON.")], id="nothing new to send"),
        pytest.param(
            [Message(role="assistant", content="It is HERON."), Message(role="user", content="And b?")],
            id="the answer was edited",
        ),
        pytest.param(
            [
                Message(role="assistant", content="It is LILAC-HERON."),
                Message(role="user", content="And b?"),
                Message(role="assistant", content="Hm."),
            ],
            id="the request holds a later assistant message",
        ),
    ],
)
def test_an_idle_session_rebuilds_when_the_request_does_not_follow_its_answer(tail: list[Message]) -> None:
    assert decide(Emitted(content="It is LILAC-HERON.", tool_call_ids=()), tail, _PROVIDER) is None
