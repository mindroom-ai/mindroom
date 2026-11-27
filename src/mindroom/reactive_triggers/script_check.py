"""Authoring validation, event normalization, and access/identity helpers for script_check triggers."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.reactive_triggers.models import (
    ScriptCheckCheckpoint,
    TriggerEvent,
)
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id
from mindroom.script_runs.manager import (
    unsupported_private_script_worker_scope_message,
    unsupported_script_worker_backend_message,
    workflow_placement,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot

_MAX_SCRIPT_CHECK_SOURCE_BYTES = 64 * 1024
_MAX_SCRIPT_CHECK_STATE_BYTES = 32 * 1024
_MAX_SCRIPT_CHECK_RESULT_BYTES = 64 * 1024
_SCRIPT_CHECK_EVENT_KIND = "script_check.match"

_ENVELOPE_STR_LIMITS = {"title": 500, "summary": 2000, "text": 8000, "actor": 256, "dedupe_key": 256}
_ENVELOPE_KEYS = frozenset(_ENVELOPE_STR_LIMITS) | {"data"}


def _reject(msg: str) -> NoReturn:
    raise ValueError(msg)


def validate_script_check_source(source_bytes: bytes) -> tuple[str, str]:
    """Validate script_check Python bytes and return ``(source_text, sha256_hex)``."""
    if len(source_bytes) > _MAX_SCRIPT_CHECK_SOURCE_BYTES:
        _reject(f"script_check source exceeds maximum size of {_MAX_SCRIPT_CHECK_SOURCE_BYTES} bytes")
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"script_check source must be valid UTF-8: {exc}"
        raise ValueError(msg) from exc
    if not source_text.strip():
        _reject("script_check source must not be empty")
    try:
        tree = ast.parse(source_text, filename="<script_check>")
        compile(tree, "<script_check>", "exec")
    except (SyntaxError, ValueError) as exc:
        msg = f"Invalid Python syntax in script_check source: {exc}"
        raise ValueError(msg) from exc
    _validate_check_function(tree)
    return source_text, hashlib.sha256(source_bytes).hexdigest()


def _validate_check_function(tree: ast.Module) -> None:
    check_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "check":
            check_node = node
    match check_node:
        case None:
            _reject("script_check source must define a top-level synchronous 'def check():' function")
        case ast.AsyncFunctionDef():
            _reject("script_check check() must be synchronous (use 'def check():', not 'async def check():')")
        case ast.FunctionDef(args=args):
            if args.posonlyargs or args.args or any(d is None for d in args.kw_defaults):
                _reject("script_check check() must take no required or positional arguments; use global tool()/state")


def _ensure_json_str_keys(value: object, *, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _reject(f"script_check {label} objects must use string keys")
            _ensure_json_str_keys(item, label=label)
    elif isinstance(value, list):
        for item in value:
            _ensure_json_str_keys(item, label=label)


def _validate_json_object(value: object, *, label: str, max_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject(f"script_check {label} must be a dict, got {type(value).__name__}")
    _ensure_json_str_keys(value, label=label)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        msg = f"script_check {label} must be strict finite JSON: {exc}"
        raise ValueError(msg) from exc
    if len(encoded) > max_bytes:
        _reject(f"script_check {label} exceeds maximum size of {max_bytes} bytes")
    parsed = json.loads(encoded)
    assert isinstance(parsed, dict)
    return parsed


def validate_script_check_state(state: object) -> dict[str, Any]:
    """Validate strict finite JSON state object within _MAX_SCRIPT_CHECK_STATE_BYTES."""
    return _validate_json_object(state, label="state", max_bytes=_MAX_SCRIPT_CHECK_STATE_BYTES)


def _extract_envelope_strings(payload: dict[str, Any]) -> dict[str, str | None]:
    envelope: dict[str, str | None] = {}
    for field, max_len in _ENVELOPE_STR_LIMITS.items():
        raw_val = payload.get(field)
        if raw_val is None:
            envelope[field] = None
            continue
        if not isinstance(raw_val, str):
            _reject(f"script_check result field '{field}' must be a string")
        if len(raw_val) > max_len:
            _reject(f"script_check result field '{field}' exceeds maximum length of {max_len}")
        stripped = raw_val.strip()
        if field == "dedupe_key" and not stripped:
            _reject("script_check result field 'dedupe_key' must not be empty")
        envelope[field] = stripped or None
    return envelope


def _extract_event_data(payload: dict[str, Any]) -> dict[str, Any]:
    extra = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
    if "data" not in payload:
        return extra
    raw_data = payload["data"]
    if raw_data is not None and not isinstance(raw_data, dict):
        _reject("script_check result field 'data' must be a dict")
    if extra:
        _reject(f"script_check result cannot combine 'data' with extra top-level keys: {', '.join(sorted(extra))}")
    return {} if raw_data is None else raw_data


def normalize_script_check_event(
    trigger_id: str,
    run_id: str,
    raw_result: object,
    *,
    now: float,
) -> TriggerEvent | None:
    """Normalize a check() return value into a TriggerEvent or None."""
    if raw_result is None:
        return None
    payload = _validate_json_object(raw_result, label="result", max_bytes=_MAX_SCRIPT_CHECK_RESULT_BYTES)
    env = _extract_envelope_strings(payload)
    event_data = _extract_event_data(payload)
    dedupe_key = env["dedupe_key"]
    if dedupe_key is not None:
        event_id = f"sc:key:{hashlib.sha256(f'{trigger_id}:{dedupe_key}'.encode()).hexdigest()[:32]}"
    else:
        event_id = f"sc:tick:{run_id}"
    return TriggerEvent(
        source_type="script_check",
        source_id=trigger_id,
        kind=_SCRIPT_CHECK_EVENT_KIND,
        event_id=event_id,
        timestamp=now,
        actor=env["actor"],
        title=env["title"],
        summary=env["summary"],
        text=env["text"],
        data=event_data,
    )


def clamp_script_check_poll_interval(raw: int | None, *, min_seconds: int) -> int:
    """Validate and clamp a script_check poll_interval_seconds value."""
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
        msg = "poll_interval_seconds must be an integer."
        raise TypeError(msg)
    return max(min_seconds, min(300 if raw is None else raw, 86400))


def script_check_health_summary(checkpoint: ScriptCheckCheckpoint | None) -> dict[str, object] | None:
    """Return owner-visible script_check health counters without exposing code, identity, or state."""
    if checkpoint is None:
        return None
    return {
        "consecutive_errors": checkpoint.consecutive_errors,
        "last_error_code": checkpoint.last_error_code,
        "last_run_at": checkpoint.last_run_at,
    }


def build_script_check_dry_run_event(
    trigger_id: str,
    *,
    actor: str | None,
    text: str | None,
    sample_data: Mapping[str, Any] | None,
    now: float,
) -> TriggerEvent:
    """Build a synthetic script_check TriggerEvent for dry-run triage without running Python."""
    data = dict(sample_data) if sample_data is not None else {}
    raw_title = data.get("title")
    raw_summary = data.get("summary")
    return TriggerEvent(
        source_type="script_check",
        source_id=trigger_id,
        kind=_SCRIPT_CHECK_EVENT_KIND,
        event_id="dry_run",
        timestamp=now,
        actor=actor,
        title=raw_title if isinstance(raw_title, str) else None,
        summary=raw_summary if isinstance(raw_summary, str) else None,
        text=text,
        data=data,
    )


@dataclass(frozen=True, slots=True)
class _ScriptCheckAccessFailure:
    """Shared failure details for script_check policy, capability, owner, and placement checks."""

    code: str
    category: Literal["policy", "agent", "placement"]
    message: str


def _script_check_agent_failure(config: Config, agent_name: str) -> _ScriptCheckAccessFailure | None:
    if agent_name not in config.agents:
        return _ScriptCheckAccessFailure(
            "agent_script_check_unavailable",
            "agent",
            f"Agent {agent_name!r} requires 'triggers' plus 'script' or 'workflow' tools for script_check triggers.",
        )
    resolved = set(config.resolve_agent(agent_name).tools.tool_names)
    if "triggers" not in resolved or not ({"script", "workflow"} & resolved):
        return _ScriptCheckAccessFailure(
            "agent_script_check_unavailable",
            "agent",
            f"Agent {agent_name!r} requires 'triggers' plus 'script' or 'workflow' tools for script_check triggers.",
        )
    if "script" not in resolved and tool_may_require_approval(
        config,
        "workflow_write",
        agent_name=agent_name,
        fixed_arguments={"action": "start"},
    ):
        return _ScriptCheckAccessFailure(
            "workflow_approval_required",
            "agent",
            (
                f"Agent {agent_name!r} relies on 'workflow' without 'script', and 'workflow_write' (action='start') may "
                "require tool approval. Grant 'script' or auto-approve 'workflow_write' launch to use script_check triggers."
            ),
        )
    return None


def _agent_supports_script_check(config: Config, agent_name: str) -> bool:
    """Return whether ``agent_name`` is configured with triggers plus script or eligible workflow tools."""
    return _script_check_agent_failure(config, agent_name) is None


def check_script_check_access(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    *,
    owner_user_id: str | None = None,
    require_placement: bool = True,
) -> _ScriptCheckAccessFailure | None:
    """Verify policy, optional human owner, executing-agent capability, and optional placement."""
    pol = config.external_trigger_policy
    failure: _ScriptCheckAccessFailure | None = None
    if not pol.is_reactive_active:
        failure = (
            _ScriptCheckAccessFailure(
                "external_triggers_disabled",
                "policy",
                "External triggers are disabled by configuration.",
            )
            if not pol.enabled
            else _ScriptCheckAccessFailure(
                "reactive_triggers_disabled",
                "policy",
                "Reactive triggers are disabled by configuration.",
            )
        )
    elif not pol.script_check_enabled:
        failure = _ScriptCheckAccessFailure(
            "script_check_disabled",
            "policy",
            "Scheduled script_check triggers are disabled by policy.",
        )
    elif owner_user_id is not None and not is_human_requester_id(owner_user_id, config, runtime_paths):
        failure = _ScriptCheckAccessFailure(
            "owner_not_human",
            "policy",
            "Only human users can own script_check triggers.",
        )
    elif (agent_failure := _script_check_agent_failure(config, agent_name)) is not None:
        failure = agent_failure
    elif require_placement:
        placement = workflow_placement(config, runtime_paths)
        if placement not in {"worker", "local"}:
            failure = _ScriptCheckAccessFailure("placement_unavailable", "placement", placement)
        elif placement == "worker":
            if backend_error := unsupported_script_worker_backend_message(runtime_paths):
                failure = _ScriptCheckAccessFailure("unsupported_worker_backend", "placement", backend_error)
            elif scope_error := unsupported_private_script_worker_scope_message(config, agent_name):
                failure = _ScriptCheckAccessFailure("unsupported_worker_scope", "placement", scope_error)
    return failure


def is_owner_joined_in_state(
    room_state: RoomStateSnapshot,
    *,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether any equivalent Matrix ID for ``owner_user_id`` is joined in ``room_state``."""
    return any(
        room_state.events.get(("m.room.member", user_id), {}).get("membership") == "join"
        for user_id in equivalent_requester_ids(owner_user_id, config, runtime_paths)
    )


