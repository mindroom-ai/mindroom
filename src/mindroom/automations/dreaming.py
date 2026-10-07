"""The dreaming automation: reconcile memory/ with what changed since it was last reconciled.

The check lists inputs that changed since a run last handled them: exported conversations, past daily notes, and the
workspace files memory cites.
When any is due, the agent edits a staging copy of memory/ in a visible run, code validates the staging, and a second
run in a thread of its own reviews the proposal against its sources.
Code applies an approved proposal only when memory did not change during the runs, and records progress only after a
run that applied or needed no change, so every other outcome leaves its inputs due.
Workspace files are read and written through no-follow descriptor walks, because worker code writes this workspace.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import stat
from bisect import bisect_left
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from mindroom.automations.steps import Ask, Done
from mindroom.automations.threads import automation_threads, automations_tracking_root
from mindroom.logging_config import get_logger
from mindroom.memory import refresh_agent_memory_search, write_scope_markdown_file
from mindroom.path_confinement import (
    open_directory_within_root,
    read_regular_file_within_root,
    write_file_within_root,
)
from mindroom.runtime_resolution import resolve_agent_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_RUNS_DIR = ".mindroom/dreaming/runs"
_MEMORY_DIR = "memory"
_EXPORTS_DIR = "thread_exports"
_KNOWLEDGE_DIR = "knowledge"
_DAILY_NOTE = re.compile(r"memory/(\d{4}-\d{2}-\d{2})\.md")
# A workspace path memory cites, such as `knowledge/docs/Meeting Notes.md` or thread_exports/<room>/<thread>.yaml: the
# whole of a code span that starts with one, or a bare path up to whitespace or markup; either without a trailing
# anchor, line number, or punctuation.
_CODE_SPAN_CITATION = re.compile(r"`((?:knowledge|thread_exports)/[^`\n]+)`")
_CITATION = re.compile(r"(?<![\w./-])((?:knowledge|thread_exports)/[^\s`'\"<>()\[\]{}|*#]+)")
_CITATION_SUFFIX = re.compile(r"(?::\d+(?:[-:]\d+)*)?[.,;:!?]*$")
_MAX_INPUTS = 40
# Enabling the automation starts from recent history instead of the whole archive.
_SEED_AGE = timedelta(days=7)
_KEPT_RUNS = 30
_MAX_FILE_BYTES = 1 << 20
# The exporter caps a room's index.json at 8 MiB.
_MAX_INDEX_BYTES = 8 << 20
# Removed lines are deleted lines found nowhere in the staged files; ordinary corrections fit the absolute allowances.
_MAX_REMOVED_FRACTION = 0.08
_MIN_REMOVED_ALLOWANCE = 10
_MAX_FILE_REMOVED_FRACTION = 0.5
_MIN_FILE_REMOVED_ALLOWANCE = 2
_DONE_LINE = "DREAM: DONE"
# Exactly one of the three verdict forms, with notes or a reason after an em dash, en dash, or hyphen; anything else,
# such as "APPROVE-WITH-CHANGES" or "APPROVE once fixed", is a rejection.
_VERDICT = re.compile(
    r"VERDICT:\s*(?:"
    r"(?P<approve>APPROVE)\.?"
    r"|(?P<notes>APPROVE[- ]WITH[- ]NOTES)(?:\s*[\u2014\u2013-]+\s*(?P<detail>.*))?"
    r"|(?P<reject>REJECT)(?:\s*[\u2014\u2013-]+\s*(?P<reason>.*))?"
    r")",
)
_MISSING = "missing"

# An input's version: its modification time and size, or missing for a cited file that no longer exists.
type _Version = tuple[int, int] | Literal["missing"]
type _Outcome = Literal["applied", "unchanged", "incomplete", "invalid", "rejected", "conflict"]


@dataclass
class _State:
    """Progress kept in primary storage: what was handled and which proposals are still unapplied."""

    reviewed: dict[str, _Version] = field(default_factory=dict)
    # The versions the last run had on its agenda, so a run that applied nothing is not repeated on the same evidence.
    attempted: dict[str, _Version] = field(default_factory=dict)
    # The oldest unapplied proposal, whose changes a rejected successor may have dropped, and the newest, whose review
    # explains the latest failure; both lead the next agenda until a run applies or needs no change.
    pending_run: str | None = None
    latest_run: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class _Input:
    """One changed workspace file the run reviews."""

    path: str
    kind: Literal["conversation", "daily_note", "source"]
    version: _Version
    # When its content last changed, in nanoseconds: a conversation's last message, otherwise the file's mtime.
    changed_ns: int = 0
    # For a cited source, the path as memory spells it and the memory files that cite it.
    cited_as: str = ""
    cited_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Tree:
    """The Markdown files under one memory/ directory, and every entry that could not be read as one."""

    files: dict[str, bytes]
    versions: dict[str, tuple[int, int]]
    rejected: tuple[str, ...]


@dataclass(frozen=True)
class _Run:
    """One dreaming run, as the check saw the workspace when it fired."""

    agent_name: str
    runtime_paths: RuntimePaths
    root: Path
    run_id: str
    # In-scope memory files by workspace-relative path, as they were at fire time; only compared, never written back.
    snapshot: Mapping[str, bytes]
    # Memory paths the run may not write: today's daily note, context files, and files that cannot be read safely.
    excluded: frozenset[str]
    due: tuple[_Input, ...]
    # Every input's version at fire time, due or not.
    inputs: Mapping[str, _Version]
    # Unapplied proposals the run carries forward, oldest first.
    carried: tuple[str, ...]

    @property
    def run_dir(self) -> str:
        """Return the run's workspace-relative artifact directory."""
        return f"{_RUNS_DIR}/{self.run_id}"


