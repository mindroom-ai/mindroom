"""Tests for compaction config/plan resolution, decision classification, and replay planning."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.history.compaction import _compaction_fallback_is_distinct
from mindroom.history.policy import (
    classify_compaction_decision,
    context_budget_after_reserve,
    resolve_history_execution_plan,
)
from mindroom.history.replay import (
    HistorySummaryBudgetError,
    estimate_prompt_visible_history_tokens,
    plan_replay_that_fits,
)
from mindroom.history.request_messages import TurnMessages
from mindroom.history.request_size import RequestFrame, request_sizer
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import (
    read_scope_state,
    with_force_compaction,
)
from mindroom.history.types import (
    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
    HistoryPolicy,
    HistoryScope,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
    ScopeSummary,
)
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, NativeCompactionReplay
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS
from mindroom.turn_model import ModelScope, resolve_turn_model
from tests.conftest import bind_runtime_paths, install_model_providers, prepare_history_for_run_for_test, seed_session
from tests.fakes.scripted_model import ScriptedModel, estimate_only_model
from tests.fakes.summary_model import summary_model
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import (
    _completed_run,
    _make_config,
    _runtime_paths,
    _session,
    folded_run_ids,
    replay_settings,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _agent_settings(config: Config) -> ResolvedHistorySettings:
    """Return the test agent's resolved history settings, which hold its compaction config."""
    return config.resolve_scope("test_agent").history_settings


def _viewed_image_message(index: int) -> Message:
    return Message(
        role="user",
        content="The tool call above generated the attached media.",
        images=[Image(id=f"mindroom_viewed_{index}", content=f"image-{index}".encode(), mime_type="image/png")],
    )


@dataclass
class _RecordingModel(ScriptedModel):
    """A fake provider that keeps every request it sized."""

    sized: list[ModelRequest] = field(default_factory=list)

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        self.sized.append(request)
        return super().request_tokens(request, start=start)


_NATIVE = NativeCompactionReplay(route="native-route", threshold=None)


def test_a_native_route_without_a_checkpoint_estimates_text_as_a_plain_request_does() -> None:
    """One history gets one estimate: a native route rounds its canonical text the way a plain request does."""
    session = _session(
        "session-1",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="a") for _ in range(4)])],
    )
    estimate = partial(
        estimate_prompt_visible_history_tokens,
        session=session,
        history_settings=replay_settings(),
        model=ScriptedModel([]),
    )

    assert estimate(native=_NATIVE) == 1
    assert estimate() == 1


def test_an_empty_media_list_adds_nothing_to_a_message_estimate() -> None:
    """A message estimates by its text and tool calls; an empty media list carries neither."""
    with_empty_media = Message(role="user", content="abcdefg", images=[], files=[])

    assert ScriptedModel([]).request_tokens(ModelRequest(messages=[with_empty_media])) == 1


@pytest.mark.parametrize("native", [None, _NATIVE])
def test_history_estimate_projects_bounded_viewed_images_without_mutating_session(
    native: NativeCompactionReplay | None,
) -> None:
    """The planner sizes the same bounded newest-first media replay a request sends."""
    messages = [_viewed_image_message(index) for index in range(6)]
    session = _session("viewed-images", runs=[_completed_run("run-1", messages=messages)])
    before = deepcopy(session)
    model = _RecordingModel([])

    estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=replay_settings(),
        model=model,
        native=native,
    )

    [request] = model.sized
    replayed_ids = [image.id for message in request.messages for image in message.images or []]
    assert replayed_ids == ["mindroom_viewed_2", "mindroom_viewed_3", "mindroom_viewed_4", "mindroom_viewed_5"]
    assert sum("omitted from replay" in str(message.content) for message in request.messages) == 2
    assert session == before


@pytest.mark.parametrize("native", [None, _NATIVE])
def test_history_estimate_charges_conservative_visual_cost(native: NativeCompactionReplay | None) -> None:
    """Planning charges each replayed image its allowance without treating image bytes as prompt text."""
    settings = replay_settings()
    without_image = _session(
        "without-image",
        runs=[
            _completed_run(
                "run-1",
                messages=[Message(role="user", content="The tool call above generated the attached media.")],
            ),
        ],
    )
    with_image = _session(
        "with-image",
        runs=[_completed_run("run-1", messages=[_viewed_image_message(1)])],
    )
    estimate = partial(estimate_prompt_visible_history_tokens, history_settings=settings, model=ScriptedModel([]))

    text_tokens = estimate(session=without_image, native=native)
    visual_tokens = estimate(session=with_image, native=native)

    assert visual_tokens - text_tokens == MEDIA_ALLOWANCE_TOKENS


