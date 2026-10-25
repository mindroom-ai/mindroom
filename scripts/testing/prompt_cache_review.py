"""Review live MindRoom prompt-cache behavior from JSONL request logs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mindroom.config.models import ModelConfig  # noqa: E402
from mindroom.constants import RuntimePaths, resolve_runtime_paths, runtime_env_path  # noqa: E402
from mindroom.llm.messages import Message  # noqa: E402
from mindroom.llm.provider import ModelRequest, ModelTurn, complete  # noqa: E402
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider  # noqa: E402
from mindroom.providers.anthropic.wire import format_claude_messages  # noqa: E402
from mindroom.providers.cache_ladder import (  # noqa: E402
    MESSAGE_RUNG_COUNT,
    mark_message_cache_rungs,
    prompt_cache_control,
)
from mindroom.providers.model_http_client import closing_model_connections  # noqa: E402

type JsonDict = dict[str, object]


def object_dict(value: object) -> JsonDict | None:
    """Return a typed string-keyed dict when the runtime value is a dict."""
    return cast("JsonDict", value) if isinstance(value, dict) else None


@dataclass(frozen=True)
class JsonlParseStats:
    """Parsing diagnostics for one JSONL file."""

    line_count: int
    document_count: int
    concatenated_document_count: int
    decode_error_count: int
    unparseable_row_count: int = 0
    unparseable_row_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestRow:
    """One logged LLM request."""

    timestamp: datetime
    session_id: str | None
    room_id: str | None
    agent_name: str
    model_id: str
    system_prompt: str
    message_count: int
    message_blobs: tuple[str, ...]
    normalized_message_blobs: tuple[str, ...]
    preview: str
    tools_blob: str = ""
    cache_enabled: bool = False
    request_log_id: str = ""
    usage: dict[str, int] | None = None

    @property
    def total_prefix_chars(self) -> int:
        """Character size of the reusable prefix (tools + system + messages)."""
        return len(self.tools_blob) + len(self.system_prompt) + sum(len(blob) for blob in self.normalized_message_blobs)


@dataclass(frozen=True)
class SessionReview:
    """Adjacent-request comparison for one session."""

    session_id: str
    prompt_family: str
    room_id: str | None
    agent_name: str
    model_id: str
    request_count: int
    adjacent_pair_count: int
    exact_full_match_count: int
    exact_minus_last_match_count: int
    prefix_extension_count: int
    message_delta_counter: Counter[int]
    message_count_trace: tuple[int, ...]
    latest_timestamp: datetime
    latest_preview: str


@dataclass(frozen=True)
class CacheSimulationOutcome:
    """Simulated provider-cache outcome for one logged request."""

    row: RequestRow
    outcome: str
    divergence: str | None
    read_chars: int


@dataclass(frozen=True)
class CacheSimulationReport:
    """Aggregated provider-cache simulation over one set of logged requests."""

    ttl_seconds: int
    lookback_blocks: int
    outcomes: tuple[CacheSimulationOutcome, ...]


@dataclass(frozen=True)
class ProbeModelSpec:
    """Resolved Vertex Claude model settings for one probe."""

    config_path: Path
    model_name: str
    model_id: str
    project_id: str
    region: str
    base_url: str | None
    cache_system_prompt: bool
    extended_cache_time: bool


@dataclass(frozen=True)
class LiveProbeTurn:
    """One live cache-probe request."""

    turn_index: int
    request_message_count: int
    normalized_prefix_extension: bool | None
    raw_prefix_extension: bool | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    response_text: str
    preview: str

    @property
    def cache_read_fraction(self) -> float | None:
        """Return cache-read tokens as a fraction of total prompt tokens."""
        denominator = self.input_tokens + self.cache_read_tokens
        if denominator <= 0:
            return None
        return self.cache_read_tokens / denominator


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the review tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        help="Config file for live probes. Defaults to the storage-root sibling config.yaml when present.",
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        help="MindRoom storage root. Defaults to MINDROOM_STORAGE_PATH or ~/.mindroom-chat/mindroom_data.",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="Specific JSONL request log to inspect. Defaults to the latest file in logs/llm_requests.",
    )
    parser.add_argument(
        "--session",
        help="Restrict output to one exact session_id.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="How many recent sessions to print when --session is not set. Default: 8",
    )
    parser.add_argument(
        "--no-simulate",
        action="store_true",
        help="Skip the TTL-aware provider-cache simulation section.",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=60,
        help="Cache entry TTL assumed by the simulation. Default: 60",
    )
    parser.add_argument(
        "--lookback-blocks",
        type=int,
        default=20,
        help="Provider cache-lookup window in content blocks. Default: 20",
    )
    parser.add_argument(
        "--probe-live",
        action="store_true",
        help="Run a live Vertex Claude prompt-cache probe through MindRoom's Claude adapter, not JSONL review.",
    )
    parser.add_argument(
        "--probe-model",
        default="default",
        help="Vertex model name from config, or a raw Claude model id. Default: default",
    )
    parser.add_argument(
        "--probe-turns",
        type=int,
        default=4,
        help="How many live turns to send in the probe conversation. Default: 4",
    )
    parser.add_argument(
        "--probe-system-lines",
        type=int,
        default=80,
        help="Number of long stable system-prompt lines for the live probe. Default: 80",
    )
    parser.add_argument(
        "--probe-first-user-lines",
        type=int,
        default=40,
        help="Number of long stable first-user lines for the live probe. Default: 40",
    )
    parser.add_argument(
        "--probe-max-output-tokens",
        type=int,
        default=16,
        help="Max output tokens per live probe turn. Default: 16",
    )
    parser.add_argument(
        "--probe-threshold",
        type=float,
        default=0.9,
        help="Pass threshold for later-turn cache read fraction. Default: 0.9",
    )
    parser.add_argument(
        "--probe-compare-plain",
        action="store_true",
        help="Also run a plain baseline without MindRoom's prompt-cache breakpoints.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the review tool CLI."""
    args = parse_args()
    storage_root = resolve_storage_root(args.storage_root)
    if args.probe_live:
        run_live_probe(
            storage_root=storage_root,
            config_path=resolve_probe_config_path(args.config, storage_root),
            model_name=args.probe_model,
            turn_count=args.probe_turns,
            system_line_count=args.probe_system_lines,
            first_user_line_count=args.probe_first_user_lines,
            max_output_tokens=args.probe_max_output_tokens,
            threshold=args.probe_threshold,
            compare_plain=args.probe_compare_plain,
        )
        return
    jsonl_path = args.jsonl or find_latest_jsonl(storage_root)

    rows, parse_stats = load_request_rows(jsonl_path, session_id_filter=args.session)
    reviews = build_session_reviews(rows)

    if args.session:
        reviews = [review for review in reviews if review.session_id == args.session]

    print_overview(
        jsonl_path=jsonl_path,
        parse_stats=parse_stats,
        rows=rows,
        reviews=reviews,
        requested_session=args.session,
        top=args.top,
    )

    if not args.no_simulate:
        print()
        print_cache_simulation(
            simulate_prompt_cache(
                rows,
                ttl_seconds=args.ttl_minutes * 60,
                lookback_blocks=args.lookback_blocks,
            ),
        )

    print()
    print_cache_actuals(rows)


