"""Internal and plugin-extensible EventSource registry and emitter seam for reactive triggers."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot
    from mindroom.reactive_triggers.dispatch import DispatchResult
    from mindroom.reactive_triggers.models import ReactiveTriggerRecord, TriggerEvent

EVENT_SOURCE_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_RESERVED_BUILTIN_SOURCE_IDS: frozenset[str] = frozenset({"slack"})

type ExternalMatchValue = str | bool | int | list[str]
type ExternalBindingValue = str | bool | int


class EventSourceInactiveError(RuntimeError):
    """Raised when an EventSourceEmitter emits after its registration was replaced or removed."""


class EventSourceEmitHandler(Protocol):
    """Async fan-out callback implemented by ReactiveTriggerService.emit_event."""

    async def __call__(
        self,
        source_id: str,
        event: TriggerEvent,
        *,
        owner_user_ids: Collection[str] | None = None,
        audience: object = None,
        dedupe_key: str | None = None,
    ) -> int:
        """Fan out one normalized event for source_id and return newly enqueued firing count."""
        ...


@dataclass(frozen=True, slots=True)
class EventSourceDescriptor:
    """User-facing metadata and subscription defaults for a registered EventSource."""

    source_id: str
    description: str
    allowed_kinds: tuple[str, ...] = ()
    match_fields: tuple[str, ...] = ()
    example: str | None = None
    default_once: bool = True
    default_new_thread: bool = False
    default_thread_key_from_event: bool = False
    default_reopen_on_fire: bool = False
    private_room_only: bool = False


@dataclass(frozen=True, slots=True)
class ValidatedSubscription:
    """Normalized kind, match filters, and immutable server-owned binding returned by validate_subscription."""

    kind: str
    match: dict[str, ExternalMatchValue] = field(default_factory=dict)
    binding: dict[str, ExternalBindingValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventSourceContext:
    """Runtime context supplied to EventSource discovery, validation, and access-check hooks."""

    owner_user_id: str
    config: Config
    runtime_paths: RuntimePaths
    target_room_id: str | None = None
    target_agent: str | None = None
    target_room_state: RoomStateSnapshot | None = None
    calling_room_id: str | None = None
    calling_room_state: RoomStateSnapshot | None = None
    client: nio.AsyncClient | None = None
    credentials_manager: CredentialsManager | None = None


class EventSource:
    """Base class for built-in and plugin-registered reactive trigger event sources."""

    source_id: str = ""
    description: str = ""
    allowed_kinds: tuple[str, ...] = ()
    match_fields: tuple[str, ...] = ()
    example: str | None = None
    default_once: bool = True
    default_new_thread: bool = False
    default_thread_key_from_event: bool = False
    default_reopen_on_fire: bool = False
    private_room_only: bool = False

    async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
        """Return user-facing metadata for ctx.owner_user_id, or None when hidden/unavailable."""
        del ctx
        return EventSourceDescriptor(
            source_id=self.source_id,
            description=self.description or f"Event source {self.source_id!r}",
            allowed_kinds=tuple(self.allowed_kinds),
            match_fields=tuple(self.match_fields),
            example=self.example,
            default_once=self.default_once,
            default_new_thread=self.default_new_thread,
            default_thread_key_from_event=self.default_thread_key_from_event,
            default_reopen_on_fire=self.default_reopen_on_fire,
            private_room_only=self.private_room_only,
        )

    async def validate_subscription(
        self,
        ctx: EventSourceContext,
        *,
        kind: str,
        match: Mapping[str, ExternalMatchValue],
        existing_binding: Mapping[str, ExternalBindingValue] | None = None,
    ) -> ValidatedSubscription:
        """Validate and normalize kind/match, preserving any existing server-owned binding."""
        del ctx
        stripped_kind = kind.strip()
        if not stripped_kind:
            msg = "Event kind must not be empty"
            raise ValueError(msg)
        if self.allowed_kinds and stripped_kind not in self.allowed_kinds:
            msg = f"Event kind {stripped_kind!r} is not allowed for event source {self.source_id!r}"
            raise ValueError(msg)
        from mindroom.reactive_triggers.models import ExternalEventSourceSpec  # noqa: PLC0415

        normalized = ExternalEventSourceSpec(source_id=self.source_id, kind=stripped_kind, match=dict(match))
        return ValidatedSubscription(
            kind=normalized.kind,
            match=normalized.match,
            binding=dict(existing_binding) if existing_binding is not None else {},
        )

    def matches_rule(
        self,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent,
        *,
        audience: object = None,
        has_thread_binding: bool = False,
    ) -> bool:
        """Return whether event matches rule; defaults to structured external_event matching."""
        from mindroom.reactive_triggers.matching import matches_external_event  # noqa: PLC0415
        from mindroom.reactive_triggers.models import ExternalEventSourceSpec  # noqa: PLC0415

        del audience, has_thread_binding
        return isinstance(rule.source, ExternalEventSourceSpec) and matches_external_event(rule.source, event)

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> DispatchResult | None:
        """Recheck live delivery access before triage/dispatch and for paused_reason reporting."""
        del ctx, rule, event
        return None

    async def check_history_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> bool:
        """Return whether the caller in ctx may inspect firing payloads for rule."""
        del ctx, rule, event
        return True


@dataclass(frozen=True, slots=True)
class EventSourceEmitter:
    """Token-fenced emitter handle that stamps source_type and source_id on emitted events."""

    source_id: str
    token: str = field(repr=False)
    _registry: EventSourceRegistry = field(repr=False)

    async def emit(
        self,
        *,
        kind: str,
        event_id: str,
        text: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        actor: str | None = None,
        thread_key: str | None = None,
        data: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
        owner_user_ids: Collection[str] | None = None,
        audience: object = None,
        dedupe_key: str | None = None,
    ) -> int:
        """Emit one normalized event stamped with this emitter's source_id, failing closed once inactive."""
        if not self._registry.is_active_token(self.source_id, self.token):
            msg = f"Event source {self.source_id!r} emitter is no longer active"
            raise EventSourceInactiveError(msg)
        stripped_kind = kind.strip()
        stripped_event_id = event_id.strip()
        if not stripped_kind or not stripped_event_id:
            msg = "Event kind and event_id must be non-empty strings"
            raise ValueError(msg)
        from mindroom.reactive_triggers.models import TriggerEvent  # noqa: PLC0415

        event = TriggerEvent(
            source_type="external_event",
            source_id=self.source_id,
            kind=stripped_kind,
            event_id=stripped_event_id,
            timestamp=time.time() if timestamp is None else timestamp,
            actor=actor,
            title=title,
            summary=summary,
            text=text,
            thread_key=thread_key,
            data=dict(data) if data is not None else {},
        )
        return await self._registry._emit_handler(
            self.source_id,
            event,
            owner_user_ids=owner_user_ids,
            audience=audience,
            dedupe_key=dedupe_key,
        )


