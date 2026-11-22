"""Pending secret requests, the card content they post, and the temporary secrets a requester gave a conversation.

An agent asks for a key or a temporary secret with a card in the thread; the requester answers through the backend
route. The registry of pending requests holds only metadata and a future for the outcome, never a value. A temporary
secret's value lives in the store below, in memory or, when the requester chose so, also in one private file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.egress_broker import EgressService
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

logger = get_logger(__name__)

SECRET_REQUEST_CONTENT_KEY = "io.mindroom.secret_request"  # noqa: S105 - Matrix content key, not a secret
SECRET_REQUEST_TTL_SECONDS = 15 * 60

type SecretRequestOutcome = Literal["saved", "declined", "expired"]
type SecretRequestStatus = Literal["pending", "saved", "declined", "expired"]
# Where the requester lets MindRoom keep a temporary secret, which sets how long: the primary's memory, or also a file.
type SecretKeep = Literal["memory", "disk"]
TEMPORARY_SECRET_KEEP_SECONDS: dict[SecretKeep, int] = {"memory": 15 * 60, "disk": 24 * 60 * 60}
_TEMPORARY_SECRETS_DIRECTORY = "temporary_secrets"


@dataclass(frozen=True)
class KeyTarget:
    """An answer becomes the agent's Agent Keys key for `service` in `worker_target`'s store."""

    service: str
    worker_target: ResolvedWorkerTarget
    # The service's hosts the card shows, which the key may only ever be sent to.
    hosts: tuple[str, ...]
    # The service the agent proposed, which an answer adds as the requester's own before it stores the key; None
    # for a service that exists.
    new_service: EgressService | None = None


@dataclass(frozen=True)
class TemporaryTarget:
    """An answer becomes the variable `name` of `holder`'s shell commands, for as long as the requester keeps it."""

    name: str
    holder: _SecretHolder


@dataclass
class PendingSecretRequest:
    """One open request for a secret, awaiting the requester's answer."""

    request_id: str
    room_id: str
    requester_id: str
    agent_name: str
    target: KeyTarget | TemporaryTarget
    expires_at: float
    outcome: asyncio.Future[SecretRequestOutcome]
    # How the requester keeps a temporary secret, set by the answer that gave it.
    kept: SecretKeep | None = None
    claimed: bool = False
    # Set while no answer is being saved, so the waiting tool stops waiting for one whose save failed.
    claim_released: asyncio.Event = field(default_factory=asyncio.Event)


class _SecretRequestRegistry:
    """Pending requests by ID; every method is synchronous on the event loop."""

    def __init__(self) -> None:
        self._requests: dict[str, PendingSecretRequest] = {}

    def register(
        self,
        *,
        room_id: str,
        requester_id: str,
        agent_name: str,
        target: KeyTarget | TemporaryTarget,
        now: float,
    ) -> PendingSecretRequest:
        """Create and store a pending request that expires after the TTL."""
        request = PendingSecretRequest(
            request_id=secrets.token_urlsafe(24),
            room_id=room_id,
            requester_id=requester_id,
            agent_name=agent_name,
            target=target,
            expires_at=now + SECRET_REQUEST_TTL_SECONDS,
            outcome=asyncio.get_running_loop().create_future(),
        )
        self._requests[request.request_id] = request
        return request

    def claim(self, request_id: str, *, room_id: str, now: float) -> PendingSecretRequest | None:
        """Take the request for answering, or None when it cannot be answered now."""
        request = self._requests.get(request_id)
        if (
            request is None
            or request.room_id != room_id
            or request.claimed
            or request.outcome.done()
            or now >= request.expires_at
        ):
            return None
        request.claimed = True
        request.claim_released.clear()
        return request

    def release(self, request_id: str) -> None:
        """Give a claim back so a failed save can be retried."""
        request = self._requests.get(request_id)
        if request is not None:
            request.claimed = False
            request.claim_released.set()

    def resolve(self, request_id: str, outcome: SecretRequestOutcome) -> None:
        """Set the outcome unless the request is unknown or already done."""
        request = self._requests.get(request_id)
        if request is not None and not request.outcome.done():
            request.outcome.set_result(outcome)

    def discard(self, request_id: str) -> None:
        """Forget the request."""
        self._requests.pop(request_id, None)


