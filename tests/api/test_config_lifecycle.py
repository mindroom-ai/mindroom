"""Direct unit tests for the API config state machine in mindroom.api.config_lifecycle.

These tests pin the committed-snapshot contract: load/validation failure handling,
generation tracking and stale-write rejection, request-pinned snapshots, the
file-watcher reload effects, and the concurrent-writer commit protocol.
"""

import asyncio
import copy
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.config.main import Config

VALID_CONFIG: dict[str, Any] = {
    "models": {"default": {"provider": "ollama", "id": "test-model"}},
    "agents": {
        "test_agent": {
            "display_name": "Test Agent",
            "role": "A test agent",
            "tools": ["calculator"],
            "instructions": ["Test instruction"],
            "rooms": ["test_room"],
        },
    },
    "defaults": {"markdown": True},
}


def _write_config(config_path: Path, data: dict[str, Any]) -> None:
    config_path.write_text(yaml.dump(data), encoding="utf-8")


def _make_api_app(runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Build one API app with a fresh published snapshot, mirroring main.initialize_api_app."""
    api_app = FastAPI()
    state = config_lifecycle.ensure_app_state(api_app)
    state.api_state = config_lifecycle.ApiState(
        config_lock=threading.Lock(),
        snapshot=config_lifecycle.ApiSnapshot(
            generation=0,
            runtime_paths=runtime_paths,
            config_data={},
        ),
    )
    config_lifecycle.register_api_app(api_app)
    return api_app


def _request_for(api_app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/config",
            "query_string": b"",
            "headers": [],
            "app": api_app,
        },
    )


def _snapshot(api_app: FastAPI) -> config_lifecycle.ApiSnapshot:
    return config_lifecycle.require_api_state(api_app).snapshot


@pytest.fixture
def runtime_paths(tmp_path: Path) -> constants.RuntimePaths:
    """Resolve one isolated runtime context backed by a real temp config file."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, VALID_CONFIG)
    return constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


@pytest.fixture
def loaded_app(runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Return one API app with the temp config already loaded and committed."""
    api_app = _make_api_app(runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True
    return api_app


class TestLoadAndValidationFailure:
    """Loading and validation-failure behavior of the committed config cache."""

    def test_initial_load_publishes_committed_snapshot(self, runtime_paths: constants.RuntimePaths) -> None:
        """A successful first load publishes data, runtime config, and generation 1."""
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True
        snapshot = _snapshot(api_app)
        assert snapshot.generation == 1
        assert snapshot.config_data["agents"]["test_agent"]["display_name"] == "Test Agent"
        assert snapshot.runtime_config is not None
        assert snapshot.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        assert snapshot.source_fingerprint is not None

    def test_reload_of_unchanged_source_does_not_bump_generation(self, loaded_app: FastAPI) -> None:
        """Reloading byte-identical source keeps the generation stable."""
        generation = _snapshot(loaded_app).generation
        assert config_lifecycle.load_config_into_app(_snapshot(loaded_app).runtime_paths, loaded_app) is True
        assert _snapshot(loaded_app).generation == generation

    def test_read_before_any_load_raises_500(self, runtime_paths: constants.RuntimePaths) -> None:
        """Reads against a never-loaded app surface the shared missing-config error."""
        api_app = _make_api_app(runtime_paths)
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.read_committed_config(_request_for(api_app), lambda config: config)
        assert exc_info.value.status_code == 500

    def test_validation_failure_keeps_last_good_committed_config(self, loaded_app: FastAPI) -> None:
        """A Pydantic validation failure marks the load failed without clobbering last-good state."""
        snapshot = _snapshot(loaded_app)
        runtime_paths = snapshot.runtime_paths
        runtime_paths.config_path.write_text("agents: not-a-mapping\n", encoding="utf-8")

        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is False

        failed = _snapshot(loaded_app)
        # The failed load still bumps the generation (the on-disk source changed).
        assert failed.generation == snapshot.generation + 1
        assert failed.config_load_result is not None
        assert failed.config_load_result.success is False
        assert failed.config_load_result.error_status_code == 422
        # Last good committed payload and runtime config are preserved, not clobbered.
        assert failed.config_data == snapshot.config_data
        assert failed.runtime_config is snapshot.runtime_config

        # But reads surface the load failure instead of silently serving stale data.
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.read_committed_config(_request_for(loaded_app), lambda config: config)
        assert exc_info.value.status_code == 422

    def test_malformed_yaml_then_good_edit_recovers(self, loaded_app: FastAPI) -> None:
        """A malformed external edit fails the load; a later good edit fully recovers."""
        runtime_paths = _snapshot(loaded_app).runtime_paths
        runtime_paths.config_path.write_text("agents: [unclosed\n", encoding="utf-8")
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is False
        assert _snapshot(loaded_app).config_data["agents"]["test_agent"]["role"] == "A test agent"

        _write_config(runtime_paths.config_path, VALID_CONFIG)
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is True
        recovered = _snapshot(loaded_app)
        assert recovered.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        result = config_lifecycle.read_committed_config(_request_for(loaded_app), lambda config: dict(config))
        assert result["agents"]["test_agent"]["display_name"] == "Test Agent"


class TestGenerationTrackingAndWrites:
    """Generation tracking and stale-write detection for API config writes."""

    def test_stale_expected_generation_is_rejected(self, loaded_app: FastAPI) -> None:
        """A write carrying a stale client generation is rejected with 409."""
        current_generation = _snapshot(loaded_app).generation
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_committed_config(
                _request_for(loaded_app),
                copy.deepcopy(VALID_CONFIG),
                error_prefix="test replace",
                expected_generation=current_generation - 1,
            )
        assert exc_info.value.status_code == 409

    def test_current_generation_write_commits_and_bumps(self, loaded_app: FastAPI) -> None:
        """A current-generation write commits to memory and disk and bumps the generation."""
        before = _snapshot(loaded_app)
        new_config = copy.deepcopy(VALID_CONFIG)
        new_config["agents"]["test_agent"]["role"] = "An updated test agent"

        new_generation = config_lifecycle.replace_committed_config(
            _request_for(loaded_app),
            new_config,
            error_prefix="test replace",
            expected_generation=before.generation,
        )

        after = _snapshot(loaded_app)
        assert new_generation == before.generation + 1
        assert after.generation == new_generation
        assert after.config_data["agents"]["test_agent"]["role"] == "An updated test agent"
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["test_agent"]["role"] == "An updated test agent"

    def test_committed_write_reload_echo_does_not_bump_generation(self, loaded_app: FastAPI) -> None:
        """The watcher reload triggered by the API's own write is fingerprint-suppressed."""
        config_lifecycle.write_committed_config(
            _request_for(loaded_app),
            lambda config: config["agents"]["test_agent"]["instructions"].append("Extra instruction"),
            error_prefix="test write",
        )
        committed = _snapshot(loaded_app)
        # The file watcher reloads after every write; the matching source fingerprint
        # must suppress a second generation bump for the API's own write.
        assert config_lifecycle.load_config_into_app(committed.runtime_paths, loaded_app) is True
        assert _snapshot(loaded_app).generation == committed.generation

    def test_invalid_mutation_is_rejected_without_commit(self, loaded_app: FastAPI) -> None:
        """A mutation producing an invalid config raises 422 and leaves the snapshot untouched."""
        before = _snapshot(loaded_app)
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                _request_for(loaded_app),
                lambda config: config.__setitem__("agents", "not-a-mapping"),
                error_prefix="test write",
            )
        assert exc_info.value.status_code == 422
        assert _snapshot(loaded_app) is before

    def test_raw_replacement_preserves_source_and_rejects_stale_writes(self, loaded_app: FastAPI) -> None:
        """Raw source replacement keeps the source byte-exact and honors generation checks."""
        before = _snapshot(loaded_app)
        raw_source = "# keep this comment\n" + yaml.dump(VALID_CONFIG)

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_raw_config_source(
                _request_for(loaded_app),
                raw_source,
                error_prefix="test raw replace",
                expected_generation=before.generation - 1,
            )
        assert exc_info.value.status_code == 409

        new_generation = config_lifecycle.replace_raw_config_source(
            _request_for(loaded_app),
            raw_source,
            error_prefix="test raw replace",
            expected_generation=before.generation,
        )
        assert new_generation == before.generation + 1
        assert before.runtime_paths.config_path.read_text(encoding="utf-8") == raw_source

    def test_full_replacement_recovers_from_broken_on_disk_config(self, loaded_app: FastAPI) -> None:
        """Full replacement skips the failed-load gate so the editor can repair a broken config."""
        runtime_paths = _snapshot(loaded_app).runtime_paths
        runtime_paths.config_path.write_text("agents: [unclosed\n", encoding="utf-8")
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is False

        # Unlike mutations, full replacement skips the failed-load gate so the
        # raw editor can repair a broken config through the API.
        config_lifecycle.replace_committed_config(
            _request_for(loaded_app),
            copy.deepcopy(VALID_CONFIG),
            error_prefix="test replace",
        )
        recovered = _snapshot(loaded_app)
        assert recovered.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        assert yaml.safe_load(runtime_paths.config_path.read_text(encoding="utf-8")) == recovered.config_data


class TestRequestSnapshotLifecycle:
    """Request-pinned snapshot consistency across concurrent commits."""

    def test_pinned_snapshot_stays_consistent_across_commits(self, loaded_app: FastAPI) -> None:
        """A snapshot pinned at request start keeps serving its original coherent state."""
        request = _request_for(loaded_app)
        pinned = config_lifecycle.bind_current_request_snapshot(request)

        config_lifecycle.write_committed_config(
            _request_for(loaded_app),
            lambda config: config["agents"]["test_agent"].__setitem__("role", "Changed mid-request"),
            error_prefix="test write",
        )
        assert _snapshot(loaded_app).generation == pinned.generation + 1

        # The pinned request keeps reading its original coherent snapshot.
        assert config_lifecycle.committed_generation(request) == pinned.generation
        role = config_lifecycle.read_committed_config(request, lambda config: config["agents"]["test_agent"]["role"])
        assert role == "A test agent"
        runtime_config, _ = config_lifecycle.read_committed_runtime_config(request)
        assert runtime_config is pinned.runtime_config
        # Re-binding returns the already pinned snapshot, never a newer one.
        assert config_lifecycle.bind_current_request_snapshot(request) is pinned

    def test_write_from_stale_pinned_snapshot_conflicts(self, loaded_app: FastAPI) -> None:
        """A write built from a request snapshot that lost the race fails with 409."""
        stale_request = _request_for(loaded_app)
        config_lifecycle.bind_current_request_snapshot(stale_request)
        config_lifecycle.write_committed_config(
            _request_for(loaded_app),
            lambda config: config["agents"]["test_agent"]["instructions"].append("Won the race"),
            error_prefix="test write",
        )
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                stale_request,
                lambda config: config["agents"]["test_agent"]["instructions"].append("Lost the race"),
                error_prefix="test write",
            )
        assert exc_info.value.status_code == 409
        instructions = _snapshot(loaded_app).config_data["agents"]["test_agent"]["instructions"]
        assert instructions == ["Test instruction", "Won the race"]


