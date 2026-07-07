"""Tests for the prompt-cache review helper script."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agno.models.message import Message

if TYPE_CHECKING:
    from types import ModuleType

    import pytest


def _load_prompt_cache_review_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "testing" / "prompt_cache_review.py"
    spec = importlib.util.spec_from_file_location("prompt_cache_review_script", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_request_rows_handles_concatenated_json_objects(tmp_path: Path) -> None:
    """Parse concatenated JSON documents from one JSONL line."""
    module = _load_prompt_cache_review_module()
    jsonl_path = tmp_path / "requests.jsonl"
    jsonl_path.write_text(
        '{"timestamp":"2026-04-11T11:00:00-07:00","agent_name":"opus","model_id":"claude-opus-4-8","system_prompt":"S","messages":[{"role":"user","content":"a"}],"message_count":1}'
        '{"timestamp":"2026-04-11T11:00:01-07:00","agent_name":"opus","model_id":"claude-opus-4-8","system_prompt":"S","messages":[{"role":"user","content":"b"}],"message_count":1}\n',
        encoding="utf-8",
    )

    rows, stats = module.load_request_rows(jsonl_path)

    assert len(rows) == 2
    assert stats.document_count == 2
    assert stats.concatenated_document_count == 1
    assert stats.decode_error_count == 0


def test_load_request_rows_skips_rows_with_corrupt_tool_call_arguments(tmp_path: Path) -> None:
    """One logged row with invalid tool_call arguments must not kill the review."""
    module = _load_prompt_cache_review_module()
    jsonl_path = tmp_path / "requests.jsonl"
    corrupt_arguments = '{"args": "curl \\\\"https://example.test?x=1%5C" | head"}'
    corrupt_row = {
        "timestamp": "2026-04-11T11:00:00-07:00",
        "agent_name": "opus",
        "model_id": "claude-opus-4-8",
        "system_prompt": "S",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [{"id": "t1", "function": {"name": "run_shell_command", "arguments": corrupt_arguments}}],
            },
        ],
        "message_count": 1,
    }
    healthy_row = {
        "timestamp": "2026-04-11T11:00:01-07:00",
        "agent_name": "opus",
        "model_id": "claude-opus-4-8",
        "system_prompt": "S",
        "messages": [{"role": "user", "content": "hello"}],
        "message_count": 1,
    }
    jsonl_path.write_text(
        json.dumps(corrupt_row) + "\n" + json.dumps(healthy_row) + "\n",
        encoding="utf-8",
    )

    rows, stats = module.load_request_rows(jsonl_path)

    assert len(rows) == 1
    assert rows[0].preview == "hello"
    assert stats.unparseable_row_count == 1
    assert stats.decode_error_count == 0
    assert len(stats.unparseable_row_errors) == 1
    assert "JSONDecodeError" in stats.unparseable_row_errors[0]


def test_build_session_reviews_detects_prefix_extension_with_two_appended_messages() -> None:
    """Treat appended request messages as a reusable-prefix extension."""
    module = _load_prompt_cache_review_module()
    rows = [
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-8",
            system_prompt="S",
            message_count=2,
            message_blobs=("m1", "m2"),
            normalized_message_blobs=("m1", "m2"),
            preview="first",
        ),
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:05-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-8",
            system_prompt="S",
            message_count=4,
            message_blobs=("m1", "m2", "m3", "m4"),
            normalized_message_blobs=("m1", "m2", "m3", "m4"),
            preview="second",
        ),
    ]

    review = module.build_session_reviews(rows)[0]

    assert review.request_count == 2
    assert review.adjacent_pair_count == 1
    assert review.exact_full_match_count == 0
    assert review.exact_minus_last_match_count == 0
    assert review.prefix_extension_count == 1
    assert review.message_delta_counter[2] == 1
    assert review.message_count_trace == (2, 4)


def test_prefix_extension_ignores_moving_cache_control_marker() -> None:
    """Ignore cache-control marker movement when comparing reusable prefixes."""
    module = _load_prompt_cache_review_module()
    rows = [
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-8",
            system_prompt="S",
            message_count=1,
            message_blobs=('{"content":[{"text":"a","cache_control":{"type":"ephemeral"}}],"role":"user"}',),
            normalized_message_blobs=('{"content":[{"text":"a"}],"role":"user"}',),
            preview="first",
        ),
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:05-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-8",
            system_prompt="S",
            message_count=2,
            message_blobs=(
                '{"content":[{"text":"a"}],"role":"user"}',
                '{"content":[{"text":"b","cache_control":{"type":"ephemeral"}}],"role":"assistant"}',
            ),
            normalized_message_blobs=(
                '{"content":[{"text":"a"}],"role":"user"}',
                '{"content":[{"text":"b"}],"role":"assistant"}',
            ),
            preview="second",
        ),
    ]

    review = module.build_session_reviews(rows)[0]

    assert review.prefix_extension_count == 1


def test_raw_prefix_extension_detects_moving_cache_control_marker() -> None:
    """Show that raw provider blobs still change when cache markers move."""
    module = _load_prompt_cache_review_module()
    previous_row = module.RequestRow(
        timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
        session_id="room:$thread",
        room_id="room",
        agent_name="opus",
        model_id="claude-opus-4-8",
        system_prompt="S",
        message_count=1,
        message_blobs=('{"content":[{"text":"a","cache_control":{"type":"ephemeral"}}],"role":"user"}',),
        normalized_message_blobs=('{"content":[{"text":"a"}],"role":"user"}',),
        preview="first",
    )
    current_row = module.RequestRow(
        timestamp=datetime.fromisoformat("2026-04-11T11:00:05-07:00"),
        session_id="room:$thread",
        room_id="room",
        agent_name="opus",
        model_id="claude-opus-4-8",
        system_prompt="S",
        message_count=2,
        message_blobs=(
            '{"content":[{"text":"a"}],"role":"user"}',
            '{"content":[{"text":"b","cache_control":{"type":"ephemeral"}}],"role":"assistant"}',
        ),
        normalized_message_blobs=(
            '{"content":[{"text":"a"}],"role":"user"}',
            '{"content":[{"text":"b"}],"role":"assistant"}',
        ),
        preview="second",
    )

    assert module.current_extends_previous(previous_row, current_row) is True
    assert module.current_extends_previous_raw(previous_row, current_row) is False


def test_build_provider_message_blobs_from_messages_can_skip_cache_ladder() -> None:
    """Allow blob building without applying the prompt-cache ladder."""
    module = _load_prompt_cache_review_module()
    messages = [
        Message(role="system", content="System prompt"),
        Message(role="user", content="Current turn"),
    ]

    raw_blobs_plain, normalized_blobs_plain, preview_plain = module.build_provider_message_blobs_from_messages(
        messages,
        "claude-sonnet-4-6",
        {"cache_system_prompt": True, "extended_cache_time": True},
        apply_cache_ladder=False,
    )
    raw_blobs_hooked, normalized_blobs_hooked, preview_hooked = module.build_provider_message_blobs_from_messages(
        messages,
        "claude-sonnet-4-6",
        {"cache_system_prompt": True, "extended_cache_time": True},
        apply_cache_ladder=True,
    )

    assert raw_blobs_plain == ('{"content":[{"text":"Current turn","type":"text"}],"role":"user"}',)
    assert raw_blobs_hooked == (
        '{"content":[{"cache_control":{"ttl":"1h","type":"ephemeral"},"text":"Current turn","type":"text"}],"role":"user"}',
    )
    assert normalized_blobs_plain == normalized_blobs_hooked
    assert preview_plain == preview_hooked == "Current turn"


def _simulation_row(
    module: ModuleType,
    *,
    timestamp: str,
    blobs: tuple[str, ...],
    marked: frozenset[int],
    agent_name: str = "agent",
    model_id: str = "claude-opus-4-8",
    system_prompt: str = "S" * 40,
    tools_blob: str = "T" * 40,
    cache_enabled: bool = True,
) -> object:
    """Build a synthetic request row with cache markers on the given message indexes."""
    raw_blobs = tuple(
        (
            f'{{"content":[{{"cache_control":{{"type":"ephemeral"}},"text":"{text}","type":"text"}}],"role":"user"}}'
            if index in marked
            else f'{{"content":[{{"text":"{text}","type":"text"}}],"role":"user"}}'
        )
        for index, text in enumerate(blobs)
    )
    normalized_blobs = tuple(f'{{"content":[{{"text":"{text}","type":"text"}}],"role":"user"}}' for text in blobs)
    return module.RequestRow(
        timestamp=datetime.fromisoformat(timestamp),
        session_id=None,
        room_id=None,
        agent_name=agent_name,
        model_id=model_id,
        system_prompt=system_prompt,
        message_count=len(blobs),
        message_blobs=raw_blobs,
        normalized_message_blobs=normalized_blobs,
        preview="preview",
        tools_blob=tools_blob,
        cache_enabled=cache_enabled,
    )


def test_simulate_prompt_cache_full_hit_on_prefix_extension() -> None:
    """A request extending the previous rung boundary reads the full prefix."""
    module = _load_prompt_cache_review_module()
    rows = [
        _simulation_row(module, timestamp="2026-04-11T11:00:00-07:00", blobs=("m1",), marked=frozenset({0})),
        _simulation_row(module, timestamp="2026-04-11T11:05:00-07:00", blobs=("m1", "m2"), marked=frozenset({1})),
    ]

    report = module.simulate_prompt_cache(rows)

    assert [outcome.outcome for outcome in report.outcomes] == ["cold", "full_hit"]
    second = report.outcomes[1]
    expected_read = (
        len(second.row.tools_blob) + len(second.row.system_prompt) + len(second.row.normalized_message_blobs[0])
    )
    assert second.read_chars == expected_read
    assert second.divergence is None


def test_simulate_prompt_cache_attributes_tool_and_system_changes() -> None:
    """Tool-array changes miss entirely; system changes still read the tools entry."""
    module = _load_prompt_cache_review_module()
    rows = [
        _simulation_row(module, timestamp="2026-04-11T11:00:00-07:00", blobs=("m1",), marked=frozenset({0})),
        _simulation_row(
            module,
            timestamp="2026-04-11T11:00:30-07:00",
            blobs=("m1", "m2"),
            marked=frozenset({1}),
            system_prompt="different system",
        ),
        _simulation_row(
            module,
            timestamp="2026-04-11T11:05:00-07:00",
            blobs=("m1", "m2"),
            marked=frozenset({1}),
            tools_blob="different tools",
        ),
    ]

    report = module.simulate_prompt_cache(rows)

    by_timestamp = {outcome.row.timestamp.isoformat(): outcome for outcome in report.outcomes}
    system_change = by_timestamp["2026-04-11T11:00:30-07:00"]
    tool_change = by_timestamp["2026-04-11T11:05:00-07:00"]
    assert system_change.outcome == "tools_hit"
    assert system_change.divergence == "system"
    assert system_change.read_chars == len(system_change.row.tools_blob)
    assert tool_change.outcome == "miss"
    assert tool_change.divergence == "tools"


def test_simulate_prompt_cache_ttl_expiry_is_cold() -> None:
    """Entries older than the TTL cannot be read."""
    module = _load_prompt_cache_review_module()
    rows = [
        _simulation_row(module, timestamp="2026-04-11T11:00:00-07:00", blobs=("m1",), marked=frozenset({0})),
        _simulation_row(module, timestamp="2026-04-11T13:00:00-07:00", blobs=("m1", "m2"), marked=frozenset({1})),
    ]

    report = module.simulate_prompt_cache(rows, ttl_seconds=3600)

    assert [outcome.outcome for outcome in report.outcomes] == ["cold", "cold"]


def test_simulate_prompt_cache_lookback_window_downgrades_deep_boundaries() -> None:
    """A matched boundary beyond the lookback window only reads tools+system."""
    module = _load_prompt_cache_review_module()
    tail = tuple(f"m{index}" for index in range(2, 27))
    rows = [
        _simulation_row(module, timestamp="2026-04-11T11:00:00-07:00", blobs=("m1",), marked=frozenset({0})),
        _simulation_row(
            module,
            timestamp="2026-04-11T11:05:00-07:00",
            blobs=("m1", *tail),
            marked=frozenset({len(tail)}),
        ),
    ]

    report = module.simulate_prompt_cache(rows, lookback_blocks=20)

    second = report.outcomes[1]
    assert second.outcome == "system_hit"
    assert second.read_chars == len(second.row.tools_blob) + len(second.row.system_prompt)


def test_bootstrap_probe_environment_resolves_relative_adc_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve relative ADC paths from runtime configuration into env vars."""
    module = _load_prompt_cache_review_module()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    adc_path = config_dir / "secrets" / "adc.json"
    runtime_paths = module.RuntimePaths(
        config_path=config_dir / "config.yaml",
        config_dir=config_dir,
        env_path=config_dir / ".env",
        storage_root=tmp_path / "mindroom_data",
        process_env={},
        env_file_values={
            "GOOGLE_APPLICATION_CREDENTIALS": "secrets/adc.json",
            "ANTHROPIC_VERTEX_PROJECT_ID": "mindroom-test",
            "CLOUD_ML_REGION": "us-central1",
        },
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)

    module.bootstrap_probe_environment(runtime_paths)

    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(adc_path)
    assert os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] == "mindroom-test"
    assert os.environ["CLOUD_ML_REGION"] == "us-central1"