@pytest.mark.asyncio
async def test_prepare_history_for_run_authored_compaction_still_plans_safe_replay_when_compaction_unavailable(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=600,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


def test_small_replay_window_does_not_cap_compaction_model_input(tmp_path: Path) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, replay_window_tokens=1),
        context_window=1_000_000,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=1_000_000,
    )

    assert execution_plan.replay_window_tokens == 1
    assert execution_plan.summary_input_budget_tokens == 881_616
    assert execution_plan.text_compaction_available is True
    assert execution_plan.unavailable_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("compaction_disabled", [False, True], ids=["unauthored", "disabled"])
async def test_prepare_history_for_run_without_active_compaction_and_no_window_skips_warning(
    tmp_path: Path,
    *,
    compaction_disabled: bool,
) -> None:
    if compaction_disabled:
        config, runtime_paths = _make_config(
            tmp_path,
            compaction=CompactionOverrideConfig(enabled=False),
            context_window=None,
        )
    else:
        config, runtime_paths = _make_config(tmp_path, context_window=None)
        config.defaults.compaction = None
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    seed_session(storage, session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replays_persisted_history is True
    assert mock_warning.call_args_list == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_warns_once_when_authored_compaction_is_unavailable(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    seed_session(storage, session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
        await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(mock_warning.call_args_list) == 1


def test_resolved_compaction_config_merges_authored_overrides(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(
                        threshold_percent=0.6,
                        timeout_seconds=75.0,
                    ),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=12_000,
                    reserve_tokens=2_048,
                    model="summary-model",
                    timeout_seconds=420.0,
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    resolved = _agent_settings(config).compaction_config

    assert resolved.enabled is True
    assert resolved.threshold_tokens is None
    assert resolved.threshold_percent == 0.6
    assert resolved.reserve_tokens == 2_048
    assert resolved.model == "summary-model"
    assert resolved.timeout_seconds == 75.0
    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=48_000,
    )
    assert execution_plan.compaction_timeout_seconds == 75.0


def test_compaction_timeout_defaults_to_ten_minutes_and_must_be_positive() -> None:
    assert CompactionConfig().timeout_seconds == 600.0

    with pytest.raises(ValueError, match="greater than 0"):
        CompactionConfig(timeout_seconds=0)

    with pytest.raises(ValueError, match="greater than 0"):
        CompactionOverrideConfig(timeout_seconds=-1)


@pytest.mark.parametrize(
    ("defaults", "agent", "automatic"),
    [
        pytest.param({}, {}, True, id="omitted-defaults"),
        pytest.param({"compaction": {}}, {}, True, id="empty-defaults"),
        pytest.param({"compaction": {"enabled": False}}, {"compaction": {}}, False, id="disabled-defaults"),
        pytest.param({"compaction": None}, {}, False, id="null-defaults"),
    ],
)
def test_only_an_authored_enabled_compaction_config_compacts_automatically(
    tmp_path: Path,
    defaults: dict[str, object],
    agent: dict[str, object],
    *,
    automatic: bool,
) -> None:
    """The settings own whether compaction runs on its own, and the plan carries their answer.

    ``defaults.compaction: null`` leaves the built-in config, which is enabled but authored by nobody, so like
    ``enabled: false`` it has no trigger and keeps ``reserve_tokens`` free for the reply. On this 48k window, fitting
    requests to the 80% trigger instead would let a request reach 38,400 tokens and leave 9,600 for the reply, below
    the 16,384 reserve. With automatic compaction the trigger is capped at the limit for the same reason.
    """
    config = Config.validate_with_runtime(
        {
            "agents": {"test_agent": {"display_name": "Test Agent", **agent}},
            "defaults": {"tools": [], **defaults},
            "models": {"default": {"provider": "openai", "id": "test-model", "context_window": 48_000}},
        },
        _runtime_paths(tmp_path),
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=48_000,
    )

    assert _agent_settings(config).automatic_compaction is automatic
    assert execution_plan.automatic_compaction is automatic
    assert execution_plan.trigger_tokens == execution_plan.limit_tokens == 48_000 - 16_384
    assert execution_plan.trigger_threshold_tokens == (38_400 if automatic else None)


def test_validate_compaction_model_references_does_not_emit_availability_warnings(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.config.main.logger.warning") as mock_warning:
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )

    assert mock_warning.call_args_list == []


def test_validate_compaction_model_references_rejects_explicit_model_without_context_window(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Explicit compaction model references require a model with context_window: agents\.test_agent\.compaction\.model -> summary-model",
    ):
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True, model="summary-model"),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=48_000,
                    ),
                    "summary-model": ModelConfig(
                        provider="openai",
                        id="summary-model-id",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )


def test_validate_compaction_model_references_rejects_disabled_explicit_model_without_context_window(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Explicit compaction model references require a model with context_window",
    ):
        bind_runtime_paths(
            Config(
                defaults=DefaultsConfig(
                    tools=[],
                    compaction=CompactionConfig(
                        enabled=False,
                        model="summary-model",
                    ),
                ),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=48_000,
                    ),
                    "summary-model": ModelConfig(
                        provider="openai",
                        id="summary-model-id",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )


def test_authored_model_dump_preserves_explicit_compaction_model_clear(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(enabled=True, model=None),
                ),
            },
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
            },
        ),
        runtime_paths,
    )

    assert config.authored_model_dump()["agents"]["test_agent"]["compaction"] == {
        "enabled": True,
        "model": None,
    }


