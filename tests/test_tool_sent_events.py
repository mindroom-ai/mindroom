"""A run marks the Matrix messages its tool calls sent as seen, so a later turn never replays them after its answer."""

from __future__ import annotations

from dataclasses import replace
from itertools import count
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from agno.tools import Toolkit

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.client_delivery import send_message_result
from mindroom.message_target import MessageTarget
from mindroom.response_turn import build_matrix_run_metadata
from mindroom.session_storage.store import seen_event_ids
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_matrix_client_mock,
    make_turn_context,
    make_visible_message,
    runtime_paths_for,
)
from tests.delegation_helpers import (
    ScriptedAgents,
    _runtime_paths,
    native_delegation_config,
    respond,
    respond_team,
    stored_run,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.matrix.visible_messages import ResolvedVisibleMessage

_ROOM = "!room:localhost"
_THREAD = "$thread"
_SESSION = "session-1"
_USER = "@user:localhost"
_POSTER = "thread_poster"
_CAPTION = "Here is the mockup."


class _PosterTools(Toolkit):
    """A tool that posts into the turn's thread as the agent, the way ``matrix_message`` sends.

    ``client`` answers each send with the next ``$sent-N`` event.
    """

    client: ClassVar[AsyncMock]

    def __init__(self) -> None:
        super().__init__(name=_POSTER, tools=[self.post])

    async def post(self, message: str) -> str:
        """Post a message into the conversation.

        Args:
            message: The text to post.

        """
        delivered = await send_message_result(self.client, _ROOM, {"msgtype": "m.text", "body": message})
        assert delivered is not None
        return delivered.event_id


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


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["$question", None], ids=["a message's turn", "a job follow-up"])
async def test_a_message_a_tool_call_sent_is_not_replayed_after_the_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_id: str | None,
) -> None:
    """The next turn's request holds the run's call, its result, and its answer, and no copy of the sent message.

    Replayed, the sent message is an assistant turn after the answer, which providers merge into it; the model then
    reads its own answers as ending with what it sent, and writes that ending itself.
    """
    config = _config(tmp_path)
    agent_id = entity_ids(config, runtime_paths_for(config))["general"].full_id
    model = ScriptedModel([calls(tool_call("post", "post-1", message=_CAPTION)), answer("Posted it."), answer("Yes.")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(config, "Post the mockup", event_id)
    thread = [
        make_visible_message(sender=_USER, body="An earlier question", event_id="$question", thread_id=_THREAD),
        make_visible_message(sender=agent_id, body=_CAPTION, event_id="$sent-1", thread_id=_THREAD),
        make_visible_message(sender=_USER, body="Did it work?", event_id="$next", thread_id=_THREAD),
    ]
    await _respond(config, "Did it work?", "$next", thread_history=thread)

    next_request = model.requests[-1]
    replayed = [(message.role, message.content) for message in next_request.messages if message.role != "system"]
    # A follow-up answers no message, so the thread's earlier question is still unread context before its prompt.
    unread = [] if event_id else ["user"]
    assert [role for role, _ in replayed] == ["user", "assistant", "tool", "assistant", *unread, "user"]
    assert replayed[1:4] == [("assistant", None), ("tool", "$sent-1"), ("assistant", "Posted it.")]
    assert not [content for _, content in replayed if content and _CAPTION in content]


@pytest.mark.asyncio
async def test_only_a_tool_call_s_sends_are_recorded(tmp_path: Path) -> None:
    """What a turn sends outside its tool calls, as its reply and that reply's edits, is not a tool call's."""
    context = _tool_context(_config(tmp_path))

    with tool_runtime_context(context):
        delivered = await send_message_result(context.client, _ROOM, {"msgtype": "m.text", "body": "The reply"})

    assert delivered is not None
    assert context.sent_event_ids == []


def _delegation_options(tmp_path: Path, entity: str, **fields: object) -> dict[str, Any]:
    """Return the config, paths, and identity of a turn of ``entity`` whose ``code`` agent has the poster tool."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        native_delegation_config(
            {
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=[_POSTER]),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            **fields,
        ),
        "code",
        users=[_USER],
    )
    entity_ids(config, paths)
    identity = ToolExecutionIdentity("matrix", entity, _USER, _ROOM, _THREAD, _THREAD, "parent")
    return {"config": config, "runtime_paths": paths, "identity": identity}


def _posting_member() -> ScriptedModel:
    return ScriptedModel([calls(tool_call("post", "post-1", message=_CAPTION)), answer("Posted it.")])


@pytest.mark.asyncio
async def test_a_delegated_child_s_sends_stay_unseen_by_its_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's run holds only the child's answer, so what the child sent still reaches the caller's next turn."""
    options = _delegation_options(tmp_path, "leader")
    leader = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate", agent_name="code", task="Post it")), answer("The child posted.")],
    )
    ScriptedAgents({"leader": leader, "code": _posting_member()}).install(monkeypatch)

    reply = await respond("leader", "Delegate", **options, run_id="parent-run")

    assert reply.endswith("The child posted.")
    assert _PosterTools.client.room_send.await_count == 1
    assert seen_event_ids(stored_run("leader", "parent-run", **options).metadata) == set()


@pytest.mark.asyncio
async def test_a_team_s_run_marks_what_its_member_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A member's run stores no metadata, and the team's run holds the member's call, so the team's run marks it."""
    team = TeamConfig(display_name="Squad", role="Posts mockups.", agents=["code"], mode="coordinate")
    options = _delegation_options(tmp_path, "squad", teams={"squad": team})
    leader = ScriptedModel(
        [calls(tool_call("delegate_task_to_member", "member", member_id="code", task="Post it")), answer("Done.")],
    )
    ScriptedAgents({"code": _posting_member()}, team=leader).install(monkeypatch)

    reply = await respond_team("squad", "Post the mockup", **options, run_id="team-run")

    assert reply.endswith("Done.")
    assert seen_event_ids(stored_run("squad", "team-run", **options).metadata) == {"$sent-1"}
