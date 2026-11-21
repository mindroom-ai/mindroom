"""Owner-account authenticated, bounded Matrix model discovery lifecycle."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeIs, cast

import nio

from mindroom.config.models import split_model_reference
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    PinnedMatrixDevice,
    authenticated_sender_is_current,
    authenticated_sender_matches,
    send_encrypted_to_device,
)
from mindroom.model_catalog import ModelCatalog
from mindroom.model_selection_scope import validate_model_picker_scope
from mindroom.thread_models import resolve_thread_model_override
from mindroom.turn_model import resolve_inherited_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.model_selection_scope import ModelPickerScope

__all__ = ["register_model_catalog_receiver"]

_REQUEST_TYPE = "io.mindroom.models.request"
_RESPONSE_TYPE = "io.mindroom.models.response"
_DEADLINE_SECONDS = 12.0
_MAX_IN_FLIGHT = 8
# One Matrix account may log in any number of devices, so per-user limits keep
# it from holding the whole shared pool.
_MAX_USER_IN_FLIGHT = 2
_MAX_USER_REQUESTS = 8
_MAX_RATE_USERS = 1024
_MAX_MODELS = 256
_MAX_RESPONSE_BYTES = 64 * 1024
logger = get_logger(__name__)


@dataclass(frozen=True)
class _Request:
    request_id: str
    room_id: str
    thread_id: str | None


class _DroppedError(Exception):
    """A request this device declines; by design, the client receives no reply."""

    def __init__(self, *, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_request(event: object) -> TypeIs[nio.AuthenticatedToDeviceEvent]:
    return isinstance(event, nio.AuthenticatedToDeviceEvent) and event.type == _REQUEST_TYPE


def _log_context(
    event: nio.AuthenticatedToDeviceEvent,
    request: _Request | None,
    received: float | None = None,
) -> dict[str, object]:
    """Identify a request without its content, for diagnosing pickers that never appear."""
    context: dict[str, object] = {
        "sender": event.sender,
        "sender_device_id": event.authenticated_sender.device_id,
    }
    if request is not None:
        context |= {"request_id": request.request_id, "room_id": request.room_id, "thread_id": request.thread_id}
    if received is not None:
        context["elapsed_ms"] = round((time.monotonic() - received) * 1000)
    return context


def _parse(event: object) -> _Request | None:
    if not _is_request(event):
        return None
    raw = event.source.get("content")
    if not isinstance(raw, Mapping):
        return None
    raw = cast("Mapping[str, object]", raw)
    if set(raw) not in ({"version", "request_id", "room_id"}, {"version", "request_id", "room_id", "thread_id"}):
        return None
    if type(raw["version"]) is not int or raw["version"] != 1:
        return None
    for key, limit in (("request_id", 128), ("room_id", 1024), ("thread_id", 1024)):
        if key == "thread_id" and key not in raw:
            continue
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return None
    return _Request(
        cast("str", raw["request_id"]),
        cast("str", raw["room_id"]),
        cast("str | None", raw.get("thread_id")),
    )


def _model_choice(field: str, reference: str | None) -> dict[str, object]:
    """Report a model reference as its catalog key in ``field``, with its level as ``reasoning`` when it names one."""
    if reference is None:
        return {field: None}
    model_name, level = split_model_reference(reference)
    return {field: model_name} if level is None else {field: model_name, "reasoning": level}


def _drop(
    event: nio.AuthenticatedToDeviceEvent,
    request: _Request | None,
    reason: str,
    received: float | None = None,
) -> bool:
    """Log one silently dropped request; the client sees only a missing reply."""
    logger.info("model_catalog_dropped", reason=reason, **_log_context(event, request, received))
    return False


class _Receiver:
    def __init__(
        self,
        *,
        client: nio.AsyncClient,
        runtime_paths: RuntimePaths,
        config_getter: Callable[[], Config],
        membership_index: AgentReplyMembershipIndex,
        answers_in_room: Callable[[str], bool],
    ) -> None:
        self.answers_in_room = answers_in_room
        self.client = client
        self.paths = runtime_paths
        self.config_getter = config_getter
        self.membership_index = membership_index
        self.catalog = ModelCatalog(client=client, runtime_paths=runtime_paths)
        self.active: dict[tuple[str, str, _Request], tuple[float, PinnedMatrixDevice]] = {}
        self.rates: OrderedDict[str, list[float]] = OrderedDict()

    def _target(self, event: nio.AuthenticatedToDeviceEvent) -> PinnedMatrixDevice | None:
        if not authenticated_sender_is_current(self.client, event):
            return None
        identity = event.authenticated_sender
        return PinnedMatrixDevice(identity.user_id, identity.device_id, identity.ed25519)

    def admit(self, event: nio.AuthenticatedToDeviceEvent) -> bool:  # noqa: C901, PLR0911
        """Reserve bounded work synchronously, before the task wrapper queues it."""
        if not _is_request(event):
            return False
        request = _parse(event)
        if request is None:
            return _drop(event, None, "unparseable")
        # One device answers per room, so a client sees one runtime there.
        if not self.answers_in_room(request.room_id):
            return _drop(event, request, "not_room_poster")
        target = self._target(event)
        if target is None:
            return _drop(event, request, "unknown_sender_device")
        key = (event.sender, event.authenticated_sender.device_id, request)
        if key in self.active:
            return _drop(event, request, "duplicate_in_flight")
        if len(self.active) >= _MAX_IN_FLIGHT:
            return _drop(event, request, "rate_limited_global")
        if sum(sender == event.sender for sender, _, _ in self.active) >= _MAX_USER_IN_FLIGHT:
            return _drop(event, request, "rate_limited_user_inflight")
        now = time.monotonic()
        while self.rates and next(iter(self.rates.values()))[-1] <= now - _DEADLINE_SECONDS:
            self.rates.popitem(last=False)
        if event.sender not in self.rates and len(self.rates) >= _MAX_RATE_USERS:
            return _drop(event, request, "rate_limited_global_users")
        recent = [stamp for stamp in self.rates.get(event.sender, []) if stamp > now - _DEADLINE_SECONDS]
        if len(recent) >= _MAX_USER_REQUESTS:
            return _drop(event, request, "rate_limited_user_window")
        self.rates[event.sender] = [*recent, now]
        self.rates.move_to_end(event.sender)
        self.active[key] = (now + _DEADLINE_SECONDS, target)
        return True

    async def _scope(
        self,
        event: nio.AuthenticatedToDeviceEvent,
        request: _Request,
        config: Config,
    ) -> ModelPickerScope:
        scope = await validate_model_picker_scope(
            client=self.client,
            config=config,
            runtime_paths=self.paths,
            membership_index=self.membership_index,
            room_id=request.room_id,
            requester_user_id=event.sender,
            thread_id=request.thread_id,
        )
        if isinstance(scope, str):
            raise _DroppedError(reason=scope)
        return scope

    def _selection(self, request: _Request, config: Config, scope: ModelPickerScope) -> dict[str, object]:
        thread_overrides = resolve_thread_model_override(self.paths, request.thread_id, config=config).active
        # One reported override must govern every entity in the picker, never only some of them.
        shared_overrides = {thread_overrides.get(name) for name in scope.entity_names}
        override = shared_overrides.pop() if len(shared_overrides) == 1 else None
        inherited = [
            (name, user_id, *resolve_inherited_model(config, self.paths, name, request.room_id))
            for name, user_id in scope.entity_user_ids
        ]
        return {
            **_model_choice("override", override),
            "inherited": [
                {"entity": name, "user_id": user_id, **_model_choice("model", model), "source": source}
                for name, user_id, model, source in inherited
            ],
        }

    async def _respond(
        self,
        event: nio.AuthenticatedToDeviceEvent,
        request: _Request,
        target: PinnedMatrixDevice,
    ) -> int:
        """Send the catalog and return its model count, or raise why the request is dropped."""
        config = self.config_getter()
        if len(config.models) > _MAX_MODELS:
            raise _DroppedError(reason="too_many_models")
        if not authenticated_sender_matches(self.client, event, target):
            raise _DroppedError(reason="sender_device_changed")
        await self._scope(event, request, config)
        entries, revision = await self.catalog.snapshot(config)
        scope = await self._scope(event, request, config)
        content: dict[str, object] = {
            "version": 1,
            "request_id": request.request_id,
            "room_id": request.room_id,
            "capabilities": ["model_selection"],
            "agent_user_ids": list(scope.agent_user_ids),
            "catalog_revision": revision,
            "models": entries,
            "selection": await asyncio.to_thread(self._selection, request, config, scope),
        }
        if request.thread_id is not None:
            content["thread_id"] = request.thread_id
        # Match the transport's default JSON representation, including escapes.
        if len(json.dumps(content).encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise _DroppedError(reason="response_too_large")

        # Authorize at the NIO handoff; NIO owns preparation and delivery retries.
        if self.config_getter() is not config:
            raise _DroppedError(reason="config_changed")
        if await self._scope(event, request, config) != scope:
            raise _DroppedError(reason="scope_changed")
        if self.config_getter() is not config:
            raise _DroppedError(reason="config_changed")
        if not authenticated_sender_matches(self.client, event, target):
            raise _DroppedError(reason="sender_device_changed")
        await send_encrypted_to_device(
            self.client,
            target,
            event_type=_RESPONSE_TYPE,
            content=content,
        )
        return len(entries)

    async def on_event(self, event: nio.AuthenticatedToDeviceEvent) -> None:
        request = _parse(event)
        if request is None:
            return
        key = (event.sender, event.authenticated_sender.device_id, request)
        admitted = self.active.get(key)
        if admitted is None:
            return
        deadline, target = admitted
        received = deadline - _DEADLINE_SECONDS
        deadline_scope = asyncio.timeout(max(0, deadline - time.monotonic()))
        try:
            async with deadline_scope:
                model_count = await self._respond(event, request, target)
        except _DroppedError as dropped:
            _drop(event, request, dropped.reason, received)
        except asyncio.CancelledError:
            # Shutdown or restart cancels the task mid-request. Only the awaited
            # body can raise it: the outcome handlers never await, so no request
            # logs twice.
            _drop(event, request, "cancelled", received)
            raise
        except Exception as exc:
            # An inner TimeoutError, such as an upload timeout, is an error, not this deadline.
            if isinstance(exc, TimeoutError) and deadline_scope.expired():
                _drop(event, request, "timeout", received)
                return
            # Operational unavailability is a timeout to the client, never a
            # partial catalog or a provider/configuration error disclosure.
            logger.warning(
                "model_catalog_dropped",
                reason="error",
                error_type=type(exc).__name__,
                **({"error_message": str(exc)} if isinstance(exc, nio.LocalProtocolError) else {}),
                **_log_context(event, request, received),
            )
        else:
            logger.info("model_catalog_answered", model_count=model_count, **_log_context(event, request, received))
        finally:
            self.active.pop(key, None)


def register_model_catalog_receiver(
    *,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    membership_index: AgentReplyMembershipIndex,
    answers_in_room: Callable[[str], bool],
    callback_wrapper: Callable[
        [Callable[[nio.AuthenticatedToDeviceEvent], Awaitable[None]]],
        Callable[..., Awaitable[None]],
    ],
) -> None:
    """Register discovery on this bot's device, which answers where ``answers_in_room``, before any task is made."""
    receiver = _Receiver(
        client=client,
        runtime_paths=runtime_paths,
        config_getter=config_getter,
        membership_index=membership_index,
        answers_in_room=answers_in_room,
    )
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: nio.AuthenticatedToDeviceEvent) -> None:
        if receiver.admit(event):
            await wrapped(event)

    client.add_to_device_callback(on_event, nio.AuthenticatedToDeviceEvent)
