"""The owned skills toolkit offers the skill functions and prompt block the prompt goldens pin, and only reads skills."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.invocation import ToolArgumentError, ToolCallContext, execute_tool_call, parse_tool_arguments
from mindroom.tool_system.output_files import ToolOutputFilePolicy, adapt_tool_functions
from mindroom.tool_system.skills import SKILL_FUNCTION_NAMES, SkillsToolkit, build_skills_toolkit
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

PROMPT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "prompts"
AGENT_NAME = "assistant"


def _golden(name: str) -> dict[str, Any]:
    return json.loads((PROMPT_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _write_skill(root: Path, name: str, description: str, *, body: str = "Follow the steps.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", "utf-8")
    return skill_dir


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(
    tmp_path: Path,
    *,
    configured: list[str] | None = None,
    output_files: bool = False,
) -> SkillsToolkit:
    storage = tmp_path / "storage"
    config = Config(agents={AGENT_NAME: AgentConfig(display_name="Assistant", role="", skills=configured or [])})
    toolkit = build_skills_toolkit(
        AGENT_NAME,
        config,
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage),
        skill_roots=[tmp_path / "global"],
        env_vars={},
        credential_keys=set(),
        output_file_policy=ToolOutputFilePolicy(workspace_root=_workspace(tmp_path)) if output_files else None,
    )
    assert toolkit is not None
    return toolkit


def _workspace(tmp_path: Path) -> Path:
    return agent_workspace_root_path(tmp_path / "storage", AGENT_NAME)


def _write_golden_workspace(tmp_path: Path, golden: dict[str, Any]) -> None:
    for relative_path, text in golden["input"]["workspace_files"].items():
        _write_file(_workspace(tmp_path) / relative_path, text)


def _wire_tool(function: ToolFunction) -> dict[str, Any]:
    """Return the request tool entry the prompt goldens record: Agno's ``Function.to_dict()`` in a function wrapper."""
    spec = function.definition()
    fields = {
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.parameters),
        "strict": spec.strict,
        "requires_confirmation": spec.requires_confirmation,
        "external_execution": spec.external_execution,
        "approval_type": spec.approval_type,
    }
    return {"type": "function", "function": {key: value for key, value in fields.items() if value is not None}}


def _golden_skill_tools(golden: dict[str, Any]) -> list[dict[str, Any]]:
    tools = golden["requests"][0]["tools"]
    return [tool for tool in tools if tool["function"]["name"] in SKILL_FUNCTION_NAMES]


@pytest.mark.parametrize(
    ("scenario", "output_files"),
    [("skill_review_fork", False), ("agent_anthropic", True)],
    ids=["plain", "output-files"],
)
def test_skill_functions_equal_the_prompt_goldens(tmp_path: Path, scenario: str, output_files: bool) -> None:
    """The adapted skill functions are the request tools Agno sent, with and without output-file wrapping."""
    golden = _golden(scenario)
    _write_golden_workspace(tmp_path, golden)

    functions = adapt_tool_functions(_build(tmp_path, output_files=output_files)).functions

    assert [_wire_tool(function) for function in functions] == _golden_skill_tools(golden)


def test_the_prompt_block_equals_the_prompt_golden(tmp_path: Path) -> None:
    """The skills section of the system prompt is the one Agno rendered, describing get_skill_script as read-only."""
    golden = _golden("agent_anthropic")
    _write_golden_workspace(tmp_path, golden)
    system_message = golden["requests"][0]["messages"][0]["content"]
    start = system_message.index("<skills_system>")
    end = system_message.index("</skills_system>") + len("</skills_system>")

    block = _build(tmp_path).prompt_block()

    assert block == system_message[start:end]
    assert "`get_skill_script(skill_name, script_path)` - Read scripts" in block
    assert "execute" not in block


