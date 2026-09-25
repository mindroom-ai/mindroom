"""Regression coverage for the generated Microsoft 365 document-card wire contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.microsoft_365_contract_fixture import build_microsoft_365_contract, serialize_microsoft_365_contract

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_contract_exports_every_card_event_and_scope(tmp_path: Path) -> None:
    """Each card event appears with its exact scope, and only edited cards carry a change."""
    contract = await build_microsoft_365_contract(tmp_path)
    cases = {case["id"]: case["event"]["content"] for case in contract["cases"]}

    assert set(cases) == {
        "thread/connected",
        "room/connected",
        "thread/saved",
        "thread/edited",
        "thread/edited-partial",
    }
    for case_id, content in cases.items():
        metadata = content["io.mindroom.document"]
        assert content["msgtype"] == "m.notice"
        assert metadata["version"] == 1
        assert metadata["event"] == case_id.split("/", 1)[1].removesuffix("-partial")
        assert ("change" in metadata) == metadata["event"].startswith("edited")
        if case_id.startswith("thread/"):
            assert metadata["thread_id"] == "$thread"
            assert content["m.relates_to"]["event_id"] == "$thread"
        else:
            assert metadata["thread_id"] is None
            assert "m.relates_to" not in content
    partial = cases["thread/edited-partial"]["io.mindroom.document"]["change"]
    assert partial["status"] == "partial"
    assert not partial["verified"]
    assert [edit["outcome"] for edit in partial["edits"]] == ["conflict", "applied"]


@pytest.mark.asyncio
async def test_contract_serialization_is_deterministic(tmp_path: Path) -> None:
    """Two exports produce byte-identical fixtures, so the client fixture can be regenerated exactly."""
    first = serialize_microsoft_365_contract(await build_microsoft_365_contract(tmp_path / "first"))
    second = serialize_microsoft_365_contract(await build_microsoft_365_contract(tmp_path / "second"))

    assert first == second