def test_resolved_compaction_config_inherits_disabled_defaults_for_pure_model_clear(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(model=None),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    model="summary-model",
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    compaction_config = _agent_settings(config).compaction_config

    assert compaction_config.enabled is False
    assert compaction_config.model is None


def test_validate_compaction_fallback_model_rejects_unknown_model(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Compaction model references unknown models: agents\.test_agent\.compaction\.fallback_model -> missing-model",
    ):
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(fallback_model="missing-model"),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
                },
            ),
            runtime_paths,
        )


def test_validate_compaction_fallback_model_rejects_missing_context_window(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"context_window: defaults\.compaction\.fallback_model -> fallback-model",
    ):
        bind_runtime_paths(
            Config(
                defaults=DefaultsConfig(
                    tools=[],
                    compaction=CompactionConfig(fallback_model="fallback-model"),
                ),
                models={
                    "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
                    "fallback-model": ModelConfig(provider="openai", id="fallback-model-id", context_window=None),
                },
            ),
            runtime_paths,
        )


def _fallback_inheritance_config(tmp_path: Path, *, override: CompactionOverrideConfig) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", compaction=override),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    model="summary-model",
                    fallback_model="fallback-model",
                ),
            ),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
                "summary-model": ModelConfig(provider="openai", id="summary-model-id", context_window=32_000),
                "fallback-model": ModelConfig(provider="openai", id="fallback-model-id", context_window=32_000),
            },
        ),
        _runtime_paths(tmp_path),
    )


def test_resolved_compaction_config_inherits_fallback_model_from_defaults(tmp_path: Path) -> None:
    config = _fallback_inheritance_config(tmp_path, override=CompactionOverrideConfig(enabled=True))

    compaction_config = _agent_settings(config).compaction_config

    assert compaction_config.enabled is True
    assert compaction_config.model == "summary-model"
    assert compaction_config.fallback_model == "fallback-model"


def test_resolved_compaction_config_inherits_disabled_defaults_for_pure_model_field_clears(tmp_path: Path) -> None:
    """Clearing only inherited model fields must not statically enable compaction."""
    for override in (
        CompactionOverrideConfig(fallback_model=None),
        CompactionOverrideConfig(model=None, fallback_model=None),
    ):
        config = _fallback_inheritance_config(tmp_path, override=override)

        compaction_config = _agent_settings(config).compaction_config

        assert compaction_config.enabled is False
        assert compaction_config.fallback_model is None


