"""Tests for OpenClaw-compatible skills with Agno integration."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING

import pytest
from agno.tools import Toolkit

import mindroom.tool_system.skills as skills_module
from mindroom.commands.handler import _collect_agent_toolkits, _run_skill_command_tool
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.thread_utils import create_session_id
from mindroom.tool_system.metadata import (
    _TOOL_REGISTRY,
    TOOL_METADATA,
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)
from mindroom.tool_system.skills import build_agent_skills, resolve_skill_command_spec
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, get_tool_execution_identity, resolve_worker_key
from tests.conftest import FakeCredentialsManager

if TYPE_CHECKING:
    from pathlib import Path

    from agno.skills import Skills


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


def _skill_names(skills: Skills | None) -> list[str]:
    return skills.get_skill_names() if skills is not None else []


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
        skill_roots=[tmp_path],
        env_vars={"TEST_ENV": "1"},
        credential_keys=set(),
    )
    assert _skill_names(eligible) == ["envconfig"]

    ineligible = build_agent_skills(
        "code",
        config,
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(ineligible) == []

    eligible_with_credentials = build_agent_skills(
        "code",
        config,
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
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(missing) == []

    monkeypatch.setattr(skills_module.shutil, "which", lambda name: f"/bin/{name}")
    available = build_agent_skills(
        "code",
        config,
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
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(eligible) == ["anybins"]

    monkeypatch.setattr(skills_module.shutil, "which", lambda _name: None)
    ineligible = build_agent_skills(
        "code",
        config,
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
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == []


def test_skill_eligibility_always_overrides(tmp_path: Path) -> None:
    """Allow always-eligible skills regardless of other requirements."""
    metadata = '{openclaw:{always:true, os:["windows"]}}'
    _write_skill(tmp_path, "always", "Always eligible", metadata)

    config = _base_config(["always"])
    skills = build_agent_skills(
        "code",
        config,
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert _skill_names(skills) == ["always"]


def test_get_agent_skills_ordering(tmp_path: Path) -> None:
    """Preserve agent skill ordering when filtering."""
    _write_skill(tmp_path, "alpha", "Alpha skill")
    _write_skill(tmp_path, "beta", "Beta skill")

    config = _base_config(["beta", "alpha"])
    skills = build_agent_skills(
        "code",
        config,
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
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )
    assert refreshed is not None
    assert refreshed.get_skill("alpha").description == "Alpha v2"


def test_skill_command_spec_parses_frontmatter(tmp_path: Path) -> None:
    """Parse command dispatch fields from SKILL.md frontmatter."""
    _write_skill(
        tmp_path,
        "dispatch",
        "Dispatch skill",
        extra_frontmatter=[
            "user-invocable: false",
            "command-dispatch: tool",
            "command-tool: demo_tool",
            "command-arg-mode: raw",
        ],
    )

    config = _base_config(["dispatch"])
    spec = resolve_skill_command_spec(
        "dispatch",
        config,
        "code",
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )

    assert spec is not None
    assert spec.user_invocable is False
    assert spec.dispatch is not None
    assert spec.dispatch.tool_name == "demo_tool"
    assert spec.dispatch.arg_mode == "raw"


def test_collect_agent_toolkits_applies_workspace_overrides_like_agent_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Skill command dispatch should reuse the same workspace override rules as create_agent()."""
    captured_calls: list[tuple[str, dict[str, object]]] = []

    def fake_get_tool_by_name(tool_name: str, **kwargs: object) -> object:
        captured_calls.append((tool_name, dict(kwargs)))
        return object()

    monkeypatch.setattr("mindroom.commands.handler.get_tool_by_name", fake_get_tool_by_name)

    workspace = tmp_path / "mind_data"
    config = _base_config(["dispatch"])
    config.agents["code"].memory_backend = "file"
    config.agents["code"].memory_file_path = str(workspace)
    config.agents["code"].tools = ["coding", "shell"]
    config.agents["code"].include_default_tools = False
    config.agents["code"].worker_scope = "user"
    config.agents["code"].worker_tools = ["coding"]

    toolkits = _collect_agent_toolkits(config, "code")

    assert workspace.is_dir()
    assert [tool_name for tool_name, _ in toolkits] == ["coding", "shell"]
    overrides_by_tool = {tool_name: kwargs.get("tool_init_overrides") for tool_name, kwargs in captured_calls}
    assert overrides_by_tool["coding"] == {"base_dir": str(workspace)}
    assert overrides_by_tool["shell"] == {"base_dir": str(workspace)}


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch() -> None:
    """Run a tool dispatch for a skill command with raw args."""

    class DemoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo_tools", tools=[self.demo])

        def demo(self, command: str, commandName: str, skillName: str) -> str:  # noqa: N803
            return f"{commandName}:{skillName}:{command}"

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    try:

        @register_tool_with_metadata(
            name="demo_toolkit",
            display_name="Demo",
            description="Demo tool",
            category=ToolCategory.DEVELOPMENT,
        )
        def demo_toolkit() -> type[Toolkit]:
            return DemoTools

        config = _base_config(["dispatch"])
        config.agents["code"].tools = ["demo_toolkit"]

        result = await _run_skill_command_tool(
            config=config,
            agent_name="code",
            command_tool="demo",
            skill_name="dispatch",
            args_text="hello",
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == "skill:dispatch:hello"


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch_uses_default_tools() -> None:
    """Run skill command dispatch when toolkit is configured via defaults.tools."""

    class DemoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo_tools", tools=[self.demo])

        def demo(self, command: str, commandName: str, skillName: str) -> str:  # noqa: N803
            return f"{commandName}:{skillName}:{command}"

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    try:

        @register_tool_with_metadata(
            name="demo_toolkit",
            display_name="Demo",
            description="Demo tool",
            category=ToolCategory.DEVELOPMENT,
        )
        def demo_toolkit() -> type[Toolkit]:
            return DemoTools

        config = _base_config(["dispatch"])
        config.agents["code"].tools = []
        config.defaults.tools = ["demo_toolkit"]

        result = await _run_skill_command_tool(
            config=config,
            agent_name="code",
            command_tool="demo",
            skill_name="dispatch",
            args_text="hello",
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == "skill:dispatch:hello"


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch_sets_execution_identity() -> None:
    """Skill tool dispatch should establish execution identity before calling tool entrypoints."""

    class DemoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo_tools", tools=[self.demo])

        def demo(self, command: str, commandName: str, skillName: str) -> str:  # noqa: N803
            identity = get_tool_execution_identity()
            assert identity is not None
            return (
                f"{identity.requester_id}:{identity.room_id}:{identity.thread_id}:{identity.session_id}:"
                f"{commandName}:{skillName}:{command}"
            )

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    try:

        @register_tool_with_metadata(
            name="demo_toolkit",
            display_name="Demo",
            description="Demo tool",
            category=ToolCategory.DEVELOPMENT,
        )
        def demo_toolkit() -> type[Toolkit]:
            return DemoTools

        config = _base_config(["dispatch"])
        config.agents["code"].tools = ["demo_toolkit"]
        config.agents["code"].worker_tools = ["demo_toolkit"]
        config.agents["code"].worker_scope = "user"

        result = await _run_skill_command_tool(
            config=config,
            agent_name="code",
            command_tool="demo",
            skill_name="dispatch",
            args_text="hello",
            requester_user_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == (
        f"@alice:example.org:!room:example.org:$thread:{create_session_id('!room:example.org', '$thread')}:"
        "skill:dispatch:hello"
    )


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch_preserves_tenant_scoped_worker_key() -> None:
    """Skill tool dispatch should resolve the same tenant-scoped worker key as normal Matrix routing."""

    class DemoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo_tools", tools=[self.demo])

        def demo(self, command: str, commandName: str, skillName: str) -> str:  # noqa: ARG002, N803
            identity = get_tool_execution_identity()
            assert identity is not None
            worker_key = resolve_worker_key("user_agent", identity, agent_name="code")
            assert worker_key is not None
            return worker_key

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    try:

        @register_tool_with_metadata(
            name="demo_toolkit",
            display_name="Demo",
            description="Demo tool",
            category=ToolCategory.DEVELOPMENT,
        )
        def demo_toolkit() -> type[Toolkit]:
            return DemoTools

        config = _base_config(["dispatch"])
        config.agents["code"].tools = ["demo_toolkit"]
        config.agents["code"].worker_tools = ["demo_toolkit"]
        config.agents["code"].worker_scope = "user_agent"

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("CUSTOMER_ID", "tenant-123")
            monkeypatch.setenv("ACCOUNT_ID", "account-456")
            result = await _run_skill_command_tool(
                config=config,
                agent_name="code",
                command_tool="demo",
                skill_name="dispatch",
                args_text="hello",
                requester_user_id="@alice:example.org",
                room_id="!room:example.org",
                thread_id="$thread",
            )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == "v1:tenant-123:user_agent:@alice:example.org:code"


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch_loads_worker_scoped_config_field_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill dispatch should build config-field tools inside execution identity."""

    class CredentialedTools(Toolkit):
        def __init__(self, api_key: str | None = None) -> None:
            if not api_key:
                msg = "Credentialed API key is required"
                raise ValueError(msg)
            self.api_key = api_key
            super().__init__(name="credentialed_tools", tools=[self.lookup])

        def lookup(self, command: str, commandName: str, skillName: str) -> str:  # noqa: N803
            return f"{self.api_key}:{commandName}:{skillName}:{command}"

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    try:

        @register_tool_with_metadata(
            name="credentialed_toolkit",
            display_name="Credentialed",
            description="Credentialed tool",
            category=ToolCategory.DEVELOPMENT,
            status=ToolStatus.REQUIRES_CONFIG,
            setup_type=SetupType.API_KEY,
            config_fields=[
                ConfigField(
                    name="api_key",
                    label="API Key",
                    type="password",
                    required=False,
                    default=None,
                ),
            ],
        )
        def credentialed_toolkit() -> type[Toolkit]:
            return CredentialedTools

        config = _base_config(["dispatch"])
        config.agents["code"].tools = ["credentialed_toolkit"]
        config.agents["code"].worker_scope = "shared"
        identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="code",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="$thread",
            tenant_id="tenant-123",
            account_id="account-456",
        )
        worker_key = resolve_worker_key("shared", identity, agent_name="code")
        assert worker_key is not None
        fake_credentials = FakeCredentialsManager(
            {},
            worker_managers={
                worker_key: FakeCredentialsManager(
                    {"credentialed_toolkit": {"api_key": "worker-key", "_source": "ui"}},
                ),
            },
        )

        monkeypatch.setattr(
            "mindroom.tool_system.metadata.get_credentials_manager",
            lambda: fake_credentials,
        )
        monkeypatch.setenv("CUSTOMER_ID", "tenant-123")
        monkeypatch.setenv("ACCOUNT_ID", "account-456")

        result = await _run_skill_command_tool(
            config=config,
            agent_name="code",
            command_tool="lookup",
            skill_name="dispatch",
            args_text="hello",
            requester_user_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == "worker-key:skill:dispatch:hello"