def test_the_prompt_block_lists_scripts_and_references(tmp_path: Path) -> None:
    """Each skill lists its scripts and references by name, and says ``none`` when it has no scripts."""
    skills_root = _workspace(tmp_path) / "skills"
    tooling = _write_skill(skills_root, "tooling", "Uses scripts.")
    _write_file(tooling / "scripts" / "run.sh", "#!/bin/sh\n")
    _write_file(tooling / "scripts" / "build.py", "print()\n")
    _write_file(tooling / "references" / "guide.md", "guide\n")
    _write_skill(skills_root, "plain", "Has nothing else.")

    block = _build(tmp_path).prompt_block()

    assert (
        "<skill>\n  <name>plain</name>\n  <description>Has nothing else.</description>\n"
        "  <scripts>none</scripts>\n</skill>\n"
        "<skill>\n  <name>tooling</name>\n  <description>Uses scripts.</description>\n"
        "  <scripts>build.py, run.sh</scripts>\n  <references>guide.md</references>\n</skill>\n\n</skills_system>"
    ) in block


def test_an_agent_without_skills_has_no_prompt_block(tmp_path: Path) -> None:
    """Configured skills that do not load leave a toolkit with no skills and no prompt section."""
    toolkit = _build(tmp_path, configured=["missing"])

    assert toolkit.skills == ()
    assert toolkit.prompt_block() == ""


async def _call(function: ToolFunction, arguments: dict[str, object]) -> tuple[bool, dict[str, Any]]:
    parsed = parse_tool_arguments(json.dumps(arguments), function)
    assert not isinstance(parsed, ToolArgumentError)
    outcome = await execute_tool_call(function, parsed, ToolCallContext(run_id="run-1", session_id="session-1"))
    return outcome.is_error, json.loads(outcome.content) if not outcome.is_error else {"text": outcome.content}


def _functions(toolkit: SkillsToolkit) -> dict[str, ToolFunction]:
    return {function.name: function for function in adapt_tool_functions(toolkit).functions}


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_skill", [True, False], ids=["workspace", "configured"])
async def test_scripts_are_read_and_never_run(tmp_path: Path, workspace_skill: bool) -> None:
    """get_skill_script returns a listed script's text, and a call that asks to run it fails without running it."""
    root = _workspace(tmp_path) / "skills" if workspace_skill else tmp_path / "global"
    skill_dir = _write_skill(root, "scripted", "Scripted skill")
    marker = tmp_path / "script-ran"
    script = f"#!/bin/sh\ntouch {marker}\n"
    _write_file(skill_dir / "scripts" / "hello.sh", script)
    (skill_dir / "scripts" / "hello.sh").chmod(0o755)
    function = _functions(_build(tmp_path, configured=None if workspace_skill else ["scripted"]))["get_skill_script"]

    assert set(function.parameters["properties"]) == {"skill_name", "script_path"}
    assert await _call(function, {"skill_name": "scripted", "script_path": "hello.sh"}) == (
        False,
        {"skill_name": "scripted", "script_path": "hello.sh", "content": script},
    )
    failed, _ = await _call(function, {"skill_name": "scripted", "script_path": "hello.sh", "execute": True})
    assert failed
    assert not marker.exists()


@pytest.mark.asyncio
async def test_workspace_files_are_read_without_following_links(tmp_path: Path) -> None:
    """A workspace skill's listed files are read through no-follow descriptors, so links swapped in are refused."""
    skill_dir = _write_skill(_workspace(tmp_path) / "skills", "docs", "Docs skill")
    references = skill_dir / "references"
    _write_file(references / "guide.md", "own guide")
    _write_file(references / "swapped.md", "own swapped")
    secret = tmp_path / "victim.md"
    secret.write_text("victim-only note", encoding="utf-8")
    (references / "planted.md").symlink_to(secret)
    os.mkfifo(references / "pipe.md")
    toolkit = _build(tmp_path)
    skill = toolkit.get_skill("docs")
    assert skill is not None
    assert skill.references == ("guide.md", "swapped.md")
    (references / "swapped.md").unlink()
    (references / "swapped.md").symlink_to(secret)
    function = _functions(toolkit)["get_skill_reference"]

    assert await _call(function, {"skill_name": "docs", "reference_path": "guide.md"}) == (
        False,
        {"skill_name": "docs", "reference_path": "guide.md", "content": "own guide"},
    )
    _, swapped = await _call(function, {"skill_name": "docs", "reference_path": "swapped.md"})
    assert swapped == {
        "skill_name": "docs",
        "reference_path": "swapped.md",
        "error": "Error reading workspace skill file swapped.md: OSError",
    }
    _, planted = await _call(function, {"skill_name": "docs", "reference_path": "planted.md"})
    assert planted == {
        "error": "Reference 'planted.md' not found in skill 'docs'",
        "available_references": ["guide.md", "swapped.md"],
    }


