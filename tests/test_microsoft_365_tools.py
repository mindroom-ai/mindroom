"""Behavior of the Microsoft 365 connected documents toolkit against an in-memory Microsoft Graph."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools import microsoft_365
from mindroom.custom_tools.microsoft_365 import Microsoft365Tools
from mindroom.oauth.microsoft import microsoft_365_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import make_matrix_client_mock
from tests.microsoft_graph_test_support import (
    ALICE,
    ALICE_TOKEN,
    BOB,
    BOB_TOKEN,
    DOCUMENT_ID,
    DRIVE_ID,
    ITEM_ID,
    ROOM_ID,
    SHARE_URL,
    THREAD_ID,
    UPLOAD_HOST,
    WEB_URL,
    FakeGraph,
    bearer,
    drive_item,
    folder_item,
    graph_error,
    publish_grant,
    runtime_paths,
    save_client_config,
    tool_context,
    worker_target,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

AGENT_USER_ID = "@mindroom_assistant:example.org"
FILE_URL = "https://contoso.sharepoint.com/sites/finance/Shared%20Documents/FY27/Forecast.xlsx"


def _tool(
    paths: RuntimePaths,
    manager: CredentialsManager,
    *,
    requester_id: str = ALICE,
    workspace: Path | None = None,
) -> Microsoft365Tools:
    return Microsoft365Tools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(requester_id),
        tool_output_workspace_root=workspace,
    )


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RuntimePaths, CredentialsManager, FakeGraph]:
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(microsoft_365_oauth_provider(), manager, ALICE_TOKEN)
    graph = FakeGraph.with_forecast().install(monkeypatch)
    return paths, manager, graph


def _context(paths: RuntimePaths, requester_id: str = ALICE) -> ToolRuntimeContext:
    client = make_matrix_client_mock(user_id=AGENT_USER_ID)
    client.room_send.return_value = nio.RoomSendResponse("$card", ROOM_ID)
    return tool_context(paths, requester_id=requester_id, client=client)


def _sent_cards(context: ToolRuntimeContext) -> list[dict[str, Any]]:
    return [call.kwargs["content"] for call in context.client.room_send.await_args_list]


def _growth_edit(before: object = 0.08, after: object = 0.12) -> dict[str, Any]:
    return {"range": "Assumptions!B4", "before": [[before]], "after": [[after]]}


def test_registered_metadata_matches_the_toolkit(tmp_path: Path) -> None:
    """The catalog advertises the toolkit's functions as a primary-runtime, agent-confined OAuth tool."""
    metadata = TOOL_METADATA["microsoft_365"]
    assert metadata.function_names == (
        "connect_office_document",
        "save_office_document",
        "read_office_document",
        "edit_office_document",
    )
    assert metadata.file_access is ToolFileAccess.AGENT
    assert metadata.requires_primary_runtime
    assert metadata.consumes_workspace_paths
    assert metadata.auth_provider == "microsoft_365"
    paths = runtime_paths(tmp_path)
    toolkit = _tool(paths, save_client_config(paths))
    functions = toolkit.get_async_functions()
    assert tuple(functions) == metadata.function_names
    assert functions["edit_office_document"].requires_confirmation is True
    assert not any(functions[name].requires_confirmation for name in metadata.function_names[:3])


@pytest.mark.asyncio
async def test_missing_connection_asks_the_requester_to_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the requester's own grant, no Graph request is made."""
    paths = runtime_paths(tmp_path)
    graph = FakeGraph.with_forecast().install(monkeypatch)
    result = json.loads(await _tool(paths, save_client_config(paths)).read_office_document(DOCUMENT_ID))

    assert result["status"] == "error"
    assert result["oauth_connection_required"] is True
    assert result["provider"] == "microsoft_365"
    assert graph.requests == []


