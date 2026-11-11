"""Tests for `mindroom config new-agent`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from typer.testing import CliRunner

from mindroom.cli import config as config_cli
from mindroom.cli.main import app
from mindroom.config import resolution
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("wide_cli_consoles")


@pytest.fixture(autouse=True)
def _clear_runtime_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


def _new_agent(config_path: Path, name: str, template: str) -> str:
    result = runner.invoke(app, ["config", "new-agent", name, "--template", template, "--path", str(config_path)])
    assert result.exit_code == 0, result.output
    return result.output


def _starter_config(config_path: Path) -> None:
    init = runner.invoke(app, ["config", "init", "--path", str(config_path), "--provider", "anthropic", "--no-input"])
    assert init.exit_code == 0, init.output


def _paste(config_path: Path, output: str, name: str) -> str:
    """Paste the printed entry under the config's agents: line, as the command says, and return the new text."""
    pasted = config_path.read_text(encoding="utf-8").replace(
        "\nagents:\n",
        "\nagents:\n" + output[output.index(f'\n  "{name}":\n') + 1 :],
        1,
    )
    config_path.write_text(pasted, encoding="utf-8")
    return pasted


def test_hermes_template_seeds_the_shared_workspace_and_keeps_edits(tmp_path: Path) -> None:
    """A hermes agent gets its workspace seeded, and once pasted, a rerun restores only missing files."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    workspace = tmp_path / "mindroom_data" / "agents" / "apollo" / "workspace"

    output = _new_agent(config_path, "apollo", "hermes")
    _paste(config_path, output, "apollo")

    assert (workspace / "AGENTS.md").is_file()
    assert (workspace / "USER.md").is_file()
    assert (workspace / "NOTICE.md").is_file()
    assert "Created 5 files" in output
    assert '"apollo":' in output
    assert '- "apollo"' in output

    (workspace / "AGENTS.md").write_text("edited\n", encoding="utf-8")
    (workspace / "SOUL.md").unlink()
    output = _new_agent(config_path, "apollo", "hermes")

    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "edited\n"
    assert (workspace / "SOUL.md").is_file()
    assert "Created 1 file in" in output


def test_mind_template_copies_the_starter_mind_into_its_own_room(tmp_path: Path) -> None:
    """A second Mind gets the starter Mind's entry and files, but its own room and the config path in TOOLS.md."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    workspace = tmp_path / "mindroom_data" / "agents" / "ada" / "workspace"

    output = _new_agent(config_path, "ada", "mind")
    pasted = yaml.safe_load(_paste(config_path, output, "ada"))

    mind, ada = pasted["agents"]["mind"], pasted["agents"]["ada"]
    assert ada["rooms"] == ["ada"]
    assert ada["access"]["members_of_rooms"] == ["ada"]
    assert ada["delegate_to"] == ["ada"]
    other_keys = set(mind) - {"rooms", "access", "delegate_to"}
    assert {key: ada[key] for key in other_keys} == {key: mind[key] for key in other_keys}
    assert "Created 7 files" in output
    assert str(config_path.resolve()) in (workspace / "TOOLS.md").read_text(encoding="utf-8")


