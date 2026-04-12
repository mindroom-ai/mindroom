"""Review live MindRoom prompt-cache behavior from JSONL logs and session DBs."""

from __future__ import annotations

# ruff: noqa: C901, D102, D103, EM101, EM102, S112, S608, TRY003
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from agno.models.message import Message
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import format_messages

if TYPE_CHECKING:
    from agno.models.response import ModelResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mindroom.constants import RuntimePaths, resolve_runtime_paths, runtime_env_path  # noqa: E402
from mindroom.vertex_claude_prompt_cache import (  # noqa: E402
    copy_messages_with_vertex_prompt_cache_breakpoint,
    install_vertex_claude_prompt_cache_hook,
)

DEFAULT_AGENT_DB = "mindroom_dev"
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


@dataclass(frozen=True)
class RunMetrics:
    """Cache metrics for one persisted Agno run."""

    created_at: datetime | None
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    input_content: str | None

    @property
    def cache_read_fraction(self) -> float | None:
        denominator = self.input_tokens + self.cache_read_tokens
        if denominator <= 0:
            return None
        return self.cache_read_tokens / denominator


@dataclass(frozen=True)
class DbSessionSummary:
    """Aggregated DB-backed metrics for one session."""

    session_id: str
    runs: tuple[RunMetrics, ...]
    run_count: int
    updated_at: datetime | None
    latest_run: RunMetrics | None
    total_input_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int

    @property
    def aggregate_cache_read_fraction(self) -> float | None:
        denominator = self.total_input_tokens + self.total_cache_read_tokens
        if denominator <= 0:
            return None
        return self.total_cache_read_tokens / denominator


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
class ProbeModelSpec:
    """Resolved direct-Agno Vertex Claude model settings for one probe."""

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
    """One live direct-Agno cache-probe request."""

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
        denominator = self.input_tokens + self.cache_read_tokens
        if denominator <= 0:
            return None
        return self.cache_read_tokens / denominator


def parse_args() -> argparse.Namespace:
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
        "--db",
        type=Path,
        help="Specific SQLite session DB to inspect. Defaults to session-aware DB discovery.",
    )
    parser.add_argument(
        "--agent-db",
        default=DEFAULT_AGENT_DB,
        help=f"Agent DB name for default DB discovery. Default: {DEFAULT_AGENT_DB}",
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
        "--probe-live",
        action="store_true",
        help="Run a direct Agno Vertex Claude prompt-cache probe instead of JSONL/DB review.",
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
        help="Also run a plain Agno baseline without the MindRoom Vertex prompt-cache hook.",
    )
    return parser.parse_args()


def main() -> None:
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
    db_path = resolve_db_path(storage_root, args.db, args.agent_db, args.session)

    rows, parse_stats = load_request_rows(jsonl_path, session_id_filter=args.session)
    reviews = build_session_reviews(rows)
    db_summaries = load_db_summaries(db_path) if db_path is not None and db_path.exists() else {}

    if args.session:
        reviews = [review for review in reviews if review.session_id == args.session]

    print_overview(
        jsonl_path=jsonl_path,
        db_path=db_path if db_path is not None and db_path.exists() else None,
        parse_stats=parse_stats,
        rows=rows,
        reviews=reviews,
        db_summaries=db_summaries,
        requested_session=args.session,
        top=args.top,
    )


def resolve_storage_root(explicit_root: Path | None) -> Path:
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
    log_dir = storage_root / "logs" / "llm_requests"
    jsonl_paths = sorted(log_dir.glob("*.jsonl"))
    if not jsonl_paths:
        msg = f"No JSONL files found in {log_dir}"
        raise SystemExit(msg)
    return jsonl_paths[-1]


def resolve_probe_config_path(explicit_config: Path | None, storage_root: Path) -> Path:
    if explicit_config is not None:
        return explicit_config.expanduser().resolve()

    sibling_config = storage_root.parent / "config.yaml"
    if sibling_config.exists():
        return sibling_config.resolve()

    return resolve_runtime_paths(storage_path=storage_root).config_path


def default_db_path(storage_root: Path, agent_name: str) -> Path | None:
    db_path = storage_root / "agents" / agent_name / "sessions" / f"{agent_name}.db"
    return db_path if db_path.exists() else None


