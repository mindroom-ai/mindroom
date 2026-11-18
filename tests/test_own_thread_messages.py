"""A thread message the agent itself sent reaches a later turn as a tagged thread message, never an assistant turn.

Providers merge consecutive assistant turns, so an own message replayed as one became the end of the answer before it;
the model then read its answers as ending with what it sent, and wrote such an ending itself.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import count
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.ai import ai_response
from mindroom.attachments.storage import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ATTACHMENT_IDS_KEY, resolve_runtime_paths
from mindroom.matrix.client_delivery import send_message_result
from mindroom.message_target import MessageTarget
from mindroom.providers.anthropic.wire import format_claude_messages
from mindroom.response_turn import build_matrix_run_metadata
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_matrix_client_mock,
    make_turn_context,
    make_visible_message,
    runtime_paths_for,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_ROOM = "!room:localhost"
_THREAD = "$thread"
_SESSION = "session-1"
_USER = "@user:localhost"
_POSTER = "thread_poster"
_CAPTION = "Here is the mockup."
_ANNOTATION = '[attachments: att_mockup (image, "mockup.png")]'


class _PosterTools(Toolkit):
    """A tool that posts into the turn's thread as the agent, the way ``matrix_message`` sends.

    ``client`` answers each send with the next ``$sent-N`` event.
    """

    client: ClassVar[AsyncMock]

    def __init__(self) -> None:
        super().__init__(name=_POSTER, tools=[self.post])

    async def post(self, message: str) -> str:
        """Post a message and its image into the conversation.

        Args:
            message: The caption to post.

        """
        events = [
            await send_message_result(self.client, _ROOM, content)
            for content in ({"msgtype": "m.text", "body": message}, {"msgtype": "m.image", "body": "mockup.png"})
        ]
        return " ".join(event.event_id for event in events if event is not None)


@pytest.fixture(autouse=True)
def _poster_tool() -> Iterator[None]:
    client = make_matrix_client_mock()
    sent = count(1)
    client.room_send = AsyncMock(
        side_effect=lambda **kwargs: nio.RoomSendResponse(f"$sent-{next(sent)}", kwargs["room_id"]),
    )
    _PosterTools.client = client
    with registered_toolkit(_POSTER, _PosterTools):
        yield


def _config(storage: Path) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", tools=[_POSTER])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage),
    )
    config.memory.backend = "none"
    return config


def _tool_context(config: Config) -> ToolRuntimeContext:
    runtime_paths = runtime_paths_for(config)
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(_ROOM, _THREAD, None),
        requester_id=_USER,
        client=_PosterTools.client,
        config=config,
        runtime_paths=runtime_paths,
        plugins=config_plugins(config, runtime_paths),
        conversation_reader=MagicMock(),
        relations=MagicMock(),
    )


async def _respond(
    config: Config,
    prompt: str,
    event_id: str | None,
    *,
    thread_history: Sequence[ResolvedVisibleMessage] = (),
) -> None:
    """Answer ``event_id`` as a Matrix turn does: in the turn's tool context, with the thread as it stands.

    A turn without an ``event_id`` answers no message, as a background job's follow-up does.
    """
    ctx = replace(
        make_turn_context(
            "general",
            session_id=_SESSION,
            reply_to_event_id=event_id,
            room_id=_ROOM,
            thread_id=_THREAD,
            requester_id=_USER,
            matrix_run_metadata=build_matrix_run_metadata(event_id, []),
        ),
        prompt=prompt,
        thread_history=thread_history,
    )
    with tool_runtime_context(_tool_context(config)):
        await ai_response(ctx, runtime_paths_for(config), config)


def _own_image(config: Config, agent_id: str, event_id: str) -> ResolvedVisibleMessage:
    """Return the agent's own image message in the thread, whose attachment the thread's room holds as ``att_mockup``."""
    storage = runtime_paths_for(config).storage_root
    image = storage / "mockup.png"
    image.write_bytes(b"png")
    record = register_local_attachment(
        storage,
        image,
        kind="image",
        attachment_id="att_mockup",
        filename="mockup.png",
        room_id=_ROOM,
        thread_id=_THREAD,
    )
    assert record is not None
    content = {"msgtype": "m.image", "body": "mockup.png", ATTACHMENT_IDS_KEY: ["att_mockup"]}
    return make_visible_message(
        sender=agent_id,
        body="mockup.png",
        event_id=event_id,
        content=content,
        thread_id=_THREAD,
    )


def _sent(request: ModelRequest) -> list[tuple[str, str | None]]:
    """Return the role and text of each message ``request`` sends after its system messages."""
    return [(message.role, message.content) for message in request.messages if message.role != "system"]


def _assistant_wire_text(request: ModelRequest) -> list[str]:
    """Return the text of each assistant turn Claude reads, after the wire merged consecutive same-role turns."""
    formatted = format_claude_messages(
        request.messages,
        append_trailing_user_message=False,
        earlier_thinking=True,
        provider="anthropic",
    )
    return [
        "".join(block["text"] for block in turn["content"] if block.get("type") == "text")
        for turn in formatted.messages
        if turn["role"] == "assistant"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["$question", None], ids=["a message's turn", "a job follow-up"])
async def test_what_a_tool_call_posted_is_no_part_of_the_answer_the_next_turn_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_id: str | None,
) -> None:
    """The next request's assistant turns are the run's own; the caption and image it posted follow as its messages."""
    config = _config(tmp_path)
    agent_id = entity_ids(config, runtime_paths_for(config))["general"].full_id
    model = ScriptedModel([calls(tool_call("post", "post-1", message=_CAPTION)), answer("Posted it."), answer("Yes.")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(config, "Post the mockup", event_id)
    thread = [
        make_visible_message(sender=_USER, body="An earlier question", event_id="$question", thread_id=_THREAD),
        make_visible_message(sender=agent_id, body=_CAPTION, event_id="$sent-1", thread_id=_THREAD),
        _own_image(config, agent_id, "$sent-2"),
        make_visible_message(sender=_USER, body="Did it work?", event_id="$next", thread_id=_THREAD),
    ]
    await _respond(config, "Did it work?", "$next", thread_history=thread)

    sent = _sent(model.requests[-1])
    # A follow-up answers no message, so the thread's earlier question is still unread context before its prompt.
    unread = [] if event_id else ["user"]
    assert [role for role, _ in sent] == ["user", "assistant", "tool", "assistant", *unread, "user", "user", "user"]
    assert sent[1:4] == [("assistant", None), ("tool", "$sent-1 $sent-2"), ("assistant", "Posted it.")]
    # Claude reads the answer alone as the last assistant turn, so nothing the model wrote ends with what it posted.
    assert _assistant_wire_text(model.requests[-1]) == ["", "Posted it."]
    # The agent still reads both as its own messages, marked as sent by it, with the image's attachment ID to act on.
    caption, image = (content or "" for _, content in sent[-3:-1])
    assert caption == f'<msg event_id="$sent-1" from="{agent_id}" sent_by_you="true"><![CDATA[{_CAPTION}]]></msg>'
    assert (
        image
        == f'<msg event_id="$sent-2" from="{agent_id}" sent_by_you="true"><![CDATA[mockup.png\n{_ANNOTATION}]]></msg>'
    )


@pytest.mark.asyncio
async def test_what_the_agent_posted_after_its_answer_was_stored_is_no_part_of_that_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message no run's tool call sent, as a background job posts one after its turn, is a thread message too."""
    config = _config(tmp_path)
    agent_id = entity_ids(config, runtime_paths_for(config))["general"].full_id
    model = ScriptedModel([answer("The render is running."), answer("Yes."), answer("Welcome.")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(config, "Render the mockup", "$question")
    thread = [
        make_visible_message(sender=_USER, body="Render the mockup", event_id="$question", thread_id=_THREAD),
        _own_image(config, agent_id, "$late"),
        make_visible_message(sender=_USER, body="Did it work?", event_id="$next", thread_id=_THREAD),
    ]
    await _respond(config, "Did it work?", "$next", thread_history=thread)

    sent = _sent(model.requests[-1])
    assert [role for role, _ in sent] == ["user", "assistant", "user", "user"]
    assert _assistant_wire_text(model.requests[-1]) == ["The render is running."]
    assert sent[2] == (
        "user",
        f'<msg event_id="$late" from="{agent_id}" sent_by_you="true"><![CDATA[mockup.png\n{_ANNOTATION}]]></msg>',
    )

    # The second turn stored the image with its input, so a third turn replays it once, where the second read it.
    thread.append(make_visible_message(sender=_USER, body="Thanks", event_id="$third", thread_id=_THREAD))
    await _respond(config, "Thanks", "$third", thread_history=thread)

    replayed = _sent(model.requests[-1])
    assert replayed[: len(sent)] == sent
    assert [role for role, content in replayed if "$late" in (content or "")] == ["user"]
    assert [role for role, _ in replayed[len(sent) :]] == ["assistant", "user"]
