"""Tests for verified in-place edits of string values in a config and its includes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_rename import config_edit
from mindroom.agent_rename.config_edit import config_value_steps
from mindroom.agent_rename.steps import FileRewrite, RenamePlan, RenameRefusedError, apply_plan
from tests.agent_rename_helpers import tree_snapshot

if TYPE_CHECKING:
    from pathlib import Path

OLD = "@old:example.org"
NEW = "@new:example.org"
NOW = datetime(2026, 10, 19, 21, 45, 58, tzinfo=UTC)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def _apply(steps: list[FileRewrite], storage_root: Path) -> None:
    apply_plan(RenamePlan(storage_root=storage_root, steps=tuple(steps), reports=()), kind="rename-account", now=NOW)


def _edit(config: Path, *, old: str = OLD, new: str = NEW) -> list[FileRewrite]:
    steps = config_value_steps(config, old_value=old, new_value=new)
    _apply(steps, config.parent / "storage")
    return steps


def test_replaces_user_id_values_keeping_comments_and_quotes(tmp_path: Path) -> None:
    """Comments, quote styles, and indentation survive; only the matching values change."""
    config = _write(
        tmp_path / "config.yaml",
        "# Who may talk to the agents\n"
        "agents:\n"
        "  nova:\n"
        "    display_name: Nova   # not touched\n"
        "    authorized_users:\n"
        "      - '@old:example.org'   # the owner\n"
        '      - "@old:example.org"\n'
        "      - '@other:example.org'\n"
        "authorization:\n"
        "    global_users: ['@old:example.org', \"@old:example.org\"]\n"
        "\n"
        "# trailing comment\n",
    )

    _edit(config)

    assert config.read_text() == (
        "# Who may talk to the agents\n"
        "agents:\n"
        "  nova:\n"
        "    display_name: Nova   # not touched\n"
        "    authorized_users:\n"
        "      - '@new:example.org'   # the owner\n"
        '      - "@new:example.org"\n'
        "      - '@other:example.org'\n"
        "authorization:\n"
        "    global_users: ['@new:example.org', \"@new:example.org\"]\n"
        "\n"
        "# trailing comment\n"
    )


def test_plain_scalars_stay_plain(tmp_path: Path) -> None:
    """A plain scalar stays plain when the new value can be written plain."""
    config = _write(
        tmp_path / "config.yaml",
        "matrix:\n  server_name: old.example.org  # home\n  other: old.example.org.uk\n",
    )

    _edit(config, old="old.example.org", new="new.example.org")

    assert config.read_text() == "matrix:\n  server_name: new.example.org  # home\n  other: old.example.org.uk\n"


@pytest.mark.parametrize(
    "new",
    ["a: b", "123", "true", "@new:example.org", "has # hash", "x,y", "[z]", "", "two\nlines"],
)
def test_plain_scalar_that_needs_quoting_is_written_double_quoted(tmp_path: Path, new: str) -> None:
    """A new value that cannot stay plain is double-quoted, and the reparse still passes."""
    config = _write(tmp_path / "config.yaml", "items:\n  - old\n  - key: old\n  - [old, other]\n")

    _edit(config, old="old", new=new)

    document = config_edit._compose(config.read_text())
    assert [(node.value, node.style) for node, _ in config_edit._walk(document) if node.value == new] == [
        (new, '"'),
        (new, '"'),
        (new, '"'),
    ]
    assert "other" in config.read_text()


def test_single_quoted_value_with_a_quote_is_escaped_by_doubling(tmp_path: Path) -> None:
    """A single-quoted value stays single-quoted and escapes embedded quotes."""
    config = _write(tmp_path / "config.yaml", "name: 'old'\n")

    _edit(config, old="old", new="it's")

    assert config.read_text() == "name: 'it''s'\n"


def test_double_quoted_value_escapes_special_characters(tmp_path: Path) -> None:
    """A double-quoted value escapes quotes, backslashes, and control characters."""
    config = _write(tmp_path / "config.yaml", 'name: "old"\n')

    _edit(config, old="old", new='a"b\\c\nd\u2028e\U0001f600')

    assert config.read_text() == 'name: "a\\"b\\\\c\\nd\\u2028e\U0001f600"\n'


def test_edits_include_files(tmp_path: Path) -> None:
    """Each !include and !expand target that holds the value gets its own step."""
    config = _write(
        tmp_path / "config.yaml",
        "agents: !include agents.yaml\nusers:\n  - !expand users/list.yaml\n  - '@old:example.org'\nplain: !include clean.yaml\n",
    )
    agents = _write(tmp_path / "agents.yaml", "nova:\n  # owner\n  owner: '@old:example.org'\n")
    users = _write(tmp_path / "users" / "list.yaml", '- "@old:example.org"\n- "@keep:example.org"\n')
    clean = _write(tmp_path / "clean.yaml", "a: b\n")
    clean_before = clean.read_bytes()

    steps = config_value_steps(config, old_value=OLD, new_value=NEW)
    assert sorted(step.path for step in steps) == sorted([config.resolve(), agents.resolve(), users.resolve()])
    _apply(steps, tmp_path / "storage")

    assert config.read_text().count(NEW) == 1
    assert agents.read_text() == "nova:\n  # owner\n  owner: '@new:example.org'\n"
    assert users.read_text() == '- "@new:example.org"\n- "@keep:example.org"\n'
    assert clean.read_bytes() == clean_before


def test_text_includes_are_not_edited(tmp_path: Path) -> None:
    """A file read with !include_text is prose, so it is not edited and does not need to parse as YAML."""
    config = _write(tmp_path / "config.yaml", "prompt: !include_text prompt.md\nowner: '@old:example.org'\n")
    prompt = _write(tmp_path / "prompt.md", "Owners:\n- '@old:example.org'\n\nnot: [valid\n")

    steps = config_value_steps(config, old_value=OLD, new_value=NEW)

    assert [step.path for step in steps] == [config.resolve()]
    _apply(steps, tmp_path / "storage")
    assert prompt.read_text() == "Owners:\n- '@old:example.org'\n\nnot: [valid\n"


def test_leaves_substrings_and_keys_alone(tmp_path: Path) -> None:
    """Only whole string values are replaced, never substrings, longer values, or mapping keys."""
    text = (
        "map:\n"
        "  '@old:example.org': keep-key\n"
        "  other: '@old:example.org.uk'\n"
        "  third: 'prefix @old:example.org'\n"
        "  number: 5\n"
        "  '@old:example.org2': x\n"
    )
    config = _write(tmp_path / "config.yaml", text + "  value: '@old:example.org'\n")

    _edit(config)

    assert config.read_text() == text + "  value: '@new:example.org'\n"


def test_no_occurrence_means_no_step(tmp_path: Path) -> None:
    """A config without the value produces no steps."""
    config = _write(tmp_path / "config.yaml", "a: '@other:example.org'\n'@old:example.org': b\n")

    assert config_value_steps(config, old_value=OLD, new_value=NEW) == []


def test_anchors_tags_and_aliases_survive(tmp_path: Path) -> None:
    """An anchor or tag in front of the value is kept, and aliases follow the edited anchor."""
    config = _write(
        tmp_path / "config.yaml",
        "owner: &owner '@old:example.org'\nagain: *owner\ntagged: !!str '@old:example.org'\nsplit: &s\n  '@old:example.org'\n",
    )

    _edit(config)

    assert config.read_text() == (
        "owner: &owner '@new:example.org'\nagain: *owner\ntagged: !!str '@new:example.org'\nsplit: &s\n  '@new:example.org'\n"
    )


def test_include_tag_paths_and_other_scalar_types_are_left_alone(tmp_path: Path) -> None:
    """An !include path is not a string value, and non-string scalars never match."""
    _write(tmp_path / "old.yaml", "x: 1\n")
    config = _write(tmp_path / "config.yaml", "a: !include old.yaml\nb: null\nc: ''\nd: old.yaml\ne: 5\n")

    _edit(config, old="old.yaml", new="new.yaml")

    assert config.read_text() == "a: !include old.yaml\nb: null\nc: ''\nd: new.yaml\ne: 5\n"


def test_crlf_bom_and_non_ascii_text_keep_their_bytes(tmp_path: Path) -> None:
    """Offsets stay correct after non-ASCII text, and line endings and the BOM are untouched."""
    config = _write(tmp_path / "config.yaml", "\ufeff# héllo \U0001f600\r\nowner: '@old:example.org'\r\nname: über\r\n")

    _edit(config)

    assert config.read_bytes() == "\ufeff# héllo \U0001f600\r\nowner: '@new:example.org'\r\nname: über\r\n".encode()


def test_block_scalar_value_refuses_at_plan_time(tmp_path: Path) -> None:
    """A value written as a block scalar cannot be edited in place, so the plan refuses instead of leaving it stale."""
    config = _write(tmp_path / "config.yaml", "owner: |-\n  @old:example.org\n")
    before = tree_snapshot(tmp_path)

    with pytest.raises(RenameRefusedError, match="block scalar"):
        config_value_steps(config, old_value=OLD, new_value=NEW)

    assert tree_snapshot(tmp_path) == before


def test_building_steps_does_not_touch_the_disk(tmp_path: Path) -> None:
    """Building the steps is a dry run: nothing is written."""
    config = _write(tmp_path / "config.yaml", "owner: '@old:example.org'\n")
    before = tree_snapshot(tmp_path)

    config_value_steps(config, old_value=OLD, new_value=NEW)

    assert tree_snapshot(tmp_path) == before


def test_unreadable_config_refuses(tmp_path: Path) -> None:
    """A config that does not load refuses with the reason."""
    config = _write(tmp_path / "config.yaml", "a: [unclosed\n")

    with pytest.raises(RenameRefusedError, match=r"config\.yaml"):
        config_value_steps(config, old_value=OLD, new_value=NEW)


def test_verification_failure_refuses_at_plan_time_and_at_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewrite whose reparse differs from the intended edit is refused when planning and again when applying."""
    config = _write(tmp_path / "config.yaml", "owner: '@old:example.org'\n")
    steps = config_value_steps(config, old_value=OLD, new_value=NEW)
    monkeypatch.setattr(config_edit, "_render_scalar", lambda _value, _style: "'@wrong:example.org'")

    with pytest.raises(RenameRefusedError, match=r"Config edit could not be verified in .*config\.yaml"):
        config_value_steps(config, old_value=OLD, new_value=NEW)
    with pytest.raises(RenameRefusedError, match=r"Config edit could not be verified in .*config\.yaml"):
        steps[0].rewrite(config.read_bytes())


