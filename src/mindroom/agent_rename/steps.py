"""Reversible plan/apply engine for renaming an agent's identity on disk.

A plan is an ordered tuple of steps.
Building a plan and describing it never touches the filesystem; only ``apply_plan`` does.
Each reversible step stores what it needs for undo in a private backup directory
and makes its last action the single commit point, so a step that fails changes nothing.
When a step fails, the completed steps are undone in reverse order.
``FinalAction`` steps are irreversible, run after every other step, and only ever produce warnings.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

BACKUPS_DIRECTORY_NAME = "agent-rename-backups"
_MANIFEST_NAME = "manifest.json"
_PRIVATE_DIRECTORY_MODE = 0o700


class RenameRefusedError(RuntimeError):
    """A precondition failed; the message is meant to be shown to the operator verbatim."""


class RenameApplyError(RuntimeError):
    """A step failed and every completed step was undone."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class RenameUndoError(RuntimeError):
    """A step failed and undoing the completed steps failed too, so the disk needs a manual restore."""

    def __init__(self, manifest: Path, message: str) -> None:
        super().__init__(message)
        self.manifest = manifest


def _write_atomic(path: Path, data: bytes, *, mode: int) -> None:
    """Replace ``path`` with ``data`` after fully writing a sibling temp file."""
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _restore_copy(saved: Path, destination: Path) -> None:
    """Replace ``destination`` with a copy of ``saved`` that keeps its mode."""
    descriptor, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(saved, temp_path)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _move(source: Path, destination: Path) -> None:
    """Move a file, directory, or symlink to a path that does not exist yet."""
    if os.path.lexists(destination):
        msg = f"Cannot move {source} to {destination}: {destination} already exists."
        raise RenameRefusedError(msg)
    shutil.move(source, destination)


def _connect_existing(database: Path) -> sqlite3.Connection:
    """Open a database in autocommit mode without creating it when it is missing."""
    return sqlite3.connect(f"{database.absolute().as_uri()}?mode=rw", uri=True, isolation_level=None)


def _make_private_directory(path: Path) -> None:
    path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)


