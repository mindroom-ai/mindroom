"""Tests for Wave 8 Batch 1 native work & project tracker toolkits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.clickup import clickup_tools
from mindroom.tools.linear import linear_tools
from mindroom.tools.notion import notion_tools
from mindroom.tools.trello import trello_tools


def test_batch1_action_scoped_grants_and_no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch 1 toolkits declare per-toolkit action_scoped_grants and never read host os.environ."""
    registry = builtin_tool_registry()
    assert registry.entries["linear"].action_scoped_grants == ("linear_read", "linear_write")
    assert registry.entries["notion"].action_scoped_grants == ("notion_write",)
    assert registry.entries["todoist"].action_scoped_grants == ("todoist_read", "todoist_write")
    assert registry.entries["clickup"].action_scoped_grants == ("clickup_read", "clickup_write")
    assert registry.entries["trello"].action_scoped_grants == ("trello_read", "trello_write")

    monkeypatch.setenv("LINEAR_API_KEY", "host-secret")
    monkeypatch.setenv("NOTION_API_KEY", "host-secret")
    monkeypatch.setenv("NOTION_DATABASE_ID", "host-db")
    monkeypatch.setenv("CLICKUP_API_KEY", "host-secret")
    monkeypatch.setenv("MASTER_SPACE_ID", "host-space")
    monkeypatch.setenv("TRELLO_API_KEY", "host-secret")
    monkeypatch.setenv("TRELLO_API_SECRET", "host-secret")
    monkeypatch.setenv("TRELLO_TOKEN", "host-secret")

    with pytest.raises(ValueError, match="Linear API key"):
        linear_tools()(api_key=None)
    with pytest.raises(ValueError, match="Notion API key"):
        notion_tools()(api_key=None, database_id="db-1")
    with pytest.raises(ValueError, match="ClickUp API key"):
        clickup_tools()(api_key=None, master_space_id="space-1")
    trello = trello_tools()()
    assert "required" in json.loads(trello.trello_read(action="list_boards"))["error"]


