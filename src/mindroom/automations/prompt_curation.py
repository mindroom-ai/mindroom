"""The prompt_curation automation: when an agent's always-loaded files grow too large, ask it to condense them.

The check measures `MEMORY.md` and the agent's `context_files` with the estimate behind `static_prompt_tokens`.
When they exceed the trigger, the prompt asks the agent for a bounded cut in a normal visible run.
Agents told to "clean up" tend to cut most of a file, so verify measures the files again once the run ends and, when
the result is outside the bounds, asks the agent once to re-check its change.
Verify never writes the files back, because other conversations with the same agent may write them meanwhile.
Files are read through no-follow descriptor walks, because worker code writes this workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from mindroom.automations.registry import automation
from mindroom.automations.steps import Ask, AutomationContext, Done
from mindroom.config.automations import MAX_FILE_SHRINK, PromptCurationAutomation
from mindroom.logging_config import get_logger
from mindroom.memory import read_scope_memory_files
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_ENTRYPOINT = "MEMORY.md"
_MEMORY_DIR_PREFIX = "memory/"
_MAX_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class _CurationPlan:
    """What one prompt asked for, and the files as they were when it was posted."""

    agent_name: str
    root: Path
    settings: PromptCurationAutomation
    # Curated files, by workspace-relative path, as they were at fire time; only compared, never written.
    snapshot: Mapping[str, bytes]
    # Every memory/ topic file the scan read at fire time, so verify can tell detail moved from detail deleted.
    memory_tokens: int
    curated: Mapping[str, int]
    upper_tokens: int
    floor_tokens: int

    @property
    def measured_tokens(self) -> int:
        """Return the curated files' total at fire time."""
        return sum(self.curated.values())


@dataclass(frozen=True)
class _CurationResult:
    """How the files compare with the plan once the run ended."""

    tokens_after: int
    changed: bool
    findings: tuple[str, ...] = ()


def _read(root: Path, path: str) -> bytes | None:
    try:
        return read_regular_file_within_root(root, path, max_bytes=_MAX_FILE_BYTES)
    except FileNotFoundError:
        return None


def _tokens(payload: bytes) -> int:
    return estimate_text_tokens(payload.decode("utf-8"))


def _memory_dir_tokens(root: Path, exclude: Iterable[str]) -> int:
    """Return the estimated tokens of the memory/ topic files, except ``exclude``.

    Curated files are excluded even under memory/, so their content is counted once.
    """
    excluded = set(exclude)
    return sum(
        estimate_text_tokens(memory_file.text)
        for memory_file in read_scope_memory_files(root)
        if memory_file.relative_path.startswith(_MEMORY_DIR_PREFIX) and memory_file.relative_path not in excluded
    )


def _curated_paths(config: Config, agent_name: str) -> list[str]:
    paths = [_ENTRYPOINT, *(PurePosixPath(path).as_posix() for path in config.get_agent(agent_name).context_files)]
    return list(dict.fromkeys(paths))


def _plan_curation(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    settings: PromptCurationAutomation,
) -> _CurationPlan | None:
    """Return the plan for a due prompt, or None while the files are within the trigger.

    Raises ``OSError``, ``ValueError``, or ``UnicodeDecodeError`` for a file that cannot be read safely.
    """
    # Automations only run for shared agents, which have no requester-private state.
    root = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=None).file_memory_root
    if root is None:
        return None
    curated_payloads = {
        path: payload for path in _curated_paths(config, agent_name) if (payload := _read(root, path)) is not None
    }
    curated = {path: _tokens(payload) for path, payload in curated_payloads.items()}
    measured = sum(curated.values())
    if measured <= settings.trigger_tokens:
        return None
    return _CurationPlan(
        agent_name=agent_name,
        root=root,
        settings=settings,
        snapshot=curated_payloads,
        memory_tokens=_memory_dir_tokens(root, exclude=curated_payloads),
        curated=curated,
        upper_tokens=round(measured * (1 - settings.min_reduction)),
        floor_tokens=round(measured * (1 - settings.max_reduction)),
    )


def _curation_prompt(config: Config, plan: _CurationPlan) -> str:
    """Render the visible prompt that asks the agent for this plan's cut."""
    return config.render_prompt(
        "PROMPT_CURATION_PROMPT_TEMPLATE",
        measured_tokens=plan.measured_tokens,
        trigger_tokens=plan.settings.trigger_tokens,
        file_sizes=", ".join(f"{path} ({tokens} tokens)" for path, tokens in plan.curated.items()),
        upper_tokens=plan.upper_tokens,
        floor_tokens=plan.floor_tokens,
        max_file_shrink_percent=round(100 * MAX_FILE_SHRINK),
    )


def _read_after_run(root: Path, path: str) -> bytes | None | OSError | ValueError:
    """Read one file after the run, returning a read failure instead of raising it."""
    try:
        return _read(root, path)
    except (OSError, ValueError) as error:
        return error


