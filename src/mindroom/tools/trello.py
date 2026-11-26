"""Trello project board management toolkit."""

from __future__ import annotations

from typing import Any, Literal

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

_ReadAction = Literal["list_boards", "board_lists", "cards"]
_WriteAction = Literal["create_board", "create_list", "create_card", "move_card"]
_BoardFilter = Literal["all", "open", "closed", "organization", "public", "starred"]


class _TrelloTools(Toolkit):
    """Manage Trello boards, lists, and cards via `py-trello`."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        token: str | None = None,
    ) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.api_secret = api_secret.strip() if isinstance(api_secret, str) and api_secret.strip() else None
        self.token = token.strip() if isinstance(token, str) and token.strip() else None
        self.client: Any = None
        if self.api_key:
            self.client = self._build_client()
        super().__init__(name="trello", tools=[self.trello_read, self.trello_write])

    def _build_client(self) -> Any:  # noqa: ANN401
        from trello import TrelloClient

        return TrelloClient(api_key=self.api_key, api_secret=self.api_secret, token=self.token)

    def _has_write_auth(self) -> bool:
        return bool(self.api_key and (self.api_secret or self.token))

    def _require_client(self, *, require_auth: bool = False) -> Any:  # noqa: ANN401
        if require_auth and not self._has_write_auth():
            msg = "Trello write operations require api_key plus api_secret or token."
            raise ValueError(msg)
        if self.client is not None:
            return self.client
        if not self.api_key:
            msg = "Trello api_key is required."
            raise ValueError(msg)
        self.client = self._build_client()
        return self.client

    def trello_read(
        self,
        action: _ReadAction,
        board_id: str | None = None,
        list_id: str | None = None,
        board_filter: _BoardFilter = "all",
    ) -> str:
        """Read Trello boards, lists, and cards.

        Actions:
        - `list_boards`: List user boards filtered by `board_filter`.
        - `board_lists`: List lists and card counts on `board_id`.
        - `cards`: List cards in `list_id`.
        """
        try:
            client = self._require_client()
            if action == "list_boards":
                boards = client.list_boards(board_filter=board_filter)
                items = [
                    {
                        "id": board.id,
                        "name": board.name,
                        "description": getattr(board, "description", ""),
                        "url": board.url,
                        "closed": board.closed,
                        "starred": getattr(board, "starred", False),
                        "organization": getattr(board, "idOrganization", None),
                    }
                    for board in boards
                ]
                return json_dumps({"filter_used": board_filter, "total_boards": len(items), "boards": items})
            if action == "board_lists":
                safe_board = safe_path_segment(require_non_empty(board_id, "board_id"), "board_id")
                board = client.get_board(safe_board)
                lists_info = [
                    {"id": lst.id, "name": lst.name, "cards_count": len(lst.list_cards())} for lst in board.list_lists()
                ]
                return json_dumps({"lists": lists_info})
            safe_list = safe_path_segment(require_non_empty(list_id, "list_id"), "list_id")
            trello_list = client.get_list(safe_list)
            cards_info = [
                {
                    "id": card.id,
                    "name": card.name,
                    "description": card.description,
                    "url": card.url,
                    "labels": [label.name for label in card.labels],
                }
                for card in trello_list.list_cards()
            ]
            return json_dumps({"cards": cards_info})
        except Exception as exc:
            return error_json(str(exc))

    def trello_write(
        self,
        action: _WriteAction,
        name: str | None = None,
        board_id: str | None = None,
        list_name: str | None = None,
        list_id: str | None = None,
        card_id: str | None = None,
        description: str = "",
        default_lists: bool = False,
        pos: str = "bottom",
    ) -> str:
        """Create boards/lists/cards or move Trello cards.

        Actions:
        - `create_board`: Create a board (`name`, optional `default_lists`).
        - `create_list`: Create a list on `board_id` (`list_name` or `name`, optional `pos`).
        - `create_card`: Create a card on `board_id` in `list_name` (`name`, optional `description`).
        - `move_card`: Move `card_id` to `list_id`.
        """
        try:
            client = self._require_client(require_auth=True)
            if action == "create_board":
                board_name = require_non_empty(name, "name")
                board = client.add_board(board_name=board_name, default_lists=default_lists)
                return json_dumps({"id": board.id, "name": board.name, "url": board.url})
            if action == "create_list":
                safe_board = safe_path_segment(require_non_empty(board_id, "board_id"), "board_id")
                clean_list = require_non_empty(list_name or name, "list_name")
                board = client.get_board(safe_board)
                new_list = board.add_list(name=clean_list, pos=pos)
                return json_dumps(
                    {"id": new_list.id, "name": new_list.name, "pos": new_list.pos, "board_id": safe_board},
                )
            if action == "create_card":
                safe_board = safe_path_segment(require_non_empty(board_id, "board_id"), "board_id")
                clean_list = require_non_empty(list_name, "list_name")
                card_title = require_non_empty(name, "name")
                board = client.get_board(safe_board)
                target_list = next((lst for lst in board.list_lists() if lst.name.lower() == clean_list.lower()), None)
                if target_list is None:
                    return error_json(f"List '{clean_list}' not found on board.")
                card = target_list.add_card(name=card_title, desc=description)
                return json_dumps({"id": card.id, "name": card.name, "url": card.url, "list": clean_list})
            safe_card = safe_path_segment(require_non_empty(card_id, "card_id"), "card_id")
            safe_list = safe_path_segment(require_non_empty(list_id, "list_id"), "list_id")
            card = client.get_card(safe_card)
            card.change_list(safe_list)
            return json_dumps({"success": True, "card_id": safe_card, "new_list_id": safe_list})
        except Exception as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="trello",
    file_access=ToolFileAccess.NONE,
    display_name="Trello",
    description="Project board management with Trello API integration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTrello",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("TRELLO_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_secret",
            env=("TRELLO_API_SECRET",),
            label="API Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="token",
            env=("TRELLO_TOKEN",),
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["py-trello"],
    docs_url="https://docs.mindroom.chat/tools/project-management/#trello",
    function_names=("trello_read", "trello_write"),
    action_scoped_grants=("trello_read", "trello_write"),
)
def trello_tools() -> type[Toolkit]:
    """Return Trello tools for project board management."""
    return _TrelloTools