def resolve_db_path(
    storage_root: Path,
    explicit_db_path: Path | None,
    agent_db: str,
    session_id: str | None,
) -> Path | None:
    if explicit_db_path is not None:
        return explicit_db_path.expanduser()

    default_path = default_db_path(storage_root, agent_db)
    if session_id is None:
        return default_path

    if default_path is not None and db_contains_session(default_path, session_id):
        return default_path

    for db_path in sorted((storage_root / "agents").glob("*/sessions/*.db")):
        if db_contains_session(db_path, session_id):
            return db_path

    return default_path


def db_contains_session(db_path: Path, session_id: str) -> bool:
    if not db_path.exists():
        return False
    connection = sqlite3.connect(db_path)
    try:
        table_name = detect_session_table_name(connection)
        cursor = connection.cursor()
        cursor.execute(f"SELECT 1 FROM {table_name} WHERE session_id = ? LIMIT 1", (session_id,))
        return cursor.fetchone() is not None
    except (sqlite3.Error, SystemExit):
        return False
    finally:
        connection.close()


def load_request_rows(
    jsonl_path: Path,
    *,
    session_id_filter: str | None = None,
) -> tuple[list[RequestRow], JsonlParseStats]:
    decoder = json.JSONDecoder()
    rows: list[RequestRow] = []
    line_count = 0
    document_count = 0
    concatenated_document_count = 0
    decode_error_count = 0

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
                payload_dict = object_dict(payload)
                if session_id_filter is not None and (
                    payload_dict is None or payload_dict.get("session_id") != session_id_filter
                ):
                    continue
                row = parse_request_row(payload_dict if payload_dict is not None else payload)
                if row is not None:
                    rows.append(row)
            if parsed_documents > 1:
                concatenated_document_count += parsed_documents - 1

    rows.sort(key=lambda row: row.timestamp)
    return rows, JsonlParseStats(
        line_count=line_count,
        document_count=document_count,
        concatenated_document_count=concatenated_document_count,
        decode_error_count=decode_error_count,
    )


def parse_request_row(payload: object) -> RequestRow | None:
    payload_dict = object_dict(payload)
    if payload_dict is None:
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
    agent_name_raw = payload_dict.get("agent_name")
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
    )


