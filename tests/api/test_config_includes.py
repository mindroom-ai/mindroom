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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.cli.config import validate_config_source_quiet
from mindroom.commands.config_commands import apply_config_change
from mindroom.config import load_attempt
from mindroom.config.commit import ConfigComposedFromIncludesError
from mindroom.config.legacy_access import AccessMigrationError
from mindroom.config.load_attempt import ConfigLoad
from mindroom.config.main import ConfigFileChangedError, ConfigRuntimeValidationError, load_config
from mindroom.custom_tools.config_manager import ConfigManagerTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.api.conftest import commit_tool_change, save_config, save_edited_config, save_raw_config
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    failed_config_load,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
)
from tests.file_watcher_helpers import record_watcher_scans

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
    assert config_lifecycle.load_config_into_app(split_runtime_paths, api_app).loaded
    return api_app


@pytest.fixture(params=[False, True], ids=["empty", "ignored-only"])
def empty_include_runtime_paths(tmp_path: Path, request: pytest.FixtureRequest) -> constants.RuntimePaths:
    """Create an authored directory include that reads no child files."""
    entries = tmp_path / "entries"
    entries.mkdir()
    if request.param:
        for name in (".hidden.yaml", "_draft.yaml", "notes.txt", ".hidden/item.yaml", "_shared/item.yaml"):
            ignored = entries / name
            ignored.parent.mkdir(parents=True, exist_ok=True)
            ignored.write_text("ignored: true\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default: {provider: ollama, id: test-model}\nagents: !include_dir_merge_named entries/\n",
        encoding="utf-8",
    )
    return constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


