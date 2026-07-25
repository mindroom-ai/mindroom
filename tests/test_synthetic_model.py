"""Tests for the deterministic built-in synthetic model."""

# Test names state behavior more precisely than repeated one-line docstrings.
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse, ModelResponseEvent
from agno.tools.sleep import SleepTools

from mindroom.synthetic_model import SyntheticModel

if TYPE_CHECKING:
    from pathlib import Path


def _model(**changes: object) -> SyntheticModel:
    defaults: dict[str, object] = {
        "id": "synthetic",
        "seed": 17,
        "min_response_chars": 128,
        "max_response_chars": 256,
        "chunk_chars": 17,
        "chars_per_second": 0,
        "identity_pattern": r"request-\d+",
    }
    return SyntheticModel(**(defaults | changes))


def _sleep_function() -> object:
    return SleepTools().functions["sleep"]


def test_plan_is_exact_seeded_and_fixed_length() -> None:
    model = _model(min_response_chars=200, max_response_chars=200)

    first = model.plan_for_prompt("noise request-42 more noise")
    second = model.plan_for_prompt("request-42")
    different = model.plan_for_prompt("request-43")

    assert first == second
    assert first != different
    assert len(first.body) == 200
    assert first.body.startswith(f"SYNTHETIC[{first.request_id}] ")
    assert first.body.endswith(f" COMPLETE[{first.request_id}]")


def test_activation_pattern_keeps_setup_requests_fast_and_tool_free() -> None:
    model = _model(
        activation_pattern=r"request-\d+",
        barrier_size=2,
        barrier_group_pattern=r"(setup)",
        barrier_timeout_seconds=0.01,
        tool_call_probability=1,
    )

    plan = model.plan_for_prompt("setup request", tool_available=True)
    responses = list(
        model.invoke_stream(
            [Message(role="user", content="setup request")],
            tools=[{"name": "sleep"}],
        ),
    )

    assert plan.body == "SYNTHETIC READY"
    assert plan.tool_call_id is None
    assert [response.content for response in responses] == ["SYNTHETIC READY"]


def test_coordination_key_shares_barrier_across_fresh_models(tmp_path: Path) -> None:
    settings: dict[str, object] = {
        "coordination_key": str(tmp_path),
        "barrier_size": 2,
        "barrier_group_pattern": r"wave=(\d+)",
        "barrier_timeout_seconds": 2,
    }
    models = (_model(**settings), _model(**settings))
    failures: list[BaseException] = []

    def run(model: SyntheticModel, request: int) -> None:
        try:
            list(
                model.invoke_stream(
                    [Message(role="user", content=f"wave=0 request-{request}")],
                ),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(model, request)) for request, model in enumerate(models)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert not any(thread.is_alive() for thread in threads)


def test_stream_runs_real_sleep_tool_then_continues_exact_body() -> None:
    model = _model(
        tool_call_probability=1,
        min_sleep_seconds=0,
        max_sleep_seconds=0,
    )
    messages = [Message(role="user", content="request-7")]
    expected = model.plan_for_prompt("request-7").body
    responses = list(
        model.response_stream(
            messages,
            tools=[_sleep_function()],
        ),
    )

    assistant_content = "".join(
        response.content
        for response in responses
        if isinstance(response, ModelResponse)
        and response.event == ModelResponseEvent.assistant_response.value
        and isinstance(response.content, str)
    )
    completed_tools = [
        execution
        for response in responses
        if isinstance(response, ModelResponse) and response.event == ModelResponseEvent.tool_call_completed.value
        for execution in response.tool_executions or ()
    ]

    assert assistant_content == expected
    assert [execution.tool_name for execution in completed_tools] == ["sleep"]
    assert [execution.tool_args for execution in completed_tools] == [{"seconds": 0}]
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]


def test_missing_tool_returns_complete_text_without_fake_call() -> None:
    model = _model(tool_call_probability=1)
    messages = [Message(role="user", content="request-8")]

    responses = list(model.invoke_stream(messages, tools=[]))

    assert (
        "".join(response.content or "" for response in responses)
        == model.plan_for_prompt(
            "request-8",
            tool_available=False,
        ).body
    )
    assert not any(response.tool_calls for response in responses)


@pytest.mark.asyncio
async def test_async_barrier_releases_one_group_together(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "synthetic.jsonl"
    model = _model(
        min_response_chars=64,
        max_response_chars=64,
        barrier_size=4,
        barrier_group_pattern=r"wave=(\d+)",
        barrier_timeout_seconds=1,
        telemetry_path=str(telemetry_path),
    )

    async def run(thread: int) -> str:
        messages = [Message(role="user", content=f"request-{thread} wave=3")]
        chunks = [response.content or "" async for response in model.ainvoke_stream(messages, tools=[])]
        return "".join(chunks)

    results = await asyncio.gather(*(run(thread) for thread in range(4)))
    telemetry = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines()]

    assert all(result for result in results)
    assert [event["kind"] for event in telemetry].count("barrier_reached") == 4
    assert {event["group"] for event in telemetry} == {"3"}
    assert all("request-" not in line for line in telemetry_path.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"min_response_chars": 63}, "at least 64"),
        ({"min_response_chars": 100, "max_response_chars": 99}, "greater than or equal"),
        ({"chunk_chars": 0}, "chunk_chars must be positive"),
        ({"chars_per_second": -1}, "chars_per_second must be non-negative"),
        ({"tool_call_probability": 1.1}, "between 0 and 1"),
        ({"min_sleep_seconds": -1}, "must be non-negative"),
        ({"min_sleep_seconds": 2, "max_sleep_seconds": 1}, "greater than or equal"),
        ({"barrier_size": 2}, "barrier_group_pattern is required"),
    ],
)
def test_invalid_settings_fail_at_construction(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_model(), **changes)
