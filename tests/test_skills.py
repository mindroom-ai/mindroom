"""Tests for OpenClaw-compatible skills with Agno integration."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

import pytest
from agno.tools import Toolkit

from mindroom.bot import _run_skill_command_tool
from mindroom.config import AgentConfig, Config
from mindroom.skills import build_agent_skills, resolve_skill_command_spec
from mindroom.tools_metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory, register_tool_with_metadata

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


@pytest.mark.asyncio
async def test_skill_command_tool_dispatch() -> None:
    """Run a tool dispatch for a skill command with raw args."""

    class DemoTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo_tools", tools=[self.demo])

        def demo(self, command: str, commandName: str, skillName: str) -> str:  # noqa: N803
            return f"{commandName}:{skillName}:{command}"

    original_registry = TOOL_REGISTRY.copy()
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
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

    assert result == "skill:dispatch:hello"