def build_provider_message_blobs(
    messages_raw: object,
    model_id: str,
    model_params: object,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    prompt_messages = parse_logged_messages(messages_raw)
    return build_provider_message_blobs_from_messages(prompt_messages, model_id, model_params)


def build_provider_message_blobs_from_messages(
    prompt_messages: list[Message],
    model_id: str,
    model_params: object,
    *,
    apply_vertex_cache_breakpoint: bool = True,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    preview = ""
    for message in prompt_messages:
        if message.role not in ("system", "developer"):
            preview = shorten_text(extract_text(message.content), 110)
            break

    if is_claude_request(model_id):
        if apply_vertex_cache_breakpoint:
            prompt_messages = apply_vertex_cache_breakpoint_if_needed(prompt_messages, model_id, model_params)
        else:
            prompt_messages = [message.model_copy(deep=True) for message in prompt_messages]
        chat_messages, _ = format_messages(prompt_messages, compress_tool_results=True)
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
    if not isinstance(messages_raw, list):
        return []

    parsed_messages: list[Message] = []
    for message in messages_raw:
        if not isinstance(message, dict):
            continue
        try:
            parsed_message = Message.model_validate(message)
        except Exception:
            continue
        parsed_messages.append(parsed_message.model_copy(update={"audio": None, "videos": None}, deep=True))
    return parsed_messages


def apply_vertex_cache_breakpoint_if_needed(
    messages: list[Message],
    model_id: str,
    model_params: object,
) -> list[Message]:
    model_params_dict = object_dict(model_params)
    if model_params_dict is None or model_params_dict.get("cache_system_prompt") is not True:
        return [message.model_copy(deep=True) for message in messages]
    model = VertexAIClaude(
        id=model_id,
        cache_system_prompt=True,
        extended_cache_time=model_params_dict.get("extended_cache_time") is True,
    )
    return copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)


def is_claude_request(model_id: str) -> bool:
    return "claude" in model_id.lower()


def extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            else:
                item_dict = object_dict(item)
                if item_dict is None:
                    continue
                text_value = item_dict.get("text")
                if isinstance(text_value, str):
                    chunks.append(text_value)
                    continue
                content_value = item_dict.get("content")
                if isinstance(content_value, str):
                    chunks.append(content_value)
        return " ".join(chunk for chunk in chunks if chunk)
    content_dict = object_dict(content)
    if content_dict is not None:
        text_value = content_dict.get("text")
        if isinstance(text_value, str):
            return text_value
        content_value = content_dict.get("content")
        if isinstance(content_value, str):
            return content_value
    return ""


def stable_json(value: object) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strip_cache_control(value: object) -> object:
    jsonable_value = to_jsonable(value)
    if isinstance(jsonable_value, dict):
        return {key: strip_cache_control(item) for key, item in jsonable_value.items() if key != "cache_control"}
    if isinstance(jsonable_value, list):
        return [strip_cache_control(item) for item in jsonable_value]
    return jsonable_value


def to_jsonable(value: object) -> object:
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
    return (
        first.system_prompt == second.system_prompt
        and first.normalized_message_blobs == second.normalized_message_blobs
    )


def rows_match_minus_last(first: RequestRow, second: RequestRow) -> bool:
    return (
        first.system_prompt == second.system_prompt
        and first.normalized_message_blobs[:-1] == second.normalized_message_blobs[:-1]
    )


def current_extends_previous(previous_row: RequestRow, current_row: RequestRow) -> bool:
    if previous_row.system_prompt != current_row.system_prompt:
        return False
    if len(previous_row.normalized_message_blobs) > len(current_row.normalized_message_blobs):
        return False
    return (
        previous_row.normalized_message_blobs
        == current_row.normalized_message_blobs[: len(previous_row.normalized_message_blobs)]
    )


def current_extends_previous_raw(previous_row: RequestRow, current_row: RequestRow) -> bool:
    if previous_row.system_prompt != current_row.system_prompt:
        return False
    if len(previous_row.message_blobs) > len(current_row.message_blobs):
        return False
    return previous_row.message_blobs == current_row.message_blobs[: len(previous_row.message_blobs)]


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
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=storage_root)
    bootstrap_probe_environment(runtime_paths)
    spec = load_probe_model_spec(config_path, runtime_paths, model_name)

    print("Live direct Agno Vertex Claude probe")
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
    google_application_credentials = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if google_application_credentials is not None and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(google_application_credentials)

    for env_name in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_BASE_URL"):
        value = runtime_paths.env_value(env_name)
        if value and env_name not in os.environ:
            os.environ[env_name] = value


def load_probe_model_spec(config_path: Path, runtime_paths: RuntimePaths, model_name: str) -> ProbeModelSpec:
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
        raise SystemExit("Missing ANTHROPIC_VERTEX_PROJECT_ID for live Vertex probe")
    if not region:
        raise SystemExit("Missing CLOUD_ML_REGION for live Vertex probe")

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
    *,
    scenario_label: str,
    install_hook: bool,
    turn_count: int,
    system_line_count: int,
    first_user_line_count: int,
    max_output_tokens: int,
) -> list[LiveProbeTurn]:
    model = build_probe_model(spec, install_hook=install_hook, max_output_tokens=max_output_tokens)
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
            *[message.model_copy(deep=True) for message in conversation_messages],
            Message(role="user", content=build_probe_user_prompt(turn_index, first_user_line_count, scenario_label)),
        ]
        request_row = build_probe_request_row(
            request_messages,
            model_id=spec.model_id,
            model_params=model_params,
            apply_vertex_cache_breakpoint=install_hook,
        )
        response = model.invoke(messages=request_messages, assistant_message=Message(role="assistant"))
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
    *,
    install_hook: bool,
    max_output_tokens: int,
) -> VertexAIClaude:
    model = VertexAIClaude(
        id=spec.model_id,
        project_id=spec.project_id,
        region=spec.region,
        cache_system_prompt=spec.cache_system_prompt,
        extended_cache_time=spec.extended_cache_time,
        temperature=0,
        max_tokens=max_output_tokens,
        base_url=spec.base_url,
    )
    if install_hook:
        install_vertex_claude_prompt_cache_hook(model)
    return model


def build_probe_system_prompt(line_count: int, scenario_label: str) -> str:
    return "\n".join(
        f"Cache validation charter {index:03d} [{scenario_label}]: preserve every earlier clause verbatim because exact prefix reuse matters."
        for index in range(1, line_count + 1)
    )


