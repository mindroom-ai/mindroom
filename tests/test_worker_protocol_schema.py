"""The worker wire models change only together with the worker protocol version."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields

import pytest
from pydantic import BaseModel, ValidationError

from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from mindroom.workers import protocol

# The digest of the wire models' schemas at each worker protocol.
# A field added, removed, renamed or retyped changes it: bump `WORKER_PROTOCOL_VERSION` with a note in
# `workers/protocol.py` and record the new version's digest here, because a worker on the same protocol refuses
# fields it does not know. Record a new digest for the same version only when nothing a request or response carries
# changed, such as a pydantic upgrade that renders the same schema differently.
_SCHEMA_DIGESTS = {
    6: "a2a88d170d9793e9e5faf89e982cf533103679060240d34b1a16a0ff866923ce",
    7: "a2a88d170d9793e9e5faf89e982cf533103679060240d34b1a16a0ff866923ce",
}


def _without_prose(schema: object) -> object:
    """Drop the title and description keywords, which docstring edits change without changing the wire.

    Keys under ``properties`` and ``$defs`` are field and model names, so a field named ``title`` stays.
    """
    if isinstance(schema, list):
        return [_without_prose(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    kept: dict[str, object] = {}
    for key, value in schema.items():
        if key in {"properties", "$defs"} and isinstance(value, dict):
            kept[key] = {name: _without_prose(child) for name, child in value.items()}
        elif key not in {"title", "description"}:
            kept[key] = _without_prose(value)
    return kept


def _wire_schema_digest() -> str:
    models = {
        name: value
        for name, value in vars(protocol).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == protocol.__name__
        and not name.startswith("_")
    }
    schemas: dict[str, object] = {
        name: _without_prose(model.model_json_schema()) for name, model in sorted(models.items())
    }
    # The execution identity crosses as its serialized dataclass, which the models' schemas show only as an object.
    schemas["execution_identity"] = [(field.name, str(field.type)) for field in fields(ToolExecutionIdentity)]
    return hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()


def test_a_field_named_like_a_schema_keyword_still_changes_the_schema() -> None:
    """Only the prose keywords are dropped, so adding a field called ``description`` is still a wire change."""

    class Plain(BaseModel):
        path: str

    class WithDescription(BaseModel):
        path: str
        description: str | None = None

    assert _without_prose(Plain.model_json_schema()) != _without_prose(WithDescription.model_json_schema())


def test_an_execution_identity_field_this_release_does_not_know_is_refused() -> None:
    """A newer primary's identity field fails the request instead of being dropped while the worker resolves it."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    route = {"protocol": protocol.WORKER_PROTOCOL_VERSION}
    protocol.WorkerRoute.model_validate({**route, "execution_identity": serialize_tool_execution_identity(identity)})

    with pytest.raises(ValidationError, match="unknown execution identity fields: org_id"):
        protocol.WorkerRoute.model_validate(
            {**route, "execution_identity": {**serialize_tool_execution_identity(identity), "org_id": "acme"}},
        )


def test_the_wire_models_change_only_with_the_protocol_version() -> None:
    """A wire model change without a protocol bump would make same-protocol workers refuse every call."""
    assert _wire_schema_digest() == _SCHEMA_DIGESTS.get(protocol.WORKER_PROTOCOL_VERSION)