@pytest.fixture
def stale_monolith_app(empty_include_runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Publish a monolith, then add an include without publishing a reload."""
    runtime_paths = empty_include_runtime_paths
    source = (
        "models:\n  default: {provider: ollama, id: test-model}\n"
        "agents:\n  operator: {display_name: Operator, role: Original role}\n"
        "teams: {}\n"
        "administrators: ['@admin:example.org']\n"
    )
    runtime_paths.config_path.write_text(source, encoding="utf-8")
    api_app = _make_api_app(runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
    before = _snapshot(api_app)
    assert before.uses_includes is False
    runtime_paths.config_path.write_text(
        source.replace("teams: {}", "teams: !include_dir_merge_named entries/"),
        encoding="utf-8",
    )
    config = load_config(runtime_paths)
    assert config.uses_includes is True
    assert config.source_files == frozenset({runtime_paths.config_path.resolve()})
    assert _snapshot(api_app) is before
    return api_app


class TestEmptyDirectoryIncludes:
    """Include topology survives even when a directory contributes no files."""

    @pytest.mark.parametrize("operation", ["inspect", "dry-run", "save"])
    def test_self_config_preserves_empty_include_with_stale_api_snapshot(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
    ) -> None:
        """Self-config reads must honor include tags even before the API reloads."""
        runtime_paths = empty_include_runtime_paths
        path = runtime_paths.config_path
        include_source = path.read_text(encoding="utf-8")
        path.write_text(
            include_source.replace("agents: !include_dir_merge_named entries/", "agents: {}"),
            encoding="utf-8",
        )
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
        before = _snapshot(api_app)
        assert before.uses_includes is False
        path.write_text(include_source, encoding="utf-8")
        monkeypatch.setattr(
            "mindroom.custom_tools.config_manager.platform_administrator_error",
            lambda _config, _message: None,
        )
        manager = ConfigManagerTools(runtime_paths)

        if operation == "inspect":
            result = manager.manage_config(operation="inspect")
            assert "structured patching is unavailable" in result
        else:
            result = manager.manage_config(
                operation="patch",
                changes=[{"op": "add", "path": "/timezone", "value": "Europe/Amsterdam"}],
                dry_run=operation == "dry-run",
            )
            assert result.startswith("Error:")
            assert "!include" in result

        assert path.read_text(encoding="utf-8") == include_source
        assert _snapshot(api_app) is before

    @pytest.mark.parametrize(
        "include_source",
        [
            "agents: !include_dir_named entries/\n",
            "agents: !include_dir_merge_named entries/\n",
            "defaults:\n  tools: !include_dir_list entries/\n",
            "defaults:\n  tools: !include_dir_merge_list entries/\n",
        ],
    )
    def test_load_config_records_include_use_without_child_files(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        include_source: str,
    ) -> None:
        """The runtime loader retains tag usage independently of the file count."""
        path = empty_include_runtime_paths.config_path
        path.write_text(
            "models:\n  default: {provider: ollama, id: test-model}\n" + include_source,
            encoding="utf-8",
        )

        config = load_config(empty_include_runtime_paths)

        assert config.source_files == frozenset({path.resolve()})
        assert config.uses_includes is True
        assert config.source_fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_load_endpoint_reports_empty_include(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
    ) -> None:
        """The dashboard receives the include warning when only the root was read."""
        main.initialize_api_app(main.app, empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, main.app).loaded
        client = TestClient(main.app, base_url="http://localhost")

        response = client.post("/api/config/load")

        assert response.status_code == 200
        assert response.headers[config_lifecycle.CONFIG_USES_INCLUDES_HEADER] == "true"
        assert _snapshot(main.app).source_files == frozenset({empty_include_runtime_paths.config_path.resolve()})
        assert config_lifecycle.config_uses_includes(_request_for(main.app)) is True

    def test_structured_save_preserves_empty_include(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
    ) -> None:
        """A structured save refuses to erase an empty directory tag."""
        api_app = _make_api_app(empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
        before = _snapshot(api_app)
        source = empty_include_runtime_paths.config_path.read_bytes()

        with pytest.raises(HTTPException) as exc_info:
            save_config(
                _request_for(api_app),
                copy.deepcopy(before.runtime_config.authored_model_dump()),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "config_composed_from_includes"
        assert empty_include_runtime_paths.config_path.read_bytes() == source
        assert _snapshot(api_app) is before

    @pytest.mark.parametrize("committed", [False, True], ids=["disk-only", "committed"])
    def test_external_writer_preserves_empty_include(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        committed: bool,
    ) -> None:
        """External structured writes protect both parsed and committed include metadata."""
        api_app = _make_api_app(empty_include_runtime_paths)
        if committed:
            assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
        source = empty_include_runtime_paths.config_path.read_bytes()
        config = load_config(empty_include_runtime_paths)

        with pytest.raises(ConfigComposedFromIncludesError, match="!include"):
            commit_tool_change(
                config.authored_model_dump(),
                empty_include_runtime_paths,
            )

        assert empty_include_runtime_paths.config_path.read_bytes() == source

    def test_raw_save_updates_empty_include_metadata(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
    ) -> None:
        """Raw saves add and remove the guard, with matching follow-up reload metadata."""
        path = empty_include_runtime_paths.config_path
        include_source = path.read_text(encoding="utf-8")
        monolith_source = "models:\n  default: {provider: ollama, id: test-model}\n"
        path.write_text(monolith_source, encoding="utf-8")
        api_app = _make_api_app(empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded

        save_raw_config(
            _request_for(api_app),
            include_source,
        )

        assert path.read_text(encoding="utf-8") == include_source
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True
        generation = _snapshot(api_app).generation
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
        assert _snapshot(api_app).generation == generation
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True

        save_raw_config(
            _request_for(api_app),
            monolith_source,
        )

        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is False
        save_config(
            _request_for(api_app),
            copy.deepcopy(_snapshot(api_app).runtime_config.authored_model_dump()),
        )

    @pytest.mark.parametrize("broken_source", ["agents: [broken\n", "agents: [invalid]\n"])
    def test_failed_reload_retains_empty_include_guard(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        broken_source: str,
    ) -> None:
        """A failed replacement load cannot clear the last committed include guard."""
        api_app = _make_api_app(empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
        payload = copy.deepcopy(_snapshot(api_app).runtime_config.authored_model_dump())
        empty_include_runtime_paths.config_path.write_text(broken_source, encoding="utf-8")

        assert not config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True
        with pytest.raises(HTTPException) as exc_info:
            save_config(
                _request_for(api_app),
                payload,
            )

        assert exc_info.value.status_code == 409
        assert empty_include_runtime_paths.config_path.read_text(encoding="utf-8") == broken_source

    def test_reinitialization_retains_only_matching_runtime_include_metadata(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        tmp_path: Path,
    ) -> None:
        """Rebinding the same runtime keeps the guard; a different runtime starts fresh."""
        api_app = _make_api_app(empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded

        main.initialize_api_app(api_app, empty_include_runtime_paths)
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True

        other_runtime = constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "other.yaml",
            storage_path=tmp_path / "other-storage",
            process_env={},
        )
        main.initialize_api_app(api_app, other_runtime)
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is False

    @pytest.mark.parametrize("loader", ["runtime", "cli", "api"])
    def test_access_migration_preserves_empty_include(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
        loader: str,
    ) -> None:
        """Every source-loading migration entry point rejects without writing a backup."""
        path = empty_include_runtime_paths.config_path
        source = path.read_bytes() + b"authorization:\n  global_users: ['@owner:example.com']\n"
        path.write_bytes(source)
        if loader == "api":
            api_app = _make_api_app(empty_include_runtime_paths)
            assert not config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded
            result = _snapshot(api_app).config_load_result
            assert result is not None
            assert result.error_status_code == 422
            assert "does not support !include" in str(result.error_detail)
            assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True
        elif loader == "runtime":
            with pytest.raises(AccessMigrationError, match="does not support !include"):
                load_config(empty_include_runtime_paths)
        else:
            with pytest.raises(AccessMigrationError, match="does not support !include"):
                validate_config_source_quiet(empty_include_runtime_paths, source=source, original=source)

        assert path.read_bytes() == source
        assert not path.with_name(f"{path.name}.pre-membership-access").exists()

    def test_raw_access_migration_preserves_empty_include(
        self,
        empty_include_runtime_paths: constants.RuntimePaths,
    ) -> None:
        """Raw source validation rejects legacy access before replacing the authored root."""
        path = empty_include_runtime_paths.config_path
        source = path.read_text(encoding="utf-8")
        api_app = _make_api_app(empty_include_runtime_paths)
        assert config_lifecycle.load_config_into_app(empty_include_runtime_paths, api_app).loaded

        with pytest.raises(HTTPException) as exc_info:
            save_raw_config(
                _request_for(api_app),
                source + "authorization:\n  global_users: ['@owner:example.com']\n",
            )

        assert exc_info.value.status_code == 422
        assert "does not support !include" in str(exc_info.value.detail)
        assert path.read_text(encoding="utf-8") == source
        assert not path.with_name(f"{path.name}.pre-membership-access").exists()


@pytest.mark.parametrize(
    "failing_source",
    [
        pytest.param(None, id="valid"),
        pytest.param("agents: !include missing.yaml\n", id="missing-file"),
        pytest.param("agents: !include broken.yaml\n", id="malformed-child"),
        pytest.param("agents: !include_dir_merge_named missing/\n", id="missing-directory"),
        pytest.param(
            "teams: !include_dir_merge_named entries/\ndefaults: [broken\n",
            id="malformed-root",
        ),
        pytest.param("agents: !include /outside.yaml\n", id="absolute-path"),
        pytest.param("agents: !include ../outside.yaml\n", id="escaping-path"),
        pytest.param("agents: !include .hidden.yaml\n", id="hidden-path"),
        pytest.param("agents: !include [unexpected]\n", id="non-scalar-path"),
        pytest.param("agents: !expand missing.yaml\n", id="misplaced-expand"),
        pytest.param("agents: !<!include> missing.yaml\ndefaults: [broken\n", id="verbatim-tag"),
        pytest.param(
            "%TAG !cfg! !\n---\nagents: !cfg!include missing.yaml\ndefaults: [broken\n",
            id="tag-directive",
        ),
    ],
)
@pytest.mark.parametrize("write_kind", ["replacement", "external"])
def test_structured_writes_recheck_includes_before_watcher_reload(
    stale_monolith_app: FastAPI,
    write_kind: str,
    failing_source: str | None,
) -> None:
    """Unpublished include topology must protect every structured writer."""
    before = _snapshot(stale_monolith_app)
    runtime_paths = before.runtime_paths
    payload = load_config(runtime_paths).authored_model_dump()
    payload["timezone"] = "Europe/Amsterdam"
    if failing_source is not None:
        runtime_paths.config_path.with_name("broken.yaml").write_text("operator: [broken\n", encoding="utf-8")
        runtime_paths.config_path.write_text(failing_source, encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_config(runtime_paths)
    source = runtime_paths.config_path.read_bytes()

    if write_kind == "external":
        with pytest.raises(ConfigComposedFromIncludesError, match="!include"):
            commit_tool_change(payload, runtime_paths, loaded=before.runtime_config)
    else:
        with pytest.raises(HTTPException) as exc_info:
            save_config(_request_for(stale_monolith_app), payload)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "config_composed_from_includes"

    assert runtime_paths.config_path.read_bytes() == source
    assert _snapshot(stale_monolith_app) is before
    assert not runtime_paths.config_path.with_suffix(".yaml.tmp").exists()


@pytest.mark.parametrize(
    "broken_source",
    [
        pytest.param("agents: [broken\n", id="plain"),
        pytest.param("# agents: !include missing.yaml\nagents: [broken\n", id="comment"),
        pytest.param("timezone: '!include missing.yaml'\nagents: [broken\n", id="single-quoted"),
        pytest.param('timezone: "!expand missing.yaml"\nagents: [broken\n', id="double-quoted"),
        pytest.param("role: |\n  !include missing.yaml\nagents: [broken\n", id="block-scalar"),
    ],
)
@pytest.mark.parametrize("write_kind", ["replacement", "external"])
def test_structured_writes_recover_broken_monolith_before_watcher_reload(
    stale_monolith_app: FastAPI,
    write_kind: str,
    broken_source: str,
) -> None:
    """A parse failure without include evidence must remain recoverable."""
    before = _snapshot(stale_monolith_app)
    runtime_paths = before.runtime_paths
    runtime_paths.config_path.write_text(broken_source, encoding="utf-8")
    payload = copy.deepcopy(before.runtime_config.authored_model_dump())
    payload["timezone"] = "Europe/Amsterdam"

    if write_kind == "external":
        # A tool's change came from the file it loaded before the break, so it is refused, never a recovery.
        with pytest.raises(ConfigFileChangedError):
            commit_tool_change(payload, runtime_paths, loaded=before.runtime_config)
        assert runtime_paths.config_path.read_text(encoding="utf-8") == broken_source
        return
    save_config(_request_for(stale_monolith_app), payload)

    after = _snapshot(stale_monolith_app)
    assert after.revision == before.revision + 1
    assert after.uses_includes is False
    assert after.runtime_config.authored_model_dump()["timezone"] == "Europe/Amsterdam"
    assert load_config(runtime_paths).timezone == "Europe/Amsterdam"


@pytest.mark.parametrize("operation", ["agent-create", "agent-update", "team-create", "config-patch"])
def test_config_tools_preserve_new_includes_before_watcher_reload(
    stale_monolith_app: FastAPI,
    operation: str,
) -> None:
    """Authorized tools must preserve newly authored include source."""
    before = _snapshot(stale_monolith_app)
    runtime_paths = before.runtime_paths
    source = runtime_paths.config_path.read_bytes()
    config = load_config(runtime_paths)
    manager = ConfigManagerTools(runtime_paths)
    context = make_test_tool_runtime_context(
        agent_name="operator",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id="$request",
        ),
        requester_id="@admin:example.org",
        client=make_matrix_client_mock(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )
    with tool_runtime_context(context):
        if operation == "agent-create":
            result = manager.manage_agent(
                operation="create",
                agent_name="new_agent",
                display_name="New Agent",
                role="New role",
            )
        elif operation == "agent-update":
            result = manager.manage_agent(operation="update", agent_name="operator", role="Updated role")
        elif operation == "team-create":
            result = manager.manage_team(
                team_name="reviewers",
                display_name="Reviewers",
                role="Review",
                agents=["operator"],
            )
        else:
            result = manager.manage_config(
                operation="patch",
                changes=[{"op": "replace", "path": "/agents/operator/role", "value": "Updated role"}],
            )

    assert "!include" in result
    assert "Changes were NOT applied." in result
    assert runtime_paths.config_path.read_bytes() == source
    assert _snapshot(stale_monolith_app) is before
    (runtime_paths.config_path.parent / "entries" / "late_team.yaml").write_text(
        "late_team: {display_name: Late Team, role: Later addition, agents: [operator]}\n",
        encoding="utf-8",
    )
    assert "late_team" in load_config(runtime_paths).teams


@pytest.mark.asyncio
async def test_confirmed_config_change_preserves_new_includes_before_watcher_reload(
    stale_monolith_app: FastAPI,
) -> None:
    """Confirmed command persistence must honor unpublished include topology."""
    before = _snapshot(stale_monolith_app)
    source = before.runtime_paths.config_path.read_bytes()

    result = await apply_config_change("agents.operator.role", "Updated role", before.runtime_paths)

    assert "!include" in result
    assert "Changes were NOT applied." in result
    assert before.runtime_paths.config_path.read_bytes() == source
    assert _snapshot(stale_monolith_app) is before


@pytest.mark.parametrize(
    "broken_source",
    [
        pytest.param(b'timezone: "\\q"\nagents: !include missing.yaml\n', id="scanner-before-include"),
        pytest.param(b'timezone: "\\q"\nagents: {}\n', id="scanner-monolith"),
        pytest.param(b"# \xff\nagents: !include missing.yaml\n", id="unicode-before-include"),
        pytest.param(b"# \xff\nagents: {}\n", id="unicode-monolith"),
    ],
)
@pytest.mark.parametrize("write_kind", ["replacement", "external"])
def test_structured_writes_preserve_unknown_source_topology_before_watcher_reload(
    stale_monolith_app: FastAPI,
    write_kind: str,
    broken_source: bytes,
) -> None:
    """Incomplete scans require source editing, even without a tag observed yet."""
    before = _snapshot(stale_monolith_app)
    runtime_paths = before.runtime_paths
    runtime_paths.config_path.write_bytes(broken_source)
    payload = copy.deepcopy(before.runtime_config.authored_model_dump())
    payload["timezone"] = "Europe/Amsterdam"

    if write_kind == "external":
        with pytest.raises(ConfigRuntimeValidationError, match="could not determine"):
            commit_tool_change(payload, runtime_paths, loaded=before.runtime_config)
    else:
        with pytest.raises(HTTPException) as exc_info:
            save_config(_request_for(stale_monolith_app), payload)
        assert exc_info.value.status_code == 422
        assert "could not determine" in str(exc_info.value.detail)
        assert "edit the source files instead" in str(exc_info.value.detail)

    assert runtime_paths.config_path.read_bytes() == broken_source
    assert _snapshot(stale_monolith_app) is before
    assert not runtime_paths.config_path.with_suffix(".yaml.tmp").exists()

    save_raw_config(
        _request_for(stale_monolith_app),
        yaml.safe_dump(payload),
    )
    assert load_config(runtime_paths).timezone == "Europe/Amsterdam"
    assert _snapshot(stale_monolith_app).uses_includes is False


@pytest.mark.parametrize("write_kind", ["replacement", "external"])
def test_structured_writes_preserve_unreadable_existing_source(
    stale_monolith_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    write_kind: str,
) -> None:
    """An unreadable existing root is not evidence that flattening is safe."""
    before = _snapshot(stale_monolith_app)
    runtime_paths = before.runtime_paths
    path = runtime_paths.config_path
    source = b"agents: !include missing.yaml\n"
    path.write_bytes(source)
    payload = copy.deepcopy(before.runtime_config.authored_model_dump())
    payload["timezone"] = "Europe/Amsterdam"
    original_read = Path.read_bytes

    def deny_root_read(candidate: Path) -> bytes:
        if candidate == path:
            message = "Config root is temporarily unreadable"
            raise PermissionError(message)
        return original_read(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", deny_root_read)
        if write_kind == "external":
            with pytest.raises(ConfigRuntimeValidationError, match="could not determine"):
                commit_tool_change(payload, runtime_paths, loaded=before.runtime_config)
        else:
            with pytest.raises(HTTPException) as exc_info:
                save_config(_request_for(stale_monolith_app), payload)
            assert exc_info.value.status_code == 422
            assert "could not determine" in str(exc_info.value.detail)

    assert path.read_bytes() == source
    assert _snapshot(stale_monolith_app) is before


class TestIncludeAwareSnapshots:
    """Committed snapshots track the full source-file set and its fingerprint."""

    def test_expand_only_config_tracks_sources_and_blocks_structured_saves(self, tmp_path: Path) -> None:
        """Expansion alone activates fingerprints and protects shared source files."""
        config_path = tmp_path / "config.yaml"
        source = "models:\n  default: {provider: ollama, id: test-model}\ndefaults:\n  tools: [!expand tools.yaml]\n"
        config_path.write_text(source, encoding="utf-8")
        shared = tmp_path / "tools.yaml"
        shared.write_text("- calculator\n", encoding="utf-8")
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={},
        )
        api_app = _make_api_app(runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
        before = _snapshot(api_app)
        assert before.source_files == frozenset({config_path.resolve(), shared.resolve()})
        assert config_lifecycle.config_uses_includes(_request_for(api_app)) is True

        shared.write_text("- file\n", encoding="utf-8")
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
        after = _snapshot(api_app)
        assert after.source_fingerprint != before.source_fingerprint
        assert after.runtime_config.authored_model_dump()["defaults"]["tools"] == ["file"]

        with pytest.raises(HTTPException) as exc_info:
            save_config(
                _request_for(api_app),
                copy.deepcopy(after.runtime_config.authored_model_dump()),
            )

        assert exc_info.value.status_code == 409
        assert config_path.read_text(encoding="utf-8") == source
        assert shared.read_text(encoding="utf-8") == "- file\n"

    def test_load_publishes_source_files_and_multi_file_fingerprint(self, split_app: FastAPI) -> None:
        """Loading an include-based config records every source file in the snapshot."""
        snapshot = _snapshot(split_app)
        config_path = snapshot.runtime_paths.config_path
        assert snapshot.source_files is not None
        assert {path.name for path in snapshot.source_files} == {"config.yaml", "models.yaml", "test_agent.yaml"}
        assert snapshot.source_fingerprint is not None
        assert snapshot.source_fingerprint != hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert snapshot.runtime_config.authored_model_dump()["agents"]["test_agent"]["display_name"] == "Test Agent"

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
        assert config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
        snapshot = _snapshot(api_app)
        assert snapshot.source_fingerprint == hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert snapshot.source_files == frozenset({config_path.resolve()})

    def test_api_load_rejects_access_migration_with_include(self, tmp_path: Path) -> None:
        """The API loader must leave every source untouched when legacy access uses includes."""
        config_path = tmp_path / "config.yaml"
        authorization_path = tmp_path / "authorization.yaml"
        config_source = "authorization: !include authorization.yaml\n"
        authorization_source = "global_users:\n  - '@owner:example.com'\n"
        config_path.write_text(config_source, encoding="utf-8")
        authorization_path.write_text(authorization_source, encoding="utf-8")
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "storage",
            process_env={},
        )
        api_app = _make_api_app(runtime_paths)

        assert not config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded

        result = _snapshot(api_app).config_load_result
        assert result is not None
        assert result.error_status_code == 422
        assert "does not support !include" in str(result.error_detail)
        assert config_path.read_text(encoding="utf-8") == config_source
        assert authorization_path.read_text(encoding="utf-8") == authorization_source
        assert not config_path.with_name(f"{config_path.name}.pre-membership-access").exists()

    def test_fingerprint_changes_when_included_file_changes(self, split_app: FastAPI) -> None:
        """Editing only an included file changes the fingerprint and bumps the generation."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        agent_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agent_file.write_text(TEST_AGENT_SOURCE.replace("A test agent", "An edited agent"), encoding="utf-8")

        assert config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded

        after = _snapshot(split_app)
        assert after.source_fingerprint != snapshot.source_fingerprint
        assert after.generation == snapshot.generation + 1
        assert after.runtime_config.authored_model_dump()["agents"]["test_agent"]["role"] == "An edited agent"

    def test_failed_reload_keeps_watching_the_broken_included_file(self, split_app: FastAPI) -> None:
        """A reload broken by an included file keeps that file in the watched source set."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        agent_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agent_file.write_text("test_agent: [not a mapping\n", encoding="utf-8")

        assert not config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded

        after = _snapshot(split_app)
        assert after.config_load_result is not None
        assert after.config_load_result.success is False
        assert after.source_files == snapshot.source_files

    def test_failed_validation_reload_unions_in_the_new_source_set(self, split_app: FastAPI) -> None:
        """A parsed-but-invalid reload watches newly added include files plus the last good set."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        new_include = runtime_paths.config_path.parent / "agents" / "bad_agent.yaml"
        new_include.write_text("bad_agent: [not, a, mapping]\n", encoding="utf-8")

        assert not config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded

        after = _snapshot(split_app)
        assert after.config_load_result is not None
        assert after.config_load_result.success is False
        assert snapshot.source_files is not None
        assert after.source_files is not None
        assert new_include.resolve() in after.source_files
        assert snapshot.source_files <= after.source_files

    def test_parse_failure_in_new_include_file_is_still_watched(self, split_app: FastAPI) -> None:
        """A syntax error inside a newly referenced include file joins the watched set."""
        snapshot = _snapshot(split_app)
        runtime_paths = snapshot.runtime_paths
        top = runtime_paths.config_path
        new_include = top.parent / "defaults.yaml"
        new_include.write_text("markdown: [unclosed\n", encoding="utf-8")
        top.write_text(
            SPLIT_TOP_SOURCE.replace("defaults:\n  markdown: true\n", "defaults: !include defaults.yaml\n"),
            encoding="utf-8",
        )

        assert not config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded

        after = _snapshot(split_app)
        assert after.config_load_result is not None
        assert after.config_load_result.success is False
        assert snapshot.source_files is not None
        assert after.source_files is not None
        assert new_include.resolve() in after.source_files
        assert snapshot.source_files <= after.source_files
        # A later successful load shrinks the union back to the real set.
        new_include.write_text("markdown: false\n", encoding="utf-8")
        assert config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded
        final = _snapshot(split_app)
        assert final.source_files is not None
        assert new_include.resolve() in final.source_files
        assert final.runtime_config.authored_model_dump()["defaults"]["markdown"] is False

    def test_successful_load_logs_the_source_file_count(
        self,
        split_runtime_paths: constants.RuntimePaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The API's loaded_agent_configuration log line reports how many files resolved."""
        events: list[tuple[str, dict[str, object]]] = []

        class _RecordingLogger:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, kwargs))

        monkeypatch.setattr(load_attempt, "logger", _RecordingLogger())
        api_app = _make_api_app(split_runtime_paths)
        assert config_lifecycle.load_config_into_app(split_runtime_paths, api_app).loaded

        counts = [kwargs["source_file_count"] for event, kwargs in events if event == "loaded_agent_configuration"]
        assert counts == [3]

    def test_load_endpoint_reports_uses_includes_header(
        self,
        split_runtime_paths: constants.RuntimePaths,
    ) -> None:
        """/api/config/load exposes the includes flag so clients can warn before a save."""
        main.initialize_api_app(main.app, split_runtime_paths)
        assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app).loaded
        client = TestClient(main.app, base_url="http://localhost")

        response = client.post("/api/config/load")

        assert response.status_code == 200
        assert response.headers[config_lifecycle.CONFIG_USES_INCLUDES_HEADER] == "true"


