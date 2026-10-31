"""The queued-message notice rewrites of the releases the recordings were made with, applied to the old store's view.

Those releases stored a reply's notice marked live (``True``) and then rewrote the stored runs: once the reply was
delivered, they left one notice marked ``"persisted"`` in the newest visible run that held the reply's notice, at the
place of its first copy there, and dropped the reply's other copies; a reply that opened its scope did the same for a
crashed reply's live notices. The current store keeps every notice as its run stored it, so comparison 1 replays
these rewrites on its view of the old store's messages (``MaskedBackend``), as the old store made them.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY, is_model_history_visible_run
from mindroom.llm.messages import Message
from tests.storage_oracle.backend import NOTICE_TURN_KEY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.runs import AnyRunRecord

_PERSISTED = "persisted"


def _marker(message: Message) -> object:
    marker = (message.provider_data or {}).get(QUEUED_MESSAGE_NOTICE_MARKER_KEY)
    return marker if marker in (True, _PERSISTED) else None


def _turn(message: Message) -> str | None:
    turn = (message.provider_data or {}).get(NOTICE_TURN_KEY)
    return turn if isinstance(turn, str) and turn else None


def _is_notice(message: Message, turn: str) -> bool:
    return _marker(message) is not None and _turn(message) == turn


def _notices(run: AnyRunRecord, turn: str) -> list[Message]:
    return [message for message in run.messages if _is_notice(message, turn)]


def finalize(runs: Sequence[AnyRunRecord], turn: str) -> list[AnyRunRecord]:
    """Return copies of the runs whose messages the old store's finalization of reply ``turn`` changed."""
    destination = next(
        (run for run in reversed(runs) if is_model_history_visible_run(run) and _notices(run, turn)),
        None,
    )
    all_matches = [message for run in runs for message in _notices(run, turn)]
    if not all_matches and destination is None:
        return []
    matches = _notices(destination, turn) if destination is not None else []
    text = next(
        (
            message.content
            for marker in (True, _PERSISTED)
            for message in matches
            if _marker(message) == marker and isinstance(message.content, str)
        ),
        None,
    )
    if (
        text is not None
        and len(all_matches) == 1
        and len(matches) == 1
        and _marker(matches[0]) == _PERSISTED
        and matches[0].content == text
    ):
        return []
    position = None
    if destination is not None:
        position = next(
            (index for index, message in enumerate(destination.messages) if _is_notice(message, turn)),
            None,
        )
    edited: dict[int, AnyRunRecord] = {}
    for run in runs:
        if _notices(run, turn):
            copy = deepcopy(run)
            copy.messages = [message for message in copy.messages if not _is_notice(message, turn)]
            edited[id(run)] = copy
    if destination is None or text is None:
        return list(edited.values())
    if id(destination) not in edited:
        edited[id(destination)] = deepcopy(destination)
    copy = edited[id(destination)]
    persisted = Message(
        role="user",
        content=text,
        provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: _PERSISTED, NOTICE_TURN_KEY: turn},
    )
    copy.messages.insert(len(copy.messages) if position is None else min(position, len(copy.messages)), persisted)
    return list(edited.values())


def recover(runs: Sequence[AnyRunRecord]) -> list[AnyRunRecord]:
    """Return copies of the runs the old store's recovery of crash-left live notices changed, as a reply opened them."""
    runs = list(runs)
    turns = {
        turn for run in runs for message in run.messages if _marker(message) is not None and (turn := _turn(message))
    }
    changed: dict[str, AnyRunRecord] = {}
    for turn in turns:
        markers = {_marker(message) for run in runs for message in _notices(run, turn)}
        if True not in markers or _PERSISTED in markers:
            continue
        for copy in finalize(runs, turn):
            changed[copy.run_id] = copy
            runs = [copy if run.run_id == copy.run_id else run for run in runs]
    return list(changed.values())
