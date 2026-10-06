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
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from mindroom.memory import read_scope_memory_files
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mindroom.config.automations import PromptCurationAutomation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_ENTRYPOINT = "MEMORY.md"
_MEMORY_DIR_PREFIX = "memory/"
_MAX_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class CurationPlan:
    """What one prompt asked for, and the files as they were when it was posted."""

    agent_name: str
    root: Path
    settings: PromptCurationAutomation
    # Curated and protected files, by workspace-relative path, as they were at fire time; only compared, never written.
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

    Curated and protected files are excluded even under memory/, so their content is counted once.
    """
    excluded = set(exclude)
    return sum(
        estimate_text_tokens(memory_file.text)
        for memory_file in read_scope_memory_files(root)
        if memory_file.relative_path.startswith(_MEMORY_DIR_PREFIX) and memory_file.relative_path not in excluded
    )


def _curated_paths(config: Config, agent_name: str, settings: PromptCurationAutomation) -> list[str]:
    paths = [_ENTRYPOINT, *(PurePosixPath(path).as_posix() for path in config.get_agent(agent_name).context_files)]
    return [path for path in dict.fromkeys(paths) if path not in settings.protected_files]


def plan_curation(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    settings: PromptCurationAutomation,
) -> CurationPlan | None:
    """Return the plan for a due prompt, or None while the files are within the trigger.

    Raises ``OSError``, ``ValueError``, or ``UnicodeDecodeError`` for a file that cannot be read safely.
    """
    # Automations only run for shared agents, which have no requester-private state.
    root = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=None).file_memory_root
    if root is None:
        return None
    curated_payloads = {
        path: payload
        for path in _curated_paths(config, agent_name, settings)
        if (payload := _read(root, path)) is not None
    }
    curated = {path: _tokens(payload) for path, payload in curated_payloads.items()}
    measured = sum(curated.values())
    if measured <= settings.trigger_tokens:
        return None
    protected_payloads = {
        path: payload for path in settings.protected_files if (payload := _read(root, path)) is not None
    }
    snapshot = {**curated_payloads, **protected_payloads}
    return CurationPlan(
        agent_name=agent_name,
        root=root,
        settings=settings,
        snapshot=snapshot,
        memory_tokens=_memory_dir_tokens(root, exclude=snapshot),
        curated=curated,
        upper_tokens=round(measured * (1 - settings.min_reduction)),
        floor_tokens=round(measured * (1 - settings.max_reduction)),
    )


def curation_prompt(config: Config, plan: CurationPlan) -> str:
    """Render the visible prompt that asks the agent for this plan's cut."""
    settings = plan.settings
    protected = ", ".join(settings.protected_files)
    return config.render_prompt(
        "PROMPT_CURATION_PROMPT_TEMPLATE",
        measured_tokens=plan.measured_tokens,
        trigger_tokens=settings.trigger_tokens,
        file_sizes=", ".join(f"{path} ({tokens} tokens)" for path, tokens in plan.curated.items()),
        upper_tokens=plan.upper_tokens,
        floor_tokens=plan.floor_tokens,
        max_file_shrink_percent=round(100 * settings.max_file_shrink),
        protected_line=f"6. Leave {protected} unchanged.\n" if protected else "",
    )


def _read_after_run(root: Path, path: str) -> bytes | None | OSError | ValueError:
    """Read one file after the run, returning a read failure instead of raising it."""
    try:
        return _read(root, path)
    except (OSError, ValueError) as error:
        return error


def _findings(
    plan: CurationPlan,
    after_run: Mapping[str, bytes | None | OSError | ValueError],
) -> tuple[int, list[str]]:
    """Return the curated files' total after the run and what looks outside the plan's bounds."""
    settings = plan.settings
    findings = [
        f"{path} changed although it is protected"
        for path in settings.protected_files
        if path in plan.snapshot and after_run[path] != plan.snapshot[path]
    ]
    after: dict[str, int] = {}
    for path, before in plan.curated.items():
        payload = after_run[path]
        if isinstance(payload, Exception):
            findings.append(f"{path} cannot be read ({payload})")
            continue
        try:
            after[path] = _tokens(payload or b"")
        except UnicodeDecodeError:
            findings.append(f"{path} is no longer valid UTF-8")
            continue
        if before and after[path] < before * (1 - settings.max_file_shrink):
            shrink = round(100 * (before - after[path]) / before)
            findings.append(f"{path} shrank {shrink}% (more than {round(100 * settings.max_file_shrink)}%)")
    total = sum(after.values())
    if total < plan.floor_tokens:
        findings.append(f"the files total {total} tokens, below the floor of {plan.floor_tokens}")
    if total >= plan.measured_tokens:
        findings.append(f"the files did not shrink ({total} tokens)")
    memory_after = _memory_dir_tokens(plan.root, exclude=plan.snapshot)
    lost = plan.measured_tokens + plan.memory_tokens - (total + memory_after)
    if lost > (max_loss := round(settings.max_content_loss * plan.measured_tokens)):
        findings.append(f"about {lost} tokens of memory were deleted rather than moved to memory/ (at most {max_loss})")
    return total, findings


def verify_curation(plan: CurationPlan) -> _CurationResult:
    """Measure the files after the run and describe anything outside the plan's bounds, without writing them."""
    after_run = {path: _read_after_run(plan.root, path) for path in plan.snapshot}
    if all(after_run[path] == payload for path, payload in plan.snapshot.items()):
        return _CurationResult(tokens_after=plan.measured_tokens, changed=False)
    tokens_after, findings = _findings(plan, after_run)
    return _CurationResult(tokens_after=tokens_after, changed=True, findings=tuple(findings))


def curation_notice(config: Config, plan: CurationPlan, result: _CurationResult) -> str:
    """Return the message posted in the prompt's thread once verify ran; findings ask the agent to re-check."""
    if result.findings:
        return config.render_prompt(
            "PROMPT_CURATION_RECHECK_TEMPLATE",
            measured_tokens=plan.measured_tokens,
            tokens_after=result.tokens_after,
            findings="; ".join(result.findings),
        )
    if not result.changed:
        return f"Prompt maintenance changed nothing; the files stay at {plan.measured_tokens} tokens."
    return f"✅ Prompt files condensed from {plan.measured_tokens} to {result.tokens_after} tokens."
