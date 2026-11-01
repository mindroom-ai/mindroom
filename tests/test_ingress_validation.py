"""Focused tests for requester identity resolution at ingress."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.commands.parsing import CommandType
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import (
    ACTING_REQUESTER_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    HOOK_SOURCE_KEY,
    ORIGINAL_SENDER_KEY,
    SOURCE_KIND_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    TOOL_TRACE_CONTENT_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.dispatch_handoff import dispatch_prompt_for_event
from mindroom.dispatch_source import (
    EDIT_SOURCE_KIND,
    EXTERNAL_TRIGGER_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.entity_resolution import mindroom_user_id
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.turn_origin import TurnIntent
from mindroom.turns.facts import command_for, is_router_voice_echo
from tests.access_schema_support import with_current_room_member_access
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.identity import MatrixID


@pytest.mark.asyncio
async def test_trusted_relay_resolves_requester_and_allows_self_authored_ingress(tmp_path: Path) -> None:
    """Trusted relays should preserve human requesters without trusting outsiders."""
    canonical_human = "@human:localhost"
    bridge_human = "@bridge_human:localhost"
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={"test_agent": {"display_name": "Test Agent"}},
                bot_accounts=["@bridge_bot:localhost"],
                mindroom_user=MindRoomUserConfig(),
                models={"default": {"provider": "openai", "id": "test-model"}},
                authorization={"aliases": {canonical_human: [bridge_human]}},
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    internal_user_id = mindroom_user_id(config, runtime_paths)
    assert internal_user_id is not None
    non_human_senders = (
        ids["test_agent"].full_id,
        ids["router"].full_id,
        "@bridge_bot:localhost",
        internal_user_id,
    )
    config.authorization.aliases[canonical_human].extend(non_human_senders)
    config.defaults.enable_streaming = False
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, runtime_paths))
    turn_store = MagicMock()
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_policy = MagicMock()
    turn_policy.can_reply_to_sender_in_room.return_value = True
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["test_agent"],
            turn_store=turn_store,
            turn_policy=turn_policy,
            settle_ignored_sources=AsyncMock(),
        ),
    )
    content = {
        "msgtype": "m.text",
        "body": "relay",
        ORIGINAL_SENDER_KEY: bridge_human,
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }

    router_relay = validator.classify_source(ids["router"].full_id, {"content": content})
    assert router_relay.lane_requester_id == canonical_human
    # A router relay of a human's message reads only the context the relay names.
    assert router_relay.router_relay
    assert router_relay.original_sender_is_human
    assert router_relay.trusted_user_relay
    assert validator.classify_source("@untrusted:localhost", {"content": content}).lane_requester_id == (
        "@untrusted:localhost"
    )
    agent_id = ids["test_agent"]
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$spawn",
            "sender": agent_id.full_id,
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": f"{agent_id.full_id} do work",
                "m.mentions": {"user_ids": [agent_id.full_id]},
                ORIGINAL_SENDER_KEY: bridge_human,
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
        },
    )
    room = nio.MatrixRoom("!room:localhost", agent_id.full_id)

    facts = validator.classify(event)
    assert facts.lane_requester_id == canonical_human
    assert await validator.precheck_event(room, event, facts)
    # The router's visible transcript of human or bot-account audio is display-only history.
    for audio_sender in (bridge_human, "@bridge_bot:localhost"):
        echo_content = {
            ORIGINAL_SENDER_KEY: audio_sender,
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        }
        assert is_router_voice_echo(
            validator.classify_source(ids["router"].full_id, {"content": echo_content}),
            echo_content,
        )

    for non_human_sender in non_human_senders:
        assert validator.classify_source(non_human_sender, None).lane_requester_id == non_human_sender

    self_echo = nio.RoomMessageText.from_dict(
        {
            "event_id": "$self-echo",
            "sender": agent_id.full_id,
            "origin_server_ts": 1234567890,
            "content": {"msgtype": "m.text", "body": "self echo"},
        },
    )
    assert not await validator.precheck_event(room, self_echo, validator.classify(self_echo))

    # A relayed or scheduled bot account stays the requester its access applies to; the internal account never does.
    for non_human_sender, expected_requester in (
        ("@bridge_bot:localhost", "@bridge_bot:localhost"),
        (internal_user_id, agent_id.full_id),
    ):
        for source_kind in (TRUSTED_INTERNAL_RELAY_SOURCE_KIND, SCHEDULED_SOURCE_KIND):
            assert (
                validator.classify_source(
                    agent_id.full_id,
                    {"content": {ORIGINAL_SENDER_KEY: non_human_sender, SOURCE_KIND_KEY: source_kind}},
                ).lane_requester_id
                == expected_requester
            )
        # The router relays a bot account as itself, never as a human's handoff.
        router_relay = validator.classify_source(
            ids["router"].full_id,
            {"content": {ORIGINAL_SENDER_KEY: non_human_sender, SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND}},
        )
        assert router_relay.router_relay is (non_human_sender == "@bridge_bot:localhost")
        assert not router_relay.original_sender_is_human
        assert not router_relay.trusted_user_relay


def _validator(tmp_path: Path) -> tuple[IngressValidator, dict[str, MatrixID]]:
    """Return a validator over a registry with a router and two agents."""
    config = bind_runtime_paths(
        Config(
            agents={"general": {"display_name": "General"}, "research": {"display_name": "Research"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, runtime_paths))
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["general"],
            turn_store=MagicMock(),
            turn_policy=MagicMock(),
            settle_ignored_sources=AsyncMock(),
        ),
    )
    return validator, ids


@pytest.mark.asyncio
@pytest.mark.parametrize("may_reply", [True, False])
async def test_a_reply_decision_settles_what_it_refuses_before_the_fence_opens(tmp_path: Path, may_reply: bool) -> None:
    """A decision whose requester lost reply access settles its sources while a configuration change still waits."""
    validator, _ids = _validator(tmp_path)
    gate = validator.deps.runtime.response_admission_gate
    validator.deps.turn_policy.can_reply_to_sender_in_room.return_value = may_reply
    held_while_settling: list[int] = []

    async def settle(event_ids: tuple[str, ...]) -> None:
        held_while_settling.append(gate.in_flight_response_count)
        assert event_ids == ("$source",)

    validator = IngressValidator(replace(validator.deps, settle_ignored_sources=settle))

    async with validator.admitted_reply_decision(
        "@user:localhost",
        "!room:localhost",
        unanswered=("$source",),
    ) as allowed:
        assert gate.in_flight_response_count == 1
        assert gate.close_if_idle() is False

    assert allowed is may_reply
    assert held_while_settling == ([] if may_reply else [1])
    assert gate.in_flight_response_count == 0
    validator.deps.turn_policy.can_reply_to_sender_in_room.assert_called_once_with("@user:localhost", "!room:localhost")


def _text(sender: str, body: str, **content: object) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": "$text",
            "sender": sender,
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": body, **content},
        },
    )


def test_only_a_managed_sender_supplies_hook_metadata(tmp_path: Path) -> None:
    """A person's message carrying hook metadata is an ordinary turn; the same content from an entity is a hook's."""
    validator, ids = _validator(tmp_path)
    hook_content = {HOOK_SOURCE_KEY: "automation/dreaming", HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2}

    person = validator.classify(_text("@user:localhost", "hello", **hook_content))
    entity = validator.classify(_text(ids["research"].full_id, "hello", **hook_content))

    assert (person.hook_source, person.message_received_depth) == (None, 0)
    assert (entity.hook_source, entity.message_received_depth) == ("automation/dreaming", 2)


def test_classification_keeps_the_four_identities_apart(tmp_path: Path) -> None:
    """The physical sender, lane requester, run requester, and relay bits each answer their own question."""
    validator, ids = _validator(tmp_path)
    research = ids["research"].full_id
    router = ids["router"].full_id

    agent_reply = validator.classify(
        _text(research, "@general over to you", **{ACTING_REQUESTER_KEY: "@user:localhost"}),
    )
    assert (agent_reply.physical_sender, agent_reply.lane_requester_id, agent_reply.run_requester_id) == (
        research,
        research,
        "@user:localhost",
    )
    assert agent_reply.acts_for_requester
    assert agent_reply.origin.acting_sender_id == research

    human_relay = validator.classify(
        _text(
            router,
            "@general help",
            **{ORIGINAL_SENDER_KEY: "@user:localhost", SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND},
        ),
    )
    assert (human_relay.physical_sender, human_relay.run_requester_id) == (router, "@user:localhost")
    assert human_relay.router_relay
    assert human_relay.trusted_user_relay
    assert human_relay.origin.intent is TurnIntent.ROUTER_HANDOFF


def test_a_room_level_router_handoff_names_the_human_message_it_relays(tmp_path: Path) -> None:
    """A room-mode handoff, which clients do not show as a reply, still aliases the human message it relays."""
    validator, ids = _validator(tmp_path)
    relay = _text(
        ids["router"].full_id,
        "@general could you help with this?",
        **{
            ORIGINAL_SENDER_KEY: "@user:localhost",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            "m.relates_to": {"io.mindroom.in_reply_to": {"event_id": "$human"}},
        },
    )

    assert validator.classify(relay).discovery_event_id == "$human"


def _entity_final_reply_with_tool_markers(validator: IngressValidator, sender: str) -> nio.RoomMessageFormatted:
    """Return an entity's completed reply, delivered as an edit of its placeholder, whose text follows tool markers."""
    body = "🔧 `search` [1]\n\n!help"
    edit = nio.RoomMessageText.from_dict(
        {
            "event_id": "$final",
            "sender": sender,
            "origin_server_ts": 1,
            "content": {
                "msgtype": "m.text",
                "body": f"* {body}",
                "m.new_content": {
                    "msgtype": "m.text",
                    "body": body,
                    STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
                    TOOL_TRACE_CONTENT_KEY: {"version": 2, "events": []},
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$placeholder"},
                STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
            },
        },
    )
    final_reply = validator.entity_final_reply(edit)
    assert final_reply is not None
    return final_reply


def _sidecar_text(sender: str) -> nio.RoomMessageFile:
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar",
            "sender": sender,
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "!help [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
                "url": "mxc://server/sidecar",
            },
        },
    )
    assert isinstance(event, nio.RoomMessageFile)
    return event


