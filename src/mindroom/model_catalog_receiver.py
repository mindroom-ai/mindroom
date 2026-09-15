"""Router-owned authenticated, bounded Matrix model discovery lifecycle."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    PinnedMatrixDevice,
    authenticated_sender_matches,
    send_encrypted_to_device,
)
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.model_catalog import ModelCatalog
from mindroom.model_selection_scope import validate_model_picker_scope
from mindroom.thread_models import resolve_thread_model_override

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.model_selection_scope import ModelPickerScope

__all__ = ["register_model_catalog_receiver"]

_REQUEST_TYPE = "io.mindroom.models.request"
_RESPONSE_TYPE = "io.mindroom.models.response"
_DEADLINE_SECONDS = 12.0
_MAX_IN_FLIGHT = 8
_MAX_DEVICE_REQUESTS = 8
_MAX_RATE_DEVICES = 1024
_MAX_MODELS = 256
_MAX_RESPONSE_BYTES = 64 * 1024
logger = get_logger(__name__)


@dataclass(frozen=True)
class _Request:
    request_id: str
    room_id: str
    thread_id: str | None


def _parse(event: object) -> _Request | None:
    if not isinstance(event, AuthenticatedToDeviceEvent) or event.type != _REQUEST_TYPE:
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


class _Receiver:
    def __init__(
        self,
        *,
        client: nio.AsyncClient,
        runtime_paths: RuntimePaths,
        config_getter: Callable[[], Config],
        membership_index: AgentReplyMembershipIndex,
    ) -> None:
        self.client = client
        self.paths = runtime_paths
        self.config_getter = config_getter
        self.membership_index = membership_index
        self.catalog = ModelCatalog(client=client, runtime_paths=runtime_paths)
        self.active: dict[tuple[str, str, _Request], tuple[float, PinnedMatrixDevice]] = {}
        self.rates: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def _target(self, event: AuthenticatedToDeviceEvent) -> PinnedMatrixDevice | None:
        olm = self.client.olm
        if olm is None:
            return None
        device = olm.device_store[event.sender].get(event.authenticated_device_id)
        if device is None or device.blacklisted:
            return None
        return PinnedMatrixDevice(event.sender, event.authenticated_device_id, device.ed25519)

    def admit(self, event: AuthenticatedToDeviceEvent) -> bool:
        """Reserve bounded work synchronously, before the task wrapper queues it."""
        request = _parse(event)
        if request is None:
            return False
        target = self._target(event)
        if target is None:
            return False
        key = (event.sender, event.authenticated_device_id, request)
        if key in self.active or len(self.active) >= _MAX_IN_FLIGHT:
            return False
        now = time.monotonic()
        while self.rates and next(iter(self.rates.values()))[-1] <= now - _DEADLINE_SECONDS:
            self.rates.popitem(last=False)
        device_key = (event.sender, event.authenticated_device_id)
        if device_key not in self.rates and len(self.rates) >= _MAX_RATE_DEVICES:
            return False
        recent = [stamp for stamp in self.rates.get(device_key, []) if stamp > now - _DEADLINE_SECONDS]
        if len(recent) >= _MAX_DEVICE_REQUESTS:
            return False
        self.rates[device_key] = [*recent, now]
        self.rates.move_to_end(device_key)
        self.active[key] = (now + _DEADLINE_SECONDS, target)
        return True

    async def _scope(
        self,
        event: AuthenticatedToDeviceEvent,
        request: _Request,
        config: Config,
    ) -> ModelPickerScope | None:
        return await validate_model_picker_scope(
            client=self.client,
            config=config,
            runtime_paths=self.paths,
            membership_index=self.membership_index,
            room_id=request.room_id,
            requester_user_id=event.sender,
            thread_id=request.thread_id,
        )

    def _selection(self, request: _Request, config: Config, scope: ModelPickerScope) -> dict[str, object]:
        return {
            "override": resolve_thread_model_override(
                self.paths,
                request.thread_id,
                configured_models=config.models,
            ).active,
            "inherited": [
                {
                    "entity": name,
                    "model": config.resolve_runtime_model(
                        entity_name=name,
                        room_id=request.room_id,
                        thread_id=None,
                        runtime_paths=self.paths,
                    ).model_name,
                }
                for name in scope.entity_names
            ],
        }

    async def _respond(self, event: AuthenticatedToDeviceEvent, request: _Request, target: PinnedMatrixDevice) -> None:
        config = self.config_getter()
        if len(config.models) > _MAX_MODELS or not authenticated_sender_matches(self.client, event, target):
            return
        scope = await self._scope(event, request, config)
        if scope is None:
            return
        entries, revision = await self.catalog.snapshot(config)
        scope = await self._scope(event, request, config)
        if scope is None:
            return
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
            return

        async def before_send() -> bool:
            return (
                self.config_getter() is config
                and await self._scope(event, request, config) == scope
                and authenticated_sender_matches(self.client, event, target)
            )

        if not await before_send():
            return
        await send_encrypted_to_device(
            self.client,
            target,
            event_type=_RESPONSE_TYPE,
            content=content,
            verify_device=False,
            before_send=before_send,
        )

    async def on_event(self, event: AuthenticatedToDeviceEvent) -> None:
        request = _parse(event)
        if request is None:
            return
        key = (event.sender, event.authenticated_device_id, request)
        admitted = self.active.get(key)
        if admitted is None:
            return
        deadline, target = admitted
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await self._respond(event, request, target)
        except Exception as exc:
            # Operational unavailability is a timeout to the client, never a
            # partial catalog or a provider/configuration error disclosure.
            logger.warning("model_catalog_unavailable", error_type=type(exc).__name__)
        finally:
            self.active.pop(key, None)


def register_model_catalog_receiver(
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    membership_index: AgentReplyMembershipIndex,
    callback_wrapper: Callable[
        [Callable[[AuthenticatedToDeviceEvent], Awaitable[None]]],
        Callable[..., Awaitable[None]],
    ],
) -> None:
    """Register router-only discovery, with admission before background task creation."""
    if agent_name != ROUTER_AGENT_NAME:
        return
    receiver = _Receiver(
        client=client,
        runtime_paths=runtime_paths,
        config_getter=config_getter,
        membership_index=membership_index,
    )
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: AuthenticatedToDeviceEvent) -> None:
        if receiver.admit(event):
            await wrapped(event)

    client.add_to_device_callback(on_event, AuthenticatedToDeviceEvent)
