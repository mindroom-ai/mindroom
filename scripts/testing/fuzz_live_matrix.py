"""Replay concurrent Matrix mutations against disposable Tuwunel and MindRoom.

Unlike ``fuzz_matrix_event_cache.py``, this runner crosses the real Matrix
transport and the complete MindRoom sync/dispatch/cache path. It starts an
isolated Tuwunel, a deterministic OpenAI-compatible stub, and the current
worktree's MindRoom process. Every run uses disposable Matrix accounts and
removes the isolated stack afterward.

Run with ``uv run python scripts/testing/fuzz_live_matrix.py --seed 42``.
Use ``--save-trace`` and ``--trace`` to replay the same logical event history
on a new disposable server.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx
import yaml

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from io import TextIOWrapper

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
MODEL_ID = "mindroom-live-fuzz"
AGENT_NAME = "general"
ROOM_KEY = "lobby"
RECOVERY_TIMELINE_LIMIT = 50
REGISTRY_READ_ATTEMPTS = 3
REGISTRY_READ_RETRY_SECONDS = 0.05


def _required_int(value: Mapping[str, object], key: str) -> int:
    field = value.get(key)
    if not isinstance(field, int) or isinstance(field, bool):
        msg = f"Live Matrix fuzz operation field {key!r} must be an integer"
        raise TypeError(msg)
    return field


def _required_string(value: Mapping[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        msg = f"Live Matrix fuzz operation field {key!r} must be a string"
        raise TypeError(msg)
    return field


def _optional_int(value: Mapping[str, object], key: str, default: int) -> int:
    field = value.get(key, default)
    if not isinstance(field, int) or isinstance(field, bool):
        msg = f"Live Matrix fuzz operation field {key!r} must be an integer"
        raise TypeError(msg)
    return field


class LiveOperationKind(StrEnum):
    """User-visible Matrix mutation families."""

    THREAD_MESSAGE = "thread_message"
    PLAIN_REPLY = "plain_reply"
    EDIT = "edit"
    REACTION = "reaction"
    REDACTION = "redaction"
    IDEMPOTENT_RETRY = "idempotent_retry"
    RESTART_MINDROOM = "restart_mindroom"


_RECOVERY_OPERATION_KINDS = {
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.IDEMPOTENT_RETRY,
}


@dataclass(frozen=True, slots=True)
class LiveOperation:
    """One replayable live Matrix action."""

    operation_id: int
    kind: LiveOperationKind
    thread: int
    target: str | None
    room: int = 0
    client: int = 0

    @property
    def event_ref(self) -> str:
        """Return the logical reference for this operation's event."""
        return f"op:{self.operation_id}"

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LiveOperation:
        """Parse one serialized operation."""
        raw_target = value.get("target")
        if raw_target is not None and not isinstance(raw_target, str):
            msg = "Live Matrix fuzz operation target must be a string or null"
            raise TypeError(msg)
        return cls(
            operation_id=_required_int(value, "operation_id"),
            kind=LiveOperationKind(_required_string(value, "kind")),
            thread=_required_int(value, "thread"),
            target=raw_target,
            room=_optional_int(value, "room", 0),
            client=_optional_int(value, "client", 0),
        )


