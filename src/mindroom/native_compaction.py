"""Non-destructive provider checkpoint selection shared by replay and history budgeting.

The checkpoint lives on the assistant message which produced it. Canonical messages
remain available for another model and for the portable text compactor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import NativeCompactionReplay
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord

logger = get_logger(__name__)

# The ``provider_data`` key under which a completed assistant response stores its native-compaction state.
NATIVE_CHECKPOINT_KEY = "mindroom_native_compaction"
# The ``provider_data`` key under which the answer to a request resent without provider-encrypted replay state stores
# its route's ``Capabilities.replay_reset_scope``: a reset, before which that route replays no such state.
ENCRYPTED_REPLAY_RESET_KEY = "mindroom_encrypted_replay_reset"


def native_compaction_route(*, provider: str, model_id: str, endpoint: str, history_generation: str) -> str:
    """Return the replay route one model, endpoint, and history generation share."""
    return hashlib.sha256(json.dumps([provider, model_id, endpoint, history_generation]).encode()).hexdigest()


def native_checkpoint_state(
    items: Sequence[dict[str, Any]],
    settings: NativeCompactionReplay | None,
    *,
    checkpoint_prefix: bool = False,
) -> dict[str, Any] | None:
    """Return the state a completed response stores under ``NATIVE_CHECKPOINT_KEY``: its settings and latest checkpoint.

    Every completed response records its route, None when native compaction is off, because approval rebuilds and
    signed-thinking replay need it even when the provider did not compact on this request. A checkpoint in ``items``
    means the provider compacted on this request, which no other MindRoom log reports. ``checkpoint_prefix`` marks
    output whose own request replayed a checkpoint.
    """
    if settings is None:
        return None
    state: dict[str, Any] = {"route": settings.route, "threshold": settings.threshold}
    if settings.basis is not None:
        state["basis"] = settings.basis
    last = next(
        (index for index in range(len(items) - 1, -1, -1) if _is_checkpoint(items[index])),
        None,
    )
    state["checkpoint_prefix"] = checkpoint_prefix or last is not None
    if last is not None:
        state["items"] = list(items[last:])
        logger.info(
            "Native compaction checkpoint",
            threshold_tokens=settings.threshold,
            checkpoint_chars=len(json.dumps([items[last]], ensure_ascii=False)),
        )
    return state


def recorded_native_settings(message: Message) -> NativeCompactionReplay | None:
    """Read the effective native policy from one completed assistant response."""
    state = (message.provider_data or {}).get(NATIVE_CHECKPOINT_KEY)
    if message.role != "assistant" or not isinstance(state, dict):
        return None
    route, threshold = state.get("route"), state.get("threshold")
    if not isinstance(route, str) or not route or "threshold" not in state:
        return None
    if threshold is not None and (type(threshold) is not int or threshold <= 0):
        return None
    basis = state.get("basis")
    return NativeCompactionReplay(route=route, threshold=threshold, basis=basis if isinstance(basis, str) else None)


def native_replay_route_matches(message: Message, route: str | None) -> bool | None:
    """Compare effective prefix provenance; None means legacy history without it."""
    data = message.provider_data or {}
    if NATIVE_CHECKPOINT_KEY not in data:
        return None
    state = data[NATIVE_CHECKPOINT_KEY]
    if state is None:
        return route is None
    if not isinstance(state, dict) or not isinstance(state.get("route"), str):
        return False
    # Missing provenance cannot prove that legacy thinking used canonical input.
    prefix_route = state["route"] if state.get("checkpoint_prefix", True) else None
    return prefix_route == route


def _is_checkpoint(item: dict[str, Any]) -> bool:
    if item.get("type") != "compaction":
        return False
    # Claude's explicit null summary is a no-op, even when other fields exist.
    if "content" in item:
        return isinstance(item["content"], str) and bool(item["content"].strip())
    return isinstance(item.get("encrypted_content"), str) and bool(item["encrypted_content"])


def checkpoint_items(message: Message, route: str | None) -> list[dict[str, Any]]:
    """Read compatible, structurally valid checkpoint data from persisted input."""
    if route is None or not message.provider_data or message.role != "assistant":
        return []
    checkpoint = message.provider_data.get(NATIVE_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict) or checkpoint.get("route") != route:
        return []
    items = checkpoint.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
        return []
    return items if _is_checkpoint(items[0]) else []


def native_checkpoint_id(message: Message) -> str | None:
    """Return the message's id when the provider compacted on it, whatever its route, else None."""
    state = (message.provider_data or {}).get(NATIVE_CHECKPOINT_KEY)
    if not isinstance(state, dict) or not checkpoint_items(message, state.get("route")):
        return None
    return message.id


