"""Regression coverage for the generated MindRoom Chat UI wire contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, get_args, get_type_hints

import pytest

from mindroom.custom_tools.chat_ui import ChatUITools
from tests.chat_ui_contract_fixture import build_chat_ui_contract, serialize_chat_ui_contract

if TYPE_CHECKING:
    from pathlib import Path


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
            *(f"open_settings/{section}" for section in get_args(get_type_hints(ChatUITools.open_settings)["section"])),
            *(f"open_panel/{panel}" for panel in get_args(get_type_hints(ChatUITools.open_panel)["panel"])),
        )
    }

    assert contract["version"] == 1
    assert contract["viewer_id"] == "@alice:localhost"
    assert contract["room_id"] == "!room:localhost"
    assert {case["id"] for case in cases} == expected_case_ids
    assert len(cases) == len(expected_case_ids)
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
async def test_contract_rejects_unmapped_registered_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a public tool must require a corresponding client contract case."""
    original_init = ChatUITools.__init__

    def init_with_new_action(self: ChatUITools) -> None:
        original_init(self)
        self.register(self.show_computer, name="new_action")

    monkeypatch.setattr(ChatUITools, "__init__", init_with_new_action)

    with pytest.raises(RuntimeError, match="new_action"):
        await build_chat_ui_contract(tmp_path)


@pytest.mark.asyncio
async def test_contract_serialization_is_deterministic_and_formatter_compatible(tmp_path: Path) -> None:
    """Independent exports must produce the same canonical pretty-printed JSON bytes."""
    first = serialize_chat_ui_contract(await build_chat_ui_contract(tmp_path / "first"))
    second = serialize_chat_ui_contract(await build_chat_ui_contract(tmp_path / "second"))

    assert first == second
    assert first.endswith("\n")
    assert json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n" == first