def resolve_storage_root(explicit_root: Path | None) -> Path:
    """Resolve the storage root whose JSONL request logs the review reads."""
    if explicit_root is not None:
        return explicit_root.expanduser()

    candidates: list[Path] = []
    env_storage_path = os.getenv("MINDROOM_STORAGE_PATH")
    if env_storage_path:
        candidates.append(Path(env_storage_path).expanduser())
    candidates.append(Path.home() / ".mindroom-chat" / "mindroom_data")
    candidates.append(Path("/home/basnijholt/.mindroom-chat/mindroom_data"))
    candidates.append(Path.cwd() / "mindroom_data")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def find_latest_jsonl(storage_root: Path) -> Path:
    """Return the newest LLM request JSONL file under the storage root."""
    log_dir = storage_root / "logs" / "llm_requests"
    jsonl_paths = sorted(log_dir.glob("*.jsonl"))
    if not jsonl_paths:
        msg = f"No JSONL files found in {log_dir}"
        raise SystemExit(msg)
    return jsonl_paths[-1]


def resolve_probe_config_path(explicit_config: Path | None, storage_root: Path) -> Path:
    """Resolve the config path used for live Vertex probes."""
    if explicit_config is not None:
        return explicit_config.expanduser().resolve()

    sibling_config = storage_root.parent / "config.yaml"
    if sibling_config.exists():
        return sibling_config.resolve()

    return resolve_runtime_paths(storage_path=storage_root).config_path


def load_request_rows(
    jsonl_path: Path,
    *,
    session_id_filter: str | None = None,
) -> tuple[list[RequestRow], JsonlParseStats]:
    """Load request rows plus JSONL parse diagnostics from one log file."""
    decoder = json.JSONDecoder()
    rows: list[RequestRow] = []
    usage_by_request_log_id: dict[str, dict[str, int]] = {}
    line_count = 0
    document_count = 0
    concatenated_document_count = 0
    decode_error_count = 0
    unparseable_row_count = 0
    unparseable_row_errors: list[str] = []

    with jsonl_path.open(encoding="utf-8", errors="replace") as handle:
        for _line_count, raw_line in enumerate(handle, start=1):
            line_count = _line_count
            stripped_line = raw_line.strip()
            if not stripped_line:
                continue
            parsed_documents = 0
            position = 0
            while position < len(stripped_line):
                while position < len(stripped_line) and stripped_line[position].isspace():
                    position += 1
                if position >= len(stripped_line):
                    break
                try:
                    payload, end_position = decoder.raw_decode(stripped_line, position)
                except json.JSONDecodeError:
                    decode_error_count += 1
                    break
                parsed_documents += 1
                document_count += 1
                position = end_position
                row_error = _append_request_row(rows, payload, session_id_filter, usage_by_request_log_id)
                if row_error is not None:
                    unparseable_row_count += 1
                    unparseable_row_errors.append(row_error)
            if parsed_documents > 1:
                concatenated_document_count += parsed_documents - 1

    rows = [
        replace(row, usage=usage_by_request_log_id[row.request_log_id])
        if row.request_log_id in usage_by_request_log_id
        else row
        for row in rows
    ]
    rows.sort(key=lambda row: row.timestamp)
    return rows, JsonlParseStats(
        line_count=line_count,
        document_count=document_count,
        concatenated_document_count=concatenated_document_count,
        decode_error_count=decode_error_count,
        unparseable_row_count=unparseable_row_count,
        unparseable_row_errors=tuple(dict.fromkeys(unparseable_row_errors))[:3],
    )


def _append_request_row(
    rows: list[RequestRow],
    payload: object,
    session_id_filter: str | None,
    usage_by_request_log_id: dict[str, dict[str, int]],
) -> str | None:
    """Parse one logged payload into ``rows``; return an error summary when it cannot be rebuilt.

    Response records (compact usage lines written after each provider call) are
    collected into ``usage_by_request_log_id`` before any session filtering:
    they carry no session id and are joined to request rows by request_log_id.

    Corrupt logged rows can fail anywhere in the provider-format rebuild (JSON
    decoding, message decoding, Claude message formatting), and one bad row
    must never kill an offline review, so this deliberately catches everything
    and reports the failure instead.
    """
    payload_dict = object_dict(payload)
    usage_record = parse_response_usage_record(payload_dict)
    if usage_record is not None:
        request_log_id, usage = usage_record
        usage_by_request_log_id[request_log_id] = usage
        return None
    if session_id_filter is not None and (payload_dict is None or payload_dict.get("session_id") != session_id_filter):
        return None
    try:
        row = parse_request_row(payload)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    if row is not None:
        rows.append(row)
    return None