class TestStructuredWritesRejected:
    """Structured saves must not silently flatten a split config."""

    def test_dashboard_structured_save_is_rejected_with_409(self, split_app: FastAPI) -> None:
        """A structured save returns a coded 409 and leaves every source file untouched."""
        runtime_paths = _snapshot(split_app).runtime_paths
        top_before = runtime_paths.config_path.read_text(encoding="utf-8")

        def _mutate(config: dict[str, Any]) -> None:
            config["agents"]["test_agent"]["role"] = "Flattened"

        with pytest.raises(HTTPException) as exc_info:
            save_edited_config(
                _request_for(split_app),
                _mutate,
            )

        assert exc_info.value.status_code == 409
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["code"] == "config_composed_from_includes"
        assert "!include" in detail["message"]
        assert runtime_paths.config_path.read_text(encoding="utf-8") == top_before

    def test_structured_save_rejected_even_when_an_included_file_is_broken(self, split_app: FastAPI) -> None:
        """A broken include file must not let a structured save flatten the split config."""
        runtime_paths = _snapshot(split_app).runtime_paths
        agents_file = runtime_paths.config_path.parent / "agents" / "test_agent.yaml"
        agents_file.write_text("test_agent: [broken\n", encoding="utf-8")

        def _mutate(config: dict[str, Any]) -> None:
            config["defaults"]["markdown"] = False

        with pytest.raises(HTTPException) as exc_info:
            save_edited_config(
                _request_for(split_app),
                _mutate,
            )

        assert exc_info.value.status_code == 409
        assert runtime_paths.config_path.read_text(encoding="utf-8") == SPLIT_TOP_SOURCE

    def test_tool_commit_is_rejected(self, split_app: FastAPI) -> None:
        """A tool commit raises the include-aware config error."""
        snapshot = _snapshot(split_app)
        payload = copy.deepcopy(snapshot.runtime_config.authored_model_dump())
        top_before = snapshot.runtime_paths.config_path.read_text(encoding="utf-8")

        with pytest.raises(ConfigComposedFromIncludesError, match="!include"):
            commit_tool_change(payload, snapshot.runtime_paths)

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

        generation = save_raw_config(
            _request_for(split_app),
            new_source,
        )

        snapshot = _snapshot(split_app)
        assert generation == snapshot.generation
        assert runtime_paths.config_path.read_text(encoding="utf-8") == new_source
        assert agents_file.read_text(encoding="utf-8") == agents_before
        assert snapshot.runtime_config.authored_model_dump()["defaults"]["markdown"] is False
        assert snapshot.source_files is not None
        assert runtime_paths.config_path.resolve() in snapshot.source_files
        assert agents_file.resolve() in snapshot.source_files

    def test_raw_replace_publishes_include_aware_fingerprint(self, split_app: FastAPI) -> None:
        """The follow-up watcher reload after a raw save of a split config is a no-op."""
        runtime_paths = _snapshot(split_app).runtime_paths
        new_source = SPLIT_TOP_SOURCE.replace("markdown: true", "markdown: false")

        save_raw_config(
            _request_for(split_app),
            new_source,
        )

        generation = _snapshot(split_app).generation
        assert config_lifecycle.load_config_into_app(runtime_paths, split_app).loaded
        assert _snapshot(split_app).generation == generation

    def test_raw_replace_rejects_a_self_include(self, split_app: FastAPI) -> None:
        """Raw source including the live config file itself is a cycle at validation time."""
        top_before = _snapshot(split_app).runtime_paths.config_path.read_text(encoding="utf-8")

        with pytest.raises(HTTPException) as exc_info:
            save_raw_config(
                _request_for(split_app),
                "agents: !include config.yaml\n",
            )

        assert exc_info.value.status_code == 422
        assert _snapshot(split_app).runtime_paths.config_path.read_text(encoding="utf-8") == top_before

    def test_raw_replace_rejects_access_migration_with_include(self, split_app: FastAPI) -> None:
        """The raw editor must reject rather than persist a legacy split configuration."""
        snapshot = _snapshot(split_app)
        authorization_path = snapshot.runtime_paths.config_path.parent / "authorization.yaml"
        authorization_path.write_text("global_users:\n  - '@owner:example.com'\n", encoding="utf-8")
        top_before = snapshot.runtime_paths.config_path.read_text(encoding="utf-8")
        source = f"{top_before}authorization: !include authorization.yaml\n"

        with pytest.raises(HTTPException) as exc_info:
            save_raw_config(
                _request_for(split_app),
                source,
            )

        assert exc_info.value.status_code == 422
        assert "does not support !include" in str(exc_info.value.detail)
        assert snapshot.runtime_paths.config_path.read_text(encoding="utf-8") == top_before
        assert not snapshot.runtime_paths.config_path.with_name(
            f"{snapshot.runtime_paths.config_path.name}.pre-membership-access",
        ).exists()

    def test_raw_replace_with_monolith_reenables_structured_saves(self, split_app: FastAPI) -> None:
        """Collapsing back to one file through the raw editor re-enables structured saves."""
        monolith_source = yaml.dump(copy.deepcopy(_snapshot(split_app).runtime_config.authored_model_dump()))

        save_raw_config(
            _request_for(split_app),
            monolith_source,
        )

        assert config_lifecycle.config_uses_includes(_request_for(split_app)) is False

        def _mutate(config: dict[str, Any]) -> None:
            config["agents"]["test_agent"]["role"] = "Structured save works again"

        save_edited_config(
            _request_for(split_app),
            _mutate,
        )
        assert (
            _snapshot(split_app).runtime_config.authored_model_dump()["agents"]["test_agent"]["role"]
            == "Structured save works again"
        )


