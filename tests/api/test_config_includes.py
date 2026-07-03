"""API behavior for configs composed from multiple files via !include.

Pins the include-aware snapshot contract: multi-file fingerprints, structured-save
rejection, raw-source editing of the top-level file, and include-aware hot reload.
"""

import asyncio
import copy
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mindroom import constants
from mindroom.api import config_lifecycle, main

SPLIT_TOP_SOURCE = (
    "agents: !include_dir_merge_named agents/\nmodels: !include models.yaml\ndefaults:\n  markdown: true\n"
)
TEST_AGENT_SOURCE = (
    "test_agent:\n"
    "  display_name: Test Agent\n"
    "  role: A test agent\n"
    "  tools: [calculator]\n"
    "  instructions: [Test instruction]\n"
    "  rooms: [test_room]\n"
)
MODELS_SOURCE = "default:\n  provider: ollama\n  id: test-model\n"


def _write_split_config(config_dir: Path) -> Path:
    config_path = config_dir / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(SPLIT_TOP_SOURCE, encoding="utf-8")
    (config_dir / "models.yaml").write_text(MODELS_SOURCE, encoding="utf-8")
    agents_dir = config_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "test_agent.yaml").write_text(TEST_AGENT_SOURCE, encoding="utf-8")
    return config_path


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
def split_runtime_paths(tmp_path: Path) -> constants.RuntimePaths:
    """Resolve one isolated runtime context backed by an include-based config tree."""
    config_path = _write_split_config(tmp_path / "conf")
    return constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