def parse_response_usage_record(payload_dict: JsonDict | None) -> tuple[str, dict[str, int]] | None:
    """Parse one response record into its (request_log_id, usage) pair, or None."""
    if payload_dict is None or payload_dict.get("record") != "response":
        return None
    request_log_id = payload_dict.get("request_log_id")
    usage = object_dict(payload_dict.get("usage"))
    if not isinstance(request_log_id, str) or not request_log_id or usage is None:
        return None
    return request_log_id, {key: value for key, value in usage.items() if isinstance(value, int)}


def parse_request_row(payload: object) -> RequestRow | None:
    """Parse one logged request payload into a normalized request row."""
    payload_dict = object_dict(payload)
    if payload_dict is None or payload_dict.get("record") == "response":
        return None
    timestamp_raw = payload_dict.get("timestamp")
    if not isinstance(timestamp_raw, str):
        return None
    try:
        timestamp = datetime.fromisoformat(timestamp_raw)
    except ValueError:
        return None

    system_prompt = payload_dict.get("system_prompt")
    if not isinstance(system_prompt, str):
        system_prompt = ""

    messages_raw = payload_dict.get("messages")
    model_id_raw = payload_dict.get("model_id")
    model_id = model_id_raw if isinstance(model_id_raw, str) else str(model_id_raw or "")
    model_params = payload_dict.get("model_params")
    message_blobs, normalized_message_blobs, preview = build_provider_message_blobs(
        messages_raw,
        model_id,
        model_params,
    )
    session_id_raw = payload_dict.get("session_id")
    room_id_raw = payload_dict.get("room_id")
    # The request logger writes "agent_id"; accept "agent_name" for older logs.
    agent_name_raw = payload_dict.get("agent_id") or payload_dict.get("agent_name")
    tools_raw = payload_dict.get("tools")
    request_log_id_raw = payload_dict.get("request_log_id")
    model_params_dict = object_dict(model_params)
    return RequestRow(
        timestamp=timestamp,
        session_id=session_id_raw if isinstance(session_id_raw, str) else None,
        room_id=room_id_raw if isinstance(room_id_raw, str) else None,
        agent_name=agent_name_raw if isinstance(agent_name_raw, str) else str(agent_name_raw or ""),
        model_id=model_id,
        system_prompt=system_prompt,
        message_count=len(message_blobs),
        message_blobs=message_blobs,
        normalized_message_blobs=normalized_message_blobs,
        preview=preview or "<no preview>",
        tools_blob=stable_json(tools_raw) if isinstance(tools_raw, list) and tools_raw else "",
        cache_enabled=model_params_dict is not None and model_params_dict.get("cache_system_prompt") is True,
        request_log_id=request_log_id_raw if isinstance(request_log_id_raw, str) else "",
    )