def build_probe_user_prompt(turn_index: int, first_user_line_count: int, scenario_label: str) -> str:
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
    apply_vertex_cache_breakpoint: bool,
) -> RequestRow:
    message_blobs, normalized_message_blobs, preview = build_provider_message_blobs_from_messages(
        messages,
        model_id,
        model_params,
        apply_vertex_cache_breakpoint=apply_vertex_cache_breakpoint,
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


def extract_model_response_text(response: ModelResponse) -> str:
    if isinstance(response.content, str) and response.content:
        return response.content
    return extract_text(response.content) or "<empty response>"


def response_usage_int(response: ModelResponse, field_name: str) -> int:
    usage = response.response_usage
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
    raise ValueError(f"Unsupported response usage field: {field_name}")


def print_live_probe_results(mode_label: str, turns: list[LiveProbeTurn], *, threshold: float) -> None:
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
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def load_db_summaries(db_path: Path) -> dict[str, DbSessionSummary]:
    connection = sqlite3.connect(db_path)
    try:
        table_name = detect_session_table_name(connection)
        cursor = connection.cursor()
        cursor.execute(f"SELECT session_id, updated_at, runs FROM {table_name}")
        summaries: dict[str, DbSessionSummary] = {}
        for session_id, updated_at_raw, runs_raw in cursor.fetchall():
            if not isinstance(session_id, str):
                continue
            runs_value = parse_nested_json(runs_raw)
            run_summaries = parse_run_summaries(runs_value)
            latest_run = run_summaries[-1] if run_summaries else None
            summaries[session_id] = DbSessionSummary(
                session_id=session_id,
                runs=tuple(run_summaries),
                run_count=len(run_summaries),
                updated_at=from_unix_timestamp(updated_at_raw),
                latest_run=latest_run,
                total_input_tokens=sum(run.input_tokens for run in run_summaries),
                total_cache_read_tokens=sum(run.cache_read_tokens for run in run_summaries),
                total_cache_write_tokens=sum(run.cache_write_tokens for run in run_summaries),
            )
        return summaries
    finally:
        connection.close()


def detect_session_table_name(connection: sqlite3.Connection) -> str:
    cursor = connection.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_sessions'")
    table_names = [name for (name,) in cursor.fetchall() if isinstance(name, str)]
    if len(table_names) != 1:
        msg = f"Expected exactly one *_sessions table, found {table_names}"
        raise SystemExit(msg)
    return table_names[0]


def parse_nested_json(value: object) -> object:
    parsed_value = value
    while isinstance(parsed_value, str):
        try:
            parsed_value = json.loads(parsed_value)
        except json.JSONDecodeError:
            return None
    return parsed_value


def parse_run_summaries(runs_value: object) -> list[RunMetrics]:
    if not isinstance(runs_value, list):
        return []

    run_summaries: list[RunMetrics] = []
    for run in runs_value:
        run_dict = object_dict(run)
        if run_dict is None:
            continue
        metrics = object_dict(run_dict.get("metrics"))
        if metrics is None:
            continue
        run_summaries.append(
            RunMetrics(
                created_at=from_unix_timestamp(run_dict.get("created_at")),
                input_tokens=coerce_int(metrics.get("input_tokens")),
                cache_read_tokens=coerce_int(metrics.get("cache_read_tokens")),
                cache_write_tokens=coerce_int(metrics.get("cache_write_tokens")),
                input_content=extract_run_input_content(run_dict.get("input")),
            ),
        )
    run_summaries.sort(key=lambda run: run.created_at or datetime.min.replace(tzinfo=UTC))
    return run_summaries


def print_overview(
    *,
    jsonl_path: Path,
    db_path: Path | None,
    parse_stats: JsonlParseStats,
    rows: list[RequestRow],
    reviews: list[SessionReview],
    db_summaries: dict[str, DbSessionSummary],
    requested_session: str | None,
    top: int,
) -> None:
    missing_session_rows = sum(1 for row in rows if row.session_id is None)
    print(f"JSONL: {jsonl_path}")
    print(
        "Parsed "
        f"{parse_stats.document_count} JSON documents from {parse_stats.line_count} lines "
        f"(concatenated docs: {parse_stats.concatenated_document_count}, decode errors: {parse_stats.decode_error_count}).",
    )
    print(f"Rows with session_id: {len(rows) - missing_session_rows}; rows without session_id: {missing_session_rows}")
    print("Comparisons ignore moving `cache_control` markers and focus on the reusable content prefix.")

    if db_path is not None:
        print(f"DB: {db_path}")
        matched_reviews = [review for review in reviews if review.session_id in db_summaries]
        unique_latest_fractions: dict[str, float | None] = {}
        for review in matched_reviews:
            latest_run = db_summaries[review.session_id].latest_run
            if latest_run is not None:
                unique_latest_fractions[review.session_id] = latest_run.cache_read_fraction
        latest_fractions = [fraction for fraction in unique_latest_fractions.values() if fraction is not None]
        if latest_fractions:
            over_ninety_count = sum(1 for fraction in latest_fractions if fraction >= 0.9)
            print(
                f"Latest DB cache-read fraction >= 90%: {over_ninety_count}/{len(latest_fractions)} matched sessions",
            )

    print()

    if not reviews:
        if requested_session is not None:
            print(f"No matching session rows found for {requested_session}.")
            return
        print("No session-scoped rows found in the selected JSONL.")
        return

    sessions_to_print = reviews if requested_session else reviews[:top]
    for review in sessions_to_print:
        print_session_review(
            review,
            db_summaries.get(review.session_id),
            show_run_history=requested_session is not None,
        )
        print()


def print_session_review(
    review: SessionReview,
    db_summary: DbSessionSummary | None,
    *,
    show_run_history: bool,
) -> None:
    latest_run = db_summary.latest_run if db_summary is not None else None
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
    if db_summary is None:
        print("  db: no matching session in DB")
        return
    print(
        "  "
        f"db latest={format_percentage(latest_run.cache_read_fraction if latest_run else None)} "
        f"(input={latest_run.input_tokens if latest_run else 0}, "
        f"cache_read={latest_run.cache_read_tokens if latest_run else 0}), "
        f"aggregate={format_percentage(db_summary.aggregate_cache_read_fraction)}, "
        f"runs={db_summary.run_count}",
    )
    if show_run_history and db_summary.runs:
        for index, run in enumerate(db_summary.runs, start=1):
            created_at = run.created_at.astimezone().strftime("%H:%M:%S %Z") if run.created_at else "unknown"
            print(
                "  "
                f"run {index}: {created_at}, "
                f"input={run.input_tokens}, "
                f"cache_read={run.cache_read_tokens}, "
                f"cache_write={run.cache_write_tokens}, "
                f"fraction={format_percentage(run.cache_read_fraction)}",
            )
        input_lengths = [len(run.input_content) for run in db_summary.runs if isinstance(run.input_content, str)]
        if input_lengths:
            print(f"  db input lengths: {' -> '.join(str(length) for length in input_lengths)}")
        input_prefix_lengths = [
            common_prefix_length(previous_run.input_content, current_run.input_content)
            for previous_run, current_run in pairwise(db_summary.runs)
            if isinstance(previous_run.input_content, str) and isinstance(current_run.input_content, str)
        ]
        if input_prefix_lengths:
            print(f"  db input common prefixes: {' -> '.join(str(length) for length in input_prefix_lengths)}")


def format_message_deltas(counter: Counter[int]) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{delta:+d}x{count}" for delta, count in sorted(counter.items()))


def format_percentage(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def shorten_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def prompt_family_label(system_prompt: str) -> str:
    for line in system_prompt.splitlines():
        normalized = " ".join(line.split())
        if normalized:
            return shorten_text(normalized, 54)
    return "<empty system prompt>"


def coerce_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def extract_run_input_content(value: object) -> str | None:
    value_dict = object_dict(value)
    if value_dict is None:
        return None
    input_content = value_dict.get("input_content")
    return input_content if isinstance(input_content, str) else None


def common_prefix_length(first: str | None, second: str | None) -> int:
    if not isinstance(first, str) or not isinstance(second, str):
        return 0
    prefix_length = 0
    for first_char, second_char in zip(first, second):
        if first_char != second_char:
            break
        prefix_length += 1
    return prefix_length


def from_unix_timestamp(value: object) -> datetime | None:
    if not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value).astimezone()


if __name__ == "__main__":
    main()
