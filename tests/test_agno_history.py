"""Tests for Agno-native multi-turn conversation history."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from pydantic import ValidationError

from mindroom.agents import (
    _get_agent_session,
    create_agent,
    get_seen_event_ids,
    remove_run_by_event_id,
)
from mindroom.ai import (
    _apply_context_window_limit,
    _build_prompt_with_unseen,
    _get_unseen_messages,
    _prepare_agent_and_prompt,
    ai_response,
)
from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, DefaultsConfig, ModelConfig
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestHistoryConfig:
    """Test history configuration fields and validation."""

    def test_defaults_num_history_runs_default(self) -> None:
        """Default num_history_runs is None (include all history)."""
        defaults = DefaultsConfig()
        assert defaults.num_history_runs is None
        assert defaults.num_history_messages is None

    def test_agent_config_history_defaults_none(self) -> None:
        """AgentConfig history fields default to None (inherit from defaults)."""
        agent = AgentConfig(display_name="Test")
        assert agent.num_history_runs is None
        assert agent.num_history_messages is None

    def test_config_rejects_both_history_knobs_agent(self) -> None:
        """Setting both num_history_runs and num_history_messages raises ValidationError."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            AgentConfig(
                display_name="Test",
                num_history_runs=5,
                num_history_messages=20,
            )

    def test_config_rejects_both_history_knobs_defaults(self) -> None:
        """Setting both on DefaultsConfig raises ValidationError."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            DefaultsConfig(
                num_history_runs=5,
                num_history_messages=20,
            )

    def test_agent_config_allows_single_knob(self) -> None:
        """Setting only one knob is fine."""
        agent_runs = AgentConfig(display_name="Test", num_history_runs=10)
        assert agent_runs.num_history_runs == 10
        assert agent_runs.num_history_messages is None

        agent_msgs = AgentConfig(display_name="Test", num_history_messages=50)
        assert agent_msgs.num_history_runs is None
        assert agent_msgs.num_history_messages == 50

    def test_defaults_num_history_messages_works(self) -> None:
        """DefaultsConfig can use num_history_messages when num_history_runs is None."""
        defaults = DefaultsConfig(num_history_runs=None, num_history_messages=50)
        assert defaults.num_history_messages == 50
        assert defaults.num_history_runs is None

    def test_defaults_num_history_messages_wired_to_agent(self) -> None:
        """Defaults-level num_history_messages flows to agent when no per-agent override."""
        config = Config.from_yaml()
        config.defaults = DefaultsConfig(num_history_runs=None, num_history_messages=50)
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.num_history_messages == 50
        assert agent.num_history_runs is None

    def test_num_history_runs_config_wired_to_agent(self) -> None:
        """Default config includes all history (None bypasses Agno's default of 3)."""
        config = Config.from_yaml()
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.add_history_to_context is True
        # Both defaults are None → post-construction override to None (all history)
        assert agent.num_history_runs is None

    def test_num_history_runs_per_agent_override(self) -> None:
        """Per-agent num_history_runs overrides defaults and clears num_history_messages."""
        config = Config.from_yaml()
        config.agents["calculator"].num_history_runs = 7
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.num_history_runs == 7
        assert agent.num_history_messages is None

    def test_num_history_messages_per_agent_override(self) -> None:
        """Per-agent num_history_messages overrides defaults and clears num_history_runs."""
        config = Config.from_yaml()
        config.agents["calculator"].num_history_messages = 50
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.num_history_messages == 50
        assert agent.num_history_runs is None

    def test_compress_tool_results_default(self) -> None:
        """create_agent() sets compress_tool_results=True by default."""
        config = Config.from_yaml()
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.compress_tool_results is True

    def test_compress_tool_results_per_agent_override(self) -> None:
        """Per-agent compress_tool_results=False overrides the default."""
        config = Config.from_yaml()
        config.agents["calculator"].compress_tool_results = False
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.compress_tool_results is False

    # -- enable_session_summaries --

    def test_enable_session_summaries_default_false(self) -> None:
        """DefaultsConfig.enable_session_summaries defaults to False."""
        defaults = DefaultsConfig()
        assert defaults.enable_session_summaries is False

    def test_agent_config_enable_session_summaries_default_none(self) -> None:
        """AgentConfig.enable_session_summaries defaults to None (inherit)."""
        agent = AgentConfig(display_name="Test")
        assert agent.enable_session_summaries is None

    def test_enable_session_summaries_wired_default(self) -> None:
        """create_agent() sets enable_session_summaries=False by default."""
        config = Config.from_yaml()
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.enable_session_summaries is False

    def test_enable_session_summaries_defaults_override(self) -> None:
        """Defaults-level enable_session_summaries=True flows to agent."""
        config = Config.from_yaml()
        config.defaults.enable_session_summaries = True
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.enable_session_summaries is True

    def test_enable_session_summaries_per_agent_true(self) -> None:
        """Per-agent enable_session_summaries=True overrides defaults False."""
        config = Config.from_yaml()
        config.agents["calculator"].enable_session_summaries = True
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.enable_session_summaries is True

    def test_enable_session_summaries_per_agent_false_overrides_defaults_true(self) -> None:
        """Per-agent enable_session_summaries=False overrides defaults True."""
        config = Config.from_yaml()
        config.defaults.enable_session_summaries = True
        config.agents["calculator"].enable_session_summaries = False
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.enable_session_summaries is False

    # -- max_tool_calls_from_history --

    def test_max_tool_calls_from_history_default_none(self) -> None:
        """DefaultsConfig.max_tool_calls_from_history defaults to None."""
        defaults = DefaultsConfig()
        assert defaults.max_tool_calls_from_history is None

    def test_agent_config_max_tool_calls_from_history_default_none(self) -> None:
        """AgentConfig.max_tool_calls_from_history defaults to None (inherit)."""
        agent = AgentConfig(display_name="Test")
        assert agent.max_tool_calls_from_history is None

    def test_max_tool_calls_from_history_wired_default(self) -> None:
        """create_agent() sets max_tool_calls_from_history=None by default."""
        config = Config.from_yaml()
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.max_tool_calls_from_history is None

    def test_max_tool_calls_from_history_defaults_override(self) -> None:
        """Defaults-level max_tool_calls_from_history=5 flows to agent."""
        config = Config.from_yaml()
        config.defaults.max_tool_calls_from_history = 5
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.max_tool_calls_from_history == 5

    def test_max_tool_calls_from_history_per_agent_override(self) -> None:
        """Per-agent max_tool_calls_from_history=3 overrides defaults None."""
        config = Config.from_yaml()
        config.agents["calculator"].max_tool_calls_from_history = 3
        with patch("mindroom.agents.SqliteDb"):
            agent = create_agent("calculator", config=config)
        assert agent.max_tool_calls_from_history == 3

    def test_max_tool_calls_from_history_defaults_rejects_negative(self) -> None:
        """DefaultsConfig rejects negative max_tool_calls_from_history."""
        with pytest.raises(ValidationError):
            DefaultsConfig(max_tool_calls_from_history=-1)

    def test_max_tool_calls_from_history_agent_rejects_negative(self) -> None:
        """AgentConfig rejects negative max_tool_calls_from_history."""
        with pytest.raises(ValidationError):
            AgentConfig(display_name="Test", max_tool_calls_from_history=-1)


# ---------------------------------------------------------------------------
# Agent helper tests
# ---------------------------------------------------------------------------


def _make_storage_with_session(session_id: str, runs: list[RunOutput] | None = None) -> SqliteDb:
    """Create a mock SqliteDb that returns a session with the given runs."""
    session = AgentSession(session_id=session_id, runs=runs)
    storage = MagicMock(spec=SqliteDb)
    storage.get_session.return_value = session
    return storage


def _make_run_output(
    run_id: str = "run-1",
    metadata: dict[str, Any] | None = None,
) -> RunOutput:
    """Create a RunOutput with optional metadata."""
    return RunOutput(run_id=run_id, metadata=metadata)


class TestGetAgentSession:
    """Test _get_agent_session helper."""

    def test_no_session(self) -> None:
        """Return None when session does not exist."""
        storage = MagicMock(spec=SqliteDb)
        storage.get_session.return_value = None
        assert _get_agent_session(storage, "sid") is None

    def test_empty_runs(self) -> None:
        """Return session with empty runs list."""
        storage = _make_storage_with_session("sid", runs=[])
        session = _get_agent_session(storage, "sid")
        assert session is not None
        assert not session.runs

    def test_has_runs(self) -> None:
        """Return session with runs."""
        run = _make_run_output()
        storage = _make_storage_with_session("sid", runs=[run])
        session = _get_agent_session(storage, "sid")
        assert session is not None
        assert len(session.runs) == 1


class TestGetSeenEventIds:
    """Test get_seen_event_ids helper."""

    def test_empty_runs(self) -> None:
        """Return empty set when session has no runs."""
        session = AgentSession(session_id="sid", runs=[])
        assert get_seen_event_ids(session) == set()

    def test_runs_without_metadata(self) -> None:
        """Return empty set when runs have no metadata."""
        run = _make_run_output(metadata=None)
        session = AgentSession(session_id="sid", runs=[run])
        assert get_seen_event_ids(session) == set()

    def test_runs_with_seen_ids(self) -> None:
        """Return union of all matrix_seen_event_ids across runs."""
        run1 = _make_run_output("r1", metadata={"matrix_seen_event_ids": ["$e1", "$e2"]})
        run2 = _make_run_output("r2", metadata={"matrix_seen_event_ids": ["$e2", "$e3"]})
        session = AgentSession(session_id="sid", runs=[run1, run2])
        assert get_seen_event_ids(session) == {"$e1", "$e2", "$e3"}

    def test_runs_with_mixed_metadata(self) -> None:
        """Runs without matrix_seen_event_ids are skipped gracefully."""
        run1 = _make_run_output("r1", metadata={"other_key": "val"})
        run2 = _make_run_output("r2", metadata={"matrix_seen_event_ids": ["$e1"]})
        session = AgentSession(session_id="sid", runs=[run1, run2])
        assert get_seen_event_ids(session) == {"$e1"}


class TestRemoveRunByEventId:
    """Test remove_run_by_event_id helper."""

    def test_no_session(self) -> None:
        """Return False when session does not exist."""
        storage = MagicMock(spec=SqliteDb)
        storage.get_session.return_value = None
        assert remove_run_by_event_id(storage, "sid", "$e1") is False

    def test_no_matching_run(self) -> None:
        """Return False when no run matches the event_id."""
        run = _make_run_output("r1", metadata={"matrix_event_id": "$other"})
        storage = _make_storage_with_session("sid", runs=[run])
        assert remove_run_by_event_id(storage, "sid", "$e1") is False
        storage.upsert_session.assert_not_called()

    def test_removes_matching_run(self) -> None:
        """Remove the matching run and save the session."""
        run1 = _make_run_output("r1", metadata={"matrix_event_id": "$e1"})
        run2 = _make_run_output("r2", metadata={"matrix_event_id": "$e2"})
        session = AgentSession(session_id="sid", runs=[run1, run2])
        storage = MagicMock(spec=SqliteDb)
        storage.get_session.return_value = session
        assert remove_run_by_event_id(storage, "sid", "$e1") is True
        storage.upsert_session.assert_called_once()
        # Verify only run2 remains
        saved_session = storage.upsert_session.call_args[0][0]
        assert len(saved_session.runs) == 1
        assert saved_session.runs[0].run_id == "r2"


# ---------------------------------------------------------------------------
# Unseen message detection tests
# ---------------------------------------------------------------------------


class TestGetUnseenMessages:
    """Test _get_unseen_messages helper."""

    def _make_config(self) -> Config:
        """Create a minimal Config for testing."""
        return Config(
            agents={"test_agent": AgentConfig(display_name="Test")},
            models={"default": {"provider": "openai", "id": "test"}},
        )

    def test_filters_agent_messages(self) -> None:
        """Messages from this agent are excluded."""
        config = self._make_config()
        agent_id = config.ids["test_agent"].full_id
        thread_history = [
            {"sender": agent_id, "body": "I am the agent", "event_id": "$a1"},
            {"sender": "@user:example.com", "body": "Hello", "event_id": "$u1"},
        ]
        unseen = _get_unseen_messages(thread_history, "test_agent", config, set(), None)
        assert len(unseen) == 1
        assert unseen[0]["event_id"] == "$u1"

    def test_filters_seen_event_ids(self) -> None:
        """Messages with event_ids in seen_event_ids are excluded."""
        config = self._make_config()
        thread_history = [
            {"sender": "@user:example.com", "body": "Old msg", "event_id": "$u1"},
            {"sender": "@user:example.com", "body": "New msg", "event_id": "$u2"},
        ]
        unseen = _get_unseen_messages(thread_history, "test_agent", config, {"$u1"}, None)
        assert len(unseen) == 1
        assert unseen[0]["event_id"] == "$u2"

    def test_filters_current_event(self) -> None:
        """The current triggering message is excluded."""
        config = self._make_config()
        thread_history = [
            {"sender": "@user:example.com", "body": "Current", "event_id": "$u1"},
        ]
        unseen = _get_unseen_messages(thread_history, "test_agent", config, set(), "$u1")
        assert len(unseen) == 0

    def test_empty_history(self) -> None:
        """Empty thread history returns empty list."""
        config = self._make_config()
        unseen = _get_unseen_messages([], "test_agent", config, set(), None)
        assert unseen == []

    def test_multi_user_scenario(self) -> None:
        """Multiple users/agents in thread, only unseen from non-self returned."""
        config = self._make_config()
        agent_id = config.ids["test_agent"].full_id
        thread_history = [
            {"sender": "@alice:example.com", "body": "Hi", "event_id": "$a1"},
            {"sender": agent_id, "body": "Hello Alice", "event_id": "$bot1"},
            {"sender": "@bob:example.com", "body": "Hey", "event_id": "$b1"},
            {"sender": "@alice:example.com", "body": "More", "event_id": "$a2"},
        ]
        # Agent has seen $a1 (from previous turn)
        unseen = _get_unseen_messages(
            thread_history,
            "test_agent",
            config,
            {"$a1"},
            "$a2",
        )
        # Only $b1 should be unseen ($a1 seen, $bot1 is self, $a2 is current)
        assert len(unseen) == 1
        assert unseen[0]["event_id"] == "$b1"


class TestBuildPromptWithUnseen:
    """Test _build_prompt_with_unseen helper."""

    def test_no_unseen(self) -> None:
        """Prompt is returned unchanged when no unseen messages."""
        result = _build_prompt_with_unseen("Hello", [])
        assert result == "Hello"

    def test_with_unseen(self) -> None:
        """Unseen messages are prepended to the prompt."""
        unseen = [
            {"sender": "@alice:example.com", "body": "Hi there"},
        ]
        result = _build_prompt_with_unseen("Hello", unseen)
        assert "Messages from other participants" in result
        assert "@alice:example.com: Hi there" in result
        assert "Current message:\nHello" in result

    def test_unseen_missing_body(self) -> None:
        """Messages without body are skipped."""
        unseen = [{"sender": "@alice:example.com"}]
        result = _build_prompt_with_unseen("Hello", unseen)
        assert result == "Hello"


# ---------------------------------------------------------------------------
# _prepare_agent_and_prompt integration tests
# ---------------------------------------------------------------------------


class TestPrepareAgentAndPrompt:
    """Test the _prepare_agent_and_prompt logic for history vs fallback."""

    @pytest.fixture
    def config(self) -> Config:
        """Load config for testing."""
        return Config.from_yaml()

    def _mock_session(self, runs: list[RunOutput] | None = None) -> AgentSession | None:
        """Create a mock AgentSession or None."""
        if runs is None:
            return None
        return AgentSession(session_id="sid", runs=runs)

    @pytest.mark.asyncio
    async def test_fallback_when_no_session(self, config: Config, tmp_path: object) -> None:
        """When session has no runs, build_prompt_with_thread_history IS called."""
        thread_history = [
            {"sender": "@user:example.com", "body": "Hi", "event_id": "$u1"},
        ]
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai._get_agent_session", return_value=None),
            patch("mindroom.ai.build_prompt_with_thread_history", return_value="stuffed") as mock_stuff,
            patch("mindroom.ai.create_session_storage"),
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            _agent, prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id="sid",
                reply_to_event_id="$u1",
            )
            mock_stuff.assert_called_once_with("enhanced", thread_history)
            assert prompt == "stuffed"
            assert unseen_ids == []

    @pytest.mark.asyncio
    async def test_agno_history_skips_thread_stuffing(self, config: Config, tmp_path: object) -> None:
        """When session has runs, build_prompt_with_thread_history is NOT called."""
        thread_history = [
            {"sender": "@user:example.com", "body": "Old", "event_id": "$u1"},
            {"sender": "@user:example.com", "body": "New", "event_id": "$u2"},
        ]
        run = _make_run_output("r1", metadata={"matrix_seen_event_ids": ["$u1"]})
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai._get_agent_session", return_value=self._mock_session([run])),
            patch("mindroom.ai.build_prompt_with_thread_history") as mock_stuff,
            patch("mindroom.ai.create_session_storage"),
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            _agent, _prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id="sid",
                reply_to_event_id="$u2",
            )
            mock_stuff.assert_not_called()
            # $u1 is seen, $u2 is current → no unseen messages, but the function still ran the Agno path
            assert unseen_ids == []

    @pytest.mark.asyncio
    async def test_session_has_runs_but_no_metadata_uses_agno_path(self, config: Config, tmp_path: object) -> None:
        """Session has runs but no matrix metadata → still uses Agno history (no stuffing)."""
        thread_history = [
            {"sender": "@user:example.com", "body": "Msg", "event_id": "$u1"},
        ]
        run = _make_run_output("r1", metadata=None)
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai._get_agent_session", return_value=self._mock_session([run])),
            patch("mindroom.ai.build_prompt_with_thread_history") as mock_stuff,
            patch("mindroom.ai.create_session_storage"),
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            # $u1 is current, so no unseen, but Agno path used
            _, _prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id="sid",
                reply_to_event_id="$u1",
            )
            mock_stuff.assert_not_called()
            assert unseen_ids == []

    @pytest.mark.asyncio
    async def test_no_session_id_uses_fallback(self, config: Config, tmp_path: object) -> None:
        """When session_id is None, fallback path is used."""
        thread_history = [{"sender": "@user:example.com", "body": "Hi", "event_id": "$u1"}]
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai.build_prompt_with_thread_history", return_value="stuffed") as mock_stuff,
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            _, _prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id=None,
            )
            mock_stuff.assert_called_once()
            assert unseen_ids == []

    @pytest.mark.asyncio
    async def test_openai_compat_with_prior_runs_skips_stuffing(self, config: Config, tmp_path: object) -> None:
        """OpenAI-compat path: no reply_to_event_id, prior runs exist → Agno replays history, no stuffing."""
        thread_history = [
            {"sender": "user", "body": "Hello"},
            {"sender": "assistant", "body": "Hi there"},
            {"sender": "user", "body": "Follow up"},
        ]
        run = _make_run_output("r1", metadata=None)
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai._get_agent_session", return_value=self._mock_session([run])),
            patch("mindroom.ai.build_prompt_with_thread_history") as mock_stuff,
            patch("mindroom.ai.create_session_storage"),
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            _, prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id="sid",
                reply_to_event_id=None,
            )
            # No stuffing — Agno handles history replay natively
            mock_stuff.assert_not_called()
            # Bare enhanced prompt (no unseen injection either)
            assert prompt == "enhanced"
            assert unseen_ids == []

    @pytest.mark.asyncio
    async def test_openai_compat_first_turn_uses_stuffing(self, config: Config, tmp_path: object) -> None:
        """OpenAI-compat path: no reply_to_event_id, no prior runs → fallback to thread stuffing."""
        thread_history = [
            {"sender": "user", "body": "Hello"},
        ]
        with (
            patch("mindroom.ai.build_memory_enhanced_prompt", new_callable=AsyncMock, return_value="enhanced"),
            patch("mindroom.ai.create_agent") as mock_create,
            patch("mindroom.ai._get_agent_session", return_value=None),
            patch("mindroom.ai.build_prompt_with_thread_history", return_value="stuffed") as mock_stuff,
            patch("mindroom.ai.create_session_storage"),
        ):
            mock_create.return_value = MagicMock(spec=Agent)
            _, prompt, unseen_ids = await _prepare_agent_and_prompt(
                "calculator",
                "test",
                tmp_path,
                None,
                config,
                thread_history=thread_history,
                session_id="sid",
                reply_to_event_id=None,
            )
            # First turn with no prior runs → stuffing fallback
            mock_stuff.assert_called_once()
            assert prompt == "stuffed"
            assert unseen_ids == []


