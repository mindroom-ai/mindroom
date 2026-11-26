"""Tests for the native Mem0 toolkit against Mem0 2's entity-filter API."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from mem0.exceptions import LLMError
from mem0.memory.main import _reject_top_level_entity_params

from mindroom.tool_system.runtime_context import detached_tool_scope
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
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
    monkeypatch.setenv("MEM0_API_KEY", "host-env-secret")
    client = _Mem0Client()
    with (
        patch("mem0.memory.main.Memory", return_value=client) as local_memory,
        patch("mem0.client.main.MemoryClient", return_value=client) as platform_client,
    ):
        toolkit = mem0_tools()(user_id="alice", api_key=api_key)
        added = toolkit.mem0_add("Likes green tea")
        found = toolkit.mem0_read(action="search", query="tea")
        listed = toolkit.mem0_read(action="get_all")
        deleted = toolkit.mem0_delete_all()

    if api_key is None:
        local_memory.assert_called_once()
        platform_client.assert_not_called()
    else:
        platform_client.assert_called_once_with(api_key=api_key)
        local_memory.assert_not_called()

    assert json.loads(added) == {"results": [{"id": "m2", "memory": "Likes green tea", "event": "ADD"}]}
    assert json.loads(found) == [{"id": "m1", "memory": "Likes green tea"}]
    assert json.loads(listed) == [{"id": "m1", "memory": "Likes green tea"}]
    assert deleted == "Successfully deleted all memories for user_id: alice."
    assert client.filters == {"search tea": {"user_id": "alice"}, "get_all": {"user_id": "alice"}}
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
    """A failed provider call's text reaches the model with credentials redacted."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    rejected_key = "sk-ant-" + "api03-" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8Ba7"
    with patch("mem0.memory.main.Memory", return_value=_FailingMem0Client(rejected_key)):
        toolkit = mem0_tools()(user_id="alice")
        added = toolkit.mem0_add("Likes green tea")
        found = toolkit.mem0_read(action="search", query="tea")

    assert added.startswith("Error adding memory: LLM extraction failed: Error code: 401 - invalid x-api-key ")
    assert found.startswith("Invalid query for key ")
    assert rejected_key not in added
    assert rejected_key not in found


class _ScopedMem0Client(_Mem0Client):
    """Fake Mem0 2.2 MemoryClient that discovers org_id and project_id from the API key."""

    def __init__(self, *, org_id: str | None, project_id: str | None) -> None:
        super().__init__()
        self.org_id = org_id
        self.project_id = project_id


def test_mem0_org_and_project_mismatch_raises_without_overwriting() -> None:
    """Configured org_id/project_id must match discovered API key scope instead of silently overwriting."""
    mismatched_org = _ScopedMem0Client(org_id="org-actual", project_id="proj-actual")
    with patch("mem0.client.main.MemoryClient", return_value=mismatched_org):
        toolkit = mem0_tools()(user_id="alice", api_key="m0-key", org_id="org-expected", project_id="proj-actual")
        with pytest.raises(ValueError, match="org_id"):
            toolkit.mem0_delete_all()
        assert mismatched_org.org_id == "org-actual"
        assert toolkit.client is None
        assert mismatched_org.top_level == {}

    mismatched_project = _ScopedMem0Client(org_id="org-actual", project_id="proj-actual")
    with patch("mem0.client.main.MemoryClient", return_value=mismatched_project):
        toolkit = mem0_tools()(user_id="alice", api_key="m0-key", org_id="org-actual", project_id="proj-expected")
        with pytest.raises(ValueError, match="project_id"):
            toolkit.mem0_add("Likes tea")
        assert mismatched_project.project_id == "proj-actual"
        assert toolkit.client is None
        assert mismatched_project.top_level == {}

    undiscovered = _ScopedMem0Client(org_id=None, project_id=None)
    with patch("mem0.client.main.MemoryClient", return_value=undiscovered):
        toolkit = mem0_tools()(user_id="alice", api_key="m0-key", org_id="org-expected")
        with pytest.raises(ValueError, match="org_id"):
            toolkit.mem0_read(action="get_all")
        assert toolkit.client is None

    matching = _ScopedMem0Client(org_id="org-actual", project_id="proj-actual")
    with patch("mem0.client.main.MemoryClient", return_value=matching):
        toolkit = mem0_tools()(user_id="alice", api_key="m0-key", org_id="org-actual", project_id="proj-actual")
        found = json.loads(toolkit.mem0_read(action="get_all"))
        assert found == [{"id": "m1", "memory": "Likes green tea"}]
        assert toolkit.client is matching


def test_mem0_detached_execution_identity_fallback() -> None:
    """Without configured user_id or current_call, mem0 falls back to current_execution_identity().requester_id."""
    client = _Mem0Client()
    identity = ToolExecutionIdentity(
        channel="openai_compat",
        agent_name="assistant",
        requester_id="user-mcp-42",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="sess-mcp",
    )
    with (
        patch("mem0.memory.main.Memory", return_value=client),
        detached_tool_scope(identity),
    ):
        toolkit = mem0_tools()()
        found = json.loads(toolkit.mem0_read(action="search", query="tea"))

    assert found == [{"id": "m1", "memory": "Likes green tea"}]
    assert client.filters == {"search tea": {"user_id": "user-mcp-42"}}
