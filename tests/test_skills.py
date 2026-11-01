"""Tests for OpenClaw-compatible skills: loading, eligibility, workspace limits, and the skills toolkit."""

from __future__ import annotations

import json
import os
import platform
import threading
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

import mindroom.tool_system.skills as skills_module
import mindroom.tools  # noqa: F401
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.skill_learning import library
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.skills import RefusedWorkspaceSkill, Skill, SkillsToolkit, build_skills_toolkit
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.cpu_budget_helpers import cpu_budget

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path


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


def _build_skills(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    roots: Sequence[Path] | None = None,
    env: Mapping[str, str] | None = None,
    env_file: Mapping[str, str] | None = None,
    credentials: Collection[str] = (),
    **kwargs: Any,  # noqa: ANN401
) -> SkillsToolkit | None:
    """Build the toolkit with ``roots`` as the default skill roots, only ``env`` and ``env_file`` in the environment.

    ``env`` is the process environment and ``env_file`` the ``.env`` values. ``credentials`` names the keys a stored
    credential carries; without ``roots`` the default roots stay as they are. Without a ``workspace_root`` in
    ``kwargs``, the toolkit reads the agent's canonical workspace under the storage root.
    """
    runtime_paths = replace(runtime_paths, process_env=dict(env or {}), env_file_values=dict(env_file or {}))
    if credentials:
        get_runtime_credentials_manager(runtime_paths).save_credentials("skill_test", dict.fromkeys(credentials, "set"))
    options = {"workspace_root": agent_workspace_root_path(runtime_paths.storage_root, agent_name)} | kwargs
    if roots is None:
        return build_skills_toolkit(agent_name, config, runtime_paths, plugin_skill_roots=(), **options)
    with patch.object(skills_module, "default_skill_roots", return_value=list(roots)):
        return build_skills_toolkit(agent_name, config, runtime_paths, plugin_skill_roots=(), **options)


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


def _skill_names(skills: SkillsToolkit | None) -> list[str]:
    return [skill.name for skill in skills.skills] if skills is not None else []


def _skill(skills: SkillsToolkit, name: str) -> Skill:
    return {skill.name: skill for skill in skills.skills}[name]


def _call(skills: SkillsToolkit, function_name: str, *arguments: str) -> dict[str, object]:
    return json.loads(skills.functions[function_name](*arguments))