_COMMAND = "command"
_CONVERSATION = "conversation"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("plain message", _COMMAND),
        ("agent-written message", _COMMAND),
        ("sidecar text", _COMMAND),
        ("finished reply with tool markers", _COMMAND),
        # A plugin or an agent can write this text, yet it would run with a person's authority.
        ("hook", _CONVERSATION),
        ("hook_dispatch", _CONVERSATION),
        ("external_trigger", _CONVERSATION),
        ("trusted_internal_relay", _CONVERSATION),
        ("scheduled", _CONVERSATION),
        ("silent_scheduled", _CONVERSATION),
        ("untrusted scheduled claim", _COMMAND),
        ("voice transcript", _CONVERSATION),
        ("voice raw-audio fallback", _CONVERSATION),
        ("image caption", _CONVERSATION),
        ("file caption", _CONVERSATION),
        ("interactive selection", _CONVERSATION),
        ("edit regeneration", _CONVERSATION),
        ("batch of two messages", _CONVERSATION),
    ],
)
def test_command_rule_per_source_kind(tmp_path: Path, case: str, expected: str) -> None:
    """One rule decides, per source kind, whether a turn's canonical text is a command."""
    validator, ids = _validator(tmp_path)
    research = ids["research"].full_id
    human = "@user:localhost"
    source_count = 1
    command_text = "!help"
    automation_kinds = {
        "hook": HOOK_SOURCE_KIND,
        "hook_dispatch": HOOK_DISPATCH_SOURCE_KIND,
        "external_trigger": EXTERNAL_TRIGGER_SOURCE_KIND,
        "trusted_internal_relay": TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        "scheduled": SCHEDULED_SOURCE_KIND,
        "silent_scheduled": SILENT_SCHEDULE_SOURCE_KIND,
    }
    explicit_kinds = {
        "voice transcript": (VOICE_SOURCE_KIND, "🎤 !help"),
        "voice raw-audio fallback": (VOICE_SOURCE_KIND, "🎤 !help"),
        "image caption": (IMAGE_SOURCE_KIND, "!help"),
        "file caption": (MEDIA_SOURCE_KIND, "!help"),
        "interactive selection": (INTERACTIVE_SELECTION_SOURCE_KIND, "!help"),
        "edit regeneration": (EDIT_SOURCE_KIND, "!help"),
    }
    if case in automation_kinds:
        content: dict[str, object] = {SOURCE_KIND_KEY: automation_kinds[case], ORIGINAL_SENDER_KEY: human}
        facts = validator.classify(_text(ids["router"].full_id, command_text, **content))
    elif case in explicit_kinds:
        source_kind, command_text = explicit_kinds[case]
        facts = validator.classify(_text(human, command_text), source_kind=source_kind)
    elif case == "agent-written message":
        facts = validator.classify(_text(research, command_text, **{ACTING_REQUESTER_KEY: human}))
    elif case == "sidecar text":
        # A long text sent as a file is text; its full text arrives with normalization.
        facts = validator.classify(_sidecar_text(human))
    elif case == "finished reply with tool markers":
        final_reply = _entity_final_reply_with_tool_markers(validator, research)
        facts = validator.classify(final_reply)
        assert final_reply.body.startswith("🔧")
        command_text = dispatch_prompt_for_event(final_reply)
    elif case == "untrusted scheduled claim":
        facts = validator.classify(_text(human, command_text, **{SOURCE_KIND_KEY: SCHEDULED_SOURCE_KIND}))
    else:
        facts = validator.classify(_text(human, command_text))
        source_count = 2 if case == "batch of two messages" else 1

    command = command_for(facts, command_text, source_count)

    assert (_COMMAND if command is not None else _CONVERSATION) == expected
    if command is not None:
        assert command.type is CommandType.HELP


def test_transport_kind_outranks_content_and_the_router_acts_for_no_one(tmp_path: Path) -> None:
    """Media, voice, and edits keep the kind their transport fixes, and only a non-router entity acts for a requester."""
    validator, ids = _validator(tmp_path)
    content = {SOURCE_KIND_KEY: HOOK_SOURCE_KIND, ACTING_REQUESTER_KEY: "@user:localhost"}

    image = validator.classify(_text(ids["research"].full_id, "chart", **content), source_kind=IMAGE_SOURCE_KIND)
    router = validator.classify(
        _text(ids["router"].full_id, "@general over to you", **{ACTING_REQUESTER_KEY: "@user:localhost"}),
    )

    assert image.source_kind == IMAGE_SOURCE_KIND
    assert router.acting_requester_id is None
    assert router.run_requester_id == ids["router"].full_id