@dataclass(frozen=True)
class _Proposal:
    """A validated proposal: the staged bytes of each changed or created file, and the files it deletes."""

    changed: Mapping[str, bytes]
    deleted: tuple[str, ...]
    dream_thread: str


def _state_root(runtime_paths: RuntimePaths, agent_name: str) -> Path:
    return automations_tracking_root(runtime_paths) / agent_name


def _load_state(runtime_paths: RuntimePaths, agent_name: str) -> _State:
    try:
        payload = json.loads(
            read_regular_file_within_root(_state_root(runtime_paths, agent_name), "dreaming.json"),
        )
    except FileNotFoundError:
        return _State()
    return _State(
        reviewed=_versions(payload["reviewed"]),
        attempted=_versions(payload["attempted"]),
        pending_run=payload["pending_run"],
        latest_run=payload["latest_run"],
        notes=payload["notes"],
    )


def _versions(payload: dict[str, Any]) -> dict[str, _Version]:
    return {path: version if version == _MISSING else (version[0], version[1]) for path, version in payload.items()}


def _update_state(runtime_paths: RuntimePaths, agent_name: str, change: Callable[[_State], None]) -> None:
    # An agent's checks and steps never overlap, so nothing else writes its state meanwhile.
    state = _load_state(runtime_paths, agent_name)
    change(state)
    _save_state(runtime_paths, agent_name, state)


def _save_state(runtime_paths: RuntimePaths, agent_name: str, state: _State) -> None:
    payload = {
        "reviewed": state.reviewed,
        "attempted": state.attempted,
        "pending_run": state.pending_run,
        "latest_run": state.latest_run,
        "notes": state.notes,
    }
    write_file_within_root(
        _state_root(runtime_paths, agent_name),
        "dreaming.json",
        json.dumps(payload, indent=1, sort_keys=True).encode(),
    )


def _walk(directory_fd: int, prefix: str, found: dict[str, os.stat_result], rejected: list[str]) -> None:
    """Collect regular files below a pinned directory, recording every link or special entry instead of following it.

    An entry that disappears during the walk, such as another writer's temporary file, is skipped.
    """
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            path = f"{prefix}/{entry.name}"
            try:
                status = entry.stat(follow_symlinks=False)
                child = (
                    os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    if stat.S_ISDIR(status.st_mode)
                    else None
                )
            except FileNotFoundError:
                continue
            if child is not None:
                try:
                    _walk(child, path, found, rejected)
                finally:
                    os.close(child)
            elif stat.S_ISREG(status.st_mode):
                found[path] = status
            else:
                rejected.append(path)


def _regular_files(root: Path, directory: str) -> dict[str, os.stat_result]:
    """Return the regular files below ``directory`` by root-relative path, or none when it does not exist."""
    found: dict[str, os.stat_result] = {}
    try:
        with open_directory_within_root(root, directory) as directory_fd:
            _walk(directory_fd, directory, found, [])
    except FileNotFoundError:
        pass
    return found


