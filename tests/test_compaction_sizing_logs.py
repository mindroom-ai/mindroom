"""Structured-log schema tests for compaction chunk sizing events."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import tiktoken
from agno.models.openai.chat import OpenAIChat
from agno.session.summary import SessionSummary
from structlog.testing import capture_logs

from mindroom.agent_storage import create_session_storage
from mindroom.history.compaction import _rewrite_working_session_for_compaction
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from tests.conftest import FakeModel
from tests.history_helpers import (  # noqa: F401
    _ALL_HISTORY_SETTINGS,
    _close_test_storages,
    _completed_run,
    _make_config,
    _session,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.session.agent import AgentSession

    from mindroom.history.compaction import _CompactionRewriteResult


async def _rewrite_with_summary_model(
    *,
    storage: BaseDb,
    working_session: AgentSession,
    summary_input_budget: int,
    summary_model: Model | None = None,
) -> _CompactionRewriteResult | None:
    return await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=working_session,
        working_session=working_session,
        summary_model=summary_model or FakeModel(id="claude-sonnet-5", provider="fake"),
        summary_model_name="summary-model",
        session_id=working_session.session_id,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        state=HistoryScopeState(force_compact_before_next_run=True),
        history_settings=_ALL_HISTORY_SETTINGS,
        available_history_budget=None,
        selected_run_ids=("run-1",),
        summary_input_budget=summary_input_budget,
        before_tokens=0,
        runs_before=len(working_session.runs or []),
        threshold_tokens=None,
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        lifecycle_notice_event_id=None,
        progress_callback=None,
        collect_compaction_hook_messages=False,
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
    expected_kind: str = "utf8_bytes_token_upper_bound",
) -> None:
    assert entry["summary_input_estimate"] == estimate
    assert entry["summary_input_estimate_kind"] == expected_kind
    assert entry["summary_input_budget_tokens"] == budget_tokens
    assert "estimated_input_tokens" not in entry
    assert "summary_input_budget" not in entry


def _expected_estimate(payload: str, expected_kind: str) -> int:
    if expected_kind == "model_tiktoken_tokens":
        return len(tiktoken.encoding_for_model("gpt-4o").encode(payload, disallowed_special=()))
    return len(payload.encode("utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary_model_factory", "expected_kind"),
    [
        pytest.param(
            lambda: FakeModel(id="claude-sonnet-5", provider="fake"),
            "utf8_bytes_token_upper_bound",
            id="claude",
        ),
        # A tiktoken-recognized id on a non-OpenAI endpoint must stay on the
        # byte bound: the id alone does not identify the serving tokenizer.
        pytest.param(
            lambda: FakeModel(id="gpt-4o", provider="fake"),
            "utf8_bytes_token_upper_bound",
            id="custom-endpoint-openai-alias",
        ),
        pytest.param(
            lambda: OpenAIChat(id="gpt-4o"),
            "model_tiktoken_tokens",
            id="genuine-openai",
        ),
    ],
)
async def test_chunk_request_and_completed_events_use_truthful_sizing_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_model_factory: Callable[[], Model],
    expected_kind: str,
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_inputs: list[str] = []

    async def record_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return SessionSummary(summary="chunk summary", updated_at=datetime.now(UTC))

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
            summary_model=summary_model_factory(),
        )

    assert rewrite_result is not None
    expected_estimate = _expected_estimate(summary_inputs[0], expected_kind)
    request_event = _single_event(logs, "Compaction summary chunk request")
    completed_event = _single_event(logs, "Compaction summary chunk completed")
    _assert_truthful_sizing_fields(
        request_event,
        estimate=expected_estimate,
        budget_tokens=8_000,
        expected_kind=expected_kind,
    )
    _assert_truthful_sizing_fields(
        completed_event,
        estimate=expected_estimate,
        budget_tokens=8_000,
        expected_kind=expected_kind,
    )


@pytest.mark.asyncio
async def test_logged_kind_stays_frozen_when_env_flips_mid_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin (ISSUE-246 fix round 3): the logged kind describes the frozen estimator.

    The endpoint flag is resolved once next to the estimator partial, so an
    OPENAI_BASE_URL appearing mid-compaction must not relabel the arithmetic
    the frozen estimator actually used.
    """
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_inputs: list[str] = []

    async def record_summary_and_flip_env(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9292/v1")
        return SessionSummary(summary="chunk summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=record_summary_and_flip_env),
        ),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=8_000,
            summary_model=OpenAIChat(id="gpt-4o"),
        )

    assert rewrite_result is not None
    expected_estimate = _expected_estimate(summary_inputs[0], "model_tiktoken_tokens")
    completed_event = _single_event(logs, "Compaction summary chunk completed")
    _assert_truthful_sizing_fields(
        completed_event,
        estimate=expected_estimate,
        budget_tokens=8_000,
        expected_kind="model_tiktoken_tokens",
    )


@pytest.mark.asyncio
async def test_chunk_failed_event_uses_truthful_sizing_fields(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_inputs: list[str] = []

    async def fail_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
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
        )

    failed_event = _single_event(logs, "Compaction summary chunk failed")
    _assert_truthful_sizing_fields(
        failed_event,
        estimate=len(summary_inputs[0].encode("utf-8")),
        budget_tokens=8_000,
    )


@pytest.mark.asyncio
async def test_no_run_fit_warning_uses_renamed_budget_field(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock()),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=1,
        )

    assert rewrite_result is None
    warning_event = _single_event(logs, "Compaction skipped because no run fit the single-pass summary budget")
    assert warning_event["summary_input_budget_tokens"] == 1
    assert "summary_input_budget" not in warning_event
    assert "estimated_input_tokens" not in warning_event
    assert "summary_input_estimate" not in warning_event
