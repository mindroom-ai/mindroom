"""The worker wire models change only together with the worker protocol version."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from mindroom.workers import protocol

# The digest of the wire models' schemas at each worker protocol.
# A field added, removed, renamed or retyped changes it: bump `WORKER_PROTOCOL_VERSION` with a note in
# `workers/protocol.py` and record the new version's digest here, because a worker on the same protocol refuses
# fields it does not know. Record a new digest for the same version only when nothing a request or response carries
# changed, such as a pydantic upgrade that renders the same schema differently.
_SCHEMA_DIGESTS = {6: "a0c3b906f1e6f4ceede7f0d26dc8b4a20b6c1387228db5f8052f68026ed3b7e5"}


def _without_prose(schema: object) -> object:
    """Drop titles and descriptions, which docstring edits change without changing the wire."""
    if isinstance(schema, dict):
        return {key: _without_prose(value) for key, value in schema.items() if key not in {"title", "description"}}
    if isinstance(schema, list):
        return [_without_prose(value) for value in schema]
    return schema


def _wire_schema_digest() -> str:
    models = {
        name: value
        for name, value in vars(protocol).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == protocol.__name__
        and not name.startswith("_")
    }
    schemas = {name: _without_prose(model.model_json_schema()) for name, model in sorted(models.items())}
    return hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()


def test_the_wire_models_change_only_with_the_protocol_version() -> None:
    """A wire model change without a protocol bump would make same-protocol workers refuse every call."""
    assert _wire_schema_digest() == _SCHEMA_DIGESTS.get(protocol.WORKER_PROTOCOL_VERSION)
