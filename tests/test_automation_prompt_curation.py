"""The prompt_curation built-in: when it asks, what it asks for, and what verify reports without writing the files."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.automations.prompt_curation import (
    CurationPlan,
    curation_notice,
    curation_prompt,
    plan_curation,
    verify_curation,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.automations import PromptCurationAutomation
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Eight 642-character sections: MEMORY.md is 1,286 tokens, over a 1,000-token trigger.
SECTIONS = [f"## Topic {index}\n" + f"Detail {index} " * 70 + "\n" for index in range(8)]
MEMORY = "# Memory\n" + "".join(SECTIONS)
POINTER = "## Topic 3\nSee memory/topics.md\n"
SOUL = "Be kind and brief.\n"
USER = "Name: Sam\n"


def _setup(tmp_path: Path, **settings: object) -> tuple[Config, PromptCurationAutomation, Path]:
    automation = PromptCurationAutomation(**{"trigger_tokens": 1_000, **settings})
    agent = AgentConfig(
        display_name="Mind",
        memory_backend="file",
        context_files=["SOUL.md", "AGENTS.md", "USER.md"],
        automations=[automation],
    )
    config = Config(agents={"mind": agent}, router=RouterConfig(model="default"))
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    root = resolve_agent_runtime("mind", config, paths, None, create=True).file_memory_root
    assert root is not None
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (root / "SOUL.md").write_text(SOUL, encoding="utf-8")
    (root / "USER.md").write_text(USER, encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "2026-10-01.md").write_text("Daily note.\n", encoding="utf-8")
    return config, automation, root


def _plan(tmp_path: Path, **settings: object) -> tuple[CurationPlan, Config, Path]:
    config, automation, root = _setup(tmp_path, **settings)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    plan = plan_curation(config, paths, "mind", automation)
    assert plan is not None
    return plan, config, root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_files_under_the_trigger_ask_for_nothing(tmp_path: Path) -> None:
    """Small prompt files post no prompt."""
    config, automation, root = _setup(tmp_path)
    (root / "MEMORY.md").write_text("# Memory\n- Prefers terse replies.\n", encoding="utf-8")
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    assert plan_curation(config, paths, "mind", automation) is None


def test_the_plan_covers_memory_and_every_context_file_with_a_gradual_band(tmp_path: Path) -> None:
    """MEMORY.md plus the context files count; the band asks for 10-15% and never more."""
    plan, _config, _root = _plan(tmp_path)

    assert plan.curated == {"MEMORY.md": 1286, "SOUL.md": 4, "USER.md": 2}
    assert plan.measured_tokens == 1292
    assert (plan.upper_tokens, plan.floor_tokens) == (1163, 1098)
    assert plan.memory_tokens == 3


def test_the_band_is_the_configured_reductions_of_the_measured_size(tmp_path: Path) -> None:
    """The band follows min_reduction and max_reduction even when that ends well below the trigger."""
    plan, _config, _root = _plan(tmp_path, trigger_tokens=1_200, min_reduction=0.2, max_reduction=0.25)

    assert (plan.upper_tokens, plan.floor_tokens) == (1034, 969)


def test_protected_files_are_excluded_from_the_band(tmp_path: Path) -> None:
    """A protected file is snapshotted for verify but not counted or offered for curation."""
    plan, config, _root = _plan(tmp_path, protected_files=["SOUL.md"])

    assert "SOUL.md" not in plan.curated
    assert "SOUL.md" in plan.snapshot
    assert "6. Leave SOUL.md unchanged." in curation_prompt(config, plan)


def test_the_prompt_states_the_exact_numbers_and_the_git_step(tmp_path: Path) -> None:
    """The visible prompt names every file, the band, the per-file cap, committing first, and the re-check."""
    plan, config, _root = _plan(tmp_path)

    prompt = curation_prompt(config, plan)

    assert "total 1292 tokens, over the 1000-token limit" in prompt
    assert "MEMORY.md (1286 tokens), SOUL.md (4 tokens), USER.md (2 tokens)" in prompt
    assert "at most 1163 tokens in total, but not below 1098" in prompt
    assert "shrink by more than 25%" in prompt
    assert "git init" in prompt
    assert "asks you to re-check" in prompt
    assert "6. Leave" not in prompt


def test_an_untouched_workspace_is_reported_unchanged(tmp_path: Path) -> None:
    """A run that edits nothing gets a plain notice."""
    plan, config, _root = _plan(tmp_path)

    result = verify_curation(plan)

    assert (result.changed, result.findings) == (False, ())
    assert curation_notice(config, plan, result) == "Prompt maintenance changed nothing; the files stay at 1292 tokens."


def test_moving_a_section_into_memory_within_bounds_is_accepted(tmp_path: Path) -> None:
    """Detail moved verbatim into memory/ with a pointer lands in the band and gets a plain notice."""
    plan, config, root = _plan(tmp_path)
    (root / "memory" / "topics.md").write_text(SECTIONS[3], encoding="utf-8")
    (root / "MEMORY.md").write_text(MEMORY.replace(SECTIONS[3], POINTER), encoding="utf-8")

    result = verify_curation(plan)

    assert (result.changed, result.findings) == (True, ())
    assert curation_notice(config, plan, result) == "✅ Prompt files condensed from 1292 to 1139 tokens."


def _over_cut(root: Path) -> None:
    (root / "MEMORY.md").write_text("# Memory\n" + SECTIONS[0], encoding="utf-8")


def _delete_without_moving(root: Path) -> None:
    (root / "MEMORY.md").write_text(MEMORY.replace(SECTIONS[3], ""), encoding="utf-8")


def _grow(root: Path) -> None:
    (root / "MEMORY.md").write_text(MEMORY + "- One more fact.\n", encoding="utf-8")


def _delete_a_file(root: Path) -> None:
    (root / "USER.md").unlink()


def _corrupt(root: Path) -> None:
    (root / "MEMORY.md").write_bytes(MEMORY.encode() + b"caf\xe9\n")


def _grow_past_the_read_cap(root: Path) -> None:
    (root / "MEMORY.md").write_bytes(b"x" * ((1 << 20) + 1))


def _replace_with_a_link(root: Path) -> None:
    outside = root.parent / "outside.md"
    outside.write_text("planted\n", encoding="utf-8")
    (root / "MEMORY.md").unlink()
    (root / "MEMORY.md").symlink_to(outside)


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (_over_cut, "MEMORY.md shrank 87% (more than 25%)"),
        (_delete_without_moving, "about 161 tokens of memory were deleted rather than moved to memory/ (at most 65)"),
        (_grow, "the files did not shrink (1296 tokens)"),
        (_delete_a_file, "USER.md shrank 100% (more than 25%)"),
        (_corrupt, "MEMORY.md is no longer valid UTF-8"),
        (_grow_past_the_read_cap, "MEMORY.md cannot be read (File exceeds its size limit: MEMORY.md)"),
        (_replace_with_a_link, "MEMORY.md cannot be read ([Errno 40] Too many levels of symbolic links: 'MEMORY.md')"),
    ],
)
def test_a_run_that_misses_the_bounds_is_asked_to_recheck_and_left_as_is(
    tmp_path: Path,
    change: Callable[[Path], None],
    finding: str,
) -> None:
    """Every bound names its finding in a re-check request, and verify writes nothing."""
    plan, config, root = _plan(tmp_path)
    change(root)
    after = _snapshot(root)

    result = verify_curation(plan)

    assert finding in result.findings
    assert _snapshot(root) == after
    notice = curation_notice(config, plan, result)
    assert notice.startswith("⚠️ Prompt maintenance needs a re-check: ")
    assert finding in notice
    assert "Compare your change with the commit you made before it" in notice


@pytest.mark.parametrize("change", [_corrupt, _grow_past_the_read_cap, _replace_with_a_link])
def test_an_unreadable_file_is_the_only_finding(tmp_path: Path, change: Callable[[Path], None]) -> None:
    """Without the file's size, verify reports only that it cannot be read, never a false deletion or floor."""
    plan, _config, root = _plan(tmp_path)
    change(root)

    result = verify_curation(plan)

    assert len(result.findings) == 1
    assert result.findings[0].startswith("MEMORY.md ")


