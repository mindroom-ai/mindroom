"""Admin-only, encrypted Matrix to-device RPC for per-account provider usage limits."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeIs, cast

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    PinnedMatrixDevice,
    authenticated_sender_is_current,
    authenticated_sender_matches,
    send_encrypted_to_device,
)
from mindroom.requester_identity import is_platform_administrator
from mindroom.usage.provider_limits import fit_to_device_budget, get_provider_usage_snapshot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["register_provider_usage_receiver"]

_REQUEST_TYPE = "io.mindroom.usage.request"
_RESPONSE_TYPE = "io.mindroom.usage.response"
_DEADLINE_SECONDS = 15.0
_MAX_IN_FLIGHT = 4
_MAX_USER_IN_FLIGHT = 2
_MAX_USER_REQUESTS = 6
_MAX_RATE_USERS = 1024
_MAX_RESPONSE_BYTES = 48_000
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Request:
    request_id: str
    room_id: str
    force_refresh: bool


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
    context: dict[str, object] = {
        "sender": event.sender,
        "sender_device_id": event.authenticated_sender.device_id,
    }
    if request is not None:
        context |= {
            "request_id": request.request_id,
            "room_id": request.room_id,
            "force_refresh": request.force_refresh,
        }
    if received is not None:
        context["elapsed_ms"] = round((time.monotonic() - received) * 1000)
    return context


def _parse(event: object) -> _Request | None:
    if not _is_request(event) or not isinstance(raw := event.source.get("content"), Mapping):
        return None
    raw = cast("Mapping[str, object]", raw)
    if set(raw) not in ({"version", "request_id", "room_id"}, {"version", "request_id", "room_id", "force_refresh"}):
        return None
    if type(raw["version"]) is not int or raw["version"] != 1:
        return None
    request_id, room_id = raw.get("request_id"), raw.get("room_id")
    force_refresh = raw.get("force_refresh", False)
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 128
        or not isinstance(room_id, str)
        or not room_id.strip()
        or len(room_id) > 1024
        or type(force_refresh) is not bool
    ):
        return None
    return _Request(request_id, room_id, force_refresh)


def _drop(
    event: nio.AuthenticatedToDeviceEvent,
    request: _Request | None,
    reason: str,
    received: float | None = None,
) -> bool:
    logger.info("provider_usage_dropped", reason=reason, **_log_context(event, request, received))
    return False


class _Receiver:
    def __init__(
        self,
        *,
        client: nio.AsyncClient,
        runtime_paths: RuntimePaths,
        config_getter: Callable[[], Config],
        answers_in_room: Callable[[str], bool],
    ) -> None:
        self.client = client
        self.paths = runtime_paths
        self.config_getter = config_getter
        self.answers_in_room = answers_in_room
        self.active: dict[tuple[str, str, _Request], tuple[float, PinnedMatrixDevice]] = {}
        self.rates: OrderedDict[str, list[float]] = OrderedDict()

    def _target(self, event: nio.AuthenticatedToDeviceEvent) -> PinnedMatrixDevice | None:
        if not authenticated_sender_is_current(self.client, event):
            return None
        identity = event.authenticated_sender
        return PinnedMatrixDevice(identity.user_id, identity.device_id, identity.ed25519)

    def admit(self, event: nio.AuthenticatedToDeviceEvent) -> bool:  # noqa: C901, PLR0911
        """Authorize and reserve bounded work synchronously before scheduling a task."""
        if not _is_request(event):
            return False
        request = _parse(event)
        if request is None:
            return _drop(event, None, "unparseable")
        # Single-responder election only; room membership is NEVER authorization.
        if not self.answers_in_room(request.room_id):
            return _drop(event, request, "not_room_poster")
        if not is_platform_administrator(event.sender, self.config_getter(), self.paths):
            return _drop(event, request, "not_admin")
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

    async def _respond(
        self,
        event: nio.AuthenticatedToDeviceEvent,
        request: _Request,
        target: PinnedMatrixDevice,
    ) -> int:
        config = self.config_getter()
        if not is_platform_administrator(event.sender, config, self.paths):
            raise _DroppedError(reason="not_admin")
        if not authenticated_sender_is_current(self.client, event) or not authenticated_sender_matches(
            self.client,
            event,
            target,
        ):
            raise _DroppedError(reason="sender_device_changed")
        snapshot = await get_provider_usage_snapshot(
            config=config,
            runtime_paths=self.paths,
            force_refresh=request.force_refresh,
        )
        content = fit_to_device_budget(snapshot.to_dict(request_id=request.request_id), max_bytes=_MAX_RESPONSE_BYTES)
        # Recheck admin status and pinned sender device immediately before sending the encrypted reply.
        if not is_platform_administrator(event.sender, self.config_getter(), self.paths):
            raise _DroppedError(reason="not_admin")
        if not authenticated_sender_is_current(self.client, event) or not authenticated_sender_matches(
            self.client,
            event,
            target,
        ):
            raise _DroppedError(reason="sender_device_changed")
        await send_encrypted_to_device(
            self.client,
            target,
            event_type=_RESPONSE_TYPE,
            content=content,
        )
        accounts = content.get("accounts")
        return len(accounts) if isinstance(accounts, list) else 0

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
        deadline_scope = asyncio.timeout(max(0.0, deadline - time.monotonic()))
        try:
            async with deadline_scope:
                account_count = await self._respond(event, request, target)
        except _DroppedError as dropped:
            _drop(event, request, dropped.reason, received)
        except asyncio.CancelledError:
            _drop(event, request, "cancelled", received)
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline_scope.expired():
                _drop(event, request, "timeout", received)
                return
            logger.warning(
                "provider_usage_dropped",
                reason="error",
                error_type=type(exc).__name__,
                **_log_context(event, request, received),
            )
        else:
            logger.info(
                "provider_usage_answered",
                account_count=account_count,
                **_log_context(event, request, received),
            )
        finally:
            self.active.pop(key, None)


def register_provider_usage_receiver(
    *,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    answers_in_room: Callable[[str], bool],
    callback_wrapper: Callable[
        [Callable[[nio.AuthenticatedToDeviceEvent], Awaitable[None]]],
        Callable[..., Awaitable[None]],
    ],
) -> None:
    """Register encrypted provider-usage RPC on this bot's device before any task is created."""
    receiver = _Receiver(
        client=client,
        runtime_paths=runtime_paths,
        config_getter=config_getter,
        answers_in_room=answers_in_room,
    )
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: nio.AuthenticatedToDeviceEvent) -> None:
        if receiver.admit(event):
            await wrapped(event)

    client.add_to_device_callback(on_event, nio.AuthenticatedToDeviceEvent)