def _findings(
    plan: _CurationPlan,
    after_run: Mapping[str, bytes | None | OSError | ValueError],
) -> tuple[int, list[str]]:
    """Return the curated files' total after the run and what looks outside the plan's bounds."""
    after: dict[str, int] = {}
    unreadable: list[str] = []
    for path in plan.curated:
        payload = after_run[path]
        if isinstance(payload, Exception):
            unreadable.append(f"{path} cannot be read ({payload})")
            continue
        try:
            after[path] = _tokens(payload or b"")
        except UnicodeDecodeError:
            unreadable.append(f"{path} is no longer valid UTF-8")
    if unreadable:
        # Without every file's size, totals and loss would read an unreadable file as deleted.
        return plan.measured_tokens, unreadable
    # The global loss allowance can hide a small file, such as SOUL.md, being cut whole, so each file has its own bound.
    findings = [
        f"{path} shrank {round(100 * (before - after[path]) / before)}% (more than {round(100 * MAX_FILE_SHRINK)}%)"
        for path, before in plan.curated.items()
        if before and after[path] < before * (1 - MAX_FILE_SHRINK)
    ]
    total = sum(after.values())
    if total < plan.floor_tokens:
        findings.append(f"the files total {total} tokens, below the floor of {plan.floor_tokens}")
    if total >= plan.measured_tokens:
        findings.append(f"the files did not shrink ({total} tokens)")
    if loss := _content_loss(plan, total):
        findings.append(loss)
    return total, findings


def _content_loss(plan: _CurationPlan, curated_tokens: int) -> str | None:
    """Describe memory content deleted rather than moved beyond the allowance, or return None."""
    lost = plan.measured_tokens + plan.memory_tokens - (curated_tokens + _memory_dir_tokens(plan.root, plan.snapshot))
    if lost <= (max_loss := round(plan.settings.max_content_loss * plan.measured_tokens)):
        return None
    return f"about {lost} tokens of memory were deleted rather than moved to memory/ (at most {max_loss})"


def _verify_curation(plan: _CurationPlan) -> _CurationResult:
    """Measure the files after the run and describe anything outside the plan's bounds, without writing them."""
    after_run = {path: _read_after_run(plan.root, path) for path in plan.snapshot}
    if all(after_run[path] == payload for path, payload in plan.snapshot.items()):
        # A run can still have deleted memory/ detail without touching the prompt files.
        loss = _content_loss(plan, plan.measured_tokens)
        return _CurationResult(tokens_after=plan.measured_tokens, changed=False, findings=(loss,) if loss else ())
    tokens_after, findings = _findings(plan, after_run)
    return _CurationResult(tokens_after=tokens_after, changed=True, findings=tuple(findings))


def _curation_notice(config: Config, plan: _CurationPlan, result: _CurationResult) -> str:
    """Return the message posted in the prompt's thread once verify ran; findings ask the agent to re-check."""
    if result.findings:
        return config.render_prompt("PROMPT_CURATION_RECHECK_TEMPLATE", findings="; ".join(result.findings))
    if not result.changed:
        return f"Prompt maintenance changed nothing; the files stay at {plan.measured_tokens} tokens."
    return f"✅ Prompt files condensed from {plan.measured_tokens} to {result.tokens_after} tokens."


@automation("prompt_curation", requires_file_memory=True)
def check_curation(ctx: AutomationContext) -> Ask | None:
    """Return the prompt asking for a cut, or None while the files are within the trigger."""
    if not isinstance(ctx.entry, PromptCurationAutomation):
        msg = f"prompt_curation cannot run the {ctx.entry.name} entry"
        raise ValueError(msg)  # noqa: TRY004 - the runner turns ValueError into a visible notice
    config, agent_name = ctx.config, ctx.agent_name
    plan = _plan_curation(config, ctx.runtime_paths, agent_name, ctx.entry)
    if plan is None:
        return None
    logger.info(
        "Prompt curation asks for a cut",
        agent=agent_name,
        # A field named only "tokens" is redacted as a credential.
        file_tokens=plan.measured_tokens,
        target_tokens=plan.upper_tokens,
        floor_tokens=plan.floor_tokens,
    )
    return Ask(_curation_prompt(config, plan), new_thread=True, then=partial(_after_curation, plan))


def _after_curation(plan: _CurationPlan, config: Config, thread_id: str, _timed_out: bool) -> Ask | Done:
    result = _verify_curation(plan)
    logger.info(
        "Prompt curation verified",
        agent=plan.agent_name,
        changed=result.changed,
        findings=list(result.findings),
        tokens_before=plan.measured_tokens,
        tokens_after=result.tokens_after,
    )
    notice = _curation_notice(config, plan, result)
    if result.findings:
        # A re-check asks the agent once, like the prompt itself; its answer is not verified again.
        return Ask(notice, new_thread=False)
    # A run that stayed within the bounds resolves its thread; an unchanged run leaves it open.
    return Done(notice, resolve=(thread_id,) if result.changed else ())
