"""Generate the real Microsoft 365 document-card wire fixture consumed by MindRoom Chat.

Run ``uv run -m tests.microsoft_365_contract_fixture --output <path>`` from the backend checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import nio
import pytest

from mindroom.config.main import Config
from mindroom.custom_tools.excel_workbooks import parse_edits
from mindroom.custom_tools.microsoft_365 import _DOCUMENT_CONTENT_KEY, Microsoft365Tools
from mindroom.message_target import MessageTarget
from mindroom.oauth.microsoft import microsoft_365_oauth_provider
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
)
from tests.microsoft_graph_test_support import (
    ALICE_TOKEN,
    DOCUMENT_ID,
    DRIVE_ID,
    ITEM_ID,
    SHARE_URL,
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

CONTRACT_ROOM_ID = "!room:localhost"
CONTRACT_THREAD_ID = "$thread"
CONTRACT_REQUESTER_ID = "@alice:localhost"
CONTRACT_AGENT_USER_ID = "@mindroom_analyst:localhost"
# A valid edit_office_document call, as an approval card shows it; the backend parser accepts it.
EDIT_ARGUMENTS: dict[str, Any] = {
    "document_id": DOCUMENT_ID,
    "summary": "Raise growth to 12% and refresh the narrative",
    "edits": [
        {
            "range": "Assumptions!B4:B5",
            "before": [[0.08], [142]],
            "after": [[0.12], [142]],
            "number_format": [["0%"], ["0"]],
        },
        {
            "range": "'Summary Sheet'!B2",
            "before": [["Revenue grows 8% YoY."]],
            "after": [["Revenue grows 12% YoY."]],
        },
    ],
    "skip_conflicts": False,
}


def _context(tmp_path: Path, *, event_id: str, threaded: bool) -> ToolRuntimeContext:
    paths = runtime_paths(tmp_path)
    client = make_matrix_client_mock(user_id=CONTRACT_AGENT_USER_ID)
    client.room_send.return_value = nio.RoomSendResponse(event_id, CONTRACT_ROOM_ID)
    return make_test_tool_runtime_context(
        agent_name="assistant",
        target=MessageTarget.resolve(
            room_id=CONTRACT_ROOM_ID,
            thread_id=CONTRACT_THREAD_ID if threaded else None,
            reply_to_event_id="$request" if threaded else None,
        ),
        requester_id=CONTRACT_REQUESTER_ID,
        client=client,
        config=bind_runtime_paths(Config(), paths),
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _toolkit(context: ToolRuntimeContext, workspace: Path) -> Microsoft365Tools:
    manager = save_client_config(context.runtime_paths)
    publish_grant(microsoft_365_oauth_provider(), manager, ALICE_TOKEN, requester_id=CONTRACT_REQUESTER_ID)
    return Microsoft365Tools(
        runtime_paths=context.runtime_paths,
        credentials_manager=manager,
        worker_target=worker_target(CONTRACT_REQUESTER_ID),
        tool_output_workspace_root=workspace,
    )


async def _connect(tool: Microsoft365Tools, _graph: FakeGraph) -> str:
    return await tool.connect_office_document(SHARE_URL)


async def _save(tool: Microsoft365Tools, _graph: FakeGraph) -> str:
    return await tool.save_office_document("Forecast.xlsx")


async def _edit(tool: Microsoft365Tools, _graph: FakeGraph) -> str:
    return await tool.edit_office_document(**EDIT_ARGUMENTS)


async def _edit_partial(tool: Microsoft365Tools, graph: FakeGraph) -> str:
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.11]])
    graph.patch_transform = lambda rows: [["Revenue grows 12% YoY (restated)."] for _row in rows]
    return await tool.edit_office_document(**{**EDIT_ARGUMENTS, "skip_conflicts": True})


_CASES: tuple[tuple[str, bool, Callable[[Microsoft365Tools, FakeGraph], Awaitable[str]]], ...] = (
    ("thread/connected", True, _connect),
    ("room/connected", False, _connect),
    ("thread/saved", True, _save),
    ("thread/edited", True, _edit),
    ("thread/edited-partial", True, _edit_partial),
)


async def build_microsoft_365_contract(tmp_path: Path) -> dict[str, object]:
    """Run every card-emitting toolkit path against a fake Graph and capture its Matrix event."""
    parse_edits(EDIT_ARGUMENTS["edits"])
    exported: list[dict[str, object]] = []
    for case_id, threaded, invoke in _CASES:
        case_dir = tmp_path / case_id.replace("/", "-")
        workspace = case_dir / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "Forecast.xlsx").write_bytes(b"PK\x03\x04contract")
        event_id = f"$microsoft-365-contract-{case_id.replace('/', '-')}"
        context = _context(case_dir, event_id=event_id, threaded=threaded)
        tool = _toolkit(context, workspace)
        with pytest.MonkeyPatch.context() as monkeypatch:
            graph = FakeGraph.with_forecast().install(monkeypatch)
            with tool_runtime_context(context):
                result = json.loads(await invoke(tool, graph))
        if result.get("card_event_id") != event_id:
            msg = f"Microsoft 365 contract case {case_id!r} did not post a card: {result!r}"
            raise RuntimeError(msg)
        content = context.client.room_send.await_args.kwargs["content"]
        if _DOCUMENT_CONTENT_KEY not in content:
            msg = f"Microsoft 365 contract case {case_id!r} posted a card without document metadata"
            raise RuntimeError(msg)
        exported.append(
            {
                "id": case_id,
                "event": {
                    "content": content,
                    "event_id": event_id,
                    "origin_server_ts": 100_000,
                    "room_id": CONTRACT_ROOM_ID,
                    "sender": CONTRACT_AGENT_USER_ID,
                    "type": "m.room.message",
                },
            },
        )
    return {
        "cases": exported,
        "edit_arguments": EDIT_ARGUMENTS,
        "room_id": CONTRACT_ROOM_ID,
        "version": 1,
        "viewer_id": CONTRACT_REQUESTER_ID,
    }


def serialize_microsoft_365_contract(contract: dict[str, object]) -> str:
    """Return canonical JSON matching the client's committed fixture format."""
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Output JSON path, or - for stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Export the deterministic fixture without Microsoft Graph or a Matrix server."""
    args = _parse_args(argv)
    with TemporaryDirectory(prefix="mindroom-microsoft-365-contract-") as tmp_root:
        serialized = serialize_microsoft_365_contract(asyncio.run(build_microsoft_365_contract(Path(tmp_root))))
    if args.output == "-":
        print(serialized, end="")
        return
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