def test_linear_read_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linear dispatches GraphQL queries and mutations cleanly and handles errors."""
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, json: dict[str, object], headers: dict[str, str], timeout: int) -> httpx.Response:
        req = httpx.Request("POST", url)
        assert url == "https://api.linear.app/graphql"
        assert headers["Authorization"] == "lin_key"
        assert timeout == 15
        calls.append(json)
        query = str(json["query"])
        if "viewer" in query:
            return httpx.Response(
                200,
                request=req,
                json={"data": {"viewer": {"id": "u1", "name": "Ada", "email": "ada@x"}}},
            )
        if "issueCreate" in query:
            return httpx.Response(
                200,
                request=req,
                json={
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {"id": "i1", "title": "Bug", "url": "https://lin/i1"},
                        },
                    },
                },
            )
        if "issueUpdate" in query:
            return httpx.Response(
                200,
                request=req,
                json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {"id": "i1", "title": "Fixed", "state": {"id": "s1", "name": "Done"}},
                        },
                    },
                },
            )
        return httpx.Response(200, request=req, json={"errors": [{"message": "boom"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    toolkit = linear_tools()(api_key="lin_key", timeout=15)

    assert json.loads(toolkit.linear_read(action="viewer"))["name"] == "Ada"
    created = json.loads(toolkit.linear_write(action="create_issue", title="Bug", team_id="t1"))
    assert created["id"] == "i1"
    updated = json.loads(toolkit.linear_write(action="update_issue", issue_id="i1", title="Fixed"))
    assert updated["title"] == "Fixed"
    err = json.loads(toolkit.linear_read(action="teams"))
    assert "boom" in err["error"]
    assert len(calls) == 4


def test_notion_toggles_and_crud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notion respects explicit None toggles, validates IDs, and runs search/create/update."""
    disabled = notion_tools()(
        api_key="ntn_key",
        database_id="db-123",
        enable_create_page=None,
        enable_update_page=None,
        enable_search_pages=None,
    )
    assert list(disabled.functions) == []

    with pytest.raises(ValueError, match="Invalid database_id"):
        notion_tools()(api_key="ntn_key", database_id="../other")

    toolkit = notion_tools()(api_key="ntn_key", database_id="db-123", enable_update_page=False)
    toolkit.client = MagicMock()
    toolkit.client.pages.create.return_value = {"id": "p1", "url": "https://notion.so/p1"}

    created = json.loads(toolkit.notion_write(action="create_page", title="Note", tag="tech", content="Hello"))
    assert created["success"] is True
    assert created["page_id"] == "p1"

    denied = json.loads(toolkit.notion_write(action="update_page", page_id="p1", content="More"))
    assert denied["success"] is False
    assert "disabled" in denied["error"]

    def fake_post(url: str, *, headers: dict[str, str], **_kwargs: object) -> httpx.Response:
        assert url == "https://api.notion.com/v1/databases/db-123/query"
        assert headers["Authorization"] == "Bearer ntn_key"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "p1",
                        "url": "https://notion.so/p1",
                        "properties": {
                            "Name": {"title": [{"text": {"content": "Note"}}]},
                            "Tag": {"select": {"name": "tech"}},
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    found = json.loads(toolkit.notion_search(tag="tech"))
    assert found["count"] == 1
    assert found["pages"][0]["title"] == "Note"


def test_clickup_read_write_and_path_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """ClickUp validates path segments and executes space/list/task flows."""
    with pytest.raises(ValueError, match="Invalid master_space_id"):
        clickup_tools()(api_key="pk_1", master_space_id="team/../bad")

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> httpx.Response:
        assert headers["Authorization"] == "pk_1"
        req = httpx.Request(method, url)
        if method == "GET" and url.endswith("/team/team1/space"):
            return httpx.Response(200, request=req, json={"spaces": [{"id": "sp1", "name": "Engineering"}]})
        if method == "GET" and url.endswith("/space/sp1/list"):
            return httpx.Response(200, request=req, json={"lists": [{"id": "l1", "name": "Backlog"}]})
        if method == "POST" and url.endswith("/list/l1/task"):
            return httpx.Response(200, request=req, json={"id": "tk1", "name": (json or {}).get("name")})
        if method == "PUT" and url.endswith("/task/tk1"):
            return httpx.Response(200, request=req, json={"id": "tk1", **(json or {})})
        if method == "DELETE" and url.endswith("/task/tk1"):
            return httpx.Response(204, request=req)
        return httpx.Response(404, request=req, text="not found")

    monkeypatch.setattr(httpx, "request", fake_request)
    toolkit = clickup_tools()(api_key="pk_1", master_space_id="team1")

    created = json.loads(toolkit.clickup_write(action="create_task", space_name="eng", task_name="Fix CI"))
    assert created["id"] == "tk1"
    updated = json.loads(toolkit.clickup_write(action="update_task", task_id="tk1", updates={"status": "done"}))
    assert updated["status"] == "done"
    deleted = json.loads(toolkit.clickup_write(action="delete_task", task_id="tk1"))
    assert deleted["success"] is True
    bad = json.loads(toolkit.clickup_read(action="get_task", task_id="../secret"))
    assert "Invalid task_id" in bad["error"]


def test_trello_read_write_and_validation() -> None:
    """Trello dispatches board/list/card operations and validates path segments."""
    toolkit = trello_tools()(api_key="k", api_secret="s", token="t")  # noqa: S106 - synthetic credentials
    fake_card = SimpleNamespace(
        id="c1",
        name="Task",
        description="desc",
        url="https://trello.com/c/1",
        labels=[SimpleNamespace(name="bug")],
        change_list=MagicMock(),
    )
    fake_list = SimpleNamespace(
        id="l1",
        name="Todo",
        pos="bottom",
        list_cards=lambda: [fake_card],
        add_card=lambda name, desc="": SimpleNamespace(id="c2", name=name, desc=desc, url="https://trello.com/c/2"),
    )
    fake_board = SimpleNamespace(
        id="b1",
        name="Roadmap",
        description="Q4",
        url="https://trello.com/b/1",
        closed=False,
        starred=True,
        idOrganization="org1",
        list_lists=lambda: [fake_list],
        add_list=lambda name, pos: SimpleNamespace(id="l2", name=name, pos=pos),
    )
    client = MagicMock()
    client.list_boards.return_value = [fake_board]
    client.get_board.return_value = fake_board
    client.get_list.return_value = fake_list
    client.get_card.return_value = fake_card
    client.add_board.return_value = fake_board
    toolkit.client = client

    boards = json.loads(toolkit.trello_read(action="list_boards"))
    assert boards["total_boards"] == 1
    cards = json.loads(toolkit.trello_read(action="cards", list_id="l1"))
    assert cards["cards"][0]["labels"] == ["bug"]
    created = json.loads(toolkit.trello_write(action="create_card", board_id="b1", list_name="todo", name="New"))
    assert created["id"] == "c2"
    moved = json.loads(toolkit.trello_write(action="move_card", card_id="c1", list_id="l2"))
    assert moved["success"] is True
    fake_card.change_list.assert_called_once_with("l2")
    bad = json.loads(toolkit.trello_read(action="board_lists", board_id="b1/../../x"))
    assert "Invalid board_id" in bad["error"]
