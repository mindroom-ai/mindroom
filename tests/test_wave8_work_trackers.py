"""Tests for Wave 8 Batch 1 native work & project tracker toolkits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest

from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolFunction
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.tools.brandfetch import brandfetch_tools
from mindroom.tools.cal_com import cal_com_tools
from mindroom.tools.clickup import clickup_tools
from mindroom.tools.google_maps import google_maps_tools
from mindroom.tools.linear import linear_tools
from mindroom.tools.mem0 import mem0_tools
from mindroom.tools.native_helpers import UNSET, resolve_toggle, restrict_actions
from mindroom.tools.notion import notion_tools
from mindroom.tools.trello import trello_tools
from mindroom.tools.zep import zep_tools

if TYPE_CHECKING:
    from mindroom.tool_system.authoring import Toolkit


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

    indented_note = "    indented block\n        sub-item\n"
    created = json.loads(toolkit.notion_write(action="create_page", title="Note", tag="tech", content=indented_note))
    assert created["success"] is True
    assert created["page_id"] == "p1"
    created_children = toolkit.client.pages.create.call_args.kwargs["children"]
    assert created_children[0]["paragraph"]["rich_text"][0]["text"]["content"] == indented_note

    updatable = notion_tools()(api_key="ntn_key", database_id="db-123")
    updatable.client = MagicMock()
    updatable.notion_write(action="update_page", page_id="p1", content=indented_note)
    appended_children = updatable.client.blocks.children.append.call_args.kwargs["children"]
    assert appended_children[0]["paragraph"]["rich_text"][0]["text"]["content"] == indented_note

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


def test_resolve_toggle_all_precedence_and_explicit_none_fail_closed() -> None:
    """all=True overrides UNSET and explicit False, while explicit None stays fail-closed."""
    assert resolve_toggle(UNSET, default=False, enable_all=True) is True
    assert resolve_toggle(False, default=True, enable_all=True) is True
    assert resolve_toggle(None, default=True, enable_all=True) is False
    assert resolve_toggle(False, default=True, enable_all=False) is False
    assert resolve_toggle(UNSET, default=False, enable_all=False) is False
    assert resolve_toggle(None, default=True, enable_all=False) is False

    override_false = notion_tools()(api_key="ntn_key", database_id="db-123", enable_update_page=False, all=True)
    assert "update_page" in override_false._write_actions
    explicit_none = notion_tools()(api_key="ntn_key", database_id="db-123", enable_update_page=None, all=True)
    assert "update_page" not in explicit_none._write_actions


def test_trello_key_only_public_read_and_gated_write() -> None:
    """Trello supports key-only public-board reads while gating write actions on full credentials."""
    toolkit = trello_tools()(api_key="public-api-key")
    assert toolkit.client is not None
    assert toolkit.client.api_key == "public-api-key"
    assert toolkit.client.public_only is True

    fake_list = SimpleNamespace(id="l1", name="Public Todo", list_cards=list)
    fake_board = SimpleNamespace(list_lists=lambda: [fake_list])
    mock_client = MagicMock()
    mock_client.get_board.return_value = fake_board
    toolkit.client = mock_client

    lists = json.loads(toolkit.trello_read(action="board_lists", board_id="pub-board"))
    assert lists == {"lists": [{"id": "l1", "name": "Public Todo", "cards_count": 0}]}

    denied = json.loads(
        toolkit.trello_write(action="create_card", board_id="pub-board", list_name="Public Todo", name="Nope"),
    )
    assert "api_key plus api_secret or token" in denied["error"]
    mock_client.add_board.assert_not_called()

    two_field = trello_tools()(api_key="key-only-plus-secret", api_secret="secret-only")  # noqa: S106
    assert two_field.client is not None
    assert two_field.client.api_key == "key-only-plus-secret"
    assert two_field.client.api_secret == "secret-only"  # noqa: S105
    assert two_field.client.resource_owner_key is None

    two_field_mock = MagicMock()
    two_field_mock.add_board.return_value = SimpleNamespace(id="b2", name="Ops", url="https://trello.com/b/2")
    two_field.client = two_field_mock
    created_board = json.loads(two_field.trello_write(action="create_board", name="Ops"))
    assert created_board["id"] == "b2"


def test_restrict_actions_fast_path_ordering_and_pruning() -> None:
    """Unified restrict_actions preserves enum order, fast-paths full sets, and prunes bullets/fields/required."""
    fn = ToolFunction(
        name="demo_action",
        description="Demo tool.\nActions:\n- `first`: uses `alpha`.\n- `second`: uses `beta`.\n- `third`: uses `gamma`.",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["first", "second", "third"]},
                "shared": {"type": "string"},
                "alpha": {"type": "string"},
                "beta": {"type": "string"},
                "gamma": {"type": "string"},
            },
            "required": ["action", "shared", "beta"],
        },
        entrypoint=lambda **_: "",
    )

    # Full-schema fast path returns the original ToolFunction unchanged even if input order differs.
    assert restrict_actions(fn, ("third", "first", "second")) is fn

    # Enum-only narrowing preserves declared enum order and prunes disabled bullets without dropping properties.
    enum_only = restrict_actions(fn, ("third", "first"))
    assert enum_only.parameters["properties"]["action"]["enum"] == ["first", "third"]
    assert set(enum_only.parameters["properties"]) == {"action", "shared", "alpha", "beta", "gamma"}
    assert "`first`" in (enum_only.description or "")
    assert "`third`" in (enum_only.description or "")
    assert "`second`" not in (enum_only.description or "")

    # Action-field narrowing prunes properties and required alongside bullets.
    pruned = restrict_actions(
        fn,
        ("third", "first"),
        action_fields={"first": "alpha", "second": "beta", "third": "gamma"},
        common_fields=("shared",),
    )
    assert pruned.parameters["properties"]["action"]["enum"] == ["first", "third"]
    assert set(pruned.parameters["properties"]) == {"action", "shared", "alpha", "gamma"}
    assert pruned.parameters["required"] == ["action", "shared"]
    assert "`second`" not in (pruned.description or "")


def test_wave8_per_action_toggles_narrow_offered_schemas() -> None:
    """Wave 8 action-dispatched toolkits omit disabled actions, bullets, and unused parameters from schemas."""

    def _offered(toolkit: Toolkit, name: str) -> ToolFunction:
        adapted = adapt_toolkit(toolkit)
        return next(fn for fn in adapted.functions if fn.name == name)

    bf_default = _offered(brandfetch_tools()(api_key="k", client_id="c"), "brandfetch_lookup")
    assert bf_default.parameters["properties"]["action"]["enum"] == ["by_identifier"]
    assert "`by_brand`" not in (bf_default.description or "")

    cal_tk = cal_com_tools()(api_key="k", enable_get_upcoming_bookings=False, enable_cancel_booking=False)
    cal_read = _offered(cal_tk, "cal_com_read")
    assert cal_read.parameters["properties"]["action"]["enum"] == ["available_slots"]
    assert "email" not in cal_read.parameters["properties"]
    assert "`upcoming_bookings`" not in (cal_read.description or "")
    cal_write = _offered(cal_tk, "cal_com_write")
    assert cal_write.parameters["properties"]["action"]["enum"] == ["create_booking", "reschedule_booking"]
    assert "`cancel_booking`" not in (cal_write.description or "")

    notion_write = _offered(notion_tools()(api_key="k", database_id="db-1", enable_update_page=False), "notion_write")
    assert notion_write.parameters["properties"]["action"]["enum"] == ["create_page"]
    assert "page_id" not in notion_write.parameters["properties"]
    assert "`update_page`" not in (notion_write.description or "")

    mem0_read = _offered(mem0_tools()(enable_search_memory=False), "mem0_read")
    assert mem0_read.parameters["properties"]["action"]["enum"] == ["get_all"]
    assert "query" not in mem0_read.parameters["properties"]
    assert "`search`" not in (mem0_read.description or "")

    zep_read = _offered(zep_tools()(api_key="k", enable_search_zep_memory=False), "zep_read")
    assert zep_read.parameters["properties"]["action"]["enum"] == ["context", "messages"]
    assert "query" not in zep_read.parameters["properties"]
    assert "search_scope" not in zep_read.parameters["properties"]
    assert "`search`" not in (zep_read.description or "")

    maps_tk = google_maps_tools()(
        key="AIzaSyTestKey",
        search_places=False,
        validate_address=False,
        reverse_geocode=False,
        get_elevation=False,
        get_timezone=False,
        get_distance_matrix=False,
    )
    places = _offered(maps_tk, "google_maps_places")
    assert places.parameters["properties"]["action"]["enum"] == ["geocode"]
    assert set(places.parameters["properties"]) - {"mindroom_output_path"} == {"action", "address", "region"}
    assert "`search_places`" not in (places.description or "")
    routes = _offered(maps_tk, "google_maps_routes")
    assert routes.parameters["properties"]["action"]["enum"] == ["directions"]
    assert "origins" not in routes.parameters["properties"]
    assert "`distance_matrix`" not in (routes.description or "")
