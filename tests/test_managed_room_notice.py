"""Tests for the note about agents a managed room's configuration does not list."""

from __future__ import annotations

import pytest

from mindroom.managed_room_notice import managed_room_notice

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
    """The notice lists every agent the reader can ask in the room."""
    assert managed_room_notice(available=available) == _SELF + expected_suffix


def test_notice_about_several_unavailable_agents_uses_plural_wording() -> None:
    """Several unavailable agents are named together, with plural wording."""
    assert managed_room_notice(available=["Research"], unavailable=["General", "Mind"]) == (
        "This room is managed in the MindRoom configuration, and General and Mind aren't among its agents, "
        "so they can't answer here. To talk to them, create a new room and invite them. "
        "In this room, you can ask Research."
    )