@contextmanager
def _scratch_beside(path: Path) -> Iterator[Path]:
    """Yield an empty private directory beside ``path``, so renames between the two stay on one filesystem.

    The directory and anything left in it are removed afterwards.
    """
    scratch = Path(tempfile.mkdtemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"))
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _swap_directory(replacement: Path, destination: Path, scratch: Path) -> None:
    """Put ``replacement`` in the place of the directory ``destination``, which moves into ``scratch``."""
    replaced = scratch / f"{destination.name}.replaced"
    destination.rename(replaced)
    try:
        replacement.rename(destination)
    except BaseException:
        replaced.rename(destination)
        raise


@dataclass(frozen=True)
class MovePath:
    """Rename a file or directory; refuses when the destination already exists."""

    source: Path
    destination: Path
    description: str
    stores_backup: ClassVar[bool] = False

    def apply(self, backup: Path) -> None:  # noqa: ARG002
        """Move ``source`` to ``destination``; a move stores no backup."""
        _move(self.source, self.destination)

    def _undo(self, backup: Path) -> None:  # noqa: ARG002
        """Move ``destination`` back to ``source``."""
        _move(self.destination, self.source)


@dataclass(frozen=True)
class SqliteEdit:
    """Run ``edit`` against a SQLite database inside one ``BEGIN IMMEDIATE`` transaction."""

    database: Path
    edit: Callable[[sqlite3.Connection], None]
    description: str
    stores_backup: ClassVar[bool] = True

    def apply(self, backup: Path) -> None:
        """Back up the database with the SQLite backup API, then run ``edit`` in one transaction."""
        _make_private_directory(backup)
        with (
            closing(_connect_existing(self.database)) as source,
            closing(sqlite3.connect(backup / self.database.name)) as saved,
        ):
            source.backup(saved)
            # The backup is created with the umask mode; undo copies this mode back, so keep the original's.
            shutil.copymode(self.database, backup / self.database.name)
            # Closing the connection without COMMIT rolls back, so a failing edit changes nothing.
            source.execute("BEGIN IMMEDIATE")
            self.edit(source)
            source.execute("COMMIT")

    def _undo(self, backup: Path) -> None:
        """Copy the backup over the database and drop its stale WAL and shared-memory files."""
        for suffix in ("-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)
        _restore_copy(backup / self.database.name, self.database)


@dataclass(frozen=True)
class FileRewrite:
    """Replace a file's bytes with ``rewrite(old_bytes)``, keeping its mode."""

    path: Path
    rewrite: Callable[[bytes], bytes]
    description: str
    stores_backup: ClassVar[bool] = True

    def apply(self, backup: Path) -> None:
        """Copy the file into ``backup``, then atomically write the rewritten bytes."""
        _make_private_directory(backup)
        shutil.copy2(self.path, backup / self.path.name)
        rewritten = self.rewrite(self.path.read_bytes())
        _write_atomic(self.path, rewritten, mode=self.path.stat().st_mode & 0o777)

    def _undo(self, backup: Path) -> None:
        """Atomically restore the copy from ``backup``."""
        _restore_copy(backup / self.path.name, self.path)


@dataclass(frozen=True)
class DirectoryEdit:
    """Run ``edit`` on a copy of a directory, then swap the edited copy into the directory's place."""

    path: Path
    edit: Callable[[Path], None]
    description: str
    stores_backup: ClassVar[bool] = True

    def apply(self, backup: Path) -> None:
        """Copy the directory into ``backup``, run ``edit`` on a second copy beside it, then swap that copy in.

        ``edit`` receives the second copy's path. The swap is the commit point, so a failing edit changes nothing.
        """
        _make_private_directory(backup)
        shutil.copytree(self.path, backup / self.path.name, symlinks=True)
        with _scratch_beside(self.path) as scratch:
            edited = scratch / self.path.name
            shutil.copytree(self.path, edited, symlinks=True)
            self.edit(edited)
            # Once swapped, the step must also return so the plan records it as done and can undo it;
            # removing the replaced original takes a while, so Ctrl-C is ignored from the swap on.
            with _sigint_ignored():
                _swap_directory(edited, self.path, scratch)
                shutil.rmtree(scratch, ignore_errors=True)

    def _undo(self, backup: Path) -> None:
        """Swap a copy of the backup into the directory's place."""
        with _scratch_beside(self.path) as scratch:
            restored = scratch / self.path.name
            shutil.copytree(backup / self.path.name, restored, symlinks=True)
            _swap_directory(restored, self.path, scratch)


@dataclass(frozen=True)
class DropPath:
    """Remove a path from its place by moving it into the backup directory, so a drop is recoverable."""

    path: Path
    description: str
    stores_backup: ClassVar[bool] = True

    def apply(self, backup: Path) -> None:
        """Move the path into ``backup``."""
        _make_private_directory(backup)
        _move(self.path, backup / self.path.name)

    def _undo(self, backup: Path) -> None:
        """Move the path back out of ``backup``."""
        _move(backup / self.path.name, self.path)


def _replace_link(path: Path, *, expected: str, target: str) -> None:
    """Atomically replace the symlink ``path`` whose text is ``expected`` with one pointing at ``target``."""
    # Path.readlink normalizes the text, which would hide a change such as an added trailing /.
    if not path.is_symlink() or os.readlink(path) != expected:  # noqa: PTH115
        msg = f"{path} is no longer a symlink to {expected}; it changed since the rename was planned."
        raise RenameRefusedError(msg)
    with _scratch_beside(path) as scratch:
        replacement = scratch / path.name
        replacement.symlink_to(target)
        # Once replaced, the step must also return so the plan records it as done and can undo it.
        with _sigint_ignored():
            replacement.replace(path)
            shutil.rmtree(scratch, ignore_errors=True)


@dataclass(frozen=True)
class RetargetLink:
    """Point the symlink ``path`` at ``target`` instead of ``old_target``, its text when the plan was built.

    Refuses when ``path`` is no longer a symlink with that text. The manifest records both texts, so no backup is kept.
    """

    path: Path
    old_target: str
    target: str
    description: str
    stores_backup: ClassVar[bool] = False

    def apply(self, backup: Path) -> None:  # noqa: ARG002
        """Replace the link with one pointing at ``target``, through a new link renamed over it."""
        _replace_link(self.path, expected=self.old_target, target=self.target)

    def _undo(self, backup: Path) -> None:  # noqa: ARG002
        """Point the link back at ``old_target``."""
        _replace_link(self.path, expected=self.target, target=self.old_target)


@dataclass(frozen=True)
class FinalAction:
    """An irreversible action that runs after every other step; its failure is only a warning."""

    action: Callable[[], None]
    description: str
    stores_backup: ClassVar[bool] = False

    def apply(self, backup: Path) -> None:  # noqa: ARG002
        """Run the action."""
        self.action()

    def _undo(self, backup: Path) -> None:
        """Do nothing: final actions are never undone."""


Step = MovePath | SqliteEdit | FileRewrite | DirectoryEdit | DropPath | RetargetLink | FinalAction


@dataclass(frozen=True)
class RenamePlan:
    """Ordered steps plus report lines for the operator; building one never touches the filesystem."""

    storage_root: Path
    steps: tuple[Step, ...]
    reports: tuple[str, ...]
    # The paths the reports name as staying where they are, which a leftover scan finds on purpose.
    staying: tuple[Path, ...] = ()

    def describe(self) -> list[str]:
        """Return every step description, then every report prefixed with ``report:``."""
        return [step.description for step in self.steps] + [f"report: {report}" for report in self.reports]


@dataclass(frozen=True)
class ApplyResult:
    """Where the backups went and which irreversible actions failed."""

    backup_dir: Path
    warnings: tuple[str, ...]


def _manifest_entry(step: Step, step_backup: Path) -> dict[str, str | None]:
    if isinstance(step, MovePath):
        source, destination = step.source, step.destination
    elif isinstance(step, SqliteEdit):
        source, destination = step.database, None
    elif isinstance(step, FileRewrite | DirectoryEdit | DropPath | RetargetLink):
        source, destination = step.path, None
    else:
        source, destination = None, None
    entry = {
        "type": type(step).__name__,
        "description": step.description,
        "source": None if source is None else str(source),
        "destination": None if destination is None else str(destination),
        "backup": str(step_backup) if step.stores_backup and step_backup.exists() else None,
    }
    if isinstance(step, RetargetLink):
        entry |= {"old_target": step.old_target, "new_target": step.target}
    return entry


def _write_manifest(plan: RenamePlan, *, kind: str, now: datetime, backup_dir: Path) -> None:
    manifest = {
        "kind": kind,
        "created_at": now.isoformat(),
        "steps": [_manifest_entry(step, _step_backup(backup_dir, index)) for index, step in enumerate(plan.steps)],
    }
    _write_atomic(backup_dir / _MANIFEST_NAME, json.dumps(manifest, indent=2).encode(), mode=0o600)


def _step_backup(backup_dir: Path, index: int) -> Path:
    return backup_dir / f"{index:03d}"


@contextmanager
def _sigint_ignored() -> Iterator[None]:
    """Ignore Ctrl-C inside the block, then restore the previous handler.

    Only the main thread may change signal handlers, so in any other thread this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        # None means the handler was not installed from Python; the default action is the closest restore.
        signal.signal(signal.SIGINT, signal.SIG_DFL if previous is None else previous)


def _describe_failure(failure: BaseException) -> str:
    """Return the failure's message, or its type for exceptions without one, like ``KeyboardInterrupt``."""
    return str(failure) or type(failure).__name__


def _undo_completed(
    completed: list[tuple[int, Step]],
    *,
    backup_dir: Path,
    failed: Step,
    failure: BaseException,
) -> None:
    """Undo completed steps in reverse order, stopping at the first undo that fails.

    Undoing the journal copies the whole database back, which can take long enough for a second Ctrl-C,
    so Ctrl-C is ignored meanwhile. Any failure while undoing, an interrupt included, raises ``RenameUndoError``,
    which tells the operator to restore by hand before starting MindRoom.
    """
    with _sigint_ignored():
        for index, step in reversed(completed):
            try:
                step._undo(_step_backup(backup_dir, index))
            except BaseException as undo_failure:
                manifest = backup_dir / _MANIFEST_NAME
                msg = (
                    f"{failed.description} failed ({_describe_failure(failure)}), "
                    f"and undoing {step.description} failed ({_describe_failure(undo_failure)}). "
                    f"Restore the remaining steps by hand from {backup_dir} (manifest: {manifest})."
                )
                raise RenameUndoError(manifest, msg) from undo_failure


def apply_plan(plan: RenamePlan, *, kind: str, now: datetime) -> ApplyResult:
    """Run a plan, undoing completed steps if one fails.

    Backups go to ``<storage_root>/agent-rename-backups/<UTC timestamp>-<kind>/`` with a ``manifest.json``
    that is rewritten after every step.
    A ``RenameRefusedError`` from a step is re-raised after the undo, any other failure becomes
    ``RenameApplyError``, and ``RenameUndoError`` means the undo itself failed.
    """
    backup_dir = plan.storage_root / BACKUPS_DIRECTORY_NAME / f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{kind}"
    _make_private_directory(backup_dir.parent)
    try:
        backup_dir.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    except FileExistsError:
        msg = f"The backup directory {backup_dir} already exists; wait a second and run again."
        raise RenameRefusedError(msg) from None
    _write_manifest(plan, kind=kind, now=now, backup_dir=backup_dir)

    indexed = list(enumerate(plan.steps))
    reversible = [(index, step) for index, step in indexed if not isinstance(step, FinalAction)]
    final = [(index, step) for index, step in indexed if isinstance(step, FinalAction)]

    completed: list[tuple[int, Step]] = []
    for index, step in reversible:
        try:
            step.apply(_step_backup(backup_dir, index))
            completed.append((index, step))
            # A manifest that cannot be written counts as this step failing, so the step is undone too.
            _write_manifest(plan, kind=kind, now=now, backup_dir=backup_dir)
        except BaseException as failure:
            try:
                _undo_completed(completed, backup_dir=backup_dir, failed=step, failure=failure)
            finally:
                # Record the failed step's backup, but never let a manifest that cannot be written hide the failure.
                with suppress(OSError):
                    _write_manifest(plan, kind=kind, now=now, backup_dir=backup_dir)
            if isinstance(failure, RenameRefusedError) or not isinstance(failure, Exception):
                raise
            msg = f"{step.description} failed: {failure}. The completed steps were undone."
            raise RenameApplyError(step.description, msg) from failure

    warnings: list[str] = []
    for position, (index, step) in enumerate(final):
        try:
            step.apply(_step_backup(backup_dir, index))
        except KeyboardInterrupt:
            # Every reversible step is done, so Ctrl-C only stops the final actions; the caller still reports the result.
            warnings.append(f"{step.description}: interrupted")
            warnings += [f"{skipped.description}: not run after the interrupt" for _, skipped in final[position + 1 :]]
            break
        except Exception as failure:
            warnings.append(f"{step.description}: {failure}")
    return ApplyResult(backup_dir=backup_dir, warnings=tuple(warnings))