@pytest.mark.asyncio
async def test_watch_config_reloads_when_an_included_file_changes(
    monkeypatch: pytest.MonkeyPatch,
    split_runtime_paths: constants.RuntimePaths,
) -> None:
    """The API config watcher triggers a reload for edits to included files."""
    main.initialize_api_app(main.app, split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app).loaded

    loop = asyncio.get_running_loop()
    loaded_paths: list[Path] = []
    load_event = asyncio.Event()
    stop_event = asyncio.Event()

    # The watcher loads in a worker thread, so the load reaches the loop through call_soon_threadsafe.
    def _record_load(runtime_paths: constants.RuntimePaths, _app: FastAPI, *, reconcile_knowledge: bool) -> ConfigLoad:
        assert reconcile_knowledge
        loaded_paths.append(runtime_paths.config_path)
        loop.call_soon_threadsafe(load_event.set)
        return failed_config_load(OSError("this test only records the load"))

    monkeypatch.setattr(config_lifecycle, "load_config_into_app", _record_load)
    scans = record_watcher_scans(monkeypatch)
    agent_file = split_runtime_paths.config_path.parent / "agents" / "test_agent.yaml"

    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))
    await scans.wait_for_scan_of(agent_file)
    future_time = time.time() + 5
    agent_file.write_text(TEST_AGENT_SOURCE.replace("A test agent", "Edited via include"), encoding="utf-8")
    os.utime(agent_file, (future_time, future_time))

    await asyncio.wait_for(load_event.wait(), timeout=2)
    stop_event.set()
    await watch_task

    assert loaded_paths == [split_runtime_paths.config_path]


