"""Test dynamic config updates for scheduling with new agents."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.state import MatrixState
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow, _parse_workflow_schedule
from tests.conftest import install_bots, install_model_providers, orchestrator_runtime_paths, published_config_load
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


def _mock_agent_bot() -> MagicMock:
    """Build a bot-shaped mock with the runtime state expected by config reloads; it reads its runtime's config."""
    bot = MagicMock(spec=AgentBot)
    bot.pending_response_owner_count = 0
    bot.pending_response_phase_counts = {}
    bot.deferred_stop_phase = None
    bot.deferred_stop_required = False
    bot.client = None
    bot.running = True
    bot.quiesce_matrix_ingestion = AsyncMock()
    return bot


@pytest_asyncio.fixture
async def orchestrator_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[[Config], _MultiAgentOrchestrator]]:
    """Create orchestrators that are always stopped during test teardown."""
    orchestrators: list[_MultiAgentOrchestrator] = []

    def create(config: Config) -> _MultiAgentOrchestrator:
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=config)
        orchestrators.append(orchestrator)
        return orchestrator

    try:
        yield create
    finally:
        for orchestrator in reversed(orchestrators):
            await orchestrator.stop()


class TestDynamicConfigUpdate:
    """Test that dynamic config updates propagate to all existing bots."""

    @pytest.mark.asyncio
    async def test_config_update_propagates_to_existing_bots(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Test that when config is updated, all existing bots get the new config."""
        # Create initial config with just one agent
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )

        # Create orchestrator and set initial config
        orchestrator = orchestrator_factory(initial_config)

        # Create a mock bot for the general agent
        mock_bot = _mock_agent_bot()
        mock_bot.running = True
        orchestrator.fleet.add("general", mock_bot)

        # Create updated config with a new agent
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "callagent": {
                    "display_name": "CallAgent",
                    "role": "Call assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )
        persist_entity_accounts(initial_config, orchestrator.runtime_paths)
        persist_entity_accounts(updated_config, orchestrator.runtime_paths)

        # Mock the explicit runtime-bound config loader used by update_config().
        with patch.object(  # noqa: SIM117
            orchestrator.config_reload,
            "load_config",
            return_value=published_config_load(updated_config),
        ):
            # Mock the bot creation and setup methods to avoid actual Matrix operations
            with (
                patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot,
                patch("mindroom.orchestration.config_updates._get_changed_agents") as mock_identify,
                patch.object(orchestrator._rooms, "setup"),
            ):
                mock_identify.return_value = set()  # No entities need restarting

                # Create a mock for the new bot
                new_bot_mock = _mock_agent_bot()
                new_bot_mock.agent_name = "callagent"
                new_bot_mock.start.return_value = None
                new_bot_mock.sync_forever.return_value = None
                mock_create_bot.return_value = new_bot_mock

                # Call update_config
                updated = await orchestrator.config_reload._update_config()

                # Verify the update happened
                assert updated is True
                assert orchestrator.config == updated_config

                # Most importantly: verify that the existing bot adopted the new config
                mock_bot.apply_published_config.assert_called_once_with()

                # Verify that the new agent was added, reading the runtime's config
                assert orchestrator.agent_bots["callagent"] is new_bot_mock
                mock_create_bot.assert_any_call("callagent", ANY, orchestrator)

    @pytest.mark.asyncio
    async def test_plugin_change_restarts_existing_bots(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Plugin entry changes should restart existing bots instead of only swapping hook registries."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            plugins=["./plugins/tool-policy-v1"],
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            plugins=["./plugins/tool-policy-v2"],
        )

        orchestrator = orchestrator_factory(initial_config)
        persist_entity_accounts(initial_config, orchestrator.runtime_paths)
        persist_entity_accounts(updated_config, orchestrator.runtime_paths)

        general_bot = _mock_agent_bot()
        general_bot.refresh_presence = AsyncMock()
        router_bot = _mock_agent_bot()
        router_bot.refresh_presence = AsyncMock()
        install_bots(
            orchestrator,
            {
                "general": general_bot,
                ROUTER_AGENT_NAME: router_bot,
            },
        )

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator.fleet, "create", new=AsyncMock()),
            patch.object(orchestrator.fleet, "start", new=AsyncMock()) as mock_start,
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is True
        # Both bots stopped for a restart before the new config was published, so neither received it.
        assert not orchestrator.agent_bots
        general_bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
        router_bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
        mock_start.assert_awaited_once_with({"general", ROUTER_AGENT_NAME})
        general_bot.apply_published_config.assert_not_called()
        router_bot.apply_published_config.assert_not_called()
        general_bot.refresh_presence.assert_not_awaited()
        router_bot.refresh_presence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scheduling_with_dynamically_added_agent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that scheduling commands work correctly with dynamically added agents."""
        # Update config to add callagent
        updated_config = Config(
            agents={
                "email_assistant": {
                    "display_name": "EmailAssistant",
                    "role": "Email assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "callagent": {
                    "display_name": "CallAgent",
                    "role": "Call assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )

        # Test that parse_workflow_schedule correctly recognizes the new agent
        request = "whenever i get an email with title urgent, notify @callagent to send me a text"
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        )
        persist_entity_accounts(updated_config, runtime_paths)

        # The parser model answers with a workflow that references both agents.
        workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/2", hour="*", day="*", month="*", weekday="*"),
            message="@email_assistant Check for emails with 'urgent' in the title. If found, @callagent notify the user by sending a text.",
            description="Monitor for urgent emails and send text notification",
        )
        model = ScriptedModel([answer(workflow.model_dump_json(exclude_unset=True))])
        install_model_providers(monkeypatch, {"default": model})
        result = await _parse_workflow_schedule(
            request,
            updated_config,
            runtime_paths,
            available_responders=[
                MatrixID(username="email_assistant", domain="localhost"),
                MatrixID(username="callagent", domain="localhost"),
            ],
            timezone="UTC",
        )

        # Verify the workflow was parsed correctly and includes both agents
        assert isinstance(result, ScheduledWorkflow)
        assert "@email_assistant" in result.message
        assert "@callagent" in result.message
        assert result.description == "Monitor for urgent emails and send text notification"

    @pytest.mark.asyncio
    async def test_defaults_streaming_toggle_updates_existing_bots_without_restart(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Changing defaults.enable_streaming should update existing bots on config reload."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            defaults={"enable_streaming": True},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            defaults={"enable_streaming": False},
        )

        orchestrator = orchestrator_factory(initial_config)

        mock_bot = _mock_agent_bot()
        orchestrator.fleet.add("general", mock_bot)
        router_bot = _mock_agent_bot()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
        ):
            updated = await orchestrator.config_reload._update_config()

        # No entities restarted, but existing bots still receive new defaults.
        assert updated is False
        assert orchestrator.config == updated_config
        assert orchestrator.config.defaults.enable_streaming is False
        mock_bot.apply_published_config.assert_called_once_with()
        router_bot.apply_published_config.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_thread_summary_threshold_defaults_update_existing_bots_without_restart(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Changing thread summary defaults should update existing bots on config reload."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            defaults={
                "thread_summary_first_threshold": 5,
                "thread_summary_subsequent_interval": 10,
            },
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            defaults={
                "thread_summary_first_threshold": 1,
                "thread_summary_subsequent_interval": 3,
            },
        )

        orchestrator = orchestrator_factory(initial_config)

        mock_bot = _mock_agent_bot()
        orchestrator.fleet.add("general", mock_bot)
        router_bot = _mock_agent_bot()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert orchestrator.config == updated_config
        assert orchestrator.config.defaults.thread_summary_first_threshold == 1
        assert orchestrator.config.defaults.thread_summary_subsequent_interval == 3
        mock_bot.apply_published_config.assert_called_once_with()
        router_bot.apply_published_config.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_room_access_change_reconciles_rooms_without_restarts(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Changing room defaults should trigger room and invitation reconciliation without restarts."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            room_defaults={"join_policy": "invite", "listed": False},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            room_defaults={"join_policy": "public", "listed": True},
        )

        orchestrator = orchestrator_factory(initial_config)

        general_bot = _mock_agent_bot()
        orchestrator.fleet.add("general", general_bot)
        router_bot = _mock_agent_bot()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator._rooms, "setup", new=AsyncMock()) as mock_setup,
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert orchestrator.config == updated_config
        general_bot.apply_published_config.assert_called_once_with()
        router_bot.apply_published_config.assert_called_once_with()
        mock_setup.assert_awaited_once_with([])

    @pytest.mark.asyncio
    async def test_access_change_reconciles_invitations_without_restarts(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Changing membership access should reconcile rooms and invalidate stale grants."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            room_defaults={"invite_users": []},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                    "access": {"members_of_rooms": ["lobby"]},
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            room_defaults={"invite_users": ["@alice:example.com"]},
        )

        orchestrator = orchestrator_factory(initial_config)

        general_bot = _mock_agent_bot()
        orchestrator.fleet.add("general", general_bot)
        router_bot = _mock_agent_bot()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator._rooms, "setup", new=AsyncMock()) as mock_setup,
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert orchestrator.config == updated_config
        general_bot.apply_published_config.assert_called_once_with()
        router_bot.apply_published_config.assert_called_once_with()
        mock_setup.assert_awaited_once_with([])
        assert orchestrator.agent_reply_memberships.snapshot.policy_signature is None
        assert orchestrator.agent_reply_memberships.needs_refresh(updated_config)

    @pytest.mark.asyncio
    async def test_matrix_space_change_preserves_ready_reply_memberships(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Unrelated room presentation changes must not erase authoritative grants."""
        agent = {
            "display_name": "GeneralAgent",
            "role": "General assistant",
            "model": "default",
            "rooms": ["lobby"],
            "access": {"members_of_rooms": ["lobby"]},
        }
        initial_config = Config(
            agents={"general": agent},
            rooms={"lobby": {"display_name": "Lobby"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
            matrix_space={"enabled": False},
        )
        updated_config = Config(
            agents={"general": agent},
            rooms={"lobby": {"display_name": "Lobby"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
            matrix_space={"enabled": True},
        )
        orchestrator = orchestrator_factory(initial_config)
        room_id = "!lobby:example.com"
        state = MatrixState.load(runtime_paths=orchestrator.runtime_paths)
        state.add_room("lobby", room_id, "#lobby:example.com", "Lobby")
        state.save(runtime_paths=orchestrator.runtime_paths)
        membership_client = AsyncMock(spec=nio.AsyncClient)
        membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
        membership_client.joined_members.return_value = nio.JoinedMembersResponse(
            members=[nio.RoomMember("@alice:example.com", None, None)],
            room_id=room_id,
        )
        await orchestrator.agent_reply_memberships.refresh(
            initial_config,
            orchestrator.runtime_paths,
            membership_client,
        )
        general_bot = _mock_agent_bot()
        router_bot = _mock_agent_bot()
        router_bot.client = membership_client
        install_bots(
            orchestrator,
            {
                "general": general_bot,
                ROUTER_AGENT_NAME: router_bot,
            },
        )

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator._rooms, "ensure_rooms_exist", new=AsyncMock(return_value={"lobby": room_id})),
            patch.object(orchestrator._rooms, "link_root_space", new=AsyncMock()),
            patch.object(orchestrator, "_finalize_config_reload", new=AsyncMock()),
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert orchestrator.agent_reply_memberships.is_allowed(
            "@alice:example.com",
            ["lobby"],
            updated_config,
            orchestrator.runtime_paths,
        )
        assert membership_client.joined_members.await_count == 1

    @pytest.mark.asyncio
    async def test_room_only_update_keeps_ready_reply_memberships_without_router_replacement(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Live room reconciliation should preserve grants without replacing the router."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby", "obsolete"],
                    "access": {"members_of_rooms": ["lobby"]},
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                    "access": {"members_of_rooms": ["lobby"]},
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )
        orchestrator = orchestrator_factory(initial_config)
        room_id = "!lobby:example.com"
        state = MatrixState.load(runtime_paths=orchestrator.runtime_paths)
        state.add_room("lobby", room_id, "#lobby:example.com", "Lobby")
        state.save(runtime_paths=orchestrator.runtime_paths)
        membership_client = AsyncMock(spec=nio.AsyncClient)
        membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
        membership_client.joined_members.return_value = nio.JoinedMembersResponse(
            members=[nio.RoomMember("@alice:example.com", None, None)],
            room_id=room_id,
        )
        await orchestrator.agent_reply_memberships.refresh(
            initial_config,
            orchestrator.runtime_paths,
            membership_client,
        )
        install_bots(
            orchestrator,
            {
                "general": _mock_agent_bot(),
                ROUTER_AGENT_NAME: _mock_agent_bot(),
            },
        )

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator, "_reconcile_post_update_rooms", new=AsyncMock()) as reconcile_rooms,
            patch.object(orchestrator, "_finalize_config_reload", new=AsyncMock()),
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        plan = reconcile_rooms.await_args.args[0]
        assert plan.restart == set()
        assert set(orchestrator.agent_bots) == {ROUTER_AGENT_NAME, "general"}
        assert plan.reconcile_rooms == {ROUTER_AGENT_NAME, "general"}
        reconcile_rooms.assert_awaited_once_with(plan, set())
        assert not orchestrator.agent_reply_memberships.needs_refresh(updated_config)
        assert orchestrator.agent_reply_memberships.is_allowed(
            "@alice:example.com",
            ["lobby"],
            updated_config,
            orchestrator.runtime_paths,
        )

    @pytest.mark.asyncio
    async def test_mindroom_user_display_name_change_updates_user_account(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Changing mindroom_user.display_name should refresh the internal user account."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            mindroom_user={"username": "mindroom_user", "display_name": "Alice Internal"},
        )

        orchestrator = orchestrator_factory(initial_config)
        persist_entity_accounts(updated_config, orchestrator.runtime_paths)
        mock_bot = _mock_agent_bot()
        mock_bot.refresh_presence = AsyncMock()
        orchestrator.fleet.add("general", mock_bot)
        router_bot = _mock_agent_bot()
        router_bot.refresh_presence = AsyncMock()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(orchestrator, "_ensure_user_account", new=AsyncMock()) as mock_ensure_user,
            patch.object(orchestrator._rooms, "setup", new=AsyncMock()) as mock_setup,
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert orchestrator.config == updated_config
        router_bot.apply_published_config.assert_called_once_with()
        mock_ensure_user.assert_awaited_once_with(updated_config)
        mock_setup.assert_awaited_once_with([])

    @pytest.mark.asyncio
    async def test_mindroom_user_username_change_is_rejected_without_partial_update(
        self,
        orchestrator_factory: Callable[[Config], _MultiAgentOrchestrator],
    ) -> None:
        """Reject changing mindroom_user.username and keep the current runtime config."""
        initial_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        )
        updated_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
            mindroom_user={"username": "alice_internal", "display_name": "Alice Internal"},
        )

        orchestrator = orchestrator_factory(initial_config)
        mock_bot = _mock_agent_bot()
        mock_bot.refresh_presence = AsyncMock()
        orchestrator.fleet.add("general", mock_bot)
        router_bot = _mock_agent_bot()
        router_bot.refresh_presence = AsyncMock()
        orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
            patch.object(
                orchestrator,
                "_ensure_user_account",
                new=AsyncMock(side_effect=PermanentMatrixStartupError("mindroom_user.username cannot be changed")),
            ) as mock_ensure_user,
            patch.object(orchestrator._rooms, "setup", new=AsyncMock()) as mock_setup,
            pytest.raises(PermanentMatrixStartupError, match="cannot be changed"),
        ):
            await orchestrator.config_reload._update_config()

        assert orchestrator.config == initial_config
        mock_bot.apply_published_config.assert_not_called()
        router_bot.apply_published_config.assert_not_called()
        mock_ensure_user.assert_awaited_once_with(updated_config)
        mock_setup.assert_not_awaited()
