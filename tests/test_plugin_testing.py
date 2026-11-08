"""A plugin's own tests load it with `mindroom.plugin_testing` and drive its routes, tools and hooks as MindRoom does.

These tests import only the kit, the plugin API, pytest and the example plugin, as a plugin's tests would.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.plugin_api import config_plugins, entity_directory
from mindroom.plugin_testing import LoadedPlugin, TurnRefused, load_plugin

if TYPE_CHECKING:
    from collections.abc import Iterator

# Turn authorization stays as a deployment has it, without MindRoom's own test bypass.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

EXAMPLE = Path(__file__).parent / "fixtures" / "plugin_testing_example"
ROOM = "!lobby:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CAROL = "@carol:example.org"
CONFIG = {"agents": {"helper": {"display_name": "Helper", "tools": ["greeter"], "access": {"users": [ALICE]}}}}
# An agent that admits the members of the room its config names by the key `lobby`.
HOST_CONFIG = {"agents": {"host": {"display_name": "Host", "tools": ["greeter"], "rooms": ["lobby"]}}}
SETTINGS = {"greeting": "Howdy"}
ROUTES = f"/api/plugins/greeter/rooms/{ROOM}"


@pytest.fixture
def plugin(tmp_path: Path) -> Iterator[LoadedPlugin]:
    """The example plugin, loaded for the test, with no rooms yet."""
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as loaded:
        yield loaded


def test_route_answers_a_signed_in_member_only(plugin: LoadedPlugin) -> None:
    """A member gets 200, no session 401, a non-member 403, a signed-out session 401, and a room no bot is in 503."""
    plugin.add_room(ROOM, members={ALICE: "Alice", BOB: "Bob"})
    alice = plugin.client(ALICE)

    assert alice.get(f"{ROUTES}/hello").json() == {"hello": ALICE}
    assert plugin.client().get(f"{ROUTES}/hello").status_code == 401
    assert plugin.client(CAROL).get(f"{ROUTES}/hello").status_code == 403
    assert alice.get("/api/plugins/greeter/rooms/!unknown:example.org/hello").status_code == 503
    plugin.sign_out(ALICE)
    assert alice.get(f"{ROUTES}/hello").status_code == 401


def test_a_route_posts_as_the_router_and_streams_to_a_member(plugin: LoadedPlugin) -> None:
    """`RoomMessenger` posts land in the room's `sent`, a relay to a person is refused, and a stream ends at sign-out.

    A client keeps working after a `with` block of its own, and its streams still end when its user signs out.
    """
    directory = entity_directory(plugin.config, plugin.runtime_paths)
    (helper,) = (user for user, entity in directory.items() if entity.name == "helper")
    room = plugin.add_room(ROOM, members={ALICE: "Alice", helper: "Helper"})

    with plugin.client(ALICE) as alice:
        posted = alice.post(f"{ROUTES}/notice")
        relayed = alice.post(f"{ROUTES}/relay", params={"mention": helper})
        relayed_to_a_person = alice.post(f"{ROUTES}/relay", params={"mention": ALICE})
        with alice.websocket_connect(f"{ROUTES}/stream") as stream:
            greeting = stream.receive_text()
    with alice.websocket_connect(f"{ROUTES}/stream") as stream:
        stream.receive_text()
        plugin.sign_out(ALICE)
        ended = stream.receive()

    assert (posted.status_code, relayed.status_code, relayed_to_a_person.status_code) == (200, 200, 400)
    assert [(sent.text, sent.source, sent.mentions) for sent in room.sent] == [
        (f"{ALICE} says hello", "greeter:http", []),
        (f"Sent for {ALICE}:\n\nhello", "greeter:http", [helper]),
    ]
    assert greeting == f"hello {ALICE}"
    assert (ended["type"], ended["code"]) == ("websocket.close", 4401)


def test_a_route_reads_the_room_as_set_up(plugin: LoadedPlugin) -> None:
    """A route reads the room's power levels, history visibility and the agents its caller may use, as set up."""
    directory = entity_directory(plugin.config, plugin.runtime_paths)
    (helper,) = (user for user, entity in directory.items() if entity.name == "helper")
    plugin.add_room(
        ROOM,
        members={ALICE: "Alice", BOB: "Bob", helper: "Helper"},
        power_levels={"users": {ALICE: 100}},
        history_visibility="joined",
    )

    about = {user: plugin.client(user).get(f"{ROUTES}/about").json() for user in (ALICE, BOB)}

    assert about[ALICE] == {"direct": False, "admin": True, "history": "joined", "agents": ["helper"]}
    assert (about[BOB]["admin"], about[BOB]["agents"]) == (False, [])