# ---------------------------------------------------------------------------
# Metadata passing tests
# ---------------------------------------------------------------------------


class TestMetadataPassing:
    """Test that event_id metadata is passed to agent.arun()."""

    @pytest.mark.asyncio
    async def test_event_id_in_metadata(self, tmp_path: object) -> None:
        """Verify matrix_event_id and matrix_seen_event_ids stored in metadata."""
        config = Config.from_yaml()

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prep,
            patch("mindroom.ai._cached_agent_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_agent = MagicMock(spec=Agent)
            mock_agent.add_history_to_context = True
            mock_prep.return_value = (mock_agent, "prompt", ["$unseen1"])
            mock_run.return_value = MagicMock(content="response", tools=None)

            await ai_response(
                agent_name="calculator",
                prompt="test",
                session_id="sid",
                storage_path=tmp_path,
                config=config,
                reply_to_event_id="$trigger",
            )

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["metadata"] == {
                "matrix_event_id": "$trigger",
                "matrix_seen_event_ids": ["$trigger", "$unseen1"],
            }

    @pytest.mark.asyncio
    async def test_no_metadata_without_reply_to_event_id(self, tmp_path: object) -> None:
        """When reply_to_event_id is None, metadata is None."""
        config = Config.from_yaml()

        with (
            patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prep,
            patch("mindroom.ai._cached_agent_run", new_callable=AsyncMock) as mock_run,
        ):
            mock_agent = MagicMock(spec=Agent)
            mock_agent.add_history_to_context = True
            mock_prep.return_value = (mock_agent, "prompt", [])
            mock_run.return_value = MagicMock(content="response", tools=None)

            await ai_response(
                agent_name="calculator",
                prompt="test",
                session_id="sid",
                storage_path=tmp_path,
                config=config,
            )

            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["metadata"] is None


# ---------------------------------------------------------------------------
# Unseen reinjection test
# ---------------------------------------------------------------------------


class TestUnseenNotReinjected:
    """Test that consumed unseen messages are not re-injected on the next turn."""

    def test_unseen_messages_not_reinjected(self) -> None:
        """Consumed unseen event_ids are excluded from detection on the next turn."""
        config = Config(
            agents={"bot": AgentConfig(display_name="Bot")},
            models={"default": {"provider": "openai", "id": "test"}},
        )

        thread_history = [
            {"sender": "@alice:example.com", "body": "Turn 1", "event_id": "$a1"},
            {"sender": "@bob:example.com", "body": "Turn 1 bob", "event_id": "$b1"},
            {"sender": "@alice:example.com", "body": "Turn 2", "event_id": "$a2"},
        ]

        # Turn 1: agent sees $a1, $b1 is unseen
        turn1_seen = {"$a1"}
        unseen_turn1 = _get_unseen_messages(thread_history, "bot", config, turn1_seen, "$a1")
        unseen_turn1_ids = [m["event_id"] for m in unseen_turn1]
        assert "$b1" in unseen_turn1_ids

        # After turn 1, matrix_seen_event_ids = [$a1, $b1]
        turn2_seen = {"$a1", "$b1"}

        # Turn 2: $b1 should NOT appear as unseen
        unseen_turn2 = _get_unseen_messages(thread_history, "bot", config, turn2_seen, "$a2")
        unseen_turn2_ids = [m["event_id"] for m in unseen_turn2]
        assert "$b1" not in unseen_turn2_ids
        assert unseen_turn2_ids == []


# ---------------------------------------------------------------------------
# Edit cleanup test
# ---------------------------------------------------------------------------


class TestEditRemovesStaleRun:
    """Test that _handle_message_edit removes stale run before regeneration."""

    @pytest.mark.asyncio
    async def test_edit_removes_stale_run(self, tmp_path: object) -> None:
        """Verify remove_run_by_event_id is called before regeneration."""
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id="@test_agent:example.com",
            display_name="Test Agent",
            password="test_password",  # noqa: S106
        )

        config = Mock()
        config.agents = {"test_agent": Mock(knowledge_bases=[])}
        config.domain = "example.com"

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        bot.client = AsyncMock(spec=nio.AsyncClient)
        bot.client.rooms = {}
        bot.client.user_id = "@test_agent:example.com"
        bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)
        bot.logger = MagicMock()

        room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")
        bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

        edit_event = nio.RoomMessageText.from_dict(
            {
                "content": {
                    "body": "* @test_agent what is 3+3?",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "@test_agent what is 3+3?", "msgtype": "m.text"},
                    "m.relates_to": {"event_id": "$original:example.com", "rel_type": "m.replace"},
                },
                "event_id": "$edit:example.com",
                "sender": "@user:example.com",
                "origin_server_ts": 1000001,
                "type": "m.room.message",
                "room_id": "!test:example.com",
            },
        )
        edit_event.source = {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {"body": "@test_agent what is 3+3?", "msgtype": "m.text"},
                "m.relates_to": {"event_id": "$original:example.com", "rel_type": "m.replace"},
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
        }

        with (
            patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
            patch.object(bot, "_edit_message", new_callable=AsyncMock),
            patch("mindroom.bot.should_agent_respond") as mock_should_respond,
            patch("mindroom.bot.should_use_streaming", new_callable=AsyncMock) as mock_streaming,
            patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai_response,
            patch("mindroom.bot.remove_run_by_event_id") as mock_remove_run,
            patch("mindroom.bot.create_session_storage") as mock_create_storage,
        ):
            mock_context.return_value = MagicMock(
                am_i_mentioned=True,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=["test_agent"],
                has_non_agent_mentions=False,
            )
            mock_should_respond.return_value = True
            mock_streaming.return_value = False
            mock_ai_response.return_value = "The answer is 6"
            mock_remove_run.return_value = True
            mock_create_storage.return_value = MagicMock(spec=SqliteDb)

            await bot._on_message(room, edit_event)

            # Verify remove_run_by_event_id was called with the original event ID
            mock_remove_run.assert_called_once()
            call_args = mock_remove_run.call_args
            assert call_args[0][1] == "!test:example.com"  # session_id (room_id:thread_id, no thread)
            assert call_args[0][2] == "$original:example.com"  # event_id


