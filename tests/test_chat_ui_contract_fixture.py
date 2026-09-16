"""Regression coverage for the generated MindRoom Chat UI wire contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.chat_ui_contract_fixture import build_chat_ui_contract, serialize_chat_ui_contract

if TYPE_CHECKING:
    from pathlib import Path


SETTINGS_SECTIONS = (
    "general",
    "account",
    "notifications",
    "devices",
    "emojis-stickers",
    "developer",
    "about",
)


@pytest.mark.asyncio
async def test_contract_exports_every_action_for_room_and_thread_scope(tmp_path: Path) -> None:
    """Dropping an action, Settings destination, or scope must break the exported contract."""
    contract = await build_chat_ui_contract(tmp_path)
    cases = contract["cases"]
    expected_case_ids = {
        f"{scope}/{case}"
        for scope in ("thread", "room")
        for case in (
            "show_computer",
            *(f"open_settings/{section}" for section in SETTINGS_SECTIONS),
            "open_panel/members",
        )
    }

    assert contract["version"] == 1
    assert contract["viewer_id"] == "@alice:localhost"
    assert contract["room_id"] == "!room:localhost"
    assert {case["id"] for case in cases} == expected_case_ids
    assert len(cases) == 18
    assert all(case["event"]["sender"] == "@mindroom_researcher:localhost" for case in cases)

    for case in cases:
        content = case["event"]["content"]
        metadata = content["io.mindroom.ui_action"]
        if case["id"].startswith("thread/"):
            assert metadata["thread_id"] == "$thread"
            assert content["m.relates_to"]["rel_type"] == "m.thread"
            assert content["m.relates_to"]["event_id"] == "$thread"
        else:
            assert metadata["thread_id"] is None
            assert "m.relates_to" not in content


@pytest.mark.asyncio
async def test_contract_serialization_is_deterministic_and_formatter_compatible(tmp_path: Path) -> None:
    """Independent exports must produce the same canonical pretty-printed JSON bytes."""
    first = serialize_chat_ui_contract(await build_chat_ui_contract(tmp_path / "first"))
    second = serialize_chat_ui_contract(await build_chat_ui_contract(tmp_path / "second"))

    assert first == second
    assert first.endswith("\n")
    assert json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n" == first
