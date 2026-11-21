"""The primary-only tailnet SSH link ledger."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

    from mindroom.constants import RuntimePaths

class Link(BaseModel):
    """One person's SSH node for one agent."""

    model_config = ConfigDict(extra="forbid")

    id: str
    requester_id: str
    agent_name: str
    hostname: str
    host_key: str


class LinkBook(BaseModel):
    """Link identities and the IDs whose retirement is final."""

    model_config = ConfigDict(extra="forbid")

    links: list[Link] = Field(default_factory=list)
    retiring_ids: set[str] = Field(default_factory=set)

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
    """Return the agent name as one DNS label."""
    return re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")[:63].rstrip("-") or "worker"


def _hostname(requester_id: str, agent_name: str, reserved: Collection[str]) -> str:
    base = default_hostname(agent_name)
    if base not in reserved:
        return base
    localpart = default_hostname(requester_id.removeprefix("@").partition(":")[0])[:61].rstrip("-")
    prefix = f"{base[:62 - len(localpart)].rstrip('-')}-{localpart}"
    if prefix not in reserved:
        return prefix
    digest = hashlib.sha256(f"{requester_id}\0{agent_name}".encode()).hexdigest()
    for length in range(8, 63, 2):
        candidate = f"{prefix[:62 - length].rstrip('-')}-{digest[:length]}"
        if candidate not in reserved:
            return candidate
    msg = "Could not allocate a unique tailnet SSH hostname."
    raise ValueError(msg)


def new_link(requester_id: str, agent_name: str, reserved: Collection[str] = ()) -> Link:
    """Allocate a hostname and a fresh ID and Ed25519 host key."""
    return Link(
        id=secrets.token_hex(8),
        requester_id=requester_id,
        agent_name=agent_name,
        hostname=_hostname(requester_id, agent_name, reserved),
        host_key=Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption(),
        ).decode(),
    )


def _links_path(runtime_paths: RuntimePaths) -> Path:
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
    """Return the stored book, or an empty one before the first link."""
    path = _links_path(runtime_paths)
    try:
        return _parse(path, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LinkBook()


def update_links[T](runtime_paths: RuntimePaths, change: Callable[[LinkBook], T]) -> T:
    """Run a change under the ledger lock and write durably only if changed."""
    path = _links_path(runtime_paths)
    with advisory_file_lock(path.with_suffix(".lock")):
        book = read_links(runtime_paths)
        stored = book.model_dump(mode="json")
        result = change(book)
        changed = book.model_dump(mode="json")
        if changed != stored:
            write_json_file_durable(path, changed, strict_atomic_replace=True)
    return result