@pytest.mark.asyncio
async def test_each_requester_uses_their_own_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Room membership never lends one user's Microsoft account to another."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    alice = json.loads(await _tool(paths, manager).read_office_document(DOCUMENT_ID, "Assumptions!B4"))
    bob_before_connecting = json.loads(
        await _tool(paths, manager, requester_id=BOB).read_office_document(DOCUMENT_ID, "Assumptions!B4"),
    )
    assert {bearer(request) for request in graph.requests} == {ALICE_TOKEN}
    publish_grant(microsoft_365_oauth_provider(), manager, BOB_TOKEN, requester_id=BOB)
    graph.requests.clear()
    bob = json.loads(await _tool(paths, manager, requester_id=BOB).read_office_document(DOCUMENT_ID, "Assumptions!B4"))

    assert alice["status"] == "ok"
    assert bob_before_connecting["oauth_connection_required"] is True
    assert bob["status"] == "ok"
    assert {bearer(request) for request in graph.requests} == {BOB_TOKEN}


@pytest.mark.asyncio
async def test_connect_posts_a_document_card_and_returns_the_outline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connecting resolves the link, returns the outline, and posts one exact card in the thread."""
    paths, manager, _graph = _setup(tmp_path, monkeypatch)
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(await _tool(paths, manager).connect_office_document(SHARE_URL))

    assert result["status"] == "ok"
    assert result["document"]["document_id"] == DOCUMENT_ID
    assert [sheet["name"] for sheet in result["outline"]["worksheets"]] == ["Assumptions", "Summary Sheet", "Lookups"]
    assert result["card_posted"] is True
    assert result["card_event_id"] == "$card"
    [card] = _sent_cards(context)
    assert card["msgtype"] == "m.notice"
    assert card["m.relates_to"]["rel_type"] == "m.thread"
    assert card["m.relates_to"]["event_id"] == THREAD_ID
    assert card["io.mindroom.document"] == {
        "version": 1,
        "event": "connected",
        "document_id": DOCUMENT_ID,
        "name": "Forecast.xlsx",
        "kind": "xlsx",
        "web_url": WEB_URL,
        "file_url": FILE_URL,
        "location": "FY27",
        "revision": {"etag": '"{ETAG},1"', "modified_at": "2026-09-25T10:05:40Z", "modified_by": "Sam Kim"},
        "requester_id": ALICE,
        "agent_user_id": AGENT_USER_ID,
        "room_id": ROOM_ID,
        "thread_id": THREAD_ID,
    }
    assert card["body"] == (
        f"Connected Forecast.xlsx (FY27) to this conversation.\ndocument_id: {DOCUMENT_ID}\n{WEB_URL}"
    )


@pytest.mark.asyncio
async def test_connect_refuses_non_workbooks_folders_and_unsafe_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only .xlsx files connect; nothing is posted for anything else."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    deck_url = "https://contoso.sharepoint.com/:p:/s/finance/deck"
    folder_url = "https://contoso.sharepoint.com/:f:/s/finance/folder"
    graph.items[(DRIVE_ID, "01DECK")] = drive_item("01DECK", name="Board.pptx")
    graph.items[(DRIVE_ID, "01FOLDER")] = folder_item("01FOLDER")
    graph.shares[microsoft_365.share_id(deck_url)] = (DRIVE_ID, "01DECK")
    graph.shares[microsoft_365.share_id(folder_url)] = (DRIVE_ID, "01FOLDER")
    context = _context(paths)
    toolkit = _tool(paths, manager)
    with tool_runtime_context(context):
        deck = json.loads(await toolkit.connect_office_document(deck_url))
        folder = json.loads(await toolkit.connect_office_document(folder_url))
        unsafe = json.loads(await toolkit.connect_office_document("http://contoso.sharepoint.com/x"))

    assert deck["code"] == "unsupported_document"
    assert folder["code"] == "not_a_file"
    assert unsafe["code"] == "invalid_url"
    assert context.client.room_send.await_count == 0


@pytest.mark.asyncio
async def test_read_returns_the_outline_or_one_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads return document identity with the outline, or one range's grids."""
    paths, manager, _graph = _setup(tmp_path, monkeypatch)
    toolkit = _tool(paths, manager)
    outline = json.loads(await toolkit.read_office_document(DOCUMENT_ID))
    cells = json.loads(await toolkit.read_office_document(DOCUMENT_ID, "Assumptions!A4:B4"))
    invalid = json.loads(await toolkit.read_office_document("not-an-id", "Assumptions!A4"))

    assert outline["document"]["name"] == "Forecast.xlsx"
    assert outline["outline"]["tables"] == [{"name": "tbl_Inputs", "range": "Assumptions!A1:C5"}]
    assert cells["formulas"] == [["Growth", 0.08]]
    assert cells["number_format"] == [["General", "0%"]]
    assert invalid["code"] == "invalid_document_id"


