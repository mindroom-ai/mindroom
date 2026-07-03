"""Team response lifecycle through the ResponseRunner team helper and the configured-team regeneration path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot, TeamBot
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
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
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.client import DeliveredMatrixEvent, ResolvedVisibleMessage
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.response_runner import (
    ResponseRequest,
    ResponseRunner,
)
from mindroom.teams import TeamIntent, TeamMemberStatus, TeamMode, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.turn_policy import _ResponderAvailability
from tests.conftest import (
    TEST_PASSWORD,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    install_send_response_mock,
    message_origin,
    patch_response_runner_module,
    replace_delivery_gateway_deps,
    request_envelope,
    runtime_paths_for,
    unwrap_extracted_collaborator,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import (
        RuntimePaths,
    )
    from mindroom.matrix.users import AgentMatrixUser


from tests.bot_helpers import (
    AgentBotTestBase,
    _configured_team_test_config,
    _configured_team_user,
    _empty_full_thread_history,
    _handled_response_event_id,
    _hook_envelope,
    _hook_plugin,
    _install_runtime_cache_support,
    _make_matrix_client_mock,
    _noop_typing_indicator,
    _visible_message,
    _visible_response_event_id,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
)


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook, after_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = entity_ids(config, runtime_paths_for(config))
        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ) as mock_send_message,
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        assert mock_send_message.await_args.args[2]["body"] == "🤝 Team Response: Thinking..."
        assert mock_edit_message.await_args.args[4] == "Team reply [hooked]"
        assert after_results == [("$team", "Team reply [hooked]", "edited", "team")]

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_preserves_enrichment_in_shared_team_session(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Shared team responses should never scrub enriched history after delivery."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = entity_ids(config, runtime_paths_for(config))
        bot._conversation_state_writer.create_storage = MagicMock(return_value=MagicMock())
        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                    correlation_id="corr-team",
                ),
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = entity_ids(config, runtime_paths_for(config))
        mock_team_response = AsyncMock(return_value="Team reply")

        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=mock_team_response,
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
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
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        prepared_message = mock_team_response.await_args.kwargs["message"]
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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = entity_ids(config, runtime_paths_for(config))
        mock_team_response = AsyncMock(return_value="Team reply")
        timestamped_prompt = "[2026-03-20 08:15 PDT] What time is it?"

        with (
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=mock_team_response,
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
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
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        prepared_message = mock_team_response.await_args.kwargs["message"]
        assert prepared_message == timestamped_prompt

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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        matrix_ids = entity_ids(config, runtime_paths)
        envelope = MessageEnvelope(
            source_event_id="$reply_plain:localhost",
            room_id="!test:localhost",
            target=MessageTarget.resolve(
                room_id="!test:localhost",
                thread_id="$raw_thread:localhost",
                reply_to_event_id="$reply_plain:localhost",
            ).with_thread_root("$canonical_thread:localhost"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="team prompt",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=mock_agent_user.agent_name,
            source_kind=MESSAGE_SOURCE_KIND,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )
        history = ThreadHistoryResult([], is_full_history=True)

        with (
            patch(
                "mindroom.matrix.conversation_cache.MatrixConversationCache.get_latest_thread_event_id_if_needed",
                new=AsyncMock(return_value="$latest:localhost"),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=record_send)),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ) as mock_edit_message,
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value="Team reply"),
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=envelope,
                    correlation_id="corr-team",
                ),
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        assert len(sent_contents) == 1
        content = sent_contents[0]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$canonical_thread:localhost"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply_plain:localhost"
        assert mock_edit_message.await_args.args[3]["m.relates_to"]["event_id"] == "$canonical_thread:localhost"

    @pytest.mark.asyncio
    async def test_team_generate_response_nonteam_fallback_delivers_without_after_response(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-team fallback should deliver directly without response lifecycle hooks."""
        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = entity_ids(config, runtime_paths)["general"]
        bot = TeamBot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )

        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[],
            outcome=TeamOutcome.NONE,
            reason="No team available",
        )

        bot._edit_message = AsyncMock(return_value=True)
        bot._delivery_gateway.deliver_final = AsyncMock()
        bot._delivery_gateway.deps.response_hooks.emit_after_response = AsyncMock()
        bot._delivery_gateway.deps.response_hooks.emit_cancelled_response = AsyncMock()

        with (
            patch.object(
                bot._turn_policy,
                "responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
        ):
            delivery_resolution = await bot._run_regenerated_response(
                ResponseRequest(
                    prompt="Team, summarize this thread",
                    thread_history=[],
                    existing_event_id="$existing",
                    existing_event_is_placeholder=True,
                    user_id="@alice:localhost",
                    response_envelope=_hook_envelope(body="hello", source_event_id="$event", thread_id="$thread"),
                    correlation_id="corr-nonteam-fallback",
                ),
            )

        bot._delivery_gateway.deliver_final.assert_not_awaited()
        bot._edit_message.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$existing",
            new_text="No team available",
            thread_id="$thread",
        )
        bot._delivery_gateway.deps.response_hooks.emit_after_response.assert_not_awaited()
        bot._delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()
        assert delivery_resolution == "$existing"

    @pytest.mark.asyncio
    async def test_configured_team_response_resolves_current_member_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """Configured TeamBot responses should use the current persisted member IDs."""
        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        initial_ids = entity_ids(config, runtime_paths)
        stale_member = initial_ids["general"]
        bot = TeamBot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)

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
                eligible_members=[],
                outcome=TeamOutcome.REJECT,
                reason="not materializable",
            )

        send_response = AsyncMock(return_value="$reject")
        install_send_response_mock(bot, send_response)

        with (
            patch.object(
                bot._turn_policy,
                "responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.bot.resolve_configured_team", side_effect=capture_resolve_configured_team),
        ):
            result = await bot._run_regenerated_response(
                ResponseRequest(
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
        assert result == "$reject"

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_registers_interactive_questions_with_bot_agent_name(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team interactive questions should be owned by the real bot agent name."""
        config = self._config_for_storage(tmp_path)
        config.defaults.show_stop_button = False
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        matrix_ids = entity_ids(config, runtime_paths_for(config))
        interactive_response = """```interactive
{"question":"Choose","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""
        with (
            patch_response_runner_module(
                typing_indicator=_noop_typing_indicator,
                should_use_streaming=AsyncMock(return_value=False),
                team_response=AsyncMock(return_value=interactive_response),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
            ),
            patch(
                "mindroom.delivery_gateway.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
            ),
            patch("mindroom.bot.interactive.register_interactive_question") as mock_register,
            patch("mindroom.bot.interactive.add_reaction_buttons", new_callable=AsyncMock) as mock_add_buttons,
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    thread_history=[],
                    user_id="@user:localhost",
                    prompt="team prompt",
                    response_envelope=request_envelope(
                        room_id="!test:localhost",
                        reply_to_event_id="$team-root",
                        prompt="team prompt",
                        user_id="@user:localhost",
                        agent_name=bot.agent_name,
                    ),
                ),
                team_agents=[matrix_ids["calculator"], matrix_ids["general"]],
                team_mode="collaborate",
            )

        assert _handled_response_event_id(resolution) == "$team"
        mock_register.assert_called_once()
        assert mock_register.call_args.args[0] == "$team"
        assert mock_register.call_args.args[1] == "!test:localhost"
        assert mock_register.call_args.args[2] is None
        assert mock_register.call_args.args[4] == bot.agent_name
        assert mock_register.call_args.args[4] != "team"
        mock_add_buttons.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_team_response_queues_memory_before_helper_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Team memory should be queued before the shared helper runs."""

        async def fake_store_conversation_memory(*args: object, **kwargs: object) -> None:
            store_calls.append((args, kwargs))

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []
        store_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        async def fail_helper(*_args: object, **_kwargs: object) -> str:
            assert any(name.startswith("memory_save_team_") for name in scheduled_names)
            msg = "boom"
            raise RuntimeError(msg)

        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = entity_ids(config, runtime_paths)["general"]
        bot = TeamBot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        history = _empty_full_thread_history()

        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch.object(
                bot._turn_policy,
                "responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch.object(bot._response_runner, "generate_team_response_helper", new=AsyncMock(side_effect=fail_helper)),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
            patch("mindroom.bot.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await bot._run_regenerated_response(
                ResponseRequest(
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

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert len(store_calls) == 1
        assert any(name.startswith("memory_save_team_") for name in scheduled_names)

    @pytest.mark.asyncio
    async def test_team_generate_response_uses_shared_thread_summary_helper_for_summary_gate(
        self,
        tmp_path: Path,
    ) -> None:
        """Team replies should reuse the shared thread-summary helper for summary gating."""

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        scheduled_tasks: list[asyncio.Task[None]] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            return task

        thread_history = [
            _visible_message(
                sender=f"@user{i}:localhost",
                body=f"Message {i}",
                event_id=f"$message{i}",
                timestamp=i,
            )
            for i in range(4)
        ]

        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = entity_ids(config, runtime_paths)["general"]
        bot = TeamBot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        refreshed_history = ThreadHistoryResult(list(thread_history), is_full_history=True)

        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch.object(
                bot._turn_policy,
                "responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch.object(
                bot._response_runner,
                "generate_team_response_helper",
                new=AsyncMock(return_value="$response"),
            ),
            patch.object(
                bot._conversation_cache,
                "get_dispatch_thread_history",
                AsyncMock(return_value=refreshed_history),
            ),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
            patch("mindroom.bot.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
        ):
            await bot._run_regenerated_response(
                ResponseRequest(
                    prompt="Team, summarize this thread",
                    thread_history=thread_history,
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

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        mock_thread_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_team_generate_response_keeps_streamed_visible_reply_when_before_response_suppresses(
        self,
        tmp_path: Path,
    ) -> None:
        """TeamBot must keep a visible streamed reply even if before_response tries to suppress it afterwards."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def suppressing_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        async def fake_store_conversation_memory(*_args: object, **_kwargs: object) -> None:
            return None

        async def fake_team_response_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[str, None]:
            yield "Team reply"

        scheduled_tasks: list[asyncio.Task[None]] = []
        scheduled_names: list[str] = []

        def schedule_background_task(
            coro: Coroutine[Any, Any, None],
            *,
            name: str,
            error_handler: object | None = None,  # noqa: ARG001
            owner: object | None = None,  # noqa: ARG001
        ) -> asyncio.Task[None]:
            task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
            scheduled_tasks.append(task)
            scheduled_names.append(name)
            return task

        config = _configured_team_test_config(tmp_path)
        runtime_paths = runtime_paths_for(config)
        team_member = entity_ids(config, runtime_paths)["general"]
        bot = TeamBot(
            _configured_team_user(config, runtime_paths),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [suppressing_hook])])
        bot.orchestrator = MagicMock(
            current_config=config,
            config=config,
            runtime_paths=runtime_paths,
        )
        history = _empty_full_thread_history()
        resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[team_member],
            member_statuses=[
                TeamResolutionMember(
                    agent=team_member,
                    name="general",
                    status=TeamMemberStatus.ELIGIBLE,
                ),
            ],
            eligible_members=[team_member],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=_noop_typing_indicator,
                team_response_stream=lambda *_args, **_kwargs: fake_team_response_stream(),
            ),
            patch.object(
                bot._turn_policy,
                "responder_availability",
                return_value=_ResponderAvailability(materializable_agent_names={"general"}, live_entity_names=None),
            ),
            patch("mindroom.bot.resolve_configured_team", return_value=resolution),
            patch.object(bot._conversation_cache, "get_dispatch_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$team-response",
                        terminal_status="completed",
                        rendered_body="Team reply",
                        visible_body_state="visible_body",
                    ),
                ),
            ),
            patch("mindroom.response_runner.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.post_response_effects.create_background_task", side_effect=schedule_background_task),
            patch("mindroom.bot.store_conversation_memory", side_effect=fake_store_conversation_memory),
            patch(
                "mindroom.post_response_effects.maybe_generate_thread_summary",
                new_callable=AsyncMock,
            ) as mock_thread_summary,
        ):
            resolution = await bot._run_regenerated_response(
                ResponseRequest(
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

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks)

        assert resolution == "$team-response"
        mock_thread_summary.assert_awaited_once()
        assert "thread_summary_!test:localhost_$thread" in scheduled_names

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

        assert thread_summary_message_count_hint(thread_history) == 5

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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        mock_team_response = AsyncMock()
        history = _empty_full_thread_history()
        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
                team_response=mock_team_response,
            ),
            patch.object(
                ResponseRunner,
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$placeholder",
                        terminal_status="completed",
                        rendered_body="stream chunk",
                        visible_body_state="visible_body",
                    ),
                ),
            ) as mock_send_streaming_response,
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    existing_event_id="$placeholder",
                    existing_event_is_placeholder=True,
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-stream",
                ),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
            )

        assert _handled_response_event_id(resolution) == "$placeholder"
        assert _visible_response_event_id(resolution) == "$placeholder"
        mock_team_response.assert_not_awaited()
        send_kwargs = mock_send_streaming_response.await_args.kwargs
        assert send_kwargs["existing_event_id"] == "$placeholder"
        assert send_kwargs["adopt_existing_placeholder"] is True

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
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        bot._redact_message_event = AsyncMock(return_value=True)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        replace_delivery_gateway_deps(bot, redact_message_event=bot._redact_message_event)
        history = _empty_full_thread_history()

        with (
            patch.object(
                unwrap_extracted_collaborator(bot._response_runner),
                "run_cancellable_response",
                new=AsyncMock(side_effect=run_cancellable_response),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new=AsyncMock(
                    return_value=StreamTransportOutcome(
                        last_physical_stream_event_id="$placeholder",
                        terminal_status="completed",
                        rendered_body="stream chunk",
                        visible_body_state="visible_body",
                    ),
                ),
            ),
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=True),
                typing_indicator=noop_typing_indicator,
                team_response_stream=fake_team_response_stream,
            ),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    existing_event_id="$placeholder",
                    existing_event_is_placeholder=True,
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-stream-suppress",
                ),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
            )

        assert resolution == "$placeholder"
        bot._redact_message_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_team_response_helper_returns_none_when_suppressed_placeholder_is_redacted(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Suppressed team placeholder responses should not leak the redacted placeholder id."""

        @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
        async def before_hook(ctx: BeforeResponseContext) -> None:
            ctx.draft.suppress = True

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        _install_runtime_cache_support(bot)
        bot.orchestrator = MagicMock()
        bot._redact_message_event = AsyncMock(return_value=True)
        bot.hook_registry = HookRegistry.from_plugins([_hook_plugin("hooked", [before_hook])])
        replace_delivery_gateway_deps(bot, redact_message_event=bot._redact_message_event)
        history = _empty_full_thread_history()

        with (
            patch_response_runner_module(
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing_indicator,
                team_response=AsyncMock(return_value="Team handled"),
            ),
            patch.object(bot._conversation_cache, "get_thread_history", AsyncMock(return_value=history)),
        ):
            resolution = await bot._response_runner.generate_team_response_helper(
                ResponseRequest(
                    prompt="Continue",
                    thread_history=[],
                    user_id="@alice:localhost",
                    existing_event_id="$placeholder",
                    existing_event_is_placeholder=True,
                    response_envelope=_hook_envelope(
                        body="Continue",
                        source_event_id="$event",
                        thread_id="$thread_root",
                    ),
                    correlation_id="corr-team-suppress",
                ),
                team_agents=[bot.matrix_id],
                team_mode="coordinate",
            )

        assert resolution is None
        bot._redact_message_event.assert_awaited_once_with(
            room_id="!test:localhost",
            event_id="$placeholder",
            reason="Suppressed placeholder response",
        )
