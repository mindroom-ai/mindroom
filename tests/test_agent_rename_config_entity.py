"""Tests for renaming an agent's key and its references in a config, its includes, and its `.env`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_rename import config_edit
from mindroom.agent_rename.config_edit import config_entity_steps, env_value_steps
from mindroom.agent_rename.steps import FileRewrite, RenamePlan, RenameRefusedError, apply_plan
from tests.agent_rename_helpers import tree_snapshot

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)
VARIABLE = "MINDROOM_CONNECTIONS_AGENT"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def _apply(steps: list[FileRewrite], storage_root: Path) -> None:
    apply_plan(RenamePlan(storage_root=storage_root, steps=tuple(steps), reports=()), kind="rename", now=NOW)


def _steps(config: Path, *, old: str = "atlas", new: str = "orion") -> list[FileRewrite]:
    return config_entity_steps(config, old_name=old, new_name=new, storage_root=config.parent / "mindroom_data")


def _rename(config: Path, *, old: str = "atlas", new: str = "orion") -> list[FileRewrite]:
    steps = _steps(config, old=old, new=new)
    _apply(steps, config.parent / "mindroom_data")
    return steps


@pytest.mark.parametrize(
    ("old_key", "new_key"),
    [("atlas", "orion"), ("'atlas'", "'orion'"), ('"atlas"', '"orion"')],
)
def test_renames_the_agent_key_keeping_comments_and_its_quote_style(tmp_path: Path, old_key: str, new_key: str) -> None:
    """The key under `agents` is renamed in its own quote style; comments and other agents stay."""
    before = (
        "# My agents\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code  # the coder\n"
        "  {key}:   # the orion\n"
        "    display_name: Atlas\n"
        "    role: Does things.\n"
        "\n"
        "# trailing comment\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(key=old_key))

    _rename(config)

    assert config.read_text() == before.format(key=new_key)


def test_renames_team_and_delegate_references_in_block_and_flow_lists(tmp_path: Path) -> None:
    """Team members and delegate targets are renamed whether written as block or one-line flow lists."""
    before = (
        "agents:\n"
        "  {name}:\n"
        "    display_name: Atlas\n"
        "  code:\n"
        "    display_name: Code\n"
        "    delegate_to:\n"
        "      - {name}  # hand off\n"
        "      - code\n"
        "  research:\n"
        "    display_name: Research\n"
        "    delegate_to: [code, '{name}']\n"
        "teams:\n"
        "  builders:\n"
        "    display_name: Builders\n"
        "    role: Build\n"
        "    agents:\n"
        '      - "{name}"\n'
        "      - code\n"
        "  squad:\n"
        "    display_name: Squad\n"
        "    role: Squad\n"
        "    agents: [research, {name}, code]\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="atlas"))

    _rename(config)

    assert config.read_text() == before.format(name="orion")


def test_renames_the_calls_key_and_the_personal_rooms_agent(tmp_path: Path) -> None:
    """`calls.agents` is keyed by agent name and `personal_rooms.agent` names one agent."""
    before = (
        "agents:\n"
        "  {name}:\n"
        "    display_name: Atlas\n"
        "calls:\n"
        "  enabled: true\n"
        "  profiles:\n"
        "    atlas: {{backend: realtime}}\n"
        "  agents:\n"
        "    {name}: atlas   # the profile keeps its name\n"
        "personal_rooms:\n"
        "  agent: {name}\n"
        "  onboarding_rooms: [lobby]\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="atlas"))

    _rename(config)

    assert config.read_text() == before.format(name="orion")


def test_renames_storage_paths_in_every_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A value naming a path in the agent's state root is renamed whether it is absolute, `~`-prefixed, or relative."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_dir = home / ".mindroom-chat"
    storage = config_dir / "mindroom_data"
    (storage / "agents" / "atlas").mkdir(parents=True)
    (config_dir / "storage-link").symlink_to(storage)
    before = (
        "agents:\n"
        "  {name}:\n"
        "    display_name: Atlas\n"
        "plugins:\n"
        "  thread_export:\n"
        "    path: ./mindroom_data/agents/{name}/workspace/thread_exports\n"
        "  location:\n"
        "    places_path: ~/.mindroom-chat/mindroom_data/agents/{name}/workspace/memory/facts/locations.yaml\n"
        "  absolute: '{storage}/agents/{name}/workspace'\n"
        "  state_root: mindroom_data/agents/{name}\n"
        "  trailing: ./mindroom_data/agents/{name}/\n"
        "  linked: ./storage-link/agents/{name}/notes.md\n"
        "  other_root: ./elsewhere/agents/atlas/workspace\n"
        "  other_agent: ./mindroom_data/agents/atlas_2/workspace\n"
        "  nested: ./mindroom_data/workers/agents/atlas/x\n"
        "  prose: Read ~/.mindroom-chat/mindroom_data/agents/atlas/notes.md first\n"
        "  url: https://example.org/agents/atlas/\n"
    )
    config = _write(config_dir / "config.yaml", before.format(name="atlas", storage=storage))

    _rename(config)

    assert config.read_text() == before.format(name="orion", storage=storage)


def test_renames_storage_paths_spelled_with_placeholders(tmp_path: Path) -> None:
    """The documented `${MINDROOM_STORAGE_PATH}` spelling, and the others MindRoom expands, keep their placeholder."""
    before = (
        "agents:\n"
        "  {name}:\n"
        "    display_name: Atlas\n"
        "knowledge_bases:\n"
        "  source_docs:\n"
        "    mode: files\n"
        "    path: ${{MINDROOM_STORAGE_PATH}}/agents/{name}/workspace/source_docs\n"
        "  bare:\n"
        "    path: $MINDROOM_STORAGE_PATH/agents/{name}/workspace/bare\n"
        "  from_config:\n"
        "    path: ${{MINDROOM_CONFIG_PATH}}/../mindroom_data/agents/{name}/workspace/docs\n"
        "  flow: [$MINDROOM_STORAGE_PATH/agents/{name}/f, '${{MINDROOM_STORAGE_PATH}}/agents/{name}/g']\n"
        "  unsupported:\n"
        "    path: ${{HOME}}/agents/atlas/workspace\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="atlas"))

    _rename(config)

    assert config.read_text() == before.format(name="orion")


def test_prose_starting_with_a_state_path_is_left_alone(tmp_path: Path) -> None:
    """A value is a path only when nothing but path characters follow the state root, on one line."""
    text = (
        "    instructions:\n"
        "      - ./mindroom_data/agents/atlas/notes.md holds your notes, read it first\n"
        "    role: |\n"
        "      ./mindroom_data/agents/atlas/notes.md holds your notes.\n"
        "      Read it first.\n"
    )
    config = _write(tmp_path / "config.yaml", "agents:\n  atlas:\n" + text)

    _rename(config)

    assert config.read_text() == "agents:\n  orion:\n" + text


def test_candidate_path_with_a_nul_is_skipped(tmp_path: Path) -> None:
    """A value that cannot be a path, such as one holding a NUL, is left alone instead of crashing the plan."""
    text = 'notes: "./mind\\0room/agents/atlas/x"\nkb: "\\0/agents/atlas/y"\n'
    config = _write(tmp_path / "config.yaml", "agents:\n  atlas: {display_name: C}\n" + text)

    _rename(config)

    assert config.read_text() == "agents:\n  orion: {display_name: C}\n" + text


def test_agent_key_reused_as_a_value_through_an_alias_refuses(tmp_path: Path) -> None:
    """A node that is both a mapping key and a value cannot be judged by one edit, so the plan refuses."""
    config = _write(
        tmp_path / "config.yaml",
        "agents:\n  &name atlas:\n    display_name: C\npersonal_rooms:\n  agent: *name\n  onboarding_rooms: [lobby]\n",
    )

    with pytest.raises(
        RenameRefusedError,
        match=r"config\.yaml \(line 2\) is used both as a mapping key and as a value",
    ):
        _steps(config)


def test_prefix_names_are_left_alone(tmp_path: Path) -> None:
    """Renaming `code` changes nothing that belongs to `code_team`."""
    before = (
        "agents:\n"
        "  {name}:\n"
        "    display_name: Code\n"
        "    delegate_to: [code_team]\n"
        "  code_team:\n"
        "    display_name: Code Team\n"
        "    delegate_to: [{name}, code_team]\n"
        "    knowledge_path: ./mindroom_data/agents/code_team/workspace\n"
        "    workspace: ./mindroom_data/agents/{name}/workspace\n"
        "teams:\n"
        "  code_squad:\n"
        "    display_name: Squad\n"
        "    role: Squad\n"
        "    agents: [{name}, code_team]\n"
        "calls:\n"
        "  agents:\n"
        "    code_team: voice\n"
        "personal_rooms:\n"
        "  agent: code_team\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="code"))

    _rename(config, old="code", new="coder")

    assert config.read_text() == before.format(name="coder")


def test_the_name_outside_reference_places_is_left_alone(tmp_path: Path) -> None:
    """A room, knowledge base, model, or prompt named like the agent is not the agent, and prose is never edited."""
    text = (
        "agents:\n"
        "  code:\n"
        "    display_name: atlas\n"
        "    model: atlas\n"
        "    rooms: [atlas]\n"
        "    knowledge_bases: [atlas]\n"
        "    instructions:\n"
        "      - Ask atlas for help.\n"
        "      - atlas\n"
        "room_models:\n"
        "  atlas: fast\n"
        "knowledge_bases:\n"
        "  atlas:\n"
        "    path: ./kb\n"
        "models:\n"
        "  atlas: {provider: openai, id: gpt}\n"
    )
    config = _write(
        tmp_path / "config.yaml",
        "agents:\n  atlas:\n    display_name: Orion\n" + text.removeprefix("agents:\n"),
    )

    _rename(config)

    assert config.read_text() == "agents:\n  orion:\n    display_name: Orion\n" + text.removeprefix("agents:\n")


def test_edits_include_files_where_they_sit_in_the_config(tmp_path: Path) -> None:
    """References in `!include` and `!expand` files are renamed according to where the file is included."""
    config = _write(
        tmp_path / "config.yaml",
        "agents: !include agents.yaml\n"
        "teams:\n"
        "  builders:\n"
        "    display_name: Builders\n"
        "    role: Build\n"
        "    agents:\n"
        "      - !expand members.yaml\n"
        "      - code\n"
        "room_models: !include rooms.yaml\n",
    )
    agents = _write(
        tmp_path / "agents.yaml",
        "# All agents\natlas: !include agents/atlas.yaml\ncode:\n  display_name: Code\n  delegate_to: [atlas]\n",
    )
    body = _write(
        tmp_path / "agents" / "atlas.yaml",
        "display_name: Atlas\nrole: !include_text role.md\nworkspace: ./mindroom_data/agents/atlas/workspace\n"
        "delegate_to:\n  - atlas\n",
    )
    role = _write(tmp_path / "agents" / "role.md", "atlas\n./mindroom_data/agents/atlas/x\n")
    members = _write(tmp_path / "members.yaml", "- atlas  # the orion\n- research\n")
    rooms = _write(tmp_path / "rooms.yaml", "atlas: fast\n")
    rooms_before = rooms.read_bytes()
    config_before = config.read_bytes()

    steps = _steps(config)

    assert sorted(step.path for step in steps) == sorted(path.resolve() for path in (agents, body, members))
    _apply(steps, tmp_path / "mindroom_data")
    assert agents.read_text() == (
        "# All agents\norion: !include agents/atlas.yaml\ncode:\n  display_name: Code\n  delegate_to: [orion]\n"
    )
    assert body.read_text() == (
        "display_name: Atlas\nrole: !include_text role.md\nworkspace: ./mindroom_data/agents/orion/workspace\n"
        "delegate_to:\n  - orion\n"
    )
    assert role.read_text() == "atlas\n./mindroom_data/agents/atlas/x\n"
    assert members.read_text() == "- orion  # the orion\n- research\n"
    assert rooms.read_bytes() == rooms_before
    assert config.read_bytes() == config_before


def test_directory_includes_are_edited_where_they_sit(tmp_path: Path) -> None:
    """A merged agents directory has the agent key in a file; a team list directory has the members."""
    config = _write(
        tmp_path / "config.yaml",
        "agents: !include_dir_merge_named agents\nteams:\n  squad:\n    display_name: S\n    role: S\n"
        "    agents: !include_dir_merge_list members\n",
    )
    orion = _write(
        tmp_path / "agents" / "orion-and-code.yaml",
        "atlas:\n  display_name: Atlas\ncode:\n  display_name: Code\n",
    )
    members = _write(tmp_path / "members" / "first.yaml", "- atlas\n- code\n")

    _rename(config)

    assert orion.read_text() == "orion:\n  display_name: Atlas\ncode:\n  display_name: Code\n"
    assert members.read_text() == "- orion\n- code\n"


def test_file_whose_place_in_the_config_is_unknown_refuses(tmp_path: Path) -> None:
    """A directory include reading a symlink to a file elsewhere is not followed, so the plan refuses."""
    config = _write(tmp_path / "config.yaml", "agents: !include_dir_merge_named agents\n")
    shared = _write(tmp_path / "shared" / "orion.yaml", "atlas:\n  display_name: Atlas\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "orion.yaml").symlink_to(shared)

    with pytest.raises(RenameRefusedError, match=r"Cannot tell which part of the config .*orion\.yaml holds"):
        _steps(config)


def test_agent_named_by_its_include_file_refuses(tmp_path: Path) -> None:
    """With `!include_dir_named` the agent key is the file name, which a text edit cannot change."""
    config = _write(tmp_path / "config.yaml", "agents: !include_dir_named agents\n")
    _write(tmp_path / "agents" / "atlas.yaml", "display_name: Atlas\n")
    _write(tmp_path / "agents" / "code.yaml", "display_name: Code\ndelegate_to: [atlas]\n")
    before = tree_snapshot(tmp_path)

    with pytest.raises(RenameRefusedError, match=r"atlas\.yaml"):
        _steps(config)

    assert tree_snapshot(tmp_path) == before


def test_value_shared_by_an_alias_with_a_non_reference_refuses(tmp_path: Path) -> None:
    """Editing an anchored value would also change an alias that is not a reference, so the plan refuses."""
    config = _write(
        tmp_path / "config.yaml",
        "agents:\n  atlas:\n    display_name: &name atlas\n"
        "teams:\n  squad:\n    display_name: S\n    role: S\n    agents: [*name]\n",
    )

    with pytest.raises(RenameRefusedError, match=r"config\.yaml \(line 3\) is shared through an alias"):
        _steps(config)


def test_list_shared_by_two_references_is_edited_once(tmp_path: Path) -> None:
    """An anchored member list reused by another team is renamed once, and the alias follows."""
    before = (
        "agents:\n  {name}:\n    display_name: Atlas\n"
        "teams:\n"
        "  a:\n    display_name: A\n    role: A\n    agents: &members [{name}, code]\n"
        "  b:\n    display_name: B\n    role: B\n    agents: *members\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="atlas"))

    _rename(config)

    assert config.read_text() == before.format(name="orion")


def test_merged_mapping_is_judged_where_it_is_merged(tmp_path: Path) -> None:
    """A `<<` merge puts the anchored agent's fields into the merging agent, so both places are references."""
    before = (
        "agents:\n"
        "  {name}:\n    display_name: Atlas\n"
        "  code: &base\n    display_name: Code\n    delegate_to: [{name}]\n"
        "  research:\n    <<: *base\n    display_name: Research\n"
    )
    config = _write(tmp_path / "config.yaml", before.format(name="atlas"))

    _rename(config)

    assert config.read_text() == before.format(name="orion")