_REGISTRY = _SecretRequestRegistry()


def secret_request_registry() -> _SecretRequestRegistry:
    """Return the process-wide registry."""
    return _REGISTRY


def secret_request_hosts(service: EgressService) -> tuple[str, ...]:
    """Return the hosts a key for `service` is sent to, as a card shows them and an answer must still find them."""
    return tuple(sorted({rule.host for rule in service.rules}))


def _new_service_content(name: str, service: EgressService) -> dict[str, object]:
    """Return what a card shows of a proposed service for the requester to decide: where each rule sends the key, how.

    Each rule's port only when it names one, and of its auth the header or parameter name or the basic username, never
    a template; the placeholders' names and values, which are never secrets.
    """
    rules: list[dict[str, object]] = []
    for rule in service.rules:
        shown: dict[str, object] = {"host": rule.host, "path_prefix": rule.path_prefix}
        if rule.port is not None:
            shown["port"] = rule.port
        if rule.auth is not None:
            named = {"name": rule.auth.name, "username": rule.auth.username}
            shown["auth"] = {"type": rule.auth.type} | {key: text for key, text in named.items() if text is not None}
        rules.append(shown)
    return {
        "name": name,
        "display_name": service.display_name or name,
        "rules": rules,
        "placeholder_env": dict(service.placeholder_env),
    }


def _where_to_get_it(url: str) -> str:
    """Return what a pending card's body says of the page the agent named, for a client that shows no button."""
    return f" Where to get it: {url}" if url else ""


def secret_request_body(
    status: SecretRequestStatus,
    *,
    agent_display_name: str,
    display_name: str,
    url: str = "",
) -> str:
    """Return the plain-text fallback body for a card in `status`; a pending one names the page that gives the key."""
    if status == "pending":
        asks = f"🔑 {agent_display_name} asks for your {display_name} key. Open MindRoom Chat to enter it."
        return asks + _where_to_get_it(url)
    if status == "saved":
        return f"🔑 {display_name} key saved."
    if status == "declined":
        return f"🔑 {display_name} key request declined."
    return f"🔑 {display_name} key request expired."


def temporary_secret_body(
    status: SecretRequestStatus,
    *,
    agent_display_name: str,
    name: str,
    label: str = "",
    url: str = "",
) -> str:
    """Return the plain-text fallback body for a temporary secret's card in `status`.

    A pending one says what the agent calls the secret and names the page that gives it.
    """
    if status == "pending":
        asked = f"{label}, {name}" if label else name
        asks = f"🔑 {agent_display_name} asks for a secret ({asked}). Open MindRoom Chat to enter it."
        return asks + _where_to_get_it(url)
    if status == "saved":
        return "🔑 Secret received."
    if status == "declined":
        return "🔑 Secret request declined."
    return "🔑 Secret request expired."


def secret_request_content(
    request: PendingSecretRequest,
    *,
    display_name: str,
    reason: str,
    status: SecretRequestStatus,
    replaces: bool = False,
    label: str = "",
    url: str = "",
    placeholder: str = "",
    response_event_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, object]:
    """Return the `io.mindroom.secret_request` object: metadata only, never a value.

    A key card says `replaces` when saving would replace a key the service already has in that store, and shows the
    service the agent proposed as `new_service`, which saving adds.

    A temporary secret's card has no service fields, so a client that knows only key cards reads none in it; it
    names the variable, the keeps the requester chooses from with their lifetimes in seconds, and, once answered, the
    keep chosen.

    What the agent chose to show is there only when given: the `url` of the page that gives the secret, the
    `placeholder` of the empty field, and a temporary secret's `label` (a key card is named after its service).

    A card asked by a reply's tool call names the reply's event (`response_event_id`) and the call (`tool_call_id`), so
    a client can draw the card at the call's line; it names neither when the call was not made by a reply that shows
    an event.
    """
    content: dict[str, object] = {"request_id": request.request_id, "requester_id": request.requester_id}
    target = request.target
    if isinstance(target, TemporaryTarget):
        content["env"] = target.name
    else:
        content |= {"service": target.service, "display_name": display_name, "hosts": list(target.hosts)}
        if target.new_service is not None:
            content["new_service"] = _new_service_content(target.service, target.new_service)
    content |= {"reason": reason, "status": status, "expires_at": int(request.expires_at * 1000)}
    if replaces:
        content["replaces"] = True
    shown = {"label": label if isinstance(target, TemporaryTarget) else "", "url": url, "placeholder": placeholder}
    content |= {key: text for key, text in shown.items() if text}
    if response_event_id is not None and tool_call_id is not None:
        content |= {"response_event_id": response_event_id, "tool_call_id": tool_call_id}
    if isinstance(target, TemporaryTarget):
        content["keep"] = dict(TEMPORARY_SECRET_KEEP_SECONDS)
        if request.kept is not None:
            content["kept"] = request.kept
    return content


