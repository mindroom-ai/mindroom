"""Tests for OpenClaw-compatible skills with Agno integration."""

from __future__ import annotations

import json
import os
import platform
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

import mindroom.tool_system.skills as skills_module
import mindroom.tools  # noqa: F401
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.skill_learning import library
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.runtime_context import LiveToolDispatchContext
from mindroom.tool_system.skills import build_agent_skills
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.cpu_budget_helpers import cpu_budget

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


def test_bundled_open_knowledge_format_skill_is_discoverable() -> None:
    """Ensure the bundled open-knowledge-format skill is discoverable."""
    listing = skills_module.resolve_skill_listing(
        "open-knowledge-format",
        roots=[skills_module._get_bundled_skills_dir()],
    )
    assert listing is not None
    assert listing.origin == "bundled"
    assert listing.description.startswith("Use when")


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
    runtime_context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.org",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
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


def _workspace_skills(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    return storage, workspace_skills


def _load_workspace_only(tmp_path: Path, storage: Path) -> Skills | None:
    return build_agent_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )


def _get_skill_reference(skills: Skills, skill_name: str, reference_path: str) -> dict[str, object]:
    reference_tool = next(tool for tool in skills.get_tools() if tool.name == "get_skill_reference")
    assert reference_tool.entrypoint is not None
    return json.loads(reference_tool.entrypoint(skill_name, reference_path))


def test_workspace_skill_with_an_unclosed_frontmatter_fence_loads_within_a_cpu_budget(tmp_path: Path) -> None:
    """A planted SKILL.md that opens frontmatter and never closes it is parsed in linear time."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    (workspace_skills / "planted").mkdir()
    for newline_count in (32 << 10, skills_module.MAX_WORKSPACE_SKILL_FILE_BYTES - 3):
        (workspace_skills / "planted" / "SKILL.md").write_text("---" + "\n" * newline_count, encoding="utf-8")
        with cpu_budget(0.5):
            skills = _load_workspace_only(tmp_path, storage)
        assert _skill_names(skills) == ["planted"]


def test_symlinked_workspace_skill_is_never_loaded(tmp_path: Path) -> None:
    """A workspace skill folder linked to another tenant's skill is refused, not read or run."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    outside_skill_path = _write_skill(tmp_path / "other-workspace", "linked", "Victim instructions")
    _write_skill_script(outside_skill_path.parent, "hello.sh", "#!/bin/sh\necho bypass\n")
    (workspace_skills / "linked").symlink_to(outside_skill_path.parent, target_is_directory=True)
    _write_skill(workspace_skills, "own", "Own skill")

    skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == ["own"]
    assert skills is not None
    assert "error" in _get_skill_script(skills, "linked", "hello.sh", execute=True)


@pytest.mark.parametrize("layout", ["linked_skills_dir", "linked_skill_file", "fifo_skill_file", "huge_skill_file"])
def test_workspace_skill_files_that_are_not_plain_files_are_refused(tmp_path: Path, layout: str) -> None:
    """Links, FIFOs, and oversized files where a SKILL.md belongs never reach the prompt or block the primary."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    secret = tmp_path / "credentials.json"
    secret.write_text('{"api_key": "primary-only"}', encoding="utf-8")
    skill_dir = workspace_skills / "planted"
    if layout == "linked_skills_dir":
        workspace_skills.rmdir()
        victim_skills = tmp_path / "victim-skills"
        _write_skill(victim_skills, "planted", "Victim skill")
        workspace_skills.symlink_to(victim_skills, target_is_directory=True)
    else:
        skill_dir.mkdir()
        if layout == "linked_skill_file":
            (skill_dir / "SKILL.md").symlink_to(secret)
        elif layout == "fifo_skill_file":
            os.mkfifo(skill_dir / "SKILL.md")
        else:
            with (skill_dir / "SKILL.md").open("wb") as skill_file:
                skill_file.write(b"---\nname: planted\ndescription: big\n---\n")
                skill_file.truncate(65 << 20)

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == []
    if layout != "linked_skills_dir":
        assert any(str(entry.get("path", "")).endswith("planted/SKILL.md") for entry in logs)


def test_workspace_skill_references_are_read_without_following_links(tmp_path: Path) -> None:
    """Listed references are read by a no-follow walk; planted or later-swapped links are refused."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    skill_dir = _write_skill(workspace_skills, "docs", "Docs skill").parent
    references = skill_dir / "references"
    references.mkdir()
    (references / "guide.md").write_text("own guide", encoding="utf-8")
    (references / "swapped.md").write_text("own swapped", encoding="utf-8")
    secret = tmp_path / "victim.md"
    secret.write_text("victim-only note", encoding="utf-8")
    (references / "planted.md").symlink_to(secret)
    os.mkfifo(references / "pipe.md")

    skills = _load_workspace_only(tmp_path, storage)
    assert skills is not None
    skill = skills.get_skill("docs")
    assert skill is not None
    assert skill.references == ["guide.md", "swapped.md"]
    (references / "swapped.md").unlink()
    (references / "swapped.md").symlink_to(secret)

    assert _get_skill_reference(skills, "docs", "guide.md")["content"] == "own guide"
    swapped = _get_skill_reference(skills, "docs", "swapped.md")
    assert "victim-only note" not in json.dumps(swapped)
    assert "error" in swapped
    assert "error" in _get_skill_reference(skills, "docs", "planted.md")


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