@pytest.mark.asyncio
async def test_edit_applies_verifies_and_posts_a_receipt_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved edit writes, verifies, and posts an edited card with the change summary."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(
            await _tool(paths, manager).edit_office_document(DOCUMENT_ID, [_growth_edit()], "Raise growth to 12%"),
        )

    assert result["status"] == "ok"
    assert result["result"] == "applied"
    assert result["verified"] is True
    assert result["cells_changed"] == 1
    assert graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").formulas[(4, 2)] == 0.12
    [card] = _sent_cards(context)
    metadata = card["io.mindroom.document"]
    assert metadata["event"] == "edited"
    assert metadata["change"] == {
        "summary": "Raise growth to 12%",
        "status": "applied",
        "verified": True,
        "cells_changed": 1,
        "edits": [{"range": "Assumptions!B4", "outcome": "applied", "cells_changed": 1, "verified": True}],
    }
    assert "Raise growth to 12%\n1 cell(s) changed, verified." in card["body"]


@pytest.mark.asyncio
async def test_edit_conflict_writes_nothing_and_posts_no_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A human edit since the read is reported with current content, and nothing is written or posted."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.11]])
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(
            await _tool(paths, manager).edit_office_document(DOCUMENT_ID, [_growth_edit()], "Raise growth to 12%"),
        )

    assert result["status"] == "error"
    assert result["code"] == "conflict"
    assert result["edits"][0]["current"] == [[0.11]]
    assert graph.paths("PATCH") == []
    assert context.client.room_send.await_count == 0


@pytest.mark.asyncio
async def test_edit_validates_arguments_before_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed edits and summaries fail without touching the workbook."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    toolkit = _tool(paths, manager)
    shape = json.loads(
        await toolkit.edit_office_document(DOCUMENT_ID, [{**_growth_edit(), "after": [[1, 2]]}], "x"),
    )
    summary = json.loads(await toolkit.edit_office_document(DOCUMENT_ID, [_growth_edit()], "  "))
    converted = json.loads(await toolkit.edit_office_document(DOCUMENT_ID, [_growth_edit(after="12%")], "x"))

    assert shape["code"] == summary["code"] == converted["code"] == "invalid_argument"
    assert "Excel would convert" in converted["message"]
    assert graph.requests == []


@pytest.mark.asyncio
async def test_rejected_token_returns_a_reconnect_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graph rejecting the stored token asks the requester to reconnect."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.tokens.clear()
    result = json.loads(await _tool(paths, manager).read_office_document(DOCUMENT_ID, "Assumptions!B4"))

    assert result["oauth_connection_required"] is True
    assert result["reason"] == "access_rejected"


@pytest.mark.asyncio
async def test_save_creates_the_mindroom_folder_and_never_replaces_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving uploads under a free name in the MindRoom folder and posts a saved card."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "Forecast.xlsx").write_bytes(b"PK\x03\x04workbook")
    graph.me_folders["MindRoom"] = folder_item("FOLDER-MindRoom")
    graph.folder_children["FOLDER-MindRoom"] = {"Forecast.xlsx": drive_item("01OLD")}
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(await _tool(paths, manager, workspace=workspace).save_office_document("Forecast.xlsx"))

    assert result["status"] == "ok"
    assert result["document"]["name"] == "Forecast (2).xlsx"
    assert graph.folder_children["FOLDER-MindRoom"]["Forecast.xlsx"]["id"] == "01OLD"
    [session] = [request for request in graph.requests if request.url.path.endswith("createUploadSession")]
    assert json.loads(session.content) == {
        "item": {"@microsoft.graph.conflictBehavior": "fail", "name": "Forecast (2).xlsx"},
    }
    [upload] = [request for request in graph.requests if request.method == "PUT"]
    assert upload.url.host == UPLOAD_HOST
    assert "authorization" not in upload.headers
    assert upload.content == b"PK\x03\x04workbook"
    [card] = _sent_cards(context)
    assert card["io.mindroom.document"]["event"] == "saved"


