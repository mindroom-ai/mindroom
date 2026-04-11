"""Tests for full LLM request logging."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from agno.models.message import Message, MessageMetrics

from mindroom.config.main import Config
from mindroom.config.models import DebugConfig
from mindroom.llm_request_logging import install_llm_request_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakeModel:
    id: str = "test-model"
    system_prompt: str | None = None
    temperature: float | None = 0.7
    client: object | None = None
    async_client: object | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "ok"}

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[dict[str, str]]:
        yield {"status": "ok"}


def _read_log_entries(log_dir: Path) -> list[dict[str, Any]]:
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    assert len(log_files) == 1
    return [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]


def test_debug_config_parses() -> None:
    """Debug config should parse both explicit and default request logging settings."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "debug": {"log_llm_requests": True, "llm_request_log_dir": "custom-logs"},
        },
    )
    assert config.debug == DebugConfig(log_llm_requests=True, llm_request_log_dir="custom-logs")
    assert (
        Config.model_validate({"models": {"default": {"provider": "openai", "id": "test-model"}}}).debug
        == DebugConfig()
    )


@pytest.mark.asyncio
async def test_llm_request_logging_writes_jsonl(tmp_path: Path) -> None:
    """Enabled request logging should emit one full JSONL entry per invoke path."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="assistant",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    messages = [
        Message(role="system", content="s" * 600, created_at=111),
        Message(
            role="user",
            content="hello",
            created_at=222,
            metrics=MessageMetrics(input_tokens=2, total_tokens=2, duration=1.5),
        ),
    ]
    assistant_message = Message(role="assistant")

    result = await model.ainvoke(messages=messages, assistant_message=assistant_message, tools=[{"name": "search"}])
    assert result == {"status": "ok"}

    streamed = [
        chunk async for chunk in model.ainvoke_stream(messages=messages, assistant_message=assistant_message, tools=[])
    ]
    assert streamed == [{"status": "ok"}]

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 2
    assert entries[0]["agent_name"] == "assistant"
    assert entries[0]["model_id"] == "test-model"
    assert entries[0]["system_prompt"] == "s" * 600
    assert entries[0]["messages"][0]["role"] == "system"
    assert entries[0]["messages"][0]["content"] == "s" * 600
    assert entries[0]["messages"][0]["created_at"] == 111
    assert entries[0]["messages"][1]["role"] == "user"
    assert entries[0]["messages"][1]["content"] == "hello"
    assert entries[0]["messages"][1]["created_at"] == 222
    assert entries[0]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["total_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["duration"] == 1.5
    assert entries[0]["message_count"] == 2
    assert entries[0]["tools"] == [{"name": "search"}]
    assert entries[0]["tool_count"] == 1
    assert entries[0]["model_params"] == {"temperature": 0.7}
    assert "timestamp" in entries[0]
    assert entries[1]["messages"][0]["created_at"] == 111
    assert entries[1]["messages"][1]["created_at"] == 222
    assert entries[1]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[1]["tools"] == []
    assert entries[1]["tool_count"] == 0


@pytest.mark.asyncio
async def test_llm_request_logging_disabled_creates_no_file(tmp_path: Path) -> None:
    """Disabled request logging should leave the target log directory untouched."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="assistant",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )
    await model.ainvoke(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    assert list(tmp_path.iterdir()) == []