def test_mind_template_restores_the_starter_minds_missing_files(tmp_path: Path) -> None:
    """Rerunning for the starter's own Mind restores a deleted file and keeps edited ones."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    workspace = tmp_path / "mindroom_data" / "agents" / "mind" / "workspace"
    (workspace / "SOUL.md").unlink()
    (workspace / "USER.md").write_text("edited\n", encoding="utf-8")

    output = _new_agent(config_path, "mind", "mind")

    assert "Created 1 file in" in output
    assert (workspace / "SOUL.md").is_file()
    assert (workspace / "USER.md").read_text(encoding="utf-8") == "edited\n"


def test_legal_template_writes_a_template_dir_the_printed_block_points_at(tmp_path: Path) -> None:
    """A legal agent gets a template directory next to the config that its printed block names."""
    config_path = tmp_path / "config.yaml"

    output = _new_agent(config_path, "helper", "legal")

    template_dir = tmp_path / "templates" / "helper"
    assert (template_dir / "USER.md").is_file()
    assert (template_dir / "skills" / "contract-review" / "SKILL.md").is_file()
    assert "Created 6 files" in output
    assert "template_dir: templates/helper" in output
    assert f"mindroom config validate --path {config_path}" in output
    assert not (tmp_path / "mindroom_data" / "agents" / "helper").exists()


@pytest.mark.parametrize("template", ["mind", "hermes", "coding", "legal", "finance"])
@pytest.mark.parametrize("name", ["apollo", "on", "123", "null"])
def test_agent_block_validates_in_the_starter_config(tmp_path: Path, template: str, name: str) -> None:
    """Each printed block, pasted under the starter config's agents: line, is valid config next to Mind."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    output = _new_agent(config_path, name, template)

    assert "agents:" not in output.splitlines()
    pasted = _paste(config_path, output, name)

    config = Config.validate_with_runtime(yaml.safe_load(pasted), resolve_runtime_paths(config_path=config_path))
    assert set(config.agents) == {"mind", name}
    agent = config.agents[name]
    assert agent.delegate_to == [name]
    if agent.private is None:
        written, context_files = tmp_path / "mindroom_data" / "agents" / name / "workspace", agent.context_files
    else:
        written, context_files = tmp_path / "templates" / name, agent.private.context_files or []
    assert context_files
    assert all((written / context_file).is_file() for context_file in context_files)
    assert (agent.private is None) == (template not in {"legal", "finance"})
    assert (written / "memory").is_dir() == (template == "mind")

    assert "Nothing to create" in _new_agent(config_path, name, template)


@pytest.mark.parametrize("template", ["legal", "finance"])
def test_personal_templates_keep_each_copy_out_of_other_peoples_tools(tmp_path: Path, template: str) -> None:
    """Legal and finance give each person a private copy, no tool that runs as MindRoom, and confined file access."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    starter = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        starter.replace("\ndefaults:\n", "\ndefaults:\n  file_access: unrestricted\n", 1),
        encoding="utf-8",
    )

    output = _new_agent(config_path, "helper", template)

    block = yaml.safe_load(output[output.index('\n  "helper":\n') :])["helper"]
    tools = {tool if isinstance(tool, str) else tool.get("name", next(iter(tool))) for tool in block["tools"]}
    assert block["private"] == {
        "per": "user",
        "template_dir": "templates/helper",
        "context_files": ["AGENTS.md", "SOUL.md", "USER.md"],
    }
    assert not tools & {"shell", "python", "coding"}
    assert block["include_default_tools"] is False
    assert block["file_access"] == "workspace"
    pasted = _paste(config_path, output, "helper")
    config = Config.validate_with_runtime(yaml.safe_load(pasted), resolve_runtime_paths(config_path=config_path))
    assert config.defaults.file_access == "unrestricted"
    assert resolution.file_access(config, "helper") == "workspace"
    assert (tmp_path / "templates" / "helper" / "USER.md").is_file()


@pytest.mark.parametrize(
    ("name", "template", "message"),
    [
        ("mind", "coding", "template directory differ"),
        ("mind", "legal", "template directory differ"),
        ("mind", "hermes", "template directory differ"),
        ("old", "coding", "already has files"),
        ("crew", "finance", "is already a team"),
        ("personal", "hermes", "is already a room"),
    ],
)
def test_refuses_a_name_whose_files_or_entry_belong_to_something_else(
    tmp_path: Path,
    name: str,
    template: str,
    message: str,
) -> None:
    """Mind's name, a leftover workspace no agent in the config owns, or a team's name exits before writing anything."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    leftover = tmp_path / "mindroom_data" / "agents" / "old" / "workspace"
    leftover.mkdir(parents=True)
    (leftover / "notes.md").write_text("kept\n", encoding="utf-8")
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write("\nteams:\n  crew:\n    display_name: Crew\n    role: Test team\n    agents:\n      - mind\n")
    before = sorted(tmp_path.rglob("*"))

    result = runner.invoke(app, ["config", "new-agent", name, "--template", template, "--path", str(config_path)])

    assert result.exit_code == 1
    assert message in result.output
    assert sorted(tmp_path.rglob("*")) == before