SCRIPT_CHECK_BOOTSTRAP_SOURCE = """\
import hashlib, hmac, json, sys, traceback
from mindroom.script_runs.transport import ScriptGatewayTransport
from mindroom.script_sdk import MindRoomTools

_MAX_STATE_BYTES, _MAX_RESULT_BYTES, _MAX_ERROR_CHARS, _MISSING = 32 * 1024, 64 * 1024, 8000, object()

def _check_str_keys(value, label):
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str): raise ValueError(f"script_check {label} objects must use string keys")
            _check_str_keys(v, label)
    elif isinstance(value, list):
        for item in value: _check_str_keys(item, label)

def _encode_json(value, label, max_bytes):
    _check_str_keys(value, label)
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > max_bytes: raise ValueError(f"script_check {label} exceeds maximum size of {max_bytes} bytes")
    return raw

class _CheckState:
    def __init__(self, initial, is_active):
        self._data = json.loads(_encode_json(dict(initial), "state", _MAX_STATE_BYTES))
        self._is_active, self.changed = is_active, False

    def _require_active(self):
        if not self._is_active():
            raise RuntimeError("state mutations are not allowed during module load; mutate state inside check().")

    def _copy(self, value): return json.loads(json.dumps(value, allow_nan=False))
    def __contains__(self, key): return key in self._data
    def __getitem__(self, key): return self._copy(self._data[key])
    def get(self, key, default=None): return default if key not in self._data else self._copy(self._data[key])
    def keys(self): return list(self._data.keys())
    def items(self): return [(k, self._copy(v)) for k, v in self._data.items()]
    def snapshot(self): return self._copy(self._data)

    def set(self, key, value):
        self._require_active()
        if not isinstance(key, str): raise ValueError("state keys must be strings")
        self._data = json.loads(_encode_json({**self._data, key: value}, "state", _MAX_STATE_BYTES))
        self.changed = True

    def delete(self, key):
        self._require_active()
        if key in self._data:
            del self._data[key]
            self.changed = True

    def pop(self, key, default=_MISSING):
        self._require_active()
        if key in self._data:
            value = self.get(key)
            del self._data[key]
            self.changed = True
            return value
        if default is _MISSING: raise KeyError(key)
        return default

    def clear(self):
        self._require_active()
        if self._data:
            self._data.clear()
            self.changed = True

def _main():
    transport = ScriptGatewayTransport(timeout_seconds=10.0)
    run_id = transport.run_id
    payload = transport.request_json("GET", f"runs/{run_id}/check-input")
    if not isinstance(payload, dict): raise RuntimeError("Invalid check-input response.")
    source, digest, initial_state = payload.get("source"), payload.get("source_digest"), payload.get("state")
    if not isinstance(source, str) or not isinstance(digest, str) or not isinstance(initial_state, dict):
        raise RuntimeError("Malformed check-input payload.")
    if not hmac.compare_digest(hashlib.sha256(source.encode("utf-8")).hexdigest(), digest):
        raise RuntimeError("script_check source digest mismatch.")
    in_check, tools = False, MindRoomTools(http_timeout_seconds=10.0, poll_interval_seconds=0.1)
    state = _CheckState(initial_state, lambda: in_check)

    def tool(toolkit_name, function_name, /, **arguments):
        if not in_check: raise RuntimeError("tool() is not allowed during module load; call tool() inside check().")
        return tools.call(toolkit_name, function_name, **arguments)

    def agent(*_args, **_kwargs):
        raise RuntimeError(
            "agent() is not allowed inside script_check triggers; "
            "return a dict payload from check() to wake the target agent."
        )

    try:
        ns = {"__name__": "__script_check__", "tool": tool, "state": state, "agent": agent}
        exec(compile(source, "<script_check>", "exec"), ns)
        check_fn = ns.get("check")
        if not callable(check_fn): raise RuntimeError("script_check source must define a callable check() function.")
        in_check = True
        try:
            result = check_fn()
        finally:
            in_check = False
        if result is not None: _encode_json(result, "result", _MAX_RESULT_BYTES)
        body = {"result": result, "state": state.snapshot(), "state_changed": state.changed, "error": None}
        transport.request_json("POST", f"runs/{run_id}/check-result", body)
    except Exception as exc:
        err = ("".join(traceback.format_exception_only(type(exc), exc)).strip() or repr(exc))[:_MAX_ERROR_CHARS]
        try:
            body = {"result": None, "state": {}, "state_changed": False, "error": err}
            transport.request_json("POST", f"runs/{run_id}/check-result", body)
        except Exception:
            pass
        print(err, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    _main()
"""
