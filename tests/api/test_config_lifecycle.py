"""Direct unit tests for the API config state machine in mindroom.api.config_lifecycle.

These tests pin the committed-snapshot contract: load/validation failure handling,
generation tracking and stale-write rejection, request-pinned snapshots, the
file-watcher reload effects, and the concurrent-writer commit protocol.
"""

import copy
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.config.main import Config, ConfigRuntimeValidationError

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
        ) -> tuple[
            config_lifecycle.ConfigLoadResult,
            dict[str, Any] | None,
            Config | None,
            str | None,
            frozenset[Path] | None,
        ]:
            result = real_load(paths)
            with state.config_lock:
                state.snapshot = config_lifecycle._published_snapshot(state.snapshot)
            return result

        monkeypatch.setattr(config_lifecycle, "_load_config_result", load_then_lose_race)
        racing_generation = state.snapshot.generation + 1
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app) is False
        # The concurrent commit's snapshot is left untouched by the stale load.
        assert _snapshot(loaded_app).generation == racing_generation


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


class TestEventJournalChangeRefusal:
    """A journal move must be refused by every config authority in the process, not just one."""

    JOURNAL_MOVE: ClassVar[dict[str, str]] = {
        "backend": "postgres",
        "database_url": "postgresql://localhost/journal",
    }

    @staticmethod
    async def _never_ready(_delivery_snapshot: object) -> bool:
        return False

    def _bind_trigger_runtime(self, api_app: FastAPI) -> config_lifecycle.ExternalTriggerRuntime:
        """Bind trigger delivery exactly as the orchestrator does, pinning today's generation."""
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.bind_external_trigger_runtime(
            api_app,
            client=object(),
            conversation_reader=object(),
            is_trigger_snapshot_ready=self._never_ready,
        )
        runtime = config_lifecycle.app_state(api_app).external_trigger_runtime
        assert runtime is not None
        return runtime

    def test_refused_journal_change_leaves_trigger_runtime_on_the_live_generation(
        self,
        loaded_app: FastAPI,
    ) -> None:
        """The API publisher refuses the same journal move the orchestrator refuses.

        The orchestrator keeps the store it opened and returns without
        resyncing or rebinding the API. If this loader published the change
        anyway, the generation would advance past the runtime binding and every
        external trigger would 503 until the next successful reload or a
        restart.
        """
        before = _snapshot(loaded_app)
        bound = self._bind_trigger_runtime(loaded_app)
        assert bound.config_generation == before.generation

        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = dict(self.JOURNAL_MOVE)
        _write_config(before.runtime_paths.config_path, moved)

        published = config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app)

        after = _snapshot(loaded_app)
        assert after.generation == bound.config_generation, (
            "the API published a generation for a journal change the runtime refused, "
            "stranding external trigger delivery on the old binding"
        )
        assert published is False
        assert after.runtime_config is before.runtime_config
        assert after.runtime_config is not None
        assert after.runtime_config.event_journal.backend == "sqlite"

    def test_committed_write_that_moves_the_journal_is_rejected(self, loaded_app: FastAPI) -> None:
        """A dashboard save carrying a journal move is refused instead of silently dropped."""
        before = _snapshot(loaded_app)
        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = dict(self.JOURNAL_MOVE)

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_committed_config(
                _request_for(loaded_app),
                moved,
                error_prefix="test replace",
            )

        assert exc_info.value.status_code == 409
        assert _snapshot(loaded_app) is before
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert "event_journal" not in on_disk, "the refused journal move was written to the config file"

    def test_raw_source_write_that_moves_the_journal_is_rejected(self, loaded_app: FastAPI) -> None:
        """The raw YAML editor is the widest door onto event_journal, so it carries the rule too."""
        before = _snapshot(loaded_app)
        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = dict(self.JOURNAL_MOVE)

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_raw_config_source(
                _request_for(loaded_app),
                yaml.dump(moved),
                error_prefix="test raw replace",
            )

        assert exc_info.value.status_code == 409
        assert _snapshot(loaded_app) is before
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert "event_journal" not in on_disk, "the refused journal move was written to the config file"

    def test_external_persist_that_moves_the_journal_is_refused(self, loaded_app: FastAPI) -> None:
        """The writer behind the chat command and the config tools carries the rule too.

        This path writes the file and republishes every registered snapshot at
        generation+1 without going through the HTTP commit paths. Left
        unguarded, a `!config set event_journal.database_url ...` reports
        success and moves published state onto a journal the orchestrator goes
        on refusing -- and, because published state then names the new journal,
        the disk loader's comparison agrees with it and never refuses again.
        """
        before = _snapshot(loaded_app)
        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = dict(self.JOURNAL_MOVE)

        with pytest.raises(ConfigRuntimeValidationError):
            config_lifecycle.validate_and_persist_config_payload(moved, before.runtime_paths)

        after = _snapshot(loaded_app)
        assert after is before, "a refused write advanced the published snapshot"
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert "event_journal" not in on_disk, "the refused journal move was written to the config file"

    def test_refused_disk_load_stops_a_later_write_from_flattening_the_file(self, loaded_app: FastAPI) -> None:
        """A refused load leaves a payload that no longer describes disk, and must say so.

        Reporting the load as successful would let the next committed write
        deep-copy the pre-edit payload and save it back, silently undoing every
        unrelated edit the operator made in the same save -- and would leave a
        runtime that cannot adopt its own config file indistinguishable from a
        healthy one.
        """
        before = _snapshot(loaded_app)
        hand_edited = copy.deepcopy(VALID_CONFIG)
        hand_edited["event_journal"] = dict(self.JOURNAL_MOVE)
        hand_edited["agents"]["hand_written"] = {
            "display_name": "Hand Written",
            "role": "Added by hand in the same save",
        }
        _write_config(before.runtime_paths.config_path, hand_edited)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is False
        refused = _snapshot(loaded_app)
        assert refused.config_load_result is not None
        assert refused.config_load_result.success is False
        assert refused.config_load_result.error_status_code == 409
        assert "event_journal" in str(refused.config_load_result.error_detail)
        assert refused.generation == before.generation, "a refused load advanced the generation"

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                _request_for(loaded_app),
                lambda config: config["defaults"].__setitem__("markdown", False),
                error_prefix="test write",
            )

        assert exc_info.value.status_code == 409
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == hand_edited, "an unrelated write flattened the operator's on-disk edits"

    def test_a_write_pinned_before_the_refusal_cannot_overwrite_it(self, loaded_app: FastAPI) -> None:
        """A refusal holds the generation still on purpose, so it needs a separate commit identity.

        Holding the generation is what lets reverting the journal field recover
        with nothing to rebind. The cost, if the generation is also the only
        thing writers compare against, is that a request pinned before the
        refusal still passes the commit check and saves its stale copy of the
        last good payload -- flattening both the operator's unrelated edit and
        the journal edit, and publishing success over the refusal.
        """
        before = _snapshot(loaded_app)
        request = _request_for(loaded_app)
        pinned = config_lifecycle.bind_current_request_snapshot(request)

        hand_edited = copy.deepcopy(VALID_CONFIG)
        hand_edited["agents"]["hand_written"] = {
            "display_name": "Hand Written",
            "role": "Added by hand in the same save",
        }
        hand_edited["event_journal"] = dict(self.JOURNAL_MOVE)
        _write_config(before.runtime_paths.config_path, hand_edited)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is False
        refused = _snapshot(loaded_app)
        assert refused.generation == pinned.generation, "this test only bites while the refusal holds the generation"

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                request,
                lambda config: config["defaults"].__setitem__("markdown", False),
                error_prefix="test write",
            )

        assert exc_info.value.status_code == 409
        assert _snapshot(loaded_app).config_load_result == refused.config_load_result, (
            "a stale pinned write published success over the refusal"
        )
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == hand_edited, "a request pinned before the refusal flattened the operator's on-disk edits"

    def test_reverting_the_journal_field_recovers_without_a_generation_bump(self, loaded_app: FastAPI) -> None:
        """Putting the field back is the documented way out, so it has to actually work."""
        before = _snapshot(loaded_app)
        bound = self._bind_trigger_runtime(loaded_app)
        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = dict(self.JOURNAL_MOVE)
        _write_config(before.runtime_paths.config_path, moved)
        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is False

        _write_config(before.runtime_paths.config_path, copy.deepcopy(VALID_CONFIG))

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is True
        after = _snapshot(loaded_app)
        assert after.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        assert after.generation == bound.config_generation, (
            "recovering from a refusal stranded external trigger delivery on the old binding"
        )

    def test_a_sqlite_database_url_edit_opens_the_same_file_and_is_adopted(self, loaded_app: FastAPI) -> None:
        """Only a change of opened database is a move; nothing else in the field is one.

        ``open_event_journal_store`` derives the SQLite path from the runtime
        storage root and reads no ``event_journal`` field to do it, so this edit
        opens exactly the same file. Refusing it would stop an adoption for a
        store that did not move -- and because a refusal never advances the
        adopted config, every later unrelated reload would be refused too.
        """
        before = _snapshot(loaded_app)
        same_store = copy.deepcopy(VALID_CONFIG)
        same_store["event_journal"] = {
            "backend": "sqlite",
            "database_url": "postgresql://localhost/never-opened-under-sqlite",
            "database_url_env": "OTHER_DATABASE_URL",
        }
        _write_config(before.runtime_paths.config_path, same_store)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is True
        adopted = _snapshot(loaded_app)
        assert adopted.generation == before.generation + 1
        assert adopted.config_data["event_journal"]["database_url_env"] == "OTHER_DATABASE_URL"

        later = copy.deepcopy(same_store)
        later["agents"]["probe"] = {"display_name": "Probe", "role": "Added after the journal edit"}
        _write_config(before.runtime_paths.config_path, later)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app) is True
        assert "probe" in _snapshot(loaded_app).config_data["agents"]

    def test_a_sqlite_database_url_write_is_saved_rather_than_rejected(self, loaded_app: FastAPI) -> None:
        """The write paths read the same rule, so they must agree that this is not a move."""
        before = _snapshot(loaded_app)
        same_store = copy.deepcopy(VALID_CONFIG)
        same_store["event_journal"] = {"backend": "sqlite", "database_url_env": "OTHER_DATABASE_URL"}

        config_lifecycle.replace_committed_config(
            _request_for(loaded_app),
            same_store,
            error_prefix="test replace",
        )

        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk["event_journal"]["database_url_env"] == "OTHER_DATABASE_URL"

    def test_a_postgres_dsn_reached_by_another_route_is_the_same_database(self, tmp_path: Path) -> None:
        """For PostgreSQL it is the resolved DSN that picks the database, not how it was written."""
        dsn = "postgresql://localhost/journal"
        config_path = tmp_path / "config.yaml"
        by_env = copy.deepcopy(VALID_CONFIG)
        by_env["event_journal"] = {"backend": "postgres"}
        _write_config(config_path, by_env)
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={"MINDROOM_EVENT_CACHE_DATABASE_URL": dsn},
        )
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True

        by_url = copy.deepcopy(VALID_CONFIG)
        by_url["event_journal"] = {"backend": "postgres", "database_url": dsn}
        _write_config(config_path, by_url)

        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True
        assert _snapshot(api_app).config_data["event_journal"]["database_url"] == dsn

    def test_a_postgres_dsn_change_is_still_refused(self, tmp_path: Path) -> None:
        """Loosening the comparison must not loosen it past the database actually opened."""
        config_path = tmp_path / "config.yaml"
        opened = copy.deepcopy(VALID_CONFIG)
        opened["event_journal"] = {"backend": "postgres", "database_url": "postgresql://localhost/journal"}
        _write_config(config_path, opened)
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={},
        )
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True

        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = {"backend": "postgres", "database_url": "postgresql://localhost/elsewhere"}
        _write_config(config_path, moved)

        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is False


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