def test_refuses_to_mix_two_templates_that_share_a_layout(tmp_path: Path) -> None:
    """Running finance on a pasted legal agent's name adds nothing, because legal's skills are already there."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    _paste(config_path, _new_agent(config_path, "counsel", "legal"), "counsel")

    result = runner.invoke(app, ["config", "new-agent", "counsel", "--template", "finance", "--path", str(config_path)])

    assert result.exit_code == 1
    assert "already holds the legal template's skills" in result.output
    assert not (tmp_path / "templates" / "counsel" / "skills" / "monthly-review").exists()


def test_refuses_a_rerun_for_a_private_agent_whose_template_dir_moved(tmp_path: Path) -> None:
    """A rerun restores files only into the template directory the configured agent actually uses."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    output = _new_agent(config_path, "assistant", "legal")
    _paste(
        config_path,
        output.replace("template_dir: templates/assistant", "template_dir: templates/custom"),
        "assistant",
    )
    (tmp_path / "templates" / "assistant" / "SOUL.md").unlink()

    result = runner.invoke(app, ["config", "new-agent", "assistant", "--template", "legal", "--path", str(config_path)])

    assert result.exit_code == 1
    assert "template directory differ" in result.output
    assert not (tmp_path / "templates" / "assistant" / "SOUL.md").exists()


def test_refuses_to_write_when_the_config_cannot_be_read(tmp_path: Path) -> None:
    """A config whose include is missing exits before writing, since its agent names are unknown."""
    config_path = tmp_path / "config.yaml"
    _starter_config(config_path)
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write("\nteams: !include missing-teams.yaml\n")
    before = sorted(tmp_path.rglob("*"))

    result = runner.invoke(app, ["config", "new-agent", "apollo", "--template", "coding", "--path", str(config_path)])

    assert result.exit_code == 1
    assert "missing-teams.yaml" in result.output
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("my-agent", "letters, digits, and underscores"),
        ("router", "reserved"),
        ("_shared", "reserved"),
        ("mind_", "start or end with an underscore"),
    ],
)
def test_rejects_an_unusable_agent_name(tmp_path: Path, name: str, message: str) -> None:
    """A name config validation would reject exits before writing anything."""
    result = runner.invoke(
        app,
        ["config", "new-agent", name, "--template", "coding", "--path", str(tmp_path / "config.yaml")],
    )

    assert result.exit_code == 1
    assert message in result.output
    assert not (tmp_path / "mindroom_data").exists()


def test_reports_a_planted_symlink_without_following_it(tmp_path: Path) -> None:
    """A template directory entry that points outside it fails with one line and writes nothing there."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents:\n" + config_cli._agent_template_block("legal", "apollo"), encoding="utf-8")
    template_dir = tmp_path / "templates" / "apollo"
    outside = tmp_path / "outside"
    outside.mkdir()
    template_dir.mkdir(parents=True)
    (template_dir / "skills").symlink_to(outside)

    result = runner.invoke(app, ["config", "new-agent", "apollo", "--template", "legal", "--path", str(config_path)])

    assert result.exit_code == 1
    assert "Could not create the template files" in result.output
    assert "Traceback" not in result.output
    assert list(outside.iterdir()) == []


def test_paths_commands_and_block_stay_on_one_line_in_a_narrow_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every printed path, command, and block line can be copied whole at 40 columns."""
    monkeypatch.setattr(config_cli.console, "_width", 40)
    config_path = tmp_path / "config.yaml"

    output = _new_agent(config_path, "assistant", "legal")

    lines = output.splitlines()
    assert f"Created 6 files in {tmp_path / 'templates' / 'assistant'}." in lines
    assert any(line.endswith(f"then run `mindroom config validate --path {config_path}`:") for line in lines)
    block = yaml.safe_load(output[output.index('\n  "assistant":\n') :])
    assert block["assistant"]["private"]["template_dir"] == "templates/assistant"