def replay_basis(summary: str | None, runs: Sequence[AnyRunRecord]) -> str:
    """Identify the history a request replays: the summary in force and the runs after it, in order."""
    return hashlib.sha256(json.dumps([summary, [run.run_id for run in runs]]).encode()).hexdigest()


def request_replay_basis(summary: str | None, runs: Sequence[AnyRunRecord], route: str) -> str:
    """Return the basis a native request on ``route`` records; empty when its replay leaves out some of ``runs``.

    The request replays from the latest checkpoint on its route, which covers only what its own request replayed:
    a run that checkpointed before waiting for approval sorts after the turns it waited for. A checkpoint whose
    history is unknown, such as one recorded before bases existed, counts as covering, as native replay assumes.
    """
    for index in range(len(runs) - 1, -1, -1):
        for message in reversed(runs[index].messages):
            if checkpoint_items(message, route):
                recorded = (message.provider_data or {})[NATIVE_CHECKPOINT_KEY].get("basis")
                covering = recorded is None or recorded == replay_basis(summary, runs[:index])
                return replay_basis(summary, runs) if covering else ""
    return replay_basis(summary, runs)


def _readable_checkpoint_summary(message: Message) -> tuple[str, str] | None:
    """Return ``(basis, summary_text)`` when ``message`` carries a readable checkpoint with a recorded basis."""
    state = (message.provider_data or {}).get(NATIVE_CHECKPOINT_KEY)
    if not isinstance(state, dict) or not isinstance(basis := state.get("basis"), str):
        return None
    items = checkpoint_items(message, state.get("route"))
    content = items[0].get("content") if items else None
    return (basis, content) if isinstance(content, str) else None


def iter_readable_checkpoints(runs: Sequence[AnyRunRecord]) -> Iterator[tuple[int, int, str, str]]:
    """Yield ``(run_index, message_position, recorded_basis, summary_text)`` newest-first for readable checkpoints."""
    for index in range(len(runs) - 1, -1, -1):
        messages = runs[index].messages
        for position in range(len(messages) - 1, -1, -1):
            if (checkpoint := _readable_checkpoint_summary(messages[position])) is not None:
                yield index, position, checkpoint[0], checkpoint[1]


def checkpoint_summary(message: Message, basis: str) -> str | None:
    """Return the readable text of the message's checkpoint, whatever its route, if its request replayed ``basis``.

    Encrypted checkpoints have no readable text, and checkpoints of unknown history never match.
    """
    checkpoint = _readable_checkpoint_summary(message)
    return checkpoint[1] if checkpoint is not None and checkpoint[0] == basis else None


def checkpoint_tail(messages: Sequence[Message], checkpoint: int) -> list[Message]:
    """Return what replaying the checkpoint on ``messages[checkpoint]`` keeps: it, its tail, and the system messages."""
    return [message for index, message in enumerate(messages) if index >= checkpoint or message.role == "system"]


@dataclass(frozen=True, slots=True)
class NativeReplay:
    """What a request replays of its messages: the one projection its wire, its size, and its usage anchor read.

    ``messages`` are the latest checkpoint with its tail and the system messages, or every message when none stands.
    Those before ``encrypted_from`` go without provider-encrypted state. ``boundary`` indexes the request's message
    the replay starts over at, -1 for none: usage an answer up to it reported sized another replay.
    """

    messages: list[Message]
    encrypted_from: int = 0
    boundary: int = -1


def native_replay(
    messages: Sequence[Message],
    route: str | None,
    *,
    reset_scope: str | None = None,
    stripped: bool = False,
) -> NativeReplay:
    """Project ``messages`` from the latest checkpoint on ``route`` or reset on ``reset_scope``, whichever is later.

    A checkpoint stands for what precedes it. A reset keeps every message and sends those before it without encrypted
    state, as a ``stripped`` request sends them all.
    """
    if stripped:
        return NativeReplay(list(messages), encrypted_from=len(messages), boundary=len(messages))
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if checkpoint_items(message, route):
            return NativeReplay(checkpoint_tail(messages, index), boundary=index)
        if (
            reset_scope is not None
            and message.role == "assistant"
            and (message.provider_data or {}).get(ENCRYPTED_REPLAY_RESET_KEY) == reset_scope
        ):
            return NativeReplay(list(messages), encrypted_from=index, boundary=index)
    return NativeReplay(list(messages))


def native_replay_messages(messages: Sequence[Message], route: str | None) -> list[Message]:
    """Project the latest checkpoint plus tail, retaining current system messages."""
    return native_replay(messages, route).messages
