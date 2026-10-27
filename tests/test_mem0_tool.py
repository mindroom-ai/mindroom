"""Tests for the standalone Mem0 toolkit against Mem0 2's entity-filter API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from mem0.exceptions import LLMError
from mem0.memory.main import _reject_top_level_entity_params

from mindroom.tools.mem0 import mem0_tools


class _Mem0Client:
    """Answer like Mem0 2, which takes the user of a search or listing only inside ``filters``."""

    def __init__(self) -> None:
        self.filters: dict[str, object] = {}
        self.top_level: dict[str, dict[str, object]] = {}

    def add(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, list[dict[str, str]]]:
        self.top_level["add"] = kwargs
        return {"results": [{"id": "m2", "memory": messages[-1]["content"], "event": "ADD"}]}

    def delete_all(self, **kwargs: object) -> dict[str, str]:
        self.top_level["delete_all"] = kwargs
        return {"message": "Memories deleted successfully!"}

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
    """Every toolkit function works on Mem0 2, local or platform; search and get-all pass the user as a filter."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    client = _Mem0Client()
    with (
        patch("agno.tools.mem0.Memory", return_value=client),
        patch("agno.tools.mem0.MemoryClient", return_value=client),
    ):
        toolkit = mem0_tools()(user_id="alice", api_key=api_key)
    # The toolkit reads its configured user and never this run's context.
    run_context: Any = None

    added = toolkit.add_memory(run_context, "Likes green tea")
    found = toolkit.search_memory(run_context, "tea")
    listed = toolkit.get_all_memories(run_context)
    deleted = toolkit.delete_all_memories(run_context)

    assert json.loads(added) == {"results": [{"id": "m2", "memory": "Likes green tea", "event": "ADD"}]}
    assert json.loads(found) == [{"id": "m1", "memory": "Likes green tea"}]
    assert json.loads(listed) == [{"id": "m1", "memory": "Likes green tea"}]
    assert deleted == "Successfully deleted all memories for user_id: alice."
    assert client.filters == {"search tea": {"user_id": "alice"}, "get_all": {"user_id": "alice"}}
    # Mem0 2 still takes the user of an add or a delete-all as a top-level argument.
    assert client.top_level == {"add": {"user_id": "alice", "infer": True}, "delete_all": {"user_id": "alice"}}


class _FailingMem0Client(_Mem0Client):
    """Fail like Mem0 2.2 when the provider rejects the key: its error carries the provider's error body."""

    def __init__(self, rejected_key: str) -> None:
        super().__init__()
        self._rejected_key = rejected_key

    def add(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, list[dict[str, str]]]:
        del messages, kwargs
        msg = f"LLM extraction failed: Error code: 401 - invalid x-api-key {self._rejected_key}"
        raise LLMError(msg)

    def search(self, query: str, **kwargs: object) -> dict[str, list[dict[str, str]]]:
        del query, kwargs
        msg = f"Invalid query for key {self._rejected_key}"
        raise ValueError(msg)


def test_mem0_tool_failures_reach_the_model_without_the_rejected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed provider call's text reaches the model with credentials redacted and Agno's usual framing."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    rejected_key = "sk-ant-" + "api03-" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8Ba7"
    with patch("agno.tools.mem0.Memory", return_value=_FailingMem0Client(rejected_key)):
        toolkit = mem0_tools()(user_id="alice")
    run_context: Any = None

    added = toolkit.add_memory(run_context, "Likes green tea")
    found = toolkit.search_memory(run_context, "tea")

    assert added.startswith("Error adding memory: LLM extraction failed: Error code: 401 - invalid x-api-key ")
    # Agno returns a ValueError's text without its error prefix, which stays so.
    assert found.startswith("Invalid query for key ")
    assert rejected_key not in added
    assert rejected_key not in found