def build_provider_message_blobs(
    messages_raw: object,
    model_id: str,
    model_params: object,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Serialize logged messages into provider-specific cache comparison blobs."""
    prompt_messages = parse_logged_messages(messages_raw)
    return build_provider_message_blobs_from_messages(prompt_messages, model_id, model_params)


def build_provider_message_blobs_from_messages(
    prompt_messages: list[Message],
    model_id: str,
    model_params: object,
    *,
    apply_cache_ladder: bool = True,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Build raw and normalized provider message blobs from parsed messages."""
    preview = ""
    for message in prompt_messages:
        if message.role not in ("system", "developer"):
            preview = shorten_text(extract_text(message.content), 110)
            break

    if is_claude_request(model_id):
        chat_messages = format_claude_messages(
            prompt_messages,
            append_trailing_user_message=False,
            citations=True,
        ).messages
        if apply_cache_ladder:
            cache_control = ladder_cache_control(model_params)
            if cache_control is not None:
                chat_messages, _ = mark_message_cache_rungs(chat_messages, cache_control, MESSAGE_RUNG_COUNT)
        raw_blobs = tuple(stable_json(message) for message in chat_messages)
        normalized_blobs = tuple(stable_json(strip_cache_control(message)) for message in chat_messages)
        return raw_blobs, normalized_blobs, preview

    normalized_messages = [
        {
            "role": message.role,
            "content": message.content,
            **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
            **({"tool_calls": message.tool_calls} if message.tool_calls else {}),
        }
        for message in prompt_messages
        if message.role not in ("system", "developer")
    ]
    serialized_messages = tuple(stable_json(message) for message in normalized_messages)
    return serialized_messages, serialized_messages, preview


def parse_logged_messages(messages_raw: object) -> list[Message]:
    """Parse logged message payloads, which the request log writes as ``Message.to_dict()``, for comparison."""
    if not isinstance(messages_raw, list):
        return []
    return [Message.from_dict(fields) for message in messages_raw if (fields := object_dict(message)) is not None]


def ladder_cache_control(model_params: object) -> dict[str, str] | None:
    """Return the ladder cache_control for logged model params, or None when caching is off."""
    model_params_dict = object_dict(model_params)
    if model_params_dict is None or model_params_dict.get("cache_system_prompt") is not True:
        return None
    return prompt_cache_control(extended_cache_time=model_params_dict.get("extended_cache_time") is True)


def is_claude_request(model_id: str) -> bool:
    """Return whether the request model is a Claude variant."""
    return "claude" in model_id.lower()


def extract_text(content: object) -> str:
    """Return a best-effort plain-text representation of message content."""
    return " ".join(chunk for chunk in extract_text_chunks(content) if chunk)


def extract_text_chunks(content: object) -> list[str]:
    """Collect plain-text fragments from supported message content shapes."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [chunk for item in content for chunk in extract_text_chunks(item) if chunk]
    content_dict = object_dict(content)
    if content_dict is None:
        return []
    return [
        value for value in (content_dict.get("text"), content_dict.get("content")) if isinstance(value, str) and value
    ]


def stable_json(value: object) -> str:
    """Serialize a value into stable JSON for deterministic prompt comparisons."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strip_cache_control(value: object) -> object:
    """Remove Claude cache-control markers from nested JSON-like values."""
    jsonable_value = to_jsonable(value)
    if isinstance(jsonable_value, dict):
        return {key: strip_cache_control(item) for key, item in jsonable_value.items() if key != "cache_control"}
    if isinstance(jsonable_value, list):
        return [strip_cache_control(item) for item in jsonable_value]
    return jsonable_value


def to_jsonable(value: object) -> object:
    """Convert nested values into JSON-serializable primitives."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump())
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return to_jsonable(value_dict)
    return str(value)


def build_session_reviews(rows: list[RequestRow]) -> list[SessionReview]:
    """Group request rows into per-session prompt-cache review summaries."""
    grouped_rows: dict[tuple[str, str, str, str], list[RequestRow]] = defaultdict(list)
    for row in rows:
        if row.session_id is None:
            continue
        grouped_rows[(row.session_id, row.system_prompt, row.agent_name, row.model_id)].append(row)

    reviews: list[SessionReview] = []
    for (session_id, system_prompt, agent_name, model_id), session_rows in grouped_rows.items():
        session_rows.sort(key=lambda row: row.timestamp)
        exact_full_match_count = 0
        exact_minus_last_match_count = 0
        prefix_extension_count = 0
        message_delta_counter: Counter[int] = Counter()

        for previous_row, current_row in pairwise(session_rows):
            if rows_match_exactly(previous_row, current_row):
                exact_full_match_count += 1
            if rows_match_minus_last(previous_row, current_row):
                exact_minus_last_match_count += 1
            if current_extends_previous(previous_row, current_row):
                prefix_extension_count += 1
                message_delta_counter[current_row.message_count - previous_row.message_count] += 1

        latest_row = session_rows[-1]
        reviews.append(
            SessionReview(
                session_id=session_id,
                prompt_family=prompt_family_label(system_prompt),
                room_id=latest_row.room_id,
                agent_name=agent_name,
                model_id=model_id,
                request_count=len(session_rows),
                adjacent_pair_count=max(0, len(session_rows) - 1),
                exact_full_match_count=exact_full_match_count,
                exact_minus_last_match_count=exact_minus_last_match_count,
                prefix_extension_count=prefix_extension_count,
                message_delta_counter=message_delta_counter,
                message_count_trace=tuple(row.message_count for row in session_rows),
                latest_timestamp=latest_row.timestamp,
                latest_preview=latest_row.preview,
            ),
        )

    reviews.sort(key=lambda review: review.latest_timestamp, reverse=True)
    return reviews


def rows_match_exactly(first: RequestRow, second: RequestRow) -> bool:
    """Return whether two rows have identical reusable prompt content."""
    return (
        first.system_prompt == second.system_prompt
        and first.normalized_message_blobs == second.normalized_message_blobs
    )


def rows_match_minus_last(first: RequestRow, second: RequestRow) -> bool:
    """Return whether two rows match when the latest message is ignored."""
    return (
        first.system_prompt == second.system_prompt
        and first.normalized_message_blobs[:-1] == second.normalized_message_blobs[:-1]
    )


def current_extends_previous(previous_row: RequestRow, current_row: RequestRow) -> bool:
    """Return whether the current row extends the previous reusable prompt prefix."""
    if previous_row.system_prompt != current_row.system_prompt:
        return False
    if len(previous_row.normalized_message_blobs) > len(current_row.normalized_message_blobs):
        return False
    return (
        previous_row.normalized_message_blobs
        == current_row.normalized_message_blobs[: len(previous_row.normalized_message_blobs)]
    )


def current_extends_previous_raw(previous_row: RequestRow, current_row: RequestRow) -> bool:
    """Return whether the raw provider payload extends the previous request prefix."""
    if previous_row.system_prompt != current_row.system_prompt:
        return False
    if len(previous_row.message_blobs) > len(current_row.message_blobs):
        return False
    return previous_row.message_blobs == current_row.message_blobs[: len(previous_row.message_blobs)]


def parsed_blob(blob: str) -> object:
    """Parse one serialized message blob back into JSON, or None on failure."""
    with suppress(json.JSONDecodeError):
        return json.loads(blob)
    return None


def message_rung_indexes(row: RequestRow) -> tuple[int, ...]:
    """Return the message indexes whose raw blobs carry a cache_control marker."""
    indexes: list[int] = []
    for index, blob in enumerate(row.message_blobs):
        if '"cache_control"' not in blob:
            continue
        parsed = object_dict(parsed_blob(blob))
        content = parsed.get("content") if parsed is not None else None
        if isinstance(content, list) and any(
            block_dict is not None and block_dict.get("cache_control")
            for block_dict in (object_dict(block) for block in content)
        ):
            indexes.append(index)
    return tuple(indexes)


def message_block_count(blob: str) -> int:
    """Approximate the provider content-block count of one message blob."""
    parsed = object_dict(parsed_blob(blob))
    content = parsed.get("content") if parsed is not None else None
    if isinstance(content, list):
        return max(1, len(content))
    return 1


def simulate_prompt_cache(  # noqa: C901, PLR0912, PLR0915
    rows: list[RequestRow],
    *,
    ttl_seconds: int = 3600,
    lookback_blocks: int = 20,
) -> CacheSimulationReport:
    """Simulate provider prompt-cache behavior across all logged Claude requests.

    Unlike the per-session adjacent-pair review, this models what the provider
    cache actually does: every cache-enabled request writes entries at its
    boundaries (tools array, system prompt, and the message rungs marked in
    the raw blobs), and a later request in the same (agent, model) stream
    reads the deepest entry whose prefix bytes match exactly, provided the
    entry is younger than the TTL and, for message boundaries, within the
    provider's content-block lookback window of the request's newest rung.
    Character counts stand in for tokens; the reuse ratio is the signal, not
    the absolute numbers.
    """
    streams: dict[tuple[str, str], list[RequestRow]] = defaultdict(list)
    for row in rows:
        if is_claude_request(row.model_id):
            streams[(row.agent_name, row.model_id)].append(row)

    rungs_cache: dict[int, tuple[int, ...]] = {}
    blocks_cache: dict[int, tuple[int, ...]] = {}

    def rungs(row: RequestRow) -> tuple[int, ...]:
        key = id(row)
        if key not in rungs_cache:
            rungs_cache[key] = message_rung_indexes(row)
        return rungs_cache[key]

    def block_counts(row: RequestRow) -> tuple[int, ...]:
        key = id(row)
        if key not in blocks_cache:
            blocks_cache[key] = tuple(message_block_count(blob) for blob in row.message_blobs)
        return blocks_cache[key]

    outcomes: list[CacheSimulationOutcome] = []
    for stream_rows in streams.values():
        stream_rows.sort(key=lambda row: row.timestamp)
        for index, row in enumerate(stream_rows):
            if not row.cache_enabled:
                outcomes.append(CacheSimulationOutcome(row=row, outcome="uncached", divergence=None, read_chars=0))
                continue
            candidates = [
                previous
                for previous in stream_rows[:index]
                if previous.cache_enabled and (row.timestamp - previous.timestamp).total_seconds() <= ttl_seconds
            ]
            if not candidates:
                outcomes.append(CacheSimulationOutcome(row=row, outcome="cold", divergence=None, read_chars=0))
                continue

            row_rungs = rungs(row)
            row_last_rung = row_rungs[-1] if row_rungs else None
            head_chars = len(row.tools_blob) + len(row.system_prompt)
            best_read = 0
            best_outcome = "miss"
            deepest_divergence: tuple[int, str] = (-1, "tools")
            for previous in candidates:
                if previous.tools_blob != row.tools_blob:
                    deepest_divergence = max(deepest_divergence, (0, "tools"))
                    continue
                if previous.system_prompt != row.system_prompt:
                    if row.tools_blob and len(row.tools_blob) > best_read:
                        best_read, best_outcome = len(row.tools_blob), "tools_hit"
                    deepest_divergence = max(deepest_divergence, (1, "system"))
                    continue
                if head_chars > best_read:
                    best_read, best_outcome = head_chars, "system_hit"
                usable_rung = None
                for rung in rungs(previous):
                    if rung >= len(row.normalized_message_blobs):
                        break
                    if previous.normalized_message_blobs[: rung + 1] == row.normalized_message_blobs[: rung + 1]:
                        usable_rung = rung
                shared = min(len(previous.normalized_message_blobs), len(row.normalized_message_blobs))
                first_diff = next(
                    (
                        position
                        for position in range(shared)
                        if previous.normalized_message_blobs[position] != row.normalized_message_blobs[position]
                    ),
                    shared,
                )
                deepest_divergence = max(deepest_divergence, (2 + first_diff, f"history msg[{first_diff}]"))
                if usable_rung is None:
                    continue
                if row_last_rung is not None and usable_rung < row_last_rung:
                    gap_blocks = sum(block_counts(row)[usable_rung + 1 : row_last_rung + 1])
                    if gap_blocks > lookback_blocks:
                        continue
                read = head_chars + sum(len(blob) for blob in row.normalized_message_blobs[: usable_rung + 1])
                if read > best_read:
                    best_read, best_outcome = read, "full_hit"
            divergence = deepest_divergence[1] if best_outcome != "full_hit" else None
            outcomes.append(
                CacheSimulationOutcome(row=row, outcome=best_outcome, divergence=divergence, read_chars=best_read),
            )

    outcomes.sort(key=lambda outcome: outcome.row.timestamp)
    return CacheSimulationReport(ttl_seconds=ttl_seconds, lookback_blocks=lookback_blocks, outcomes=tuple(outcomes))


def print_cache_actuals(rows: list[RequestRow]) -> None:
    """Print provider-reported cache usage joined from logged response records.

    Unlike the simulation, these are the provider's own numbers: what each
    request actually read from and wrote to the prompt cache. Token counts are
    real tokens, so they are comparable across requests and against billing.
    """
    measured = [row for row in rows if row.usage is not None and is_claude_request(row.model_id)]
    if not measured:
        print("ACTUAL CACHE USAGE: no Claude response usage records found (older logs predate response logging).")
        return

    def token_totals(selected: list[RequestRow]) -> tuple[int, int, int]:
        usages = [row.usage or {} for row in selected]
        read = sum(usage.get("cache_read_tokens", 0) for usage in usages)
        write = sum(usage.get("cache_write_tokens", 0) for usage in usages)
        uncached = sum(usage.get("input_tokens", 0) for usage in usages)
        return read, write, uncached

    read, write, uncached = token_totals(measured)
    prompt_total = read + write + uncached
    print(f"ACTUAL CACHE USAGE (provider-reported, {len(measured)} of {len(rows)} requests measured)")
    print(f"  prompt tokens: cache_read={read:,} cache_write={write:,} uncached={uncached:,}")
    if prompt_total:
        print(f"  actual prefix reuse: {read / prompt_total * 100:.1f}% of prompt tokens")
    cold_misses = [row for row in measured if row.cache_enabled and (row.usage or {}).get("cache_read_tokens", 0) == 0]
    if cold_misses:
        print(f"  cache-enabled requests that read nothing: {len(cold_misses)}")

    by_stream: dict[tuple[str, str], list[RequestRow]] = defaultdict(list)
    for row in measured:
        by_stream[(row.agent_name, row.model_id)].append(row)
    ranked = sorted(
        by_stream.items(),
        key=lambda item: -sum((row.usage or {}).get("cache_read_tokens", 0) for row in item[1]),
    )
    for (agent_name, model_id), stream_rows in ranked[:5]:
        stream_read, stream_write, stream_uncached = token_totals(stream_rows)
        stream_total = stream_read + stream_write + stream_uncached
        reuse = f"{stream_read / stream_total * 100:.1f}%" if stream_total else "n/a"
        print(
            f"    agent={agent_name} model={model_id} requests={len(stream_rows)} "
            f"reuse={reuse} read={stream_read:,} write={stream_write:,} uncached={stream_uncached:,}",
        )


def print_cache_simulation(report: CacheSimulationReport) -> None:
    """Print the provider-cache simulation summary."""
    outcomes = report.outcomes
    if not outcomes:
        print("CACHE SIMULATION: no Claude requests found.")
        return
    counts = Counter(outcome.outcome for outcome in outcomes)
    total_chars = sum(outcome.row.total_prefix_chars for outcome in outcomes)
    read_chars = sum(outcome.read_chars for outcome in outcomes)
    print(
        f"CACHE SIMULATION (Claude requests, TTL={report.ttl_seconds // 60}m, "
        f"lookback~{report.lookback_blocks} blocks)",
    )
    outcome_order = ("full_hit", "system_hit", "tools_hit", "miss", "cold", "uncached")
    outcome_summary = " ".join(f"{name}={counts[name]}" for name in outcome_order if counts[name])
    print(f"  requests={len(outcomes)} {outcome_summary}")
    if total_chars:
        print(f"  estimated prefix reuse: {read_chars / total_chars * 100:.1f}% of prompt chars")
    divergences = Counter(outcome.divergence.split(" ")[0] for outcome in outcomes if outcome.divergence)
    if divergences:
        formatted = ", ".join(f"{name}={count}" for name, count in divergences.most_common())
        print(f"  divergence causes (deepest-matching candidate): {formatted}")
    worst = sorted(
        (outcome for outcome in outcomes if outcome.outcome in ("miss", "system_hit", "tools_hit")),
        key=lambda outcome: outcome.row.total_prefix_chars - outcome.read_chars,
        reverse=True,
    )[:5]
    if worst:
        print("  largest reprocessed prefixes:")
    for outcome in worst:
        wasted = outcome.row.total_prefix_chars - outcome.read_chars
        print(
            f"    {outcome.row.timestamp:%m-%d %H:%M:%S} agent={outcome.row.agent_name} "
            f"model={outcome.row.model_id} {outcome.outcome} diverged at {outcome.divergence}: "
            f"~{wasted:,} chars reprocessed",
        )


def run_live_probe(
    *,
    storage_root: Path,
    config_path: Path,
    model_name: str,
    turn_count: int,
    system_line_count: int,
    first_user_line_count: int,
    max_output_tokens: int,
    threshold: float,
    compare_plain: bool,
) -> None:
    """Run a live Vertex Claude prompt-cache probe through MindRoom's Claude adapter."""
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=storage_root)
    bootstrap_probe_environment(runtime_paths)
    spec = load_probe_model_spec(config_path, runtime_paths, model_name)

    print("Live Vertex Claude probe")
    print(f"Config: {spec.config_path}")
    print(
        f"Model: {spec.model_name} -> {spec.model_id} | project={spec.project_id} | region={spec.region} | "
        f"cache_system_prompt={spec.cache_system_prompt} | extended_cache_time={spec.extended_cache_time}",
    )
    print(
        f"Scenario: turns={turn_count}, system_lines={system_line_count}, first_user_lines={first_user_line_count}, "
        f"max_output_tokens={max_output_tokens}",
    )
    print()

    modes = [("hooked", True)]
    if compare_plain:
        modes.insert(0, ("plain", False))

    for label, install_hook in modes:
        turns = run_live_probe_sequence(
            spec,
            runtime_paths,
            scenario_label=label,
            install_hook=install_hook,
            turn_count=turn_count,
            system_line_count=system_line_count,
            first_user_line_count=first_user_line_count,
            max_output_tokens=max_output_tokens,
        )
        print_live_probe_results(label, turns, threshold=threshold)
        print()


