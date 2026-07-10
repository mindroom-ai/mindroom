"""Orchestrator runtime lifecycle: orchestrator_main, API server, config updates, and startup/shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import nio
import pytest
import uvicorn

import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.approval_manager import (
    get_approval_store,
    initialize_approval_store,
)
from mindroom.authorization import is_authorized_sender as is_authorized_sender_for_test
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import (
    ROUTER_AGENT_NAME,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.hooks import (
    HookRegistry,
)
from mindroom.matrix.client import PermanentMatrixStartupError
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, AgentMatrixUser
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestration.plugin_watch import _collect_plugin_root_changes
from mindroom.orchestration.runtime import (
    _matrix_homeserver_startup_timeout_seconds_from_env,
    run_with_retry,
    wait_for_matrix_homeserver,
)
from mindroom.orchestrator import (
    _EmbeddedApiServerContext,
    _MultiAgentOrchestrator,
    _run_api_server,
    _run_auxiliary_task_forever,
    _SignalAwareUvicornServer,
    _wait_for_runtime_completion,
    main,
)
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.runtime_state import get_runtime_state, reset_runtime_state, set_runtime_ready
from mindroom.runtime_support import StartupThreadPrewarmRegistry
from mindroom.startup_errors import PermanentStartupError
from mindroom.tool_approval import _shutdown_approval_store
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from mindroom.tool_system.worker_routing import agent_state_root_path
from tests.approval_test_support import resolve_pending_approval as _resolve_pending_approval
from tests.bot_helpers import (
    AgentBotTestBase,
    _approval_reload_config,
    _approval_removal_plan,
    _cleanup_recorder,
    _live_pending_approval,
    _mock_approval_reload_bot,
    _mock_managed_bot,
    _run_orchestrator_start_until_ready,
    _runtime_bound_config,
    _wait_for_pending_approval_id,
)
from tests.conftest import (
    TEST_PASSWORD,
    bind_mock_config_cache,
    bind_runtime_paths,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    make_matrix_client_mock,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_orchestrator_main_reraises_permanent_startup_error(self, tmp_path: Path) -> None:
        """Permanent startup errors should stop the process and surface the failure."""
        reset_runtime_state()
        blocking_event = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        mock_orchestrator.stop = AsyncMock()
        mock_orchestrator.running = False

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await blocking_event.wait()

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            pytest.raises(PermanentMatrixStartupError, match="boom"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=False)

        mock_orchestrator.stop.assert_awaited_once()
        state = get_runtime_state()
        assert state.phase == "idle"
        assert state.detail is None

    @pytest.mark.asyncio
    async def test_embedded_uvicorn_signal_handler_requests_application_shutdown(self) -> None:
        """Uvicorn process signals should propagate to the top-level shutdown event."""
        shutdown_requested = asyncio.Event()

        async def app(scope: object, receive: object, send: object) -> None:
            del scope
            del receive
            del send

        server = _SignalAwareUvicornServer(
            uvicorn.Config(app, host="127.0.0.1", port=0),
            shutdown_requested,
        )

        with patch("mindroom.orchestrator.logger.info") as mock_info:
            server.handle_exit(signal.SIGTERM, None)

        assert shutdown_requested.is_set()
        assert server.should_exit is True
        assert server._captured_signals == []
        mock_info.assert_any_call(
            "embedded_api_server_signal_received",
            signal_number=int(signal.SIGTERM),
            signal_name="SIGTERM",
        )

    @pytest.mark.asyncio
    async def test_run_api_server_fails_fast_when_serve_returns_unexpectedly(self, tmp_path: Path) -> None:
        """server.serve() returning outside shutdown should be a fatal API lifecycle failure."""

        class ReturningServer:
            should_exit = False
            force_exit = False

            def __init__(self, _config: object, _shutdown_requested: asyncio.Event | None) -> None:
                pass

            async def serve(self) -> None:
                return None

        with (
            patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
            patch("mindroom.orchestrator._SignalAwareUvicornServer", ReturningServer),
            patch("mindroom.api.main.initialize_api_app"),
            patch("mindroom.api.main.bind_orchestrator_knowledge_refresh_scheduler"),
            patch("mindroom.orchestrator.logger.error") as mock_error,
            pytest.raises(RuntimeError, match="Embedded API server exited unexpectedly"),
        ):
            await _run_api_server(
                "127.0.0.1",
                0,
                "INFO",
                self._runtime_paths(tmp_path),
                shutdown_requested=asyncio.Event(),
            )

        mock_error.assert_called_once()
        assert mock_error.call_args.args == ("fatal_embedded_api_server_exit",)

    @pytest.mark.asyncio
    async def test_run_api_server_allows_expected_shutdown_after_serve_returns(self, tmp_path: Path) -> None:
        """server.serve() returning after an intentional shutdown should not be fatal."""

        class ReturningServer:
            should_exit = True
            force_exit = False

            def __init__(self, _config: object, _shutdown_requested: asyncio.Event | None) -> None:
                pass

            async def serve(self) -> None:
                return None

        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        with (
            patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
            patch("mindroom.orchestrator._SignalAwareUvicornServer", ReturningServer),
            patch("mindroom.api.main.initialize_api_app"),
            patch("mindroom.api.main.bind_orchestrator_knowledge_refresh_scheduler"),
            patch("mindroom.orchestrator.logger.error") as mock_error,
        ):
            await _run_api_server(
                "127.0.0.1",
                0,
                "INFO",
                self._runtime_paths(tmp_path),
                shutdown_requested=shutdown_requested,
            )

        mock_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_api_server_converts_uvicorn_system_exit_to_runtime_error(self, tmp_path: Path) -> None:
        """Uvicorn bind failures call sys.exit; the embedded task should report a normal runtime failure."""

        class ExitingServer:
            should_exit = False
            force_exit = False

            def __init__(self, _config: object, _shutdown_requested: asyncio.Event | None) -> None:
                pass

            async def serve(self) -> None:
                raise SystemExit(1)

        with (
            patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
            patch("mindroom.orchestrator._SignalAwareUvicornServer", ExitingServer),
            patch("mindroom.api.main.initialize_api_app"),
            patch("mindroom.api.main.bind_orchestrator_knowledge_refresh_scheduler"),
            patch("mindroom.orchestrator.logger.error") as mock_error,
            pytest.raises(RuntimeError, match="Embedded API server exited unexpectedly") as exc_info,
        ):
            await _run_api_server(
                "127.0.0.1",
                0,
                "INFO",
                self._runtime_paths(tmp_path),
                shutdown_requested=asyncio.Event(),
            )

        cause = exc_info.value.__cause__
        assert isinstance(cause, SystemExit)
        assert cause.code == 1
        mock_error.assert_called_once()
        assert mock_error.call_args.args == ("fatal_embedded_api_server_exit",)
        assert mock_error.call_args.kwargs["reason"] == "server.serve() raised SystemExit"
        assert mock_error.call_args.kwargs["exc_info"] == (SystemExit, cause, cause.__traceback__)

    @pytest.mark.asyncio
    async def test_runtime_completion_raises_done_orchestrator_failure_before_clean_shutdown_return(self) -> None:
        """Simultaneous shutdown/API completion must not hide orchestrator failures."""
        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        async def _failed_orchestrator() -> None:
            msg = "orchestrator failed during shutdown"
            raise RuntimeError(msg)

        async def _api_done() -> None:
            return None

        orchestrator_task = asyncio.create_task(_failed_orchestrator(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        api_task = asyncio.create_task(_api_done(), name="api_server")
        await asyncio.sleep(0)

        try:
            with pytest.raises(RuntimeError, match="orchestrator failed during shutdown"):
                await _wait_for_runtime_completion(
                    orchestrator_task=orchestrator_task,
                    shutdown_wait_task=shutdown_wait_task,
                    api_task=api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=_EmbeddedApiServerContext(host="127.0.0.1", port=0),
                )
        finally:
            await asyncio.gather(orchestrator_task, shutdown_wait_task, api_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_runtime_completion_raises_orchestrator_failure_during_api_shutdown_grace(self) -> None:
        """API shutdown grace must keep observing orchestrator failures."""
        shutdown_requested = asyncio.Event()
        shutdown_requested.set()

        async def _failed_orchestrator() -> None:
            await asyncio.sleep(0.01)
            msg = "orchestrator failed during API shutdown grace"
            raise RuntimeError(msg)

        async def _blocked_api() -> None:
            await asyncio.Event().wait()

        orchestrator_task = asyncio.create_task(_failed_orchestrator(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        api_task = asyncio.create_task(_blocked_api(), name="api_server")

        try:
            with (
                patch("mindroom.orchestrator._EMBEDDED_API_SHUTDOWN_GRACE_SECONDS", 0.05),
                pytest.raises(RuntimeError, match="orchestrator failed during API shutdown grace"),
            ):
                await _wait_for_runtime_completion(
                    orchestrator_task=orchestrator_task,
                    shutdown_wait_task=shutdown_wait_task,
                    api_task=api_task,
                    shutdown_requested=shutdown_requested,
                    api_server=_EmbeddedApiServerContext(host="127.0.0.1", port=0),
                )
        finally:
            api_task.cancel()
            await asyncio.gather(orchestrator_task, shutdown_wait_task, api_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_runtime_completion_raises_when_orchestrator_returns_without_shutdown_request(self) -> None:
        """A clean orchestrator return without a shutdown signal should restart the service."""
        shutdown_requested = asyncio.Event()

        async def _orchestrator_done() -> None:
            return None

        orchestrator_task = asyncio.create_task(_orchestrator_done(), name="orchestrator")
        shutdown_wait_task = asyncio.create_task(shutdown_requested.wait(), name="application_shutdown_wait")
        await asyncio.sleep(0)

        try:
            with pytest.raises(RuntimeError, match="MindRoom orchestrator exited unexpectedly"):
                await _wait_for_runtime_completion(
                    orchestrator_task=orchestrator_task,
                    shutdown_wait_task=shutdown_wait_task,
                    api_task=None,
                    shutdown_requested=shutdown_requested,
                    api_server=_EmbeddedApiServerContext(host="127.0.0.1", port=0),
                )
        finally:
            shutdown_wait_task.cancel()
            await asyncio.gather(orchestrator_task, shutdown_wait_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_orchestrator_main_logs_api_shutdown_timeout_before_cancelling_stuck_api_task(
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should wait for API grace timeout before cancelling a stuck API task."""
        reset_runtime_state()
        events: list[str] = []
        orchestrator_cancelled = asyncio.Event()
        api_cancelled = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()

        async def _start() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                orchestrator_cancelled.set()
                raise

        async def _api_requests_shutdown_and_blocks(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("api_cancelled")
                api_cancelled.set()
                raise

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        def _record_warning(*_args: object, **_kwargs: object) -> None:
            events.append("timeout_logged")

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown_and_blocks),
            patch("mindroom.orchestrator.logger.warning", side_effect=_record_warning) as mock_warning,
            patch("mindroom.orchestrator._EMBEDDED_API_SHUTDOWN_GRACE_SECONDS", 0.01),
        ):
            await asyncio.wait_for(
                main(
                    log_level="INFO",
                    runtime_paths=self._runtime_paths(tmp_path),
                    api=True,
                    api_host="127.0.0.1",
                ),
                timeout=1,
            )

        assert events[:2] == ["timeout_logged", "api_cancelled"]
        assert orchestrator_cancelled.is_set()
        assert api_cancelled.is_set()
        mock_warning.assert_called_once_with(
            "embedded_api_server_shutdown_timeout",
            host="127.0.0.1",
            port=8765,
            timeout_seconds=0.01,
        )
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_waits_for_api_server_graceful_shutdown_after_request(
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should let the embedded API server run its own cleanup before teardown."""
        reset_runtime_state()
        api_shutdown_started = asyncio.Event()
        api_allow_finish = asyncio.Event()
        api_completed = asyncio.Event()
        api_cancelled = asyncio.Event()
        start_blocker = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()

        async def _start() -> None:
            await start_blocker.wait()

        async def _api_requests_shutdown_then_finishes(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()
            api_shutdown_started.set()
            try:
                await api_allow_finish.wait()
            except asyncio.CancelledError:
                api_cancelled.set()
                raise
            api_completed.set()

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown_then_finishes),
        ):
            main_task = asyncio.create_task(
                main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True),
            )
            try:
                await asyncio.wait_for(api_shutdown_started.wait(), timeout=1)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not main_task.done()
                assert not api_cancelled.is_set()
                api_allow_finish.set()
                await asyncio.wait_for(main_task, timeout=1)
            finally:
                api_allow_finish.set()
                if not main_task.done():
                    main_task.cancel()
                await asyncio.gather(main_task, return_exceptions=True)

        assert api_completed.is_set()
        assert not api_cancelled.is_set()
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_stops_when_api_server_requests_shutdown(self, tmp_path: Path) -> None:
        """Regression coverage for API server signal shutdown not leaving the process half alive."""
        reset_runtime_state()
        start_released = asyncio.Event()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None

        async def _start() -> None:
            await start_released.wait()

        async def _stop() -> None:
            start_released.set()

        async def _api_requests_shutdown(
            _host: str,
            _port: int,
            _log_level: str,
            _runtime_paths: RuntimePaths,
            _knowledge_refresh_scheduler: object,
            shutdown_requested: asyncio.Event | None,
        ) -> None:
            assert shutdown_requested is not None
            shutdown_requested.set()

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)
        mock_orchestrator.stop = AsyncMock(side_effect=_stop)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_requests_shutdown),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True)

        mock_orchestrator.stop.assert_awaited_once()
        mock_orchestrator.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_fails_when_api_server_exits_unexpectedly(self, tmp_path: Path) -> None:
        """An unexpected API-server task failure should stop the top-level run non-silently."""
        reset_runtime_state()
        mock_orchestrator = MagicMock()
        mock_orchestrator.knowledge_refresh_scheduler = None
        mock_orchestrator.stop = AsyncMock()
        start_blocker = asyncio.Event()

        async def _start() -> None:
            await start_blocker.wait()

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        async def _api_fails(*_args: object, **_kwargs: object) -> None:
            msg = "Embedded API server exited unexpectedly"
            raise RuntimeError(msg)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch("mindroom.orchestrator._run_api_server", side_effect=_api_fails),
            pytest.raises(RuntimeError, match="Embedded API server exited unexpectedly"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=True)

        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_watches_resolved_config_path(self, tmp_path: Path) -> None:
        """The top-level config watcher should follow the orchestrator's canonical config path."""
        reset_runtime_state()
        watched_paths: list[Path] = []
        config_watcher_ran = asyncio.Event()
        resolved_config_path = (tmp_path / "nested" / "config.yaml").resolve()
        mock_orchestrator = MagicMock()
        mock_orchestrator.config_path = resolved_config_path
        mock_orchestrator._require_config_path.return_value = resolved_config_path
        mock_orchestrator.stop = AsyncMock()

        async def _watch_config_task(path: Path, _orchestrator: object) -> None:
            watched_paths.append(path)
            config_watcher_ran.set()

        async def _run_auxiliary(
            task_name: str,
            operation: Callable[[], Awaitable[None]],
            *,
            should_restart: Callable[[], bool] | None = None,
        ) -> None:
            del task_name
            del should_restart
            await operation()

        async def _start() -> None:
            await asyncio.wait_for(config_watcher_ran.wait(), timeout=1)
            msg = "boom"
            raise PermanentMatrixStartupError(msg)

        mock_orchestrator.start = AsyncMock(side_effect=_start)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._watch_config_task", side_effect=_watch_config_task),
            patch("mindroom.orchestrator._watch_skills_task", new=AsyncMock()),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", side_effect=_run_auxiliary),
            pytest.raises(PermanentMatrixStartupError, match="boom"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(tmp_path), api=False)

        assert watched_paths == [resolved_config_path]

    @pytest.mark.asyncio
    async def test_orchestrator_main_commits_runtime_storage_root_before_logging_and_credential_sync(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct orchestrator callers should get the same storage-root contract as the CLI wrapper."""
        reset_runtime_state()
        runtime_storage = tmp_path / "runtime-storage"
        observed_logging_root: Path | None = None
        observed_credentials_root: Path | None = None
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock(side_effect=RuntimeError("stop after storage capture"))
        mock_orchestrator.stop = AsyncMock()

        def _capture_logging(*, level: str, runtime_paths: RuntimePaths) -> None:
            del level
            nonlocal observed_logging_root
            observed_logging_root = runtime_paths.storage_root

        def _capture_credentials_sync(runtime_paths: RuntimePaths) -> None:
            nonlocal observed_credentials_root
            observed_credentials_root = runtime_paths.storage_root

        with (
            patch("mindroom.orchestrator.setup_logging", side_effect=_capture_logging),
            patch("mindroom.orchestrator.sync_env_to_credentials", side_effect=_capture_credentials_sync),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            pytest.raises(RuntimeError, match="stop after storage capture"),
        ):
            await main(log_level="INFO", runtime_paths=self._runtime_paths(runtime_storage), api=False)

        assert observed_logging_root == runtime_storage.resolve()
        assert observed_credentials_root == runtime_storage.resolve()

    @pytest.mark.asyncio
    async def test_orchestrator_main_shuts_down_primary_worker_manager(self, tmp_path: Path) -> None:
        """The orchestrator should clear stale workers before startup and shut them down on exit."""
        reset_runtime_state()
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock(side_effect=asyncio.CancelledError())
        mock_orchestrator.stop = AsyncMock()
        mock_orchestrator.running = False
        shutdown_calls: list[dict[str, object]] = []

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        runtime_paths = self._runtime_paths(tmp_path)
        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch(
                "mindroom.orchestrator.shutdown_primary_worker_manager",
                side_effect=lambda **kwargs: shutdown_calls.append(kwargs),
            ),
        ):
            await main(log_level="INFO", runtime_paths=runtime_paths, api=False)

        assert shutdown_calls == [{"timeout_seconds": 0.0}, {}]
        mock_orchestrator.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_main_shuts_down_primary_worker_manager_when_env_sync_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup failures before orchestrator creation should still shut down worker managers."""
        reset_runtime_state()
        shutdown_calls: list[dict[str, object]] = []
        runtime_paths = self._runtime_paths(tmp_path)

        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials", side_effect=RuntimeError("boom")),
            patch("mindroom.orchestrator._MultiAgentOrchestrator") as mock_orchestrator_cls,
            patch(
                "mindroom.orchestrator.shutdown_primary_worker_manager",
                side_effect=lambda **kwargs: shutdown_calls.append(kwargs),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await main(
                log_level="INFO",
                runtime_paths=runtime_paths,
                api=False,
            )

        assert shutdown_calls == [{"timeout_seconds": 0.0}, {}]
        mock_orchestrator_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_orchestrator_main_shuts_down_primary_worker_manager_when_stop_fails(self, tmp_path: Path) -> None:
        """Shutdown failures should still attempt primary worker manager shutdown."""
        reset_runtime_state()
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock(side_effect=asyncio.CancelledError())
        mock_orchestrator.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
        mock_orchestrator.running = False
        shutdown_calls: list[dict[str, object]] = []

        async def _blocked_auxiliary_task(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        runtime_paths = self._runtime_paths(tmp_path)
        with (
            patch("mindroom.orchestrator.setup_logging"),
            patch("mindroom.orchestrator.sync_env_to_credentials"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator", return_value=mock_orchestrator),
            patch("mindroom.orchestrator._run_auxiliary_task_forever", new=_blocked_auxiliary_task),
            patch(
                "mindroom.orchestrator.shutdown_primary_worker_manager",
                side_effect=lambda **kwargs: shutdown_calls.append(kwargs),
            ),
            pytest.raises(RuntimeError, match="stop boom"),
        ):
            await main(log_level="INFO", runtime_paths=runtime_paths, api=False)

        assert shutdown_calls == [{"timeout_seconds": 0.0}, {}]
        mock_orchestrator.stop.assert_awaited_once()


class TestMultiAgentOrchestrator:
    """Test cases for MultiAgentOrchestrator class."""

    @pytest.mark.asyncio
    async def test_orchestrator_initialization(self, tmp_path: Path) -> None:
        """Test MultiAgentOrchestrator initialization."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        assert orchestrator.agent_bots == {}
        assert not orchestrator.running

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_invites_authorized_users(self, tmp_path: Path) -> None:
        """Global users and room-permitted users should be invited to managed rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost", "!room2:localhost"],
                    ),
                },
                authorization={
                    "global_users": ["@alice:localhost"],
                    "room_permissions": {"!room1:localhost": ["@bob:localhost"]},
                    "default_room_access": False,
                },
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        room_members = {
            "!room1:localhost": {"@mindroom_general:localhost", "@mindroom_router:localhost"},
            "!room2:localhost": {"@mindroom_general:localhost", "@mindroom_router:localhost"},
        }

        async def mock_get_room_members(_client: AsyncMock, room_id: str) -> set[str]:
            return room_members[room_id]

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=list(room_members))),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
        ):
            await orchestrator._ensure_room_invitations()

        invited_users_by_room = {(call.args[1], call.args[2]) for call in mock_invite.await_args_list}
        assert invited_users_by_room == {
            ("!room1:localhost", "@alice:localhost"),
            ("!room2:localhost", "@alice:localhost"),
            ("!room1:localhost", "@bob:localhost"),
        }

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_skips_room_when_members_fetch_fails(self, tmp_path: Path) -> None:
        """A failed membership fetch must skip that room instead of inviting everyone into it."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost", "!room2:localhost"],
                    ),
                },
                authorization={
                    "global_users": ["@alice:localhost"],
                    "default_room_access": False,
                },
                mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        state = MatrixState.load(runtime_paths=orchestrator.runtime_paths)
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, "mindroom_user", "internal-password")
        state.save(runtime_paths=orchestrator.runtime_paths)

        room_members: dict[str, set[str] | None] = {
            "!room1:localhost": None,
            "!room2:localhost": {"@mindroom_general:localhost", "@mindroom_router:localhost"},
        }

        async def mock_get_room_members(_client: AsyncMock, room_id: str) -> set[str] | None:
            return room_members[room_id]

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=list(room_members))),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
        ):
            await orchestrator._ensure_room_invitations()

        invited_users_by_room = {(call.args[1], call.args[2]) for call in mock_invite.await_args_list}
        assert invited_users_by_room == {
            ("!room2:localhost", "@mindroom_user:localhost"),
            ("!room2:localhost", "@alice:localhost"),
        }

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_invites_authorized_users_to_standalone_rooms(
        self,
        tmp_path: Path,
    ) -> None:
        """Managed rooms without responders should still invite authorized users."""
        config = _runtime_bound_config(
            Config(
                rooms={"lobby": {"display_name": "Lobby"}},
                authorization={
                    "global_users": ["@alice:localhost"],
                    "default_room_access": False,
                },
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}
        state = MatrixState.load(runtime_paths=orchestrator.runtime_paths)
        state.add_room("lobby", "!room1:localhost", "#lobby:localhost", "Lobby")
        state.save(runtime_paths=orchestrator.runtime_paths)

        async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> set[str]:
            return {"@mindroom_router:localhost"}

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!room1:localhost"])),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
            patch("mindroom.orchestrator.configured_bot_user_ids_for_room", return_value=set()),
        ):
            await orchestrator._ensure_room_invitations()

        mock_invite.assert_awaited_once_with(router_bot.client, "!room1:localhost", "@alice:localhost")

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_skips_non_matrix_authorization_entries(self, tmp_path: Path) -> None:
        """Only concrete Matrix user IDs should be invited from authorization lists."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost"],
                    ),
                },
                authorization={
                    "global_users": ["@alice:localhost", "@admin:*", "alice"],
                    "default_room_access": False,
                },
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> set[str]:
            return {"@mindroom_general:localhost", "@mindroom_router:localhost"}

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.is_authorized_sender", side_effect=is_authorized_sender_for_test),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!room1:localhost"])),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
        ):
            await orchestrator._ensure_room_invitations()

        invited_users = [call.args[2] for call in mock_invite.await_args_list]
        assert invited_users == ["@alice:localhost"]

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_skips_internal_user_when_unconfigured(self, tmp_path: Path) -> None:
        """When mindroom_user is unset, stale internal account credentials must not trigger invites."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!room1:localhost"],
                    ),
                },
                authorization={"default_room_access": False},
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        state = MatrixState.load(runtime_paths=orchestrator.runtime_paths)
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, "legacy_internal_user", "legacy-password")
        state.save(runtime_paths=orchestrator.runtime_paths)

        async def mock_get_room_members(_client: AsyncMock, _room_id: str) -> set[str]:
            return {"@mindroom_general:localhost", "@mindroom_router:localhost"}

        mock_invite = AsyncMock(return_value=True)

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!room1:localhost"])),
            patch("mindroom.orchestrator.get_room_members", side_effect=mock_get_room_members),
            patch("mindroom.orchestrator.invite_to_room", mock_invite),
        ):
            await orchestrator._ensure_room_invitations()

        mock_invite.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_room_invitations_ignores_persisted_ad_hoc_invited_rooms(self, tmp_path: Path) -> None:
        """Persisted ad-hoc invites must not leak into normal invitation fan-out."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["!managed:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        invited_rooms_path = agent_state_root_path(runtime_paths.storage_root, "general") / "invited_rooms.json"
        invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
        invited_rooms_path.write_text('[\n  "!ad-hoc:localhost"\n]\n', encoding="utf-8")

        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = MagicMock()
        router_bot.client = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        with (
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
            patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=["!ad-hoc:localhost"])),
            patch("mindroom.orchestrator.get_room_members", new=AsyncMock()) as mock_get_room_members,
            patch("mindroom.orchestrator.invite_to_room", AsyncMock()) as mock_invite,
        ):
            await orchestrator._ensure_room_invitations()

        mock_get_room_members.assert_not_awaited()
        mock_invite.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_skips_internal_user_join_when_unconfigured(self, tmp_path: Path) -> None:
        """When mindroom_user is unset, orchestrator should not attempt internal-user room joins."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        bot = AsyncMock()
        bot.agent_name = "general"
        bot.rooms = []
        bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()) as mock_ensure_user_in_rooms,
        ):
            await orchestrator._setup_rooms_and_memberships([bot])

        assert bot.rooms == ["!room1:localhost"]
        mock_ensure_user_in_rooms.assert_not_awaited()
        assert bot.ensure_rooms.await_count == 2

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_retries_invites_after_router_joins(self, tmp_path: Path) -> None:
        """Invite-only existing rooms should get a second invitation/join pass after router joins."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
                mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config

        router_bot = AsyncMock()
        router_bot.agent_name = ROUTER_AGENT_NAME
        router_bot.rooms = []
        router_bot.ensure_rooms = AsyncMock()

        general_bot = AsyncMock()
        general_bot.agent_name = "general"
        general_bot.rooms = []
        general_bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()) as mock_invitations,
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()) as mock_ensure_user_in_rooms,
        ):
            await orchestrator._setup_rooms_and_memberships([router_bot, general_bot])

        assert router_bot.rooms == ["!room1:localhost"]
        assert general_bot.rooms == ["!room1:localhost"]
        assert router_bot.ensure_rooms.await_count == 1
        assert general_bot.ensure_rooms.await_count == 2
        assert mock_invitations.await_count == 2
        assert mock_ensure_user_in_rooms.await_count == 2

    @pytest.mark.asyncio
    async def test_setup_rooms_and_memberships_reruns_room_reconciliation_after_router_joins(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup should rerun room reconciliation after the router joins existing rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="GeneralAgent",
                        rooms=["lobby"],
                    ),
                },
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = config

        router_joined = False
        reconciliation_join_states: list[bool] = []

        async def record_room_reconciliation() -> None:
            reconciliation_join_states.append(router_joined)

        async def router_join_rooms() -> None:
            nonlocal router_joined
            router_joined = True

        router_bot = AsyncMock()
        router_bot.agent_name = ROUTER_AGENT_NAME
        router_bot.rooms = []
        router_bot.ensure_rooms = AsyncMock(side_effect=router_join_rooms)

        general_bot = AsyncMock()
        general_bot.agent_name = "general"
        general_bot.rooms = []
        general_bot.ensure_rooms = AsyncMock()

        with (
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock(side_effect=record_room_reconciliation)),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
            patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
            patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
            patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
            patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()),
        ):
            await orchestrator._setup_rooms_and_memberships([router_bot, general_bot])

        assert reconciliation_join_states == [False, True]
        assert router_bot.ensure_rooms.await_count == 1
        assert general_bot.ensure_rooms.await_count == 2

    @pytest.mark.asyncio
    async def test_reconcile_post_update_rooms_runs_for_room_metadata_changes(
        self,
        tmp_path: Path,
    ) -> None:
        """Display-name-only room edits should run room reconciliation without bot restarts."""
        config = _runtime_bound_config(
            Config(rooms={"lobby": {"display_name": "Project Lobby"}}),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
        orchestrator.config = config
        plan = ConfigUpdatePlan(
            new_config=config,
            changed_mcp_servers=set(),
            configured_entities={ROUTER_AGENT_NAME},
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
            room_metadata_changed=True,
        )

        with (
            patch.object(
                orchestrator,
                "_ensure_rooms_exist",
                new=AsyncMock(return_value={"lobby": "!room1:localhost"}),
            ) as ensure_rooms,
            patch.object(orchestrator, "_ensure_root_space", new=AsyncMock()) as ensure_space,
        ):
            await orchestrator._reconcile_post_update_rooms(plan, changed_entities=set())

        ensure_rooms.assert_awaited_once_with()
        ensure_space.assert_awaited_once_with({"lobby": "!room1:localhost"})

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator initialization
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.orchestrator.load_config")
    async def test_orchestrator_initialize(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test initializing the orchestrator with agents."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        cache_path = bind_mock_config_cache(mock_config, tmp_path)
        mock_load_config.return_value = mock_config

        with patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            try:
                await orchestrator.initialize()

                # Should have 3 bots: calculator, general, and router
                assert len(orchestrator.agent_bots) == 3
                assert "calculator" in orchestrator.agent_bots
                assert "general" in orchestrator.agent_bots
                assert "router" in orchestrator.agent_bots
                assert orchestrator._runtime_support.event_cache.db_path == cache_path
            finally:
                await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_orchestrator_initialize_uses_custom_config_path(self, tmp_path: Path) -> None:
        """Initialize should load the exact config file owned by the orchestrator."""
        config_path = tmp_path / "custom-config.yaml"
        mock_config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)

        with (
            patch("mindroom.orchestrator.load_config", return_value=mock_config) as mock_load_config,
            patch("mindroom.orchestrator.load_plugins"),
            patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()),
            patch.object(
                _MultiAgentOrchestrator,
                "_prepare_entity_accounts",
                new=AsyncMock(
                    return_value={
                        ROUTER_AGENT_NAME: AgentMatrixUser(
                            agent_name=ROUTER_AGENT_NAME,
                            user_id="@mindroom_router:localhost",
                            display_name="Router",
                            password=TEST_PASSWORD,
                        ),
                    },
                ),
            ),
            patch.object(_MultiAgentOrchestrator, "_create_managed_bot"),
        ):
            orchestrator = _MultiAgentOrchestrator(
                runtime_paths=resolve_runtime_paths(
                    config_path=config_path,
                    storage_path=tmp_path,
                    process_env={},
                ),
            )
            try:
                await orchestrator.initialize()
            finally:
                await orchestrator._close_runtime_support_services()

        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    async def test_initialize_degrades_when_shared_event_cache_init_fails(self, tmp_path: Path) -> None:
        """Initialize should keep starting bots when the shared event cache cannot open."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))

        with (
            patch("mindroom.orchestrator.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_prepare_user_account", new=AsyncMock()),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch(
                "mindroom.runtime_support.SqliteEventCache.initialize",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(_MultiAgentOrchestrator, "_create_managed_bot") as mock_create_managed_bot,
        ):
            await orchestrator.initialize()

        assert orchestrator.config is config
        assert mock_create_managed_bot.call_count == 2
        assert orchestrator._runtime_support.event_cache.is_initialized is False

    @pytest.mark.asyncio
    async def test_sync_event_cache_service_uses_shared_runtime_support_sync(self, tmp_path: Path) -> None:
        """Shared runtime cache lifecycle should route through the shared sync helper."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        router_bot = _mock_managed_bot(config)
        general_bot = _mock_managed_bot(config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
        initial_support = orchestrator._runtime_support
        synced_support = SimpleNamespace(
            event_cache=make_event_cache_mock(),
            event_cache_write_coordinator=make_event_cache_write_coordinator_mock(),
            startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        )

        with patch(
            "mindroom.orchestrator.sync_owned_runtime_support",
            new=AsyncMock(return_value=synced_support),
            create=True,
        ) as sync_owned_runtime_support:
            await orchestrator._sync_event_cache_service(config)

        sync_owned_runtime_support.assert_awaited_once()
        assert sync_owned_runtime_support.await_args.args == (initial_support,)
        assert sync_owned_runtime_support.await_args.kwargs == {
            "cache_config": config.cache,
            "runtime_paths": orchestrator.runtime_paths,
            "logger": ANY,
            "background_task_owner": orchestrator._event_cache_write_task_owner,
            "init_failure_reason_prefix": "shared_runtime_init_failed",
            "log_db_path_change": True,
        }
        assert orchestrator._runtime_support is synced_support
        assert router_bot.event_cache is synced_support.event_cache
        assert general_bot.event_cache is synced_support.event_cache
        assert router_bot.event_cache_write_coordinator is synced_support.event_cache_write_coordinator
        assert general_bot.event_cache_write_coordinator is synced_support.event_cache_write_coordinator

    @pytest.mark.asyncio
    async def test_initialize_does_not_activate_hook_runtime_before_user_account_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup must not swap the live hook runtime before user-account prep succeeds."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = MagicMock()
        config.agents = {}
        config.teams = {}
        initial_hook_registry = orchestrator.hook_registry
        new_hook_registry = HookRegistry.empty()

        with (
            patch("mindroom.orchestrator.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch("mindroom.orchestrator.HookRegistry.from_plugins", return_value=new_hook_registry),
            patch("mindroom.orchestrator.set_scheduling_hook_registry") as mock_set_scheduling_hook_registry,
            patch.object(
                orchestrator,
                "_prepare_user_account",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(_MultiAgentOrchestrator, "_create_managed_bot") as mock_create_managed_bot,
            pytest.raises(RuntimeError, match="boom"),
        ):
            await orchestrator.initialize()

        assert orchestrator.config is None
        assert orchestrator.hook_registry is initial_hook_registry
        mock_set_scheduling_hook_registry.assert_not_called()
        mock_create_managed_bot.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator start
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.orchestrator.load_config")
    async def test_orchestrator_start(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test starting all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        bind_mock_config_cache(mock_config, tmp_path)
        mock_load_config.return_value = mock_config

        with patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            try:
                await orchestrator.initialize()  # Need to initialize first

                # Mock start for all bots to avoid actual login/setup
                start_mocks = []
                for bot in orchestrator.agent_bots.values():
                    # Create a mock that tracks the call
                    mock_start = AsyncMock()
                    # Replace start with our mock
                    bot.start = mock_start
                    start_mocks.append(mock_start)
                    bot.running = False

                # Start the orchestrator but don't wait for sync_forever
                start_tasks = [bot.start() for bot in orchestrator.agent_bots.values()]

                await asyncio.gather(*start_tasks)
                orchestrator.running = True  # Manually set since we're not calling orchestrator.start()

                assert orchestrator.running
                # Verify start was called for each bot
                for mock_start in start_mocks:
                    mock_start.assert_called_once()
            finally:
                await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_orchestrator_start_sets_up_rooms_before_auxiliary_workers(self, tmp_path: Path) -> None:
        """Room creation/invites should happen before auxiliary runtime workers."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        bot = MagicMock()
        bot.agent_name = "router"
        bot.try_start = AsyncMock(return_value=True)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _setup_rooms(_: list[Any]) -> None:
            call_order.append("setup_rooms")

        async def _sync_runtime_support_services(*_args: object, **_kwargs: object) -> None:
            call_order.append("support_services")

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
            patch.object(orchestrator, "_sync_runtime_support_services", side_effect=_sync_runtime_support_services),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert call_order == ["wait_for_homeserver", "setup_rooms", "support_services"]
        bot.try_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_start_syncs_knowledge_watchers_after_runtime_starts(self, tmp_path: Path) -> None:
        """Normal startup should start watch-owned knowledge refresh after reply paths are live."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        config = MagicMock()
        orchestrator.config = config

        bot = MagicMock()
        bot.agent_name = "router"
        bot.try_start = AsyncMock(return_value=True)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        async def _sync_runtime_support_services(*args: object, **kwargs: object) -> None:
            assert orchestrator.running is True
            assert args == (config,)
            assert kwargs == {"start_watcher": True}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(
                orchestrator,
                "_sync_runtime_support_services",
                side_effect=_sync_runtime_support_services,
            ) as sync_runtime_support_services,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        sync_runtime_support_services.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orchestrator_start_discards_tool_approval_cards_on_router_ready(
        self,
        tmp_path: Path,
    ) -> None:
        """Router readiness should trigger Matrix-backed startup discard after room setup."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = [SimpleNamespace(timeout_days=10.0)]

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = False
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")

        async def _start_bot() -> bool:
            bot.running = True
            return True

        bot.try_start = AsyncMock(side_effect=_start_bot)

        async def _emit_bot_ready(_response: object) -> None:
            await orchestrator.handle_bot_ready(bot)

        bot._on_sync_response = AsyncMock(side_effect=_emit_bot_ready)
        bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": bot}

        call_order: list[str] = []
        startup_discarded = asyncio.Event()

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _setup_rooms(_: list[Any]) -> None:
            call_order.append("setup_rooms")

        async def _sync_runtime_support_services(*_: object, **__: object) -> None:
            call_order.append("support_services")

        async def _discard_pending_on_startup(*, lookback_hours: int) -> int:
            assert lookback_hours == 240
            call_order.append("startup_discard")
            startup_discarded.set()
            return 2

        async def _sync_forever_with_restart(started_bot: object) -> None:
            await cast("Any", started_bot)._on_sync_response(MagicMock(spec=nio.SyncResponse))

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
            patch(
                "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
                new=AsyncMock(side_effect=_discard_pending_on_startup),
            ) as expire_orphaned_approval_cards_on_startup,
            patch.object(orchestrator, "_sync_runtime_support_services", side_effect=_sync_runtime_support_services),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", side_effect=_sync_forever_with_restart),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)
            await asyncio.wait_for(startup_discarded.wait(), timeout=1.0)

        assert call_order == [
            "wait_for_homeserver",
            "setup_rooms",
            "support_services",
            "startup_discard",
        ]
        expire_orphaned_approval_cards_on_startup.assert_awaited_once_with(lookback_hours=240)
        bot.try_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_bot_ready_skips_startup_discard_for_non_router_bots(
        self,
        tmp_path: Path,
    ) -> None:
        """Only the router owns startup approval cleanup."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        bot = MagicMock()
        bot.agent_name = "code"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_code:localhost")

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(),
        ) as expire_orphaned_approval_cards_on_startup:
            await orchestrator.handle_bot_ready(bot)

        expire_orphaned_approval_cards_on_startup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_transport_waits_for_runtime_support_before_startup_discard(
        self,
        tmp_path: Path,
    ) -> None:
        """Router first sync alone must not discard startup approval cards before runtime support is ready."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = [SimpleNamespace(timeout_days=10.0)]

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator.agent_bots = {"router": bot}

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(return_value=1),
        ) as expire_orphaned_approval_cards_on_startup:
            orchestrator._approval_transport.reset_startup_cleanup_gate()
            await orchestrator.handle_bot_ready(bot)
            expire_orphaned_approval_cards_on_startup.assert_not_awaited()

            await orchestrator._approval_transport.mark_startup_runtime_support_ready()

        expire_orphaned_approval_cards_on_startup.assert_awaited_once_with(lookback_hours=240)

    @pytest.mark.asyncio
    async def test_approval_transport_waits_for_router_ready_before_startup_discard(
        self,
        tmp_path: Path,
    ) -> None:
        """Runtime support readiness alone must not discard startup approval cards before router first sync."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = [SimpleNamespace(timeout_days=10.0)]

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator.agent_bots = {"router": bot}

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(return_value=1),
        ) as expire_orphaned_approval_cards_on_startup:
            orchestrator._approval_transport.reset_startup_cleanup_gate()
            await orchestrator._approval_transport.mark_startup_runtime_support_ready()
            expire_orphaned_approval_cards_on_startup.assert_not_awaited()

            await orchestrator.handle_bot_ready(bot)

        expire_orphaned_approval_cards_on_startup.assert_awaited_once_with(lookback_hours=240)

    @pytest.mark.asyncio
    async def test_approval_transport_concurrent_startup_gates_discard_once(
        self,
        tmp_path: Path,
    ) -> None:
        """Router-ready and runtime-ready races must still run startup discard once."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = []

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator.agent_bots = {"router": bot}

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(return_value=1),
        ) as expire_orphaned_approval_cards_on_startup:
            orchestrator._approval_transport.reset_startup_cleanup_gate()
            await asyncio.gather(
                orchestrator.handle_bot_ready(bot),
                orchestrator._approval_transport.mark_startup_runtime_support_ready(),
            )

        expire_orphaned_approval_cards_on_startup.assert_awaited_once_with(lookback_hours=168)

    @pytest.mark.asyncio
    async def test_approval_transport_reset_allows_fresh_startup_discard(
        self,
        tmp_path: Path,
    ) -> None:
        """A fresh runtime start must be able to run startup discard after the previous run did."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()
        orchestrator.config.tool_approval.timeout_days = 7.0
        orchestrator.config.tool_approval.rules = []

        bot = MagicMock()
        bot.agent_name = "router"
        bot.running = True
        bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        orchestrator.agent_bots = {"router": bot}

        with patch(
            "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
            new=AsyncMock(return_value=1),
        ) as expire_orphaned_approval_cards_on_startup:
            orchestrator._approval_transport.reset_startup_cleanup_gate()
            await orchestrator.handle_bot_ready(bot)
            await orchestrator._approval_transport.mark_startup_runtime_support_ready()

            orchestrator._approval_transport.reset_startup_cleanup_gate()
            await orchestrator._approval_transport.mark_startup_runtime_support_ready()
            await orchestrator.handle_bot_ready(bot)

        assert expire_orphaned_approval_cards_on_startup.await_count == 2

    @pytest.mark.asyncio
    async def test_orchestrator_waits_for_homeserver_before_initialize(self, tmp_path: Path) -> None:
        """Matrix readiness must gate initialize(), which creates the internal Matrix user."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        call_order: list[str] = []

        async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
            call_order.append("wait_for_homeserver")

        async def _initialize() -> None:
            call_order.append("initialize")
            orchestrator.config = MagicMock()
            bot = MagicMock()
            bot.agent_name = "router"
            bot.try_start = AsyncMock(return_value=True)
            bot.stop = AsyncMock()
            orchestrator.agent_bots = {"router": bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
            patch.object(orchestrator, "initialize", side_effect=_initialize),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert call_order[:2] == ["wait_for_homeserver", "initialize"]

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_returns_when_versions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The homeserver wait should return as soon as `/versions` succeeds."""
        calls = 0

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, url: str) -> httpx.Response:
                nonlocal calls
                calls += 1
                request = httpx.Request("GET", url)
                return httpx.Response(200, json={"versions": ["v1.1"]}, request=request)

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        await wait_for_matrix_homeserver(
            runtime_paths=runtime_paths,
            timeout_seconds=0.1,
            retry_interval_seconds=0,
        )

        assert calls == 1

    def test_matrix_homeserver_startup_timeout_defaults_to_infinite(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset or zero startup timeouts should wait forever."""
        monkeypatch.delenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", raising=False)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) is None

        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "0")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) is None

    def test_matrix_homeserver_startup_timeout_reads_positive_seconds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A positive timeout env var should bound the startup wait."""
        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "45")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        assert _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths) == 45

    def test_matrix_homeserver_startup_timeout_rejects_negative_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative timeout values are invalid."""
        monkeypatch.setenv("MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS", "-1")
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )
        with pytest.raises(ValueError, match="must be 0 or a positive integer"):
            _matrix_homeserver_startup_timeout_seconds_from_env(runtime_paths)

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_retries_on_connection_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient transport failures should be retried until `/versions` succeeds."""
        responses: list[Exception | httpx.Response] = [
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom again"),
            httpx.Response(
                200,
                json={"versions": ["v1.1"]},
                request=httpx.Request("GET", "http://localhost/_matrix/client/versions"),
            ),
        ]

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, _url: str) -> httpx.Response:
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        await wait_for_matrix_homeserver(
            runtime_paths=runtime_paths,
            timeout_seconds=0.1,
            retry_interval_seconds=0,
        )

        assert responses == []

    @pytest.mark.asyncio
    async def test_wait_for_matrix_homeserver_times_out_when_never_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The homeserver wait should fail fast when `/versions` never becomes valid."""

        class _FakeAsyncClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
                del exc_type, exc, tb

            async def get(self, url: str) -> httpx.Response:
                request = httpx.Request("GET", url)
                return httpx.Response(503, text="starting", request=request)

        monkeypatch.setattr("mindroom.orchestration.runtime.httpx.AsyncClient", _FakeAsyncClient)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env=dict(os.environ),
        )

        with pytest.raises(TimeoutError, match="Timed out waiting for Matrix homeserver"):
            await wait_for_matrix_homeserver(
                runtime_paths=runtime_paths,
                timeout_seconds=0.01,
                retry_interval_seconds=0.001,
            )

    @pytest.mark.asyncio
    async def test_orchestrator_start_schedules_retry_for_failed_agents(self, tmp_path: Path) -> None:
        """Startup should keep degraded agents around and retry them in the background."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.try_start = AsyncMock(return_value=True)
        router_bot.stop = AsyncMock()

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(return_value=False)
        failing_bot.stop = AsyncMock()

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert "general" in orchestrator.agent_bots
        mock_schedule_retry.assert_awaited_once_with("general")

    @pytest.mark.asyncio
    async def test_orchestrator_start_skips_retry_for_permanent_failures(self, tmp_path: Path) -> None:
        """Permanent startup failures should leave bots disabled without retry loops."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.try_start = AsyncMock(return_value=True)
        router_bot.stop = AsyncMock()

        failing_bot = MagicMock()
        failing_bot.agent_name = "general"
        failing_bot.try_start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        failing_bot.stop = AsyncMock()

        orchestrator.agent_bots = {"router": router_bot, "general": failing_bot}

        with (
            patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
            patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
        ):
            await _run_orchestrator_start_until_ready(orchestrator)

        assert "general" in orchestrator.agent_bots
        mock_schedule_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_background_bot_recovery_stops_on_permanent_room_setup_failure(self, tmp_path: Path) -> None:
        """Recovered background starts should not retry permanent room setup failures forever."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        orchestrator.config = MagicMock()

        bot = MagicMock()
        bot.agent_name = "general"
        bot.try_start = AsyncMock(return_value=True)
        orchestrator.agent_bots = {"general": bot}

        with (
            patch.object(orchestrator, "_entities_blocked_by_failed_mcp_servers", return_value=set()),
            patch.object(
                orchestrator,
                "_setup_rooms_and_memberships",
                new=AsyncMock(side_effect=PermanentStartupError("bad ADC")),
            ),
            pytest.raises(PermanentStartupError, match="bad ADC"),
        ):
            await orchestrator._run_bot_start_retry("general")

    @pytest.mark.asyncio
    async def test_shutdown_expires_in_flight_approval_send_after_event_id_arrives(  # noqa: PLR0915
        self,
        tmp_path: Path,
    ) -> None:
        """Shutdown should settle approval sends that receive a card id during shutdown."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator.config = bind_runtime_paths(Config(), runtime_paths)
        orchestrator._capture_runtime_loop()

        send_started = asyncio.Event()
        allow_send_to_finish = asyncio.Event()

        async def _room_send(
            room_id: str,
            message_type: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> nio.RoomSendResponse:
            del content
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            send_started.set()
            await allow_send_to_finish.wait()
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
        router_client.room_send = AsyncMock(side_effect=_room_send)
        router_client.rooms["!room:localhost"].add_member(router_client.user_id, "Router", None)
        router_bot = MagicMock()
        router_bot.agent_name = "router"
        router_bot.running = True
        router_bot.client = router_client
        router_bot.event_cache = make_event_cache_mock()
        router_bot.stop = AsyncMock()

        code_bot = MagicMock()
        code_bot.agent_name = "code"
        code_bot.running = True
        code_bot.client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
        code_bot.stop = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id=None,
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            await send_started.wait()
            orchestrator._knowledge_refresh_scheduler = MagicMock()
            orchestrator._knowledge_refresh_scheduler.shutdown = AsyncMock()

            with (
                patch.object(orchestrator.config_reload, "cancel", new=AsyncMock()),
                patch.object(orchestrator, "_stop_memory_auto_flush_worker", new=AsyncMock()),
                patch.object(orchestrator._knowledge_source_watcher, "shutdown", new=AsyncMock()),
                patch.object(orchestrator, "_cancel_bot_start_tasks", new=AsyncMock()),
                patch.object(orchestrator, "_stop_mcp_manager", new=AsyncMock()),
                patch.object(orchestrator, "_close_runtime_support_services", new=AsyncMock()),
            ):
                stop_task = asyncio.create_task(orchestrator.stop())
                await asyncio.sleep(0)
                assert stop_task.done() is False
                allow_send_to_finish.set()
                await stop_task

            decision = await asyncio.wait_for(task, timeout=1)
            assert decision.status == "expired"
            assert decision.reason == "MindRoom shut down before approval completed."
            assert router_bot.running is False
            router_bot.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
        finally:
            allow_send_to_finish.set()
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_orchestrator_stop_shuts_down_approvals_before_mcp_manager(
        self,
        tmp_path: Path,
    ) -> None:
        """Pending approvals should expire even if MCP shutdown fails."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
        calls: list[str] = []

        async def _shutdown_approvals() -> None:
            calls.append("approvals")

        async def _stop_mcp_manager() -> None:
            calls.append("mcp")
            msg = "mcp shutdown failed"
            raise RuntimeError(msg)

        orchestrator._knowledge_refresh_scheduler = MagicMock()
        orchestrator._knowledge_refresh_scheduler.shutdown = AsyncMock()

        with (
            patch(
                "mindroom.orchestrator.shutdown_approval_runtime",
                new=AsyncMock(side_effect=_shutdown_approvals),
            ) as mock_shutdown_approvals,
            patch.object(orchestrator.config_reload, "cancel", new=AsyncMock()),
            patch.object(orchestrator, "_stop_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator._knowledge_source_watcher, "shutdown", new=AsyncMock()),
            patch.object(orchestrator, "_cancel_bot_start_tasks", new=AsyncMock()),
            patch.object(orchestrator, "_stop_mcp_manager", new=AsyncMock(side_effect=_stop_mcp_manager)),
            pytest.raises(RuntimeError, match="mcp shutdown failed"),
        ):
            await orchestrator.stop()

        assert calls == ["approvals", "mcp"]
        mock_shutdown_approvals.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_restarts_after_failure(self) -> None:
        """Auxiliary supervisors should restart tasks that crash."""
        started = asyncio.Event()
        calls = 0

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "boom"
                raise RuntimeError(msg)
            started.set()
            await asyncio.Future()

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator.logger.exception"),
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == 2

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_logs_traceback_on_failure(self) -> None:
        """Auxiliary task crashes should keep traceback logging intact."""
        started = asyncio.Event()
        calls = 0

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "boom"
                raise RuntimeError(msg)
            started.set()
            await asyncio.Future()

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0),
            patch("mindroom.orchestrator.logger.exception") as mock_exception,
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        mock_exception.assert_called_once_with(
            "Auxiliary task crashed; restarting",
            task_name="test task",
        )

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_exits_cleanly_when_shutdown_requested(self) -> None:
        """Shutdown should suppress restart logging for clean auxiliary exits."""
        shutdown_requested = False
        calls = 0

        async def _operation() -> None:
            nonlocal calls, shutdown_requested
            calls += 1
            shutdown_requested = True

        with patch("mindroom.orchestrator.logger.warning") as mock_warning:
            await _run_auxiliary_task_forever(
                "test task",
                _operation,
                should_restart=lambda: not shutdown_requested,
            )

        assert calls == 1
        mock_warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_suppresses_crash_log_when_shutdown_requested(self) -> None:
        """Shutdown should suppress crash logging for auxiliary teardown errors."""
        shutdown_requested = False
        calls = 0

        async def _operation() -> None:
            nonlocal calls, shutdown_requested
            calls += 1
            shutdown_requested = True
            msg = "boom"
            raise RuntimeError(msg)

        with patch("mindroom.orchestrator.logger.exception") as mock_exception:
            await _run_auxiliary_task_forever(
                "test task",
                _operation,
                should_restart=lambda: not shutdown_requested,
            )

        assert calls == 1
        mock_exception.assert_not_called()

    def test_signal_aware_uvicorn_server_marks_shutdown_requested_on_signal(self) -> None:
        """Uvicorn signal handling should surface shutdown intent before serve() returns."""
        shutdown_requested = asyncio.Event()
        config = uvicorn.Config(app=lambda _scope, _receive, _send: None)
        server = _SignalAwareUvicornServer(config, shutdown_requested)

        with patch.object(uvicorn.Server, "handle_exit"):
            server.handle_exit(signal.SIGINT, None)

        assert shutdown_requested.is_set()

    @pytest.mark.asyncio
    async def test_run_auxiliary_task_forever_resets_backoff_after_healthy_run(self) -> None:
        """Long healthy runs should reset crash-loop backoff for auxiliary tasks."""
        retry_attempts: list[int] = []
        calls = 0
        third_start = asyncio.Event()

        async def _operation() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(0.02)
            if calls == 3:
                third_start.set()
                await asyncio.Future()
            msg = "boom"
            raise RuntimeError(msg)

        with (
            patch("mindroom.orchestrator._AUXILIARY_TASK_RESTART_MAX_DELAY_SECONDS", 0.01),
            patch("mindroom.orchestrator.logger.exception"),
            patch(
                "mindroom.orchestrator.retry_delay_seconds",
                side_effect=lambda attempt, **_: retry_attempts.append(attempt) or 0,
            ),
        ):
            task = asyncio.create_task(
                _run_auxiliary_task_forever("test task", _operation),
            )
            await asyncio.wait_for(third_start.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls == 3
        assert retry_attempts == [1, 1]

    @pytest.mark.asyncio
    async def test_run_with_retry_can_skip_runtime_state_updates(self) -> None:
        """Background retries must not flip a ready runtime back to startup state."""
        reset_runtime_state()
        set_runtime_ready()
        attempts = 0

        async def _operation() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                msg = "boom"
                raise RuntimeError(msg)

        with (
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_INITIAL_DELAY_SECONDS", 0),
            patch("mindroom.orchestration.runtime.STARTUP_RETRY_MAX_DELAY_SECONDS", 0),
        ):
            await run_with_retry(
                "background retry",
                _operation,
                update_runtime_state=False,
            )

        state = get_runtime_state()
        assert attempts == 2
        assert state.phase == "ready"
        assert state.detail is None
        reset_runtime_state()

    @pytest.mark.asyncio
    async def test_update_config_syncs_runtime_services_when_running(self, tmp_path: Path) -> None:
        """Hot reload should sync runtime services without global knowledge refresh work."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, api_enabled=False)
        config = bind_runtime_paths(Config(), runtime_paths)

        orchestrator.config = config
        orchestrator.running = True
        router_bot = MagicMock()
        router_bot.config = config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot}

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()) as mock_sync_runtime,
        ):
            updated = await orchestrator.config_reload.update_config()

        assert updated is False
        mock_sync_runtime.assert_awaited_once()
        published_config = mock_sync_runtime.await_args.args[0]
        assert published_config is orchestrator.config
        assert published_config.authored_model_dump() == config.authored_model_dump()
        assert mock_sync_runtime.await_args.kwargs == {"start_watcher": True, "previous_config": config}
        assert not hasattr(orchestrator, "_schedule_knowledge_refresh")

    @pytest.mark.asyncio
    async def test_sync_runtime_support_services_rebinds_approval_store_cache(self, tmp_path: Path) -> None:
        """Approval store transport should track replaced runtime cache objects."""
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                models={"default": ModelConfig(provider="test", id="test-model")},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
        old_cache = MagicMock()
        new_cache = MagicMock()
        support = SimpleNamespace(
            event_cache=new_cache,
            event_cache_write_coordinator=MagicMock(),
            startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        )
        store = initialize_approval_store(runtime_paths, event_cache=old_cache)

        try:
            with (
                patch("mindroom.orchestrator.sync_owned_runtime_support", new=AsyncMock(return_value=support)),
                patch.object(orchestrator._knowledge_source_watcher, "sync", new=AsyncMock()),
                patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            ):
                await orchestrator._sync_runtime_support_services(config, start_watcher=False)

            assert get_approval_store() is store
            assert store._event_cache is new_cache
        finally:
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_update_config_keeps_router_owned_approvals_pending_when_requesting_bot_is_removed(
        self,
        tmp_path: Path,
    ) -> None:
        """Hot reload should not expire a pending approval just because the requesting bot was removed."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator._capture_runtime_loop()

        old_config = _approval_reload_config(tmp_path, include_code=True)
        new_config = _approval_reload_config(tmp_path, include_code=False)
        orchestrator.config = old_config
        orchestrator.running = True

        event_order: list[str] = []
        approval_ids: list[str] = []

        async def _router_room_send(
            *,
            room_id: str,
            message_type: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> nio.RoomSendResponse:
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            if "m.new_content" in content:
                event_order.append("edit")
                return nio.RoomSendResponse(event_id="$approval-edit", room_id=room_id)
            event_order.append("send")
            approval_id = content.get("approval_id")
            assert isinstance(approval_id, str)
            approval_ids.append(approval_id)
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="router",
            user_id="@mindroom_router:localhost",
            room_send=AsyncMock(side_effect=_router_room_send),
        )
        code_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="code",
            user_id="@mindroom_code:localhost",
            room_send=AsyncMock(),
        )
        code_bot.cleanup = AsyncMock(side_effect=_cleanup_recorder(event_order))
        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        plan = _approval_removal_plan(new_config)
        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id="$thread",
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            approval_id = await _wait_for_pending_approval_id(store, approval_ids)

            with (
                patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
                patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
                patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
                patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
                patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
                patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
                patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            ):
                updated = await orchestrator.config_reload.update_config()

            assert updated is True
            assert task.done() is False
            assert event_order == ["send", "cleanup"]
            pending = await _live_pending_approval(store, room_id="!room:localhost", approval_id=approval_id)
            assert pending is not None

            await _resolve_pending_approval(
                store,
                pending,
                status="approved",
            )
            decision = await task

            assert decision.status == "approved"
            assert event_order == ["send", "cleanup", "edit"]
            assert router_bot.client is not None
            assert router_bot.client.room_send.await_count == 2
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()
            await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_requesting_bot_room_reconcile_keeps_router_owned_approval_pending(  # noqa: PLR0915
        self,
        tmp_path: Path,
    ) -> None:
        """Leaving the requesting bot's room should not force-expire a router-owned approval."""
        runtime_paths = TestAgentBot._runtime_paths(tmp_path)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
        orchestrator._capture_runtime_loop()

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "code": {
                        "display_name": "CodeAgent",
                        "role": "Writes code",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "code": {
                        "display_name": "CodeAgent",
                        "role": "Writes code",
                        "model": "default",
                        "rooms": [],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        orchestrator.config = old_config

        event_order: list[str] = []
        approval_ids: list[str] = []

        async def _router_room_send(
            *,
            room_id: str,
            message_type: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> nio.RoomSendResponse:
            assert room_id == "!room:localhost"
            assert message_type == "io.mindroom.tool_approval"
            if "m.new_content" in content:
                event_order.append("edit")
                return nio.RoomSendResponse(event_id="$approval-edit", room_id=room_id)
            event_order.append("send")
            approval_id = content.get("approval_id")
            assert isinstance(approval_id, str)
            approval_ids.append(approval_id)
            return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

        router_bot = _mock_approval_reload_bot(
            old_config,
            agent_name="router",
            user_id="@mindroom_router:localhost",
            room_send=AsyncMock(side_effect=_router_room_send),
        )

        code_user = AgentMatrixUser(
            agent_name="code",
            user_id="@mindroom_code:localhost",
            display_name="CodeAgent",
            password=TEST_PASSWORD,
        )
        code_bot = AgentBot(
            code_user,
            tmp_path,
            config=old_config,
            runtime_paths=runtime_paths_for(old_config),
            rooms=["!room:localhost"],
        )
        code_bot.orchestrator = orchestrator
        code_bot.client = make_matrix_client_mock(user_id=code_user.user_id)
        code_bot.client.room_send = AsyncMock()
        code_bot.client.rooms["!room:localhost"].add_member(code_user.user_id, code_user.display_name, None)
        code_bot.latest_thread_event_id_if_needed = AsyncMock(
            return_value="$latest-thread-event",
        )
        code_bot.running = True

        orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

        leave_non_dm_rooms = AsyncMock(side_effect=lambda *_args, **_kwargs: event_order.append("leave"))
        task: asyncio.Task[object] | None = None
        try:
            store = initialize_approval_store(
                runtime_paths,
                sender=orchestrator._approval_transport.send_approval_event,
                editor=orchestrator._approval_transport.edit_approval_event,
            )
            task = asyncio.create_task(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id="$thread",
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    timeout_seconds=60,
                ),
            )

            approval_id = await _wait_for_pending_approval_id(store, approval_ids)

            code_bot.config = new_config
            code_bot.rooms = []

            with (
                patch("mindroom.bot_room_lifecycle.get_joined_rooms", new=AsyncMock(return_value=["!room:localhost"])),
                patch("mindroom.bot_room_lifecycle.leave_non_dm_rooms", new=leave_non_dm_rooms),
            ):
                await code_bot.leave_unconfigured_rooms()

            assert task.done() is False
            pending = await _live_pending_approval(store, room_id="!room:localhost", approval_id=approval_id)
            assert pending is not None
            assert event_order == ["send", "leave"]
            leave_non_dm_rooms.assert_awaited_once()

            await _resolve_pending_approval(
                store,
                pending,
                status="approved",
            )
            decision = await task

            assert decision.status == "approved"
            assert event_order == ["send", "leave", "edit"]
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await _shutdown_approval_store()

    @pytest.mark.asyncio
    async def test_update_config_uses_custom_config_path(self, tmp_path: Path) -> None:
        """Hot reload should keep reading the orchestrator's custom config path."""
        config_path = tmp_path / "custom-config.yaml"
        runtime_paths = resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path,
            process_env={},
        )
        current_config = bind_runtime_paths(Config(), runtime_paths)
        new_config = bind_runtime_paths(Config(timezone="America/New_York"), runtime_paths)
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=runtime_paths,
            api_enabled=False,
        )
        orchestrator.config = current_config
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers=set(),
            configured_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config) as mock_load_config,
            patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
            patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        ):
            updated = await orchestrator.config_reload.update_config()

        assert updated is False
        mock_load_config.assert_called_once()
        assert mock_load_config.call_args.args[0].config_path == config_path.resolve()

    @pytest.mark.asyncio
    async def test_update_config_does_not_swap_hook_runtime_on_failed_reload(self, tmp_path: Path) -> None:
        """Failed reloads must leave the active hook snapshot and scheduling registry untouched."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        current_config = MagicMock()
        current_config.authorization.global_users = []
        current_config.cache = MagicMock()
        new_config = MagicMock()
        new_config.authorization.global_users = []
        new_config.cache = MagicMock()
        old_hook_registry = HookRegistry.empty()
        new_hook_registry = HookRegistry.empty()

        orchestrator.config = current_config
        orchestrator.hook_registry = old_hook_registry
        plan = SimpleNamespace(
            mindroom_user_changed=True,
            new_config=new_config,
            changed_mcp_servers=set(),
            entities_to_restart=set(),
            new_entities=set(),
            added_entities=set(),
            configured_entities=set(),
            removed_entities=set(),
            only_support_service_changes=True,
        )

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch("mindroom.orchestrator.HookRegistry.from_plugins", return_value=new_hook_registry),
            patch("mindroom.orchestrator.set_scheduling_hook_registry") as mock_set_scheduling_hook_registry,
            patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
            patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
            patch.object(
                orchestrator,
                "_prepare_user_account",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await orchestrator.config_reload.update_config()

        assert orchestrator.config is current_config
        assert orchestrator.hook_registry is old_hook_registry
        mock_set_scheduling_hook_registry.assert_not_called()
        mock_clear_snapshot_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_config_does_not_stop_mcp_entities_before_plugin_reload_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        """Plugin reload validation must happen before any MCP-driven entity shutdown."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        current_config = Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/current"],
        )
        new_config = Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/updated"],
        )

        orchestrator.config = current_config
        bot = _mock_managed_bot(current_config)
        bot.running = True
        orchestrator.agent_bots = {"general": bot}
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers={"demo-server"},
            configured_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        stop_entities_before_mcp_sync = AsyncMock(return_value={"general"})

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
            patch.object(
                orchestrator,
                "_stop_entities_before_mcp_sync",
                new=stop_entities_before_mcp_sync,
            ),
            patch(
                "mindroom.orchestrator.prepare_plugin_reload",
                side_effect=RuntimeError("broken plugin"),
            ),
            patch("mindroom.orchestrator.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
            pytest.raises(RuntimeError, match="broken plugin"),
        ):
            await orchestrator.config_reload.update_config()

        stop_entities_before_mcp_sync.assert_not_awaited()
        assert bot.running is True
        assert orchestrator.config is current_config
        mock_clear_snapshot_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_config_does_not_leak_plugin_state_before_config_commit(
        self,
        tmp_path: Path,
    ) -> None:
        """Plugin validation during config reload must not mutate live plugin state on later failure."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        plugin_root = tmp_path / "plugins" / "updated"
        skill_dir = plugin_root / "skills" / "updated-skill"
        skill_dir.mkdir(parents=True)
        (plugin_root / "mindroom.plugin.json").write_text(
            '{"name":"updated","tools_module":"tools.py","hooks_module":"hooks.py","skills":["skills"]}',
            encoding="utf-8",
        )
        (plugin_root / "tools.py").write_text(
            "from agno.tools import Toolkit\n"
            "from mindroom.tool_system.declarations import ToolCategory\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
            "\n"
            "class UpdatedTool(Toolkit):\n"
            "    def __init__(self) -> None:\n"
            "        super().__init__(name='updated', tools=[])\n"
            "\n"
            "@register_tool_with_metadata(\n"
            "    name='updated_plugin_tool',\n"
            "    display_name='Updated Plugin Tool',\n"
            "    description='updated plugin tool',\n"
            "    category=ToolCategory.DEVELOPMENT,\n"
            ")\n"
            "def updated_plugin_tools():\n"
            "    return UpdatedTool\n",
            encoding="utf-8",
        )
        (plugin_root / "hooks.py").write_text(
            "from mindroom.hooks import hook\n"
            "\n"
            "@hook('message:received')\n"
            "async def audit(ctx):\n"
            "    del ctx\n"
            "    return None\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: updated-skill\ndescription: demo\n---\n",
            encoding="utf-8",
        )

        current_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=[],
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=["./plugins/updated"],
            ),
            tmp_path,
        )

        orchestrator.config = current_config
        old_hook_registry = HookRegistry.empty()
        orchestrator.hook_registry = old_hook_registry
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers={"demo-server"},
            configured_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        original_plugin_skill_roots = _get_plugin_skill_roots()
        set_plugin_skill_roots([])
        try:
            with (
                patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
                patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_stop_entities_before_mcp_sync",
                    new=AsyncMock(side_effect=RuntimeError("stop failed")),
                ),
                pytest.raises(RuntimeError, match="stop failed"),
            ):
                await orchestrator.config_reload.update_config()

            assert orchestrator.config is current_config
            assert orchestrator.hook_registry is old_hook_registry
            assert "updated_plugin_tool" not in TOOL_METADATA
            assert _get_plugin_skill_roots() == []
        finally:
            set_plugin_skill_roots(original_plugin_skill_roots)

    @pytest.mark.asyncio
    async def test_update_config_preserves_watcher_dirty_state_for_stale_prepared_plugin_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        """A plugin edit during staged config reload must still be seen by the watcher."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        plugin_root = tmp_path / "plugins" / "updated"
        plugin_root.mkdir(parents=True)
        hooks_path = plugin_root / "hooks.py"
        (plugin_root / "mindroom.plugin.json").write_text(
            '{"name":"updated","hooks_module":"hooks.py","skills":[]}',
            encoding="utf-8",
        )
        hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

        current_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=[],
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
                models={"default": {"provider": "test", "id": "test-model"}},
                plugins=["./plugins/updated"],
            ),
            tmp_path,
        )

        orchestrator.config = current_config
        orchestrator.hook_registry = HookRegistry.empty()
        plan = ConfigUpdatePlan(
            new_config=new_config,
            changed_mcp_servers=set(),
            configured_entities=set(),
            entities_to_restart=set(),
            new_entities=set(),
            removed_entities=set(),
            mindroom_user_changed=False,
            matrix_room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
        )

        original_plugin_skill_roots = _get_plugin_skill_roots()
        original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
        original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
        original_modules = set(sys.modules)
        set_plugin_skill_roots([])
        try:

            async def mutate_plugin_after_prepare(*_args: object, **_kwargs: object) -> set[str]:
                hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
                return set()

            with (
                patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
                patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
                patch.object(
                    orchestrator,
                    "_stop_entities_before_mcp_sync",
                    new=AsyncMock(side_effect=mutate_plugin_after_prepare),
                ),
                patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
                patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
                patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
                patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
            ):
                updated = await orchestrator.config_reload.update_config()

            assert updated is False
            loaded_hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module
            assert loaded_hooks_module.VALUE == 1

            changed_paths = await _collect_plugin_root_changes(
                tuple(orchestrator.plugin_watch.last_snapshot_by_root),
                orchestrator.plugin_watch.last_snapshot_by_root,
            )
            assert changed_paths == {hooks_path.resolve()}
        finally:
            plugin_module._PLUGIN_CACHE.clear()
            plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
            plugin_module._MODULE_IMPORT_CACHE.clear()
            plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
            set_plugin_skill_roots(original_plugin_skill_roots)
            for module_name in set(sys.modules) - original_modules:
                if module_name.startswith("mindroom_plugin_"):
                    sys.modules.pop(module_name, None)

    @pytest.mark.asyncio
    async def test_update_config_initializes_shared_event_cache_for_unchanged_bots(self, tmp_path: Path) -> None:
        """Cache service should initialize and bind when a test runtime skipped startup wiring."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True
        router_bot = _mock_managed_bot(old_config)
        general_bot = _mock_managed_bot(old_config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.config_reload.update_config()
                assert updated is False
                assert router_bot.event_cache is orchestrator._runtime_support.event_cache
                assert general_bot.event_cache is orchestrator._runtime_support.event_cache
                assert (
                    router_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
                assert (
                    general_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
            finally:
                await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_update_config_keeps_shared_event_cache_when_db_path_changes(self, tmp_path: Path) -> None:
        """Hot reload should keep the active cache service and defer db_path changes to restart."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
                cache={"db_path": "event-cache-old.db"},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
                cache={"db_path": "event-cache-new.db"},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True
        router_bot = _mock_managed_bot(old_config)
        general_bot = _mock_managed_bot(old_config)
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
        await orchestrator._sync_event_cache_service(old_config)
        old_cache = orchestrator._runtime_support.event_cache
        assert old_cache is not None

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins", return_value=[]),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.config_reload.update_config()
                assert updated is False
                assert orchestrator._runtime_support.event_cache is old_cache
                assert old_cache.db_path == old_config.cache.resolve_db_path(orchestrator.runtime_paths)
                assert router_bot.event_cache is old_cache
                assert general_bot.event_cache is old_cache
                assert orchestrator._runtime_support.event_cache_write_coordinator is not None
                assert (
                    router_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
                assert (
                    general_bot.event_cache_write_coordinator
                    is orchestrator._runtime_support.event_cache_write_coordinator
                )
            finally:
                await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    async def test_update_config_keeps_failed_new_bot_and_schedules_retry(self, tmp_path: Path) -> None:
        """Hot reload should retain failed bots and retry them instead of dropping them."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                    "coach": {
                        "display_name": "Coach",
                        "role": "Coaching assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True

        router_bot = MagicMock()
        router_bot.config = old_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        general_bot = MagicMock()
        general_bot.config = old_config
        general_bot.enable_streaming = True
        general_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        new_bot = MagicMock()
        new_bot.agent_name = "coach"
        new_bot.running = False
        new_bot.try_start = AsyncMock(return_value=False)
        new_bot.ensure_rooms = AsyncMock(side_effect=AssertionError("ensure_rooms called on failed bot"))

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch("mindroom.orchestrator.create_bot_for_entity", return_value=new_bot),
            patch.object(
                orchestrator,
                "_prepare_entity_accounts",
                new=AsyncMock(
                    return_value={
                        "coach": AgentMatrixUser(
                            agent_name="coach",
                            user_id="@mindroom_coach:localhost",
                            display_name="CoachAgent",
                            password=TEST_PASSWORD,
                        ),
                    },
                ),
            ),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.config_reload.update_config()
            finally:
                await orchestrator._close_runtime_support_services()

        assert updated is True
        assert orchestrator.agent_bots["coach"] is new_bot
        new_bot.ensure_rooms.assert_not_awaited()
        mock_schedule_retry.assert_awaited_once_with("coach")

    @pytest.mark.asyncio
    async def test_update_config_keeps_permanently_failed_new_bot_without_retry(self, tmp_path: Path) -> None:
        """Hot reload should retain permanently failed bots without scheduling retries."""
        orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))

        old_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )
        new_config = _runtime_bound_config(
            Config(
                agents={
                    "general": {
                        "display_name": "GeneralAgent",
                        "role": "General assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                    "coach": {
                        "display_name": "Coach",
                        "role": "Coaching assistant",
                        "model": "default",
                        "rooms": ["lobby"],
                    },
                },
                models={"default": {"provider": "test", "id": "test-model"}},
            ),
            tmp_path,
        )

        orchestrator.config = old_config
        orchestrator.running = True

        router_bot = MagicMock()
        router_bot.config = old_config
        router_bot.enable_streaming = True
        router_bot._set_presence_with_model_info = AsyncMock()
        general_bot = MagicMock()
        general_bot.config = old_config
        general_bot.enable_streaming = True
        general_bot._set_presence_with_model_info = AsyncMock()
        orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

        new_bot = MagicMock()
        new_bot.agent_name = "coach"
        new_bot.running = False
        new_bot.try_start = AsyncMock(side_effect=PermanentMatrixStartupError("boom"))
        new_bot.ensure_rooms = AsyncMock(side_effect=AssertionError("ensure_rooms called on failed bot"))

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestrator.load_plugins"),
            patch(
                "mindroom.orchestration.config_updates._identify_entities_to_restart",
                return_value=set(),
            ),
            patch("mindroom.orchestrator.create_bot_for_entity", return_value=new_bot),
            patch.object(
                orchestrator,
                "_prepare_entity_accounts",
                new=AsyncMock(
                    return_value={
                        "coach": AgentMatrixUser(
                            agent_name="coach",
                            user_id="@mindroom_coach:localhost",
                            display_name="CoachAgent",
                            password=TEST_PASSWORD,
                        ),
                    },
                ),
            ),
            patch.object(orchestrator, "_schedule_bot_start_retry", new=AsyncMock()) as mock_schedule_retry,
            patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock()),
            patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        ):
            try:
                updated = await orchestrator.config_reload.update_config()
            finally:
                await orchestrator._close_runtime_support_services()

        assert updated is True
        assert orchestrator.agent_bots["coach"] is new_bot
        new_bot.ensure_rooms.assert_not_awaited()
        mock_schedule_retry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator stop
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.orchestrator.load_config")
    async def test_orchestrator_stop(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test stopping all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        bind_mock_config_cache(mock_config, tmp_path)
        mock_load_config.return_value = mock_config

        with patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            try:
                await orchestrator.initialize()

                # Mock the agent clients and ensure_user_account
                for bot in orchestrator.agent_bots.values():
                    bot.client = AsyncMock()
                    bot.running = True
                    bot.ensure_user_account = AsyncMock()

                await orchestrator.stop()

                assert not orchestrator.running
                for bot in orchestrator.agent_bots.values():
                    assert not bot.running
                    if bot.client is not None:
                        bot.client.close.assert_called_once()
            finally:
                await orchestrator._close_runtime_support_services()

    @pytest.mark.asyncio
    @pytest.mark.requires_matrix  # Requires real Matrix server for orchestrator streaming
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    @patch("mindroom.orchestrator.load_config")
    async def test_orchestrator_streaming_default_config(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that orchestrator respects defaults.enable_streaming."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.defaults.enable_streaming = False
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        bind_mock_config_cache(mock_config, tmp_path)
        mock_load_config.return_value = mock_config

        with patch("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=TestAgentBot._runtime_paths(tmp_path))
            try:
                await orchestrator.initialize()

                # All bots should have streaming disabled except teams (which never stream)
                for bot in orchestrator.agent_bots.values():
                    if hasattr(bot, "enable_streaming"):
                        assert bot.enable_streaming is False
            finally:
                await orchestrator._close_runtime_support_services()