def test_a_room_is_direct_as_running_agents_tell_and_no_load_sees_another_ones_answer(tmp_path: Path) -> None:
    """A room its members joined as a direct message is one, as is one member alone with the router; loads never share."""
    cases = [
        ({ALICE: "Alice", BOB: "Bob"}, True, True),
        ({ALICE: "Alice", BOB: "Bob"}, False, False),
        ({ALICE: "Alice"}, False, True),
    ]
    for index, (members, direct, expected) in enumerate(cases):
        with load_plugin(EXAMPLE, tmp_path / str(index), config=CONFIG, settings=SETTINGS) as plugin:
            plugin.add_room(ROOM, members=members, direct=direct)

            assert plugin.client(ALICE).get(f"{ROUTES}/about").json()["direct"] is expected


@pytest.mark.asyncio
async def test_tool_call_answers_with_the_plugin_settings_and_the_gate_declines(plugin: LoadedPlugin) -> None:
    """An admitted sender's call returns the tool's value, which reads the settings; the gate hook declines a call."""
    plugin.add_room(ROOM, members={ALICE: "Alice", BOB: "Bob"})
    greeted = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)
    declined = await plugin.call_tool("greeter", "greet", {"name": "Stranger"}, agent="helper", sender=ALICE, room=ROOM)

    assert (greeted.status, greeted.value) == ("ok", "Howdy, Ann! (greeting 1)")
    assert declined.status == "declined"
    assert "Strangers are not greeted." in declined.failure_text


@pytest.mark.asyncio
async def test_a_tool_the_agent_lacks_or_one_that_needs_approval_is_refused(tmp_path: Path) -> None:
    """A tool or function the agent lacks raises `LookupError`, and a function that may need approval never runs."""
    config = {
        "agents": {
            **CONFIG["agents"],
            "quiet": {"display_name": "Quiet", "access": {"users": [ALICE]}},
        },
        "tool_approval": {"rules": [{"match": "greet", "action": "require_approval"}]},
    }
    with load_plugin(EXAMPLE, tmp_path, config=config, settings=SETTINGS) as plugin:
        plugin.add_room(ROOM, members={ALICE: "Alice"})

        with pytest.raises(LookupError, match="has no tool 'greeter'"):
            await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="quiet", sender=ALICE, room=ROOM)
        with pytest.raises(LookupError, match="has no function 'wave'"):
            await plugin.call_tool("greeter", "wave", {}, agent="helper", sender=ALICE, room=ROOM)
        with pytest.raises(PermissionError, match="may need approval"):
            await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)


@pytest.mark.asyncio
async def test_a_member_of_a_room_the_agent_serves_is_admitted_by_its_room_key(tmp_path: Path) -> None:
    """An agent admits the members of the rooms its config names, found by the key the test gives a room."""
    with load_plugin(EXAMPLE, tmp_path, config=HOST_CONFIG, settings=SETTINGS) as plugin:
        plugin.add_room(ROOM, members={BOB: "Bob"}, key="lobby")

        greeted = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="host", sender=BOB, room=ROOM)

    assert greeted.status == "ok"


@pytest.mark.asyncio
async def test_a_member_of_a_room_never_added_with_its_key_is_refused(tmp_path: Path) -> None:
    """Without the room key, the agent's room grant admits no one, so the call is refused."""
    with load_plugin(EXAMPLE, tmp_path, config=HOST_CONFIG, settings=SETTINGS) as plugin:
        plugin.add_room(ROOM, members={BOB: "Bob"})

        with pytest.raises(TurnRefused):
            await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="host", sender=BOB, room=ROOM)


@pytest.mark.asyncio
async def test_message_fires_the_hook_whose_send_lands_in_the_room(plugin: LoadedPlugin) -> None:
    """An admitted sender's message runs the `message:received` hook, which reads the room's state and sends to it."""
    room = plugin.add_room(ROOM, members={ALICE: "Alice"})

    await plugin.fire("message:received", agent="helper", sender=ALICE, room=ROOM, body="hi")

    assert [(sent.text, sent.source) for sent in room.sent] == [("Got: hi (shared)", "greeter:message:received")]


