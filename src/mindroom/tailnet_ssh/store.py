"""The primary-only tailnet SSH link ledger."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


class Link(BaseModel):
    """One SSH node for a worker key."""

    model_config = ConfigDict(extra="forbid")

    id: str
    worker_key: str
    hostname: str
    host_key: str


class LinkBook(BaseModel):
    """Persisted identities for current and previously listed workers."""

    model_config = ConfigDict(extra="forbid")

    links: list[Link] = Field(default_factory=list)

    def by_id(self, link_id: str) -> Link | None:
        """Return the link with this id."""
        return next((link for link in self.links if link.id == link_id), None)

    def ensure(self, worker_key: str) -> Link:
        """Keep the existing identity, or allocate it the first time a worker is listed."""
        link_id = hashlib.sha256(worker_key.encode()).hexdigest()[:16]
        if (link := self.by_id(link_id)) is not None:
            if link.worker_key != worker_key:
                msg = "Tailnet SSH link ID collision."
                raise ValueError(msg)
            return link
        reserved = {item.hostname for item in self.links}
        hostname = re.sub(r"[^a-z0-9]+", "-", worker_key.lower()).strip("-") or "worker"
        if len(hostname) > 63 or hostname in reserved:
            hostname = f"{hostname[:46].rstrip('-')}-{link_id}"
            if hostname in reserved:
                msg = "Tailnet SSH hostname collision."
                raise ValueError(msg)
        link = Link(
            id=link_id,
            worker_key=worker_key,
            hostname=hostname,
            host_key=Ed25519PrivateKey.generate()
            .private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
            .decode(),
        )
        self.links.append(link)
        return link


def _links_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / "tailnet_ssh.json"


def read_links(runtime_paths: RuntimePaths) -> LinkBook:
    """Return the stored book, or an empty one before the first link."""
    path = _links_path(runtime_paths)
    try:
        return LinkBook.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LinkBook()
    except ValidationError as exc:
        errors = exc.errors()
    # Validation locations, values, and the chained exception can expose private host keys.
    types = ", ".join(error["type"] for error in errors)
    msg = f"Tailnet SSH file {path} is invalid: {len(errors)} validation errors ({types})"
    raise ValueError(msg)


@contextmanager
def update_links(runtime_paths: RuntimePaths) -> Iterator[LinkBook]:
    """Hold the ledger lock and write back changes only on success."""
    path = _links_path(runtime_paths)
    with advisory_file_lock(path.with_suffix(".lock")):
        book = read_links(runtime_paths)
        stored = book.model_dump(mode="json")
        yield book
        changed = book.model_dump(mode="json")
        if changed != stored:
            write_json_file_durable(path, changed, strict_atomic_replace=True)
