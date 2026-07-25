"""Deterministic built-in model for realistic local load and tool-call testing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.models.base import Model
from agno.models.response import ModelResponse

if TYPE_CHECKING:
    from agno.models.message import Message

_LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex "
    "ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat "
    "nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim "
    "id est laborum. "
)
_MINIMUM_RESPONSE_CHARS = 64


@dataclass
class _AsyncCoordinationState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    async_barrier_members: dict[str, set[str]] = field(default_factory=dict)
    async_released_groups: set[str] = field(default_factory=set)
    stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _CoordinationState:
    sync_condition: threading.Condition = field(default_factory=threading.Condition)
    sync_barrier_members: dict[str, set[str]] = field(default_factory=dict)
    sync_released_groups: set[str] = field(default_factory=set)
    sync_stream_lock: threading.Lock = field(default_factory=threading.Lock)
    async_states: dict[asyncio.AbstractEventLoop, _AsyncCoordinationState] = field(default_factory=dict)
    async_states_lock: threading.Lock = field(default_factory=threading.Lock)
    telemetry_lock: threading.Lock = field(default_factory=threading.Lock)


_COORDINATION_STATES: dict[str, _CoordinationState] = {}
_COORDINATION_STATES_LOCK = threading.Lock()


def _coordination_state(key: str | None) -> _CoordinationState:
    if key is None:
        return _CoordinationState()
    with _COORDINATION_STATES_LOCK:
        return _COORDINATION_STATES.setdefault(key, _CoordinationState())


@dataclass(frozen=True, slots=True)
class SyntheticPlan:
    """One replayable response and optional tool continuation."""

    request_id: str
    body: str
    split_at: int | None
    tool_call_id: str | None
    sleep_seconds: int | None

    @property
    def prefix(self) -> str:
        """Return content emitted before the tool call."""
        return self.body if self.split_at is None else self.body[: self.split_at]

    @property
    def suffix(self) -> str:
        """Return content emitted after the tool result."""
        return "" if self.split_at is None else self.body[self.split_at :]


def _request_digest(seed: int, identity: str) -> bytes:
    return hashlib.sha256(f"{seed}\0{identity}".encode()).digest()


def synthetic_request_id(seed: int, identity: str) -> str:
    """Return a stable opaque identifier for one synthetic request."""
    return _request_digest(seed, identity).hex()[:16]


def _fixed_length_body(request_id: str, length: int) -> str:
    prefix = f"SYNTHETIC[{request_id}] "
    suffix = f" COMPLETE[{request_id}]"
    filler_length = length - len(prefix) - len(suffix)
    filler = (_LOREM * ((filler_length // len(_LOREM)) + 1))[:filler_length]
    return prefix + filler + suffix


def synthetic_plan(
    identity: str,
    *,
    seed: int,
    min_response_chars: int,
    max_response_chars: int,
    tool_call_probability: float,
    min_sleep_seconds: int,
    max_sleep_seconds: int,
    tool_available: bool,
) -> SyntheticPlan:
    """Build one deterministic response plan without retaining mutable request state."""
    digest = _request_digest(seed, identity)
    randomizer = random.Random(int.from_bytes(digest))  # noqa: S311 - replayable test behavior
    request_id = digest.hex()[:16]
    response_chars = randomizer.randint(min_response_chars, max_response_chars)
    body = _fixed_length_body(request_id, response_chars)
    use_tool = tool_available and randomizer.random() < tool_call_probability
    if not use_tool:
        return SyntheticPlan(
            request_id=request_id,
            body=body,
            split_at=None,
            tool_call_id=None,
            sleep_seconds=None,
        )
    lower_split = max(len(f"SYNTHETIC[{request_id}] "), response_chars // 4)
    upper_split = min(response_chars - len(f" COMPLETE[{request_id}]"), response_chars * 3 // 4)
    split_at = randomizer.randint(lower_split, upper_split)
    return SyntheticPlan(
        request_id=request_id,
        body=body,
        split_at=split_at,
        tool_call_id=f"synthetic-{request_id}",
        sleep_seconds=randomizer.randint(min_sleep_seconds, max_sleep_seconds),
    )


@dataclass
class SyntheticModel(Model):
    """Generate deterministic streamed text and optional real tool-call continuations."""

    name: str | None = "Synthetic"
    provider: str | None = "Synthetic"
    seed: int = 1
    min_response_chars: int = 320
    max_response_chars: int = 960
    chunk_chars: int = 40
    chars_per_second: float = 80.0
    tool_call_probability: float = 0.0
    min_sleep_seconds: int = 1
    max_sleep_seconds: int = 3
    identity_pattern: str | None = None
    activation_pattern: str | None = None
    inactive_response: str = "SYNTHETIC READY"
    barrier_size: int = 0
    barrier_group_pattern: str | None = None
    barrier_timeout_seconds: float = 90.0
    serialize_streams: bool = False
    telemetry_path: str | None = None
    coordination_key: str | None = None
    _identity_regex: re.Pattern[str] | None = field(init=False, default=None, repr=False)
    _activation_regex: re.Pattern[str] | None = field(init=False, default=None, repr=False)
    _barrier_group_regex: re.Pattern[str] | None = field(init=False, default=None, repr=False)
    _state: _CoordinationState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate deterministic generation and optional concurrency controls."""
        super().__post_init__()
        self._validate_generation_settings()
        self._validate_barrier_settings()
        self._identity_regex = re.compile(self.identity_pattern) if self.identity_pattern else None
        self._activation_regex = re.compile(self.activation_pattern) if self.activation_pattern else None
        self._barrier_group_regex = re.compile(self.barrier_group_pattern) if self.barrier_group_pattern else None
        self._state = _coordination_state(self.coordination_key)

    def _validate_generation_settings(self) -> None:
        if not self.inactive_response:
            msg = "inactive_response must not be empty"
            raise ValueError(msg)
        if self.min_response_chars < _MINIMUM_RESPONSE_CHARS:
            msg = f"min_response_chars must be at least {_MINIMUM_RESPONSE_CHARS}"
            raise ValueError(msg)
        if self.max_response_chars < self.min_response_chars:
            msg = "max_response_chars must be greater than or equal to min_response_chars"
            raise ValueError(msg)
        if self.chunk_chars < 1:
            msg = "chunk_chars must be positive"
            raise ValueError(msg)
        if self.chars_per_second < 0:
            msg = "chars_per_second must be non-negative"
            raise ValueError(msg)
        if not 0 <= self.tool_call_probability <= 1:
            msg = "tool_call_probability must be between 0 and 1"
            raise ValueError(msg)
        if self.min_sleep_seconds < 0:
            msg = "min_sleep_seconds must be non-negative"
            raise ValueError(msg)
        if self.max_sleep_seconds < self.min_sleep_seconds:
            msg = "max_sleep_seconds must be greater than or equal to min_sleep_seconds"
            raise ValueError(msg)

    def _validate_barrier_settings(self) -> None:
        if self.barrier_size < 0:
            msg = "barrier_size must be non-negative"
            raise ValueError(msg)
        if self.barrier_size and self.barrier_group_pattern is None:
            msg = "barrier_group_pattern is required when barrier_size is enabled"
            raise ValueError(msg)
        if self.barrier_timeout_seconds <= 0:
            msg = "barrier_timeout_seconds must be positive"
            raise ValueError(msg)

    def plan_for_prompt(self, prompt: str, *, tool_available: bool = True) -> SyntheticPlan:
        """Return the exact plan used for one prompt."""
        identity = self._identity(prompt)
        if not self._is_active(prompt):
            return SyntheticPlan(
                request_id=synthetic_request_id(self.seed, identity),
                body=self.inactive_response,
                split_at=None,
                tool_call_id=None,
                sleep_seconds=None,
            )
        return synthetic_plan(
            identity,
            seed=self.seed,
            min_response_chars=self.min_response_chars,
            max_response_chars=self.max_response_chars,
            tool_call_probability=self.tool_call_probability,
            min_sleep_seconds=self.min_sleep_seconds,
            max_sleep_seconds=self.max_sleep_seconds,
            tool_available=tool_available,
        )

    def invoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> ModelResponse:
        """Return one deterministic non-streaming model response."""
        plan, phase, group = self._execution(messages, tools, tool_choice)
        self._reach_sync_barrier(plan, phase, group)
        lock = self._state.sync_stream_lock if self.serialize_streams else _NullLock()
        with lock:
            self._record("request_started", plan, phase, group)
            try:
                content = self._content_for_phase(plan, phase)
                self._sleep_sync(len(content))
                return ModelResponse(
                    content=content,
                    tool_calls=self._tool_calls_for_phase(plan, phase),
                )
            finally:
                self._record("request_finished", plan, phase, group)

    async def ainvoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> ModelResponse:
        """Return one deterministic asynchronous non-streaming model response."""
        plan, phase, group = self._execution(messages, tools, tool_choice)
        await self._reach_async_barrier(plan, phase, group)
        lock = self._get_async_stream_lock() if self.serialize_streams else _AsyncNullLock()
        async with lock:
            self._record("request_started", plan, phase, group)
            try:
                content = self._content_for_phase(plan, phase)
                await self._sleep_async(len(content))
                return ModelResponse(
                    content=content,
                    tool_calls=self._tool_calls_for_phase(plan, phase),
                )
            finally:
                self._record("request_finished", plan, phase, group)

    def invoke_stream(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> Iterator[ModelResponse]:
        """Stream deterministic fixed-rate chunks and an optional tool call."""
        plan, phase, group = self._execution(messages, tools, tool_choice)
        self._reach_sync_barrier(plan, phase, group)
        lock = self._state.sync_stream_lock if self.serialize_streams else _NullLock()
        with lock:
            self._record("request_started", plan, phase, group)
            try:
                for chunk in self._chunks(self._content_for_phase(plan, phase)):
                    self._sleep_sync(len(chunk))
                    yield ModelResponse(content=chunk)
                tool_calls = self._tool_calls_for_phase(plan, phase)
                if tool_calls:
                    self._record("tool_call_emitted", plan, phase, group)
                    yield ModelResponse(tool_calls=tool_calls)
            finally:
                self._record("request_finished", plan, phase, group)

    async def ainvoke_stream(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        """Asynchronously stream deterministic chunks and an optional tool call."""
        plan, phase, group = self._execution(messages, tools, tool_choice)
        await self._reach_async_barrier(plan, phase, group)
        lock = self._get_async_stream_lock() if self.serialize_streams else _AsyncNullLock()
        async with lock:
            self._record("request_started", plan, phase, group)
            try:
                for chunk in self._chunks(self._content_for_phase(plan, phase)):
                    await self._sleep_async(len(chunk))
                    yield ModelResponse(content=chunk)
                tool_calls = self._tool_calls_for_phase(plan, phase)
                if tool_calls:
                    self._record("tool_call_emitted", plan, phase, group)
                    yield ModelResponse(tool_calls=tool_calls)
            finally:
                self._record("request_finished", plan, phase, group)

    def _parse_provider_response(self, response: object, **_kwargs: object) -> ModelResponse:
        if not isinstance(response, ModelResponse):
            msg = "SyntheticModel only parses ModelResponse values"
            raise TypeError(msg)
        return response

    def _parse_provider_response_delta(self, response: object) -> ModelResponse:
        if not isinstance(response, ModelResponse):
            msg = "SyntheticModel only parses ModelResponse values"
            raise TypeError(msg)
        return response

    def _execution(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None,
        tool_choice: str | Mapping[str, Any] | None,
    ) -> tuple[SyntheticPlan, str, str | None]:
        prompt = self._latest_user_text(messages)
        tool_available = tool_choice != "none" and self._tool_available(tools, "sleep")
        plan = self.plan_for_prompt(prompt, tool_available=tool_available)
        last_message = messages[-1] if messages else None
        continuation = (
            plan.tool_call_id is not None
            and last_message is not None
            and last_message.role == self.tool_message_role
            and last_message.tool_call_id == plan.tool_call_id
        )
        phase = "continuation" if continuation else "initial"
        group = self._barrier_group(prompt) if self._is_active(prompt) else None
        return plan, phase, group

    def _is_active(self, prompt: str) -> bool:
        return self._activation_regex is None or self._activation_regex.search(prompt) is not None

    def _identity(self, prompt: str) -> str:
        if self._identity_regex is None:
            return prompt
        match = self._identity_regex.search(prompt)
        return match.group(0) if match is not None else prompt

    def _barrier_group(self, prompt: str) -> str | None:
        if self._barrier_group_regex is None:
            return None
        match = self._barrier_group_regex.search(prompt)
        if match is None:
            return None
        return match.group(1) if match.lastindex else match.group(0)

    @staticmethod
    def _latest_user_text(messages: Sequence[Message]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.get_content_string()
        return ""

    @staticmethod
    def _tool_available(tools: Sequence[Mapping[str, Any]] | None, tool_name: str) -> bool:
        for tool in tools or ():
            function = tool.get("function")
            if isinstance(function, Mapping) and function.get("name") == tool_name:
                return True
            if tool.get("name") == tool_name:
                return True
        return False

    @staticmethod
    def _content_for_phase(plan: SyntheticPlan, phase: str) -> str:
        return plan.suffix if phase == "continuation" else plan.prefix

    @staticmethod
    def _tool_calls_for_phase(plan: SyntheticPlan, phase: str) -> list[dict[str, Any]]:
        if phase != "initial" or plan.tool_call_id is None or plan.sleep_seconds is None:
            return []
        return [
            {
                "id": plan.tool_call_id,
                "type": "function",
                "function": {
                    "name": "sleep",
                    "arguments": json.dumps({"seconds": plan.sleep_seconds}, separators=(",", ":")),
                },
            },
        ]

    def _chunks(self, content: str) -> Iterator[str]:
        for start in range(0, len(content), self.chunk_chars):
            yield content[start : start + self.chunk_chars]

    def _sleep_sync(self, character_count: int) -> None:
        if self.chars_per_second:
            time.sleep(character_count / self.chars_per_second)

    async def _sleep_async(self, character_count: int) -> None:
        if self.chars_per_second:
            await asyncio.sleep(character_count / self.chars_per_second)

    def _reach_sync_barrier(self, plan: SyntheticPlan, phase: str, group: str | None) -> None:
        if phase != "initial" or group is None or self.barrier_size == 0:
            return
        deadline = time.monotonic() + self.barrier_timeout_seconds
        state = self._state
        with state.sync_condition:
            members = state.sync_barrier_members.setdefault(group, set())
            members.add(plan.request_id)
            self._record("barrier_reached", plan, phase, group)
            if len(members) >= self.barrier_size:
                state.sync_released_groups.add(group)
                state.sync_condition.notify_all()
            while group not in state.sync_released_groups:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    msg = f"synthetic model barrier {group!r} reached {len(members)}/{self.barrier_size}"
                    raise TimeoutError(msg)
                state.sync_condition.wait(timeout=remaining)

    async def _reach_async_barrier(self, plan: SyntheticPlan, phase: str, group: str | None) -> None:
        if phase != "initial" or group is None or self.barrier_size == 0:
            return
        state = self._get_async_state()
        async with asyncio.timeout(self.barrier_timeout_seconds):
            async with state.condition:
                members = state.async_barrier_members.setdefault(group, set())
                members.add(plan.request_id)
                self._record("barrier_reached", plan, phase, group)
                if len(members) >= self.barrier_size:
                    state.async_released_groups.add(group)
                    state.condition.notify_all()
                await state.condition.wait_for(lambda: group in state.async_released_groups)

    def _get_async_state(self) -> _AsyncCoordinationState:
        loop = asyncio.get_running_loop()
        with self._state.async_states_lock:
            closed_loops = [known_loop for known_loop in self._state.async_states if known_loop.is_closed()]
            for closed_loop in closed_loops:
                del self._state.async_states[closed_loop]
            state = self._state.async_states.get(loop)
            if state is None:
                state = _AsyncCoordinationState()
                self._state.async_states[loop] = state
            return state

    def _get_async_stream_lock(self) -> asyncio.Lock:
        return self._get_async_state().stream_lock

    def _record(self, kind: str, plan: SyntheticPlan, phase: str, group: str | None) -> None:
        if self.telemetry_path is None:
            return
        payload = {
            "kind": kind,
            "request_id": plan.request_id,
            "phase": phase,
            "group": group,
            "time": time.monotonic(),
        }
        path = Path(self.telemetry_path)
        with self._state.telemetry_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _AsyncNullLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


__all__ = [
    "SyntheticModel",
    "SyntheticPlan",
    "synthetic_plan",
    "synthetic_request_id",
]