@dataclass(frozen=True, slots=True)
class _RegisteredEventSource:
    source: EventSource
    owner_plugin: str | None
    token: str
    emitter: EventSourceEmitter


class EventSourceRegistry:
    """Reload-safe, token-fenced registry of built-in and plugin-owned EventSources."""

    def __init__(self, config_provider: Callable[[], Config], emit_handler: EventSourceEmitHandler) -> None:
        self._config_provider = config_provider
        self._emit_handler = emit_handler
        self._lock = threading.RLock()
        self._entries: dict[str, _RegisteredEventSource] = {}

    def register(self, source: EventSource, *, owner_plugin: str | None = None) -> EventSourceEmitter:
        """Register an EventSource with namespace/collision checks and atomic same-plugin hot-reload replacement."""
        source_id = source.source_id
        if not isinstance(source_id, str) or EVENT_SOURCE_ID_PATTERN.fullmatch(source_id) is None:
            msg = f"Invalid event source_id {source_id!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"
            raise ValueError(msg)
        if owner_plugin is not None:
            if not owner_plugin or owner_plugin != owner_plugin.strip():
                msg = f"Invalid owner_plugin {owner_plugin!r}"
                raise ValueError(msg)
            if source_id != owner_plugin and not source_id.startswith((f"{owner_plugin}_", f"{owner_plugin}-")):
                msg = (
                    f"Plugin {owner_plugin!r} event source_id {source_id!r} must equal {owner_plugin!r} "
                    f"or start with '{owner_plugin}_' or '{owner_plugin}-'"
                )
                raise ValueError(msg)
            if source_id in _RESERVED_BUILTIN_SOURCE_IDS:
                msg = f"Event source {source_id!r} collides with a reserved built-in event source"
                raise ValueError(msg)

        with self._lock:
            if source_id in self._config_provider().external_trigger_policy.event_sources:
                msg = f"Event source {source_id!r} collides with a YAML-configured external event source"
                raise ValueError(msg)
            existing = self._entries.get(source_id)
            if existing is not None and not (owner_plugin is not None and existing.owner_plugin == owner_plugin):
                owner = f"plugin {existing.owner_plugin!r}" if existing.owner_plugin is not None else "built-in"
                msg = f"Event source {source_id!r} is already registered by {owner}"
                raise ValueError(msg)
            token = uuid4().hex
            emitter = EventSourceEmitter(source_id=source_id, token=token, _registry=self)
            self._entries[source_id] = _RegisteredEventSource(source, owner_plugin, token, emitter)
            return emitter

    def unregister(self, source_id: str, token: str) -> bool:
        """Unregister source_id only if token still matches the active registration."""
        with self._lock:
            entry = self._entries.get(source_id)
            if entry is None or entry.token != token:
                return False
            del self._entries[source_id]
            return True

    def get(self, source_id: str) -> EventSource | None:
        """Return the registered EventSource for source_id, or None."""
        with self._lock:
            entry = self._entries.get(source_id)
            return entry.source if entry is not None else None

    def all(self) -> tuple[EventSource, ...]:
        """Return all currently registered EventSources sorted by source_id."""
        with self._lock:
            return tuple(self._entries[key].source for key in sorted(self._entries))

    def emitter_for_plugin(self, plugin_name: str, source_id: str) -> EventSourceEmitter | None:
        """Return the active EventSourceEmitter when source_id is currently registered by plugin_name."""
        with self._lock:
            entry = self._entries.get(source_id)
            return entry.emitter if entry is not None and entry.owner_plugin == plugin_name else None

    def is_active_token(self, source_id: str, token: str) -> bool:
        """Return whether token still matches the active registration for source_id."""
        with self._lock:
            entry = self._entries.get(source_id)
            return entry is not None and entry.token == token