@pytest.mark.asyncio
async def test_save_creates_a_missing_mindroom_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A first save creates the folder without replacing anything."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plan.xlsx").write_bytes(b"PK\x03\x04")
    result = json.loads(
        await _tool(paths, manager, workspace=workspace).save_office_document("plan.xlsx", name="Plan.xlsx"),
    )

    assert result["status"] == "ok"
    assert result["card_posted"] is False
    assert "MindRoom" in graph.me_folders
    assert "Plan.xlsx" in graph.folder_children["FOLDER-MindRoom"]


@pytest.mark.asyncio
async def test_save_refuses_paths_names_and_files_it_must_not_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paths outside the workspace, non-workbooks, unsafe names, and oversized files never upload."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.xlsx").write_bytes(b"PK\x03\x04")
    (workspace / "notes.txt").write_bytes(b"hello")
    (workspace / "fake.xlsx").write_bytes(b"not a zip")
    (workspace / "big.xlsx").write_bytes(b"PK\x03\x04" + b"0" * 32)
    monkeypatch.setattr(microsoft_365, "_MAX_UPLOAD_BYTES", 16)
    toolkit = _tool(paths, manager, workspace=workspace)

    outside = json.loads(await toolkit.save_office_document(str(tmp_path / "outside.xlsx")))
    text = json.loads(await toolkit.save_office_document("notes.txt"))
    fake = json.loads(await toolkit.save_office_document("fake.xlsx"))
    big = json.loads(await toolkit.save_office_document("big.xlsx"))
    unsafe = json.loads(await toolkit.save_office_document("fake.xlsx", name="../x.xlsx"))

    assert outside["code"] == "invalid_path"
    assert text["code"] == fake["code"] == big["code"] == unsafe["code"] == "invalid_argument"
    assert "upload limit" in big["message"]
    assert graph.requests == []


def _workspace_with(tmp_path: Path, name: str = "Forecast.xlsx") -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / name).write_bytes(b"PK\x03\x04workbook")
    return workspace


@pytest.mark.asyncio
async def test_save_moves_on_when_a_name_is_taken_between_check_and_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent upload of the same name makes the session fail, not replace, and the next name is used."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.me_folders["MindRoom"] = folder_item("FOLDER-MindRoom")
    graph.folder_children["FOLDER-MindRoom"] = {"Forecast.xlsx": drive_item("01RACE")}
    child = f"/drives/{DRIVE_ID}/items/FOLDER-MindRoom:/Forecast.xlsx"
    graph.overrides[("GET", child)] = lambda _request: graph_error(404, "itemNotFound", "Not found")
    result = json.loads(
        await _tool(paths, manager, workspace=_workspace_with(tmp_path)).save_office_document("Forecast.xlsx"),
    )

    assert result["document"]["name"] == "Forecast (2).xlsx"
    assert graph.folder_children["FOLDER-MindRoom"]["Forecast.xlsx"]["id"] == "01RACE"
    assert [request.method for request in graph.requests if request.url.host == UPLOAD_HOST] == ["PUT", "DELETE", "PUT"]


