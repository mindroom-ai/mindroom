"""Direct unit tests for the API config state machine in mindroom.api.config_lifecycle.

These tests pin the committed-snapshot contract: load/validation failure handling,
generation tracking and stale-write rejection, request-pinned snapshots, the
file-watcher reload effects, and the concurrent-writer commit protocol.
"""

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI, HTTPException

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.config.load_attempt import ConfigLoad
from mindroom.config.main import Config, ConfigRuntimeValidationError, load_config
from mindroom.config.personal_agents import read_personal_agent_roster
from mindroom.event_journal.opening import record_opened_event_journal
from mindroom.personal_agents import _append_personal_agent_roster_entry
from tests.api.conftest import api_snapshot, make_api_app, request_for
from tests.conftest import test_runtime_paths
from tests.test_generated_agents import BASE as GENERATED_BASE
from tests.test_generated_agents import _created as created_entry
from tests.test_generated_agents import _load as load_generated
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry

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
    api_app = make_api_app(runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
    return api_app


@pytest.mark.parametrize("writer", ["persist", "api"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "defaults",
    [{}, {"tools": []}, {"tools": [], "markdown": False}, {"tools": ["scheduler"]}, {"tools": ["approved_egress"]}],
)
def test_approved_egress_structured_save_preserves_authored_tool_defaults(
    tmp_path: Path,
    writer: str,
    enabled: bool,
    defaults: dict[str, Any],
) -> None:
    """A structured save must preserve tool selection after the runtime overlay is disabled."""
    payload: dict[str, Any] = {
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {"research": {"display_name": "Research"}},
        "timezone": "Europe/Paris",
    }
    if defaults:
        payload["defaults"] = defaults
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, payload)
    paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_APPROVED_EGRESS_ENABLED": str(enabled).lower()},
    )

    if writer == "persist":
        config_lifecycle.validate_and_persist_config_payload(payload, paths)
    else:
        api_app = make_api_app(paths)
        assert config_lifecycle.load_config_into_app(paths, api_app).loaded
        config_lifecycle.replace_committed_config(request_for(api_app), payload, error_prefix="Save failed")

    saved = yaml.safe_load(config_path.read_text())
    assert saved.get("defaults", {}) == defaults
    assert saved["timezone"] == "Europe/Paris"
    disabled_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_APPROVED_EGRESS_ENABLED": "false"},
    )
    reloaded = load_config(disabled_paths)
    assert reloaded.resolve_agent("research").tools.tool_names == tuple(defaults.get("tools", ["scheduler"]))


@pytest.mark.parametrize("writer", ["persist", "structured", "raw"])
def test_save_refuses_a_name_a_personal_agent_reserved_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    """A save that validated before a user's pending agent took one of its names is refused without writing the file."""
    paths = test_runtime_paths(tmp_path)
    _write_config(paths.config_path, PERSONAL_AGENTS_BASE)
    api_app = make_api_app(paths)
    assert config_lifecycle.load_config_into_app(paths, api_app).loaded
    saved = paths.config_path.read_bytes()
    payload = copy.deepcopy(PERSONAL_AGENTS_BASE)
    payload["agents"] = {"mind_alice_smith": {"display_name": "Authored", "model": "default"}}
    validate = Config.validate_with_runtime

    def validate_then_reserve(data: object, runtime_paths: constants.RuntimePaths, **kwargs: bool) -> Config:
        config = validate(data, runtime_paths, **kwargs)
        # Alice's provisioning reserves the same name between this save's validation and its write.
        if not read_personal_agent_roster(runtime_paths):
            _append_personal_agent_roster_entry(runtime_paths, personal_agent_entry(pending=True))
        return config

    monkeypatch.setattr(Config, "validate_with_runtime", staticmethod(validate_then_reserve))
    request = request_for(api_app)
    writers = {
        "persist": lambda: config_lifecycle.validate_and_persist_config_payload(payload, paths),
        "structured": lambda: config_lifecycle.replace_committed_config(request, payload, error_prefix="Save failed"),
        "raw": lambda: config_lifecycle.replace_raw_config_source(request, yaml.dump(payload), error_prefix="Save"),
    }
    with pytest.raises((ConfigRuntimeValidationError, HTTPException), match=r"agents\.mind_alice_smith collides"):
        writers[writer]()

    assert paths.config_path.read_bytes() == saved