class TestFileWatcherReload:
    """File-watcher reload effects on committed state."""

    def test_external_edit_updates_committed_state_and_generation(self, loaded_app: FastAPI) -> None:
        """A valid external file edit advances committed data and the generation."""
        before = _snapshot(loaded_app)
        external = copy.deepcopy(VALID_CONFIG)
        external["agents"]["test_agent"]["role"] = "Edited outside the API"
        _write_config(before.runtime_paths.config_path, external)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is True
        after = _snapshot(loaded_app)
        assert after.generation == before.generation + 1
        assert after.config_data["agents"]["test_agent"]["role"] == "Edited outside the API"

    def test_stale_load_is_discarded_after_concurrent_commit(
        self,
        loaded_app: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reload that loses the race to a concurrent commit is discarded entirely."""
        state = config_lifecycle.require_api_state(loaded_app)
        runtime_paths = state.snapshot.runtime_paths
        real_load = config_lifecycle._load_config_result

        def load_then_lose_race(
            paths: constants.RuntimePaths,
        ) -> tuple[config_lifecycle.ConfigLoadResult, dict[str, Any] | None, Config | None, str | None]:
            result = real_load(paths)
            with state.config_lock:
                state.snapshot = config_lifecycle._published_snapshot(state.snapshot)
            return result

        monkeypatch.setattr(config_lifecycle, "_load_config_result", load_then_lose_race)
        racing_generation = state.snapshot.generation + 1
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is False
        # The concurrent commit's snapshot is left untouched by the stale load.
        assert _snapshot(loaded_app).generation == racing_generation

    @pytest.mark.asyncio
    async def test_watch_config_runs_reload_callback_on_change(self, runtime_paths: constants.RuntimePaths) -> None:
        """The watcher seam invokes the reload callback when the file changes."""
        reloads: list[bool] = []

        async def fake_watch_file(
            path: Path | str,
            callback: Callable[[], Awaitable[None]],
            stop_event: asyncio.Event | None,
        ) -> None:
            assert Path(path) == runtime_paths.config_path
            assert stop_event is not None
            await callback()

        await config_lifecycle._watch_config(
            asyncio.Event(),
            runtime_paths,
            lambda: reloads.append(True) or True,
            watch_file_impl=fake_watch_file,
        )
        assert reloads == [True]


class TestExternalWriterPublishing:
    """External config writers publishing into registered API apps."""

    def test_validate_and_persist_publishes_to_registered_apps(self, loaded_app: FastAPI) -> None:
        """validate_and_persist_config_payload advances every matching registered app snapshot."""
        before = _snapshot(loaded_app)
        payload = copy.deepcopy(VALID_CONFIG)
        payload["agents"]["test_agent"]["role"] = "Updated by an external writer"

        config_lifecycle.validate_and_persist_config_payload(payload, before.runtime_paths)

        after = _snapshot(loaded_app)
        assert after.generation == before.generation + 1
        assert after.config_data["agents"]["test_agent"]["role"] == "Updated by an external writer"
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == after.config_data


class TestConcurrencySmoke:
    """Interleaved writers racing on the same committed snapshot."""

    def test_interleaved_writers_one_winner_per_generation(self, loaded_app: FastAPI) -> None:
        """Exactly one writer wins each generation and the final state is never torn."""
        before = _snapshot(loaded_app)
        writer_count = 4
        # The constructor timeout applies to every wait(), so a writer that dies
        # before reaching the barrier breaks it for the others immediately instead
        # of letting them idle until pytest's global 60s timeout.
        barrier = threading.Barrier(writer_count, timeout=10)
        outcomes: dict[str, str | int] = {}

        def write_marker(marker: str) -> None:
            request = _request_for(loaded_app)
            barrier.wait()
            try:
                config_lifecycle.write_committed_config(
                    request,
                    lambda config: config["agents"]["test_agent"]["instructions"].append(marker),
                    error_prefix="test write",
                )
                outcomes[marker] = "ok"
            except HTTPException as exc:
                outcomes[marker] = exc.status_code

        threads = [threading.Thread(target=write_marker, args=(f"marker-{i}",)) for i in range(writer_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert set(outcomes.values()) <= {"ok", 409}
        winners = {marker for marker, outcome in outcomes.items() if outcome == "ok"}
        assert winners

        after = _snapshot(loaded_app)
        # Exactly one writer wins each generation; losers see the stale-write conflict.
        assert after.generation == before.generation + len(winners)
        instructions = after.config_data["agents"]["test_agent"]["instructions"]
        assert instructions[0] == "Test instruction"
        assert set(instructions[1:]) == winners
        # No torn state: the on-disk file matches the committed snapshot exactly.
        on_disk = yaml.safe_load(after.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == after.config_data
        assert after.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
