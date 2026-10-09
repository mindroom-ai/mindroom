"""Tests for the note about agents a managed room's configuration does not list."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.managed_room_notice import managed_room_join_notice, managed_room_notice
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_SELF = (
    "This room is managed in the MindRoom configuration, and I'm not one of its agents, so I can't answer here. "
    "To talk to me, create a new room and invite me."
)


@pytest.mark.parametrize(
    ("available", "expected_suffix"),
    [
        ([], ""),
        (["Research"], " In this room, you can ask Research."),
        (["Research", "Ops"], " In this room, you can ask Research or Ops."),
        (["Research", "Ops", "Code", "Docs"], " In this room, you can ask Research, Ops, Code, or Docs."),
    ],
)
def test_self_notice_names_every_agent_the_reader_can_ask(available: list[str], expected_suffix: str) -> None:
    assert managed_room_notice(available=available) == _SELF + expected_suffix


def test_notice_about_several_unavailable_agents_uses_plural_wording() -> None:
    assert managed_room_notice(available=["Research"], unavailable=["General", "Mind"]) == (
        "This room is managed in the MindRoom configuration, and General and Mind aren't among its agents, "
        "so they can't answer here. To talk to them, create a new room and invite them. "
        "In this room, you can ask Research."
    )


def test_router_never_posts_the_join_notice(tmp_path: Path) -> None:
    room_id = "!managed:localhost"
    config = bind_runtime_paths(
        Config(agents={"research": AgentConfig(display_name="Research", rooms=[room_id])}),
        test_runtime_paths(tmp_path),
    )
    room = MagicMock(room_id=room_id)
    room.canonical_alias = None

    notice = managed_room_join_notice(
        room,
        agent_name=ROUTER_AGENT_NAME,
        agent_user_id="@mindroom_router:localhost",
        inviter="@inviter:localhost",
        config=config,
        runtime_paths=runtime_paths_for(config),
        membership_index=MagicMock(),
    )

    assert notice is None
