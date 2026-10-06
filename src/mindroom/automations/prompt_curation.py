"""The prompt_curation automation: when an agent's always-loaded files grow too large, ask it to condense them.

The check measures `MEMORY.md` and the agent's `context_files` with the estimate behind `static_prompt_tokens`.
When they exceed the trigger, the prompt asks the agent for a bounded cut in a normal visible run.
Agents told to "clean up" tend to cut most of a file, so the bounds are enforced afterwards in code: verify compares
the files with the snapshot taken when the prompt was posted and writes the snapshot back when the run missed them.
Files are read and written through no-follow descriptor walks, because worker code writes this workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.memory import read_scope_memory_files
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.automations import PromptCurationAutomation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_ENTRYPOINT = "MEMORY.md"
_MEMORY_DIR_PREFIX = "memory/"
_MAX_FILE_BYTES = 1 << 20
# Later passes are not asked to go below this share of the trigger.
_STOP_RATIO = 0.9


@dataclass(frozen=True)
class CurationPlan:
    """What one prompt asked for, and the files as they were when it was posted."""

    agent_name: str
    root: Path
    settings: PromptCurationAutomation
    # Curated and protected files, by workspace-relative path, as they were at fire time.
    snapshot: Mapping[str, bytes]
    # memory/ topic files as they were at fire time, so a run that deletes archived detail can be undone.
    memory_snapshot: Mapping[str, str]
    # Every memory/ topic file the scan read at fire time, including ones past the read cap, so verify measures
    # content loss against what it reads afterwards.
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
    violations: tuple[str, ...] = ()

    @property
    def restored(self) -> bool:
        """Return whether verify wrote the snapshot back."""
        return bool(self.violations)


def _read(root: Path, path: str) -> bytes | None:
    try:
        return read_regular_file_within_root(root, path, max_bytes=_MAX_FILE_BYTES)
    except FileNotFoundError:
        return None


def _tokens(payload: bytes) -> int:
    return estimate_text_tokens(payload.decode("utf-8"))


def _memory_dir_files(root: Path, exclude: Iterable[str]) -> dict[str, tuple[str, bool]]:
    """Return memory/ topic files by path as their text and whether it is the whole file, except ``exclude``.

    Curated and protected files are excluded even under memory/, so their content is counted and restored once.
    """
    excluded = set(exclude)
    return {
        memory_file.relative_path: (memory_file.text, memory_file.rewritable)
        for memory_file in read_scope_memory_files(root)
        if memory_file.relative_path.startswith(_MEMORY_DIR_PREFIX) and memory_file.relative_path not in excluded
    }


def _memory_after_run(plan: CurationPlan) -> dict[str, tuple[str, bool]]:
    """Return memory/ topic files after the run, reading any snapshotted file the scan skipped directly."""
    memory_now = _memory_dir_files(plan.root, exclude=plan.snapshot)
    # The scan stops at its byte budget, cutting one file short and skipping the rest, so a snapshotted file it did not
    # read whole is read directly; only an absent one counts as deleted.
    for path in plan.memory_snapshot:
        if path in memory_now and memory_now[path][1]:
            continue
        try:
            payload = _read(plan.root, path)
            if payload is not None:
                memory_now[path] = (payload.decode("utf-8"), True)
        except (OSError, ValueError):
            # Present but not readable whole: never restored, and counted as scanned or empty so the loss guard errs safe.
            memory_now.setdefault(path, ("", False))
    return memory_now


def _restored_archive(archived: str, current: str) -> str:
    """Return a topic file's archived text followed by everything the run left in it, so nothing is lost."""
    if not current:
        return archived
    separator = "" if not archived or archived.endswith("\n") else "\n"
    return archived + separator + current


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

    Raises ``OSError``, ``ValueError``, or ``UnicodeDecodeError`` for a file that cannot be read or rewritten safely.
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
    upper = max(round(measured * (1 - settings.min_reduction)), round(_STOP_RATIO * settings.trigger_tokens))
    memory_files = _memory_dir_files(root, exclude=snapshot)
    return CurationPlan(
        agent_name=agent_name,
        root=root,
        settings=settings,
        snapshot=snapshot,
        # Only whole files can be restored, so only those are snapshotted.
        memory_snapshot={path: text for path, (text, whole) in memory_files.items() if whole},
        memory_tokens=sum(estimate_text_tokens(text) for text, _whole in memory_files.values()),
        curated=curated,
        upper_tokens=upper,
        floor_tokens=upper - round(measured * (settings.max_reduction - settings.min_reduction)),
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


def _violations(
    plan: CurationPlan,
    current: Mapping[str, bytes | None],
    memory_now: Mapping[str, tuple[str, bool]],
) -> list[str]:
    settings = plan.settings
    violations = [
        f"{path} changed but is protected"
        for path in settings.protected_files
        if path in plan.snapshot and current[path] != plan.snapshot[path]
    ]
    after: dict[str, int] = {}
    for path, before in plan.curated.items():
        payload = current[path] or b""
        try:
            after[path] = _tokens(payload)
        except UnicodeDecodeError:
            violations.append(f"{path} is no longer valid UTF-8")
            continue
        if before and after[path] < before * (1 - settings.max_file_shrink):
            shrink = round(100 * (before - after[path]) / before)
            violations.append(f"{path} shrank {shrink}% (more than {round(100 * settings.max_file_shrink)}%)")
    total = sum(after.values())
    if total < plan.floor_tokens:
        violations.append(f"the files total {total} tokens, below the floor of {plan.floor_tokens}")
    if total >= plan.measured_tokens:
        violations.append(f"the files did not shrink ({total} tokens)")
    if loss := _content_loss(plan, total, memory_now):
        violations.append(loss)
    return violations


def _content_loss(plan: CurationPlan, curated_tokens: int, memory_now: Mapping[str, tuple[str, bool]]) -> str | None:
    """Describe memory content deleted instead of moved beyond the allowance, or return None."""
    before = plan.measured_tokens + plan.memory_tokens
    lost = before - (curated_tokens + sum(estimate_text_tokens(text) for text, _whole in memory_now.values()))
    if lost <= (max_loss := round(plan.settings.max_content_loss * plan.measured_tokens)):
        return None
    return f"{lost} tokens of memory were deleted instead of moved to memory/ (at most {max_loss})"


def _restore(root: Path, path: str, payload: bytes) -> None:
    relative = PurePosixPath(path)
    with open_directory_within_root(root, relative.parent.as_posix(), create=True) as directory_fd:
        atomic_write_bytes_at(
            directory_fd,
            relative.name,
            payload,
            file_mode=existing_file_mode(directory_fd, relative.name),
        )


def _read_after_run(root: Path, path: str) -> bytes | None | OSError | ValueError:
    """Read one file after the run, returning a read failure instead of raising it."""
    try:
        return _read(root, path)
    except (OSError, ValueError) as error:
        return error


def _restore_archives(plan: CurationPlan, damaged: Iterable[str], memory_now: Mapping[str, tuple[str, bool]]) -> None:
    for path in damaged:
        restored = _restored_archive(plan.memory_snapshot[path], memory_now.get(path, ("", True))[0])
        _restore(plan.root, path, restored.encode("utf-8"))


def verify_curation(plan: CurationPlan) -> _CurationResult:
    """Check the files against the plan, writing the snapshot back over every changed file when a guard fails."""
    after_run = {path: _read_after_run(plan.root, path) for path in plan.snapshot}
    changed = [path for path, payload in plan.snapshot.items() if after_run[path] != payload]
    memory_now = _memory_after_run(plan)
    # Appending keeps a topic file's archived text; deleting, truncating, or rewriting it does not.
    damaged = [
        path
        for path, text in plan.memory_snapshot.items()
        # A file now past the read cap or no longer valid UTF-8 cannot be compared or restored safely, so it stays.
        if path not in memory_now or (memory_now[path][1] and not memory_now[path][0].startswith(text))
    ]
    if not changed:
        # Untouched prompt files leave only the loss guard: other memory/ edits, such as a memory tool update from
        # another conversation, stay unless detail was deleted.
        if not damaged or (loss := _content_loss(plan, plan.measured_tokens, memory_now)) is None:
            return _CurationResult(tokens_after=plan.measured_tokens, changed=False)
        _restore_archives(plan, damaged, memory_now)
        return _CurationResult(tokens_after=plan.measured_tokens, changed=True, violations=(loss,))
    # A run that replaced a file with a link or grew it past the read cap is restored like any other miss.
    unreadable = [
        f"{path} cannot be read ({error})" for path, error in after_run.items() if isinstance(error, Exception)
    ]
    current = {path: None if isinstance(payload, Exception) else payload for path, payload in after_run.items()}
    if violations := unreadable or _violations(plan, current, memory_now):
        for path in changed:
            _restore(plan.root, path, plan.snapshot[path])
        _restore_archives(plan, damaged, memory_now)
        return _CurationResult(tokens_after=plan.measured_tokens, changed=True, violations=tuple(violations))
    tokens_after = sum(_tokens(current[path] or b"") for path in plan.curated)
    return _CurationResult(tokens_after=tokens_after, changed=True)


def curation_notice(plan: CurationPlan, result: _CurationResult) -> str:
    """Return the one-line notice posted in the prompt's thread once verify ran."""
    if result.restored:
        return f"↩️ Restored the snapshot: {'; '.join(result.violations)}."
    if not result.changed:
        return f"Prompt maintenance changed nothing; the files stay at {plan.measured_tokens} tokens."
    return f"✅ Prompt files condensed from {plan.measured_tokens} to {result.tokens_after} tokens."
