"""The staged copy of an agent workspace that one curation pass edits.

A pass never writes the live files while it runs: its edits stay in memory, so a rejected pass leaves every file
byte-identical and a live turn never loads a half-curated prompt.
Publication is compare-and-swap per file.
A curatable file is written only when it still holds what the pass read, or that text plus lines a live turn
appended meanwhile, which are kept; any other concurrent change abandons the whole result.
``memory/`` additions are appended to whatever the live file holds, and are written before the curatable files, so
an interrupted publication duplicates content instead of losing it.
Every read and write goes through the file-memory descriptor helpers, because worker code writes this workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from mindroom.memory import read_scope_file, read_scope_memory_files, write_scope_file
from mindroom.prompt_curation.policy import MEMORY_DIR_PREFIX, PassMeasurement
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.prompt_curation import PromptCurationConfig
    from mindroom.memory import ScopeMemoryFile

# memory/<topic>.md or one folder deeper; topic files the pass creates stay easy to find.
_MAX_MEMORY_PATH_PARTS = 3


def _normalized(path: str) -> str:
    return PurePosixPath(path.strip()).as_posix()


def _is_memory_markdown(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        path.startswith(MEMORY_DIR_PREFIX)
        and 2 <= len(parts) <= _MAX_MEMORY_PATH_PARTS
        and ".." not in parts
        and not any(part.casefold() == ".git" for part in parts)
        # Case-sensitive, like the file-memory listing, so search always finds what a pass moved.
        and path.endswith(".md")
    )


def _joined(text: str, addition: str) -> str:
    """Return ``text`` followed by ``addition`` on its own line."""
    separator = "\n" if text and addition and not text.endswith("\n") else ""
    return f"{text}{separator}{addition}"


def curatable_tokens(root: Path, settings: PromptCurationConfig) -> int:
    """Measure only the curatable files, which is all a turn needs to know whether a pass is due."""
    return sum(
        estimate_text_tokens(memory_file.text)
        for path in settings.files
        if (memory_file := read_scope_file(root, path)) is not None
    )


@dataclass
class StagedWorkspace:
    """One pass's view of the curatable files and ``memory/``, with its edits held in memory."""

    root: Path
    settings: PromptCurationConfig
    baseline: dict[str, ScopeMemoryFile]
    curated: tuple[str, ...]
    _edited: dict[str, str] = field(default_factory=dict)
    _appended: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, settings: PromptCurationConfig) -> StagedWorkspace:
        """Read the curatable files and ``memory/``; raise when a curatable file cannot be rewritten safely."""
        baseline = {memory_file.relative_path: memory_file for memory_file in read_scope_memory_files(root)}
        curated: list[str] = []
        for path in settings.files:
            memory_file = baseline.get(path) or read_scope_file(root, path)
            if memory_file is None:
                continue
            if not memory_file.rewritable:
                msg = f"{path} is oversized or not valid UTF-8, so curation cannot rewrite it safely"
                raise ValueError(msg)
            baseline[path] = memory_file
            curated.append(path)
        return cls(root=root, settings=settings, baseline=baseline, curated=tuple(curated))

    def read(self, path: str) -> str:
        """Return a curatable or ``memory/`` file as the pass currently sees it."""
        path = _normalized(path)
        if path in self.curated:
            return self._edited.get(path, self.baseline[path].text)
        if path not in self.settings.files and not _is_memory_markdown(path):
            msg = "Curation can only read the curatable files and memory/ Markdown files"
            raise ValueError(msg)
        if path in self.settings.files or (path not in self.baseline and path not in self._appended):
            msg = f"{path} does not exist"
            raise ValueError(msg)
        baseline = self.baseline.get(path)
        return _joined(baseline.text if baseline is not None else "", self._appended.get(path, ""))

    def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        refuse: Callable[[str, str], str | None] | None = None,
    ) -> None:
        """Replace the one exact occurrence of ``old_text`` in a curatable file.

        ``refuse`` sees the path and the edited text before it is staged and returns a reason to refuse it.
        """
        path = _normalized(path)
        if path in self.settings.protected_files:
            msg = f"{path} is protected; curation never changes it"
            raise ValueError(msg)
        if path not in self.settings.files:
            msg = f"{path} is not curatable; only edit the curatable files: {', '.join(self.curated)}"
            raise ValueError(msg)
        text = self.read(path)
        if not old_text:
            msg = "old_text must not be empty"
            raise ValueError(msg)
        if (count := text.count(old_text)) != 1:
            msg = (
                f"old_text not found in {path}"
                if count == 0
                else f"old_text matches {count} places in {path}; include more surrounding text"
            )
            raise ValueError(msg)
        edited = text.replace(old_text, new_text, 1)
        if refuse is not None and (reason := refuse(path, edited)) is not None:
            raise ValueError(reason)
        self._edited[path] = edited

    def append(self, path: str, content: str) -> None:
        """Append ``content`` as whole lines to a ``memory/`` Markdown file, creating it when needed."""
        path = _normalized(path)
        if not _is_memory_markdown(path):
            msg = "Curation can only append to Markdown files under memory/, such as memory/projects.md"
            raise ValueError(msg)
        if (existing := self.baseline.get(path)) is not None and not existing.rewritable:
            msg = f"{path} is oversized or not valid UTF-8; append to another file"
            raise ValueError(msg)
        if not content.strip():
            msg = "content must not be empty"
            raise ValueError(msg)
        block = content if content.endswith("\n") else f"{content}\n"
        self._appended[path] = self._appended.get(path, "") + block

    def changed_paths(self) -> frozenset[str]:
        """Return every path whose staged text differs from what the pass read."""
        edited = {path for path, text in self._edited.items() if text != self.baseline[path].text}
        return frozenset(edited | set(self._appended))

    def measurement(self) -> PassMeasurement:
        """Measure the curatable files, all memory content, and the changed paths as currently staged."""
        paths = set(self.baseline) | set(self._appended)
        return PassMeasurement(
            curated={path: estimate_text_tokens(self.read(path)) for path in self.curated},
            total_memory_tokens=sum(estimate_text_tokens(self.read(path)) for path in paths),
            changed_paths=self.changed_paths(),
        )

    def publish(self) -> Literal["published", "conflict"]:
        """Write the staged result, or write nothing when a curatable file was rewritten during the pass."""
        writes: list[tuple[str, str]] = []
        for path, addition in self._appended.items():
            live = read_scope_file(self.root, path)
            if live is not None and not live.rewritable:
                return "conflict"
            writes.append((path, _joined(live.text if live is not None else "", addition)))
        for path in sorted(self.changed_paths() - set(self._appended)):
            base = self.baseline[path].text
            live = read_scope_file(self.root, path)
            if live is None or not live.rewritable or not live.text.startswith(base):
                return "conflict"
            writes.append((path, _joined(self._edited[path], live.text[len(base) :])))
        for path, text in writes:
            write_scope_file(self.root, path, text)
        return "published"
