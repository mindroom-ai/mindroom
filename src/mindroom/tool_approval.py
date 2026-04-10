"""Tool-call approval store, persistence, and rule evaluation."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.constants import RuntimePaths, resolve_config_relative_path, safe_replace
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

    from mindroom.config.main import Config

ApprovalStatus = Literal["pending", "approved", "denied", "expired"]

_APPROVALS_DIRNAME = "approvals"
_DEFAULT_RESTART_REASON = "MindRoom restarted before approval completed."
_DEFAULT_REINITIALIZE_REASON = "MindRoom reinitialized before approval completed."
_DEFAULT_SHUTDOWN_REASON = "MindRoom shut down before approval completed."
_DEFAULT_TIMEOUT_REASON = "Tool approval request timed out."
_STORE: ApprovalStore | None = None
_SCRIPT_CACHE: dict[tuple[str, int], ModuleType] = {}
logger = get_logger(__name__)


class ToolApprovalScriptError(RuntimeError):
    """One approval-script load or execution failure."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _approval_request_from_payload(payload: object) -> ApprovalRequest:
    if not isinstance(payload, dict):
        msg = "approval file does not contain a JSON object"
        raise TypeError(msg)
    return ApprovalRequest.from_dict(cast("dict[str, Any]", payload))


def _set_future_result_if_pending(
    future: asyncio.Future[str],
    status: str,
) -> None:
    if not future.done():
        future.set_result(status)