@pytest.fixture
def split_app(split_runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Return one API app with the include-based config loaded and committed."""
    api_app = _make_api_app(split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, api_app) is True
    return api_app


class TestIncludeAwareSnapshots:
    """Committed snapshots track the full source-file set and its fingerprint."""

    def test_load_publishes_source_files_and_multi_file_fingerprint(self, split_app: FastAPI) -> None:
        """Loading an include-based config records every source file in the snapshot."""
        snapshot = _snapshot(split_app)
        config_path = snapshot.runtime_paths.config_path
        assert snapshot.source_files is not None
        assert {path.name for path in snapshot.source_files} == {"config.yaml", "models.yaml", "test_agent.yaml"}
        assert snapshot.source_fingerprint is not None
        assert snapshot.source_fingerprint != hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert snapshot.config_data["agents"]["test_agent"]["display_name"] == "Test Agent"

    def test_single_file_config_keeps_plain_content_fingerprint(self, tmp_path: Path) -> None:
        """Monolith configs keep the plain sha256 fingerprint written by structured saves."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"models": {"default": {"provider": "ollama", "id": "test-model"}}}),
            encoding="utf-8",
        )
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={},
        )
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app) is True
        snapshot = _snapshot(api_app)
        assert snapshot.source_fingerprint == hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert snapshot.source_files == frozenset({config_path.resolve()})

    def test_fingerprint_changes_when_included_file_changes(self, split_app: FastAPI) -> None:
        """Editing only an included file changes the fingerprint and bumps the generation."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        agent_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agent_file.write_text(TEST_AGENT_SOURCE.replace("A test agent", "An edited agent"), encoding="utf-8")

        assert config_lifecycle.load_config_into_app(runtime_paths, split_app) is True

        after = _snapshot(split_app)
        assert after.source_fingerprint != snapshot.source_fingerprint
        assert after.generation == snapshot.generation + 1
        assert after.config_data["agents"]["test_agent"]["role"] == "An edited agent"

    def test_failed_reload_keeps_watching_the_broken_included_file(self, split_app: FastAPI) -> None:
        """A reload broken by an included file keeps that file in the watched source set."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        agent_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agent_file.write_text("test_agent: [not a mapping\n", encoding="utf-8")

        assert config_lifecycle.load_config_into_app(runtime_paths, split_app) is False

        after = _snapshot(split_app)
        assert after.config_load_result is not None
        assert after.config_load_result.success is False
        assert after.source_files == snapshot.source_files


class TestStructuredWritesRejected:
    """Structured saves must not silently flatten a split config."""

    def test_write_committed_config_is_rejected_with_409(self, split_app: FastAPI) -> None:
        """Structured mutations return 409 and leave every source file untouched."""
        runtime_paths = _snapshot(split_app).runtime_paths
        top_before = runtime_paths.config_path.read_text(encoding="utf-8")

        def _mutate(config: dict[str, Any]) -> None:
            config["agents"]["test_agent"]["role"] = "Flattened"

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_committed_config(
                _request_for(split_app),
                _mutate,
                error_prefix="Failed to save configuration",
            )

        assert exc_info.value.status_code == 409
        assert "!include" in str(exc_info.value.detail)
        assert runtime_paths.config_path.read_text(encoding="utf-8") == top_before

    def test_replace_committed_config_is_rejected_with_409(self, split_app: FastAPI) -> None:
        """Whole-config structured replacement returns 409 for include-based configs."""
        replacement = copy.deepcopy(_snapshot(split_app).config_data)
        replacement["agents"]["test_agent"]["role"] = "Flattened"

        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.replace_committed_config(
                _request_for(split_app),
                replacement,
                error_prefix="Failed to save configuration",
            )

        assert exc_info.value.status_code == 409

    def test_external_writer_persist_is_rejected(self, split_app: FastAPI) -> None:
        """validate_and_persist_config_payload raises the include-aware config error."""
        snapshot = _snapshot(split_app)
        payload = copy.deepcopy(snapshot.config_data)
        top_before = snapshot.runtime_paths.config_path.read_text(encoding="utf-8")

        with pytest.raises(config_lifecycle._ConfigComposedFromIncludesError, match="!include"):
            config_lifecycle.validate_and_persist_config_payload(payload, snapshot.runtime_paths)

        assert snapshot.runtime_paths.config_path.read_text(encoding="utf-8") == top_before
        assert _snapshot(split_app).generation == snapshot.generation


class TestRawSourceWithIncludes:
    """The raw editor keeps operating on the top-level file's literal text."""

    def test_config_uses_includes_reflects_the_loaded_source_set(self, split_app: FastAPI) -> None:
        """config_uses_includes is true exactly when includes resolved during load."""
        assert config_lifecycle.config_uses_includes(_request_for(split_app)) is True

    def test_raw_replace_of_top_level_file_still_works(self, split_app: FastAPI) -> None:
        """Raw saves rewrite only the top-level file and keep the include source set."""
        runtime_paths = _snapshot(split_app).runtime_paths
        agents_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agents_before = agents_file.read_text(encoding="utf-8")
        new_source = SPLIT_TOP_SOURCE.replace("markdown: true", "markdown: false")

        generation = config_lifecycle.replace_raw_config_source(
            _request_for(split_app),
            new_source,
            error_prefix="Failed to save raw configuration",
        )

        snapshot = _snapshot(split_app)
        assert generation == snapshot.generation
        assert runtime_paths.config_path.read_text(encoding="utf-8") == new_source
        assert agents_file.read_text(encoding="utf-8") == agents_before
        assert snapshot.config_data["defaults"]["markdown"] is False
        assert snapshot.source_files is not None
        assert runtime_paths.config_path.resolve() in snapshot.source_files
        assert agents_file.resolve() in snapshot.source_files

    def test_raw_replace_with_monolith_reenables_structured_saves(self, split_app: FastAPI) -> None:
        """Collapsing back to one file through the raw editor re-enables structured saves."""
        monolith_source = yaml.dump(copy.deepcopy(_snapshot(split_app).config_data))

        config_lifecycle.replace_raw_config_source(
            _request_for(split_app),
            monolith_source,
            error_prefix="Failed to save raw configuration",
        )

        assert config_lifecycle.config_uses_includes(_request_for(split_app)) is False

        def _mutate(config: dict[str, Any]) -> None:
            config["agents"]["test_agent"]["role"] = "Structured save works again"

        config_lifecycle.write_committed_config(
            _request_for(split_app),
            _mutate,
            error_prefix="Failed to save configuration",
        )
        assert _snapshot(split_app).config_data["agents"]["test_agent"]["role"] == "Structured save works again"


@pytest.mark.asyncio
async def test_watch_config_reloads_when_an_included_file_changes(
    monkeypatch: pytest.MonkeyPatch,
    split_runtime_paths: constants.RuntimePaths,
) -> None:
    """The API config watcher triggers a reload for edits to included files."""
    main.initialize_api_app(main.app, split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app) is True

    loaded_paths: list[Path] = []
    load_event = asyncio.Event()
    stop_event = asyncio.Event()

    def _record_load(runtime_paths: constants.RuntimePaths, _app: FastAPI) -> bool:
        loaded_paths.append(runtime_paths.config_path)
        load_event.set()
        return False

    monkeypatch.setattr(config_lifecycle, "load_config_into_app", _record_load)

    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))
    await asyncio.sleep(0.05)
    agent_file = split_runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
    future_time = time.time() + 5
    agent_file.write_text(TEST_AGENT_SOURCE.replace("A test agent", "Edited via include"), encoding="utf-8")
    os.utime(agent_file, (future_time, future_time))

    await asyncio.wait_for(load_event.wait(), timeout=2)
    stop_event.set()
    await watch_task

    assert loaded_paths == [split_runtime_paths.config_path]
