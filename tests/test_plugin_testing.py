"""A plugin's own tests load it with `mindroom.plugin_testing` and drive its routes, tools and hooks as MindRoom does.

These tests import only the kit, pytest and the example plugin, as a plugin's tests would.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.plugin_testing import LoadedPlugin, TurnRefused, load_plugin

if TYPE_CHECKING:
    from collections.abc import Iterator

# Turn authorization stays as a deployment has it, without MindRoom's own test bypass.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

EXAMPLE = Path(__file__).parent / "fixtures" / "plugin_testing_example"
ROOM = "!lobby:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CONFIG = {"agents": {"helper": {"display_name": "Helper", "tools": ["greeter"], "access": {"users": [ALICE]}}}}
SETTINGS = {"greeting": "Howdy"}
HELLO = f"/api/plugins/greeter/rooms/{ROOM}/hello"


@pytest.fixture
def plugin(tmp_path: Path) -> Iterator[LoadedPlugin]:
    """The example plugin, loaded for the test, with Alice and Bob joined to the lobby."""
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as loaded:
        loaded.add_room(ROOM, members={ALICE: "Alice", BOB: "Bob"})
        yield loaded


def test_route_answers_a_signed_in_member_only(plugin: LoadedPlugin) -> None:
    """A member gets 200, a client without a session 401, a non-member 403, and a signed-out session 401."""
    alice = plugin.client(ALICE)

    assert alice.get(HELLO).json() == {"hello": ALICE}
    assert plugin.client().get(HELLO).status_code == 401
    assert plugin.client("@carol:example.org").get(HELLO).status_code == 403
    plugin.sign_out(ALICE)
    assert alice.get(HELLO).status_code == 401


def test_a_room_is_direct_as_running_agents_tell_and_no_load_sees_another_ones_answer(tmp_path: Path) -> None:
    """A room its members joined as a direct message is one, and so is any room of two; each load asks afresh."""
    carol = "@carol:example.org"
    cases = [
        ({ALICE: "Alice", BOB: "Bob", carol: None}, True, True),
        ({ALICE: "Alice", BOB: "Bob", carol: None}, False, False),
        ({ALICE: "Alice", BOB: "Bob"}, False, True),
    ]
    for index, (members, direct, expected) in enumerate(cases):
        with load_plugin(EXAMPLE, tmp_path / str(index), config=CONFIG, settings=SETTINGS) as plugin:
            plugin.add_room(ROOM, members=members, direct=direct)

            assert plugin.client(ALICE).get(f"/api/plugins/greeter/rooms/{ROOM}/direct").json() == {"direct": expected}


@pytest.mark.asyncio
async def test_tool_call_answers_with_the_plugin_settings_and_the_gate_declines(plugin: LoadedPlugin) -> None:
    """An admitted sender's call returns the tool's value, which reads the settings; the gate hook declines a call."""
    greeted = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)
    declined = await plugin.call_tool("greeter", "greet", {"name": "Stranger"}, agent="helper", sender=ALICE, room=ROOM)

    assert (greeted.status, greeted.value) == ("ok", "Howdy, Ann! (greeting 1)")
    assert declined.status == "declined"
    assert "Strangers are not greeted." in declined.failure_text


@pytest.mark.asyncio
async def test_message_fires_the_hook_whose_send_lands_in_the_room(tmp_path: Path) -> None:
    """An admitted sender's message runs the `message:received` hook, and what it sends lands in the room's `sent`."""
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as plugin:
        room = plugin.add_room(ROOM, members={ALICE: "Alice"})

        await plugin.fire("message:received", agent="helper", sender=ALICE, room=ROOM, body="hi")

    assert [(sent.text, sent.source) for sent in room.sent] == [("Got: hi", "greeter:message:received")]


@pytest.mark.asyncio
async def test_a_sender_the_agent_does_not_admit_is_refused_and_nothing_runs(tmp_path: Path) -> None:
    """Bob, whom the agent's access does not admit, and Alice outside the room are refused a call and a message."""
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as plugin:
        room = plugin.add_room(ROOM, members={ALICE: "Alice", BOB: "Bob"})
        plugin.add_room("!elsewhere:example.org", members={BOB: "Bob"})

        for sender, room_id in ((BOB, ROOM), (ALICE, "!elsewhere:example.org")):
            with pytest.raises(TurnRefused):
                await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=sender, room=room_id)
            with pytest.raises(TurnRefused):
                await plugin.fire("message:received", agent="helper", sender=sender, room=room_id, body="hi")
        greeted = await plugin.call_tool("greeter", "greet", {"name": "Ann"}, agent="helper", sender=ALICE, room=ROOM)

    assert room.sent == []
    assert greeted.value == "Howdy, Ann! (greeting 1)"


def test_the_block_restores_the_published_plugins_and_their_modules(tmp_path: Path) -> None:
    """Once the block ends, the plugin modules in `sys.modules` are those before it, and the plugin serves no route."""

    def plugin_modules() -> dict[str, object]:
        return {name: module for name, module in sys.modules.items() if name.startswith("mindroom_plugin_")}

    before = plugin_modules()
    with load_plugin(EXAMPLE, tmp_path, config=CONFIG, settings=SETTINGS) as plugin:
        plugin.add_room(ROOM, members={ALICE: "Alice"})
        alice = plugin.client(ALICE)
        assert alice.get(HELLO).status_code == 200
        assert plugin_modules() != before

    assert plugin_modules() == before
    assert alice.get(HELLO).status_code == 404


def test_a_plugin_that_fails_to_load_raises_its_error(tmp_path: Path) -> None:
    """A plugin whose module raises fails the load, naming that error."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "mindroom.plugin.json").write_text('{"name": "broken", "tools_module": "tools.py"}', encoding="utf-8")
    (broken / "tools.py").write_text("raise RuntimeError('broken on import')\n", encoding="utf-8")

    with pytest.raises(ValueError, match="broken on import"), load_plugin(broken, tmp_path / "runtime"):
        pass
