"""Conversation databases as a layout version 4 release left them, for the steps that move them to the current one."""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Any

from mindroom.session_storage.agent_storage import prepare_conversation_database
from mindroom.session_storage.schema import TableNames, quote_identifier
from mindroom.session_storage.store import begun, connect_conversation_database, set_visible_messages, upsert_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.history.types import HistoryScope
    from mindroom.session_storage.store import ConversationStore


def store_rows(store: ConversationStore, session_id: str, rows: Sequence[dict[str, Any]]) -> None:
    """Write stored run dicts into ``store``'s session ``session_id`` as they are, the way their release's store did.

    A team run's members are rows of their own, which ``rows`` lists apart from it; each row's position in ``rows``
    is its place in the log.
    """
    names = TableNames.of_database(store.path)
    with closing(connect_conversation_database(store.path)) as connection, begun(connection, write=True):
        connection.execute(
            f"INSERT OR IGNORE INTO {quote_identifier(names.sessions)} (session_id, created_at) VALUES (?, 0)",  # noqa: S608
            (session_id,),
        )
        for seq, row in enumerate(rows, start=1):
            data = {key: value for key, value in row.items() if key != "member_responses"}
            data["session_id"] = session_id
            kind = "team" if "team_id" in data else "agent"
            created_at = data.get("created_at", 0)
            upsert_run(connection, names, session_id, data, kind, seq=seq, created_at=created_at, updated_at=created_at)
            set_visible_messages(connection, names, data["run_id"], data.get("messages") or [], data["status"])


def upgrade_from_layout_4(store: ConversationStore, owner: HistoryScope) -> None:
    """Record layout version 4 in ``store``'s database, then open it as the next store open does, which upgrades it."""
    names = TableNames.of_database(store.path)
    with closing(connect_conversation_database(store.path)) as connection, begun(connection, write=True):
        connection.execute(f"UPDATE {quote_identifier(names.log_meta)} SET version = 4 WHERE id = 1")  # noqa: S608
    prepare_conversation_database(store.path, names, owner)
