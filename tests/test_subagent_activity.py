"""The `subagent:changed` hook event: its names, snapshot types, plugin API version, and `plugin_testing` support."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.hooks.types import _DEFAULT_EVENT_TIMEOUT_MS, BUILTIN_EVENT_NAMES, validate_event_name
from mindroom.plugin_api import (
    EVENT_SUBAGENT_CHANGED,
    PLUGIN_API_VERSION,
    SubagentChangedContext,
    SubagentItem,
)
from mindroom.plugin_testing import load_plugin
from tests.subagent_activity_helpers import make_snapshot

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
CONFIG = {"agents": {"helper": {"display_name": "Helper", "access": {"users": [ALICE]}}}}


def test_subagent_changed_is_a_builtin_observer_with_a_500_ms_timeout() -> None:
    """The event is built in, observes with a 500 ms timeout, and its namespace is reserved."""
    assert EVENT_SUBAGENT_CHANGED == "subagent:changed"
    assert EVENT_SUBAGENT_CHANGED in BUILTIN_EVENT_NAMES
    assert _DEFAULT_EVENT_TIMEOUT_MS[EVENT_SUBAGENT_CHANGED] == 500
    with pytest.raises(ValueError, match="reserved"):
        validate_event_name("subagent:custom")


def test_plugin_api_version_is_1_30() -> None:
    """The plugin API version names the release that added the event."""
    assert PLUGIN_API_VERSION == (1, 30)


def test_snapshot_items_are_frozen() -> None:
    """Snapshots and their items are immutable values."""
    snapshot = make_snapshot(items=(SubagentItem(kind="text", text="hi"),))
    with pytest.raises(AttributeError):
        snapshot.status = "failed"  # type: ignore[misc]
    assert snapshot.items[0].tool is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_plugin_testing_fires_subagent_changed(tmp_path: Path) -> None:
    """`fire` runs a plugin's `subagent:changed` hooks with the snapshot and returns the context."""
    plugin_dir = tmp_path / "watcher"
    plugin_dir.mkdir()
    (plugin_dir / "mindroom.plugin.json").write_text(
        '{"name": "watcher", "hooks_module": "hooks.py"}',
        encoding="utf-8",
    )
    (plugin_dir / "hooks.py").write_text(
        "from pathlib import Path\n"
        "from mindroom.plugin_api import hook\n"
        "@hook('subagent:changed')\n"
        "async def seen(ctx):\n"
        "    with Path(__file__).with_name('seen.txt').open('a') as f:\n"
        "        f.write(ctx.subagent.delegation_id + '\\n')\n",
        encoding="utf-8",
    )
    with load_plugin(plugin_dir, tmp_path / "runtime", config=CONFIG) as plugin:
        plugin.add_room(ROOM, members={ALICE: "Alice"})
        context = await plugin.fire(
            "subagent:changed",
            agent="helper",
            sender=ALICE,
            room=ROOM,
            subagent=make_snapshot(),
        )
        assert isinstance(context, SubagentChangedContext)
        assert context.event_name == EVENT_SUBAGENT_CHANGED
        assert context.subagent.delegation_id == "d1"
        assert (plugin_dir / "seen.txt").read_text(encoding="utf-8").split() == ["d1"]
