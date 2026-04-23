"""Tests for OpenClaw-compatible skills with Agno integration."""

from __future__ import annotations

import json
import os
import platform
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

import mindroom.tool_system.skills as skills_module
import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, ToolRuntimeContext
from mindroom.tool_system.skills import build_agent_skills
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import make_conversation_cache_mock, make_event_cache_mock

if TYPE_CHECKING:
    from pathlib import Path

    from agno.skills import Skills


def _runtime_paths(storage_path: Path, *, config_path: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=config_path or storage_path / "config.yaml",
        storage_path=storage_path,
    )


def _write_skill(
    tmp_path: Path,
    name: str,
    description: str,
    metadata: str | None = None,
    extra_frontmatter: list[str] | None = None,
) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    lines = ["---", f"name: {name}", f"description: {description}"]
    if metadata is not None:
        lines.append(f"metadata: '{metadata}'")
    if extra_frontmatter:
        lines.extend(extra_frontmatter)
    lines.append("---")
    lines.append("")
    lines.append("# Body")

    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("\n".join(lines), encoding="utf-8")
    return skill_path


def _write_skill_script(skill_dir: Path, name: str, content: str) -> None:
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / name).write_text(content, encoding="utf-8")


def _base_config(skills: list[str]) -> Config:
    return Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="",
                tools=["file"],
                skills=skills,
            ),
        },
    )


def _base_config_with_omitted_skills() -> Config:
    return Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="",
                tools=["file"],
            ),
        },
    )


def _skill_names(skills: Skills | None) -> list[str]:
    return skills.get_skill_names() if skills is not None else []


def _get_skill_script(skills: Skills, skill_name: str, script_path: str, *, execute: bool) -> dict[str, object]:
    script_tool = next(tool for tool in skills.get_tools() if tool.name == "get_skill_script")
    assert script_tool.entrypoint is not None
    return json.loads(script_tool.entrypoint(skill_name, script_path, execute=execute))


def test_bundled_mindroom_docs_skill_is_discoverable() -> None:
    """Ensure the bundled mindroom-docs skill is discoverable."""
    listing = skills_module.resolve_skill_listing(
        "mindroom-docs",
        roots=[skills_module._get_bundled_skills_dir()],
    )
    assert listing is not None
    assert listing.origin == "bundled"
    assert (listing.path.parent / "references" / "reference-index.md").exists()


def test_get_bundled_skills_dir_uses_package_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve bundled skills from package data when repo checkout path is unavailable."""
    package_dir = tmp_path / "mindroom" / "_bundled_skills"
    package_dir.mkdir(parents=True)

    monkeypatch.setattr(skills_module, "_BUNDLED_SKILLS_DEV_DIR", tmp_path / "missing-repo-skills")
    monkeypatch.setattr(skills_module, "_BUNDLED_SKILLS_PACKAGE_DIR", package_dir)

    assert skills_module._get_bundled_skills_dir() == package_dir


def test_parse_skill_with_json5_metadata(tmp_path: Path) -> None:
    """Parse JSON5 metadata from SKILL.md frontmatter."""
    metadata = "{openclaw:{always:true,},}"
    _write_skill(tmp_path, "alpha", "Alpha skill", metadata)

    config = _base_config(["alpha"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )

    assert skills is not None
    skill = skills.get_skill("alpha")
    assert skill is not None
    assert skill.metadata["openclaw"]["always"] is True


def test_skill_eligibility_env_and_config(tmp_path: Path) -> None:
    """Gate skills on env vars and config path truthiness."""
    metadata = '{openclaw:{requires:{env:["TEST_ENV"], config:["agents.code.tools"]}}}'
    _write_skill(tmp_path, "envconfig", "Requires env and config", metadata)

    config = _base_config(["envconfig"])
    eligible = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={"TEST_ENV": "1"},
        credential_keys=set(),
    )
    assert _skill_names(eligible) == ["envconfig"]

    ineligible = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(ineligible) == []

    eligible_with_credentials = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys={"TEST_ENV"},
    )
    assert _skill_names(eligible_with_credentials) == ["envconfig"]


def test_skill_eligibility_requires_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate skills on required binaries."""
    metadata = '{openclaw:{requires:{bins:["git","make"]}}}'
    _write_skill(tmp_path, "bins", "Requires bins", metadata)

    config = _base_config(["bins"])

    def only_git(name: str) -> str | None:
        return "/bin/git" if name == "git" else None

    monkeypatch.setattr(skills_module.shutil, "which", only_git)
    missing = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(missing) == []

    monkeypatch.setattr(skills_module.shutil, "which", lambda name: f"/bin/{name}")
    available = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(available) == ["bins"]