def secret_request_log_fields(target: KeyTarget | TemporaryTarget) -> dict[str, str]:
    """Return what a log line says a request asks for: a service's key, or a variable."""
    return {"variable": target.name} if isinstance(target, TemporaryTarget) else {"service": target.service}


@dataclass(frozen=True)
class _SecretHolder:
    """The conversation a temporary secret was given to: one agent's turns for one requester in one room and thread."""

    agent_name: str
    room_id: str
    thread_id: str | None
    requester_id: str


def secret_holder(identity: ToolExecutionIdentity | None) -> _SecretHolder | None:
    """Return the conversation a tool call runs in, or None for a call outside a requester's Matrix conversation."""
    if identity is None or not identity.agent_name or identity.room_id is None or identity.requester_id is None:
        return None
    return _SecretHolder(
        agent_name=identity.agent_name,
        room_id=identity.room_id,
        thread_id=identity.resolved_thread_id,
        requester_id=identity.requester_id,
    )


@dataclass(frozen=True)
class _Given:
    value: str
    expires_at: float
    # The file of a value kept on disk.
    path: Path | None


class _TemporarySecrets:
    """The values requesters gave conversations, by holder and variable name, until each expires.

    Every value is in memory; one kept on disk also has a file under the storage root, which the first complete read
    of that root reads back after a restart. Every tool call reads the store on the event loop, with
    `values_in_memory`, which touches no file and waits for none: file work runs under a lock of its own, and the
    lock that guards the values is never held across it.
    """

    def __init__(self) -> None:
        # Guards the two fields below, and is never held while a file is read, written or deleted.
        self._lock = threading.Lock()
        self._given: dict[tuple[_SecretHolder, str], _Given] = {}
        self._loaded: set[Path] = set()
        # One answer or complete read works on the files at a time.
        self._files = threading.Lock()

    def give(
        self,
        holder: _SecretHolder,
        name: str,
        value: str,
        *,
        keep: SecretKeep,
        now: float,
        storage_root: Path,
    ) -> None:
        """Keep `value` as `holder`'s variable `name` for the lifetime of `keep`, in place of an earlier value.

        Writes a kept value's file, so it runs in a thread.
        """
        directory = storage_root / _TEMPORARY_SECRETS_DIRECTORY
        expires_at = now + TEMPORARY_SECRET_KEEP_SECONDS[keep]
        with self._files:
            self._load(directory)
            self._delete_expired(now)
            with self._lock:
                earlier = self._given.get((holder, name))
            path = _write_file(directory, holder, name, value, expires_at) if keep == "disk" else None
            if earlier is not None and earlier.path is not None and earlier.path != path:
                # Before the new value counts: a file that stayed would bring the old value back after a restart,
                # so one that cannot be removed refuses the new value.
                earlier.path.unlink(missing_ok=True)
            with self._lock:
                self._given[(holder, name)] = _Given(value, expires_at, path)

    def values(self, holder: _SecretHolder, *, storage_root: Path, now: float | None = None) -> dict[str, str]:
        """Return `holder`'s live values by variable name, at `now` or else at this moment.

        The complete read: it reads the root's files at its first use and deletes the files of expired values, so it
        runs in a thread wherever an event loop runs.
        """
        now = time.time() if now is None else now
        with self._files:
            self._load(storage_root / _TEMPORARY_SECRETS_DIRECTORY)
            self._delete_expired(now)
            with self._lock:
                return {name: given.value for (owner, name), given in self._given.items() if owner == holder}

    def values_in_memory(
        self,
        holder: _SecretHolder,
        *,
        storage_root: Path,
        now: float | None = None,
    ) -> dict[str, str] | None:
        """Return `holder`'s live values without touching a file, or None when `values` has to run first.

        None before the root's files were read, and while an expired value's file waits to be deleted.
        """
        now = time.time() if now is None else now
        with self._lock:
            if storage_root / _TEMPORARY_SECRETS_DIRECTORY not in self._loaded:
                return None
            expired = [key for key, given in self._given.items() if now >= given.expires_at]
            if any(self._given[key].path is not None for key in expired):
                return None
            for key in expired:
                del self._given[key]
            return {name: given.value for (owner, name), given in self._given.items() if owner == holder}

    def _delete_expired(self, now: float) -> None:
        """Drop every expired value and delete its file; the caller holds the files' lock."""
        with self._lock:
            expired = [key for key, given in self._given.items() if now >= given.expires_at]
            paths = [given.path for key in expired if (given := self._given.pop(key)).path is not None]
        for path in paths:
            _delete_file(path)

    def _load(self, directory: Path) -> None:
        """Read `directory`'s files at its first use; the caller holds the files' lock."""
        with self._lock:
            if directory in self._loaded:
                return
        stored = _stored_values(directory)
        with self._lock:
            self._loaded.add(directory)
            for key, given in stored:
                self._given.setdefault(key, given)