def bootstrap_probe_environment(runtime_paths: RuntimePaths) -> None:
    """Populate probe-specific environment variables from the runtime config."""
    google_application_credentials = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if google_application_credentials is not None and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(google_application_credentials)

    for env_name in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_BASE_URL"):
        value = runtime_paths.env_value(env_name)
        if value and env_name not in os.environ:
            os.environ[env_name] = value


def load_probe_model_spec(config_path: Path, runtime_paths: RuntimePaths, model_name: str) -> ProbeModelSpec:
    """Load and validate the Vertex Claude settings used for a live probe."""
    config_data = object_dict(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
    if config_data is None:
        msg = f"Expected mapping config at {config_path}"
        raise SystemExit(msg)

    model_id = model_name
    extra_kwargs: JsonDict = {}
    models = object_dict(config_data.get("models"))
    if models is not None and model_name in models:
        model_config = object_dict(models[model_name])
        if model_config is None:
            msg = f"Invalid model config for {model_name}"
            raise SystemExit(msg)
        provider = str(model_config.get("provider", ""))
        if provider != "vertexai_claude":
            msg = f"Model {model_name} uses provider {provider!r}, not vertexai_claude"
            raise SystemExit(msg)
        model_id = str(model_config.get("id", "")).strip()
        extra_kwargs = object_dict(model_config.get("extra_kwargs")) or {}
    elif "claude" not in model_name.lower():
        available = sorted(models) if isinstance(models, dict) else []
        msg = f"Unknown Vertex probe model {model_name!r}. Available configured models: {available}"
        raise SystemExit(msg)

    project_id = str(
        extra_kwargs.get("project_id") or runtime_paths.env_value("ANTHROPIC_VERTEX_PROJECT_ID") or "",
    ).strip()
    region = str(extra_kwargs.get("region") or runtime_paths.env_value("CLOUD_ML_REGION") or "").strip()
    base_url_raw = str(
        extra_kwargs.get("base_url") or runtime_paths.env_value("ANTHROPIC_VERTEX_BASE_URL") or "",
    ).strip()
    if not project_id:
        msg = "Missing ANTHROPIC_VERTEX_PROJECT_ID for live Vertex probe"
        raise SystemExit(msg)
    if not region:
        msg = "Missing CLOUD_ML_REGION for live Vertex probe"
        raise SystemExit(msg)

    return ProbeModelSpec(
        config_path=config_path,
        model_name=model_name,
        model_id=model_id,
        project_id=project_id,
        region=region,
        base_url=base_url_raw or None,
        cache_system_prompt=coerce_bool(extra_kwargs.get("cache_system_prompt"), default=True),
        extended_cache_time=coerce_bool(extra_kwargs.get("extended_cache_time"), default=True),
    )


def coerce_bool(value: object, *, default: bool) -> bool:
    """Coerce a loose config value into a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def run_live_probe_sequence(
    spec: ProbeModelSpec,
    runtime_paths: RuntimePaths,
    *,
    scenario_label: str,
    install_hook: bool,
    turn_count: int,
    system_line_count: int,
    first_user_line_count: int,
    max_output_tokens: int,
) -> list[LiveProbeTurn]:
    """Run the repeated live probe requests for one scenario label."""
    provider = build_probe_model(spec, runtime_paths, install_hook=install_hook, max_output_tokens=max_output_tokens)
    system_prompt = build_probe_system_prompt(system_line_count, scenario_label)
    conversation_messages: list[Message] = [Message(role="system", content=system_prompt)]
    previous_request_row: RequestRow | None = None
    turn_results: list[LiveProbeTurn] = []
    model_params = {
        "cache_system_prompt": spec.cache_system_prompt,
        "extended_cache_time": spec.extended_cache_time,
    }

    for turn_index in range(1, turn_count + 1):
        request_messages = [
            *conversation_messages,
            Message(role="user", content=build_probe_user_prompt(turn_index, first_user_line_count, scenario_label)),
        ]
        request_row = build_probe_request_row(
            request_messages,
            model_id=spec.model_id,
            model_params=model_params,
            apply_cache_ladder=install_hook,
        )
        request = ModelRequest(
            messages=tuple(request_messages),
            stream=False,
        )
        response = asyncio.run(closing_model_connections(complete(provider, request)))
        response_text = extract_model_response_text(response)
        turn_results.append(
            LiveProbeTurn(
                turn_index=turn_index,
                request_message_count=request_row.message_count,
                normalized_prefix_extension=(
                    None
                    if previous_request_row is None
                    else current_extends_previous(previous_request_row, request_row)
                ),
                raw_prefix_extension=(
                    None
                    if previous_request_row is None
                    else current_extends_previous_raw(previous_request_row, request_row)
                ),
                input_tokens=response_usage_int(response, "input_tokens"),
                output_tokens=response_usage_int(response, "output_tokens"),
                cache_read_tokens=response_usage_int(response, "cache_read_tokens"),
                cache_write_tokens=response_usage_int(response, "cache_write_tokens"),
                response_text=response_text,
                preview=request_row.preview,
            ),
        )
        conversation_messages = [*request_messages, Message(role="assistant", content=response_text)]
        previous_request_row = request_row
    return turn_results


def build_probe_model(
    spec: ProbeModelSpec,
    runtime_paths: RuntimePaths,
    *,
    install_hook: bool,
    max_output_tokens: int,
) -> ClaudeProvider:
    """Construct the Vertex Claude adapter used for a live probe sequence; the plain one sends no breakpoints."""
    extra_kwargs: dict[str, object] = {
        "project_id": spec.project_id,
        "region": spec.region,
        "cache_system_prompt": spec.cache_system_prompt and install_hook,
        "extended_cache_time": spec.extended_cache_time,
        "temperature": 0,
        "max_tokens": max_output_tokens,
    }
    if spec.base_url:
        extra_kwargs["base_url"] = spec.base_url
    model_config = ModelConfig(provider="vertexai_claude", id=spec.model_id, extra_kwargs=extra_kwargs)
    return build_provider(model_config, runtime_paths)


def build_probe_system_prompt(line_count: int, scenario_label: str) -> str:
    """Build the long stable system prompt used for cache validation."""
    return "\n".join(
        f"Cache validation charter {index:03d} [{scenario_label}]: preserve every earlier clause verbatim because exact prefix reuse matters."
        for index in range(1, line_count + 1)
    )


def build_probe_user_prompt(turn_index: int, first_user_line_count: int, scenario_label: str) -> str:
    """Build the user prompt for one live probe turn."""
    if turn_index == 1:
        stable_body = "\n".join(
            f"Stable user block {index:03d} [{scenario_label}]: this line exists only to create one long reusable user prefix."
            for index in range(1, first_user_line_count + 1)
        )
        return (
            f"This is live cache validation turn 01 for {scenario_label}.\n"
            "The following block is intentionally long and should become reusable cached user input.\n"
            f"{stable_body}\n"
            "Reply with exactly ACK-01."
        )
    return f"Live cache validation turn {turn_index:02d} for {scenario_label}. Reply with exactly ACK-{turn_index:02d}."


def build_probe_request_row(
    messages: list[Message],
    *,
    model_id: str,
    model_params: object,
    apply_cache_ladder: bool,
) -> RequestRow:
    """Build a request row for a synthetic live probe turn."""
    message_blobs, normalized_message_blobs, preview = build_provider_message_blobs_from_messages(
        messages,
        model_id,
        model_params,
        apply_cache_ladder=apply_cache_ladder,
    )
    system_prompt = "\n".join(extract_text(message.content) for message in messages if message.role == "system")
    return RequestRow(
        timestamp=datetime.now().astimezone(),
        session_id=None,
        room_id=None,
        agent_name="live_probe",
        model_id=model_id,
        system_prompt=system_prompt,
        message_count=len(message_blobs),
        message_blobs=message_blobs,
        normalized_message_blobs=normalized_message_blobs,
        preview=preview or "<no preview>",
    )


def extract_model_response_text(response: ModelTurn) -> str:
    """Extract plain text from a model response for probe logging."""
    content = response.message.content
    if isinstance(content, str) and content:
        return content
    return extract_text(content) or "<empty response>"


def response_usage_int(response: ModelTurn, field_name: str) -> int:
    """Return one integer usage field from a model response."""
    usage = response.message.metrics
    if usage is None:
        return 0
    if field_name == "input_tokens":
        return coerce_int(usage.input_tokens)
    if field_name == "output_tokens":
        return coerce_int(usage.output_tokens)
    if field_name == "cache_read_tokens":
        return coerce_int(usage.cache_read_tokens)
    if field_name == "cache_write_tokens":
        return coerce_int(usage.cache_write_tokens)
    msg = f"Unsupported response usage field: {field_name}"
    raise ValueError(msg)


def print_live_probe_results(mode_label: str, turns: list[LiveProbeTurn], *, threshold: float) -> None:
    """Print the live probe summary for one probe mode."""
    print(f"{mode_label.upper()} MODE")
    for turn in turns:
        normalized_prefix = format_probe_bool(turn.normalized_prefix_extension)
        raw_prefix = format_probe_bool(turn.raw_prefix_extension)
        print(
            "  "
            f"turn {turn.turn_index}: "
            f"messages={turn.request_message_count}, "
            f"normalized_prefix={normalized_prefix}, "
            f"raw_prefix={raw_prefix}, "
            f"input={turn.input_tokens}, "
            f"cache_read={turn.cache_read_tokens}, "
            f"cache_write={turn.cache_write_tokens}, "
            f"output={turn.output_tokens}, "
            f"fraction={format_percentage(turn.cache_read_fraction)}, "
            f"reply={shorten_text(turn.response_text, 24)}",
        )
    later_turns = [turn for turn in turns[1:] if turn.cache_read_fraction is not None]
    if later_turns:
        later_fractions = [turn.cache_read_fraction for turn in later_turns if turn.cache_read_fraction is not None]
        over_threshold_count = sum(1 for fraction in later_fractions if fraction >= threshold)
        print(
            f"  later turns >= {threshold * 100:.1f}% cache_read: {over_threshold_count}/{len(later_fractions)}",
        )


def format_probe_bool(value: bool | None) -> str:
    """Format a nullable boolean for probe output."""
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def print_overview(
    *,
    jsonl_path: Path,
    parse_stats: JsonlParseStats,
    rows: list[RequestRow],
    reviews: list[SessionReview],
    requested_session: str | None,
    top: int,
) -> None:
    """Print the top-level review overview of the logged sessions."""
    missing_session_rows = sum(1 for row in rows if row.session_id is None)
    print(f"JSONL: {jsonl_path}")
    print(
        "Parsed "
        f"{parse_stats.document_count} JSON documents from {parse_stats.line_count} lines "
        f"(concatenated docs: {parse_stats.concatenated_document_count}, decode errors: {parse_stats.decode_error_count}).",
    )
    if parse_stats.unparseable_row_count:
        print(f"Skipped {parse_stats.unparseable_row_count} rows whose logged payload could not be rebuilt.")
        for row_error in parse_stats.unparseable_row_errors:
            print(f"  {shorten_text(row_error, 140)}")
    print(f"Rows with session_id: {len(rows) - missing_session_rows}; rows without session_id: {missing_session_rows}")
    print("Comparisons ignore moving `cache_control` markers and focus on the reusable content prefix.")
    print()

    if not reviews:
        if requested_session is not None:
            print(f"No matching session rows found for {requested_session}.")
            return
        print("No session-scoped rows found in the selected JSONL.")
        return

    sessions_to_print = reviews if requested_session else reviews[:top]
    for review in sessions_to_print:
        print_session_review(review, show_run_history=requested_session is not None)
        print()


def print_session_review(review: SessionReview, *, show_run_history: bool) -> None:
    """Print one session review row."""
    print(shorten_text(review.session_id, 88))
    print(
        "  "
        f"{review.latest_timestamp.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"agent={review.agent_name} | model={review.model_id} | family={review.prompt_family}",
    )
    print(
        "  "
        f"requests={review.request_count}, "
        f"full={review.exact_full_match_count}/{review.adjacent_pair_count}, "
        f"minus_last={review.exact_minus_last_match_count}/{review.adjacent_pair_count}, "
        f"prefix_extensions={review.prefix_extension_count}/{review.adjacent_pair_count}, "
        f"deltas={format_message_deltas(review.message_delta_counter)}",
    )
    if show_run_history:
        print(f"  message_counts: {' -> '.join(str(count) for count in review.message_count_trace)}")
    print(f"  preview: {review.latest_preview}")


def format_message_deltas(counter: Counter[int]) -> str:
    """Format message-count deltas for human-readable output."""
    if not counter:
        return "none"
    return ", ".join(f"{delta:+d}x{count}" for delta, count in sorted(counter.items()))


def format_percentage(value: float | None) -> str:
    """Format a fraction as a percentage string."""
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def shorten_text(text: str, limit: int) -> str:
    """Normalize whitespace and clamp text to a display width."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def prompt_family_label(system_prompt: str) -> str:
    """Derive a short family label from the first non-empty system-prompt line."""
    for line in system_prompt.splitlines():
        normalized = " ".join(line.split())
        if normalized:
            return shorten_text(normalized, 54)
    return "<empty system prompt>"


def coerce_int(value: object) -> int:
    """Coerce a loose numeric value into an integer or zero."""
    return value if isinstance(value, int) else 0


if __name__ == "__main__":
    main()