def test_resolve_history_execution_plan_carries_fallback_model_name(tmp_path: Path) -> None:
    config = _fallback_inheritance_config(tmp_path, override=CompactionOverrideConfig(enabled=True))

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=48_000,
    )

    assert execution_plan.compaction_model_name == "summary-model"
    assert execution_plan.compaction_fallback_model_name == "fallback-model"
    assert execution_plan.compaction_fallback_summary_input_budget_tokens == 10_800


def test_resolve_history_execution_plan_uses_each_compaction_model_window_for_summary_budget(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    model="summary-model",
                    fallback_model="fallback-model",
                    replay_window_tokens=200_000,
                ),
            ),
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=1_000_000),
                "summary-model": ModelConfig(provider="openai", id="summary-model-id", context_window=1_000_000),
                "fallback-model": ModelConfig(provider="openai", id="fallback-model-id", context_window=200_000),
            },
        ),
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=1_000_000,
    )

    assert execution_plan.replay_window_tokens == 200_000
    assert execution_plan.summary_input_budget_tokens == 881_616
    assert execution_plan.compaction_fallback_summary_input_budget_tokens == 161_616


def test_compaction_fallback_is_distinct_guards_same_alias_and_same_target(tmp_path: Path) -> None:
    """A fallback naming the primary alias or the same canonical (provider, id) target is never loaded."""
    config = bind_runtime_paths(
        Config(
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
                "summary-model": ModelConfig(provider="openai", id="summary-model-id", context_window=32_000),
                "summary-model-alias": ModelConfig(provider="openai", id="summary-model-id", context_window=32_000),
                "fallback-model": ModelConfig(provider="anthropic", id="summary-model-id", context_window=32_000),
                "vertex-summary": ModelConfig(
                    provider="vertexai_claude",
                    id="claude-sonnet-5",
                    context_window=32_000,
                ),
                # Same serving model as vertex-summary; the provider spelling
                # differs only by hyphenation and case, which model loading
                # canonicalizes away.
                "vertex-summary-spelling-variant": ModelConfig(
                    provider="Vertexai-Claude",
                    id="claude-sonnet-5",
                    context_window=32_000,
                ),
                # ``openai_codex`` is an alias of ``codex``, so both select the same route.
                "codex-summary": ModelConfig(provider="codex", id="gpt-6.1-sol", context_window=32_000),
                "codex-summary-alias": ModelConfig(provider="openai_codex", id="gpt-6.1-sol", context_window=32_000),
            },
        ),
        _runtime_paths(tmp_path),
    )

    with patch("mindroom.history.compaction.logger.warning") as warning_mock:
        assert (
            _compaction_fallback_is_distinct(
                config,
                primary_model_name="summary-model",
                fallback_model_name="summary-model",
            )
            is False
        )
        assert (
            _compaction_fallback_is_distinct(
                config,
                primary_model_name="summary-model",
                fallback_model_name="summary-model-alias",
            )
            is False
        )
        assert (
            _compaction_fallback_is_distinct(
                config,
                primary_model_name="vertex-summary",
                fallback_model_name="vertex-summary-spelling-variant",
            )
            is False
        )
        assert (
            _compaction_fallback_is_distinct(
                config,
                primary_model_name="codex-summary",
                fallback_model_name="codex-summary-alias",
            )
            is False
        )
        assert (
            _compaction_fallback_is_distinct(
                config,
                primary_model_name="summary-model",
                fallback_model_name="fallback-model",
            )
            is True
        )

    assert warning_mock.call_count == 4
    assert all(
        call.args[0] == "Compaction fallback resolves to the primary serving model; continuing without a fallback"
        for call in warning_mock.call_args_list
    )
    assert warning_mock.call_args_list[0].kwargs == {
        "compaction_model": "summary-model",
        "fallback_model": "summary-model",
    }


