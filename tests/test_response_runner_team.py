"""Team response lifecycle through the ResponseRunner team helper and the configured-team regeneration path."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import TOOL_TRACE_CONTENT_KEY
from mindroom.conversation_resolver import ConversationResolver
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
)
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    AfterResponseContext,
    BeforeResponseContext,
    HookRegistry,
    MessageEnvelope,
    hook,
)
from mindroom.matrix.client import DeliveredMatrixEvent, ResolvedVisibleMessage
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, PostResponseEffectsSupport
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
)
from mindroom.response_sources import ResponseSources
from mindroom.room_model_overrides import set_room_model_override
from mindroom.teams.formation import TeamIntent, TeamMemberStatus, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.teams.spec import TeamMode
from mindroom.thread_summary.schedule import thread_summary_message_count_hint
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.turn_policy import TurnPolicy, _ResponderAvailability
from tests.bot_helpers import (
    AgentBotTestBase,
    _configured_team_test_config,
    _configured_team_user,
    _handled_response_event_id,
    _hook_envelope,
    _hook_plugin,
    _make_matrix_client_mock,
    _noop_typing_indicator,
    _visible_message,
    _visible_response_event_id,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
    make_test_agent_bot,
    unique_room_send_responses,
)
from tests.conftest import (
    TEST_PASSWORD,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    make_visible_message,
    message_origin,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    replace_response_runner_deps,
    request_envelope,
    runtime_paths_for,
    unwrap_extracted_collaborator,
)
from tests.identity_helpers import entity_ids
from tests.reply_span_helpers import seed_finished_reply
from tests.response_attempt_helpers import install_direct_response_admission
from tests.team_turn_helpers import scripted_team_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import (
        RuntimePaths,
    )
    from mindroom.matrix.conversation_reads import DeliveredResponse
    from mindroom.matrix.users import AgentMatrixUser


@dataclass(frozen=True)
class _RecordedPostResponseEffects(PostResponseEffectsSupport):
    """Post-response support that records the thread-summary handoff and runs no other effect."""

    summary_checks: list[tuple[str, str, int | None]] = field(default_factory=list)
    queued_summaries: list[tuple[str, str, str]] = field(default_factory=list)

    def build_deps(
        self,
        *,
        room_id: str,
        membership_turn_id: str,
        queue_memory_persistence: Callable[[], None] | None = None,
        queue_skill_review: Callable[[str], Awaitable[None]] | None = None,
        notify_response_finished: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> PostResponseEffectsDeps:
        del room_id, membership_turn_id, queue_memory_persistence, queue_skill_review, notify_response_finished
        del persist_response_event_id

        def should_queue_thread_summary(room_id: str, thread_id: str, message_count_hint: int | None) -> bool:
            self.summary_checks.append((room_id, thread_id, message_count_hint))
            return True

        def queue_thread_summary(
            room_id: str,
            thread_id: str,
            _entity_name: str | None,
            delivered_response: DeliveredResponse,
        ) -> None:
            self.queued_summaries.append((room_id, thread_id, delivered_response.event_id))

        return PostResponseEffectsDeps(
            logger=self.logger,
            should_queue_thread_summary=should_queue_thread_summary,
            queue_thread_summary=queue_thread_summary,
        )


def _record_post_response_effects(bot: AgentBot) -> _RecordedPostResponseEffects:
    """Install the recording post-response support on the bot's response runner."""
    support = unwrap_extracted_collaborator(bot._response_runner).deps.post_response_effects
    recorded = _RecordedPostResponseEffects(
        runtime=support.runtime,
        logger=support.logger,
        runtime_paths=support.runtime_paths,
        conversation_reader=support.conversation_reader,
        membership=support.membership,
        agent_name=support.agent_name,
    )
    replace_response_runner_deps(bot, post_response_effects=recorded)
    return recorded


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_team_model_snapshot_precedes_locked_turn_preparation(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A room-default change during locked preparation must wait for the next team turn."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        config.models["large"] = ModelConfig(provider="openai", id="large-model")
        runtime_paths = runtime_paths_for(config)
        set_room_model_override(
            runtime_paths,
            room_id="!test:localhost",
            model_name="large",
        )
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(current_config=config, config=config, runtime_paths=runtime_paths)
        coordinator = unwrap_extracted_collaborator(bot._response_runner)
        original_prepare_admitted_turn = coordinator._prepare_admitted_locked_turn
        team_stream = scripted_team_stream("Team reply")

        async def change_room_default_during_preparation(*args: object, **kwargs: object) -> ResponseRequest | None:
            set_room_model_override(
                runtime_paths,
                room_id="!test:localhost",
                model_name="default",
            )
            return await original_prepare_admitted_turn(*args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(coordinator, "_prepare_admitted_locked_turn", new=change_room_default_during_preparation),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            await coordinator.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        spec, _ctx = team_stream.call_args.args
        assert spec.model_name == "large"
        assert spec.member_model_names == {"calculator": "large", "general": "large"}

    @pytest.mark.asyncio
    async def test_scheduled_model_overrides_coordinator_and_members(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A schedule model applies to both coordinator and members despite room defaults."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        config.models["large"] = ModelConfig(provider="openai", id="large-model")
        config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
        runtime_paths = runtime_paths_for(config)
        set_room_model_override(
            runtime_paths,
            room_id="!test:localhost",
            model_name="large",
        )
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(current_config=config, config=config, runtime_paths=runtime_paths)
        coordinator = unwrap_extracted_collaborator(bot._response_runner)
        original_prepare_admitted_turn = coordinator._prepare_admitted_locked_turn
        team_stream = scripted_team_stream("Team reply")

        async def change_room_default_during_preparation(*args: object, **kwargs: object) -> ResponseRequest | None:
            set_room_model_override(
                runtime_paths,
                room_id="!test:localhost",
                model_name="default",
            )
            return await original_prepare_admitted_turn(*args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(coordinator, "_prepare_admitted_locked_turn", new=change_room_default_during_preparation),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            await coordinator.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                    scheduled_model="cheap",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        spec, _ctx = team_stream.call_args.args
        assert spec.model_name == "cheap"
        assert spec.member_model_names == {"calculator": "cheap", "general": "cheap"}

    @pytest.mark.asyncio
    async def test_over_budget_requester_team_uses_fallback_for_priced_models(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An over-budget requester's team turn swaps every priced coordinator and member model."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        config.models["large"] = ModelConfig(
            provider="openai",
            id="large-model",
            pricing=ModelPricing(input=5, output=30),
        )
        config.models["local"] = ModelConfig(provider="openai", id="local-model")
        config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
        config.agents["calculator"].model = "large"
        config.agents["general"].model = "local"
        config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)
        runtime_paths = runtime_paths_for(config)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
            budgets=BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: config),
        )
        coordinator = unwrap_extracted_collaborator(bot._response_runner)
        team_stream = scripted_team_stream("Team reply")

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            await coordinator.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        spec, _ctx = team_stream.call_args.args
        assert spec.model_name == "cheap"
        assert spec.member_model_names == {"calculator": "cheap", "general": "local"}

    @pytest.mark.asyncio
    async def test_team_model_snapshot_is_complete_before_streaming_check_yields(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """One room-default change must not split a turn across model aliases."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        config.models["large"] = ModelConfig(provider="openai", id="large-model")
        runtime_paths = runtime_paths_for(config)
        set_room_model_override(
            runtime_paths,
            room_id="!test:localhost",
            model_name="large",
        )
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(current_config=config, config=config, runtime_paths=runtime_paths)
        team_stream = scripted_team_stream("Team reply")

        async def change_room_default(*_args: object, **_kwargs: object) -> bool:
            set_room_model_override(
                runtime_paths,
                room_id="!test:localhost",
                model_name="default",
            )
            return False

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(side_effect=change_room_default),
                team_response_stream=team_stream,
            ),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        spec, _ctx = team_stream.call_args.args
        assert spec.model_name == "large"
        assert spec.member_model_names == {"calculator": "large", "general": "large"}

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_applies_hooks_to_final_team_message(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team final output should use the same before/after hook flow."""
        after_results: list[tuple[str, str, str, str]] = []

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.response_text = f"{ctx.draft.response_text} [hooked]"

        @hook(EVENT_MESSAGE_AFTER_RESPONSE)
        async def after_hook(ctx: AfterResponseContext) -> None:
            after_results.append(
                (
                    ctx.result.response_event_id,
                    ctx.result.response_text,
                    ctx.result.delivery_kind,
                    ctx.result.response_kind,
                ),
            )

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ) as mock_send_message,
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=scripted_team_stream("Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        # Both the placeholder and its edit go out through the outbox now, so
        # they are the two calls on one mock: the first is the placeholder, the
        # second the frozen replace envelope.
        placeholder, edit = (call.args[2] for call in mock_send_message.await_args_list)
        assert placeholder["body"] == "🤝 Team Response: Thinking..."
        assert edit["m.new_content"]["body"] == "Team reply [hooked]"
        assert mock_edit_message.await_count == 0
        assert after_results == [("$team", "Team reply [hooked]", "edited", "team")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("show_tool_calls", [True, False])
    async def test_a_blocking_team_reply_delivers_its_tool_trace_when_tool_calls_show(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        show_tool_calls: bool,
    ) -> None:
        """A blocking team reply carries the trace of the document it shows, as a streamed one does."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        config.defaults.show_tool_calls = show_tool_calls
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(current_config=config, config=config, runtime_paths=runtime_paths_for(config))
        trace = ToolTraceEntry(type="tool_call_completed", tool_name="touch", args_preview="label=a")

        def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncIterator[StructuredStreamChunk]:
            async def document() -> AsyncIterator[StructuredStreamChunk]:
                yield StructuredStreamChunk(content="Team reply", tool_trace=[trace])

            return document()

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ) as mock_send_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=fake_team_response_stream,
            ),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        _placeholder, edit = (call.args[2] for call in mock_send_message.await_args_list)
        delivered = edit["m.new_content"]
        assert delivered["body"] == "Team reply"
        if show_tool_calls:
            assert [event["tool_name"] for event in delivered[TOOL_TRACE_CONTENT_KEY]["events"]] == ["touch"]
        else:
            assert TOOL_TRACE_CONTENT_KEY not in delivered

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_preserves_enrichment_in_shared_team_session(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Shared team responses should never scrub enriched history after delivery."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=scripted_team_stream("Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_merges_raw_prompt_with_model_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper must preserve the raw user prompt when model-only context is present."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        team_stream = scripted_team_stream("Team reply")

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="Summarize the latest invoice.",
                    model_prompt="Available attachment IDs: att_invoice. Use tool calls to inspect or process them.",
                    response_envelope=_hook_envelope(
                        body="Summarize the latest invoice.",
                        source_event_id="$team-root",
                    ),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        _spec, ctx = team_stream.call_args.args
        prepared_message = ctx.prompt
        assert "Summarize the latest invoice." in prepared_message
        assert "Available attachment IDs: att_invoice." in prepared_message
        assert prepared_message.index("Summarize the latest invoice.") < prepared_message.index(
            "Available attachment IDs: att_invoice.",
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_does_not_duplicate_already_timestamped_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper should treat an already timestamped prompt as the same user turn."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        team_stream = scripted_team_stream("Team reply")
        timestamped_prompt = "[2026-03-20 08:15 PDT] What time is it?"

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="What time is it?",
                    model_prompt=timestamped_prompt,
                    response_envelope=_hook_envelope(
                        body="What time is it?",
                        source_event_id="$team-root",
                    ),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        _spec, ctx = team_stream.call_args.args
        prepared_message = ctx.prompt
        assert prepared_message == timestamped_prompt

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_timestamps_history_in_the_running_agents_timezone(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An ad hoc team's history uses the timezone of the agent whose bot runs it, not the global one."""
        config = self._config_for_storage(tmp_path)
        config.timezone = "UTC"
        config.agents["calculator"].timezone = "America/Los_Angeles"
        config.defaults.show_stop_button = False
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        team_stream = scripted_team_stream("Team reply")

        with (
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=team_stream,
            ),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$team-root",),
                        logical_source_event_ids=("$team-root",),
                    ),
                    thread_history=[
                        make_visible_message(sender="@user:localhost", body="older text", timestamp=1_774_018_800_000),
                    ],
                    user_id="@user:localhost",
                    prompt="What changed?",
                    response_envelope=_hook_envelope(body="What changed?", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        _spec, ctx = team_stream.call_args.args
        [older] = ctx.thread_history
        assert older.body == "[2026-03-20 08:00 PDT] older text"

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_uses_resolved_thread_root_for_placeholder_and_edit(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team helper should preserve the canonical thread root across placeholder and edit flow."""
        sent_contents: list[dict[str, object]] = []

        async def record_send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> DeliveredMatrixEvent:
            sent_contents.append(content)
            return delivered_matrix_event("$team", content)

        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        runtime_paths = runtime_paths_for(config)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        install_direct_response_admission(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id="$raw_thread:localhost",
                reply_to_event_id="$reply_plain:localhost",
            ).with_thread_root("$canonical_thread:localhost"),
            body="team prompt",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch("mindroom.delivery_gateway.send_message_outcome", new=AsyncMock(side_effect=record_send)),
            patch(
                "mindroom.delivery_gateway.edit_message_outcome",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response_stream=scripted_team_stream("Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=(envelope.source_event_id,),
                        logical_source_event_ids=(envelope.source_event_id,),
                    ),
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=envelope,
                    correlation_id="corr-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        # Two sends now: the placeholder, then its frozen replace envelope.
        assert len(sent_contents) == 2
        content, edit = sent_contents
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$canonical_thread:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"
        assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$team"}
        assert edit["m.new_content"]["body"] == "Team reply"
        assert mock_edit_message.await_count == 0

    @pytest.mark.asyncio
    async def test_rejected_team_regeneration_enters_the_runner_with_its_existing_reply(
        self,
        tmp_path: Path,
    ) -> None:
        """A regeneration its team rejects enters the runner's rejection entry with the reply it regenerates."""
        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = entity_ids(config, runtime_paths)["general"]
        bot = make_test_agent_bot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
        )
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )

        resolution = TeamResolution(
            intent=TeamIntent.CONFIGURED_TEAM,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.NOT_MATERIALIZABLE,
                ),
            ],
            outcome=TeamOutcome.REJECT,
            reason="No team available",
        )

        deliver_team_rejection = AsyncMock(return_value=None)

        with (
            patch.object(
                TurnPolicy,
                "_responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names=set(), live_entity_names=None),
            ),
            patch("mindroom.turn_policy.resolve_configured_team", return_value=resolution),
            patch.object(
                unwrap_extracted_collaborator(bot._response_runner),
                "deliver_team_rejection",
                new=deliver_team_rejection,
            ),
        ):
            await bot._edit_regenerator._run_regeneration(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Team, summarize this thread",
                    thread_history=[],
                    existing_event_id="$existing",
                    user_id="@alice:localhost",
                    response_envelope=_hook_envelope(body="hello", source_event_id="$event", thread_id="$thread"),
                    correlation_id="corr-team-rejection",
                ),
            )

        call = deliver_team_rejection.await_args
        assert call.kwargs == {
            "member_names": ["general"],
            "team_mode": "coordinate",
            "rejection_message": "No team available",
        }
        assert call.args[0].existing_event_id == "$existing"

    @pytest.mark.asyncio
    async def test_configured_team_response_resolves_current_member_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """Configured team responses should use the current persisted member IDs."""
        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        initial_ids = entity_ids(config, runtime_paths)
        stale_member = initial_ids["general"]
        bot = make_test_agent_bot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
        )
        _wrap_extracted_collaborators(bot)
        bot.client = _make_matrix_client_mock()

        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account(
            "agent_general",
            "actual_general_live",
            TEST_PASSWORD,
            domain=config.get_domain(runtime_paths),
        )
        state.save(runtime_paths=runtime_paths)
        current_member = entity_ids(config, runtime_paths)["general"]
        assert stale_member.full_id != current_member.full_id

        captured_member_ids: list[list[str]] = []

        def capture_resolve_configured_team(
            team_name: str,
            team_members: list[Any],
            mode: TeamMode,
            config_arg: Config,
            runtime_paths_arg: RuntimePaths,
            *,
            materializable_agent_names: set[str] | None = None,
        ) -> TeamResolution:
            assert team_name == "support_team"
            assert mode is TeamMode.COORDINATE
            assert config_arg is config
            assert runtime_paths_arg == runtime_paths
            assert materializable_agent_names == {"general"}
            captured_member_ids.append([member.full_id for member in team_members])
            return TeamResolution(
                intent=TeamIntent.CONFIGURED_TEAM,
                requested_members=team_members,
                member_statuses=[
                    TeamResolutionMember(
                        agent=current_member,
                        name="general",
                        status=TeamMemberStatus.NOT_MATERIALIZABLE,
                    ),
                ],
                outcome=TeamOutcome.REJECT,
                reason="not materializable",
            )

        deliver_team_rejection = AsyncMock(return_value="$reject")

        with (
            patch.object(
                TurnPolicy,
                "_responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.turn_policy.resolve_configured_team", side_effect=capture_resolve_configured_team),
            patch.object(
                unwrap_extracted_collaborator(bot._response_runner),
                "deliver_team_rejection",
                new=deliver_team_rejection,
            ),
        ):
            await bot._edit_regenerator._run_regeneration(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Team, summarize this thread",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="Team, summarize this thread",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
            )

        assert captured_member_ids == [[current_member.full_id]]
        assert stale_member.full_id not in captured_member_ids[0]
        assert deliver_team_rejection.await_args.kwargs["rejection_message"] == "not materializable"

    @pytest.mark.asyncio
    async def test_team_generate_response_sizes_the_summary_gate_by_the_refreshed_thread(
        self,
        tmp_path: Path,
    ) -> None:
        """A team reply hands the summary gate the size of the thread it refreshed under the lock, counting itself."""
        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]
        refreshed_history = ThreadHistoryResult(thread_history, is_full_history=True)

        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = make_test_agent_bot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
        )
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$team-response")
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
            agent_bots={name: MagicMock(running=True) for name in (*config.agents, *config.teams)},
        )
        effects = _record_post_response_effects(bot)

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing_indicator,
                team_response_stream=scripted_team_stream("Team reply", completes=True),
            ),
            patch.object(
                ConversationResolver,
                "fetch_thread_history",
                new=AsyncMock(return_value=refreshed_history),
            ),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Team, summarize this thread",
                    # The ingress history is older than the thread the turn reads once it holds the lock.
                    thread_history=thread_history[:2],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="Team, summarize this thread",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
                member_names=["general"],
                team_mode="coordinate",
            )

        # The four refreshed thread messages plus the team's reply.
        assert effects.summary_checks == [("!test:localhost", "$thread", 5)]

    @pytest.mark.asyncio
    async def test_team_generate_response_keeps_streamed_visible_reply_when_before_response_suppresses(
        self,
        tmp_path: Path,
    ) -> None:
        """A team must keep a visible streamed reply even if before_response tries to suppress it afterwards."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def suppressing_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = make_test_agent_bot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
        )
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$team-response")
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [suppressing_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
            agent_bots={name: MagicMock(running=True) for name in (*config.agents, *config.teams)},
        )
        effects = _record_post_response_effects(bot)

        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing_indicator,
            team_response_stream=scripted_team_stream("Team reply", completes=True),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Team, summarize this thread",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        thread_id="$thread",
                        prompt="Team, summarize this thread",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
                member_names=["general"],
                team_mode="coordinate",
            )

        # The stream filled the reply's placeholder, and the answer stands as a delivered reply.
        assert resolution == "$team-response1"
        final_edit = bot.client.room_send.await_args_list[-1].kwargs["content"]
        assert final_edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$team-response1"}
        assert final_edit["m.new_content"]["body"] == "Team reply"
        bot.client.room_redact.assert_not_awaited()
        assert effects.queued_summaries == [("!test:localhost", "$thread", "$team-response1")]

    def test_thread_summary_message_count_hint_excludes_existing_summaries(self) -> None:
        """Thread-summary hints should count the post-response non-summary total."""
        thread_history = [
            ResolvedVisibleMessage.synthetic(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                timestamp=1700000000 + i,
                event_id=f"$message{i}",
            )
            for i in range(4)
        ]
        thread_history.append(
            ResolvedVisibleMessage.synthetic(
                sender="@mindroom_general:localhost",
                body="🧵 Existing summary",
                timestamp=1700000005,
                event_id="$summary",
                content={
                    "msgtype": "m.notice",
                    "body": "🧵 Existing summary",
                    "io.mindroom.thread_summary": {
                        "version": 1,
                        "summary": "🧵 Existing summary",
                        "message_count": 4,
                        "model": "default",
                    },
                },
                thread_id="$thread",
            ),
        )

        assert (
            thread_summary_message_count_hint(
                thread_history,
                trusted_sender_ids=frozenset({"@mindroom_general:localhost"}),
            )
            == 5
        )

    @pytest.mark.asyncio
    async def test_generate_team_response_streams_into_placeholder_event(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team streaming should stay enabled when reusing the startup placeholder."""

        @asynccontextmanager
        async def noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            yield

        async def run_cancellable_response(*_args: object, **kwargs: object) -> str:
            response_kwargs = cast("dict[str, Callable[[str | None], Awaitable[None]]]", kwargs)
            response_function = response_kwargs["response_function"]
            await response_function("$placeholder")
            return "$placeholder"

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "stream chunk"

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        # The startup placeholder lands, binding the reply's event.
        unique_room_send_responses(bot.client, prefix="$placeholder")
        bot.orchestrator = MagicMock()
        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
            ),
            patch.object(
                ResponseRunner,
                "_run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$placeholder",
                        rendered_body="stream chunk",
                        visible_body_state="visible_body",
                    ),
                ),
            ) as mock_send_streaming_response,
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-stream",
                ),
                member_names=[bot.agent_name],
                team_mode="coordinate",
            )

        assert _handled_response_event_id(resolution) == "$placeholder"
        assert _visible_response_event_id(resolution) == "$placeholder"
        send_kwargs = mock_send_streaming_response.await_args.kwargs
        # The stream adopts the placeholder this turn's reply created.
        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        assert reply is not None
        assert send_kwargs["existing_event_id"] == reply.event_id is not None
        assert send_kwargs["adopt_existing_placeholder"] is True

    @pytest.mark.asyncio
    async def test_silent_schedule_team_response_is_blocking_and_allows_no_report(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Silent scheduled team turns bypass an enabled stream and accept empty completion."""
        typing_events: list[str] = []

        @asynccontextmanager
        async def record_typing(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
            typing_events.append("started")
            yield

        config = self._config_for_storage(tmp_path)
        assert config.defaults.show_tool_calls is True
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$response")
        bot.orchestrator = MagicMock()
        team_stream = scripted_team_stream("Finding")
        send_streaming_response = AsyncMock()

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=record_typing,
                team_response_stream=team_stream,
            ),
            patch("mindroom.delivery_gateway.send_streaming_response", new=send_streaming_response),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Check for updates",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$event",
                        prompt="Check for updates",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                    ),
                    correlation_id="corr-silent-team",
                ),
                member_names=[bot.agent_name],
                team_mode="coordinate",
            )

        # The turn's stream is collected and delivered whole, never streamed into the room.
        send_streaming_response.assert_not_awaited()
        team_stream.assert_called_once()
        _spec, ctx = team_stream.call_args.args
        assert ctx.allow_no_report_response is True
        assert ctx.compaction_lifecycle is None
        assert typing_events == []
        # One message, the final answer: no placeholder to edit or redact, and no stop button.
        [send] = bot.client.room_send.await_args_list
        assert send.kwargs["message_type"] == "m.room.message"
        assert send.kwargs["content"]["body"] == "Finding"
        bot.client.room_redact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silent_schedule_ad_hoc_team_writes_receipt_to_each_member(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A dynamic team's resolved participants must each receive the run receipt."""
        config = self._config_for_storage(tmp_path)
        runtime_paths = runtime_paths_for(config)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock()

        async def team_response_with_started_receipt_check(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            for agent_name in ("calculator", "general"):
                receipt_directory = (
                    runtime_paths.storage_root / "agents" / agent_name / "workspace" / ".mindroom" / "scheduled_runs"
                )
                [receipt_path] = list(receipt_directory.glob("*.json"))
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                assert receipt["status"] == "started"
            yield "Finding"

        with (
            patch_response_runner_module(
                team_response_stream=team_response_with_started_receipt_check,
            ),
            patch(
                "mindroom.delivery_gateway.DeliveryGateway.send_text",
                new=AsyncMock(return_value="$response"),
            ),
        ):
            await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$ad-hoc-silent-run",),
                        logical_source_event_ids=("$ad-hoc-silent-run",),
                    ),
                    prompt="Check for updates",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$ad-hoc-silent-run",
                        prompt="Check for updates",
                        user_id="@alice:localhost",
                        agent_name=bot.agent_name,
                        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                    ),
                    correlation_id="corr-ad-hoc-silent-team",
                ),
                member_names=["calculator", "general"],
                team_mode="collaborate",
            )

        for agent_name in ("calculator", "general"):
            receipt_directory = (
                runtime_paths.storage_root / "agents" / agent_name / "workspace" / ".mindroom" / "scheduled_runs"
            )
            receipts = list(receipt_directory.glob("*.json"))
            assert len(receipts) == 1
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            assert receipt["entity_name"] == bot.agent_name
            assert receipt["agent_name"] == agent_name
            assert receipt["status"] == "completed"

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_keeps_streamed_visible_reply_when_before_response_suppresses(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Streaming team helpers must keep the visible reply once real streamed text lands."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "stream chunk"

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        unique_room_send_responses(bot.client, prefix="$stream")
        bot.orchestrator = MagicMock()
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])

        with patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing_indicator,
            team_response_stream=fake_team_response_stream,
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-stream-suppress",
                ),
                member_names=[bot.agent_name],
                team_mode="coordinate",
            )

        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        assert reply is not None
        # The stream filled the reply's placeholder, and the suppression after it left the answer standing.
        assert resolution == reply.event_id == "$stream1"
        assert reply.state is rl.ReplyState.COMPLETED
        final_edit = bot.client.room_send.await_args_list[-1].kwargs["content"]
        assert final_edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$stream1"}
        assert final_edit["m.new_content"]["body"] == "stream chunk"
        bot.client.room_redact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_returns_none_when_suppressed_placeholder_is_redacted(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """A suppressed team answer leaves no reply event behind and reports none."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = make_test_agent_bot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        bot.orchestrator = MagicMock()
        bot._redact_message_event = AsyncMock(return_value=True)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        replace_delivery_gateway_deps(bot, redact_message_event=bot._redact_message_event)

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing_indicator,
                team_response_stream=scripted_team_stream("Team handled"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    sources=ResponseSources(
                        pending_event_ids=("$event",),
                        logical_source_event_ids=("$event",),
                    ),
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-suppress",
                ),
                member_names=[bot.agent_name],
                team_mode="coordinate",
            )

        assert resolution is None
        reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
        assert reply is not None
        assert reply.state is rl.ReplyState.GONE
        # Nothing was shown, so nothing is left to redact.
        assert reply.event_id is None
        bot._redact_message_event.assert_not_awaited()


def _team_regeneration_request(
    bot: AgentBot,
    *,
    prompt: str,
    on_reply_claimed: Callable[[], Awaitable[None]],
) -> ResponseRequest:
    """Return the request an edit of ``$event`` builds to regenerate its answer ``$existing``."""
    return ResponseRequest(
        sources=ResponseSources(pending_event_ids=("$edit",), logical_source_event_ids=("$event",)),
        prompt=prompt,
        thread_history=[],
        existing_event_id="$existing",
        user_id="@alice:localhost",
        # The edit drives the regeneration; its target is the conversation the edited message's turn answered.
        response_envelope=request_envelope(
            reply_to_event_id="$edit",
            target=MessageTarget.resolve("!test:localhost", "$thread", "$event"),
            prompt=prompt,
            user_id="@alice:localhost",
            agent_name=bot.agent_name,
        ),
        correlation_id="$edit",
        on_reply_claimed=on_reply_claimed,
        edit_regeneration=True,
    )


async def _team_bot_with_existing_reply(tmp_path: Path) -> AgentBot:
    """Return a configured team's bot whose reply ``$existing`` answered ``$event``."""
    config = _configured_team_test_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    bot = make_test_agent_bot(
        _configured_team_user(config, runtime_paths),
        tmp_path,
        config=config,
        runtime_paths=runtime_paths,
    )
    install_direct_response_admission(bot)
    bot.client = _make_matrix_client_mock()
    unique_room_send_responses(bot.client, prefix="$regenerated")
    await seed_finished_reply(
        bot.journal_principal(),
        "$existing",
        sources=ResponseSources(pending_event_ids=("$event",), logical_source_event_ids=("$event",)),
        room_id="!test:localhost",
        thread_id="$thread",
        entity_name=bot.agent_name,
    )
    return bot


def _edits_of_existing_reply(bot: AgentBot) -> list[str]:
    """Return the bodies of every edit this bot sent to ``$existing``."""
    return [
        call.kwargs["content"]["m.new_content"]["body"]
        for call in bot.client.room_send.await_args_list
        if call.kwargs["content"].get("m.relates_to") == {"rel_type": "m.replace", "event_id": "$existing"}
    ]


@pytest.mark.asyncio
async def test_rejected_team_regeneration_edits_its_existing_reply_with_the_reason(tmp_path: Path) -> None:
    """A regeneration whose team is rejected commits the edit and shows the reason in the reply it regenerates."""
    bot = await _team_bot_with_existing_reply(tmp_path)
    commit_edit = AsyncMock()

    with patch_response_runner_module(
        typing_indicator=_noop_typing_indicator,
        should_use_streaming=AsyncMock(return_value=False),
    ):
        await bot._response_runner.deliver_team_rejection(
            _team_regeneration_request(bot, prompt="Team, summarize this thread", on_reply_claimed=commit_edit),
            member_names=["general"],
            team_mode="coordinate",
            rejection_message="Team 'support_team' cannot answer: general is unavailable.",
        )

    commit_edit.assert_awaited_once()
    assert _edits_of_existing_reply(bot) == ["Team 'support_team' cannot answer: general is unavailable."]
    reply = await bot._reply_runtime.store.replies.for_event("$existing")
    assert reply is not None
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.current_span_id is None


@pytest.mark.asyncio
async def test_empty_team_regeneration_finalizes_its_existing_reply_empty(tmp_path: Path) -> None:
    """An edit that empties the prompt of a team's message finalizes the reply it regenerates without answering."""
    bot = await _team_bot_with_existing_reply(tmp_path)
    commit_edit = AsyncMock()
    team_response_stream = AsyncMock()

    with patch_response_runner_module(
        typing_indicator=_noop_typing_indicator,
        should_use_streaming=AsyncMock(return_value=False),
        team_response_stream=team_response_stream,
    ):
        await bot._response_runner.generate_response_for_empty_prompt(
            _team_regeneration_request(bot, prompt="  ", on_reply_claimed=commit_edit),
            response_kind="team",
        )

    team_response_stream.assert_not_called()
    commit_edit.assert_awaited_once()
    # The reply keeps showing its earlier answer: nothing is sent, edited, or redacted.
    bot.client.room_send.assert_not_awaited()
    bot.client.room_redact.assert_not_awaited()
    reply = await bot._reply_runtime.store.replies.for_event("$existing")
    assert reply is not None
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.current_span_id is None
