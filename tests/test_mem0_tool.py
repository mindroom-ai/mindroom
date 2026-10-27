"""Tests for the standalone Mem0 toolkit against Mem0 2's entity-filter API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from mem0.memory.main import _reject_top_level_entity_params

from mindroom.tools.mem0 import mem0_tools


class _Mem0Client:
    """Answer like Mem0 2, which takes the user of a search or listing only inside ``filters``."""

    def __init__(self) -> None:
        self.filters: dict[str, object] = {}

    def search(self, query: str, **kwargs: object) -> dict[str, list[dict[str, str]]]:
        _reject_top_level_entity_params(kwargs, "search")
        self.filters[f"search {query}"] = kwargs["filters"]
        return {"results": [{"id": "m1", "memory": "Likes green tea"}]}

    def get_all(self, **kwargs: object) -> dict[str, list[dict[str, str]]]:
        _reject_top_level_entity_params(kwargs, "get_all")
        self.filters["get_all"] = kwargs["filters"]
        return {"results": [{"id": "m1", "memory": "Likes green tea"}]}


@pytest.mark.parametrize("api_key", [None, "m0-platform-key"], ids=["local", "platform"])
def test_mem0_tool_search_and_listing_pass_the_user_as_a_filter(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
) -> None:
    """The toolkit's search and get-all return memories from Mem0 2, local or platform, instead of its error text."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    client = _Mem0Client()
    with (
        patch("agno.tools.mem0.Memory", return_value=client),
        patch("agno.tools.mem0.MemoryClient", return_value=client),
    ):
        toolkit = mem0_tools()(user_id="alice", api_key=api_key)
    # The toolkit reads its configured user and never this run's context.
    run_context: Any = None

    found = toolkit.search_memory(run_context, "tea")
    listed = toolkit.get_all_memories(run_context)

    assert json.loads(found) == [{"id": "m1", "memory": "Likes green tea"}]
    assert json.loads(listed) == [{"id": "m1", "memory": "Likes green tea"}]
    assert client.filters == {"search tea": {"user_id": "alice"}, "get_all": {"user_id": "alice"}}