@pytest.mark.parametrize("section", ["agents", "teams"])
def test_save_refuses_a_created_agent_name(tmp_path: Path, section: str) -> None:
    """An authored agent or team under a created agent's name would take over its Matrix account, so saving it fails."""
    paths = test_runtime_paths(tmp_path)
    load_generated(tmp_path, created=[created_entry()])
    _write_config(paths.config_path, GENERATED_BASE)
    saved = paths.config_path.read_bytes()
    payload = copy.deepcopy(GENERATED_BASE)
    authored = {"display_name": "Authored", "role": "Takes the name", "agents": ["creator"]}
    payload.setdefault(section, {})["creator_research"] = (
        authored if section == "teams" else {"display_name": "Authored"}
    )

    with pytest.raises(ConfigRuntimeValidationError, match=rf"{section}\.creator_research collides"):
        config_lifecycle.validate_and_persist_config_payload(payload, paths)

    assert paths.config_path.read_bytes() == saved


class TestLoadAndValidationFailure:
    """Loading and validation-failure behavior of the committed config cache."""

    def test_initial_load_publishes_committed_snapshot(self, runtime_paths: constants.RuntimePaths) -> None:
        """A successful first load publishes data, runtime config, and generation 1."""
        api_app = make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
        snapshot = api_snapshot(api_app)
        assert snapshot.generation == 1
        assert snapshot.config_data["agents"]["test_agent"]["display_name"] == "Test Agent"
        assert snapshot.runtime_config is not None
        assert snapshot.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        assert snapshot.source_fingerprint is not None

    def test_initial_load_persists_access_migration_and_fingerprints_rewrite(self, tmp_path: Path) -> None:
        """The API loader must publish the migrated monolith under its rewritten fingerprint."""
        config_path = tmp_path / "config.yaml"
        original = "authorization:\n  global_users:\n    - '@owner:example.com'\n"
        config_path.write_text(original, encoding="utf-8")
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={},
        )
        api_app = make_api_app(runtime_paths)

        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded

        snapshot = api_snapshot(api_app)
        rewritten = config_path.read_bytes()
        assert snapshot.config_data["administrators"] == ["@owner:example.com"]
        assert snapshot.source_fingerprint == hashlib.sha256(rewritten).hexdigest()
        assert b"global_users" not in rewritten
        backup_path = config_path.with_name(f"{config_path.name}.pre-membership-access")
        assert backup_path.read_text(encoding="utf-8") == original

    def test_reload_of_unchanged_source_does_not_bump_generation(self, loaded_app: FastAPI) -> None:
        """Reloading byte-identical source keeps the generation stable."""
        generation = api_snapshot(loaded_app).generation
        assert config_lifecycle.load_config_into_app(api_snapshot(loaded_app).runtime_paths, loaded_app).loaded
        assert api_snapshot(loaded_app).generation == generation

    def test_read_before_any_load_raises_500(self, runtime_paths: constants.RuntimePaths) -> None:
        """Reads against a never-loaded app surface the shared missing-config error."""
        api_app = make_api_app(runtime_paths)
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.read_committed_config(request_for(api_app), lambda config: config)
        assert exc_info.value.status_code == 500

    def test_validation_failure_keeps_last_good_committed_config(self, loaded_app: FastAPI) -> None:
        """A Pydantic validation failure marks the load failed without clobbering last-good state."""
        snapshot = api_snapshot(loaded_app)
        runtime_paths = snapshot.runtime_paths
        runtime_paths.config_path.write_text("agents: not-a-mapping\n", encoding="utf-8")

        assert not config_lifecycle.load_config_into_app(runtime_paths, loaded_app).loaded

        failed = api_snapshot(loaded_app)
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
            config_lifecycle.read_committed_config(request_for(loaded_app), lambda config: config)
        assert exc_info.value.status_code == 422

    def test_malformed_yaml_then_good_edit_recovers(self, loaded_app: FastAPI) -> None:
        """A malformed external edit fails the load; a later good edit fully recovers."""
        runtime_paths = api_snapshot(loaded_app).runtime_paths
        runtime_paths.config_path.write_text("agents: [unclosed\n", encoding="utf-8")
        assert not config_lifecycle.load_config_into_app(runtime_paths, loaded_app).loaded
        assert api_snapshot(loaded_app).config_data["agents"]["test_agent"]["role"] == "A test agent"

        _write_config(runtime_paths.config_path, VALID_CONFIG)
        assert config_lifecycle.load_config_into_app(runtime_paths, loaded_app).loaded
        recovered = api_snapshot(loaded_app)
        assert recovered.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        result = config_lifecycle.read_committed_config(request_for(loaded_app), lambda config: dict(config))
        assert result["agents"]["test_agent"]["display_name"] == "Test Agent"