@pytest.mark.asyncio
async def test_configured_skill_files_linked_outside_their_directory_are_refused(tmp_path: Path) -> None:
    """Configured skills are read by path, and a listed file that resolves outside its directory is refused."""
    skill_dir = _write_skill(tmp_path / "global", "shared", "Configured skill")
    secret = tmp_path / "secret.md"
    secret.write_text("operator secret", encoding="utf-8")
    _write_file(skill_dir / "references" / "guide.md", "shared guide")
    (skill_dir / "references" / "outside.md").symlink_to(secret)
    function = _functions(_build(tmp_path, configured=["shared"]))["get_skill_reference"]

    assert await _call(function, {"skill_name": "shared", "reference_path": "guide.md"}) == (
        False,
        {"skill_name": "shared", "reference_path": "guide.md", "content": "shared guide"},
    )
    assert await _call(function, {"skill_name": "shared", "reference_path": "outside.md"}) == (
        False,
        {"error": "Invalid reference path: 'outside.md'", "skill_name": "shared"},
    )


@pytest.mark.asyncio
async def test_skill_functions_answer_unknown_skills_and_files_with_their_listing(tmp_path: Path) -> None:
    """Unknown skills, missing paths, and unlisted files are answered with what the model can ask for instead."""
    skill_dir = _write_skill(_workspace(tmp_path) / "skills", "docs", "Docs skill")
    _write_file(skill_dir / "scripts" / "run.sh", "#!/bin/sh\n")
    functions = _functions(_build(tmp_path))

    assert await _call(functions["get_skill_instructions"], {"skill_name": "other"}) == (
        False,
        {"error": "Skill 'other' not found", "available_skills": "docs"},
    )
    assert await _call(functions["get_skill_script"], {"skill_name": "docs"}) == (
        False,
        {
            "error": "A script_path is required to read a script from skill 'docs'",
            "skill_name": "docs",
            "available_scripts": ["run.sh"],
        },
    )
    assert await _call(functions["get_skill_script"], {"skill_name": "docs", "script_path": "other.sh"}) == (
        False,
        {"error": "Script 'other.sh' not found in skill 'docs'", "available_scripts": ["run.sh"]},
    )
    assert await _call(functions["get_skill_instructions"], {"skill_name": "docs"}) == (
        False,
        {
            "skill_name": "docs",
            "description": "Docs skill",
            "instructions": "Follow the steps.",
            "available_scripts": ["run.sh"],
            "available_references": [],
        },
    )


@pytest.mark.asyncio
async def test_workspace_loads_record_usage_and_configured_skills_do_not(tmp_path: Path) -> None:
    """Loading a workspace skill or one of its files feeds the learner's inactivity clock; configured skills do not."""
    workspace_skills = _workspace(tmp_path) / "skills"
    _write_file(_write_skill(workspace_skills, "local", "Workspace skill") / "references" / "notes.md", "notes")
    _write_skill(tmp_path / "global", "shared", "Configured skill")
    functions = _functions(_build(tmp_path, configured=["shared"]))
    usage_path = workspace_skills / ".usage.json"

    await _call(functions["get_skill_reference"], {"skill_name": "local", "reference_path": "notes.md"})
    read = json.loads(usage_path.read_text(encoding="utf-8"))["local"]["last_used_at"]
    await _call(functions["get_skill_instructions"], {"skill_name": "local"})
    await _call(functions["get_skill_instructions"], {"skill_name": "shared"})

    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    assert usage["local"]["last_used_at"] > read
    assert "shared" not in usage
    assert not (tmp_path / "global" / ".usage.json").exists()


def test_skills_from_later_sources_win_a_name(tmp_path: Path) -> None:
    """A skill from an explicit root wins its name over a workspace skill, so the explicit skill is the one offered."""
    _write_skill(_workspace(tmp_path) / "skills", "alpha", "Workspace alpha")
    _write_skill(tmp_path / "global", "alpha", "Explicit alpha")

    toolkit = _build(tmp_path, configured=["alpha"])

    skill = toolkit.get_skill("alpha")
    assert skill is not None
    assert (skill.description, skill.source_path) == ("Explicit alpha", (tmp_path / "global" / "alpha").resolve())
