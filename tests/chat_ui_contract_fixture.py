"""Generate the real backend wire fixture consumed by MindRoom Chat."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, get_args, get_type_hints

import nio

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.custom_tools.chat_ui import ChatUITools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Sequence


ROOM_ID = "!room:example.org"
THREAD_ID = "$root"
REQUESTER_ID = "@alice:example.org"

CONTRACT_ROOM_ID = "!room:localhost"
CONTRACT_REQUESTER_ID = "@alice:localhost"
CONTRACT_THREAD_ID = "$thread"


def make_chat_ui_context(
    tmp_path: Path,
    *,
    room_id: str = ROOM_ID,
    thread_id: str | None = THREAD_ID,
    reply_to_event_id: str | None = "$request",
    requester_id: str = REQUESTER_ID,
    event_id: str = "$ui-action",
    agent_name: str = "researcher",
    transport_agent_name: str | None = None,
    include_team: bool = False,
) -> ToolRuntimeContext:
    """Build the explicit stable runtime used by Chat UI tool tests and export."""
    config = bind_runtime_paths(
        Config(
            agents={"researcher": AgentConfig(display_name="Researcher")},
            teams=(
                {
                    "research": TeamConfig(
                        display_name="Research Team",
                        role="Coordinate research",
                        agents=["researcher"],
                    ),
                }
                if include_team
                else {}
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    agent_user_id = entity_ids(config, runtime_paths)[agent_name].full_id
    client = make_matrix_client_mock(user_id=agent_user_id)
    client.room_send.return_value = nio.RoomSendResponse(event_id, room_id)
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        transport_agent_name=transport_agent_name,
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            room_mode=thread_id is None,
        ),
        requester_id=requester_id,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def sent_chat_ui_content(context: ToolRuntimeContext) -> dict[str, object]:
    """Return the exact content passed to the Matrix room-send boundary."""
    content = context.client.room_send.await_args.kwargs["content"]
    if not isinstance(content, dict):
        msg = "Chat UI contract export expected a dictionary Matrix payload"
        raise TypeError(msg)
    return content


@dataclass(frozen=True)
class _ContractCase:
    case_id: str
    scope: str
    action: str
    argument: str | None = None


def _contract_cases() -> tuple[_ContractCase, ...]:
    actions = (
        _ContractCase("show_computer", "", "show_computer"),
        *(
            _ContractCase(f"open_settings/{section}", "", "open_settings", section)
            for section in get_args(get_type_hints(ChatUITools.open_settings)["section"])
        ),
        *(
            _ContractCase(f"open_panel/{panel}", "", "open_panel", panel)
            for panel in get_args(get_type_hints(ChatUITools.open_panel)["panel"])
        ),
    )
    registered_actions = set(ChatUITools().get_async_functions())
    exported_actions = {case.action for case in actions}
    if registered_actions != exported_actions:
        msg = (
            "Chat UI contract action coverage differs from registered tools: "
            f"registered={sorted(registered_actions)}, exported={sorted(exported_actions)}"
        )
        raise RuntimeError(msg)
    return tuple(
        _ContractCase(case.case_id, scope, case.action, case.argument)
        for scope in ("thread", "room")
        for case in actions
    )


async def _invoke_contract_case(tool: ChatUITools, case: _ContractCase) -> str:
    if case.action == "show_computer":
        return await tool.show_computer()
    if case.action == "open_settings":
        return await tool.open_settings(section=case.argument)  # type: ignore[arg-type]
    return await tool.open_panel(panel=case.argument)  # type: ignore[arg-type]


async def build_chat_ui_contract(tmp_path: Path) -> dict[str, object]:
    """Execute every real Chat UI tool variant and capture its Matrix event."""
    exported_cases: list[dict[str, object]] = []
    for case in _contract_cases():
        case_id = f"{case.scope}/{case.case_id}"
        event_id = f"$chat-ui-contract-{case_id.replace('/', '-')}"
        threaded = case.scope == "thread"
        context = make_chat_ui_context(
            tmp_path / case_id.replace("/", "-"),
            room_id=CONTRACT_ROOM_ID,
            thread_id=CONTRACT_THREAD_ID if threaded else None,
            reply_to_event_id="$request" if threaded else None,
            requester_id=CONTRACT_REQUESTER_ID,
            event_id=event_id,
        )
        with tool_runtime_context(context):
            result = json.loads(await _invoke_contract_case(ChatUITools(), case))
        if result.get("status") != "ok" or result.get("event_id") != event_id:
            msg = f"Chat UI contract case {case_id!r} failed to emit: {result!r}"
            raise RuntimeError(msg)
        exported_cases.append(
            {
                "id": case_id,
                "event": {
                    "content": sent_chat_ui_content(context),
                    "event_id": event_id,
                    "origin_server_ts": 100_000,
                    "room_id": CONTRACT_ROOM_ID,
                    "sender": context.client.user_id,
                    "type": "m.room.message",
                },
            },
        )
    return {
        "cases": exported_cases,
        "room_id": CONTRACT_ROOM_ID,
        "version": 1,
        "viewer_id": CONTRACT_REQUESTER_ID,
    }


def serialize_chat_ui_contract(contract: dict[str, object]) -> str:
    """Return canonical JSON matching the client's committed fixture format."""
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Output JSON path, or - for stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Export the deterministic fixture without requiring a Matrix server."""
    args = _parse_args(argv)
    with TemporaryDirectory(prefix="mindroom-chat-ui-contract-") as tmp_root:
        serialized = serialize_chat_ui_contract(asyncio.run(build_chat_ui_contract(Path(tmp_root))))
    if args.output == "-":
        print(serialized, end="")
        return
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
