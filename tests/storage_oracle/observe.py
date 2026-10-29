"""The observables of spec v4 section 4.7: everything a reader of a conversation can see, as JSON-safe values.

Each observable is computed on its own, and one that raises records the error instead, so an unreadable row shows
where it fails without hiding the rest. Dicts are key-sorted when a recording is written, and every set is a sorted
list.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

from mindroom.history.request_messages import TurnMessages, build_request, build_resume_request, replayed_history
from mindroom.history.session_runs import session_runs
from mindroom.history.storage import (
    _read_scope_history_event_ids,
    has_queued_force_compaction,
    read_scope_seen_event_ids,
    run_folded_tool_call_ids,
)
from mindroom.history.types import ResolvedReplayPlan
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.media_inputs import MediaInputs
from mindroom.usage.storage import quote_identifier
from tests.history_helpers import replay_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.history.types import HistoryScope, ScopeSummary
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

_SETTINGS = replay_settings()
_PLAN = ResolvedReplayPlan(mode="configured", estimated_tokens=0, add_history_to_context=True)
_PROBE_ID = "probe"


def _error(error: Exception) -> dict[str, str]:
    return {"error": f"{type(error).__name__}: {error}"}


def _safely(read: Callable[[], object]) -> object:
    try:
        return read()
    except Exception as error:
        return _error(error)


def _summary(summary: ScopeSummary | None) -> dict[str, str] | None:
    return None if summary is None else {"text": summary.text, "updated_at": summary.updated_at.isoformat()}


def _session(session: ConversationSession | None) -> dict[str, object] | None:
    if session is None:
        return None
    return {
        "kind": session.kind,
        "owner_id": session.owner_id,
        "metadata": session.metadata,
        "session_state": session.session_state,
        "created_at": session.created_at,
        "summary": _summary(session.summary),
    }


def _row(store: ConversationStore, session_id: str) -> dict[str, object] | None:
    """Return the session row's requester, update time, and ``session_data`` (state and usage totals).

    The state shows inside ``session_data``, where the layouts the recordings were made in kept it.
    """
    with closing(sqlite3.connect(store.path)) as connection:
        row = connection.execute(
            "SELECT user_id, created_at, updated_at, session_data, session_state "  # noqa: S608
            f"FROM {quote_identifier(store.names.sessions)} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    user_id, created_at, updated_at, session_data, session_state = row
    data = _json(session_data)
    if session_state is not None:
        data = {**(data if isinstance(data, dict) else {}), "session_state": _json(session_state)}
    return {"user_id": user_id, "created_at": created_at, "updated_at": updated_at, "session_data": data}


def _ledger(store: ConversationStore, session_id: str) -> list[list[object]]:
    """Return the usage ledger's rows of the session: run id (None for a run without one) and snapshot."""
    with closing(sqlite3.connect(store.path)) as connection:
        rows = connection.execute(
            f"SELECT run_id, usage_data FROM {quote_identifier(store.names.usage)} WHERE session_id = ?",  # noqa: S608
            (session_id,),
        ).fetchall()
    return sorted(([run_id, _json(data)] for run_id, data in rows), key=lambda row: json.dumps(row, sort_keys=True))


def _json(raw: object) -> object:
    if not isinstance(raw, str | bytes):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return {"undecodable": raw if isinstance(raw, str) else raw.decode(errors="replace")}


def _messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    return [message.to_dict() for message in messages]


def _probe_request(session: ConversationSession | None, scope: HistoryScope) -> list[dict[str, Any]]:
    """Return the request a reply's probe prompt sends after its system message, replaying the whole history."""
    history, summary = replayed_history(session, scope, _SETTINGS, _PLAN)
    turn = TurnMessages(prompt=Message(role="user", content="probe", id=_PROBE_ID, created_at=0))
    return _messages(build_request(summary=summary, history=history, turn=turn, media=MediaInputs()))


def _resume_requests(session: ConversationSession | None, scope: HistoryScope) -> dict[str, object]:
    """Return the request that resumes each paused run, and for a team run each embedded member's own."""
    if session is None:
        return {}
    history, summary = replayed_history(session, scope, _SETTINGS, _PLAN)
    requests: dict[str, object] = {}
    for run in session.runs:
        if run.parent_run_id is not None or run.status is not RunStatus.paused:
            continue
        members = run.member_responses if isinstance(run, TeamRunRecord) else []
        requests[run.run_id] = {
            "request": _messages(build_resume_request(paused=run.messages, summary=summary, history=history)),
            "members": {
                member.run_id: {
                    "status": member.status.value,
                    "request": _messages(build_resume_request(paused=member.messages, summary=None, history=[])),
                }
                for member in members
            },
        }
    return requests


def observe(
    store: ConversationStore,
    session_id: str,
    scope: HistoryScope,
    *,
    run_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    """Return the observables of one session of ``store`` in ``scope``.

    ``run_ids`` are the runs whose folded tool calls are read; by default every run ``list_runs`` returns.
    """
    session, load_error = _attempt(lambda: store.load_session(session_id))
    listed, list_error = _attempt(lambda: store.list_runs(session_id))
    folded_of = list(run_ids) if run_ids is not None else [run.run_id for run in listed or []]
    return {
        "session": load_error or _session(session),
        "row": _safely(lambda: _row(store, session_id)),
        "loaded_runs": load_error or ([run.to_dict() for run in session.runs] if session is not None else []),
        "request": _safely(lambda: _probe_request(session, scope)),
        "resume_requests": _safely(lambda: _resume_requests(session, scope)),
        "seen_event_ids": _safely(
            lambda: sorted(read_scope_seen_event_ids(store, session, scope)) if session is not None else [],
        ),
        "redaction_candidates": _safely(
            lambda: sorted(_read_scope_history_event_ids(store, session)) if session is not None else [],
        ),
        "folded_tool_call_ids": _safely(lambda: _folded_tool_call_ids(store, session_id, folded_of)),
        "list_runs": list_error
        or [run.to_dict() for run in sorted(listed or [], key=lambda run: (run.created_at, run.run_id))],
        "session_runs": _safely(lambda: [run.run_id for run in session_runs(store, session_id)]),
        "force_queued": _safely(lambda: has_queued_force_compaction(store, session_id, scope)),
        "ledger": _safely(lambda: _ledger(store, session_id)),
    }


def _attempt[Result](read: Callable[[], Result]) -> tuple[Result | None, dict[str, str] | None]:
    try:
        return read(), None
    except Exception as error:
        return None, _error(error)


def _folded_tool_call_ids(store: ConversationStore, session_id: str, run_ids: Sequence[str]) -> dict[str, list[str]]:
    """Return the tool calls each run's mid-turn folds summarized, for the runs that have any."""
    folded = {
        run_id: sorted(run_folded_tool_call_ids(store, session_id=session_id, run_id=run_id)) for run_id in run_ids
    }
    return {run_id: call_ids for run_id, call_ids in folded.items() if call_ids}