def test_bundled_mindroom_docs_skill_is_discoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the bundled mindroom-docs skill is discoverable."""
    bundled_root = skills_module._get_bundled_skills_dir()
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [bundled_root])
    listing = skills_module.resolve_skill_listing("mindroom-docs", ())
    assert listing is not None
    assert listing.origin == "bundled"
    assert (listing.path.parent / "references" / "reference-index.md").exists()


@pytest.mark.parametrize("skill_name", ["open-knowledge-format", "agent-conversations"])
def test_bundled_skill_loads_for_allowlisted_agent(tmp_path: Path, skill_name: str) -> None:
    """Ensure an agent that allowlists a bundled skill can load it."""
    skills = _build_skills(
        "code",
        _base_config([skill_name]),
        _runtime_paths(tmp_path),
        roots=[skills_module._get_bundled_skills_dir()],
    )

    assert _skill_names(skills) == [skill_name]


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
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )

    assert skills is not None
    skill = _skill(skills, "alpha")
    assert skill.metadata["openclaw"]["always"] is True


def test_skill_eligibility_env_and_config(tmp_path: Path) -> None:
    """Gate skills on env vars and config path truthiness."""
    metadata = '{openclaw:{requires:{env:["TEST_ENV"], config:["agents.code.tools"]}}}'
    _write_skill(tmp_path, "envconfig", "Requires env and config", metadata)

    config = _base_config(["envconfig"])
    eligible = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
        env={"TEST_ENV": "1"},
    )
    assert _skill_names(eligible) == ["envconfig"]

    ineligible = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(ineligible) == []

    eligible_with_credentials = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
        credentials={"TEST_ENV"},
    )
    assert _skill_names(eligible_with_credentials) == ["envconfig"]


@pytest.mark.parametrize(
    ("env", "env_file", "eligible"),
    [
        ({"TEST_ENV_FILE": "test-env-value"}, {}, True),
        ({"TEST_ENV": ""}, {"TEST_ENV": "1"}, True),
        ({"TEST_ENV": " "}, {}, False),
    ],
    ids=["file", "env-file-under-blank-export", "blank-export-only"],
)
def test_skill_env_requirement_reads_variables_as_provider_keys_are_read(
    tmp_path: Path,
    env: dict[str, str],
    env_file: dict[str, str],
    eligible: bool,
) -> None:
    """A ``<NAME>_FILE`` variable meets ``requires.env``, and a blank export is unset, so it does not hide ``.env``."""
    _write_skill(tmp_path, "envskill", "Requires env", '{openclaw:{requires:{env:["TEST_ENV"]}}}')
    (tmp_path / "test-env-value").write_text("1\n", encoding="utf-8")

    skills = _build_skills(
        "code",
        _base_config(["envskill"]),
        _runtime_paths(tmp_path),
        roots=[tmp_path],
        env=env,
        env_file=env_file,
    )

    assert _skill_names(skills) == (["envskill"] if eligible else [])


def test_skill_eligibility_requires_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate skills on required binaries."""
    metadata = '{openclaw:{requires:{bins:["git","make"]}}}'
    _write_skill(tmp_path, "bins", "Requires bins", metadata)

    config = _base_config(["bins"])

    def only_git(name: str) -> str | None:
        return "/bin/git" if name == "git" else None

    monkeypatch.setattr(skills_module.shutil, "which", only_git)
    missing = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(missing) == []

    monkeypatch.setattr(skills_module.shutil, "which", lambda name: f"/bin/{name}")
    available = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
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
    eligible = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(eligible) == ["anybins"]

    monkeypatch.setattr(skills_module.shutil, "which", lambda _name: None)
    ineligible = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(ineligible) == []


