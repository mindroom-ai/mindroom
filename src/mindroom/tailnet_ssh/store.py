"""Primary-only storage of tailnet SSH links and each person's SSH public keys in the tracking directory."""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.config.tailnet_ssh import normalize_public_key
from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths

MAX_KEYS = 20
HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class Link(BaseModel):
    """One person's SSH node for one agent's worker; ``suspended`` is computed by the link service, never stored."""

    model_config = ConfigDict(extra="forbid")

    id: str
    requester_id: str
    agent_name: str
    worker_key: str
    hostname: str
    desired: Literal["on", "off", "removing"]
    host_key: str
    host_public_key: str
    enrolled_login: str | None
    closed_before: float
    login_after: float
    created_at: float
    enrolled_at: float | None
    park_when_removed: bool = False


class LinkBook(BaseModel):
    """Every link, and each person's SSH public key lines keyed by canonical Matrix ID."""

    model_config = ConfigDict(extra="forbid")

    links: list[Link] = Field(default_factory=list)
    keys: dict[str, list[str]] = Field(default_factory=dict)

    def link(self, requester_id: str, agent_name: str) -> Link | None:
        """Return the person's link to the agent, of which there is at most one."""
        return next(
            (link for link in self.links if link.requester_id == requester_id and link.agent_name == agent_name),
            None,
        )

    def by_id(self, link_id: str) -> Link | None:
        """Return the link with this id."""
        return next((link for link in self.links if link.id == link_id), None)


def default_hostname(agent_name: str) -> str:
    """Return the agent name as one DNS label: lowercase, each run of ``_`` as one ``-``, at most 63 characters."""
    label = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
    return label[:63].rstrip("-")


def new_link(requester_id: str, agent_name: str, worker_key: str, hostname: str, *, now: float) -> Link:
    """Return a link that is on, with a fresh Ed25519 host key so ``known_hosts`` survives worker restarts."""
    host_key = Ed25519PrivateKey.generate()
    return Link(
        id=secrets.token_hex(8),
        requester_id=requester_id,
        agent_name=agent_name,
        worker_key=worker_key,
        hostname=hostname,
        desired="on",
        host_key=host_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode(),
        host_public_key=host_key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode(),
        enrolled_login=None,
        closed_before=now,
        login_after=now,
        created_at=now,
        enrolled_at=None,
    )


def _key_identity(line: str) -> str:
    """Return a normalized key line without its comment, so renaming a key neither duplicates nor removes it."""
    return " ".join(line.split()[:2])


def set_keys(book: LinkBook, requester_id: str, lines: Sequence[str], *, now: float) -> None:
    """Replace the person's keys with the pasted lines, or raise ``ValueError`` with a sentence and change nothing.

    A stored key that is gone sets ``closed_before`` on the person's links, which closes their open sessions.
    """
    keys: dict[str, str] = {}
    for entry in lines:
        for line in entry.splitlines():
            if line.strip():
                key = normalize_public_key(line)
                keys.setdefault(_key_identity(key), key)
    if len(keys) > MAX_KEYS:
        msg = f"At most {MAX_KEYS} SSH keys are allowed per person; {len(keys)} were given."
        raise ValueError(msg)
    if any(_key_identity(line) not in keys for line in book.keys.get(requester_id, [])):
        for link in book.links:
            if link.requester_id == requester_id:
                link.closed_before = now
    if keys:
        book.keys[requester_id] = list(keys.values())
    else:
        book.keys.pop(requester_id, None)


def remove_links(book: LinkBook, where: Callable[[Link], bool], *, now: float, park: bool = False) -> list[Link]:
    """Mark the matching links ``removing`` and close their sessions, and return them; the only path to ``removing``.

    A link already being removed is left as it is, so a condition checked on every reconcile changes nothing twice.
    With ``park``, ``finish_removal`` keeps the link as ``off`` instead of dropping it.
    """
    removed = [link for link in book.links if link.desired != "removing" and where(link)]
    for link in removed:
        link.desired = "removing"
        link.closed_before = now
        link.park_when_removed = park
    return removed


def finish_removal(book: LinkBook, link_id: str) -> None:
    """Drop a ``removing`` link the gateway has logged out, or park it as ``off`` without its enrolment."""
    link = book.by_id(link_id)
    if link is None or link.desired != "removing":
        return
    if link.park_when_removed:
        link.desired = "off"
        link.enrolled_login = None
        link.enrolled_at = None
    else:
        book.links.remove(link)


def _links_path(runtime_paths: RuntimePaths) -> Path:
    """Return the primary-only file of links and keys, which no worker mounts."""
    return tracking_dir(runtime_paths) / "tailnet_ssh.json"


def _parse(path: Path, text: str) -> LinkBook:
    """Return the stored book, or raise ``ValueError`` with the path and the error types only.

    Pydantic's message, its error locations, and the chained error quote the input, which holds private host keys.
    """
    try:
        return LinkBook.model_validate_json(text)
    except ValidationError as exc:  # Invalid JSON is a ValidationError too.
        errors = exc.errors()
    types = ", ".join(error["type"] for error in errors)
    msg = f"Tailnet SSH file {path} is invalid: {len(errors)} validation errors ({types})"
    raise ValueError(msg)


def read_links(runtime_paths: RuntimePaths) -> LinkBook:
    """Return the stored book, or an empty one before the first link or key."""
    path = _links_path(runtime_paths)
    try:
        return _parse(path, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LinkBook()


def update_links[T](runtime_paths: RuntimePaths, change: Callable[[LinkBook], T]) -> T:
    """Apply one change to the stored book under the file lock, store the result, and return what the change returns.

    A change that raises stores nothing.
    """
    path = _links_path(runtime_paths)
    with advisory_file_lock(tracking_dir(runtime_paths) / "tailnet_ssh.lock"):
        try:
            book = _parse(path, path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            book = LinkBook()
        result = change(book)
        write_json_file_durable(path, book.model_dump(mode="json"), strict_atomic_replace=True)
    return result