# ---------------------------------------------------------------------------
# Full scenario test
# ---------------------------------------------------------------------------


class TestFullScenario:
    """End-to-end scenario: multi-user thread + edit + restart."""

    def test_multi_user_edit_restart(self) -> None:
        """Asserts exact unseen IDs injected each turn and no reinjection."""
        config = Config(
            agents={"bot": AgentConfig(display_name="Bot")},
            models={"default": {"provider": "openai", "id": "test"}},
        )
        agent_id = config.ids["bot"].full_id

        # Simulate a thread with multiple users
        thread_history = [
            {"sender": "@alice:example.com", "body": "Hi bot", "event_id": "$a1"},
            {"sender": agent_id, "body": "Hi Alice!", "event_id": "$bot1"},
            {"sender": "@bob:example.com", "body": "Me too", "event_id": "$b1"},
            {"sender": "@carol:example.com", "body": "Hello all", "event_id": "$c1"},
            {"sender": "@alice:example.com", "body": "Follow up", "event_id": "$a2"},
        ]

        # Turn 1: Agent responded to $a1. It saw $a1.
        turn1_seen = {"$a1"}
        # Current message for turn 2 is $a2
        unseen_turn2 = _get_unseen_messages(thread_history, "bot", config, turn1_seen, "$a2")
        unseen_turn2_ids = [m["event_id"] for m in unseen_turn2]
        # Should see $b1 and $c1 (not $bot1 (self), not $a1 (seen), not $a2 (current))
        assert unseen_turn2_ids == ["$b1", "$c1"]

        # After turn 2, all consumed: $a1, $b1, $c1, $a2
        turn3_seen = {"$a1", "$b1", "$c1", "$a2"}

        # Turn 3: New message from bob
        thread_history.append({"sender": "@bob:example.com", "body": "Another", "event_id": "$b2"})
        thread_history.append({"sender": "@alice:example.com", "body": "Turn 3", "event_id": "$a3"})

        unseen_turn3 = _get_unseen_messages(thread_history, "bot", config, turn3_seen, "$a3")
        unseen_turn3_ids = [m["event_id"] for m in unseen_turn3]
        assert unseen_turn3_ids == ["$b2"]

        # Simulate restart: seen_event_ids still come from stored metadata
        # Same assertion holds — no reinjection
        restart_seen = {"$a1", "$b1", "$c1", "$a2", "$b2", "$a3"}
        unseen_after_restart = _get_unseen_messages(thread_history, "bot", config, restart_seen, "$a3")
        assert unseen_after_restart == []