@dataclass(frozen=True, slots=True)
class LiveFuzzScenario:
    """Concurrent live batches with logical references instead of event IDs."""

    thread_count: int
    batches: tuple[tuple[LiveOperation, ...], ...]
    profile: str = "fuzz"
    room_count: int = 1
    client_count: int = 1

    def to_json(self) -> str:
        """Serialize the complete trace for exact replay on a fresh server."""
        return json.dumps(
            {
                "version": 1,
                "profile": self.profile,
                "room_count": self.room_count,
                "client_count": self.client_count,
                "thread_count": self.thread_count,
                "batches": [[asdict(operation) for operation in batch] for batch in self.batches],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> LiveFuzzScenario:
        """Load a trace emitted by :meth:`to_json`."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            msg = "unsupported live Matrix fuzz trace"
            raise ValueError(msg)
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, list):
            msg = "live Matrix fuzz trace is missing batches"
            raise TypeError(msg)
        scenario = cls(
            thread_count=_required_int(payload, "thread_count"),
            batches=tuple(
                tuple(LiveOperation.from_dict(cast("dict[str, object]", operation)) for operation in batch)
                for batch in raw_batches
            ),
            profile=_required_string(payload, "profile"),
            room_count=_optional_int(payload, "room_count", 1),
            client_count=_optional_int(payload, "client_count", 1),
        )
        scenario.validate()
        return scenario

    def validate(self) -> None:
        """Reject traces with impossible same-batch or forward dependencies."""
        if self.thread_count < 1 or self.room_count < 1 or self.client_count < 1:
            msg = "live Matrix fuzz trace must contain at least one room, client, and thread"
            raise ValueError(msg)
        if self.profile not in {"fuzz", "recovery", "saturation"}:
            msg = f"unsupported live Matrix fuzz profile {self.profile!r}"
            raise ValueError(msg)
        if self.profile == "recovery":
            known_events = {
                f"root:{room}:{thread}" for room in range(self.room_count) for thread in range(self.thread_count)
            }
        else:
            known_events = {f"root:{thread}" for thread in range(self.thread_count)}
        known_responses = {f"response:{event_ref}" for event_ref in known_events}
        event_rooms = {event_ref: (_logical_ref_room(event_ref) or 0) for event_ref in known_events | known_responses}
        message_events = set(known_events)
        operation_ids: set[int] = set()
        recovery_message_lanes: set[tuple[int, int, int]] = set()

        for batch in self.batches:
            _validate_live_batch_shape(batch)
            new_events: set[str] = set()
            new_responses: set[str] = set()
            new_messages: set[str] = set()
            for operation in batch:
                _validate_profile_operation(self.profile, operation)
                _validate_live_operation(
                    operation,
                    thread_count=self.thread_count,
                    operation_ids=operation_ids,
                    allowed_targets=known_events | known_responses,
                    event_rooms=event_rooms,
                    message_events=message_events,
                    room_count=self.room_count,
                    client_count=self.client_count,
                )
                if operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                    new_events.add(operation.event_ref)
                    event_rooms[operation.event_ref] = operation.room
                if operation.kind in {
                    LiveOperationKind.THREAD_MESSAGE,
                    LiveOperationKind.PLAIN_REPLY,
                }:
                    if self.profile == "recovery":
                        lane = (operation.room, operation.client, operation.thread)
                        if lane in recovery_message_lanes:
                            msg = (
                                "recovery messages must use unique room, client, and thread lanes "
                                "so intentional coalescing does not weaken the exact-reply oracle"
                            )
                            raise ValueError(msg)
                        recovery_message_lanes.add(lane)
                    new_messages.add(operation.event_ref)
                    new_responses.add(f"response:{operation.event_ref}")
                    event_rooms[f"response:{operation.event_ref}"] = operation.room

            known_events.update(new_events)
            known_responses.update(new_responses)
            message_events.update(new_messages)


def _validate_live_batch_shape(batch: tuple[LiveOperation, ...]) -> None:
    if not batch:
        msg = "live Matrix fuzz batches must not be empty"
        raise ValueError(msg)
    restart_operations = [operation for operation in batch if operation.kind is LiveOperationKind.RESTART_MINDROOM]
    if restart_operations and len(batch) != 1:
        msg = "MindRoom restart must be a singleton batch"
        raise ValueError(msg)
    reply_threads = [
        (operation.room, operation.thread)
        for operation in batch
        if operation.kind
        in {
            LiveOperationKind.THREAD_MESSAGE,
            LiveOperationKind.PLAIN_REPLY,
        }
    ]
    if len(reply_threads) != len(set(reply_threads)):
        msg = "same-thread messages requiring replies must use separate batches"
        raise ValueError(msg)


def _validate_profile_operation(profile: str, operation: LiveOperation) -> None:
    if profile == "recovery" and operation.kind not in _RECOVERY_OPERATION_KINDS:
        msg = f"recovery profile does not support {operation.kind}"
        raise ValueError(msg)


def _validate_live_operation(
    operation: LiveOperation,
    *,
    thread_count: int,
    operation_ids: set[int],
    allowed_targets: set[str],
    event_rooms: Mapping[str, int],
    message_events: set[str],
    room_count: int,
    client_count: int,
) -> None:
    if operation.operation_id in operation_ids:
        msg = f"duplicate live Matrix fuzz operation ID {operation.operation_id}"
        raise ValueError(msg)
    operation_ids.add(operation.operation_id)
    _validate_operation_location(
        operation,
        thread_count=thread_count,
        room_count=room_count,
        client_count=client_count,
    )
    if operation.kind is LiveOperationKind.RESTART_MINDROOM:
        if operation.target is not None:
            msg = "MindRoom restart must not have a target"
            raise ValueError(msg)
        return
    if operation.target is None:
        msg = f"{operation.kind} requires a target"
        raise ValueError(msg)
    if operation.target not in allowed_targets:
        msg = f"unknown or same-batch target {operation.target!r}"
        raise ValueError(msg)
    if event_rooms[operation.target] != operation.room:
        msg = f"cross-room target {operation.target!r} from room {operation.room}"
        raise ValueError(msg)
    if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY and operation.target not in message_events:
        msg = "idempotent retries may only target messages"
        raise ValueError(msg)


def _validate_operation_location(
    operation: LiveOperation,
    *,
    thread_count: int,
    room_count: int,
    client_count: int,
) -> None:
    if not 0 <= operation.thread < thread_count:
        msg = f"invalid thread {operation.thread}"
        raise ValueError(msg)
    if not 0 <= operation.room < room_count:
        msg = f"invalid room {operation.room}"
        raise ValueError(msg)
    if not 0 <= operation.client < client_count:
        msg = f"invalid client {operation.client}"
        raise ValueError(msg)


def _logical_ref_room(logical_ref: str) -> int | None:
    """Extract the room slot encoded by recovery root references."""
    root_ref = logical_ref.removeprefix("response:")
    parts = root_ref.split(":")
    if len(parts) == 3 and parts[0] == "root":
        return int(parts[1])
    return None


_WEIGHTED_KINDS = (
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.THREAD_MESSAGE,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.PLAIN_REPLY,
    LiveOperationKind.EDIT,
    LiveOperationKind.EDIT,
    LiveOperationKind.REACTION,
    LiveOperationKind.REACTION,
    LiveOperationKind.REACTION,
    LiveOperationKind.REDACTION,
    LiveOperationKind.IDEMPOTENT_RETRY,
)


@dataclass(slots=True)
class _ScenarioGenerationState:
    messages: dict[int, list[str]]
    responses: dict[int, list[str]]
    editable: dict[int, list[str]]
    reaction_targets: dict[int, list[str]]
    redactable: dict[int, list[str]]
    redacted: set[str]


def _initial_generation_state(thread_count: int) -> _ScenarioGenerationState:
    return _ScenarioGenerationState(
        messages={thread: [f"root:{thread}"] for thread in range(thread_count)},
        responses={thread: [f"response:root:{thread}"] for thread in range(thread_count)},
        editable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        reaction_targets={thread: [f"root:{thread}", f"response:root:{thread}"] for thread in range(thread_count)},
        redactable={thread: [f"root:{thread}"] for thread in range(thread_count)},
        redacted=set(),
    )


def _choose_operation(
    randomizer: random.Random,
    state: _ScenarioGenerationState,
    *,
    operation_id: int,
    thread_count: int,
) -> LiveOperation:
    thread = randomizer.randrange(thread_count)
    kind = randomizer.choice(_WEIGHTED_KINDS)
    available_edits = [target for target in state.editable[thread] if target not in state.redacted]
    available_redactions = [target for target in state.redactable[thread] if target not in state.redacted]
    available_retries = [target for target in state.messages[thread] if target not in state.redacted]

    if kind is LiveOperationKind.THREAD_MESSAGE:
        target = randomizer.choice(state.messages[thread])
    elif kind is LiveOperationKind.PLAIN_REPLY:
        target = randomizer.choice(state.responses[thread])
    elif kind is LiveOperationKind.EDIT:
        target = randomizer.choice(available_edits or state.messages[thread])
    elif kind is LiveOperationKind.REACTION:
        target = randomizer.choice(state.reaction_targets[thread])
    elif kind is LiveOperationKind.REDACTION and available_redactions:
        target = randomizer.choice(available_redactions)
    elif kind is LiveOperationKind.IDEMPOTENT_RETRY and available_retries:
        target = randomizer.choice(available_retries)
    else:
        kind = LiveOperationKind.REACTION
        target = randomizer.choice(state.reaction_targets[thread])
    return LiveOperation(operation_id=operation_id, kind=kind, thread=thread, target=target)


def _update_generation_state(
    state: _ScenarioGenerationState,
    operations: Collection[LiveOperation],
) -> None:
    for operation in operations:
        if operation.kind in {
            LiveOperationKind.THREAD_MESSAGE,
            LiveOperationKind.PLAIN_REPLY,
        }:
            state.messages[operation.thread].append(operation.event_ref)
            state.responses[operation.thread].append(f"response:{operation.event_ref}")
            state.editable[operation.thread].append(operation.event_ref)
            state.reaction_targets[operation.thread].extend(
                (operation.event_ref, f"response:{operation.event_ref}"),
            )
            state.redactable[operation.thread].append(operation.event_ref)
        elif operation.kind in {LiveOperationKind.EDIT, LiveOperationKind.REACTION}:
            state.reaction_targets[operation.thread].append(operation.event_ref)
            state.redactable[operation.thread].append(operation.event_ref)
        elif operation.kind is LiveOperationKind.REDACTION:
            assert operation.target is not None
            state.redacted.add(operation.target)


def live_scenario_from_seed(
    seed: int,
    *,
    steps: int,
    thread_count: int = 45,
    max_batch_size: int = 16,
    restart_interval: int = 100,
) -> LiveFuzzScenario:
    """Generate realistic concurrent batches with only prior-batch dependencies."""
    if steps < 1 or thread_count < 1 or max_batch_size < 1 or restart_interval < 0:
        msg = "steps, threads, and batch size must be positive; restart interval must be non-negative"
        raise ValueError(msg)

    randomizer = random.Random(seed)  # noqa: S311 - deterministic test trace generation
    state = _initial_generation_state(thread_count)
    batches: list[tuple[LiveOperation, ...]] = []
    operation_id = 0
    generated = 0
    next_restart = restart_interval

    while generated < steps:
        if restart_interval and generated >= next_restart:
            batches.append(
                (
                    LiveOperation(
                        operation_id=operation_id,
                        kind=LiveOperationKind.RESTART_MINDROOM,
                        thread=0,
                        target=None,
                    ),
                ),
            )
            operation_id += 1
            next_restart += restart_interval

        batch_size = min(steps - generated, randomizer.randint(1, max_batch_size))
        operations: list[LiveOperation] = []
        reply_threads: set[int] = set()
        for offset in range(batch_size):
            operation = _choose_operation(
                randomizer,
                state,
                operation_id=operation_id + offset,
                thread_count=thread_count,
            )
            needs_reply = operation.kind in {
                LiveOperationKind.THREAD_MESSAGE,
                LiveOperationKind.PLAIN_REPLY,
            }
            if needs_reply and operation.thread in reply_threads:
                operation = LiveOperation(
                    operation_id=operation.operation_id,
                    kind=LiveOperationKind.REACTION,
                    thread=operation.thread,
                    target=randomizer.choice(state.reaction_targets[operation.thread]),
                )
                needs_reply = False
            operations.append(operation)
            if needs_reply:
                reply_threads.add(operation.thread)
        operation_id += batch_size

        batches.append(tuple(operations))
        generated += len(operations)
        _update_generation_state(state, operations)

    scenario = LiveFuzzScenario(thread_count=thread_count, batches=tuple(batches))
    scenario.validate()
    return scenario


def recovery_scenario_from_seed(
    seed: int,
    *,
    messages_per_room: int = 64,
    room_count: int = 3,
    thread_count: int = 12,
    client_count: int = 6,
    max_batch_size: int = 12,
) -> LiveFuzzScenario:
    """Build a deterministic multi-room outage and limited-sync recovery schedule."""
    if min(messages_per_room, room_count, thread_count, client_count, max_batch_size) < 1:
        msg = "recovery profile dimensions must be positive"
        raise ValueError(msg)
    if messages_per_room <= RECOVERY_TIMELINE_LIMIT:
        msg = (
            f"recovery profile requires more than {RECOVERY_TIMELINE_LIMIT} messages per room to exceed the sync window"
        )
        raise ValueError(msg)
    if messages_per_room > client_count * thread_count:
        msg = (
            "recovery profile needs at least one unique client/thread lane per message "
            "to distinguish transport loss from intentional coalescing"
        )
        raise ValueError(msg)

    randomizer = random.Random(seed)  # noqa: S311 - deterministic test trace generation
    pending = dict.fromkeys(range(room_count), messages_per_room)
    lanes = {
        room: [(client, thread) for client in range(client_count) for thread in range(thread_count)]
        for room in range(room_count)
    }
    for room_lanes in lanes.values():
        randomizer.shuffle(room_lanes)
    batches: list[tuple[LiveOperation, ...]] = []
    sent_messages: list[LiveOperation] = []
    operation_id = 0
    round_index = 0
    while any(pending.values()):
        batch: list[LiveOperation] = []
        used_threads: set[tuple[int, int]] = set()
        candidates = [room for room in range(room_count) for _ in range(min(pending[room], max_batch_size))]
        randomizer.shuffle(candidates)
        for room in candidates:
            if len(batch) >= max_batch_size or pending[room] == 0:
                continue
            available_lanes = [lane for lane in lanes[room] if (room, lane[1]) not in used_threads]
            if not available_lanes:
                continue
            client, thread = randomizer.choice(available_lanes)
            lanes[room].remove((client, thread))
            used_threads.add((room, thread))
            root_ref = f"root:{room}:{thread}"
            kind = (
                LiveOperationKind.PLAIN_REPLY
                if (operation_id + round_index) % 3 == 0
                else LiveOperationKind.THREAD_MESSAGE
            )
            target = f"response:{root_ref}" if kind is LiveOperationKind.PLAIN_REPLY else root_ref
            operation = LiveOperation(
                operation_id=operation_id,
                kind=kind,
                thread=thread,
                target=target,
                room=room,
                client=client,
            )
            batch.append(operation)
            sent_messages.append(operation)
            pending[room] -= 1
            operation_id += 1
        batches.append(tuple(batch))

        retry_candidates = [operation for operation in sent_messages[-len(batch) :] if operation.operation_id % 11 == 0]
        if retry_candidates:
            batches.append(
                tuple(
                    LiveOperation(
                        operation_id=operation_id + offset,
                        kind=LiveOperationKind.IDEMPOTENT_RETRY,
                        thread=source.thread,
                        target=source.event_ref,
                        room=source.room,
                        client=source.client,
                    )
                    for offset, source in enumerate(retry_candidates)
                ),
            )
            operation_id += len(retry_candidates)
        round_index += 1

    scenario = LiveFuzzScenario(
        thread_count=thread_count,
        batches=tuple(batches),
        profile="recovery",
        room_count=room_count,
        client_count=client_count,
    )
    scenario.validate()
    return scenario


def saturation_scenario(
    *,
    hot_turns: int = 100,
    parallel_threads: int = 12,
    parallel_turns: int = 8,
) -> LiveFuzzScenario:
    """Reproduce the long-thread plus 12-way saturation workload."""
    thread_count = parallel_threads + 1
    batches: list[tuple[LiveOperation, ...]] = []
    operation_id = 0
    hot_parent = "response:root:0"
    for _ in range(hot_turns):
        operation = LiveOperation(
            operation_id=operation_id,
            kind=LiveOperationKind.THREAD_MESSAGE,
            thread=0,
            target=hot_parent,
        )
        batches.append((operation,))
        hot_parent = f"response:{operation.event_ref}"
        operation_id += 1

    parallel_parents = {thread: f"response:root:{thread}" for thread in range(1, thread_count)}
    for _ in range(parallel_turns):
        batch: list[LiveOperation] = []
        for thread in range(1, thread_count):
            operation = LiveOperation(
                operation_id=operation_id,
                kind=LiveOperationKind.THREAD_MESSAGE,
                thread=thread,
                target=parallel_parents[thread],
            )
            batch.append(operation)
            parallel_parents[thread] = f"response:{operation.event_ref}"
            operation_id += 1
        batches.append(tuple(batch))

    scenario = LiveFuzzScenario(
        thread_count=thread_count,
        batches=tuple(batches),
        profile="saturation",
    )
    scenario.validate()
    return scenario


class _ModelHandler(BaseHTTPRequestHandler):
    """Small deterministic OpenAI-compatible endpoint for live transport tests."""

    protocol_version = "HTTP/1.1"
    call_ids = itertools.count(1)
    stream_segments = 4
    stream_delay = 0.001

    def _send_json(self, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": MODEL_ID, "object": "model", "owned_by": "mindroom-fuzz"}],
                },
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length))
        call_id = next(self.call_ids)
        content = self.response_text_for(call_id)
        if payload.get("stream") is True:
            self._send_stream(call_id, content)
            return
        self._send_json(
            {
                "id": f"live-fuzz-response-{call_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    },
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    @classmethod
    def response_text_for(cls, call_id: int) -> str:
        """Return the only complete body accepted for one model call."""
        segments = " ".join(f"segment-{index:03d}" for index in range(cls.stream_segments))
        return f"LIVE-FUZZ call={call_id} {segments} END call={call_id}"

    def _send_stream(self, call_id: int, content: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {
            "id": f"live-fuzz-response-{call_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
        }
        self._write_sse(
            {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
        )
        words = content.split()
        for index in range(0, len(words), 2):
            chunk_text = " ".join(words[index : index + 2])
            if index + 2 < len(words):
                chunk_text += " "
            self._write_sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk_text},
                            "finish_reason": None,
                        },
                    ],
                },
            )
            time.sleep(self.stream_delay)
        self._write_sse(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _write_sse(self, payload: Mapping[str, object]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401
        """Keep hundreds of deterministic model calls out of test output."""


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast("int", sock.getsockname()[1])


def _run_command(*command: str) -> str:
    result = subprocess.run(
        command,
        check=False,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        msg = f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)
    return result.stdout


def _read_instance_registry() -> dict[str, Any]:
    """Read the non-atomic deploy registry without trusting a partial write."""
    for attempt in range(REGISTRY_READ_ATTEMPTS):
        if not INSTANCE_REGISTRY.exists():
            return {}
        try:
            registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            if attempt == REGISTRY_READ_ATTEMPTS - 1:
                msg = "instance registry remained unreadable; refusing to start a fuzz stack"
                raise RuntimeError(msg) from error
            time.sleep(REGISTRY_READ_RETRY_SECONDS)
            continue
        if not isinstance(registry, dict):
            msg = "instance registry must contain a JSON object"
            raise TypeError(msg)
        return registry
    msg = "unreachable registry retry state"
    raise AssertionError(msg)


def _active_fuzz_instances() -> tuple[str, ...]:
    """Probe the registry and return fuzz stacks whose real homeserver is alive."""
    registry = _read_instance_registry()
    instances = registry.get("instances", {})
    if not isinstance(instances, dict):
        return ()
    active: list[str] = []
    for name, value in instances.items():
        if not isinstance(name, str) or not name.startswith("fuzz") or not isinstance(value, dict):
            continue
        matrix_port = value.get("matrix_port")
        if not isinstance(matrix_port, int):
            continue
        try:
            response = httpx.get(
                f"http://127.0.0.1:{matrix_port}/_matrix/client/versions",
                timeout=0.5,
            )
        except httpx.HTTPError:
            continue
        if response.is_success:
            active.append(name)
    return tuple(sorted(active))


class ManagedTuwunelStack:
    """Disposable Tuwunel plus the current worktree's MindRoom runtime."""

    def __init__(
        self,
        *,
        room_count: int = 1,
        stream_segments: int = 4,
        stream_delay: float = 0.001,
    ) -> None:
        token = secrets.token_hex(4)
        self.instance_name = f"fuzz{token}"
        self.namespace = self.instance_name
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mindroom-live-matrix-fuzz-")
        self.root = Path(self.temp_dir.name)
        self.storage_path = self.root / "mindroom_data"
        self.config_path = self.root / "config.yaml"
        self.log_path = self.root / "mindroom.log"
        self.api_port = _available_port()
        self.homeserver = ""
        self.server_name = ""
        self.room_id = ""
        self.room_ids: tuple[str, ...] = ()
        self.room_keys = tuple(ROOM_KEY if room == 0 else f"fuzz_room_{room}" for room in range(room_count))
        self.agent_id = ""
        self.router_id = ""
        self.preexisting_fuzz_servers = 0
        self._created = False
        self._model_server: ThreadingHTTPServer | None = None
        self._model_thread: threading.Thread | None = None
        self._mindroom_process: subprocess.Popen[str] | None = None
        self._log_handle: TextIOWrapper | None = None
        self._env: dict[str, str] = {}
        self._stream_segments = stream_segments
        self._stream_delay = stream_delay

    def start(self) -> None:
        """Create every live dependency and wait for the managed room."""
        active_instances = _active_fuzz_instances()
        self.preexisting_fuzz_servers = len(active_instances)
        if active_instances:
            msg = f"live fuzz server already active: {', '.join(active_instances)}"
            raise RuntimeError(msg)
        _run_command("just", "local-instances-create", self.instance_name, "tuwunel")
        self._created = True
        registry = _read_instance_registry()
        instance = registry["instances"][self.instance_name]
        matrix_port = int(instance["matrix_port"])
        domain = str(instance["domain"])
        self.homeserver = f"http://127.0.0.1:{matrix_port}"
        self.server_name = f"m-{domain}"
        self.agent_id = f"@mindroom_{AGENT_NAME}_{self.namespace}:{self.server_name}"
        self.router_id = f"@mindroom_router_{self.namespace}:{self.server_name}"

        _run_command("just", "local-instances-start-matrix", self.instance_name)
        self._wait_for_url(f"{self.homeserver}/_matrix/client/versions", timeout=30)
        model_port = self._start_model_server()
        self._write_config(model_port)
        self._env = {
            **os.environ,
            "MATRIX_HOMESERVER": self.homeserver,
            "MATRIX_SERVER_NAME": self.server_name,
            "MATRIX_SSL_VERIFY": "false",
            "MINDROOM_CONFIG_PATH": str(self.config_path),
            "MINDROOM_NAMESPACE": self.namespace,
            "MINDROOM_STORAGE_PATH": str(self.storage_path),
            "MINDROOM_LOG_LEVEL": "INFO",
            "OPENAI_API_KEY": "sk-live-fuzz",
            "UV_PYTHON": "3.13",
        }
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._start_mindroom()

    def restart_mindroom(self) -> None:
        """Restart only MindRoom while preserving its cache and Matrix account."""
        self._stop_mindroom()
        self._start_mindroom()

    def stop_mindroom(self) -> None:
        """Stop MindRoom while leaving the isolated homeserver writable."""
        self._stop_mindroom()

    def resume_mindroom(self) -> None:
        """Resume MindRoom against its durable cache and saved sync token."""
        self._start_mindroom()

    def close(self) -> None:
        """Stop child processes and delete the exact disposable instance."""
        self._stop_mindroom()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        if self._model_server is not None:
            self._model_server.shutdown()
            self._model_server.server_close()
            self._model_server = None
        if self._model_thread is not None:
            self._model_thread.join(timeout=5)
            self._model_thread = None
        if self._created:
            _run_command("just", "local-instances-remove", self.instance_name)
            self._created = False
        self.temp_dir.cleanup()

    def log_tail(self, lines: int = 80) -> str:
        """Return recent MindRoom output when a live invariant fails."""
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def diagnostic_counts(self) -> dict[str, int]:
        """Count saturation signals in the complete runtime output."""
        if not self.log_path.exists():
            return {}
        log = self.log_path.read_text(encoding="utf-8", errors="replace")
        lines = log.splitlines()
        return {
            "cache_coordinator_timeouts": log.count("thread_read_error=cache_coordinator_timeout"),
            "degraded_thread_reads": log.count("matrix_cache_thread_read_degraded"),
            "dispatch_read_timeouts": log.count("thread_read_error=dispatch_read_timeout"),
            "event_loop_stalls": log.count("event_loop_stall_detected"),
            "limited_sync_backfill_warnings": log.count("limited-timeline backfill"),
            "limited_sync_certification_events": sum(
                "matrix_sync_certification_uncertain" in line and "limited_sync_timeline" in line for line in lines
            ),
        }

    def _start_model_server(self) -> int:
        _ModelHandler.stream_segments = self._stream_segments
        _ModelHandler.stream_delay = self._stream_delay
        self._model_server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        port = self._model_server.server_address[1]
        self._model_thread = threading.Thread(
            target=self._model_server.serve_forever,
            name="mindroom-live-fuzz-model",
            daemon=True,
        )
        self._model_thread.start()
        return port

    def _write_config(self, model_port: int) -> None:
        config = {
            "models": {
                "default": {
                    "provider": "openai",
                    "id": MODEL_ID,
                    "extra_kwargs": {"base_url": f"http://127.0.0.1:{model_port}/v1"},
                },
            },
            "agents": {
                AGENT_NAME: {
                    "display_name": "Live Fuzz Agent",
                    "role": "Return a deterministic acknowledgement.",
                    "model": "default",
                    "tools": [],
                    "rooms": list(self.room_keys),
                    "learning": False,
                },
            },
            "defaults": {"tools": [], "enable_streaming": True, "markdown": False},
            "memory": {"backend": "file"},
            "router": {"model": "default"},
            "mindroom_user": {"username": "livefuzzowner", "display_name": "Live Fuzz Owner"},
            "matrix_room_access": {
                "mode": "multi_user",
                "multi_user_join_rule": "public",
                "publish_to_room_directory": False,
                "invite_only_rooms": [],
                "reconcile_existing_rooms": False,
            },
            "authorization": {
                "default_room_access": True,
                "global_users": [],
                "agent_reply_permissions": {},
            },
        }
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def _start_mindroom(self) -> None:
        assert self._log_handle is not None
        self._mindroom_process = subprocess.Popen(
            [
                "uv",
                "run",
                "--no-sync",
                "mindroom",
                "run",
                "--api-port",
                str(self.api_port),
                "--log-level",
                "INFO",
            ],
            cwd=PROJECT_ROOT,
            env=self._env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_for_url(f"http://127.0.0.1:{self.api_port}/api/health", timeout=60)
        state_path = self.storage_path / "matrix_state.yaml"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._mindroom_process.poll() is not None:
                msg = f"MindRoom exited during startup:\n{self.log_tail()}"
                raise RuntimeError(msg)
            if state_path.exists():
                state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                state_rooms = state.get("rooms", {}) if isinstance(state, dict) else {}
                room_ids = [
                    room.get("room_id")
                    for room_key in self.room_keys
                    if isinstance(state_rooms, dict)
                    and isinstance((room := state_rooms.get(room_key)), dict)
                    and isinstance(room.get("room_id"), str)
                ]
                if len(room_ids) == len(self.room_keys):
                    self.room_ids = tuple(cast("list[str]", room_ids))
                    self.room_id = self.room_ids[0]
                    return
            time.sleep(0.2)
        msg = f"MindRoom did not create rooms {self.room_keys!r}:\n{self.log_tail()}"
        raise TimeoutError(msg)

    def _stop_mindroom(self) -> None:
        process = self._mindroom_process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._mindroom_process = None

    @staticmethod
    def _wait_for_url(url: str, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=1)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        msg = f"timed out waiting for {url}"
        raise TimeoutError(msg)


@dataclass(frozen=True, slots=True)
class _SentPayload:
    event_type: str
    txn_id: str
    content: dict[str, Any]


class LiveMatrixClient:
    """Minimal real Matrix client used by the live fuzzer."""

    def __init__(
        self,
        homeserver: str,
        room_id: str,
        *,
        room_slot: int = 0,
        client_slot: int = 0,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.room_id = room_id
        self.room_slot = room_slot
        self.client_slot = client_slot
        self.http = httpx.AsyncClient(timeout=30)
        self.access_token = ""
        self.next_batch: str | None = None
        self.seen_events: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self.http.aclose()

    async def register(self) -> str:
        """Register one disposable account without exposing its token."""
        username = f"livefuzz{secrets.token_hex(6)}"
        password = secrets.token_urlsafe(24)
        payload: dict[str, Any] = {
            "auth": {"type": "m.login.dummy"},
            "username": username,
            "password": password,
        }
        response = await self.http.post(f"{self.homeserver}/_matrix/client/v3/register", json=payload)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            session = response.json().get("session")
            if isinstance(session, str):
                payload["auth"]["session"] = session
                response = await self.http.post(
                    f"{self.homeserver}/_matrix/client/v3/register",
                    json=payload,
                )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        user_id = data.get("user_id")
        if not isinstance(token, str) or not isinstance(user_id, str):
            msg = "Matrix registration omitted access_token or user_id"
            raise TypeError(msg)
        self.access_token = token
        return user_id

    async def join_room(self) -> None:
        """Join the managed public room."""
        room_id = quote(self.room_id, safe="")
        await self._request("POST", f"/_matrix/client/v3/join/{room_id}", json_body={})

    async def send_event(
        self,
        event_type: str,
        txn_id: str,
        content: Mapping[str, Any],
    ) -> str:
        """Send one event with a caller-stable transaction ID."""
        room_id = quote(self.room_id, safe="")
        encoded_type = quote(event_type, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/send/{encoded_type}/{encoded_txn}",
            json_body=content,
        )
        event_id = data.get("event_id")
        if not isinstance(event_id, str):
            msg = f"Matrix send omitted event_id: {data}"
            raise TypeError(msg)
        return event_id

    async def redact(self, target_event_id: str, txn_id: str) -> str:
        """Redact one event authored by the disposable account."""
        room_id = quote(self.room_id, safe="")
        event_id = quote(target_event_id, safe="")
        encoded_txn = quote(txn_id, safe="")
        data = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{encoded_txn}",
            json_body={"reason": "live cache fuzz"},
        )
        redaction_id = data.get("event_id")
        if not isinstance(redaction_id, str):
            msg = f"Matrix redaction omitted event_id: {data}"
            raise TypeError(msg)
        return redaction_id

    async def sync(
        self,
        since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        """Read one incremental sync window from the real homeserver."""
        params: dict[str, str | int] = {
            "timeout": timeout_ms,
            "filter": json.dumps({"room": {"timeline": {"limit": timeline_limit}}}),
        }
        if since is not None:
            params["since"] = since
        return await self._request("GET", "/_matrix/client/v3/sync", params=params)

    async def sync_incremental(
        self,
        *,
        timeout_ms: int,
        allow_limited: bool = False,
    ) -> None:
        """Advance this client's private sync cursor and retain room events."""
        data = await self.sync(self.next_batch, timeout_ms=timeout_ms)
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        joined = data.get("rooms", {}).get("join", {})
        room = joined.get(self.room_id, {}) if isinstance(joined, dict) else {}
        timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
        if timeline.get("limited") is True and not allow_limited:
            msg = "incremental Matrix fuzz sync unexpectedly returned a limited timeline"
            raise AssertionError(msg)
        events = timeline.get("events", [])
        if not isinstance(events, list):
            msg = "Matrix sync room timeline events must be a list"
            raise TypeError(msg)
        for raw_event in events:
            if not isinstance(raw_event, dict):
                continue
            event = cast("dict[str, Any]", raw_event)
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                self.seen_events[event_id] = event
        self.next_batch = next_batch

    async def messages_before(
        self,
        from_token: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read one real backward pagination page for an independent oracle."""
        room_id = quote(self.room_id, safe="")
        params: dict[str, str | int] = {
            "dir": "b",
            "from": from_token,
            "limit": limit,
        }
        if to_token is not None:
            params["to"] = to_token
        data = await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages",
            params=params,
        )
        raw_chunk = data.get("chunk", [])
        if not isinstance(raw_chunk, list):
            msg = "Matrix room messages chunk must be a list"
            raise TypeError(msg)
        chunk = [cast("dict[str, Any]", event) for event in raw_chunk if isinstance(event, dict)]
        end = data.get("end")
        if end is not None and not isinstance(end, str):
            msg = "Matrix room messages end token must be a string or null"
            raise TypeError(msg)
        return chunk, end

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        response = await self.http.request(
            method,
            f"{self.homeserver}{path}",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json=json_body,
            params=params,
        )
        if response.is_error:
            msg = f"Matrix {method} {path} failed with HTTP {response.status_code}: {response.text}"
            raise RuntimeError(msg)
        data = response.json()
        if not isinstance(data, dict):
            msg = f"Matrix {method} {path} returned non-object JSON"
            raise TypeError(msg)
        return data


class ExactReplyOracle:
    """Track canonical agent replies from real incremental `/sync` responses."""

    def __init__(
        self,
        client: LiveMatrixClient,
        agent_id: str,
        *,
        internal_relay_senders: Collection[str] = (),
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.internal_relay_senders = frozenset(internal_relay_senders)
        self.internal_source_ids: set[str] = set()
        self.next_batch: str | None = None
        self.expected_sources: dict[str, str] = {}
        self.response_ids: dict[str, set[str]] = defaultdict(set)
        self.response_event_by_ref: dict[str, str] = {}
        self.latest_reply_bodies: dict[str, tuple[int, int, str]] = {}
        self.seen_event_ids: set[str] = set()
        self._ingest_ordinal = 0
        self.limited_timeline_count = 0
        self.pagination_page_count = 0
        self.gap_audit_page_count = 0
        self.gap_audit_missing_sources: set[str] = set()
        self._gap_audit_sources: set[str] | None = None
        self._gap_audit_completed = False
        self._last_response_at = time.monotonic()

    async def initialize(self) -> None:
        """Establish a sync token before the fuzz traffic starts."""
        await self._sync_once(timeout_ms=0, allow_limited=True)

    def expect(self, logical_ref: str, event_id: str) -> None:
        """Require exactly one canonical agent reply to a source event."""
        self.expected_sources[event_id] = logical_ref
        if self._gap_audit_sources is not None and not self._gap_audit_completed:
            self._gap_audit_sources.add(event_id)

    def arm_gap_audit(self) -> None:
        """Scope the next limited-gap audit to subsequently expected sources."""
        if self.next_batch is None:
            msg = "limited-gap audit requires an established sync token"
            raise RuntimeError(msg)
        self._gap_audit_sources = set()
        self.gap_audit_missing_sources.clear()
        self._gap_audit_completed = False

    async def audit_armed_limited_gap(self) -> None:
        """Sync the offline burst and require the armed limited-gap audit to run."""
        if self._gap_audit_sources is None:
            msg = "limited-gap audit is not armed"
            raise RuntimeError(msg)
        await self._sync_once(
            timeout_ms=0,
            allow_limited=True,
            timeline_limit=RECOVERY_TIMELINE_LIMIT,
        )
        if not self._gap_audit_completed:
            msg = "recovery source burst did not produce a limited observer timeline"
            raise AssertionError(msg)

    async def wait_until_exact(
        self,
        *,
        deadline_seconds: float,
        settle_seconds: float,
        allow_limited: bool = False,
    ) -> None:
        """Wait until all sources have one reply and the room stays quiet."""
        deadline = time.monotonic() + deadline_seconds
        settled_after = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            await self._sync_once(timeout_ms=250, allow_limited=allow_limited)
            self._assert_no_wrong_replies()
            incomplete_streams = self._incomplete_streaming_sources()
            if all(len(self.response_ids[source]) == 1 for source in self.expected_sources) and not incomplete_streams:
                settled_after = max(settled_after, self._last_response_at + settle_seconds)
                if time.monotonic() >= settled_after:
                    return
        missing = {
            logical_ref: {
                "event_id": event_id,
                "reply_count": len(self.response_ids[event_id]),
            }
            for event_id, logical_ref in self.expected_sources.items()
            if len(self.response_ids[event_id]) != 1 or event_id in incomplete_streams
        }
        missing_gap_refs = sorted(self.expected_sources[event_id] for event_id in self.gap_audit_missing_sources)
        msg = (
            f"timed out waiting for exact agent replies in room {self.client.room_slot}: "
            f"{missing}; bounded_gap_missing={missing_gap_refs}"
        )
        raise AssertionError(msg)

    def resolve_response_ref(self, response_ref: str) -> str:
        """Resolve a logical agent-response reference to its real event ID."""
        event_id = self.response_event_by_ref.get(response_ref)
        if event_id is None:
            msg = f"response event not observed for {response_ref!r}"
            raise KeyError(msg)
        return event_id

    async def _sync_once(
        self,
        *,
        timeout_ms: int,
        allow_limited: bool = False,
        timeline_limit: int = 2000,
    ) -> None:
        since = self.next_batch
        data = await self.client.sync(
            since,
            timeout_ms=timeout_ms,
            timeline_limit=timeline_limit,
        )
        next_batch = data.get("next_batch")
        if not isinstance(next_batch, str):
            msg = "Matrix sync omitted next_batch"
            raise TypeError(msg)
        self.next_batch = next_batch
        joined = data.get("rooms", {}).get("join", {})
        room = joined.get(self.client.room_id, {}) if isinstance(joined, dict) else {}
        timeline = room.get("timeline", {}) if isinstance(room, dict) else {}
        if timeline.get("limited") is True and not allow_limited:
            msg = "live fuzz oracle received a limited timeline; reduce batch size"
            raise AssertionError(msg)
        if timeline.get("limited") is True:
            self.limited_timeline_count += 1
            if since is not None:
                await self._ingest_limited_gap(timeline, since)
        events = timeline.get("events", [])
        if not isinstance(events, list):
            return
        for raw_event in events:
            if isinstance(raw_event, dict):
                self._ingest_event(raw_event)

    async def _ingest_limited_gap(
        self,
        timeline: Mapping[str, Any],
        since: str,
    ) -> None:
        """Ingest only the bounded incremental gap hidden by a limited sync."""
        prev_batch = timeline.get("prev_batch")
        if not isinstance(prev_batch, str):
            msg = "limited Matrix timeline omitted prev_batch"
            raise TypeError(msg)
        timeline_events = timeline.get("events", [])
        observed_ids = {
            event_id
            for event in timeline_events
            if isinstance(event, dict) and isinstance((event_id := event.get("event_id")), str)
        }
        token = prev_batch
        seen_tokens: set[str] = set()
        for _ in range(20):
            if token in seen_tokens:
                msg = f"bounded Matrix pagination repeated token {token!r}"
                raise AssertionError(msg)
            seen_tokens.add(token)
            events, next_token = await self.client.messages_before(
                token,
                to_token=since,
            )
            self.pagination_page_count += 1
            if self._gap_audit_sources is not None and not self._gap_audit_completed:
                self.gap_audit_page_count += 1
            observed_ids.update(event_id for event in events if isinstance((event_id := event.get("event_id")), str))
            for event in reversed(events):
                self._ingest_event(event)
            if next_token is None:
                break
            token = next_token
        else:
            msg = "bounded Matrix oracle pagination exceeded 20 pages"
            raise AssertionError(msg)
        if self._gap_audit_sources is not None and not self._gap_audit_completed:
            self.gap_audit_missing_sources.update(self._gap_audit_sources - observed_ids)
            self._gap_audit_completed = True

    def _ingest_event(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event_id)
        self._ingest_ordinal += 1
        if event.get("sender") in self.internal_relay_senders:
            self.internal_source_ids.add(event_id)
            return
        if event.get("sender") != self.agent_id or event.get("type") != "m.room.message":
            return
        content = event.get("content")
        if not isinstance(content, dict):
            return
        relation = content.get("m.relates_to")
        self._track_reply_body(event_id, event, content, relation)
        if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
            return
        reply = relation.get("m.in_reply_to")
        source_event_id = reply.get("event_id") if isinstance(reply, dict) else None
        if not isinstance(source_event_id, str):
            return
        self.response_ids[source_event_id].add(event_id)
        logical_ref = self.expected_sources.get(source_event_id)
        if logical_ref is not None:
            self.response_event_by_ref[f"response:{logical_ref}"] = event_id
        self._last_response_at = time.monotonic()

    def _track_reply_body(
        self,
        event_id: str,
        event: Mapping[str, Any],
        content: Mapping[str, Any],
        relation: object,
    ) -> None:
        """Fold originals and `m.replace` edits into one visible reply body."""
        is_edit = isinstance(relation, dict) and relation.get("rel_type") == "m.replace"
        response_event_id = relation.get("event_id") if is_edit else event_id
        if not isinstance(response_event_id, str):
            return
        new_content = content.get("m.new_content")
        body_source = new_content if isinstance(new_content, dict) else content
        body = body_source.get("body")
        if not isinstance(body, str):
            return
        raw_timestamp = event.get("origin_server_ts")
        timestamp = raw_timestamp if isinstance(raw_timestamp, int) else 0
        candidate = (timestamp, self._ingest_ordinal, body)
        current = self.latest_reply_bodies.get(response_event_id)
        if current is None or candidate[:2] >= current[:2]:
            self.latest_reply_bodies[response_event_id] = candidate
            self._last_response_at = time.monotonic()

    def _incomplete_streaming_sources(self) -> set[str]:
        """Return sources whose one reply is still a placeholder or partial edit."""
        incomplete: set[str] = set()
        for source_event_id in self.expected_sources:
            response_event_ids = self.response_ids[source_event_id]
            if len(response_event_ids) != 1:
                continue
            response_event_id = next(iter(response_event_ids))
            latest = self.latest_reply_bodies.get(response_event_id)
            if latest is None or not self._is_complete_model_body(latest[2]):
                incomplete.add(source_event_id)
        return incomplete

    @staticmethod
    def _is_complete_model_body(body: str) -> bool:
        match = re.fullmatch(r"LIVE-FUZZ call=(\d+) .* END call=(\d+)", body)
        if match is None or match.group(1) != match.group(2):
            return False
        return body == _ModelHandler.response_text_for(int(match.group(1)))

    def _assert_no_wrong_replies(self) -> None:
        duplicates = {
            self.expected_sources.get(source, source): sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if len(event_ids) > 1
        }
        unexpected = {
            source: sorted(event_ids)
            for source, event_ids in self.response_ids.items()
            if source not in self.expected_sources and source not in self.internal_source_ids
        }
        if duplicates or unexpected:
            msg = f"agent reply invariant failed: duplicates={duplicates}, unexpected={unexpected}"
            raise AssertionError(msg)


class LiveFuzzRunner:
    """Translate logical operations into concurrent real Matrix writes."""

    def __init__(
        self,
        stack: ManagedTuwunelStack,
        clients: tuple[LiveMatrixClient, ...],
        scenario: LiveFuzzScenario,
        *,
        reply_timeout: float,
        settle_seconds: float,
    ) -> None:
        self.stack = stack
        self.clients = clients
        self.client = clients[0]
        self.scenario = scenario
        self.reply_timeout = reply_timeout
        self.settle_seconds = settle_seconds
        self.oracle = ExactReplyOracle(
            self.client,
            stack.agent_id,
            internal_relay_senders=(stack.router_id,),
        )
        self.event_ids: dict[str, str] = {}
        self.response_event_ids: dict[str, str] = {}
        self.sent_payloads: dict[str, _SentPayload] = {}
        self.operation_count = 0
        self.restart_count = 0
        self.executed_batches = 0

    async def run(self) -> dict[str, int | str]:
        """Execute every batch and enforce the reply invariant after each."""
        await asyncio.gather(*(client.register() for client in self.clients))
        await asyncio.gather(*(client.join_room() for client in self.clients))
        if self.scenario.profile == "recovery":
            return await self._run_recovery()
        if self.scenario.profile == "saturation":
            await asyncio.gather(
                *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
            )
            return await self._run_saturation()

        await self.oracle.initialize()
        await self._send_roots(range(self.scenario.thread_count))
        return await self._run_batches(
            self.scenario.batches,
        )

    async def _run_recovery(self) -> dict[str, int | str]:
        """Replay a bounded outage, limited sync, reconnect, and duplicate audit."""
        observers = tuple(self._recovery_client(room, 0) for room in range(self.scenario.room_count))
        oracles = tuple(
            ExactReplyOracle(
                client,
                self.stack.agent_id,
                internal_relay_senders=(self.stack.router_id,),
            )
            for client in observers
        )
        await asyncio.gather(*(oracle.initialize() for oracle in oracles))
        await self._send_recovery_roots(oracles)

        for oracle in oracles:
            oracle.arm_gap_audit()
        self.stack.stop_mindroom()
        offline_started = time.monotonic()
        message_count = 0
        retry_count = 0
        for batch in self.scenario.batches:
            results = await asyncio.gather(*(self._apply_recovery_operation(operation) for operation in batch))
            for operation, event_id, payload in results:
                self.operation_count += 1
                if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY:
                    retry_count += 1
                    continue
                message_count += 1
                self.event_ids[operation.event_ref] = event_id
                self.sent_payloads[operation.event_ref] = payload
                oracles[operation.room].expect(operation.event_ref, event_id)
            self.executed_batches += 1

        await asyncio.gather(*(oracle.audit_armed_limited_gap() for oracle in oracles))
        self.stack.resume_mindroom()
        self.restart_count += 1
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                    allow_limited=True,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)
        recovery_seconds = time.monotonic() - offline_started

        self.stack.restart_mindroom()
        self.restart_count += 1
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=max(self.settle_seconds, 1),
                    allow_limited=True,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            oracle._assert_no_wrong_replies()

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": sum(len(oracle.expected_sources) for oracle in oracles),
            "clients": self.scenario.client_count * self.scenario.room_count,
            "duplicates": 0,
            "messages": message_count,
            "missing": 0,
            "operations": self.operation_count,
            "oracle_limited_timelines": sum(oracle.limited_timeline_count for oracle in oracles),
            "oracle_gap_audit_pages": sum(oracle.gap_audit_page_count for oracle in oracles),
            "oracle_pagination_pages": sum(oracle.pagination_page_count for oracle in oracles),
            "recovery_runtime_ms": round(recovery_seconds * 1000),
            "restarts": self.restart_count,
            "rooms": self.scenario.room_count,
            "roots": self.scenario.thread_count * self.scenario.room_count,
            "status": "PASS",
            "threads": self.scenario.thread_count * self.scenario.room_count,
            "transaction_retries": retry_count,
        }

    async def _send_recovery_roots(self, oracles: tuple[ExactReplyOracle, ...]) -> None:
        """Create hot and cold thread roots in every room before the outage."""

        async def send_root(room: int, thread: int) -> tuple[int, str, str, _SentPayload]:
            logical_ref = f"root:{room}:{thread}"
            content = self._message_content(f"Live recovery root {room}:{thread}")
            payload = _SentPayload("m.room.message", f"live-recovery-{logical_ref}", content)
            event_id = await self._recovery_client(room, 0).send_event(
                payload.event_type,
                payload.txn_id,
                payload.content,
            )
            return room, logical_ref, event_id, payload

        roots = await asyncio.gather(
            *(
                send_root(room, thread)
                for room in range(self.scenario.room_count)
                for thread in range(self.scenario.thread_count)
            ),
        )
        for room, logical_ref, event_id, payload in roots:
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            oracles[room].expect(logical_ref, event_id)
        await asyncio.gather(
            *(
                oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                )
                for oracle in oracles
            ),
        )
        for oracle in oracles:
            self.response_event_ids.update(oracle.response_event_by_ref)

    async def _apply_recovery_operation(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str, _SentPayload]:
        """Send one scheduled offline mutation or exact transaction retry."""
        assert operation.target is not None
        client = self._recovery_client(operation.room, operation.client)
        if operation.kind is LiveOperationKind.IDEMPOTENT_RETRY:
            payload = self.sent_payloads[operation.target]
            event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
            expected_event_id = self.event_ids[operation.target]
            if event_id != expected_event_id:
                msg = f"recovery retry changed event ID for {operation.target}: {expected_event_id} -> {event_id}"
                raise AssertionError(msg)
            return operation, event_id, payload

        target_event_id = self._resolve_event_ref(operation.target)
        if operation.kind is LiveOperationKind.THREAD_MESSAGE:
            root_event_id = self.event_ids[f"root:{operation.room}:{operation.thread}"]
            relation = {
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": target_event_id},
            }
        elif operation.kind is LiveOperationKind.PLAIN_REPLY:
            relation = {"m.in_reply_to": {"event_id": target_event_id}}
        else:
            msg = f"unsupported recovery operation: {operation.kind}"
            raise AssertionError(msg)
        content = self._message_content(
            f"Live recovery message {operation.operation_id}",
            relation=relation,
        )
        payload = _SentPayload(
            "m.room.message",
            f"live-recovery-op-{operation.operation_id}",
            content,
        )
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
        return operation, event_id, payload

    def _recovery_client(self, room: int, client: int) -> LiveMatrixClient:
        """Return the uniquely scheduled sender in one recovery room."""
        return next(item for item in self.clients if item.room_slot == room and item.client_slot == client)

    async def _run_saturation(self) -> dict[str, int | str]:
        """Run hot and parallel turns without cross-thread barriers."""
        parallel_start = self._saturation_parallel_start()
        expected_sources: set[str] = set()

        hot_root, hot_response = await self._saturation_turn(
            self.clients[0],
            label="hot-root",
            thread_root=None,
            reply_to=None,
            expected_sources=expected_sources,
        )
        for batch in self.scenario.batches[:parallel_start]:
            operation = batch[0]
            _, hot_response = await self._saturation_turn(
                self.clients[0],
                label=operation.event_ref,
                thread_root=hot_root,
                reply_to=hot_response,
                expected_sources=expected_sources,
            )
            self.operation_count += 1
            self.executed_batches += 1

        parallel_batches = self.scenario.batches[parallel_start:]

        async def run_parallel_thread(thread: int) -> None:
            client = self._client_for_thread(thread)
            root, response = await self._saturation_turn(
                client,
                label=f"root:{thread}",
                thread_root=None,
                reply_to=None,
                expected_sources=expected_sources,
            )
            for batch in parallel_batches:
                operation = next(item for item in batch if item.thread == thread)
                _, response = await self._saturation_turn(
                    client,
                    label=operation.event_ref,
                    thread_root=root,
                    reply_to=response,
                    expected_sources=expected_sources,
                )
                self.operation_count += 1

        await asyncio.gather(
            *(run_parallel_thread(thread) for thread in range(1, self.scenario.thread_count)),
        )
        self.executed_batches += len(parallel_batches)

        # A duplicate response may finish just after its twin. Let all model
        # streams settle, then audit the union of every sender's sync history.
        await asyncio.sleep(max(self.settle_seconds, 1.0))
        await asyncio.gather(
            *(client.sync_incremental(timeout_ms=0, allow_limited=True) for client in self.clients),
        )
        all_events = {event_id: event for client in self.clients for event_id, event in client.seen_events.items()}
        response_ids = self._canonical_response_ids(all_events.values())
        duplicates = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id in expected_sources and len(event_ids) != 1
        }
        missing = sorted(expected_sources - response_ids.keys())
        unexpected = {
            source_event_id: sorted(event_ids)
            for source_event_id, event_ids in response_ids.items()
            if source_event_id not in expected_sources
        }
        if duplicates or missing or unexpected:
            msg = (
                "saturation reply invariant failed: "
                f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}"
            )
            raise AssertionError(msg)

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(expected_sources),
            "operations": self.operation_count,
            "restarts": 0,
            "roots": self.scenario.thread_count,
            "status": "PASS",
        }

    async def _saturation_turn(
        self,
        client: LiveMatrixClient,
        *,
        label: str,
        thread_root: str | None,
        reply_to: str | None,
        expected_sources: set[str],
    ) -> tuple[str, str]:
        """Send one old-harness turn and wait for its completed stream."""
        content = self._message_content(
            f"Live saturation {label}",
            relation=(
                {
                    "rel_type": "m.thread",
                    "event_id": thread_root,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": reply_to},
                }
                if thread_root is not None and reply_to is not None
                else None
            ),
        )
        txn_id = f"live-saturation-{label}-{secrets.token_hex(4)}"
        source_event_id = await client.send_event("m.room.message", txn_id, content)
        expected_sources.add(source_event_id)
        root_event_id = thread_root or source_event_id
        response_event_id = await self._wait_for_completed_response(
            client,
            root_event_id=root_event_id,
            source_event_id=source_event_id,
        )
        return root_event_id, response_event_id

    async def _wait_for_completed_response(
        self,
        client: LiveMatrixClient,
        *,
        root_event_id: str,
        source_event_id: str,
    ) -> str:
        """Wait until one source has exactly one fully streamed response."""
        deadline = time.monotonic() + self.reply_timeout
        while time.monotonic() < deadline:
            response_ids = self._canonical_response_ids(
                client.seen_events.values(),
                root_event_id=root_event_id,
            ).get(source_event_id, set())
            if len(response_ids) > 1:
                msg = f"duplicate agent replies for {source_event_id}: {sorted(response_ids)}"
                raise AssertionError(msg)
            if len(response_ids) == 1:
                response_event_id = next(iter(response_ids))
                if "END call=" in self._latest_event_body(client.seen_events.values(), response_event_id):
                    return response_event_id
            await client.sync_incremental(timeout_ms=1000, allow_limited=True)
        msg = f"agent response timeout for {source_event_id}"
        raise TimeoutError(msg)

    def _canonical_response_ids(
        self,
        events: Collection[Mapping[str, Any]],
        *,
        root_event_id: str | None = None,
    ) -> dict[str, set[str]]:
        """Index canonical agent originals by their direct source event."""
        response_ids: dict[str, set[str]] = defaultdict(set)
        for event in events:
            if event.get("type") != "m.room.message" or event.get("sender") != self.stack.agent_id:
                continue
            event_id = event.get("event_id")
            content = event.get("content")
            if not isinstance(event_id, str) or not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
                continue
            if root_event_id is not None and relation.get("event_id") != root_event_id:
                continue
            in_reply_to = relation.get("m.in_reply_to")
            source_event_id = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
            if isinstance(source_event_id, str):
                response_ids[source_event_id].add(event_id)
        return response_ids

    @staticmethod
    def _latest_event_body(
        events: Collection[Mapping[str, Any]],
        response_event_id: str,
    ) -> str:
        """Return the newest original or edit body for one response."""
        candidates: list[tuple[int, str]] = []
        for event in events:
            event_id = event.get("event_id")
            content = event.get("content")
            if not isinstance(event_id, str) or not isinstance(content, dict):
                continue
            relation = content.get("m.relates_to")
            is_original = event_id == response_event_id
            is_edit = (
                isinstance(relation, dict)
                and relation.get("rel_type") == "m.replace"
                and relation.get("event_id") == response_event_id
            )
            if not is_original and not is_edit:
                continue
            new_content = content.get("m.new_content")
            body_source = new_content if isinstance(new_content, dict) else content
            body = body_source.get("body")
            if isinstance(body, str):
                timestamp = event.get("origin_server_ts")
                candidates.append((timestamp if isinstance(timestamp, int) else 0, body))
        return max(candidates, default=(0, ""))[1]

    async def _run_batches(
        self,
        batches: tuple[tuple[LiveOperation, ...], ...],
        *,
        batch_index_offset: int = 0,
    ) -> dict[str, int | str]:
        """Run one contiguous scenario segment against already-created roots."""
        for relative_batch_index, batch in enumerate(batches):
            batch_index = batch_index_offset + relative_batch_index
            if batch[0].kind is LiveOperationKind.RESTART_MINDROOM:
                self.stack.restart_mindroom()
                self.restart_count += 1
            else:
                results = await asyncio.gather(*(self._apply(operation) for operation in batch))
                for operation, event_id, payload in results:
                    self.operation_count += 1
                    if event_id is not None and operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY:
                        self.event_ids[operation.event_ref] = event_id
                    if payload is not None:
                        self.sent_payloads[operation.event_ref] = payload
                    if operation.kind in {
                        LiveOperationKind.THREAD_MESSAGE,
                        LiveOperationKind.PLAIN_REPLY,
                    }:
                        assert event_id is not None
                        self.oracle.expect(operation.event_ref, event_id)
            try:
                await self.oracle.wait_until_exact(
                    deadline_seconds=self.reply_timeout,
                    settle_seconds=self.settle_seconds,
                )
            except AssertionError as exc:
                msg = f"{exc} after live batch {batch_index}"
                raise AssertionError(msg) from exc
            self.executed_batches += 1

        return {
            "batches": self.executed_batches,
            "canonical_agent_replies": len(self.oracle.expected_sources),
            "operations": self.operation_count,
            "restarts": self.restart_count,
            "roots": self.scenario.thread_count,
            "status": "PASS",
        }

    def _saturation_parallel_start(self) -> int:
        """Return the first batch belonging to the parallel saturation phase."""
        return next(
            (
                index
                for index, batch in enumerate(self.scenario.batches)
                if any(operation.thread != 0 for operation in batch)
            ),
            len(self.scenario.batches),
        )

    def _client_for_thread(self, thread: int) -> LiveMatrixClient:
        """Use the original multi-sender mapping for saturation traces."""
        if self.scenario.profile != "saturation":
            return self.client
        client_index = max(thread - 1, 0)
        return self.clients[client_index]

    async def _send_roots(self, threads: Collection[int]) -> None:
        async def send_root(thread: int) -> tuple[int, str, _SentPayload]:
            logical_ref = f"root:{thread}"
            content = self._message_content(f"Live fuzz root {thread}")
            payload = _SentPayload("m.room.message", f"live-fuzz-{logical_ref}", content)
            event_id = await self._client_for_thread(thread).send_event(
                payload.event_type,
                payload.txn_id,
                payload.content,
            )
            return thread, event_id, payload

        roots = await asyncio.gather(*(send_root(thread) for thread in threads))
        for thread, event_id, payload in roots:
            logical_ref = f"root:{thread}"
            self.event_ids[logical_ref] = event_id
            self.sent_payloads[logical_ref] = payload
            self.oracle.expect(logical_ref, event_id)
        await self.oracle.wait_until_exact(
            deadline_seconds=self.reply_timeout,
            settle_seconds=self.settle_seconds,
        )

    async def _apply(
        self,
        operation: LiveOperation,
    ) -> tuple[LiveOperation, str | None, _SentPayload | None]:
        assert operation.target is not None
        target_event_id = self._resolve_event_ref(operation.target)
        txn_id = f"live-fuzz-op-{operation.operation_id}"
        client = self._client_for_thread(operation.thread)

        if operation.kind is LiveOperationKind.THREAD_MESSAGE:
            root_event_id = self.event_ids[f"root:{operation.thread}"]
            content = self._message_content(
                f"Live fuzz thread message {operation.operation_id}",
                relation={
                    "rel_type": "m.thread",
                    "event_id": root_event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": target_event_id},
                },
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.PLAIN_REPLY:
            content = self._message_content(
                f"Live fuzz plain reply {operation.operation_id}",
                relation={"m.in_reply_to": {"event_id": target_event_id}},
            )
            payload = _SentPayload("m.room.message", txn_id, content)
            event_id = await client.send_event(payload.event_type, txn_id, content)
            return operation, event_id, payload

        if operation.kind is LiveOperationKind.EDIT:
            new_content = self._message_content(f"Live fuzz edited message {operation.operation_id}")
            content = {
                **new_content,
                "m.new_content": new_content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
            }
            event_id = await client.send_event("m.room.message", txn_id, content)
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REACTION:
            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target_event_id,
                    "key": f"fuzz-{operation.operation_id}",
                },
            }
            event_id = await client.send_event("m.reaction", txn_id, content)
            return operation, event_id, None

        if operation.kind is LiveOperationKind.REDACTION:
            event_id = await client.redact(target_event_id, txn_id)
            return operation, event_id, None

        payload = self.sent_payloads[operation.target]
        event_id = await client.send_event(payload.event_type, payload.txn_id, payload.content)
        if event_id != target_event_id:
            msg = f"idempotent retry changed event ID for {operation.target}: {target_event_id} -> {event_id}"
            raise AssertionError(msg)
        return operation, event_id, None

    def _resolve_event_ref(self, logical_ref: str) -> str:
        if logical_ref.startswith("response:"):
            recovery_event_id = self.response_event_ids.get(logical_ref)
            if recovery_event_id is not None:
                return recovery_event_id
            return self.oracle.resolve_response_ref(logical_ref)
        event_id = self.event_ids.get(logical_ref)
        if event_id is None:
            msg = f"event not observed for {logical_ref!r}"
            raise KeyError(msg)
        return event_id

    def _message_content(
        self,
        body: str,
        *,
        relation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"{body} {self.stack.agent_id}",
            "m.mentions": {"user_ids": [self.stack.agent_id]},
        }
        if relation is not None:
            content["m.relates_to"] = dict(relation)
        return content


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        msg = "must be non-negative"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("fuzz", "recovery", "saturation"), default="fuzz")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--steps", type=_positive_int, default=200)
    parser.add_argument("--rooms", type=_positive_int, default=3)
    parser.add_argument("--clients", type=_positive_int, default=4)
    parser.add_argument("--messages-per-room", type=_positive_int, default=64)
    parser.add_argument("--threads", type=_positive_int, default=45)
    parser.add_argument("--max-batch-size", type=_positive_int, default=16)
    parser.add_argument("--restart-interval", type=_non_negative_int, default=100)
    parser.add_argument(
        "--reply-timeout",
        type=float,
        help="per-reply deadline (default: 60s fuzz, 180s saturation)",
    )
    parser.add_argument("--settle-seconds", type=float, default=0.75)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--save-trace", type=Path)
    parser.add_argument("--failure-log", type=Path)
    return parser.parse_args()


async def _run_live(
    stack: ManagedTuwunelStack,
    scenario: LiveFuzzScenario,
    *,
    reply_timeout: float,
    settle_seconds: float,
) -> dict[str, int | str]:
    if scenario.profile == "recovery":
        clients = tuple(
            LiveMatrixClient(
                stack.homeserver,
                stack.room_ids[room],
                room_slot=room,
                client_slot=client,
            )
            for room in range(scenario.room_count)
            for client in range(scenario.client_count)
        )
    else:
        client_count = scenario.thread_count - 1 if scenario.profile == "saturation" else 1
        clients = tuple(LiveMatrixClient(stack.homeserver, stack.room_id) for _ in range(client_count))
    try:
        return await LiveFuzzRunner(
            stack,
            clients,
            scenario,
            reply_timeout=reply_timeout,
            settle_seconds=settle_seconds,
        ).run()
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def main() -> None:
    """Run one trace against a fresh disposable real-server stack."""
    args = _parse_args()
    scenario = (
        LiveFuzzScenario.from_json(args.trace.read_text(encoding="utf-8"))
        if args.trace is not None
        else (
            saturation_scenario()
            if args.profile == "saturation"
            else (
                recovery_scenario_from_seed(
                    args.seed,
                    messages_per_room=args.messages_per_room,
                    room_count=args.rooms,
                    thread_count=args.threads,
                    client_count=args.clients,
                    max_batch_size=args.max_batch_size,
                )
                if args.profile == "recovery"
                else live_scenario_from_seed(
                    args.seed,
                    steps=args.steps,
                    thread_count=args.threads,
                    max_batch_size=args.max_batch_size,
                    restart_interval=args.restart_interval,
                )
            )
        )
    )
    if args.save_trace is not None:
        args.save_trace.write_text(scenario.to_json() + "\n", encoding="utf-8")
    reply_timeout = args.reply_timeout
    if reply_timeout is None:
        reply_timeout = 180 if scenario.profile == "saturation" else 60

    stack = ManagedTuwunelStack(
        room_count=scenario.room_count,
        stream_segments=96 if scenario.profile == "saturation" else 4,
        stream_delay=0.012 if scenario.profile == "saturation" else 0.001,
    )
    started = time.monotonic()
    try:
        stack.start()
        result = asyncio.run(
            _run_live(
                stack,
                scenario,
                reply_timeout=reply_timeout,
                settle_seconds=args.settle_seconds,
            ),
        )
        result["profile"] = scenario.profile
        result["seed"] = args.seed if args.trace is None else "trace"
        result["nio_revision"] = os.getenv("MINDROOM_NIO_FUZZ_COMMIT", "installed")
        result["preexisting_fuzz_servers"] = stack.preexisting_fuzz_servers
        result["runtime_ms"] = round((time.monotonic() - started) * 1000)
        result.update(stack.diagnostic_counts())
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("Live Matrix fuzz trace:", file=sys.stderr)
        print(args.trace or scenario.to_json(), file=sys.stderr)
        print(json.dumps(stack.diagnostic_counts(), sort_keys=True), file=sys.stderr)
        if args.failure_log is not None and stack.log_path.exists():
            args.failure_log.write_text(
                stack.log_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        log_tail = stack.log_tail()
        if log_tail:
            print("MindRoom log tail:", file=sys.stderr)
            print(log_tail, file=sys.stderr)
        raise
    finally:
        stack.close()


if __name__ == "__main__":
    main()