@pytest.mark.asyncio
async def test_save_into_a_linked_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """folder_url resolves a shared folder and uploads there."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    folder_url = "https://contoso.sharepoint.com/:f:/s/finance/reports"
    graph.items[(DRIVE_ID, "01REPORTS")] = folder_item("01REPORTS")
    graph.shares[microsoft_365.share_id(folder_url)] = (DRIVE_ID, "01REPORTS")
    result = json.loads(
        await _tool(paths, manager, workspace=_workspace_with(tmp_path)).save_office_document(
            "Forecast.xlsx",
            folder_url=folder_url,
        ),
    )

    assert result["status"] == "ok"
    assert "Forecast.xlsx" in graph.folder_children["01REPORTS"]
    assert graph.me_folders == {}


@pytest.mark.asyncio
async def test_save_reports_when_every_candidate_name_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the numbered candidates run out, nothing is uploaded."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.me_folders["MindRoom"] = folder_item("FOLDER-MindRoom")
    graph.folder_children["FOLDER-MindRoom"] = {
        name: drive_item(f"01T{index}")
        for index, name in enumerate(["Forecast.xlsx", *(f"Forecast ({n}).xlsx" for n in range(2, 21))])
    }
    result = json.loads(
        await _tool(paths, manager, workspace=_workspace_with(tmp_path)).save_office_document("Forecast.xlsx"),
    )

    assert result["code"] == "name_unavailable"
    assert not [request for request in graph.requests if request.url.host == UPLOAD_HOST]


@pytest.mark.asyncio
async def test_save_refuses_a_mindroom_file_in_place_of_the_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file named MindRoom is never used as a folder."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.me_folders["MindRoom"] = drive_item("01FILE", name="MindRoom")
    result = json.loads(
        await _tool(paths, manager, workspace=_workspace_with(tmp_path)).save_office_document("Forecast.xlsx"),
    )

    assert result["code"] == "not_a_folder"
    assert "pass folder_url" in result["message"]


@pytest.mark.asyncio
async def test_save_uses_a_folder_another_request_created_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder-creation conflict re-reads the folder created by the other request."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)

    def created_elsewhere(_request: httpx.Request) -> httpx.Response:
        graph.me_folders["MindRoom"] = folder_item("FOLDER-MindRoom")
        return graph_error(409, "nameAlreadyExists", "The specified item name already exists.")

    graph.overrides[("POST", "/me/drive/root/children")] = created_elsewhere
    result = json.loads(
        await _tool(paths, manager, workspace=_workspace_with(tmp_path)).save_office_document("Forecast.xlsx"),
    )

    assert result["status"] == "ok"
    assert "Forecast.xlsx" in graph.folder_children["FOLDER-MindRoom"]


@pytest.mark.asyncio
async def test_edit_keeps_the_receipt_when_the_follow_up_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a write lands, a failed metadata read still returns the receipt instead of a plain error."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.overrides[("GET", f"/drives/{DRIVE_ID}/items/{ITEM_ID}")] = lambda _request: graph_error(
        429,
        "TooManyRequests",
        "Slow down.",
    )
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(
            await _tool(paths, manager).edit_office_document(DOCUMENT_ID, [_growth_edit()], "Raise growth to 12%"),
        )

    assert result["status"] == "ok"
    assert result["result"] == "applied"
    assert result["card_posted"] is False
    assert result["card_error"]["code"] == "rate_limited"
    assert graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").formulas[(4, 2)] == 0.12
    assert context.client.room_send.await_count == 0


@pytest.mark.asyncio
async def test_partial_edit_card_omits_cell_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The room card lists outcomes only; conflicting and written grids stay in the agent's receipt."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.11]])
    context = _context(paths)
    edits = [_growth_edit(), {"range": "Assumptions!B5", "before": [[142]], "after": [[150]]}]
    with tool_runtime_context(context):
        result = json.loads(
            await _tool(paths, manager).edit_office_document(DOCUMENT_ID, edits, "Adjust inputs", skip_conflicts=True),
        )

    assert result["code"] == "partial"
    assert result["message"] == "Some edits were applied; check each edit's outcome."
    assert result["edits"][0]["current"] == [[0.11]]
    [card] = _sent_cards(context)
    assert card["io.mindroom.document"]["change"]["edits"] == [
        {"range": "Assumptions!B4", "outcome": "conflict", "cells_changed": 0},
        {"range": "Assumptions!B5", "outcome": "applied", "cells_changed": 1, "verified": True},
    ]


@pytest.mark.asyncio
async def test_failed_edit_explains_later_edits_were_not_attempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first write returns the failure message and posts no card."""
    paths, manager, graph = _setup(tmp_path, monkeypatch)
    path = (
        f"/drives/{DRIVE_ID}/items/{ITEM_ID}/workbook/worksheets/{{00000000-0001-0000-0000-000000000000}}"
        "/range(address='B4')"
    )

    def locked(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return graph_error(423, "resourceLocked", "Locked.")
        return graph._route(request, path)

    graph.overrides[("GET", path)] = locked
    graph.overrides[("PATCH", path)] = locked
    context = _context(paths)
    with tool_runtime_context(context):
        result = json.loads(await _tool(paths, manager).edit_office_document(DOCUMENT_ID, [_growth_edit()], "x"))

    assert result["code"] == "failed"
    assert result["message"] == "The first write failed and later edits were not attempted; check each edit's outcome."
    assert context.client.room_send.await_count == 0