class TestGenerationTrackingAndWrites:
    """Generation tracking and stale-write detection for API config writes."""

    def test_stale_expected_generation_is_rejected(self, loaded_app: FastAPI) -> None:
        """A write carrying a stale client generation is rejected with 409."""
        current_generation = api_snapshot(loaded_app).generation
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_committed_config(
                request_for(loaded_app),
                copy.deepcopy(VALID_CONFIG),
                error_prefix="test replace",
                expected_generation=current_generation - 1,
            )
        assert exc_info.value.status_code == 409

    def test_current_generation_write_commits_and_bumps(self, loaded_app: FastAPI) -> None:
        """A current-generation write commits to memory and disk and bumps the generation."""
        before = api_snapshot(loaded_app)
        new_config = copy.deepcopy(VALID_CONFIG)
        new_config["agents"]["test_agent"]["role"] = "An updated test agent"

        new_generation = config_lifecycle.replace_committed_config(
            request_for(loaded_app),
            new_config,
            error_prefix="test replace",
            expected_generation=before.generation,
        )

        after = api_snapshot(loaded_app)
        assert new_generation == before.generation + 1
        assert after.generation == new_generation
        assert after.config_data["agents"]["test_agent"]["role"] == "An updated test agent"
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk["agents"]["test_agent"]["role"] == "An updated test agent"

    def test_committed_write_reload_echo_does_not_bump_generation(self, loaded_app: FastAPI) -> None:
        """The watcher reload triggered by the API's own write is fingerprint-suppressed."""
        config_lifecycle.write_committed_config(
            request_for(loaded_app),
            lambda config: config["agents"]["test_agent"]["instructions"].append("Extra instruction"),
            error_prefix="test write",
        )
        committed = api_snapshot(loaded_app)
        # The file watcher reloads after every write; the matching source fingerprint
        # must suppress a second generation bump for the API's own write.
        assert config_lifecycle.load_config_into_app(committed.runtime_paths, loaded_app).loaded
        assert api_snapshot(loaded_app).generation == committed.generation

    def test_invalid_mutation_is_rejected_without_commit(self, loaded_app: FastAPI) -> None:
        """A mutation producing an invalid config raises 422 and leaves the snapshot untouched."""
        before = api_snapshot(loaded_app)
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                request_for(loaded_app),
                lambda config: config.__setitem__("agents", "not-a-mapping"),
                error_prefix="test write",
            )
        assert exc_info.value.status_code == 422
        assert api_snapshot(loaded_app) is before

    def test_raw_replacement_preserves_source_and_rejects_stale_writes(self, loaded_app: FastAPI) -> None:
        """Raw source replacement keeps the source byte-exact and honors generation checks."""
        before = api_snapshot(loaded_app)
        raw_source = "# keep this comment\n" + yaml.dump(VALID_CONFIG)

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_raw_config_source(
                request_for(loaded_app),
                raw_source,
                error_prefix="test raw replace",
                expected_generation=before.generation - 1,
            )
        assert exc_info.value.status_code == 409

        new_generation = config_lifecycle.replace_raw_config_source(
            request_for(loaded_app),
            raw_source,
            error_prefix="test raw replace",
            expected_generation=before.generation,
        )
        assert new_generation == before.generation + 1
        assert before.runtime_paths.config_path.read_text(encoding="utf-8") == raw_source

    def test_full_replacement_recovers_from_broken_on_disk_config(self, loaded_app: FastAPI) -> None:
        """Full replacement skips the failed-load gate so the editor can repair a broken config."""
        runtime_paths = api_snapshot(loaded_app).runtime_paths
        runtime_paths.config_path.write_text("agents: [unclosed\n", encoding="utf-8")
        assert not config_lifecycle.load_config_into_app(runtime_paths, loaded_app).loaded

        # Unlike mutations, full replacement skips the failed-load gate so the
        # raw editor can repair a broken config through the API.
        config_lifecycle.replace_committed_config(
            request_for(loaded_app),
            copy.deepcopy(VALID_CONFIG),
            error_prefix="test replace",
        )
        recovered = api_snapshot(loaded_app)
        assert recovered.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
        assert yaml.safe_load(runtime_paths.config_path.read_text(encoding="utf-8")) == recovered.config_data