def test_resolve_history_execution_plan_uses_compaction_model_window_only_for_summary_budget(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(
                        model="summary-model",
                        replay_window_tokens=20_000,
                    ),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=None,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=None,
    )

    # The summary model's 32,000 window, half of it reserved, minus prompt overhead and a 10% margin.
    assert execution_plan.summary_input_budget_tokens == 10_800
    assert execution_plan.replay_window_tokens == 20_000
    assert execution_plan.trigger_threshold_tokens == 16_000
    # The 20,000-token replay window minus its reserve, clamped to half the window, caps the 16,000 trigger.
    assert execution_plan.trigger_tokens == 10_000
    assert execution_plan.limit_tokens == 10_000
    assert execution_plan.text_compaction_available is True

    # A request of a 2,000-token static prompt and 8,001 tokens of history.
    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=False,
        request_tokens=10_001,
        summary_request_tokens=2_000,
        allow_native_compaction=True,
    )
    assert decision.mode == "required"
    assert decision.reason == "request_exceeds_limit"


def test_resolve_turn_model_uses_room_override_for_team(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            teams={
                "team_123": TeamConfig(
                    display_name="Test Team",
                    role="Coordinate work",
                    agents=["test_agent"],
                    model="default",
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            room_models={"!room:localhost": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        runtime_paths,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="team_123", room_id="!room:localhost"),
        None,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_turn_model_uses_room_override_for_agent(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"!room:localhost": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id="!room:localhost"),
        None,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 48_000


def test_resolve_history_execution_plan_marks_non_positive_summary_budget_unavailable(tmp_path: Path) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=4_096,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=4_096,
    )

    assert execution_plan.summary_input_budget_tokens == 0
    assert execution_plan.text_compaction_available is False
    assert execution_plan.unavailable_reason == "non_positive_summary_input_budget"


@pytest.mark.parametrize(
    ("context_window_tokens", "expected_summary_input_budget", "expected_available"),
    [
        (10_000, 2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS, False),
        (10_001, 2 * COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS + 1, True),
    ],
)
def test_resolve_history_execution_plan_enforces_minimum_summary_input_budget(
    tmp_path: Path,
    context_window_tokens: int,
    expected_summary_input_budget: int,
    expected_available: bool,
) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=context_window_tokens,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=context_window_tokens,
    )

    assert execution_plan.summary_input_budget_tokens == expected_summary_input_budget
    assert execution_plan.text_compaction_available is expected_available
    assert (execution_plan.unavailable_reason is None) is expected_available


@pytest.mark.parametrize(
    ("context_window_tokens", "reserve_tokens", "expected"),
    [
        (1_000, 100, 900),
        (1_000, 800, 500),
        (0, 100, 0),
        (-10, 5, 0),
    ],
)
def test_context_budget_after_reserve_preserves_replay_budget_bounds(
    context_window_tokens: int,
    reserve_tokens: int,
    expected: int,
) -> None:
    assert context_budget_after_reserve(context_window_tokens, reserve_tokens) == expected


def test_resolve_history_execution_plan_keeps_replay_headroom_when_compaction_disabled(
    tmp_path: Path,
) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(
            enabled=False,
            threshold_tokens=100,
        ),
        context_window=1_000,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=1_000,
    )

    assert execution_plan.trigger_threshold_tokens is None
    # Without automatic compaction a request fits the limit: the window minus its reserve, clamped to half the window.
    assert execution_plan.trigger_tokens == execution_plan.limit_tokens == 500


@pytest.mark.parametrize(
    ("active_context_window", "expected_replay_window", "expected_summary_input_budget"),
    [
        (1_000_000, 200_000, 881_616),
        (100_000, 100_000, 71_616),
    ],
)
def test_resolve_history_execution_plan_caps_replay_without_capping_summary_input(
    tmp_path: Path,
    active_context_window: int,
    expected_replay_window: int,
    expected_summary_input_budget: int,
) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(replay_window_tokens=200_000),
        context_window=active_context_window,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=active_context_window,
    )

    assert execution_plan.replay_window_tokens == expected_replay_window
    assert execution_plan.summary_input_budget_tokens == expected_summary_input_budget
    assert execution_plan.trigger_threshold_tokens == int(expected_replay_window * 0.8)
    # The trigger falls below the limit here, so a request compacts at the trigger.
    assert execution_plan.trigger_tokens == int(expected_replay_window * 0.8)
    limit = execution_plan.limit_tokens
    assert limit is not None
    assert limit == expected_replay_window - 16_384

    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=False,
        request_tokens=limit + 1,
        summary_request_tokens=10_000,
        allow_native_compaction=True,
    )
    assert decision.mode == "required"
    assert decision.reason == "request_exceeds_limit"


