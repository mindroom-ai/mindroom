"""Structured-log schema tests for compaction chunk sizing events."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from mindroom.history.compaction import _rewrite_working_session_for_compaction
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import seed_session
from tests.fakes.summary_model import summary_model as _summary_model
from tests.fakes.token_counter import fake_token_counter, quarter_tokens
from tests.history_helpers import (  # noqa: F401
    _ALL_HISTORY_SETTINGS,
    _completed_run,
    _make_config,
    _session,
    _summarizer,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.history.compaction import _CompactionRewriteResult
    from mindroom.llm.provider import ModelProvider
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession


async def _rewrite_with_summary_model(
    *,
    storage: ConversationStore,
    working_session: ConversationSession,
    summary_input_budget: int,
    summary_model: ModelProvider,
) -> _CompactionRewriteResult | None:
    return await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=working_session,
        working_session=working_session,
        summarizer=_summarizer(summary_model, summary_input_budget, session_id=working_session.session_id),
        runs=[run for run in working_session.runs or [] if run.run_id == "run-1"],
        before_persist=AsyncMock(),
        after_persist=AsyncMock(),
    )


def _single_event(logs: list[dict[str, object]], event: str) -> dict[str, object]:
    matches = [entry for entry in logs if entry["event"] == event]
    assert len(matches) == 1
    return matches[0]


def _assert_truthful_sizing_fields(
    entry: dict[str, object],
    *,
    estimate: int,
    budget_tokens: int,
) -> None:
    assert entry["summary_input_estimate"] == estimate
    assert entry["summary_input_budget_tokens"] == budget_tokens
    assert "estimated_input_tokens" not in entry
    assert "summary_input_budget" not in entry


@pytest.mark.asyncio
async def test_chunk_events_report_the_summary_input_as_the_summary_model_counts_it(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1")]))
    summary_inputs: list[str] = []

    async def record_summary(*, summary_input: str, **_kwargs: object) -> str:
        summary_inputs.append(summary_input)
        return "chunk summary"

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=record_summary),
        ),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=8_000,
            summary_model=_summary_model(token_counter=fake_token_counter()),
        )

    assert rewrite_result is not None
    for event in ("Compaction summary chunk request", "Compaction summary chunk completed"):
        _assert_truthful_sizing_fields(
            _single_event(logs, event),
            estimate=quarter_tokens(summary_inputs[0]),
            budget_tokens=8_000,
        )


@pytest.mark.asyncio
async def test_failed_event_uses_truthful_sizing_fields(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1")]))
    summary_inputs: list[str] = []

    async def fail_summary(*, summary_input: str, **_kwargs: object) -> str:
        summary_inputs.append(summary_input)
        msg = "provider exploded"
        raise RuntimeError(msg)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fail_summary),
        ),
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="provider exploded"),
    ):
        await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=8_000,
            summary_model=_summary_model(),
        )

    _assert_truthful_sizing_fields(
        _single_event(logs, "Compaction summary chunk failed"),
        estimate=quarter_tokens(summary_inputs[0]),
        budget_tokens=8_000,
    )


@pytest.mark.asyncio
async def test_no_run_fit_warning_uses_token_budget_field(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1")]))

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock()),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=1,
            summary_model=_summary_model(),
        )

    assert rewrite_result is None
    event = _single_event(logs, "Compaction skipped because no run fit the single-pass summary budget")
    assert event["summary_input_budget_tokens"] == 1
    assert "summary_input_budget" not in event
    assert "estimated_input_tokens" not in event