def test_skill_eligibility_any_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow skills when any binary requirement is satisfied."""
    metadata = '{openclaw:{requires:{anyBins:["rg","fd"]}}}'
    _write_skill(tmp_path, "anybins", "Any bins", metadata)

    config = _base_config(["anybins"])

    def only_fd(name: str) -> str | None:
        return "/bin/fd" if name == "fd" else None

    monkeypatch.setattr(skills_module.shutil, "which", only_fd)
    eligible = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(eligible) == ["anybins"]

    monkeypatch.setattr(skills_module.shutil, "which", lambda _name: None)
    ineligible = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(ineligible) == []


def test_skill_eligibility_os_mismatch(tmp_path: Path) -> None:
    """Exclude skills when OS requirements do not match."""
    current = platform.system().lower()
    other = "linux" if current == "windows" else "windows"

    metadata = f'{{openclaw:{{os:["{other}"]}}}}'
    _write_skill(tmp_path, "oscheck", "OS restricted", metadata)

    config = _base_config(["oscheck"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == []


def test_skill_eligibility_always_overrides_requirements(tmp_path: Path) -> None:
    """Allow always-eligible skills regardless of missing requirements."""
    metadata = '{openclaw:{always:true, requires:{env:["MISSING_ENV"]}}}'
    _write_skill(tmp_path, "always", "Always eligible", metadata)

    config = _base_config(["always"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["always"]


def test_skill_eligibility_os_mismatch_wins_over_always(tmp_path: Path) -> None:
    """Exclude OS-mismatched skills even when always is true."""
    current = platform.system().lower()
    other = "linux" if current == "windows" else "windows"

    metadata = f'{{openclaw:{{always:true, os:["{other}"]}}}}'
    _write_skill(tmp_path, "always-wrong-os", "Always wrong OS", metadata)

    config = _base_config(["always-wrong-os"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == []


def test_get_agent_skills_ordering(tmp_path: Path) -> None:
    """Preserve agent skill ordering when filtering."""
    _write_skill(tmp_path, "alpha", "Alpha skill")
    _write_skill(tmp_path, "beta", "Beta skill")

    config = _base_config(["beta", "alpha"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )

    assert _skill_names(skills) == ["beta", "alpha"]


def test_skill_cache_refreshes_on_change(tmp_path: Path) -> None:
    """Reload cached skills when SKILL.md changes."""
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v1")

    config = _base_config(["alpha"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None
    assert skills.get_skill("alpha").description == "Alpha v1"

    old_mtime = skill_path.stat().st_mtime_ns
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v2")
    os.utime(skill_path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))

    refreshed = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert refreshed is not None
    assert refreshed.get_skill("alpha").description == "Alpha v2"


def test_live_skill_dispatch_context_rejects_mismatched_execution_identity() -> None:
    """Live dispatch contracts should reject identities that do not match the runtime context."""
    config = _base_config(["dispatch"])
    runtime_paths = resolve_runtime_paths()
    runtime_context = ToolRuntimeContext(
        agent_name="code",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="@alice:example.org",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
    )

    with pytest.raises(ValueError, match="must match the provided tool runtime context"):
        LiveToolDispatchContext(
            runtime_context=runtime_context,
            execution_identity=ToolExecutionIdentity(
                channel="matrix",
                agent_name="other-agent",
                requester_id="@bob:example.org",
                room_id="!other:example.org",
                thread_id="$other-thread",
                resolved_thread_id="$other-thread",
                session_id="!other:example.org:$other-thread",
            ),
        )


def test_workspace_skills_dir_discovered(tmp_path: Path) -> None:
    """Skills in the agent workspace skills/ dir should be discovered."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "ws-skill", "Workspace skill")

    config = _base_config(["ws-skill"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(storage),
        skill_roots=[tmp_path / "empty"],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["ws-skill"]


def test_empty_allowlist_autoloads_workspace_skills(tmp_path: Path) -> None:
    """Load workspace skills even when the configured allowlist is empty."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "auto", "Auto-loaded workspace skill")

    config = _base_config([])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["auto"]


def test_omitted_allowlist_autoloads_workspace_skills(tmp_path: Path) -> None:
    """Load workspace skills when skills is omitted from agent config."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "auto", "Auto-loaded workspace skill")

    skills = build_agent_skills(
        "code",
        _base_config_with_omitted_skills(),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["auto"]


def test_empty_allowlist_does_not_load_global_skills(tmp_path: Path) -> None:
    """Keep bundled, plugin, and user skills behind the configured allowlist."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    _write_skill(global_root, "global", "Configured global skill")

    skills = build_agent_skills(
        "code",
        _base_config([]),
        _runtime_paths(tmp_path / "storage"),
        skill_roots=[global_root],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == []


def test_workspace_skills_override_default_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace skills should override same-named default-root skills."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    _write_skill(global_root, "alpha", "Default alpha")
    monkeypatch.setattr(skills_module, "_get_default_skill_roots", lambda: [global_root])

    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "alpha", "Workspace alpha")

    skills = build_agent_skills(
        "code",
        _base_config(["alpha"]),
        _runtime_paths(storage),
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None
    assert _skill_names(skills) == ["alpha"]
    assert skills.get_skill("alpha").description == "Workspace alpha"


def test_malformed_workspace_skill_is_skipped(tmp_path: Path) -> None:
    """Skip malformed workspace skills without rejecting other workspace skills."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "bad", "Bad metadata", "{openclaw:")
    _write_skill(workspace_skills, "good", "Good metadata")

    skills = build_agent_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["good"]


def test_workspace_skill_script_read_allowed_but_execute_blocked(tmp_path: Path) -> None:
    """Workspace skill scripts can be read but not executed through skill tools."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    skill_path = _write_skill(workspace_skills, "scripted", "Scripted workspace skill")
    _write_skill_script(skill_path.parent, "hello.sh", "#!/bin/sh\necho workspace\n")

    skills = build_agent_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None

    read_result = _get_skill_script(skills, "scripted", "hello.sh", execute=False)
    assert read_result["content"] == "#!/bin/sh\necho workspace\n"

    execute_result = _get_skill_script(skills, "scripted", "hello.sh", execute=True)
    assert execute_result["error"] == "Workspace skill scripts cannot be executed through get_skill_script"


def test_symlinked_workspace_skill_script_execute_blocked(tmp_path: Path) -> None:
    """Block workspace script execution even when the skill directory is a symlink."""
    storage = tmp_path / "storage"
    outside_root = tmp_path / "outside"
    outside_skill_path = _write_skill(outside_root, "linked", "Linked workspace skill")
    _write_skill_script(outside_skill_path.parent, "hello.sh", "#!/bin/sh\necho bypass\n")

    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    (workspace_skills / "linked").symlink_to(outside_skill_path.parent, target_is_directory=True)

    skills = build_agent_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None

    execute_result = _get_skill_script(skills, "linked", "hello.sh", execute=True)
    assert execute_result["error"] == "Workspace skill scripts cannot be executed through get_skill_script"


def test_non_workspace_skill_script_execute_unchanged(tmp_path: Path) -> None:
    """Configured non-workspace skill scripts keep Agno execute behavior."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    skill_path = _write_skill(global_root, "scripted", "Scripted global skill")
    _write_skill_script(skill_path.parent, "hello.sh", "#!/bin/sh\necho global\n")

    skills = build_agent_skills(
        "code",
        _base_config(["scripted"]),
        _runtime_paths(tmp_path / "storage"),
        skill_roots=[global_root],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None

    execute_result = _get_skill_script(skills, "scripted", "hello.sh", execute=True)
    assert execute_result["stdout"] == "global\n"
    assert execute_result["returncode"] == 0


def test_workspace_skills_reload_after_dir_created_late(tmp_path: Path) -> None:
    """Reload should discover workspace skills created after agent startup."""
    storage = tmp_path / "storage"
    config = _base_config(["later"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(storage),
        skill_roots=[tmp_path / "empty"],
        env_vars={},
        credential_keys=set(),
    )

    assert skills is not None
    assert _skill_names(skills) == []

    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "later", "Later workspace skill")

    skills.reload()

    assert _skill_names(skills) == ["later"]


def test_workspace_skills_do_not_override_explicit_roots(tmp_path: Path) -> None:
    """Explicit skill roots should win over workspace duplicates."""
    explicit_root = tmp_path / "explicit"
    explicit_root.mkdir()
    _write_skill(explicit_root, "alpha", "Explicit alpha")

    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "alpha", "Workspace alpha")

    config = _base_config(["alpha"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(storage),
        skill_roots=[explicit_root],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None
    assert _skill_names(skills) == ["alpha"]
    assert skills.get_skill("alpha").description == "Explicit alpha"


def test_skill_listings_use_name_fallback_for_missing_description(tmp_path: Path) -> None:
    """Skills missing description should still appear in listings."""
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: alpha\n---\n\n# Body\n", encoding="utf-8")

    listings = skills_module.list_skill_listings([tmp_path])

    assert [(listing.name, listing.description) for listing in listings] == [("alpha", "alpha")]
    listing = skills_module.resolve_skill_listing("alpha", [tmp_path])
    assert listing is not None
    assert listing.description == "alpha"


def test_skill_with_no_frontmatter_uses_name_fallback_across_discovery_paths(tmp_path: Path) -> None:
    """Skills without YAML frontmatter should still resolve consistently."""
    skill_dir = tmp_path / "mindroom-dev"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# MindRoom Dev\n\nJust markdown, no frontmatter.\n")

    config = _base_config(["mindroom-dev"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["mindroom-dev"]
    skill = skills.get_skill("mindroom-dev")
    assert skill.description == "mindroom-dev"

    listing = skills_module.resolve_skill_listing("mindroom-dev", [tmp_path])
    assert listing is not None
    assert listing.description == "mindroom-dev"


def test_skill_with_mindroom_prefix_loads(tmp_path: Path) -> None:
    """Skills with 'mindroom' in the name should load without issues."""
    _write_skill(tmp_path, "mindroom-helper", "MindRoom helper skill")

    config = _base_config(["mindroom-helper"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["mindroom-helper"]


def test_skill_with_empty_scripts_dir_loads(tmp_path: Path) -> None:
    """Skills with an empty scripts/ directory should load fine."""
    _write_skill(tmp_path, "empty-scripts", "Has empty scripts dir")
    (tmp_path / "empty-scripts" / "scripts").mkdir()

    config = _base_config(["empty-scripts"])
    skills = build_agent_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["empty-scripts"]