def _read_memory_tree(root: Path, base: str) -> _Tree:
    """Read every Markdown file under ``base``/memory, by path relative to ``base``.

    Raises ``OSError`` when ``base`` or memory/ is a link or cannot be opened.
    """
    files: dict[str, bytes] = {}
    versions: dict[str, tuple[int, int]] = {}
    found: dict[str, os.stat_result] = {}
    rejected: list[str] = []
    with open_directory_within_root(root, base or Path()) as base_fd:
        try:
            with open_directory_within_root(base_fd, _MEMORY_DIR) as memory_fd:
                _walk(memory_fd, _MEMORY_DIR, found, rejected)
        except FileNotFoundError:
            return _Tree({}, {}, ())
        for path in sorted(found):
            try:
                payload = _read_markdown(base_fd, path)
            except FileNotFoundError:
                # Deleted since the walk listed it.
                continue
            except ValueError:
                payload = None
            if payload is None:
                rejected.append(path)
                continue
            files[path] = payload
            versions[path] = (found[path].st_mtime_ns, found[path].st_size)
    return _Tree(files, versions, tuple(sorted(rejected)))


def _read_markdown(base_fd: int, path: str) -> bytes | None:
    """Return one Markdown file's bytes, or None when it is not Markdown or not UTF-8.

    Raises ``ValueError`` for a file over the size cap or not regular, and ``FileNotFoundError`` for one gone.
    """
    if not path.endswith(".md"):
        return None
    payload = read_regular_file_within_root(base_fd, path, max_bytes=_MAX_FILE_BYTES)
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return payload


def _version(root: Path, path: str) -> _Version | None:
    """Return a cited workspace file's version, missing when it is gone, or None when it is not a regular file."""
    parent, name = path.rsplit("/", 1)
    try:
        with open_directory_within_root(root, parent) as parent_fd:
            status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        # A link or a file on the way is not a path memory can cite.
        return None
    return (status.st_mtime_ns, status.st_size) if stat.S_ISREG(status.st_mode) else None


def _thread_id(export_path: str) -> str:
    return unquote(PurePosixPath(export_path).stem)