def test_classify_compaction_decision_forced_compaction_takes_priority() -> None:
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        trigger_tokens=12_000,
        limit_tokens=12_000,
        summary_input_budget_tokens=5_000,
        compaction_timeout_seconds=600.0,
    )

    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=True,
        request_tokens=7_000,
        summary_request_tokens=2_000,
        allow_native_compaction=True,
    )

    assert decision.mode == "required"
    assert decision.reason == "forced"


@pytest.mark.parametrize(
    ("request_tokens", "allow_native_compaction", "expected_mode", "expected_reason"),
    [
        (12_000, True, "none", "under_trigger"),
        (12_000, False, "none", "under_trigger"),
        (12_001, True, "none", "within_limit"),
        (12_001, False, "required", "over_trigger_native_disabled"),
        (22_001, True, "required", "request_exceeds_limit"),
        (22_001, False, "required", "request_exceeds_limit"),
    ],
)
def test_classify_compaction_decision_compacts_at_trigger_only_without_native_compaction(
    request_tokens: int,
    expected_mode: str,
    expected_reason: str,
    *,
    allow_native_compaction: bool,
) -> None:
    """Native compaction owns the range between the trigger and the limit, so runs without it compact there."""
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        trigger_tokens=12_000,
        limit_tokens=22_000,
        summary_input_budget_tokens=5_000,
        compaction_timeout_seconds=600.0,
    )

    # The request without history, a 2,000-token static prompt, stays well under the trigger.
    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=False,
        request_tokens=request_tokens,
        summary_request_tokens=2_000,
        allow_native_compaction=allow_native_compaction,
    )

    assert decision.mode == expected_mode
    assert decision.reason == expected_reason
    assert (decision.request_tokens, decision.trigger_tokens, decision.limit_tokens) == (request_tokens, 12_000, 22_000)


def test_plan_replay_that_fits_reduces_replay_for_non_authored_scope() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )

    history_settings = replay_settings(HistoryPolicy(mode="runs", limit=2))
    replay_plan = plan_replay_that_fits(
        session=session,
        history_settings=history_settings,
        available_history_budget=250,
        current_history_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=history_settings,
            model=ScriptedModel([]),
        ),
        model=ScriptedModel([]),
    )

    assert replay_plan.mode == "limited"
    assert replay_plan.num_history_runs == 1
    assert replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_without_budget_clears_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert read_scope_state(persisted).force_compact_before_next_run is False
    assert prepared.compaction_reply_outcome == "none"


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_budget_returns_configured_replay_plan(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan == ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=replay_settings(HistoryPolicy(mode="runs", limit=2)),
            model=ScriptedModel([]),
        ),
        add_history_to_context=True,
        num_history_runs=2,
        num_history_messages=None,
    )
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_tracks_disabled_replay_separately_from_session_persistence(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=500,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
        ],
    )
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "disabled"
    assert prepared.replays_persisted_history is False


def _frame(system: str) -> RequestFrame:
    return RequestFrame(
        ModelRequest(messages=()),
        (Message(role="system", content=system),),
        TurnMessages(prompt=Message(role="user", content="Current prompt")),
    )


async def _prepare_then_finalize_with_a_larger_prompt(
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext,
) -> PreparedHistoryState:
    """Prepare under a short system prompt, then plan replay for a final request whose prompt nearly fills the limit."""
    resolved_inputs = resolve_entity_preparation_inputs(
        config=config,
        runtime_paths=runtime_paths,
        history_settings=_agent_settings(config),
        active_model_name="default",
        active_context_window=None,
    )
    limit = resolved_inputs.execution_plan.limit_tokens
    assert limit == 64_000 - 16_384
    prepared = await prepare_scope_history(
        sizer=request_sizer(estimate_only_model(), config, "default"),
        # The prompt compaction ran for left room for the summary.
        frame=_frame("You are helpful."),
        resolved_inputs=resolved_inputs,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        compaction_lifecycle=None,
        pipeline_timing=None,
        allow_native_compaction=True,
    )
    # The final request without history leaves one token of the limit: (characters + 14 prompt characters) / 4.
    final_frame = _frame("s" * (4 * (limit - 1) - len("Current prompt")))
    return finalize_history_preparation(prepared_scope_history=prepared, frame=final_frame, pipeline_timing=None)