@pytest.mark.asyncio
async def test_each_event_returns_what_its_hooks_give_mindroom(plugin: LoadedPlugin) -> None:
    """Enrichment returns its items, a transform the changed draft, and observers and custom events run their hooks."""
    room = plugin.add_room(ROOM, members={ALICE: "Alice"})
    fire = {"agent": "helper", "sender": ALICE, "room": ROOM, "body": "hi"}
    items = await plugin.fire("message:enrich", **fire, target_entity_name="helper", target_member_names=None)
    draft = await plugin.fire(
        "message:before_response",
        **fire,
        response_text="Hello",
        response_kind="ai",
        tool_trace=None,
        extra_content=None,
    )
    final = await plugin.fire("message:final_response_transform", **fire, response_text="Hello", response_kind="ai")
    instructions = await plugin.fire("system:enrich", **fire, target_entity_name="helper", target_member_names=None)
    await plugin.fire(
        "message:after_response",
        **fire,
        response_text="Hello",
        response_event_id="$reply",
        delivery_kind="sent",
        response_kind="ai",
    )
    await plugin.fire("message:cancelled", **fire, failure_reason="stopped")
    waved = await plugin.fire(" greeter:waved ", **fire, payload={"times": 2}, source_plugin="greeter")

    assert [(item.key, item.text) for item in items] == [("greeting", f"Greet {ALICE} with Howdy.")]
    assert [(item.key, item.text) for item in instructions] == [("tone", "Be friendly.")]
    assert draft.response_text == final.response_text == "Hello -- the greeter"
    assert waved.payload == {"times": 2}
    assert [sent.text for sent in room.sent] == [
        "Replied: Hello",
        "Cancelled: stopped",
        f"Waving back at {ALICE} for 2 waves",
    ]
    with pytest.raises(ValueError, match=r"not reaction:received"):
        await plugin.fire("reaction:received", **{**fire, "sender": BOB})


@pytest.mark.asyncio
async def test_a_sender_the_agent_does_not_admit_is_refused_and_nothing_runs(plugin: LoadedPlugin) -> None:
    """Bob, whom the agent's access does not admit, and Alice outside the room are refused a call and a message."""
    room = plugin.add_room(ROOM, members={ALICE: "Alice", BOB: "Bob"})
    plugin.add_room("!elsewhere:example.org", members={BOB: "Bob"})

    for sender, room_id in ((BOB, ROOM), (ALICE, "!elsewhere:example.org"), (ALICE, "!unknown:example.org")):
        with pytest.raises(TurnRefused):
            await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=sender, room=room_id)
        with pytest.raises(TurnRefused):
            await plugin.fire("message:received", agent="helper", sender=sender, room=room_id, body="hi")
    greeted = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)

    assert room.sent == []
    assert greeted.value == "Howdy, Ann! (greeting 1)"


def test_the_block_restores_the_published_plugins_and_their_modules(tmp_path: Path) -> None:
    """Once the block ends, the plugin modules in `sys.modules` are those before it, and the plugin is not loaded."""

    def plugin_modules() -> dict[str, object]:
        return {name: module for name, module in sys.modules.items() if name.startswith("mindroom_plugin_")}

    before = plugin_modules()
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as plugin:
        loaded = [entry.name for entry in config_plugins(plugin.config, plugin.runtime_paths).loaded_plugins]
        assert plugin_modules() != before

    assert loaded == ["greeter"]
    assert plugin_modules() == before
    assert config_plugins(plugin.config, plugin.runtime_paths).loaded_plugins == ()


def test_a_plugin_that_fails_to_load_raises_its_error(tmp_path: Path) -> None:
    """A plugin whose tools, routes or lifecycle module raises fails the load, naming that error."""
    for module in ("tools", "routes", "lifecycle"):
        broken = tmp_path / module
        broken.mkdir()
        manifest = f'{{"name": "broken", "{module}_module": "{module}.py"}}'
        (broken / "mindroom.plugin.json").write_text(manifest, encoding="utf-8")
        (broken / f"{module}.py").write_text(f"raise RuntimeError('broken {module}')\n", encoding="utf-8")

        with pytest.raises(Exception, match=f"broken {module}"), load_plugin(broken, tmp_path / f"{module}-runtime"):
            pass