def test_skill_eligibility_os_mismatch(tmp_path: Path) -> None:
    """Exclude skills when OS requirements do not match."""
    current = platform.system().lower()
    other = "linux" if current == "windows" else "windows"

    metadata = f'{{openclaw:{{os:["{other}"]}}}}'
    _write_skill(tmp_path, "oscheck", "OS restricted", metadata)

    config = _base_config(["oscheck"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(skills) == []


def test_skill_eligibility_always_overrides_requirements(tmp_path: Path) -> None:
    """Allow always-eligible skills regardless of missing requirements."""
    metadata = '{openclaw:{always:true, requires:{env:["MISSING_ENV"]}}}'
    _write_skill(tmp_path, "always", "Always eligible", metadata)

    config = _base_config(["always"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(skills) == ["always"]


def test_skill_eligibility_os_mismatch_wins_over_always(tmp_path: Path) -> None:
    """Exclude OS-mismatched skills even when always is true."""
    current = platform.system().lower()
    other = "linux" if current == "windows" else "windows"

    metadata = f'{{openclaw:{{always:true, os:["{other}"]}}}}'
    _write_skill(tmp_path, "always-wrong-os", "Always wrong OS", metadata)

    config = _base_config(["always-wrong-os"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(skills) == []


def test_get_agent_skills_ordering(tmp_path: Path) -> None:
    """Preserve agent skill ordering when filtering."""
    _write_skill(tmp_path, "alpha", "Alpha skill")
    _write_skill(tmp_path, "beta", "Beta skill")

    config = _base_config(["beta", "alpha"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )

    assert _skill_names(skills) == ["beta", "alpha"]


def test_skill_cache_refreshes_on_change(tmp_path: Path) -> None:
    """Reload cached skills when SKILL.md changes."""
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v1")

    config = _base_config(["alpha"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert skills is not None
    assert _skill(skills, "alpha").description == "Alpha v1"

    old_mtime = skill_path.stat().st_mtime_ns
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v2")
    os.utime(skill_path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))

    refreshed = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert refreshed is not None
    assert _skill(refreshed, "alpha").description == "Alpha v2"


def test_workspace_skills_dir_discovered(tmp_path: Path) -> None:
    """Skills in the agent workspace skills/ dir should be discovered."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "ws-skill", "Workspace skill")

    config = _base_config(["ws-skill"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(storage),
        roots=[tmp_path / "empty"],
    )
    assert _skill_names(skills) == ["ws-skill"]


def test_empty_allowlist_autoloads_workspace_skills(tmp_path: Path) -> None:
    """Load workspace skills even when the configured allowlist is empty."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "auto", "Auto-loaded workspace skill")

    config = _base_config([])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
    )
    assert _skill_names(skills) == ["auto"]


def test_omitted_allowlist_autoloads_workspace_skills(tmp_path: Path) -> None:
    """Load workspace skills when skills is omitted from agent config."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "auto", "Auto-loaded workspace skill")

    skills = _build_skills(
        "code",
        _base_config_with_omitted_skills(),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
    )
    assert _skill_names(skills) == ["auto"]


def test_empty_allowlist_does_not_load_global_skills(tmp_path: Path) -> None:
    """Keep bundled, plugin, and user skills behind the configured allowlist."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    _write_skill(global_root, "global", "Configured global skill")

    skills = _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(tmp_path / "storage"),
        roots=[global_root],
    )
    assert _skill_names(skills) == []


def test_workspace_skills_override_default_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace skills should override same-named default-root skills."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    _write_skill(global_root, "alpha", "Default alpha")
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [global_root])

    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "alpha", "Workspace alpha")

    skills = _build_skills(
        "code",
        _base_config(["alpha"]),
        _runtime_paths(storage),
    )
    assert skills is not None
    assert _skill_names(skills) == ["alpha"]
    assert _skill(skills, "alpha").description == "Workspace alpha"


def test_malformed_workspace_skill_is_skipped(tmp_path: Path) -> None:
    """Skip malformed workspace skills without rejecting other workspace skills."""
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "bad", "Bad metadata", "{openclaw:")
    _write_skill(workspace_skills, "good", "Good metadata")

    skills = _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
    )
    assert _skill_names(skills) == ["good"]


def _workspace_skills(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / "storage"
    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    return storage, workspace_skills


def _load_workspace_only(tmp_path: Path, storage: Path) -> SkillsToolkit | None:
    return _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
    )


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
    assert "error" in _call(skills, "get_skill_script", "linked", "hello.sh")


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


def test_workspace_skills_created_after_a_build_load_on_the_next_build(tmp_path: Path) -> None:
    """Agents load their skills when they are built, so a workspace skill created later loads on the next build."""
    storage = tmp_path / "storage"
    config = _base_config(["later"])

    def build() -> SkillsToolkit | None:
        return _build_skills(
            "code",
            config,
            _runtime_paths(storage),
            roots=[tmp_path / "empty"],
        )

    assert _skill_names(build()) == []

    workspace_skills = agent_workspace_root_path(storage, "code") / "skills"
    workspace_skills.mkdir(parents=True)
    _write_skill(workspace_skills, "later", "Later workspace skill")

    assert _skill_names(build()) == ["later"]


def test_skill_listings_use_name_fallback_for_missing_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills missing description should still appear in listings."""
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: alpha\n---\n\n# Body\n", encoding="utf-8")
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [tmp_path])

    listings = skills_module.list_skill_listings(())

    assert [(listing.name, listing.description) for listing in listings] == [("alpha", "alpha")]
    listing = skills_module.resolve_skill_listing("alpha", ())
    assert listing is not None
    assert listing.description == "alpha"


def test_skill_listings_include_the_plugins_skills_labeled_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin's skill directory is one of the listed roots, and its skills say they come from a plugin."""
    plugin_root = tmp_path / "plugin-skills"
    _write_skill(plugin_root, "plugin-only", "From a plugin")
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: tmp_path / "user")

    listing = skills_module.resolve_skill_listing("plugin-only", (plugin_root,))

    assert listing is not None
    assert (listing.description, listing.origin) == ("From a plugin", "plugin")


@pytest.mark.parametrize(
    ("frontmatter", "description"),
    [
        ("description: Aliased skill\nmetadata:\n  defaults: &d {os: [linux]}\n  openclaw: *d\n", "Aliased skill"),
        # Not YAML: an unquoted value holding ": " is read as ``key: value`` lines.
        ("description: Use when: the build fails\n", "Use when: the build fails"),
    ],
    ids=["yaml-aliases", "not-yaml"],
)
def test_operator_skill_listings_read_frontmatter_like_agent_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frontmatter: str,
    description: str,
) -> None:
    """An operator-root skill an agent loads is listed as the agent sees it, though workspace skills would refuse it."""
    skill_dir = tmp_path / "operator"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\nname: operator\n{frontmatter}---\n", encoding="utf-8")
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [tmp_path])

    loaded = [(skill.name, skill.description) for skill in skills_module._load_root_skills(tmp_path)]
    listed = [(listing.name, listing.description) for listing in skills_module.list_skill_listings(())]

    assert loaded == listed == [("operator", description)]
    assert skills_module.resolve_skill_listing("operator", ()) is not None


