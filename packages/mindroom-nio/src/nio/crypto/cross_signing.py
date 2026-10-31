"""Cross-signing identity management (mindroom-nio fork feature).

Bot-style clients have no interactive verification flow, yet MSC4153-era
clients exclude devices that are not cross-signed from encrypted rooms.
This module holds a minimal self-managed cross-signing identity: a master
key and a self-signing key whose private seeds are persisted next to the
olm store, used to sign the account's own devices.

A user-signing key is deliberately not created: bots never verify other
users, and refusing cross-user trust keeps the surface small.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from ..api import Api

if TYPE_CHECKING:
    from typing import Protocol

    class _EccConstruct(Protocol):
        def __call__(self, *, curve: str, seed: bytes) -> ECC.EccKey: ...


CROSS_SIGNING_SIDECAR_SUFFIX = "_cross_signing.json"


def _unpadded_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _b64_to_bytes(data: str) -> bytes:
    return base64.b64decode(data + "=" * (-len(data) % 4))


def _signing_key(seed: bytes) -> ECC.EccKey:
    construct = cast("_EccConstruct", ECC.construct)
    return construct(curve="Ed25519", seed=seed)


def _public_key_b64(seed: bytes) -> str:
    raw = _signing_key(seed).public_key().export_key(format="raw")
    return _unpadded_b64(raw)


def sign_json(seed: bytes, json_dict: dict[str, Any]) -> str:
    """Sign the canonical JSON of ``json_dict`` (without signatures/unsigned)."""
    unsigned_dict = {
        key: value
        for key, value in json_dict.items()
        if key not in ("signatures", "unsigned")
    }
    message = Api.to_canonical_json(unsigned_dict).encode("utf-8")
    signer = eddsa.new(_signing_key(seed), "rfc8032")
    return _unpadded_b64(signer.sign(message))


@dataclass
class CrossSigningIdentity:
    """A self-managed cross-signing identity for one Matrix account."""

    user_id: str
    master_seed: bytes
    self_signing_seed: bytes
    uploaded: bool = False
    signed_devices: list[str] = field(default_factory=list)

    @classmethod
    def generate(cls, user_id: str) -> CrossSigningIdentity:
        """Create a fresh cross-signing identity for one account."""
        return cls(
            user_id=user_id,
            master_seed=os.urandom(32),
            self_signing_seed=os.urandom(32),
        )

    @classmethod
    def load(cls, path: Path) -> CrossSigningIdentity | None:
        """Load a persisted identity, returning None only when truly absent.

        A missing sidecar returns None so callers can generate a fresh
        identity. A sidecar that exists but is unreadable or corrupt raises
        instead: silently returning None would make callers rotate the
        master/self-signing keys and invalidate existing signatures.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        payload = json.loads(raw)
        return cls(
            user_id=payload["user_id"],
            master_seed=_b64_to_bytes(payload["master_seed"]),
            self_signing_seed=_b64_to_bytes(payload["self_signing_seed"]),
            uploaded=bool(payload.get("uploaded", False)),
            signed_devices=[
                device
                for device in payload.get("signed_devices", [])
                if isinstance(device, str)
            ],
        )

    def save(self, path: Path) -> None:
        """Persist the identity, creating the file with owner-only permissions.

        The file is opened with mode 0o600 from creation so the private key
        seeds are never briefly world-readable under the process umask.
        """
        payload = {
            "user_id": self.user_id,
            "master_seed": _unpadded_b64(self.master_seed),
            "self_signing_seed": _unpadded_b64(self.self_signing_seed),
            "uploaded": self.uploaded,
            "signed_devices": self.signed_devices,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))

    @property
    def master_public_key(self) -> str:
        """Unpadded base64 public master key."""
        return _public_key_b64(self.master_seed)

    @property
    def self_signing_public_key(self) -> str:
        """Unpadded base64 public self-signing key."""
        return _public_key_b64(self.self_signing_seed)

    def master_key_payload(self) -> dict[str, Any]:
        """The master key object for /keys/device_signing/upload."""
        public = self.master_public_key
        return {
            "user_id": self.user_id,
            "usage": ["master"],
            "keys": {f"ed25519:{public}": public},
        }

    def self_signing_key_payload(self) -> dict[str, Any]:
        """The self-signing key object, signed by the master key."""
        public = self.self_signing_public_key
        payload: dict[str, Any] = {
            "user_id": self.user_id,
            "usage": ["self_signing"],
            "keys": {f"ed25519:{public}": public},
        }
        signature = sign_json(self.master_seed, payload)
        payload["signatures"] = {
            self.user_id: {f"ed25519:{self.master_public_key}": signature}
        }
        return payload

    def signed_device_payload(self, device_keys: dict[str, Any]) -> dict[str, Any]:
        """One device-keys object carrying our self-signing signature."""
        signature = sign_json(self.self_signing_seed, device_keys)
        signed = {
            key: value
            for key, value in device_keys.items()
            if key not in ("signatures", "unsigned")
        }
        signed["signatures"] = {
            self.user_id: {f"ed25519:{self.self_signing_public_key}": signature}
        }
        return signed


def _sanitize_user_id_for_filename(user_id: str) -> str:
    """Reduce a Matrix user_id to a single safe path component.

    The Matrix user_id schema (``^@.*:.+$``) does not forbid ``/`` or ``\\``, so a
    malicious homeserver could return an id with path separators and redirect the
    sidecar read/write outside the store. Collapse every separator so the result
    is always one filename component.
    """
    return user_id.replace("/", "_").replace("\\", "_").replace("\0", "_")


def cross_signing_sidecar_path(store_path: str, user_id: str) -> Path:
    """The on-disk location of one account's cross-signing identity.

    Raises ValueError if the resolved path would escape ``store_path``.
    """
    root = Path(store_path).resolve()
    path = (
        root
        / f"{_sanitize_user_id_for_filename(user_id)}{CROSS_SIGNING_SIDECAR_SUFFIX}"
    ).resolve()
    if root != path.parent:
        msg = f"Refusing cross-signing sidecar path outside the store: {user_id!r}"
        raise ValueError(msg)
    return path
