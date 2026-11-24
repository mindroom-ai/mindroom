"""Personal agents start from a workspace template: the built-in starter files by default, a directory, or none."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.agents import create_agent, ensure_default_agent_workspaces
from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRosterEntry, personal_agent_username
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths

STARTER_CONTEXT_FILES = ("SOUL.md", "AGENTS.md", "USER.md", "IDENTITY.md", "TOOLS.md", "HEARTBEAT.md")
STARTER_FILES = (*STARTER_CONTEXT_FILES, "MEMORY.md")
SOUL_HEADING = "# SOUL.md - Who You Are"
AGENT_NAME = "helper_alice"

BASE: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "test-model"}},
    "personal_agents": {
        "helper": {
            "agent": {
                "display_name": "{user_display_name}'s Helper",
                "model": "default",
                "tools": [],
                "include_default_tools": False,
            },
        },
    },
}


def _entry() -> PersonalAgentRosterEntry:
    user_id = "@alice:example.org"
    return PersonalAgentRosterEntry(
        template="helper",
        user_id=user_id,
        username=personal_agent_username(user_id),
        agent_name=AGENT_NAME,
        matrix_username=None,
        user_display_name="Alice",
        source_room_id="!lobby:example.org",
    )


def _data(**template: object) -> dict[str, Any]:
    data = deepcopy(BASE)
    data["personal_agents"]["helper"].update(template)
    return data


def _onboarded_config(paths: RuntimePaths, data: dict[str, Any]) -> Config:
    _append_personal_agent_roster_entry(paths, _entry())
    config = Config.validate_with_runtime(data, paths)
    persist_entity_accounts(config, paths)
    return config


def _role(config: Config, paths: RuntimePaths) -> str:
    build: AgentBuild = create_agent(
        AGENT_NAME,
        config,
        paths,
        execution_identity=None,
        model=turn_model(config, paths, AGENT_NAME),
        plugins=config_plugins(config, paths),
        knowledge=None,
        supports_native_tool_approval=True,
    )
    role = build.prompt.role
    assert role is not None
    return role


def _workspace(paths: RuntimePaths) -> Path:
    return agent_workspace_root_path(paths.storage_root, AGENT_NAME)


def test_generated_agent_gets_the_starter_workspace_and_loads_its_context_files(tmp_path: Path) -> None:
    """Without the option, the first build copies the starter files and the prompt holds the six context files."""
    paths = test_runtime_paths(tmp_path)
    config = _onboarded_config(paths, _data())

    role = _role(config, paths)

    workspace = _workspace(paths)
    assert sorted(entry.name for entry in workspace.iterdir() if entry.name != "knowledge") == sorted(
        [*STARTER_FILES, "memory"],
    )
    assert (workspace / "memory").is_dir()
    for name in STARTER_CONTEXT_FILES:
        assert (workspace / name).read_text(encoding="utf-8").splitlines()[-1].strip() in role
    assert SOUL_HEADING in role
    # AGENTS.md points the agent at the runtime context for the active homeserver.
    assert "Active Matrix homeserver: `http://localhost:8008`" in role


def test_scaffold_never_overwrites_existing_workspace_files(tmp_path: Path) -> None:
    """An existing agent keeps its memory, notes, and edited files and gains only the files it lacks."""
    paths = test_runtime_paths(tmp_path)
    workspace = _workspace(paths)
    (workspace / "memory").mkdir(parents=True)
    kept = {
        "MEMORY.md": b"# Memory\n- Alice likes tea\n",
        "SOUL.md": b"My own soul.\n",
        "memory/2026-11-10.md": b"notes\n",
    }
    for name, payload in kept.items():
        (workspace / name).write_bytes(payload)
    config = _onboarded_config(paths, _data())

    ensure_default_agent_workspaces(config, paths)
    role = _role(config, paths)

    for name, payload in kept.items():
        assert (workspace / name).read_bytes() == payload
    assert all((workspace / name).is_file() for name in STARTER_FILES)
    assert "My own soul." in role
    assert SOUL_HEADING not in role


def test_option_off_writes_nothing_and_loads_nothing(tmp_path: Path) -> None:
    """With the option off the generated agent has no context files and its workspace stays empty."""
    paths = test_runtime_paths(tmp_path)
    config = _onboarded_config(paths, _data(workspace_template=None))

    ensure_default_agent_workspaces(config, paths)
    role = _role(config, paths)

    assert config.agents[AGENT_NAME].context_files == []
    workspace = _workspace(paths)
    assert not any(entry.name in STARTER_FILES or entry.name == "memory" for entry in workspace.iterdir())
    assert config.get_prompt("PERSONALITY_CONTEXT_SECTION_HEADING") not in role
    assert "Active Matrix homeserver" not in role


def test_template_context_files_replace_the_starter_six(tmp_path: Path) -> None:
    """A template that sets context_files loads exactly those, while the starter files are still copied."""
    paths = test_runtime_paths(tmp_path)
    data = _data()
    data["personal_agents"]["helper"]["agent"]["context_files"] = ["USER.md"]
    config = _onboarded_config(paths, data)

    role = _role(config, paths)

    assert config.agents[AGENT_NAME].context_files == ["USER.md"]
    assert "# USER.md - About Your Human" in role
    assert SOUL_HEADING not in role
    assert all((_workspace(paths) / name).is_file() for name in STARTER_FILES)


def test_template_directory_is_copied_without_overwriting(tmp_path: Path) -> None:
    """A directory template is copied like the starter files, and agent.context_files decides what loads."""
    paths = test_runtime_paths(tmp_path)
    template_dir = paths.config_path.parent / "helper_template"
    (template_dir / "notes").mkdir(parents=True)
    (template_dir / "GUIDE.md").write_text("Template guide.\n", encoding="utf-8")
    (template_dir / "notes" / "start.md").write_text("Template note.\n", encoding="utf-8")
    workspace = _workspace(paths)
    workspace.mkdir(parents=True)
    (workspace / "GUIDE.md").write_text("My guide.\n", encoding="utf-8")
    data = _data(workspace_template="helper_template")
    data["personal_agents"]["helper"]["agent"]["context_files"] = ["GUIDE.md"]
    config = _onboarded_config(paths, data)

    role = _role(config, paths)

    assert (workspace / "GUIDE.md").read_text(encoding="utf-8") == "My guide.\n"
    assert (workspace / "notes" / "start.md").read_text(encoding="utf-8") == "Template note.\n"
    assert not (workspace / "SOUL.md").exists()
    assert "My guide." in role


def test_template_directory_must_exist(tmp_path: Path) -> None:
    """A template directory that does not exist is refused when the config loads."""
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(ValidationError, match=r"workspace_template is invalid: .* does not exist"):
        Config.validate_with_runtime(_data(workspace_template="missing_dir"), paths)


def test_planted_link_is_never_followed_and_the_agent_still_builds(tmp_path: Path) -> None:
    """A link worker code planted at a starter file's name is neither written through nor read."""
    paths = test_runtime_paths(tmp_path)
    workspace = _workspace(paths)
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret\n", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to(outside)
    (workspace / "TOOLS.md").symlink_to(tmp_path / "missing.md")
    config = _onboarded_config(paths, _data())

    with capture_logs() as logs:
        role = _role(config, paths)

    assert outside.read_text(encoding="utf-8") == "outside secret\n"
    assert not (tmp_path / "missing.md").exists()
    assert "outside secret" not in role
    assert any(entry["event"] == "agent_workspace_scaffold_skipped" for entry in logs)


def test_agent_onboarded_on_a_running_runtime_gets_the_files(tmp_path: Path) -> None:
    """The reload that adds a newly onboarded user's agent scaffolds its workspace before its first turn."""
    paths = test_runtime_paths(tmp_path)
    startup_config = Config.validate_with_runtime(_data(), paths)
    ensure_default_agent_workspaces(startup_config, paths)
    assert not _workspace(paths).exists()

    reloaded = _onboarded_config(paths, _data())
    ensure_default_agent_workspaces(reloaded, paths)

    assert all((_workspace(paths) / name).is_file() for name in STARTER_FILES)