class TestRequestSnapshotLifecycle:
    """Request-pinned snapshot consistency across concurrent commits."""

    def test_pinned_snapshot_stays_consistent_across_commits(self, loaded_app: FastAPI) -> None:
        """A snapshot pinned at request start keeps serving its original coherent state."""
        request = request_for(loaded_app)
        pinned = config_lifecycle.bind_current_request_snapshot(request)

        config_lifecycle.write_committed_config(
            request_for(loaded_app),
            lambda config: config["agents"]["test_agent"].__setitem__("role", "Changed mid-request"),
            error_prefix="test write",
        )
        assert api_snapshot(loaded_app).generation == pinned.generation + 1

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
        stale_request = request_for(loaded_app)
        config_lifecycle.bind_current_request_snapshot(stale_request)
        config_lifecycle.write_committed_config(
            request_for(loaded_app),
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
        instructions = api_snapshot(loaded_app).config_data["agents"]["test_agent"]["instructions"]
        assert instructions == ["Test instruction", "Won the race"]


class TestFileWatcherReload:
    """File-watcher reload effects on committed state."""

    def test_external_edit_updates_committed_state_and_generation(self, loaded_app: FastAPI) -> None:
        """A valid external file edit advances committed data and the generation."""
        before = api_snapshot(loaded_app)
        external = copy.deepcopy(VALID_CONFIG)
        external["agents"]["test_agent"]["role"] = "Edited outside the API"
        _write_config(before.runtime_paths.config_path, external)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app).loaded
        after = api_snapshot(loaded_app)
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
        real_load = config_lifecycle.load_config_attempt

        def load_then_lose_race(paths: constants.RuntimePaths) -> ConfigLoad:
            attempt = real_load(paths)
            with state.config_lock:
                state.snapshot = config_lifecycle._published_snapshot(state.snapshot)
            return attempt

        monkeypatch.setattr(config_lifecycle, "load_config_attempt", load_then_lose_race)
        racing_generation = state.snapshot.generation + 1
        published = config_lifecycle.load_config_into_app(runtime_paths, loaded_app)
        assert published.superseded
        assert published.config is not None
        # The concurrent commit's snapshot is left untouched by the stale load.
        assert api_snapshot(loaded_app).generation == racing_generation


class TestExternalWriterPublishing:
    """External config writers publishing into registered API apps."""

    def test_validate_and_persist_publishes_to_registered_apps(self, loaded_app: FastAPI) -> None:
        """validate_and_persist_config_payload advances every matching registered app snapshot."""
        before = api_snapshot(loaded_app)
        payload = copy.deepcopy(VALID_CONFIG)
        payload["agents"]["test_agent"]["role"] = "Updated by an external writer"

        config_lifecycle.validate_and_persist_config_payload(payload, before.runtime_paths)

        after = api_snapshot(loaded_app)
        assert after.generation == before.generation + 1
        assert after.config_data["agents"]["test_agent"]["role"] == "Updated by an external writer"
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == after.config_data


