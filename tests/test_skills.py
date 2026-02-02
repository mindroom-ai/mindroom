"""Tests for OpenClaw-compatible skills parsing and gating."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

from mindroom.config import AgentConfig, Config
from mindroom.skills import (
    build_skills_prompt,
    filter_skills_by_eligibility,
    get_agent_skills,
    parse_skill_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(tmp_path: Path, name: str, description: str, metadata: str | None = None) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    lines = ["---", f"name: {name}", f"description: {description}"]
    if metadata is not None:
        lines.append(f"metadata: '{metadata}'")
    lines.append("---")
    lines.append("")
    lines.append("# Body")

    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("\n".join(lines), encoding="utf-8")
    return skill_path


def _base_config() -> Config:
    return Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="",
                tools=["file"],
            ),
        },
    )


def test_parse_skill_with_json5_metadata(tmp_path: Path) -> None:
    """Parse JSON5 metadata from SKILL.md frontmatter."""
    metadata = "{openclaw:{always:true,},}"
    skill_path = _write_skill(tmp_path, "alpha", "Alpha skill", metadata)

    skill = parse_skill_file(skill_path, source="test")

    assert skill is not None
    assert skill.name == "alpha"
    assert skill.description == "Alpha skill"
    assert skill.metadata["openclaw"]["always"] is True


def test_parse_skill_missing_frontmatter(tmp_path: Path) -> None:
    """Skip skills without frontmatter."""
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# No frontmatter", encoding="utf-8")

    skill = parse_skill_file(skill_path, source="test")

    assert skill is None


def test_skill_eligibility_env_and_config(tmp_path: Path) -> None:
    """Gate skills on env vars and config path truthiness."""
    metadata = '{openclaw:{requires:{env:["TEST_ENV"], config:["agents.code.tools"]}}}'
    skill_path = _write_skill(tmp_path, "envconfig", "Requires env and config", metadata)
    skill = parse_skill_file(skill_path, source="test")
    assert skill is not None

    config = _base_config()
    eligible = filter_skills_by_eligibility(
        [skill],
        config,
        env_vars={"TEST_ENV": "1"},
        credential_keys=set(),
    )
    assert eligible == [skill]

    ineligible = filter_skills_by_eligibility(
        [skill],
        config,
        env_vars={},
        credential_keys=set(),
    )
    assert ineligible == []

    eligible_with_credentials = filter_skills_by_eligibility(
        [skill],
        config,
        env_vars={},
        credential_keys={"TEST_ENV"},
    )
    assert eligible_with_credentials == [skill]


def test_skill_eligibility_os_mismatch(tmp_path: Path) -> None:
    """Exclude skills when OS requirements do not match."""
    current = platform.system().lower()
    other = "linux" if current == "windows" else "windows"

    metadata = f'{{openclaw:{{os:["{other}"]}}}}'
    skill_path = _write_skill(tmp_path, "oscheck", "OS restricted", metadata)
    skill = parse_skill_file(skill_path, source="test")
    assert skill is not None

    config = _base_config()
    eligible = filter_skills_by_eligibility(
        [skill],
        config,
        env_vars={},
        credential_keys=set(),
    )
    assert eligible == []


def test_skill_eligibility_always_overrides(tmp_path: Path) -> None:
    """Allow always-eligible skills regardless of other requirements."""
    metadata = '{openclaw:{always:true, os:["windows"]}}'
    skill_path = _write_skill(tmp_path, "always", "Always eligible", metadata)
    skill = parse_skill_file(skill_path, source="test")
    assert skill is not None

    config = _base_config()
    eligible = filter_skills_by_eligibility(
        [skill],
        config,
        env_vars={},
        credential_keys=set(),
    )
    assert eligible == [skill]


def test_get_agent_skills_ordering(tmp_path: Path) -> None:
    """Preserve agent skill ordering when filtering."""
    _write_skill(tmp_path, "alpha", "Alpha skill")
    _write_skill(tmp_path, "beta", "Beta skill")

    config = Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="",
                tools=["file"],
                skills=["beta", "alpha"],
            ),
        },
    )

    skills = get_agent_skills(
        "code",
        config,
        skill_roots=[tmp_path],
        env_vars={},
        credential_keys=set(),
    )

    assert [skill.name for skill in skills] == ["beta", "alpha"]


def test_build_skills_prompt_format(tmp_path: Path) -> None:
    """Render the available skills prompt block."""
    skill_path = _write_skill(tmp_path, "alpha", "Alpha skill")
    skill = parse_skill_file(skill_path, source="test")
    assert skill is not None

    prompt = build_skills_prompt([skill])

    assert "<available_skills>" in prompt
    assert "<name>alpha</name>" in prompt
    assert "<description>Alpha skill</description>" in prompt
    assert "<location>" in prompt
