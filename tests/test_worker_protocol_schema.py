"""The worker wire models change only together with the worker protocol version."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

from mindroom.tool_system import worker_routing
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from mindroom.workers import protocol

# The digest of the wire models' schemas at each worker protocol.
# A field added, removed, renamed or retyped changes it: bump `WORKER_PROTOCOL_VERSION` with a note in
# `workers/protocol.py` and record the new version's digest here, because a worker on the same protocol refuses
# fields it does not know. Record a new digest for the same version only when nothing a request or response carries
# changed, such as a pydantic upgrade that renders the same schema differently.
# The one exception is an opt-in field listed in `_OPT_IN_FIELDS`: a body carries it only when a call asks for what
# it adds, so every other body is byte for byte what it was, and a peer that predates the field refuses only the
# calls that ask. Such a field is left out of the digest and needs no bump.
_SCHEMA_DIGESTS = {
    8: "d6320479ed4156c12809f20bf04ebe9252c180c7a7965f46a898680301c59cf3",
}
# The opt-in fields of each wire model, with the worker protocol that was current when each one was added.
_OPT_IN_FIELDS = {
    "ExecuteRequest": {"structured_result": 8},
    "ExecuteResponse": {"metadata": 8},
}
# What a call that asks for nothing new sends and gets back at protocol 8, as the release before any opt-in field
# rendered them.
_PLAIN_REQUEST = (
    '{"protocol":8,"worker_key":null,"worker_scope":null,"routing_agent_name":null,"execution_identity":null,'
    '"private_agent_names":null,"config_snapshot":null,"base_dir":null,"tool_name":"shell",'
    '"function_name":"run_shell_command","args":[],"kwargs":{"args":"echo hi"},"lease_id":null,'
    '"tool_config_overrides":{},"tool_init_overrides":{},"execution_env":{},'
    '"environment":{"set_names":[],"unset_names":[],"path_prepend":[]},"extra_env_passthrough":null,'
    '"request_id":null,"run_as_job":false}'
)
_PLAIN_ANSWERS = (
    '{"ok":true,"result":"hi","error":null,"failure_kind":null}',
    '{"ok":false,"result":null,"error":"boom","failure_kind":"tool"}',
)


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
    schemas: dict[str, object] = {}
    for name, model in sorted(models.items()):
        schema = model.model_json_schema()
        for opt_in in _OPT_IN_FIELDS.get(name, ()):
            del schema["properties"][opt_in]
        schemas[name] = _without_prose(schema)
    # The execution identity crosses as its serialized dataclass, which the models' schemas show only as an object, so
    # its fields' resolved types count, such as every value its channel takes.
    schemas["execution_identity"] = [(name, str(hint)) for name, hint in get_type_hints(ToolExecutionIdentity).items()]
    return hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()


def test_a_field_named_like_a_schema_keyword_still_changes_the_schema() -> None:
    """Only the prose keywords are dropped, so adding a field called ``description`` is still a wire change."""

    class Plain(BaseModel):
        path: str

    class WithDescription(BaseModel):
        path: str
        description: str | None = None

    assert _without_prose(Plain.model_json_schema()) != _without_prose(WithDescription.model_json_schema())


def test_a_new_execution_channel_changes_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker refuses an identity whose channel it does not know, so a new channel value is a wire change."""
    digest = _wire_schema_digest()
    channels = Literal[worker_routing._ExecutionChannel, "new"]
    monkeypatch.setattr(worker_routing, "_ExecutionChannel", channels)

    assert _wire_schema_digest() != digest


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


def test_the_opt_in_fields_are_exactly_the_listed_ones() -> None:
    """A model leaves a field out of its bodies only when this file lists it, so no field skips the digest unseen."""
    declared = {
        name: set(model.opt_in_fields)
        for name, model in vars(protocol).items()
        if isinstance(model, type) and issubclass(model, BaseModel) and getattr(model, "opt_in_fields", None)
    }

    assert declared == {name: set(listed) for name, listed in _OPT_IN_FIELDS.items()}


def test_a_plain_call_is_byte_identical_to_the_wire_before_the_opt_in_fields() -> None:
    """A call that asks for nothing new sends and gets the bodies a peer from before the opt-in fields knows."""
    request = protocol.ExecuteRequest(
        protocol=8,
        tool_name="shell",
        function_name="run_shell_command",
        kwargs={"args": "echo hi"},
    )
    answers = (
        protocol.ExecuteResponse(ok=True, result="hi"),
        protocol.ExecuteResponse(ok=False, error="boom", failure_kind="tool"),
    )

    assert request.model_dump_json() == _PLAIN_REQUEST
    assert json.dumps(request.model_dump(mode="json"), separators=(",", ":")) == _PLAIN_REQUEST
    assert tuple(answer.model_dump_json() for answer in answers) == _PLAIN_ANSWERS
    assert protocol.ExecuteRequest.model_validate_json(_PLAIN_REQUEST) == request


def test_a_call_that_opts_in_carries_its_field_which_an_older_peer_refuses() -> None:
    """Only the calls that ask for what a field adds fail on a peer that predates it, and they fail loudly."""
    asking = protocol.ExecuteRequest.model_validate_json(_PLAIN_REQUEST).model_copy(update={"structured_result": True})
    answer = protocol.ExecuteResponse(ok=True, result="hi", metadata={"exit_code": 3})

    assert asking.model_dump(mode="json")["structured_result"] is True
    assert answer.model_dump(mode="json")["metadata"] == {"exit_code": 3}
    assert protocol.ExecuteResponse.model_validate_json(answer.model_dump_json()) == answer
    with pytest.raises(ValidationError, match="unknown_field"):
        protocol.CancelRequest.model_validate({"protocol": 8, "request_id": "r", "unknown_field": True})