# ---------------------------------------------------------------------------
# Token-aware context window pre-check tests
# ---------------------------------------------------------------------------


class TestApplyContextWindowLimit:
    """Test dynamic history reduction based on context window."""

    @staticmethod
    def _make_config(context_window: int | None = None) -> Config:
        """Create a Config with the given context_window on the default model."""
        return Config(
            agents={"test_agent": AgentConfig(display_name="Test")},
            models={"default": {"provider": "openai", "id": "test", "context_window": context_window}},
        )

    @staticmethod
    def _make_agent(
        role: str = "Short role.",
        instructions: list[str] | None = None,
        num_history_runs: int | None = None,
        num_history_messages: int | None = None,
    ) -> MagicMock:
        """Create a mock Agent with the given history settings."""
        agent = MagicMock(spec=Agent)
        agent.role = role
        agent.instructions = instructions or []
        agent.num_history_runs = num_history_runs
        agent.num_history_messages = num_history_messages
        agent.add_history_to_context = True
        return agent

    @staticmethod
    def _make_msg(content: str, *, from_history: bool = False, role: str = "user") -> Message:
        """Create a real Agno Message with text content."""
        return Message(role=role, content=content, from_history=from_history)

    def _make_session(
        self,
        run_contents: list[str],
        *,
        from_history_indices: set[int] | None = None,
        statuses: list[RunStatus] | None = None,
    ) -> AgentSession:
        """Create a session with runs containing the given content strings."""
        history_indices = from_history_indices or set()
        runs = []
        for i, content in enumerate(run_contents):
            msg = self._make_msg(content, from_history=i in history_indices)
            status = statuses[i] if statuses is not None else RunStatus.running
            runs.append(RunOutput(run_id=f"r{i}", messages=[msg], status=status))
        return AgentSession(session_id="sid", runs=runs)

    def test_no_context_window(self, tmp_path: object) -> None:
        """No context_window configured -> num_history_runs unchanged."""
        config = self._make_config(context_window=None)
        agent = self._make_agent(num_history_runs=5)
        _apply_context_window_limit(agent, "test_agent", config, "Hello", "sid", tmp_path)
        assert agent.num_history_runs == 5

    def test_no_session_id(self, tmp_path: object) -> None:
        """No session_id -> num_history_runs unchanged."""
        config = self._make_config(context_window=1000)
        agent = self._make_agent(num_history_runs=5)
        _apply_context_window_limit(agent, "test_agent", config, "Hello", None, tmp_path)
        assert agent.num_history_runs == 5

    def test_skips_when_num_history_messages_set(self, tmp_path: object) -> None:
        """When num_history_messages is set, skip run-based reduction."""
        config = self._make_config(context_window=100)
        agent = self._make_agent(num_history_messages=10)
        _apply_context_window_limit(agent, "test_agent", config, "Hello", "sid", tmp_path)
        assert agent.num_history_messages == 10

    def test_no_session_no_change(self, tmp_path: object) -> None:
        """No existing session -> num_history_runs unchanged."""
        config = self._make_config(context_window=100)
        agent = self._make_agent(num_history_runs=5)
        with (
            patch("mindroom.ai.create_session_storage"),
            patch("mindroom.ai._get_agent_session", return_value=None),
        ):
            _apply_context_window_limit(agent, "test_agent", config, "Hello", "sid", tmp_path)
        assert agent.num_history_runs == 5

    def test_within_budget_no_change(self, tmp_path: object) -> None:
        """Under threshold -> num_history_runs unchanged."""
        # context_window=10000, threshold=8000
        # Static: ~10 tokens, history: ~50 tokens -> well under 8000
        config = self._make_config(context_window=10000)
        agent = self._make_agent(role="Short role.", num_history_runs=None)
        session = self._make_session(["a" * 100, "b" * 100])
        _apply_context_window_limit(agent, "test_agent", config, "Hello", "sid", tmp_path, session=session)
        assert agent.num_history_runs is None  # Unchanged

    def test_reduces_history_over_budget(self, tmp_path: object) -> None:
        """Over threshold -> num_history_runs reduced."""
        # context_window=100, threshold=80
        # Static: role(40 chars = 10 tokens) + prompt(40 chars = 10 tokens) = 20 tokens
        # History: 5 runs x 100 chars = 25 tokens each, total = 125 tokens
        # Grand total: 20 + 125 = 145 > 80
        # Budget for history: 80 - 20 = 60 tokens
        # Each run ~ 25 tokens -> fits 2 runs (50 <= 60)
        config = self._make_config(context_window=100)
        agent = self._make_agent(role="x" * 40, num_history_runs=None)
        session = self._make_session(["a" * 100] * 5)
        _apply_context_window_limit(agent, "test_agent", config, "y" * 40, "sid", tmp_path, session=session)
        assert agent.num_history_runs == 2

    def test_reduces_with_explicit_limit(self, tmp_path: object) -> None:
        """When num_history_runs is already set but still too high, it gets reduced."""
        config = self._make_config(context_window=100)
        agent = self._make_agent(role="x" * 40, num_history_runs=5)
        session = self._make_session(["a" * 100] * 10)
        _apply_context_window_limit(agent, "test_agent", config, "y" * 40, "sid", tmp_path, session=session)
        assert agent.num_history_runs == 2

    def test_disables_history_when_latest_run_exceeds_budget(self, tmp_path: object) -> None:
        """If no runs fit budget, history is disabled for this run."""
        config = self._make_config(context_window=10)
        agent = self._make_agent(role="x" * 100, num_history_runs=5)
        session = self._make_session(["a" * 1000] * 5)
        _apply_context_window_limit(agent, "test_agent", config, "y" * 100, "sid", tmp_path, session=session)
        assert agent.add_history_to_context is False

    def test_disables_history_when_static_prompt_exhausts_budget(self, tmp_path: object) -> None:
        """If static prompt exceeds threshold, history is disabled for this run."""
        config = self._make_config(context_window=50)  # threshold=40
        agent = self._make_agent(role="x" * 200, num_history_runs=3)
        session = self._make_session(["a" * 20] * 3)
        _apply_context_window_limit(agent, "test_agent", config, "y" * 200, "sid", tmp_path, session=session)
        assert agent.add_history_to_context is False

    def test_no_change_when_already_within_limit(self, tmp_path: object) -> None:
        """With explicit num_history_runs that fits within budget, no change."""
        config = self._make_config(context_window=10000)
        agent = self._make_agent(role="Short.", num_history_runs=2)
        session = self._make_session(["a" * 100, "b" * 100])
        _apply_context_window_limit(agent, "test_agent", config, "Hello", "sid", tmp_path, session=session)
        assert agent.num_history_runs == 2  # Unchanged

    def test_ignores_messages_already_tagged_as_history(self, tmp_path: object) -> None:
        """Messages tagged with from_history should not be counted again."""
        config = self._make_config(context_window=100)  # threshold=80
        agent = self._make_agent(role="x" * 40, num_history_runs=None)
        session = self._make_session(
            ["a" * 100, "b" * 1000],
            from_history_indices={1},
        )
        _apply_context_window_limit(agent, "test_agent", config, "y" * 40, "sid", tmp_path, session=session)
        assert agent.num_history_runs is None
        assert agent.add_history_to_context is True

    def test_ignores_non_replayable_error_runs(self, tmp_path: object) -> None:
        """Errored runs should not influence history budgeting."""
        config = self._make_config(context_window=100)  # threshold=80
        agent = self._make_agent(role="x" * 40, num_history_runs=None)
        session = self._make_session(
            ["a" * 100, "b" * 1000],
            statuses=[RunStatus.running, RunStatus.error],
        )
        _apply_context_window_limit(agent, "test_agent", config, "y" * 40, "sid", tmp_path, session=session)
        assert agent.num_history_runs is None
        assert agent.add_history_to_context is True

    def test_model_config_context_window_field(self) -> None:
        """ModelConfig accepts and stores context_window."""
        mc = ModelConfig(provider="openai", id="gpt-4", context_window=128000)
        assert mc.context_window == 128000

    def test_model_config_context_window_defaults_none(self) -> None:
        """context_window defaults to None."""
        mc = ModelConfig(provider="openai", id="gpt-4")
        assert mc.context_window is None

    def test_model_config_context_window_must_be_positive(self) -> None:
        """context_window rejects zero values."""
        with pytest.raises(ValidationError):
            ModelConfig(provider="openai", id="gpt-4", context_window=0)