@pytest.mark.asyncio
async def test_watch_config_coalesces_a_multi_file_burst_into_one_reload(
    monkeypatch: pytest.MonkeyPatch,
    split_runtime_paths: constants.RuntimePaths,
) -> None:
    """Edits to several source files within one update burst trigger exactly one reload."""
    main.initialize_api_app(main.app, split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app).loaded

    loop = asyncio.get_running_loop()
    loaded_paths: list[Path] = []
    load_event = asyncio.Event()
    stop_event = asyncio.Event()

    # The watcher loads in a worker thread, so the load reaches the loop through call_soon_threadsafe.
    def _record_load(runtime_paths: constants.RuntimePaths, _app: FastAPI, *, reconcile_knowledge: bool) -> ConfigLoad:
        assert reconcile_knowledge
        loaded_paths.append(runtime_paths.config_path)
        loop.call_soon_threadsafe(load_event.set)
        return failed_config_load(OSError("this test only records the load"))

    monkeypatch.setattr(config_lifecycle, "load_config_into_app", _record_load)
    scans = record_watcher_scans(monkeypatch)
    config_dir = split_runtime_paths.config_path.parent
    burst_paths = (
        split_runtime_paths.config_path,
        config_dir / "models.yaml",
        config_dir / "agents" / "test_agent.yaml",
    )

    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))
    await scans.wait_for_scan_of(*burst_paths)
    future_time = time.time() + 5
    for path in burst_paths:
        path.write_text(path.read_text(encoding="utf-8") + "# touched\n", encoding="utf-8")
        os.utime(path, (future_time, future_time))
    await asyncio.wait_for(load_event.wait(), timeout=2)
    await scans.wait_for_next_scan()

    stop_event.set()
    await watch_task

    assert loaded_paths == [split_runtime_paths.config_path]