def test_operator_skill_frontmatter_that_is_not_yaml_warns_with_its_file_and_line(tmp_path: Path) -> None:
    """The warning for an operator skill read as ``key: value`` lines names its SKILL.md and the line YAML refused."""
    skill_file = tmp_path.resolve() / "operator" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("---\nname: operator\ndescription: Use when: X\n---\n", encoding="utf-8")

    with capture_logs() as logs:
        skills = skills_module._load_root_skills(tmp_path)

    assert [(skill.name, skill.description) for skill in skills] == [("operator", "Use when: X")]
    [warning] = [entry for entry in logs if entry["event"] == "Error parsing skill frontmatter as YAML"]
    assert warning["path"] == str(skill_file)
    assert "line 3, column" in warning["error"]


def test_operator_skill_listings_skip_a_skill_with_a_blank_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skill whose frontmatter name is blank is not listed, and the skills beside it still are."""
    _write_skill(tmp_path, "valid", "Valid skill")
    nameless_dir = tmp_path / "nameless"
    nameless_dir.mkdir()
    (nameless_dir / "SKILL.md").write_text('---\nname: ""\ndescription: No name\n---\n', encoding="utf-8")
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [tmp_path])

    assert [listing.name for listing in skills_module.list_skill_listings(())] == ["valid"]


def test_skill_with_no_frontmatter_uses_name_fallback_across_discovery_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills without YAML frontmatter should still resolve consistently."""
    skill_dir = tmp_path / "mindroom-dev"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# MindRoom Dev\n\nJust markdown, no frontmatter.\n")

    config = _base_config(["mindroom-dev"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(skills) == ["mindroom-dev"]
    skill = _skill(skills, "mindroom-dev")
    assert skill.description == "mindroom-dev"

    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [tmp_path])
    listing = skills_module.resolve_skill_listing("mindroom-dev", ())
    assert listing is not None
    assert listing.description == "mindroom-dev"


def test_skill_with_mindroom_prefix_loads(tmp_path: Path) -> None:
    """Skills with 'mindroom' in the name should load without issues."""
    _write_skill(tmp_path, "mindroom-helper", "MindRoom helper skill")

    config = _base_config(["mindroom-helper"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
    )
    assert _skill_names(skills) == ["mindroom-helper"]


def test_skill_with_empty_scripts_dir_loads(tmp_path: Path) -> None:
    """Skills with an empty scripts/ directory should load fine."""
    _write_skill(tmp_path, "empty-scripts", "Has empty scripts dir")
    (tmp_path / "empty-scripts" / "scripts").mkdir()

    config = _base_config(["empty-scripts"])
    skills = _build_skills(
        "code",
        config,
        _runtime_paths(tmp_path),
        roots=[tmp_path],
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

    skills = _build_skills(
        "code",
        _base_config(["demo"]),
        _runtime_paths(tmp_path),
        roots=[tmp_path / "skills"],
        output_file_policy=ToolOutputFilePolicy(workspace_root=workspace_root),
    )
    assert skills is not None
    tools = {function.name: function for function in adapt_toolkit(skills).functions}
    assert set(tools) == {"get_skill_instructions", "get_skill_reference", "get_skill_script"}

    for tool in tools.values():
        assert "mindroom_output_path" in tool.parameters["properties"]

    instructions_tool = tools["get_skill_instructions"]
    inline_result = json.loads(instructions_tool.entrypoint("demo"))
    assert inline_result["skill_name"] == "demo"

    redirected = instructions_tool.entrypoint("demo", mindroom_output_path="out/instructions.json")
    receipt = redirected["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (workspace_root / "out" / "instructions.json").exists()

    reference_tool = tools["get_skill_reference"]
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

    skills = _build_skills(
        "code",
        _base_config(["demo"]),
        _runtime_paths(tmp_path),
        roots=[tmp_path / "skills"],
    )
    assert skills is not None
    assert _call(skills, "get_skill_instructions", "demo")["skill_name"] == "demo"


def test_skill_root_loads_are_cached_until_a_skill_file_changes(tmp_path: Path) -> None:
    """A root's skills load once and load again after one of its files is edited."""
    skill_path = _write_skill(tmp_path, "alpha", "Alpha v1")
    try:
        first_load = skills_module._load_root_skills(tmp_path)
        assert first_load[0].description == "Alpha v1"
        assert skills_module._load_root_skills(tmp_path) is first_load

        old_mtime = skill_path.stat().st_mtime_ns
        skill_path = _write_skill(tmp_path, "alpha", "Alpha v2")
        os.utime(skill_path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))

        refreshed = skills_module._load_root_skills(tmp_path)
        assert refreshed is not first_load
        assert refreshed[0].description == "Alpha v2"
    finally:
        skills_module.clear_skill_cache()


def test_operator_skill_support_files_refresh_the_cached_skill(tmp_path: Path) -> None:
    """Adding a script or removing a reference is offered on the next load though SKILL.md is unchanged."""
    skill_dir = _write_skill(tmp_path, "alpha", "Alpha").parent
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    config = _base_config(["alpha"])
    try:
        before = _build_skills("code", config, _runtime_paths(tmp_path), roots=[tmp_path])
        assert before is not None
        assert (_skill(before, "alpha").scripts, _skill(before, "alpha").references) == ((), ("guide.md",))

        _write_skill_script(skill_dir, "check.sh", "echo ok\n")
        with_script = _build_skills("code", config, _runtime_paths(tmp_path), roots=[tmp_path])

        assert with_script is not None
        assert (_skill(with_script, "alpha").scripts, _skill(with_script, "alpha").references) == (
            ("check.sh",),
            ("guide.md",),
        )
        assert "<scripts>check.sh</scripts>" in with_script.prompt_block()
        assert _call(with_script, "get_skill_script", "alpha", "check.sh")["content"] == "echo ok\n"

        (skill_dir / "references" / "guide.md").unlink()
        without_reference = _build_skills("code", config, _runtime_paths(tmp_path), roots=[tmp_path])

        assert without_reference is not None
        assert _skill(without_reference, "alpha").references == ()
    finally:
        skills_module.clear_skill_cache()


def test_a_skill_folder_linked_into_an_operator_root_is_listed_offered_and_refreshed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skill folder symlinked into a root after a first load shows, and its edits and its removal show too."""
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "Alpha")
    linked_file = _write_skill(tmp_path / "elsewhere", "beta", "Beta v1")
    config = _base_config(["alpha", "beta"])
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [root])

    def listed() -> list[tuple[str, str]]:
        return [(listing.name, listing.description) for listing in skills_module.list_skill_listings(())]

    def offered() -> SkillsToolkit | None:
        return _build_skills("code", config, _runtime_paths(tmp_path), roots=[root])

    try:
        assert listed() == [("alpha", "Alpha")]
        assert _skill_names(offered()) == ["alpha"]

        (root / "beta").symlink_to(linked_file.parent, target_is_directory=True)
        assert listed() == [("alpha", "Alpha"), ("beta", "Beta v1")]
        assert _skill_names(offered()) == ["alpha", "beta"]

        old_mtime = linked_file.stat().st_mtime_ns
        _write_skill(tmp_path / "elsewhere", "beta", "Beta v2")
        os.utime(linked_file, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
        assert listed() == [("alpha", "Alpha"), ("beta", "Beta v2")]
        skills = offered()
        assert skills is not None
        assert _skill(skills, "beta").description == "Beta v2"

        (root / "beta").unlink()
        assert listed() == [("alpha", "Alpha")]
        assert _skill_names(offered()) == ["alpha"]
    finally:
        skills_module.clear_skill_cache()


def test_an_operator_root_that_cannot_be_listed_hides_only_its_own_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root whose directory cannot be listed loads nothing, while the other roots still load and list."""
    unreadable = (tmp_path / "unreadable").resolve()
    readable = tmp_path / "readable"
    _write_skill(unreadable, "beta", "Beta")
    _write_skill(readable, "alpha", "Alpha")
    skill_folders = skills_module._skill_folders

    def refuse_unreadable(root: Path) -> list[Path]:
        if root == unreadable:
            raise PermissionError(root)
        return skill_folders(root)

    monkeypatch.setattr(skills_module, "_skill_folders", refuse_unreadable)
    monkeypatch.setattr(skills_module, "default_skill_roots", lambda _plugin_skill_roots: [unreadable, readable])

    assert [listing.name for listing in skills_module.list_skill_listings(())] == ["alpha"]
    config = _base_config(["alpha", "beta"])
    assert _skill_names(_build_skills("code", config, _runtime_paths(tmp_path), roots=[unreadable, readable])) == [
        "alpha",
    ]


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
    parse_count = 0

    def recording_parse(content: str) -> tuple[dict[str, Any], str]:
        nonlocal parse_count
        parse_count += 1
        return parse(content)

    monkeypatch.setattr(skills_module, "_parse_skill_frontmatter", recording_parse)

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert _skill_names(skills) == []
    assert 0 < parse_count < 10
    assert any("budget" in entry["event"] for entry in logs if entry["log_level"] == "warning")


def test_workspace_skill_descriptions_count_toward_the_budget_and_are_capped(tmp_path: Path) -> None:
    """Descriptions reach every system prompt, so they are capped and counted with the rest of each skill."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    for index in range(20):
        _write_skill(workspace_skills, f"skill-{index:02d}", "d" * (900 << 10))

    with capture_logs() as logs:
        skills = _load_workspace_only(tmp_path, storage)

    assert skills is not None
    assert len(skills.prompt_block()) < 1 << 20
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


@pytest.mark.parametrize(
    ("frontmatter", "reason"),
    [
        ("name: xl_check\ndescription: Use when: the task needs an xlsx check", "line 3, column 22"),
        (f"name: {'x' * 65}\ndescription: Check a workbook", "64 characters"),
    ],
)
def test_refused_workspace_skills_are_reported_with_why(tmp_path: Path, frontmatter: str, reason: str) -> None:
    """A SKILL.md that does not load is reported by its workspace path and reason, while its quoted twin loads."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    (workspace_skills / "xl_check").mkdir()
    (workspace_skills / "xl_check" / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# Body\n", encoding="utf-8")
    (workspace_skills / "xl_quoted").mkdir()
    (workspace_skills / "xl_quoted" / "SKILL.md").write_text(
        '---\nname: xl_quoted\ndescription: "Use when: the task needs an xlsx check"\n---\n\n# Body\n',
        encoding="utf-8",
    )
    refused: list[RefusedWorkspaceSkill] = []

    skills = _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
        refused=refused,
    )

    assert _skill_names(skills) == ["xl_quoted"]
    assert [skill.path for skill in refused] == ["skills/xl_check/SKILL.md"]
    assert reason in refused[0].reason
    assert "\n" not in refused[0].reason


def test_a_workspace_skills_directory_that_is_one_refused_skill_is_reported(tmp_path: Path) -> None:
    """A workspace whose skills/SKILL.md cannot load is reported at that path."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    (workspace_skills / "SKILL.md").write_text("---\n- not a mapping\n---\n\n# Body\n", encoding="utf-8")
    refused: list[RefusedWorkspaceSkill] = []

    skills = _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
        refused=refused,
    )

    assert skills is None
    assert [(skill.path, skill.reason) for skill in refused] == [
        ("skills/SKILL.md", "Skill frontmatter must be a mapping"),
    ]


def test_refusal_reasons_cannot_bloat_the_prompt(tmp_path: Path) -> None:
    """A parse error that quotes planted frontmatter is cut to a description's length before it reaches a prompt."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    (workspace_skills / "tagged").mkdir()
    (workspace_skills / "tagged" / "SKILL.md").write_text(
        f"---\nname: tagged\ndescription: !<tag:example.com,2026:{'t' * 100_000}> planted\n---\n\n# Body\n",
        encoding="utf-8",
    )
    refused: list[RefusedWorkspaceSkill] = []

    _build_skills(
        "code",
        _base_config([]),
        _runtime_paths(storage),
        roots=[tmp_path / "global"],
        refused=refused,
    )

    assert [skill.path for skill in refused] == ["skills/tagged/SKILL.md"]
    assert "constructor" in refused[0].reason
    assert len(refused[0].reason) <= skills_module.MAX_WORKSPACE_SKILL_DESCRIPTION_CHARS


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

    assert skills is None or len(skills.prompt_block()) < 1 << 20
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


def test_a_workspace_whose_skills_directory_is_one_skill_records_no_usage(tmp_path: Path) -> None:
    """Usage records live beside skill directories, so loading a skills/ that is itself one skill writes none."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills.parent, "skills", "Whole-directory skill")
    skills = _load_workspace_only(tmp_path, storage)
    assert skills is not None
    assert "Body" in str(_call(skills, "get_skill_instructions", "skills")["instructions"])
    assert not (workspace_skills / ".usage.json").exists()
    assert not (workspace_skills.parent / ".usage.json").exists()


@pytest.mark.asyncio
async def test_workspace_skill_loads_on_the_event_loop_record_usage_in_a_thread(tmp_path: Path) -> None:
    """Agno calls skill tools on the event loop, so the usage write, which syncs files, must not block it."""
    storage, workspace_skills = _workspace_skills(tmp_path)
    _write_skill(workspace_skills, "local", "Workspace skill")
    skills = _load_workspace_only(tmp_path, storage)
    assert skills is not None
    writers: list[threading.Thread] = []
    record = skills_module.record_skill_use

    def spy(skill_path: Path) -> None:
        writers.append(threading.current_thread())
        record(skill_path)

    with patch.object(skills_module, "record_skill_use", spy):
        _call(skills, "get_skill_instructions", "local")
        assert await wait_for_background_tasks(SETTLE_TIMEOUT_SECONDS)
    assert writers
    assert threading.main_thread() not in writers
    assert "last_used_at" in json.loads((workspace_skills / ".usage.json").read_text(encoding="utf-8"))["local"]