def _knowledge_aliases(root: Path) -> dict[str, str]:
    """Map each entry of the workspace's `knowledge/` to the workspace-relative path it shows, reading links unfollowed.

    MindRoom links each workspace-local knowledge base there; a link that leaves the workspace is skipped.
    """
    workspace = root.resolve()
    knowledge_root = workspace / _KNOWLEDGE_DIR
    aliases: dict[str, str] = {}
    try:
        with open_directory_within_root(root, _KNOWLEDGE_DIR) as knowledge_fd, os.scandir(knowledge_fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    aliases[entry.name] = f"{_KNOWLEDGE_DIR}/{entry.name}"
                elif entry.is_symlink():
                    target = Path(os.path.normpath(knowledge_root / os.readlink(entry.name, dir_fd=knowledge_fd)))
                    if target.is_relative_to(workspace) and target != workspace:
                        aliases[entry.name] = target.relative_to(workspace).as_posix()
    except FileNotFoundError:
        pass
    return aliases


def _canonical_citation(cited: str, aliases: Mapping[str, str]) -> str | None:
    parts = cited.split("/")
    if "" in parts or "." in parts or ".." in parts:
        return None
    if parts[0] == _EXPORTS_DIR:
        return cited if len(parts) > 1 else None
    if len(parts) == 2 or parts[1] not in aliases:
        # A file directly under knowledge/, or one in a base no longer assigned to the agent, whose link is gone.
        return cited
    return "/".join([aliases[parts[1]], *parts[2:]])


def _citations(snapshot: Mapping[str, bytes]) -> dict[str, list[str]]:
    """Return each cited workspace path, as memory spells it, with the memory files that cite it."""
    cited: dict[str, set[str]] = {}
    for path, payload in snapshot.items():
        text = payload.decode()
        spans = [match.group(1).partition("#")[0].strip() for match in _CODE_SPAN_CITATION.finditer(text)]
        bare = [match.group(1) for match in _CITATION.finditer(_CODE_SPAN_CITATION.sub(" ", text))]
        for spelled in spans + bare:
            cited.setdefault(_CITATION_SUFFIX.sub("", spelled), set()).add(path)
    return {spelled: sorted(paths) for spelled, paths in cited.items()}


def _last_messages(root: Path, exports: Mapping[str, os.stat_result]) -> dict[str, int]:
    """Return each indexed thread export's last message time in nanoseconds, from its room's index.json.

    An export written today can hold a conversation that ended months ago, so its mtime says nothing about its age.
    """
    times: dict[str, int] = {}
    for index_path in (path for path in exports if path.endswith("/index.json")):
        try:
            index = json.loads(read_regular_file_within_root(root, index_path, max_bytes=_MAX_INDEX_BYTES))
            entries = index["threads"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        room = index_path.rsplit("/", 1)[0]
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict) and isinstance(name := entry.get("file"), str):
                timestamp = entry.get("last_timestamp")
                if isinstance(timestamp, int) and not isinstance(timestamp, bool):
                    times[f"{room}/{name}"] = timestamp * 1_000_000
    return times


def _collect_inputs(
    root: Path,
    snapshot: Mapping[str, bytes],
    versions: Mapping[str, tuple[int, int]],
    today: str,
    automation_thread_ids: set[str],
) -> dict[str, _Input]:
    inputs: dict[str, _Input] = {}
    exports = _regular_files(root, _EXPORTS_DIR)
    last_messages = _last_messages(root, exports)
    for path, status in sorted(exports.items()):
        if path.endswith(".yaml") and _thread_id(path) not in automation_thread_ids:
            version = (status.st_mtime_ns, status.st_size)
            inputs[path] = _Input(path, "conversation", version, last_messages.get(path, status.st_mtime_ns))
    for path, version in versions.items():
        if path in snapshot and (match := _DAILY_NOTE.fullmatch(path)) and match.group(1) < today:
            inputs[path] = _Input(path, "daily_note", version, version[0])
    aliases = _knowledge_aliases(root)
    for spelled, cited_by in sorted(_citations(snapshot).items()):
        path = _canonical_citation(spelled, aliases)
        if (
            path is None
            or path in inputs
            or (path.startswith(f"{_EXPORTS_DIR}/") and _thread_id(path) in automation_thread_ids)
        ):
            continue
        if (version := _version(root, path)) is not None:
            changed_ns = 0 if version == _MISSING else version[0]
            inputs[path] = _Input(path, "source", version, changed_ns, cited_as=spelled, cited_by=tuple(cited_by))
    return inputs


def _unseen_and_old(inputs: Iterable[_Input], reviewed: Mapping[str, _Version], now: datetime) -> dict[str, _Version]:
    """Return inputs no run has reviewed whose content is older than the seed age, to count as already handled.

    Enabling the automation, or thread exports later, then starts from recent history instead of the whole archive.
    """
    cutoff = (now - _SEED_AGE).timestamp() * 1e9
    return {
        item.path: item.version
        for item in inputs
        if item.path not in reviewed and item.version != _MISSING and item.changed_ns < cutoff
    }


def _order(item: _Input) -> tuple[int, int, str]:
    # Dead citations have no time, so they come first; everything else oldest first.
    return (0, 0, item.path) if item.version == _MISSING else (1, item.changed_ns, item.path)


def _prune_runs(root: Path, keep: tuple[str, ...]) -> None:
    """Keep the newest run directories and the unapplied ones; staging goes with the rest."""
    try:
        with open_directory_within_root(root, _RUNS_DIR) as runs_fd:
            with os.scandir(runs_fd) as entries:
                names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
            for name in names[:-_KEPT_RUNS]:
                if name not in keep:
                    shutil.rmtree(name, dir_fd=runs_fd)
    except FileNotFoundError:
        return


def _agenda(run: _Run, state: _State, waiting: int) -> str:
    lines = ["# Dreaming agenda", "", f"Run `{run.run_id}`; paths are relative to your workspace."]
    if run.carried:
        lines += [
            "",
            "## 0. Previous proposals",
            "",
            "These proposals were never applied; read each one's `proposal.patch`, `report.md`, and `verdict.md`:",
            "",
            *(f"- `{_RUNS_DIR}/{run_id}/`" for run_id in run.carried),
        ]
    if state.notes:
        lines += ["", "## Notes from the last review", "", state.notes]
    sections = (
        ("conversation", "## 1. New or updated conversations"),
        ("daily_note", "## 1. New daily notes"),
    )
    for kind, heading in sections:
        items = [f"- `{item.path}`" for item in run.due if item.kind == kind]
        if items:
            lines += ["", heading, "", *items]
    changed = [item for item in run.due if item.kind == "source" and item.version != _MISSING]
    dead = [item for item in run.due if item.kind == "source" and item.version == _MISSING]
    for heading, items, suffix in (
        ("## 2. Changed cited sources", changed, ""),
        ("## 2. Dead citations", dead, ": no longer exists"),
    ):
        if items:
            lines += ["", heading, ""]
            lines += [
                f"- `{item.cited_as}`, cited by {', '.join(f'`{path}`' for path in item.cited_by)}{suffix}"
                for item in items
            ]
    if waiting:
        lines += ["", f"{waiting} more changed inputs wait for later runs."]
    return "\n".join(lines) + "\n"


def _context_files(config: Config, agent_name: str) -> set[str]:
    return {PurePosixPath(path).as_posix() for path in config.get_agent(agent_name).context_files}


def check_dreaming(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> Ask | None:
    """Return the dream prompt when an input changed or a proposal is unapplied, or None.

    Raises ``OSError`` or ``ValueError`` when memory or the state cannot be read safely.
    """
    # Automations only run for shared agents, whose file memory is the workspace root.
    runtime = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=None)
    root = runtime.file_memory_root
    # A workspace appears with the agent's first turn.
    if root is None or not root.is_dir():
        return None
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
    tree = _read_memory_tree(root, "")
    context_files = _context_files(config, agent_name)
    excluded = frozenset({f"{_MEMORY_DIR}/{today}.md", *context_files, *tree.rejected})
    snapshot = {path: payload for path, payload in tree.files.items() if path not in excluded}
    state = _load_state(runtime_paths, agent_name)
    inputs = _collect_inputs(root, snapshot, tree.versions, today, automation_threads(runtime_paths))
    if seeded := _unseen_and_old(inputs.values(), state.reviewed, now):
        state.reviewed.update(seeded)
        _save_state(runtime_paths, agent_name, state)
    due = sorted((item for item in inputs.values() if state.reviewed.get(item.path) != item.version), key=_order)
    # Only a conversation or daily note the last run did not see starts a run; changed sources and unapplied
    # proposals join it, so an idle agent costs nothing and a failed run is not repeated on the same evidence.
    if not any(item.kind != "source" and state.attempted.get(item.path) != item.version for item in due):
        return None
    carried = tuple(dict.fromkeys(run_id for run_id in (state.pending_run, state.latest_run) if run_id is not None))
    run = _Run(
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        root=root,
        run_id=now.strftime("%Y%m%dT%H%M%S%fZ"),
        snapshot=snapshot,
        excluded=excluded,
        due=tuple(due[:_MAX_INPUTS]),
        inputs={path: item.version for path, item in inputs.items()},
        carried=carried,
    )
    _prune_runs(root, keep=carried)
    write_file_within_root(root, f"{run.run_dir}/agenda.md", _agenda(run, state, len(due) - len(run.due)).encode())
    with open_directory_within_root(root, f"{run.run_dir}/staging/{_MEMORY_DIR}", create=True):
        pass
    for path, payload in snapshot.items():
        write_file_within_root(root, f"{run.run_dir}/staging/{path}", payload)
    logger.info("Dreaming starts a run", agent=agent_name, run=run.run_id, inputs=len(run.due))
    return Ask(
        config.render_prompt(
            "DREAMING_PROMPT_TEMPLATE",
            input_count=len(run.due),
            agenda_path=f"{run.run_dir}/agenda.md",
            staging_path=f"{run.run_dir}/staging/{_MEMORY_DIR}",
            report_path=f"{run.run_dir}/report.md",
        ),
        new_thread=True,
        then=partial(_after_dream, run),
    )


def _read_optional(root: Path, path: str) -> str | None:
    try:
        return read_regular_file_within_root(root, path, max_bytes=_MAX_FILE_BYTES).decode("utf-8", errors="replace")
    except (FileNotFoundError, ValueError):
        return None


def _last_line(text: str | None) -> str:
    """Return the last non-blank line without the code, emphasis, list, or quote marks a model may add to it."""
    lines = [
        stripped
        for line in (text or "").splitlines()
        if (stripped := line.replace("`", "").replace("*", "").strip().lstrip("->").strip())
    ]
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class _Deletion:
    path: str
    line: int
    text: str
    kept: bool
    before: str | None
    after: str | None


def _deletions(run: _Run, staged: Mapping[str, bytes]) -> list[_Deletion]:
    """List every deleted non-blank line, kept when a staged line is it or starts with it.

    That covers a line moved to another file, a duplicate removed, and a line marked superseded by appending to it.
    """
    staged_lines = sorted({line.strip() for payload in staged.values() for line in payload.decode().splitlines()})

    def kept(text: str) -> bool:
        # Lines that start with ``text`` sort right at or after it.
        index = bisect_left(staged_lines, text)
        return index < len(staged_lines) and staged_lines[index].startswith(text)

    deletions: list[_Deletion] = []
    for path, before in run.snapshot.items():
        old = before.decode().splitlines()
        new = staged[path].decode().splitlines() if path in staged else []
        if path in staged and staged[path] == before:
            continue
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for tag, start, end, _new_start, _new_end in matcher.get_opcodes():
            if tag not in {"replace", "delete"}:
                continue
            for index in range(start, end):
                text = old[index].strip()
                if text:
                    deletions.append(
                        _Deletion(
                            path=path,
                            line=index + 1,
                            text=old[index],
                            kept=kept(text),
                            before=old[index - 1] if index > 0 else None,
                            after=old[index + 1] if index + 1 < len(old) else None,
                        ),
                    )
    return deletions


def _budget_findings(run: _Run, deletions: list[_Deletion]) -> list[str]:
    def nonblank(payload: bytes) -> int:
        return sum(1 for line in payload.decode().splitlines() if line.strip())

    removed = [deletion for deletion in deletions if not deletion.kept]
    findings: list[str] = []
    allowance = max(_MIN_REMOVED_ALLOWANCE, round(_MAX_REMOVED_FRACTION * sum(map(nonblank, run.snapshot.values()))))
    if len(removed) > allowance:
        findings.append(f"{len(removed)} lines are deleted and found nowhere else (at most {allowance})")
    for path, payload in run.snapshot.items():
        count = sum(1 for deletion in removed if deletion.path == path)
        if count > max(_MIN_FILE_REMOVED_ALLOWANCE, _MAX_FILE_REMOVED_FRACTION * nonblank(payload)):
            findings.append(f"{path} loses {count} of its {nonblank(payload)} lines without keeping them elsewhere")
    return findings


def _patch(run: _Run, proposal: _Proposal) -> str:
    chunks: list[str] = []
    for path in sorted({*proposal.changed, *proposal.deleted}):
        before = run.snapshot.get(path)
        after = proposal.changed.get(path)
        chunks.extend(
            difflib.unified_diff(
                before.decode().splitlines(keepends=True) if before is not None else [],
                after.decode().splitlines(keepends=True) if after is not None else [],
                fromfile=f"a/{path}" if before is not None else "/dev/null",
                tofile=f"b/{path}" if after is not None else "/dev/null",
            ),
        )
    # A last line without a newline keeps the marker `git apply` needs to restore the bytes exactly.
    return "".join(chunk if chunk.endswith("\n") else f"{chunk}\n\\ No newline at end of file\n" for chunk in chunks)


def _deletion_report(deletions: list[_Deletion]) -> str:
    lines: list[str] = []
    for deletion in deletions:
        lines.append(f"{deletion.path}:{deletion.line} ({'kept elsewhere' if deletion.kept else 'removed'})")
        if deletion.before is not None:
            lines.append(f"  {deletion.line - 1:>5}  {deletion.before}")
        lines.append(f"- {deletion.line:>5}  {deletion.text}")
        if deletion.after is not None:
            lines.append(f"  {deletion.line + 1:>5}  {deletion.after}")
        lines.append("")
    return "\n".join(lines)


def _after_dream(
    run: _Run,
    config: Config,
    thread_id: str,
    timed_out: bool,
    *,
    rechecked: bool = False,
) -> Ask | Done:
    """Validate the dream's staging, then ask for a review in a thread of its own."""
    if timed_out:
        return _end(run, "incomplete", "⚠️ Dreaming stopped: the run did not finish within an hour.")
    if _last_line(_read_optional(run.root, f"{run.run_dir}/report.md")) != _DONE_LINE:
        reason = f"the run's report does not end with `{_DONE_LINE}`, so its agenda was not finished"
        return _end(run, "incomplete", f"⚠️ Dreaming stopped: {reason}.")
    staged = _read_memory_tree(run.root, f"{run.run_dir}/staging")
    deletions = _deletions(run, staged.files)
    findings = [f"{name} is outside memory/, which is all this run may change" for name in _outside_memory(run)]
    findings += [f"{path} cannot be staged, only Markdown under memory/ can" for path in staged.rejected]
    findings += [f"{path} is outside what this run may change" for path in staged.files if path in run.excluded]
    findings += _budget_findings(run, deletions)
    if findings:
        if rechecked:
            return _end(run, "invalid", f"⚠️ Dreaming stopped: {'; '.join(findings)}.")
        return Ask(
            config.render_prompt(
                "DREAMING_RECHECK_TEMPLATE",
                findings="; ".join(findings),
                staging_path=f"{run.run_dir}/staging/{_MEMORY_DIR}",
                report_path=f"{run.run_dir}/report.md",
            ),
            new_thread=False,
            then=partial(_after_dream, run, rechecked=True),
        )
    proposal = _Proposal(
        changed={path: payload for path, payload in staged.files.items() if run.snapshot.get(path) != payload},
        deleted=tuple(path for path in run.snapshot if path not in staged.files),
        dream_thread=thread_id,
    )
    if not proposal.changed and not proposal.deleted:
        return _end_without_change(run, thread_id)
    write_file_within_root(run.root, f"{run.run_dir}/proposal.patch", _patch(run, proposal).encode())
    write_file_within_root(run.root, f"{run.run_dir}/deleted.txt", _deletion_report(deletions).encode())
    # Only the review may write a verdict, so a file the dream left there never counts as one.
    _remove(run.root, f"{run.run_dir}/verdict.md")
    # Pending from now on, so a restart before the review finishes still carries the proposal forward.
    _update_state(run.runtime_paths, run.agent_name, partial(_keep_pending, run=run))
    return Ask(
        config.render_prompt(
            "DREAMING_VERIFY_TEMPLATE",
            patch_path=f"{run.run_dir}/proposal.patch",
            changed_files=len(proposal.changed) + len(proposal.deleted),
            agenda_path=f"{run.run_dir}/agenda.md",
            report_path=f"{run.run_dir}/report.md",
            deleted_path=f"{run.run_dir}/deleted.txt",
            deleted_lines=len(deletions),
            removed_lines=sum(1 for deletion in deletions if not deletion.kept),
            verdict_path=f"{run.run_dir}/verdict.md",
        ),
        new_thread=True,
        then=partial(_after_verify, run, proposal),
    )


def _outside_memory(run: _Run) -> list[str]:
    """Return what the run staged beside memory/, such as an edited MEMORY.md."""
    with (
        open_directory_within_root(run.root, f"{run.run_dir}/staging") as staging_fd,
        os.scandir(staging_fd) as entries,
    ):
        return sorted(entry.name for entry in entries if entry.name != _MEMORY_DIR)


def _end_without_change(run: _Run, thread_id: str) -> Done:
    """Acknowledge the inputs of a run that changed nothing, unless memory changed under it."""
    if not _unchanged_since_fire(run):
        return _end(run, "conflict", "⚠️ Memory changed during the run, so its review is repeated next time.")
    return _end(
        run,
        "unchanged",
        f"Dreaming changed nothing (inputs reviewed: {len(run.due)}).",
        resolve=(thread_id,),
    )


def _unchanged_since_fire(run: _Run) -> bool:
    """Return whether every in-scope memory path and its bytes still match the snapshot.

    This is batch conflict rejection, not a transaction: a memory write landing between this check and the writes
    that follow it is not detected. MindRoom's memory writers, including the memory tool's own updates, share no lock,
    so closing that window would mean serializing all of them; the automation accepts it.
    """
    current = _read_memory_tree(run.root, "")
    return {path: payload for path, payload in current.files.items() if path not in run.excluded} == dict(run.snapshot)


def _after_verify(run: _Run, proposal: _Proposal, config: Config, thread_id: str, timed_out: bool) -> Done:
    """Apply an approved proposal when memory did not change since the run fired."""
    if timed_out:
        verdict, detail = "REJECT", "the review did not finish within an hour"
    elif match := _VERDICT.fullmatch(_last_line(_read_optional(run.root, f"{run.run_dir}/verdict.md"))):
        verdict = "APPROVE" if match["approve"] else "APPROVE-WITH-NOTES" if match["notes"] else "REJECT"
        detail = (match["detail"] or match["reason"] or "").strip().rstrip(".")
    else:
        verdict, detail = "REJECT", "the review did not end with one of the three verdict lines"
    if verdict == "REJECT":
        return _end(
            run,
            "rejected",
            f"⚠️ Dreaming was not applied: {detail or 'the review rejected it'}. "
            "The next run carries the proposal forward.",
        )
    # A config reload during the run can make a proposed file a context file, which the automation never writes.
    if not _unchanged_since_fire(run) or {*proposal.changed, *proposal.deleted} & _context_files(
        config,
        run.agent_name,
    ):
        return _end(
            run,
            "conflict",
            "⚠️ Memory changed during the run, so the approved proposal was not applied. "
            "The next run carries it forward.",
        )
    for path, payload in proposal.changed.items():
        write_scope_markdown_file(run.root, Path(path), payload)
    for path in proposal.deleted:
        _remove(run.root, path)
    notes = detail if verdict == "APPROVE-WITH-NOTES" and detail else None
    summary = (
        f"✅ Dreaming applied the reviewed proposal "
        f"(files written: {len(proposal.changed)}, removed: {len(proposal.deleted)})."
    )
    return _end(
        run,
        "applied",
        f"{summary} Notes for the next run: {notes}" if notes else summary,
        resolve=(proposal.dream_thread, thread_id),
        notes=notes,
        applied=tuple(proposal.changed),
        # The refresh is scheduled on the event loop, which this step does not run on.
        on_loop=partial(refresh_agent_memory_search, run.agent_name, run.root, config, run.runtime_paths),
    )


def _remove(root: Path, path: str) -> None:
    """Delete one workspace file without following a link, if it exists."""
    parent, name = path.rsplit("/", 1)
    with open_directory_within_root(root, parent) as parent_fd, suppress(FileNotFoundError):
        os.unlink(name, dir_fd=parent_fd)


def _keep_pending(state: _State, run: _Run) -> None:
    """Carry the run's proposal into the next agenda as the newest unapplied one, beside the oldest."""
    if state.pending_run is None:
        state.pending_run = run.run_id
    state.latest_run = run.run_id


def _end(
    run: _Run,
    outcome: _Outcome,
    notice: str,
    *,
    resolve: tuple[str, ...] = (),
    notes: str | None = None,
    applied: tuple[str, ...] = (),
    on_loop: Callable[[], None] | None = None,
) -> Done:
    """Record the run's outcome, drop its staging, and return the notice that ends the chain.

    A proposal that reached its review is already pending, so only success needs recording here.
    """

    def record_progress(state: _State) -> None:
        reviewed = {path: version for path, version in state.reviewed.items() if path in run.inputs}
        for item in run.due:
            reviewed[item.path] = item.version
        # A daily note this run edited is handled at its new version, so the edit does not make it due again.
        for path in applied:
            if path in reviewed and (version := _version(run.root, path)) is not None:
                reviewed[path] = version
        state.reviewed = reviewed
        state.pending_run = None
        state.latest_run = None
        state.notes = notes

    def record_end(state: _State) -> None:
        state.attempted = {item.path: item.version for item in run.due}
        if outcome in {"applied", "unchanged"}:
            record_progress(state)

    _update_state(run.runtime_paths, run.agent_name, record_end)
    try:
        with open_directory_within_root(run.root, run.run_dir) as run_fd:
            shutil.rmtree("staging", dir_fd=run_fd)
    except FileNotFoundError:
        pass
    logger.info("Dreaming run ended", agent=run.agent_name, run=run.run_id, outcome=outcome)
    return Done(notice, resolve=resolve, on_loop=on_loop)