@pytest.mark.asyncio
async def test_prepare_history_for_run_preserves_compaction_when_summary_exceeds_final_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    install_model_providers(monkeypatch, {"default": summary_model()})
    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(
                return_value="merged summary",
            ),
        ),
        pytest.raises(HistorySummaryBudgetError, match=r"summary.*budget"),
    ):
        await _prepare_then_finalize_with_a_larger_prompt(
            config,
            runtime_paths,
            ScopeSessionContext(
                HistoryScope(kind="agent", scope_id="test_agent"),
                storage,
                session,
                "session-1",
                storage_factory=lambda: storage,
            ),
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "merged summary"
    assert persisted.runs == []
    state = read_scope_state(persisted)
    assert len(folded_run_ids(storage)) == 2
    assert state.force_compact_before_next_run is False


def test_a_history_size_that_fits_replays_even_where_the_summary_estimate_does_not() -> None:
    """A reported history size holds the summary as the provider counted it, below an estimate padded by its margin."""
    session = _session("summary-only", runs=[])
    session.summary = ScopeSummary(text="Project fact. " * 2000, updated_at=datetime.now(UTC))

    plan = plan_replay_that_fits(
        session=session,
        history_settings=replay_settings(),
        available_history_budget=5_000,
        current_history_tokens=4_000,
        model=ScriptedModel([]),
    )

    assert (plan.mode, plan.estimated_tokens) == ("configured", 4_000)


def test_replay_planner_rejects_summary_that_exceeds_budget_without_changing_history() -> None:
    """A summary-only session must fail explicitly instead of returning an oversized replay plan."""
    summary = ScopeSummary(text="Project fact. " * 2000, updated_at=datetime.now(UTC))
    session = _session("summary-only", runs=[])
    session.summary = summary
    settings = replay_settings()
    size = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=settings,
        model=ScriptedModel([]),
    )

    with pytest.raises(HistorySummaryBudgetError, match=r"summary.*budget"):
        plan_replay_that_fits(
            session=session,
            history_settings=settings,
            available_history_budget=500,
            current_history_tokens=size,
            model=ScriptedModel([]),
        )

    assert session.summary is summary
    assert session.summary.text == "Project fact. " * 2000
    assert session.runs == []

    recovered = plan_replay_that_fits(
        session=session,
        history_settings=settings,
        available_history_budget=10_000,
        current_history_tokens=size,
        model=ScriptedModel([]),
    )
    assert recovered.mode == "configured"
    assert recovered.estimated_tokens <= 10_000


def test_replay_planner_retains_fitting_summary_when_raw_history_cannot_fit() -> None:
    """A fitting summary remains available when only the raw runs exceed the replay budget."""
    session = _session(
        "summary-and-runs",
        runs=[_completed_run("run-1", messages=[Message(role="user", content="x" * 4000)])],
    )
    session.summary = ScopeSummary(text="Project facts", updated_at=datetime.now(UTC))
    settings = replay_settings()
    plan = plan_replay_that_fits(
        session=session,
        history_settings=settings,
        available_history_budget=100,
        current_history_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=settings,
            model=ScriptedModel([]),
        ),
        model=ScriptedModel([]),
    )
    assert plan.mode == "disabled"
    assert 0 < plan.estimated_tokens <= 100
    assert session.summary.text == "Project facts"


def test_plan_replay_that_fits_disables_replay_when_no_history_fits_budget() -> None:
    available_history_budget = quarter_tokens("budget")
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 600),
                    Message(role="assistant", content="a" * 600),
                ],
            ),
        ],
    )

    history_settings = replay_settings()
    replay_plan = plan_replay_that_fits(
        session=session,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        current_history_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=history_settings,
            model=ScriptedModel([]),
        ),
        model=ScriptedModel([]),
    )

    assert replay_plan.mode == "disabled"
    # History stays on with zero raw runs so the saved summary still replays first.
    assert replay_plan.add_history_to_context is True
    assert replay_plan.num_history_runs == 0
    assert replay_plan.num_history_messages is None