class TestCommitRevision:
    """Every publication gets a commit identity, including ones the generation ignores."""

    def test_a_write_pinned_before_a_republication_is_rejected(self, loaded_app: FastAPI) -> None:
        """Generation is a consumer identity and deliberately stands still sometimes.

        A byte-identical reload republishes the snapshot without advancing the
        generation, because nothing a consumer binds to changed. A writer that
        used the generation as its own compare-and-swap identity would be blind
        to exactly those publications, and would commit a payload built against
        a snapshot that is no longer the current one. The separate revision is
        what a writer checks.
        """
        before = api_snapshot(loaded_app)
        request = request_for(loaded_app)
        pinned = config_lifecycle.bind_current_request_snapshot(request)

        assert config_lifecycle.load_config_into_app(before.runtime_paths, loaded_app).loaded
        republished = api_snapshot(loaded_app)
        assert republished.generation == pinned.generation, "this test only bites while the generation stands still"
        assert republished.revision > pinned.revision

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                request,
                lambda config: config["defaults"].__setitem__("markdown", False),
                error_prefix="test write",
            )

        assert exc_info.value.status_code == 409
        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk["defaults"]["markdown"] is True, "a write pinned before the republication still committed"


class TestEventJournalPendingRestart:
    """The saved event_journal is not always the one in force, and the API says which."""

    def test_no_pending_restart_when_the_saved_journal_is_the_open_one(self, loaded_app: FastAPI) -> None:
        """A flag is only meaningful if it is quiet by default."""
        snapshot = api_snapshot(loaded_app)
        assert snapshot.runtime_config is not None
        record_opened_event_journal(snapshot.runtime_config.event_journal, runtime_paths=snapshot.runtime_paths)

        assert config_lifecycle.config_pending_restart(request_for(loaded_app)) is False

    def test_a_saved_journal_move_is_reported_as_pending_a_restart(self, loaded_app: FastAPI) -> None:
        """The dashboard shows the saved value, so it has to be told the value is not live yet."""
        before = api_snapshot(loaded_app)
        assert before.runtime_config is not None
        record_opened_event_journal(before.runtime_config.event_journal, runtime_paths=before.runtime_paths)

        moved = copy.deepcopy(VALID_CONFIG)
        moved["event_journal"] = {"backend": "postgres", "database_url": "postgresql://journal.invalid/moved"}
        config_lifecycle.replace_committed_config(
            request_for(loaded_app),
            moved,
            error_prefix="test replace",
        )

        on_disk = yaml.safe_load(before.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk["event_journal"]["backend"] == "postgres", "the write was rejected instead of saved"
        assert config_lifecycle.config_pending_restart(request_for(loaded_app)) is True


class TestConcurrencySmoke:
    """Interleaved writers racing on the same committed snapshot."""

    def test_interleaved_writers_one_winner_per_generation(self, loaded_app: FastAPI) -> None:
        """Exactly one writer wins each generation and the final state is never torn."""
        before = api_snapshot(loaded_app)
        writer_count = 4
        # The constructor timeout applies to every wait(), so a writer that dies
        # before reaching the barrier breaks it for the others immediately instead
        # of letting them idle until pytest's global 60s timeout.
        barrier = threading.Barrier(writer_count, timeout=10)
        outcomes: dict[str, str | int] = {}

        def write_marker(marker: str) -> None:
            request = request_for(loaded_app)
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

        after = api_snapshot(loaded_app)
        # Exactly one writer wins each generation; losers see the stale-write conflict.
        assert after.generation == before.generation + len(winners)
        instructions = after.config_data["agents"]["test_agent"]["instructions"]
        assert instructions[0] == "Test instruction"
        assert set(instructions[1:]) == winners
        # No torn state: the on-disk file matches the committed snapshot exactly.
        on_disk = yaml.safe_load(after.runtime_paths.config_path.read_text(encoding="utf-8"))
        assert on_disk == after.config_data
        assert after.config_load_result == config_lifecycle.ConfigLoadResult(success=True)
