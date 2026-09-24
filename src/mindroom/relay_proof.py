"""Runtime authorship proofs for relayed requester identity on Matrix events.

Ingress promotes ``com.mindroom.original_sender`` to the turn requester when a
managed MindRoom account relays a human request: voice transcripts, scheduled
fires, hook dispatches, external triggers, and router handoffs. Those same
accounts also deliver model-authored content, so the transport sender alone
never proves that runtime code -- rather than a tool call the model drove --
chose the relayed identity.

Every runtime relay stamps a keyed proof over the identity claim it authored,
and ingress refuses an ``original_sender`` whose proof is missing or wrong. The
key lives only in the install's storage root and is never rendered into a
prompt or an event, so content a model composes cannot carry a proof for an
identity the runtime did not choose.

The proof covers the identity claim, not the event body: it shows runtime code
authored *this* ``(original_sender, source_kind)`` pair, which is exactly what
the trust decision reads.
"""

from __future__ import annotations

import hmac
import os
import secrets
from contextlib import suppress
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.constants import ORIGINAL_SENDER_KEY, RELAY_PROOF_KEY, SOURCE_KIND_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_SIGNING_KEY_FILENAME = "relay_signing_key"
_SIGNING_KEY_BYTES = 32
_signing_keys: dict[str, bytes] = {}


def sign_relay_metadata(content: MutableMapping[str, Any], runtime_paths: RuntimePaths) -> None:
    """Stamp this runtime's authorship proof over the identity claim in *content*.

    Call this at the point runtime code decides the relayed identity, after the
    final ``original_sender`` and ``source_kind`` values are in place.
    """
    original_sender = content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        content[RELAY_PROOF_KEY] = _relay_proof(original_sender, content.get(SOURCE_KIND_KEY), runtime_paths)
    else:
        content.pop(RELAY_PROOF_KEY, None)


def relay_metadata_is_runtime_authored(content: Mapping[str, Any], runtime_paths: RuntimePaths) -> bool:
    """Return whether the identity claim in *content* carries this runtime's proof.

    Content without an ``original_sender`` claims no identity, so it needs no
    proof.
    """
    original_sender = content.get(ORIGINAL_SENDER_KEY)
    if not isinstance(original_sender, str) or not original_sender:
        return True
    proof = content.get(RELAY_PROOF_KEY)
    if not isinstance(proof, str):
        return False
    return hmac.compare_digest(
        proof,
        _relay_proof(original_sender, content.get(SOURCE_KIND_KEY), runtime_paths),
    )


def _relay_proof(original_sender: str, source_kind: object, runtime_paths: RuntimePaths) -> str:
    """Return the keyed proof for one relayed identity claim."""
    claim = "\x00".join((original_sender, source_kind if isinstance(source_kind, str) else ""))
    return hmac.new(_signing_key(runtime_paths), claim.encode(), sha256).hexdigest()


def _signing_key(runtime_paths: RuntimePaths) -> bytes:
    """Return the install's relay signing key, reading it from disk once."""
    storage_root = runtime_paths.storage_root
    cache_key = str(storage_root)
    key = _signing_keys.get(cache_key)
    if key is None:
        key = _load_or_create_signing_key(storage_root / _SIGNING_KEY_FILENAME)
        _signing_keys[cache_key] = key
    return key


def _load_or_create_signing_key(path: Path) -> bytes:
    """Return the persisted signing key, generating it exactly once per install."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temp_path.write_bytes(secrets.token_bytes(_SIGNING_KEY_BYTES))
        temp_path.chmod(0o600)
        # A concurrent runtime may publish the key first; whichever lands wins.
        with suppress(FileExistsError):
            os.link(temp_path, path)
        temp_path.unlink()
    key = path.read_bytes()
    if len(key) != _SIGNING_KEY_BYTES:
        msg = f"Relay signing key at {path} is corrupt; remove it so a new key is generated"
        raise RuntimeError(msg)
    return key
