"""Room power levels across room versions, including room version 12's privileged creators."""

from __future__ import annotations

import math

import pytest

from mindroom.matrix.room_power import has_privileged_creators, privileged_creators, user_power_level


@pytest.mark.parametrize(
    ("room_version", "privileged"),
    [
        ("1", False),
        ("10", False),
        ("11", False),
        ("12", True),
        ("13", True),
        ("org.matrix.hydra.11", True),
        ("x", False),
    ],
)
def test_only_room_version_12_and_later_privilege_creators(room_version: str, privileged: bool) -> None:
    """Version 12 and its hydra preview give creators unlimited power; older and unknown versions do not."""
    assert has_privileged_creators(room_version) is privileged


def test_a_version_12_create_event_names_its_sender_and_additional_creators() -> None:
    """The creators are the create event's sender plus additional_creators."""
    create_event = {
        "sender": "@router:example.com",
        "content": {"room_version": "12", "additional_creators": ["@owner:example.com", 7]},
    }

    assert privileged_creators(create_event) == {"@router:example.com", "@owner:example.com"}


@pytest.mark.parametrize("content", [{"room_version": "11"}, {}, "not a mapping"])
def test_older_rooms_have_no_privileged_creators(content: object) -> None:
    """A pre-12 or versionless create event privileges nobody."""
    assert privileged_creators({"sender": "@router:example.com", "content": content}) == frozenset()


def test_user_power_level_gives_creators_unlimited_power() -> None:
    """A privileged creator outranks every listed level; others fall back to users_default."""
    content = {"users": {"@admin:example.com": 100, "@flag:example.com": True}, "users_default": 10}
    creators = frozenset({"@router:example.com"})

    assert user_power_level(content, "@router:example.com", creators) == math.inf
    assert user_power_level(content, "@admin:example.com", creators) == 100
    assert user_power_level(content, "@flag:example.com", creators) == 10
    assert user_power_level({}, "@anyone:example.com") == 0