def test_new_name_that_needs_quotes_is_quoted(tmp_path: Path) -> None:
    """A new name YAML would read as another type is double-quoted in keys and values."""
    config = _write(
        tmp_path / "config.yaml",
        "agents:\n  atlas: {display_name: C}\npersonal_rooms:\n  agent: atlas\n",
    )

    _rename(config, new="123")

    assert config.read_text() == 'agents:\n  "123": {display_name: C}\npersonal_rooms:\n  agent: "123"\n'


def test_no_reference_means_no_step_and_building_writes_nothing(tmp_path: Path) -> None:
    """A config without the agent produces no steps, and building steps never writes."""
    config = _write(tmp_path / "config.yaml", "agents:\n  code:\n    display_name: Code\n")
    with_agent = _write(tmp_path / "other" / "config.yaml", "agents:\n  atlas:\n    display_name: C\n")
    before = tree_snapshot(tmp_path)

    assert _steps(config) == []
    assert [step.path for step in _steps(with_agent)] == [with_agent.resolve()]

    assert tree_snapshot(tmp_path) == before


def test_unverifiable_edit_refuses_at_plan_time_and_at_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit whose reparse differs from the intended transform is refused when planning and when applying."""
    config = _write(tmp_path / "config.yaml", "agents:\n  atlas:\n    display_name: C\n")
    steps = _steps(config)
    before = tree_snapshot(tmp_path)
    monkeypatch.setattr(config_edit, "_render_scalar", lambda *_args, **_kwargs: "wrong")

    with pytest.raises(RenameRefusedError, match=r"could not be verified in .*config\.yaml"):
        _steps(config)
    with pytest.raises(RenameRefusedError, match="could not be verified"):
        _apply(steps, tmp_path / "mindroom_data")

    after = {key: value for key, value in tree_snapshot(tmp_path).items() if not key.startswith("mindroom_data")}
    assert after == before


def test_env_value_is_replaced_keeping_other_lines(tmp_path: Path) -> None:
    """Only the exact value of the named variable changes; comments, quotes, and other lines stay."""
    env = _write(
        tmp_path / ".env",
        "# MindRoom\r\n"
        "OPENROUTER_API_KEY=sk-atlas\r\n"
        "MINDROOM_CONNECTIONS_AGENT=atlas  # the portal agent\r\n"
        "export MINDROOM_CONNECTIONS_AGENT='atlas'\r\n"
        'MINDROOM_CONNECTIONS_AGENT = "atlas"\r\n'
        "MINDROOM_CONNECTIONS_AGENT=atlas_2\r\n"
        "OTHER=atlas\r\n",
    )
    env.chmod(0o600)

    steps = env_value_steps(env, variable=VARIABLE, old_value="atlas", new_value="orion")
    _apply(steps, tmp_path / "mindroom_data")

    assert env.read_bytes() == (
        b"# MindRoom\r\n"
        b"OPENROUTER_API_KEY=sk-atlas\r\n"
        b"MINDROOM_CONNECTIONS_AGENT=orion  # the portal agent\r\n"
        b"export MINDROOM_CONNECTIONS_AGENT='orion'\r\n"
        b'MINDROOM_CONNECTIONS_AGENT = "orion"\r\n'
        b"MINDROOM_CONNECTIONS_AGENT=atlas_2\r\n"
        b"OTHER=atlas\r\n"
    )
    assert env.stat().st_mode & 0o777 == 0o600


def test_env_without_the_value_has_no_step(tmp_path: Path) -> None:
    """A missing `.env` or one without the exact value produces no steps."""
    assert env_value_steps(tmp_path / ".env", variable=VARIABLE, old_value="atlas", new_value="orion") == []
    env = _write(tmp_path / ".env", "MINDROOM_CONNECTIONS_AGENT=code\nATLAS=atlas\n")

    assert env_value_steps(env, variable=VARIABLE, old_value="atlas", new_value="orion") == []


def test_env_value_dotenv_would_read_differently_refuses(tmp_path: Path) -> None:
    """A new value python-dotenv would not read back unchanged is refused instead of written."""
    env = _write(tmp_path / ".env", "MINDROOM_CONNECTIONS_AGENT=atlas\n")

    with pytest.raises(RenameRefusedError, match=r"could not be verified in .*\.env"):
        env_value_steps(env, variable=VARIABLE, old_value="atlas", new_value="a #b")