@dataclass(slots=True)
class ApprovalRequest:
    """One persisted tool-approval request."""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    channel: str | None
    tenant_id: str | None
    account_id: str | None
    matched_rule: str
    script_path: str | None
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus = "pending"
    resolution_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    _future: asyncio.Future[str] | None = field(default=None, repr=False, compare=False)
    _future_loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted/API-visible request payload."""
        return {
            "id": self.id,
            "status": self.status,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "agent_name": self.agent_name,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "requester_id": self.requester_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "tenant_id": self.tenant_id,
            "account_id": self.account_id,
            "matched_rule": self.matched_rule,
            "script_path": self.script_path,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "resolution_reason": self.resolution_reason,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at is not None else None,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ApprovalRequest:
        """Rebuild one persisted request without runtime-only wait state."""
        return cls(
            id=cast("str", payload["id"]),
            tool_name=cast("str", payload["tool_name"]),
            arguments=cast("dict[str, Any]", payload["arguments"]),
            agent_name=cast("str", payload["agent_name"]),
            room_id=cast("str | None", payload.get("room_id")),
            thread_id=cast("str | None", payload.get("thread_id")),
            requester_id=cast("str | None", payload.get("requester_id")),
            session_id=cast("str | None", payload.get("session_id")),
            channel=cast("str | None", payload.get("channel")),
            tenant_id=cast("str | None", payload.get("tenant_id")),
            account_id=cast("str | None", payload.get("account_id")),
            matched_rule=cast("str", payload["matched_rule"]),
            script_path=cast("str | None", payload.get("script_path")),
            created_at=cast("datetime", _parse_datetime(cast("str", payload["created_at"]))),
            expires_at=cast("datetime", _parse_datetime(cast("str", payload["expires_at"]))),
            status=cast("ApprovalStatus", payload["status"]),
            resolution_reason=cast("str | None", payload.get("resolution_reason")),
            resolved_at=_parse_datetime(cast("str | None", payload.get("resolved_at"))),
            resolved_by=cast("str | None", payload.get("resolved_by")),
        )


@dataclass(slots=True)
class ApprovalSubscription:
    """One live approval subscription with batched cross-thread wakeups."""

    loop: asyncio.AbstractEventLoop
    _items: deque[dict[str, Any]] = field(default_factory=deque, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _notifier: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _scheduled: bool = field(default=False, repr=False)
    _active: bool = field(default=True, repr=False)

    def push(
        self,
        event: dict[str, Any],
        *,
        current_loop: asyncio.AbstractEventLoop | None,
    ) -> bool:
        """Append one event and schedule at most one wakeup while items remain queued."""
        should_notify = False
        with self._state_lock:
            if not self._active:
                return True
            self._items.append(event)
            if not self._scheduled:
                self._scheduled = True
                should_notify = True
        if not should_notify:
            return True
        try:
            if current_loop is self.loop:
                self._notifier.set()
            else:
                self.loop.call_soon_threadsafe(self._notifier.set)
        except RuntimeError:
            self.close()
            return False
        return True

    async def get(self) -> dict[str, Any]:
        """Wait for the next approval event."""
        while True:
            with self._state_lock:
                if self._items:
                    item = self._items.popleft()
                    if not self._items:
                        self._scheduled = False
                        self._notifier.clear()
                    return item
                self._scheduled = False
                self._notifier.clear()
            await self._notifier.wait()

    def close(self) -> None:
        """Deactivate one subscription and drop queued events."""
        with self._state_lock:
            self._active = False
            self._items.clear()
            self._scheduled = False
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._notifier.set)
            except RuntimeError:
                return


class ApprovalStore:
    """In-memory store with per-request JSON persistence and live subscribers."""

    def __init__(self, storage_dir: Path) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._subscribers: dict[int, ApprovalSubscription] = {}
        self._state_lock = threading.Lock()
        self._subscriber_lock = threading.Lock()
        self._resolve_lock = asyncio.Lock()
        self._storage_dir = storage_dir
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    @property
    def storage_dir(self) -> Path:
        """Return the approvals storage directory."""
        return self._storage_dir

    def _request_path(self, request_id: str) -> Path:
        return self._storage_dir / f"{request_id}.json"

    def _persist_request(self, request: ApprovalRequest) -> None:
        target_path = self._request_path(request.id)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._storage_dir,
            prefix=f"{request.id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(request.to_dict(), handle, sort_keys=True)
            handle.write("\n")
            tmp_path = Path(handle.name)
        safe_replace(tmp_path, target_path)

    def _resolve_request_state(
        self,
        request_id: str,
        *,
        status: Literal["approved", "denied", "expired"],
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> tuple[ApprovalRequest, asyncio.Future[str] | None, asyncio.AbstractEventLoop | None]:
        with self._state_lock:
            request = self._requests.get(request_id)
            if request is None:
                msg = f"Approval request '{request_id}' was not found."
                raise LookupError(msg)
            if request.status != "pending":
                msg = f"Approval request '{request_id}' is already {request.status}."
                raise ValueError(msg)

            request.status = status
            request.resolution_reason = reason
            request.resolved_at = _utcnow()
            request.resolved_by = resolved_by
            return request, request._future, request._future_loop

    def _finish_resolution(
        self,
        request: ApprovalRequest,
        *,
        status: Literal["approved", "denied", "expired"],
        future: asyncio.Future[str] | None,
        future_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._persist_request(request)
        self._broadcast("updated", request)

        if future is not None and future_loop is not None and not future.done():
            try:
                future_loop.call_soon_threadsafe(_set_future_result_if_pending, future, status)
            except RuntimeError:
                logger.debug(
                    "Approval future loop already closed",
                    request_id=request.id,
                    status=status,
                )

    def _broadcast(self, event_type: Literal["created", "updated"], request: ApprovalRequest) -> None:
        stale_subscribers: list[int] = []
        with self._subscriber_lock:
            subscribers = list(self._subscribers.values())
        if not subscribers:
            return

        event = {"type": event_type, "approval": request.to_dict()}
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for subscriber in subscribers:
            if subscriber.loop.is_closed():
                stale_subscribers.append(id(subscriber))
                continue
            if not subscriber.push(event, current_loop=current_loop):
                stale_subscribers.append(id(subscriber))
        if stale_subscribers:
            removed_subscribers: list[ApprovalSubscription] = []
            with self._subscriber_lock:
                for subscriber_id in stale_subscribers:
                    removed = self._subscribers.pop(subscriber_id, None)
                    if removed is not None:
                        removed_subscribers.append(removed)
            for subscriber in removed_subscribers:
                subscriber.close()

    def _expire_loaded_pending_requests(self) -> None:
        pending_requests: list[ApprovalRequest] = []
        with self._state_lock:
            for request in self._requests.values():
                if request.status != "pending":
                    continue
                request.status = "expired"
                request.resolution_reason = _DEFAULT_RESTART_REASON
                request.resolved_at = _utcnow()
                request.resolved_by = None
                pending_requests.append(request)
        for request in pending_requests:
            self._persist_request(request)

    def load_existing(self) -> None:
        """Load persisted request records and expire orphaned pending entries."""
        loaded_requests: dict[str, ApprovalRequest] = {}
        for request_path in sorted(self._storage_dir.glob("*.json")):
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                request = _approval_request_from_payload(payload)
            except Exception:
                logger.exception("Failed to load persisted approval request", path=str(request_path))
                continue
            loaded_requests[request.id] = request
        with self._state_lock:
            self._requests = loaded_requests
        self._expire_loaded_pending_requests()

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """Return one request by ID."""
        with self._state_lock:
            return self._requests.get(request_id)

    def list_pending(self) -> list[ApprovalRequest]:
        """Return pending requests sorted by creation time."""
        with self._state_lock:
            pending_requests = [request for request in self._requests.values() if request.status == "pending"]
        return sorted(pending_requests, key=lambda request: request.created_at)

    def list_pending_records(self) -> list[dict[str, Any]]:
        """Return pending approval records for API responses."""
        with self._state_lock:
            pending_requests = sorted(
                (request for request in self._requests.values() if request.status == "pending"),
                key=lambda request: request.created_at,
            )
        return [request.to_dict() for request in pending_requests]

    def subscribe(self) -> ApprovalSubscription:
        """Register one live subscriber queue on the current event loop."""
        subscription = ApprovalSubscription(loop=asyncio.get_running_loop())
        with self._subscriber_lock:
            self._subscribers[id(subscription)] = subscription
        return subscription

    def unsubscribe(self, subscription: ApprovalSubscription) -> None:
        """Remove one subscriber queue."""
        removed: ApprovalSubscription | None = None
        with self._subscriber_lock:
            removed = self._subscribers.pop(id(subscription), None)
        if removed is not None:
            removed.close()

    def clear_subscribers(self) -> None:
        """Drop every live subscriber."""
        subscribers: list[ApprovalSubscription] = []
        with self._subscriber_lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.close()

    def expire_pending_requests(self, *, reason: str) -> None:
        """Expire every pending request and wake any live waiters."""
        pending_request_ids = [request.id for request in self.list_pending()]
        for request_id in pending_request_ids:
            try:
                request, future, future_loop = self._resolve_request_state(
                    request_id,
                    status="expired",
                    reason=reason,
                )
            except (LookupError, ValueError):
                continue
            self._finish_resolution(
                request,
                status="expired",
                future=future,
                future_loop=future_loop,
            )

    async def create_request(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        agent_name: str,
        room_id: str | None,
        thread_id: str | None,
        requester_id: str | None,
        session_id: str | None,
        channel: str | None,
        tenant_id: str | None,
        account_id: str | None,
        matched_rule: str,
        script_path: str | None,
        timeout_seconds: float,
    ) -> ApprovalRequest:
        """Create, persist, and broadcast one pending approval request."""
        created_at = _utcnow()
        request = ApprovalRequest(
            id=uuid4().hex,
            tool_name=tool_name,
            arguments=arguments,
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            requester_id=requester_id,
            session_id=session_id,
            channel=channel,
            tenant_id=tenant_id,
            account_id=account_id,
            matched_rule=matched_rule,
            script_path=script_path,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=timeout_seconds),
        )
        request._future_loop = asyncio.get_running_loop()
        request._future = request._future_loop.create_future()
        with self._state_lock:
            self._requests[request.id] = request
        self._persist_request(request)
        self._broadcast("created", request)
        return request

    async def resolve(
        self,
        request_id: str,
        *,
        status: Literal["approved", "denied", "expired"],
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> ApprovalRequest:
        """Resolve one pending request exactly once."""
        async with self._resolve_lock:
            request, future, future_loop = self._resolve_request_state(
                request_id,
                status=status,
                reason=reason,
                resolved_by=resolved_by,
            )
        self._finish_resolution(
            request,
            status=status,
            future=future,
            future_loop=future_loop,
        )
        return request

    async def approve(
        self,
        request_id: str,
        *,
        resolved_by: str | None = None,
    ) -> ApprovalRequest:
        """Approve one pending request."""
        return await self.resolve(request_id, status="approved", resolved_by=resolved_by)

    async def deny(
        self,
        request_id: str,
        *,
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> ApprovalRequest:
        """Deny one pending request."""
        return await self.resolve(request_id, status="denied", reason=reason, resolved_by=resolved_by)

    async def expire(
        self,
        request_id: str,
        *,
        reason: str | None = None,
    ) -> ApprovalRequest:
        """Expire one pending request."""
        return await self.resolve(request_id, status="expired", reason=reason)


def _check_callable_from_module(
    module: ModuleType,
    resolved_path: Path,
) -> Callable[[str, dict[str, Any], str], bool] | Callable[[str, dict[str, Any], str], Awaitable[bool]]:
    check = getattr(module, "check", None)
    if not callable(check):
        msg = f"Approval script '{resolved_path}' must define callable check(tool_name, arguments, agent_name)."
        raise ToolApprovalScriptError(msg)
    return cast(
        "Callable[[str, dict[str, Any], str], bool] | Callable[[str, dict[str, Any], str], Awaitable[bool]]",
        check,
    )


def _load_script_module(
    script: str,
    runtime_paths: RuntimePaths,
) -> tuple[ModuleType, Path]:
    resolved_path = resolve_config_relative_path(script, runtime_paths)
    if not resolved_path.is_file():
        msg = f"Approval script '{resolved_path}' was not found."
        raise ToolApprovalScriptError(msg)

    mtime_ns = resolved_path.stat().st_mtime_ns
    cache_key = (str(resolved_path), mtime_ns)
    cached_module = _SCRIPT_CACHE.get(cache_key)
    if cached_module is not None:
        return cached_module, resolved_path

    spec = importlib.util.spec_from_file_location(f"mindroom_tool_approval_{uuid4().hex}", resolved_path)
    if spec is None or spec.loader is None:
        msg = f"Approval script '{resolved_path}' could not be loaded."
        raise ToolApprovalScriptError(msg)

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"Approval script '{resolved_path}' failed to import: {exc!s}"
        raise ToolApprovalScriptError(msg) from exc

    stale_keys = [key for key in _SCRIPT_CACHE if key[0] == str(resolved_path) and key != cache_key]
    for stale_key in stale_keys:
        _SCRIPT_CACHE.pop(stale_key, None)
    _SCRIPT_CACHE[cache_key] = module
    return module, resolved_path


async def evaluate_tool_approval(
    config: Config,
    runtime_paths: RuntimePaths,
    tool_name: str,
    arguments: dict[str, Any],
    agent_name: str,
) -> tuple[bool, str, str | None, float]:
    """Return the approval decision for one tool call."""
    approval_config = config.tool_approval
    require_approval = approval_config.default == "require_approval"
    matched_rule = "<default>"
    script_path: str | None = None
    timeout_seconds = approval_config.timeout_days * 24 * 60 * 60

    for rule in approval_config.rules:
        if not fnmatchcase(tool_name, rule.match):
            continue
        matched_rule = rule.match
        if rule.timeout_days is not None:
            timeout_seconds = rule.timeout_days * 24 * 60 * 60
        if rule.action is not None:
            return rule.action == "require_approval", matched_rule, None, timeout_seconds

        assert rule.script is not None
        module, resolved_path = _load_script_module(rule.script, runtime_paths)
        script_path = str(resolved_path)
        check = _check_callable_from_module(module, resolved_path)
        try:
            result = check(tool_name, arguments, agent_name)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            msg = f"Approval script '{resolved_path}' failed: {exc!s}"
            raise ToolApprovalScriptError(msg) from exc
        if not isinstance(result, bool):
            msg = f"Approval script '{resolved_path}' returned a non-bool result."
            raise ToolApprovalScriptError(msg)
        return result, matched_rule, script_path, timeout_seconds

    return require_approval, matched_rule, script_path, timeout_seconds


def get_approval_store() -> ApprovalStore | None:
    """Return the module-level approval store when initialized."""
    return _STORE


def initialize_approval_store(runtime_paths: RuntimePaths) -> ApprovalStore:
    """Initialize the module-level approval store for one runtime context."""
    global _STORE

    storage_dir = runtime_paths.storage_root / _APPROVALS_DIRNAME
    if _STORE is not None and _STORE.storage_dir == storage_dir:
        return _STORE
    if _STORE is not None:
        _STORE.expire_pending_requests(reason=_DEFAULT_REINITIALIZE_REASON)
        _STORE.clear_subscribers()

    store = ApprovalStore(storage_dir)
    store.load_existing()
    _STORE = store
    return store


async def shutdown_approval_store(
    reason: str = _DEFAULT_SHUTDOWN_REASON,
) -> None:
    """Expire pending approvals, clear subscribers, and drop the module-level store."""
    global _STORE

    store = _STORE
    if store is None:
        _SCRIPT_CACHE.clear()
        return

    store.expire_pending_requests(reason=reason)
    store.clear_subscribers()
    _STORE = None
    _SCRIPT_CACHE.clear()