def test_a_fact_another_conversation_writes_during_the_run_is_kept(tmp_path: Path) -> None:
    """Verify never writes, so a fact added in a parallel thread survives even a run that misses the bounds."""
    plan, _config, root = _plan(tmp_path)
    _over_cut(root)
    with (root / "MEMORY.md").open("a", encoding="utf-8") as memory:
        memory.write("- Dentist on Friday.\n")

    result = verify_curation(plan)

    assert result.findings
    assert (root / "MEMORY.md").read_text().endswith("- Dentist on Friday.\n")


def test_changing_a_protected_file_is_reported(tmp_path: Path) -> None:
    """A protected file edit is a finding even when the cut itself is fine."""
    plan, _config, root = _plan(tmp_path, protected_files=["SOUL.md"])
    (root / "SOUL.md").write_text("Rewritten.\n", encoding="utf-8")
    (root / "memory" / "topics.md").write_text(SECTIONS[3], encoding="utf-8")
    (root / "MEMORY.md").write_text(MEMORY.replace(SECTIONS[3], POINTER), encoding="utf-8")

    result = verify_curation(plan)

    assert result.findings == ("SOUL.md changed although it is protected",)
    assert (root / "SOUL.md").read_text() == "Rewritten.\n"


def test_a_planted_link_stops_the_check(tmp_path: Path) -> None:
    """A symlinked context file is refused instead of followed."""
    config, automation, root = _setup(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "USER.md").unlink()
    (root / "USER.md").symlink_to(outside)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    with pytest.raises((OSError, ValueError)):
        plan_curation(config, paths, "mind", automation)


