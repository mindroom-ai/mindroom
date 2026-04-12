"""Tests for the prompt-cache review helper script."""

from __future__ import annotations

# ruff: noqa: D103
import importlib.util
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
    module = _load_prompt_cache_review_module()
    jsonl_path = tmp_path / "requests.jsonl"
    jsonl_path.write_text(
        '{"timestamp":"2026-04-11T11:00:00-07:00","agent_name":"opus","model_id":"claude-opus-4-6","system_prompt":"S","messages":[{"role":"user","content":"a"}],"message_count":1}'
        '{"timestamp":"2026-04-11T11:00:01-07:00","agent_name":"opus","model_id":"claude-opus-4-6","system_prompt":"S","messages":[{"role":"user","content":"b"}],"message_count":1}\n',
        encoding="utf-8",
    )

    rows, stats = module.load_request_rows(jsonl_path)

    assert len(rows) == 2
    assert stats.document_count == 2
    assert stats.concatenated_document_count == 1
    assert stats.decode_error_count == 0


def test_build_session_reviews_detects_prefix_extension_with_two_appended_messages() -> None:
    module = _load_prompt_cache_review_module()
    rows = [
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-6",
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
            model_id="claude-opus-4-6",
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
    module = _load_prompt_cache_review_module()
    rows = [
        module.RequestRow(
            timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
            session_id="room:$thread",
            room_id="room",
            agent_name="opus",
            model_id="claude-opus-4-6",
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
            model_id="claude-opus-4-6",
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
    module = _load_prompt_cache_review_module()
    previous_row = module.RequestRow(
        timestamp=datetime.fromisoformat("2026-04-11T11:00:00-07:00"),
        session_id="room:$thread",
        room_id="room",
        agent_name="opus",
        model_id="claude-opus-4-6",
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
        model_id="claude-opus-4-6",
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


def test_build_provider_message_blobs_from_messages_can_skip_vertex_breakpoint() -> None:
    module = _load_prompt_cache_review_module()
    messages = [
        Message(role="system", content="System prompt"),
        Message(role="user", content="Current turn"),
    ]

    raw_blobs_plain, normalized_blobs_plain, preview_plain = module.build_provider_message_blobs_from_messages(
        messages,
        "claude-sonnet-4-6",
        {"cache_system_prompt": True, "extended_cache_time": True},
        apply_vertex_cache_breakpoint=False,
    )
    raw_blobs_hooked, normalized_blobs_hooked, preview_hooked = module.build_provider_message_blobs_from_messages(
        messages,
        "claude-sonnet-4-6",
        {"cache_system_prompt": True, "extended_cache_time": True},
        apply_vertex_cache_breakpoint=True,
    )

    assert raw_blobs_plain == ('{"content":[{"text":"Current turn","type":"text"}],"role":"user"}',)
    assert raw_blobs_hooked == (
        '{"content":[{"cache_control":{"ttl":"1h","type":"ephemeral"},"text":"Current turn","type":"text"}],"role":"user"}',
    )
    assert normalized_blobs_plain == normalized_blobs_hooked
    assert preview_plain == preview_hooked == "Current turn"


def test_bootstrap_probe_environment_resolves_relative_adc_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