def test_operator_skill_listings_parse_yaml_aliases_like_agent_loading(tmp_path: Path) -> None:
    """Operator-root skills that agents load with aliased frontmatter stay listed, unlike workspace skills."""
    skill_dir = tmp_path / "aliased"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: aliased\ndescription: Aliased skill\nmetadata:\n  defaults: &d {os: [linux]}\n  openclaw: *d\n---\n",
        encoding="utf-8",
    )

    assert [listing.name for listing in skills_module.list_skill_listings([tmp_path])] == ["aliased"]
    assert skills_module.resolve_skill_listing("aliased", [tmp_path]) is not None
    assert [skill.name for skill in skills_module._load_root_skills(tmp_path)] == ["aliased"]


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


def test_skill_tools_accept_mindroom_output_path(tmp_path: Path) -> None:
    """Skill access tools honor the shared output-path redirect like other tools."""
    skill_path = _write_skill(tmp_path / "skills", "demo", "Demo skill")
    references_dir = skill_path.parent / "references"
    references_dir.mkdir()
    (references_dir / "guide.md").write_text("# Guide", encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    skills = build_agent_skills(
        "code",
        _base_config(["demo"]),
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path / "skills"],
        env_vars={},
        credential_keys=set(),
        output_file_policy=ToolOutputFilePolicy(workspace_root=workspace_root),
    )
    assert skills is not None
    tools = {tool.name: tool for tool in skills.get_tools()}
    assert set(tools) == {"get_skill_instructions", "get_skill_reference", "get_skill_script"}

    for tool in tools.values():
        tool.process_entrypoint()
        assert "mindroom_output_path" in tool.parameters["properties"]

    instructions_tool = tools["get_skill_instructions"]
    assert instructions_tool.entrypoint is not None
    inline_result = json.loads(instructions_tool.entrypoint("demo"))
    assert inline_result["skill_name"] == "demo"

    redirected = instructions_tool.entrypoint("demo", mindroom_output_path="out/instructions.json")
    receipt = redirected["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (workspace_root / "out" / "instructions.json").exists()

    reference_tool = tools["get_skill_reference"]
    assert reference_tool.entrypoint is not None
    redirected_reference = reference_tool.entrypoint(
        "demo",
        "guide.md",
        mindroom_output_path="out/reference.txt",
    )
    assert redirected_reference["mindroom_tool_output"]["status"] == "saved_to_file"
    saved_reference = (workspace_root / "out" / "reference.txt").read_text(encoding="utf-8")
    assert "# Guide" in saved_reference


def test_skill_tools_without_policy_stay_unwrapped(tmp_path: Path) -> None:
    """Without an output-file policy skill tools keep their original entrypoints."""
    _write_skill(tmp_path / "skills", "demo", "Demo skill")

    skills = build_agent_skills(
        "code",
        _base_config(["demo"]),
        _runtime_paths(tmp_path),
        skill_roots=[tmp_path / "skills"],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None
    instructions_tool = next(tool for tool in skills.get_tools() if tool.name == "get_skill_instructions")
    assert instructions_tool.entrypoint is not None
    assert json.loads(instructions_tool.entrypoint("demo"))["skill_name"] == "demo"


def test_set_plugin_skill_roots_unchanged_keeps_skill_cache(tmp_path: Path) -> None:
    """Re-applying identical plugin skill roots must not drop cached skill loads."""
    _write_skill(tmp_path, "alpha", "Alpha v1")
    original_roots = skills_module.get_plugin_skill_roots()
    try:
        skills_module.set_plugin_skill_roots([tmp_path])
        first_load = skills_module._load_root_skills(tmp_path)
        assert [skill.name for skill in first_load] == ["alpha"]

        skills_module.set_plugin_skill_roots([tmp_path])

        assert skills_module._load_root_skills(tmp_path) is first_load
    finally:
        skills_module.set_plugin_skill_roots(original_roots)
        skills_module.clear_skill_cache()


def test_set_plugin_skill_roots_change_clears_skill_cache(tmp_path: Path) -> None:
    """Changing plugin skill roots must invalidate cached skill loads."""
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    _write_skill(root_one, "alpha", "Alpha v1")
    _write_skill(root_two, "beta", "Beta v1")
    original_roots = skills_module.get_plugin_skill_roots()
    try:
        skills_module.set_plugin_skill_roots([root_one])
        first_load = skills_module._load_root_skills(root_one)
        assert [skill.name for skill in first_load] == ["alpha"]

        skills_module.set_plugin_skill_roots([root_one, root_two])

        assert skills_module._load_root_skills(root_one) is not first_load
    finally:
        skills_module.set_plugin_skill_roots(original_roots)
        skills_module.clear_skill_cache()


def test_skill_edits_stay_visible_when_plugin_roots_are_reapplied(tmp_path: Path) -> None:
    """Snapshot validation must still catch file edits after a no-op root update."""
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v1")
    original_roots = skills_module.get_plugin_skill_roots()
    try:
        skills_module.set_plugin_skill_roots([tmp_path])
        first_load = skills_module._load_root_skills(tmp_path)
        assert first_load[0].description == "Alpha v1"

        old_mtime = skill_path.stat().st_mtime_ns
        skill_path = _write_skill(tmp_path, "alpha", "Alpha v2")
        os.utime(skill_path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
        skills_module.set_plugin_skill_roots([tmp_path])

        refreshed = skills_module._load_root_skills(tmp_path)
        assert refreshed is not first_load
        assert refreshed[0].description == "Alpha v2"
    finally:
        skills_module.set_plugin_skill_roots(original_roots)
        skills_module.clear_skill_cache()


def test_workspace_skills_above_the_count_cap_are_skipped_with_a_warning(tmp_path: Path) -> None:
    """A workspace with more skills than the cap loads the first ones and says so instead of silently dropping."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    for index in range(skills_module._MAX_WORKSPACE_SKILLS + 1):
        _write_skill(workspace_skills, f"skill-{index:04d}", "Numbered skill")

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert len(_skill_names(skills)) == skills_module._MAX_WORKSPACE_SKILLS
    assert any(
        entry["log_level"] == "warning" and entry.get("limit") == skills_module._MAX_WORKSPACE_SKILLS for entry in logs
    )


def test_workspace_skills_stay_within_a_file_cap_and_a_total_budget(tmp_path: Path) -> None:
    """One oversized SKILL.md is refused, and skills beyond the total budget are skipped, each with a warning."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills, "huge", "Too big")
    (workspace_skills / "huge" / "SKILL.md").write_text(
        "---\nname: huge\ndescription: big\n---\n" + "x" * (2 << 20),
        encoding="utf-8",
    )
    for index in range(10):
        _write_skill(workspace_skills, f"skill-{index}", "Large skill")
        with (workspace_skills / f"skill-{index}" / "SKILL.md").open("a", encoding="utf-8") as skill_file:
            skill_file.write("x" * (900 << 10))

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    names = _skill_names(skills)
    assert "huge" not in names
    assert 0 < len(names) < 10
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert any(str(entry.get("path", "")).endswith("huge/SKILL.md") for entry in warnings)
    assert any("budget" in entry["event"] for entry in warnings)


def test_workspace_skills_that_fail_to_load_still_spend_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each SKILL.md is charged before it is parsed, so planted files cannot make one build parse without bound."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    for index in range(20):
        broken = workspace_skills / f"broken-{index:02d}"
        broken.mkdir()
        (broken / "SKILL.md").write_text("---\n- not a mapping\n---\n" + "x" * (900 << 10), encoding="utf-8")
    _write_skill(workspace_skills, "valid", "Sorted after every broken skill")
    parse = skills_module._parse_skill_frontmatter
    parsed_paths: set[str] = set()

    def recording_parse(content: str, *, path: str, allow_missing: bool) -> tuple[dict[str, Any], str] | None:
        parsed_paths.add(path)
        return parse(content, path=path, allow_missing=allow_missing)

    monkeypatch.setattr(skills_module, "_parse_skill_frontmatter", recording_parse)

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == []
    assert len(parsed_paths) < 10
    assert any("budget" in entry["event"] for entry in logs if entry["log_level"] == "warning")


def test_workspace_skill_descriptions_count_toward_the_budget_and_are_capped(tmp_path: Path) -> None:
    """Descriptions reach every system prompt, so they are capped and counted with the rest of each skill."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    for index in range(20):
        _write_skill(workspace_skills, f"skill-{index:02d}", "d" * (900 << 10))

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert skills is not None
    assert len(skills.get_system_prompt_snippet()) < 1 << 20
    assert any("description" in entry["event"] for entry in logs if entry["log_level"] == "warning")


def test_workspace_skill_frontmatter_with_yaml_aliases_is_refused(tmp_path: Path) -> None:
    """A few lines of aliases describe a tree far larger than the file, so such frontmatter is refused."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills, "plain", "Plain skill")
    aliased_dir = workspace_skills / "aliased"
    aliased_dir.mkdir()
    (aliased_dir / "SKILL.md").write_text(
        "---\nname: aliased\ndescription: Aliased skill\nmetadata:\n"
        f"  a: &a [{', '.join(['x'] * 100)}]\n"
        f"  b: &b [{', '.join(['*a'] * 100)}]\n"
        f"  c: [{', '.join(['*b'] * 100)}]\n"
        "---\nbody\n",
        encoding="utf-8",
    )

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == ["plain"]
    assert any("aliases" in str(entry.get("error", "")) for entry in logs if entry["log_level"] == "warning")


def test_workspace_skill_frontmatter_nested_too_deep_is_refused(tmp_path: Path) -> None:
    """The C composer recurses once per nesting level, so deep frontmatter is refused before it overflows the stack."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills, "plain", "Plain skill")
    nested_dir = workspace_skills / "nested"
    nested_dir.mkdir()
    (nested_dir / "SKILL.md").write_text(
        f"---\nname: nested\ndescription: Nested skill\nmetadata: {'[' * 100_000}{']' * 100_000}\n---\nbody\n",
        encoding="utf-8",
    )

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == ["plain"]
    assert any("nest" in str(entry.get("error", "")) for entry in logs if entry["log_level"] == "warning")


@pytest.mark.parametrize("oversized", ["names", "scripts"])
def test_workspace_skill_names_and_listings_cannot_bloat_the_prompt(tmp_path: Path, oversized: str) -> None:
    """Names and script or reference listings reach every system prompt, so they are capped with a warning."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    if oversized == "names":
        for index in range(20):
            skill_dir = workspace_skills / f"skill-{index:02d}"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {'n' * (900 << 10)}{index}\ndescription: Named skill\n---\nbody\n",
                encoding="utf-8",
            )
    else:
        skill_dir = _write_skill(workspace_skills, "many-scripts", "Scripted skill").parent
        (skill_dir / "scripts").mkdir()
        for index in range(20_000):
            (skill_dir / "scripts" / f"script-{index:05d}-{'s' * 150}.sh").touch()

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert skills is None or len(skills.get_system_prompt_snippet()) < 1 << 20
    assert any(entry["log_level"] == "warning" for entry in logs)


_NEW_SKILL = "---\nname: many-files\ndescription: Use when testing bounded scans\n---\nBody\n"


def _plant_entries(directory: Path) -> None:
    """Fill a workspace directory the way worker code could, with twice the entries a scan may examine."""
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(2048):
        (directory / f"planted-{index:04d}").touch()


def _count_directory_scans(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many entries each descriptor-relative ``os.scandir`` or ``os.listdir`` consumes from now on."""
    scanned: list[int] = []
    real_scandir = os.scandir
    real_listdir = os.listdir

    class CountingEntries:
        def __init__(self, directory_fd: int) -> None:
            self.entries = real_scandir(directory_fd)
            self.index = len(scanned)
            scanned.append(0)

        def __enter__(self) -> CountingEntries:
            return self

        def __exit__(self, *exc_info: object) -> None:
            self.entries.close()

        def __iter__(self) -> CountingEntries:
            return self

        def __next__(self) -> os.DirEntry[str]:
            entry = next(self.entries)
            scanned[self.index] += 1
            return entry

    def counting_scandir(path: int | str = ".") -> object:
        return CountingEntries(path) if isinstance(path, int) else real_scandir(path)

    def counting_listdir(path: int | str = ".") -> list[str]:
        names = real_listdir(path)
        if isinstance(path, int):
            scanned.append(len(names))
        return names

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os, "listdir", counting_listdir)
    return scanned


@pytest.mark.parametrize("planted", ["skills", "scripts"])
def test_workspace_skill_listings_stop_scanning_planted_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planted: str,
) -> None:
    """Every agent build examines a bounded number of workspace entries however many worker code planted."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    if planted == "skills":
        _plant_entries(workspace_skills)
    else:
        _plant_entries(_write_skill(workspace_skills, "many-scripts", "Scripted skill").parent / "scripts")
    scanned = _count_directory_scans(monkeypatch)

    _load_workspace_only(tmp_path, storage)

    assert scanned
    assert max(scanned) <= 1024


@pytest.mark.parametrize("change", ["creation", "edit", "archival"])
def test_skill_learning_stops_scanning_planted_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Skill creation, edits, and archival examine a bounded number of workspace entries however many were planted."""
    _storage, workspace_skills = _workspace_skills(tmp_path)
    notes = "references/notes.md"
    if change == "edit":
        library.create_skill(workspace_skills, "many-files", _NEW_SKILL, reserved_names=frozenset(), learner=False)
        library.write_skill_file(workspace_skills, "many-files", notes, "Notes.", expected_digest=None, learner=False)
        _plant_entries(workspace_skills / "many-files")
        _plant_entries(workspace_skills / ".history" / "many-files")
    else:
        _plant_entries(workspace_skills)
    scanned = _count_directory_scans(monkeypatch)

    if change == "creation":
        library.create_skill(workspace_skills, "many-files", _NEW_SKILL, reserved_names=frozenset(), learner=False)
    elif change == "edit":
        current = library.read_skill_file(workspace_skills, "many-files", notes)
        assert current is not None
        library.write_skill_file(
            workspace_skills,
            "many-files",
            notes,
            "Newer notes.",
            expected_digest=current.digest,
            learner=False,
        )
    else:
        library.archive_unused_skills(workspace_skills, archive_after_days=30, now=datetime.now(UTC))

    assert scanned
    assert max(scanned) <= 1024


def test_workspace_skill_loads_record_usage_but_configured_skills_do_not(tmp_path: Path) -> None:
    """Loading a workspace skill or one of its files feeds the learner's inactivity clock; configured skills do not."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    skill_path = _write_skill(workspace_skills, "local", "Workspace skill")
    (skill_path.parent / "references").mkdir()
    (skill_path.parent / "references" / "notes.md").write_text("notes", encoding="utf-8")
    _write_skill(tmp_path / "global", "shared", "Configured skill")
    skills = build_agent_skills(
        "code",
        _base_config(["shared"]),
        _runtime_paths(storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
    )
    assert skills is not None
    get_instructions = next(tool for tool in skills.get_tools() if tool.name == "get_skill_instructions").entrypoint
    assert get_instructions is not None
    assert _get_skill_reference(skills, "local", "notes.md")["content"] == "notes"
    read = json.loads((workspace_skills / ".usage.json").read_text(encoding="utf-8"))["local"]["last_used_at"]
    get_instructions(skill_name="local")
    get_instructions(skill_name="shared")

    usage = json.loads((workspace_skills / ".usage.json").read_text(encoding="utf-8"))
    assert usage["local"]["last_used_at"] > read
    assert "shared" not in usage
    assert not (tmp_path / "global" / ".usage.json").exists()


def test_a_workspace_whose_skills_directory_is_one_skill_records_no_usage(tmp_path: Path) -> None:
    """Usage records live beside skill directories, so loading a skills/ that is itself one skill writes none."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills.parent, "skills", "Whole-directory skill")
    skills = _load_workspace_only(tmp_path, storage)
    assert skills is not None
    get_instructions = next(tool for tool in skills.get_tools() if tool.name == "get_skill_instructions").entrypoint
    assert get_instructions is not None
    assert "Body" in get_instructions(skill_name="skills")
    assert not (workspace_skills / ".usage.json").exists()
    assert not (workspace_skills.parent / ".usage.json").exists()


@pytest.mark.asyncio
async def test_workspace_skill_loads_on_the_event_loop_record_usage_in_a_thread(tmp_path: Path) -> None:
    """Agno calls skill tools on the event loop, so the usage write, which syncs files, must not block it."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills, "local", "Workspace skill")
    skills = _load_workspace_only(tmp_path, storage)
    assert skills is not None
    get_instructions = next(tool for tool in skills.get_tools() if tool.name == "get_skill_instructions").entrypoint
    assert get_instructions is not None
    writers: list[threading.Thread] = []
    record = skills_module.record_skill_use

    def spy(skill_path: Path) -> None:
        writers.append(threading.current_thread())
        record(skill_path)

    with patch.object(skills_module, "record_skill_use", spy):
        get_instructions(skill_name="local")
        assert await wait_for_background_tasks(5)
    assert writers
    assert threading.main_thread() not in writers
    assert "last_used_at" in json.loads((workspace_skills / ".usage.json").read_text(encoding="utf-8"))["local"]