def _stored_values(directory: Path) -> list[tuple[tuple[_SecretHolder, str], _Given]]:
    """Return the values `directory` holds, deleting what is none: a foreign file, or a write that never finished."""
    try:
        paths = sorted(directory.iterdir()) if directory.is_dir() else []
    except OSError as error:
        logger.warning("temporary_secrets_not_read", error_type=type(error).__name__)
        return []
    stored_values = []
    for path in paths:
        stored = _read_file(path) if path.suffix == ".json" else None
        if stored is None:
            _delete_file(path)
        else:
            stored_values.append(stored)
    return stored_values


def _delete_file(path: Path) -> None:
    """Delete a value's file; one that cannot be deleted is logged, since no read may fail over it."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("temporary_secret_file_not_deleted", error_type=type(error).__name__)


def _write_file(directory: Path, holder: _SecretHolder, name: str, value: str, expires_at: float) -> Path:
    """Write one value's private file, named after its holder and variable so a new value replaces it."""
    identity = {**asdict(holder), "name": name}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    payload = json.dumps({**identity, "value": value, "expires_at": expires_at}).encode()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_write_bytes_at(directory_fd, f"{digest}.json", payload, file_mode=0o600)
    finally:
        os.close(directory_fd)
    return directory / f"{digest}.json"


def _read_file(path: Path) -> tuple[tuple[_SecretHolder, str], _Given] | None:
    """Return the value a file holds, or None when it is not one this store wrote."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        holder = _SecretHolder(
            agent_name=stored["agent_name"],
            room_id=stored["room_id"],
            thread_id=stored["thread_id"],
            requester_id=stored["requester_id"],
        )
        key = (holder, stored["name"])
        given = _Given(stored["value"], float(stored["expires_at"]), path)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    strings = (holder.agent_name, holder.room_id, holder.requester_id, key[1], given.value)
    if not all(isinstance(item, str) for item in strings) or not isinstance(holder.thread_id, str | None):
        return None
    return key, given


_TEMPORARY_SECRETS = _TemporarySecrets()


def temporary_secrets() -> _TemporarySecrets:
    """Return the process-wide store of temporary secrets."""
    return _TEMPORARY_SECRETS
