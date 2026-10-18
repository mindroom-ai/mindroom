"""Stored turns become conversation text one way: who said what, runtime prompts left out, credentials removed."""

from __future__ import annotations

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.history.conversation_text import utterances
from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY, QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER
from mindroom.hooks import EnrichmentItem, render_enrichment_block
from mindroom.prompt_message_tags import render_msg_tag


def _run(*messages: Message, status: RunStatus = RunStatus.completed, **metadata: object) -> RunOutput:
    return RunOutput(
        run_id="r1",
        session_id="s1",
        agent_id="mind",
        user_id="@alice:x",
        status=status,
        metadata=dict(metadata),
        messages=list(messages),
    )


def _turn(**metadata: object) -> RunOutput:
    """One finished turn: Alice asks, the agent calls a tool and answers."""
    return _run(
        Message(role="user", content=render_msg_tag(sender="@alice:x", body="When does Atlas ship?")),
        Message(role="assistant", content="", tool_calls=[{"function": {"name": "get_issue", "arguments": "{}"}}]),
        Message(role="tool", content="Issue 42: Friday", tool_name="get_issue"),
        Message(role="assistant", content="Atlas ships Friday."),
        **metadata,
    )


def _kinds(run: RunOutput, *, agent_work: bool = False) -> list[tuple[str, str | None, str]]:
    return [(item.kind, item.sender, item.unredacted) for item in utterances(run, agent_work=agent_work)]


def test_a_prompt_is_split_by_who_sent_each_message() -> None:
    """Each tagged message is its sender's, an untagged prompt is the requester's, and a reply is the agent's.

    Empty messages and non-text content are not conversation.
    """
    prompt = (
        "<messages>"
        + render_msg_tag(sender="@bob:x", body="Bob owns Atlas.")
        + render_msg_tag(sender="@mindroom_research:x", body="Checked: Carol does.")
        + render_msg_tag(sender="@carol:x", body="  ")
        + "</messages>"
    )
    run = _run(
        Message(role="user", content=prompt),
        Message(role="assistant", content=[{"type": "text", "text": "Thinking."}]),
        Message(role="assistant", content="Noted."),
        requester_id="@bob:x",
    )
    assert _kinds(run) == [
        ("said", "@bob:x", "Bob owns Atlas."),
        ("said", "@mindroom_research:x", "Checked: Carol does."),
        ("reply", None, "Noted."),
    ]
    # Without a recorded requester, the run's user is the one who asked.
    assert _kinds(_run(Message(role="user", content="<conversation>plain</conversation>"))) == [
        ("said", "@alice:x", "plain"),
    ]


def _history_copy() -> RunOutput:
    run = _turn()
    for message in run.messages or []:
        message.from_history = True
    run.messages = [Message(role="system", content="You are mind."), *(run.messages or [])]
    return run


def _child() -> RunOutput:
    run = _turn()
    run.parent_run_id = "parent"
    return run


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(_history_copy(), id="history-copy"),
        pytest.param(_child(), id="child"),
        pytest.param(_run(Message(role="user", content="hi"), status=RunStatus.paused), id="paused"),
        pytest.param(_run(Message(role="user", content="hi"), status=RunStatus.cancelled), id="cancelled"),
        pytest.param(_run(Message(role="user", content="hi"), status=RunStatus.error), id="errored"),
    ],
)
def test_what_the_agent_history_does_not_show_is_not_conversation(run: RunOutput) -> None:
    """Copies of earlier turns, system messages, and runs left out of the agent's history are never read."""
    assert _kinds(run, agent_work=True) == []


@pytest.mark.parametrize(
    ("metadata", "conversation", "agent_work"),
    [
        pytest.param({}, ["said", "reply"], ["said", "reply", "tool_result", "reply"], id="person"),
        # A follow-up's prompt is MindRoom's instruction to collect job results; its reply reports them.
        pytest.param(
            {TURN_INTENT_RUN_METADATA_KEY: "job_follow_up"},
            ["reply"],
            ["runtime", "reply", "tool_result", "reply"],
            id="job-follow-up",
        ),
        # An automation's turn is maintenance, not conversation, but a reviewer still sees how the agent worked.
        pytest.param(
            {HOOK_SOURCE_RUN_METADATA_KEY: "automation/dreaming"},
            [],
            ["runtime", "reply", "tool_result", "reply"],
            id="automation",
        ),
        # A stopped turn's answer is a rendered summary that can quote tool previews, not the agent's words.
        pytest.param(
            {"mindroom_replay_state": "interrupted"},
            ["said"],
            ["said", "reply", "tool_result", "reply"],
            id="stopped",
        ),
    ],
)
def test_runtime_prompts_and_stopped_summaries_are_nobodys_words(
    metadata: dict[str, object],
    conversation: list[str],
    agent_work: list[str],
) -> None:
    """Conversation readers get only what people and the agent said; a review also gets the agent's work."""
    run = _turn(**metadata)
    assert [kind for kind, _, _ in _kinds(run)] == conversation
    assert [kind for kind, _, _ in _kinds(run, agent_work=True)] == agent_work


def test_a_queued_message_notice_is_nobodys_words() -> None:
    """The pause notice MindRoom stores as a user message when someone writes mid-turn is not the requester's."""
    notice = Message(
        role="user",
        content="[SYSTEM NOTICE] A newer message arrived; pause here.",
        provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER},
    )
    run = _run(
        Message(role="user", content="Deploy it."),
        Message(role="assistant", content="Deploying."),
        notice,
        Message(role="assistant", content="Paused."),
    )

    assert _kinds(run) == [
        ("said", "@alice:x", "Deploy it."),
        ("reply", None, "Deploying."),
        ("reply", None, "Paused."),
    ]
    assert [kind for kind, _, _ in _kinds(run, agent_work=True)] == ["said", "reply", "runtime", "reply"]


def test_utterances_leave_out_hook_context() -> None:
    """Context a hook attached to a message is MindRoom's, not the sender's words."""
    block = render_enrichment_block([EnrichmentItem(key="location", text="latitude: 47.6")])
    run = _run(
        Message(
            role="user",
            content=render_msg_tag(sender="@alice:example.test", body=f"When does Atlas ship?\n\n{block}"),
        ),
    )

    said = [u for u in utterances(run) if u.kind == "said"]

    assert [u.unredacted for u in said] == ["When does Atlas ship?"]


def test_a_message_holding_only_hook_context_is_not_an_utterance() -> None:
    """A message with nothing the person typed adds no empty line to the conversation."""
    block = render_enrichment_block([EnrichmentItem(key="location", text="latitude: 47.6")])
    run = _run(Message(role="user", content=render_msg_tag(sender="@alice:example.test", body=block)))

    assert [u for u in utterances(run) if u.kind == "said"] == []
