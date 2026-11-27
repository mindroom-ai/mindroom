"""Authoring validation, event normalization, and access/identity helpers for script_check triggers."""

from __future__ import annotations

import ast
import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from mindroom.reactive_triggers.models import (
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
    TriggerEvent,
)
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id
from mindroom.script_runs.manager import workflow_placement
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

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


def agent_supports_script_check(config: Config, agent_name: str) -> bool:
    """Return whether ``agent_name`` is configured with triggers plus script or workflow tools."""
    if agent_name not in config.agents:
        return False
    resolved = set(config.resolve_agent(agent_name).tools.tool_names)
    return "triggers" in resolved and bool({"script", "workflow"} & resolved)


def check_script_check_availability(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> tuple[Literal["policy", "agent", "placement"], str] | None:
    """Verify shared policy, executing-agent capability, and placement for script_check triggers."""
    pol = config.external_trigger_policy
    if not pol.enabled:
        return ("policy", "External triggers are disabled by configuration.")
    if not pol.reactive_enabled:
        return ("policy", "Reactive triggers are disabled by configuration.")
    if not pol.script_check_enabled:
        return ("policy", "Scheduled script_check triggers are disabled by policy.")
    if not agent_supports_script_check(config, agent_name):
        return (
            "agent",
            f"Agent {agent_name!r} requires 'triggers' plus 'script' or 'workflow' tools for script_check triggers.",
        )
    placement = workflow_placement(config, runtime_paths)
    if placement not in {"worker", "local"}:
        return ("placement", placement)
    return None


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


def serving_entity_for_identity(identity: ToolExecutionIdentity, agent_name: str, config: Config) -> str:
    """Return whose bot and access answer for a run or script_check with ``identity``.

    A member of a configured team that authored or started it in that team is reached through the team, and an agent
    served through another configured agent uses that agent; otherwise ``agent_name`` answers directly.
    """
    recipient = identity.recipient
    team = config.teams.get(recipient)
    through = recipient in config.agents if team is None else agent_name in team.agents
    return recipient if through else agent_name


def serving_entity_for_checkpoint(
    checkpoint: ScriptCheckCheckpoint | None,
    agent_name: str,
    config: Config,
) -> str:
    """Resolve the saved serving entity from ``checkpoint.identity_json``, falling back to ``agent_name``."""
    if checkpoint is None:
        return agent_name
    try:
        identity = parse_tool_execution_identity_payload(
            checkpoint.identity_json,
            error_prefix="script_check execution_identity",
        )
    except (TypeError, ValueError):
        return agent_name
    return serving_entity_for_identity(identity, agent_name, config)


def script_check_accessed_entities(config: Config, serving_entity: str, agent_name: str) -> set[str]:
    """Return the entities a requester must reach for a script_check served through ``serving_entity``."""
    team = config.teams.get(serving_entity)
    if team is not None and agent_name in team.agents:
        return {serving_entity}
    return {serving_entity, agent_name}


def check_script_check_source_access(
    *,
    owner_user_id: str,
    source_spec: ScriptCheckSourceSpec,
    config: Config,
    runtime_paths: RuntimePaths,
    require_placement: bool = False,
) -> str | None:
    """Verify current policy, human owner, executing-agent capability, and optional placement."""
    pol = config.external_trigger_policy
    if not pol.enabled:
        return "external_triggers_disabled"
    if not pol.reactive_enabled:
        return "reactive_triggers_disabled"
    if not pol.script_check_enabled:
        return "script_check_disabled"
    if not is_human_requester_id(owner_user_id, config, runtime_paths):
        return "owner_not_human"
    if not agent_supports_script_check(config, source_spec.agent):
        return "agent_script_check_unavailable"
    return (
        "placement_unavailable"
        if require_placement and workflow_placement(config, runtime_paths) not in {"worker", "local"}
        else None
    )