def test_verification_failure_leaves_the_disk_untouched_when_applying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal at apply happens before the file is written, so the tree is unchanged."""
    config = _write(tmp_path / "config.yaml", "owner: '@old:example.org'\n")
    steps = config_value_steps(config, old_value=OLD, new_value=NEW)
    before = tree_snapshot(tmp_path)
    monkeypatch.setattr(config_edit, "_render_scalar", lambda _value, _style: "[broken")

    with pytest.raises(RenameRefusedError, match="could not be verified"):
        _apply(steps, tmp_path / "storage")

    after = {key: value for key, value in tree_snapshot(tmp_path).items() if not key.startswith("storage")}
    assert after == before


def test_apply_edits_the_file_as_it_is_at_apply_time(tmp_path: Path) -> None:
    """The rewrite works on the current bytes, so an edit made after planning is not lost."""
    config = _write(tmp_path / "config.yaml", "owner: '@old:example.org'\n")
    steps = config_value_steps(config, old_value=OLD, new_value=NEW)
    config.write_text("# added later\nowner: '@old:example.org'\nextra: '@old:example.org'\n")

    _apply(steps, tmp_path / "storage")

    assert config.read_text() == "# added later\nowner: '@new:example.org'\nextra: '@new:example.org'\n"


def test_symlinked_config_is_edited_through_the_link(tmp_path: Path) -> None:
    """The step targets the real file, so a symlinked config stays a symlink."""
    real = _write(tmp_path / "real" / "config.yaml", "owner: '@old:example.org'\n")
    link = tmp_path / "config.yaml"
    link.symlink_to(real)

    _edit(link)

    assert link.is_symlink()
    assert real.read_text() == "owner: '@new:example.org'\n"


def test_config_the_production_loader_accepts_is_editable(tmp_path: Path) -> None:
    """The edit is judged by the parser MindRoom reads configs with, which accepts a tab after a colon."""
    config = _write(tmp_path / "config.yaml", "owner:\t'@old:example.org'\n")

    _edit(config)

    assert config.read_text() == "owner:\t'@new:example.org'\n"