async def _wait_for_snapshot(
    api_app: FastAPI,
    predicate: Callable[[config_lifecycle.ApiSnapshot], bool],
    timeout_seconds: float = 5.0,
) -> config_lifecycle.ApiSnapshot:
    """Poll the app's published snapshot until ``predicate`` holds."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        snapshot = _snapshot(api_app)
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.02)
    msg = "snapshot condition not met before timeout"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_watch_config_recovers_after_fixing_a_broken_new_include(
    monkeypatch: pytest.MonkeyPatch,
    split_runtime_paths: constants.RuntimePaths,
) -> None:
    """Fixing only a broken newly referenced include file triggers a reload that goes live."""
    main.initialize_api_app(main.app, split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app).loaded
    stop_event = asyncio.Event()
    scans = record_watcher_scans(monkeypatch)
    top = split_runtime_paths.config_path
    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))
    await scans.wait_for_scan_of(top)

    new_include = top.parent / "defaults.yaml"
    future_time = time.time() + 5
    new_include.write_text("markdown: [unclosed\n", encoding="utf-8")
    top.write_text(
        SPLIT_TOP_SOURCE.replace("defaults:\n  markdown: true\n", "defaults: !include defaults.yaml\n"),
        encoding="utf-8",
    )
    os.utime(new_include, (future_time, future_time))
    os.utime(top, (future_time, future_time))

    failed = await _wait_for_snapshot(
        main.app,
        lambda snapshot: snapshot.config_load_result is not None and not snapshot.config_load_result.success,
    )
    # The old config stays live and the broken new include file is watched.
    assert failed.runtime_config.authored_model_dump()["agents"]["test_agent"]["display_name"] == "Test Agent"
    assert failed.source_files is not None
    assert new_include.resolve() in failed.source_files
    # Let the watcher baseline the newly watched file before fixing it.
    await scans.wait_for_scan_of(new_include)

    # Fix ONLY the new include file; the next scans reload and go live.
    future_time += 5
    new_include.write_text("markdown: false\n", encoding="utf-8")
    os.utime(new_include, (future_time, future_time))

    recovered = await _wait_for_snapshot(
        main.app,
        lambda snapshot: snapshot.config_load_result is not None and snapshot.config_load_result.success,
    )
    assert recovered.runtime_config.authored_model_dump()["defaults"]["markdown"] is False

    stop_event.set()
    await watch_task


@pytest.mark.asyncio
async def test_watched_config_mtimes_retries_when_snapshot_changes_during_scan(
    monkeypatch: pytest.MonkeyPatch,
    split_app: FastAPI,
    tmp_path: Path,
) -> None:
    """A completed scan must belong to the snapshot returned with it."""
    replacement_config_path = _write_split_config(tmp_path / "replacement")
    replacement_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=replacement_config_path,
        storage_path=tmp_path / "replacement-storage",
        process_env={},
    )
    scanned_paths: list[frozenset[Path]] = []

    async def fake_to_thread(function: Callable[..., object], paths: frozenset[Path]) -> object:
        scanned_paths.append(paths)
        if len(scanned_paths) == 1:
            main.initialize_api_app(split_app, replacement_runtime_paths)
        return function(paths)

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    runtime_paths, mtimes = await main._watched_config_mtimes(split_app)

    assert runtime_paths == replacement_runtime_paths
    assert set(mtimes) == {replacement_config_path}
    assert len(scanned_paths) == 2


@pytest.mark.asyncio
async def test_watched_config_mtimes_retries_when_a_load_replaces_sources(
    monkeypatch: pytest.MonkeyPatch,
    split_app: FastAPI,
) -> None:
    """A source-set replacement must invalidate an in-flight scan of the previous source set."""
    initial_snapshot = _snapshot(split_app)
    runtime_config = initial_snapshot.runtime_config
    assert runtime_config is not None
    flattened_source = yaml.safe_dump(runtime_config.authored_model_dump(), sort_keys=True)
    scanned_paths: list[frozenset[Path]] = []

    async def fake_to_thread(function: Callable[..., object], paths: frozenset[Path]) -> object:
        scanned_paths.append(paths)
        if len(scanned_paths) == 1:
            initial_snapshot.runtime_paths.config_path.write_text(flattened_source, encoding="utf-8")
            assert config_lifecycle.load_config_into_app(initial_snapshot.runtime_paths, split_app).loaded
            replacement_snapshot = _snapshot(split_app)
            assert replacement_snapshot is not initial_snapshot
            assert replacement_snapshot.source_files == frozenset(
                {initial_snapshot.runtime_paths.config_path.resolve()},
            )
        return function(paths)

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    runtime_paths, mtimes = await main._watched_config_mtimes(split_app)

    assert runtime_paths == initial_snapshot.runtime_paths
    assert set(mtimes) == {initial_snapshot.runtime_paths.config_path.resolve()}
    assert len(scanned_paths) == 2


@pytest.mark.asyncio
async def test_api_config_watcher_scans_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    split_runtime_paths: constants.RuntimePaths,
) -> None:
    """The API watcher stats every config source each poll, so it must offload the scan."""
    main.initialize_api_app(main.app, split_runtime_paths)
    assert config_lifecycle.load_config_into_app(split_runtime_paths, main.app).loaded

    offloaded: list[str] = []
    two_scans_offloaded = asyncio.Event()
    stop_event = asyncio.Event()

    async def fake_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        offloaded.append(getattr(function, "__name__", repr(function)))
        if offloaded.count("paths_mtime_snapshot") == 2:
            two_scans_offloaded.set()
        return function(*args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))
    await asyncio.wait_for(two_scans_offloaded.wait(), timeout=5)
    stop_event.set()
    await asyncio.wait_for(watch_task, timeout=2)

    assert offloaded.count("paths_mtime_snapshot") >= 2