ARCHIVE = "# Projects\n" + "Archived project detail. " * 80 + "\n"


def _plan_with_archive(tmp_path: Path) -> tuple[CurationPlan, Path]:
    config, automation, root = _setup(tmp_path)
    (root / "memory" / "projects.md").write_text(ARCHIVE, encoding="utf-8")
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    plan = plan_curation(config, paths, "mind", automation)
    assert plan is not None
    return plan, root


def test_deleting_archived_memory_while_cutting_is_reported(tmp_path: Path) -> None:
    """A cut in bounds that also empties a memory/ topic file deleted detail, which the loss finding names."""
    plan, root = _plan_with_archive(tmp_path)
    (root / "memory" / "projects.md").unlink()
    (root / "memory" / "topics.md").write_text(SECTIONS[3], encoding="utf-8")
    (root / "MEMORY.md").write_text(MEMORY.replace(SECTIONS[3], POINTER), encoding="utf-8")

    result = verify_curation(plan)

    assert any("deleted rather than moved" in finding for finding in result.findings)
    assert not (root / "memory" / "projects.md").exists()


def test_deleting_archived_memory_with_the_prompt_files_untouched_is_reported(tmp_path: Path) -> None:
    """A run that only empties a memory/ topic file still deleted detail, so it gets a re-check."""
    plan, root = _plan_with_archive(tmp_path)
    (root / "memory" / "projects.md").unlink()
    after = _snapshot(root)

    result = verify_curation(plan)

    assert not result.changed
    assert len(result.findings) == 1
    assert "deleted rather than moved" in result.findings[0]
    assert _snapshot(root) == after


def test_an_archive_already_past_the_read_cap_does_not_hide_deleted_detail(tmp_path: Path) -> None:
    """The loss baseline counts a topic file that was past 1 MiB at fire time, so it cannot pass as moved detail."""
    config, automation, root = _setup(tmp_path)
    (root / "memory" / "big.md").write_text("Old detail.\n" * 100_000, encoding="utf-8")
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    plan = plan_curation(config, paths, "mind", automation)
    assert plan is not None
    _delete_without_moving(root)

    result = verify_curation(plan)

    assert "about 161 tokens of memory were deleted rather than moved to memory/ (at most 65)" in result.findings


def test_a_context_file_under_memory_is_counted_once(tmp_path: Path) -> None:
    """Moving detail out of a context file that lives under memory/ is a move, not a deletion."""
    config, automation, root = _setup(tmp_path)
    context = "# Context\n" + "".join(SECTIONS)
    (root / "memory" / "context.md").write_text(context, encoding="utf-8")
    (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    config.agents["mind"].context_files = ["memory/context.md"]
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    plan = plan_curation(config, paths, "mind", automation)
    assert plan is not None
    (root / "memory" / "topics.md").write_text(SECTIONS[3], encoding="utf-8")
    (root / "memory" / "context.md").write_text(context.replace(SECTIONS[3], POINTER), encoding="utf-8")

    result = verify_curation(plan)

    assert (result.changed, result.findings) == (True, ())


def test_reorganizing_an_archive_without_losing_content_is_accepted(tmp_path: Path) -> None:
    """Rewriting a topic file is fine as long as the detail stays and the cut is in bounds."""
    plan, root = _plan_with_archive(tmp_path)
    (root / "memory" / "projects.md").write_text(SECTIONS[3] + ARCHIVE, encoding="utf-8")
    (root / "MEMORY.md").write_text(MEMORY.replace(SECTIONS[3], POINTER), encoding="utf-8")

    result = verify_curation(plan)

    assert (result.changed, result.findings) == (True, ())
