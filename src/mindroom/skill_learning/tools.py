"""Skill tools shared by chat and the learner's review, with Hermes' ownership and read-before-write guards.

In chat, ``skill_manage`` changes any workspace skill and the skills it creates belong to their human owner, like
Hermes' foreground ``skill_manage``. In a review, the learner changes only learner-owned skills, must load a file in the
same review before changing it, and records each change as it lands, like Hermes' background-review guards. The review's
read tools take the names and arguments of the agent's skill tools, so a review can serve the schemas the agent's own
request advertised.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mindroom.skill_learning.library import (
    SkillEditError,
    SkillFile,
    content_digest,
    create_skill,
    read_skill_file,
    remove_skill_file,
    support_file_paths,
    write_skill_file,
)
from mindroom.tool_system.skills import build_agent_skills, list_skill_listings
from mindroom.tool_system.workspace_skills import SKILL_FILENAME, parse_skill_markdown

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.skills import Skills

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# A plain alias: Agno does not unwrap PEP 695 type aliases when it builds the provider schema.
SkillAction = Literal["create", "patch", "edit", "write_file", "remove_file"]


@dataclass(frozen=True)
class _CatalogEntry:
    """One skill the agent can use."""

    name: str
    description: str
    directory: str | None
    learned: bool
    instructions: str

    @property
    def owner(self) -> str:
        """Return who may change the skill: the learner, its human owner, or nobody for configured skills."""
        if self.directory is None:
            return "configured"
        return "learner" if self.learned else "user"


@dataclass(frozen=True)
class SkillCatalog:
    """The agent's effective skills and every name a new skill must not shadow."""

    skills: Skills | None
    entries: dict[str, _CatalogEntry]
    reserved_names: frozenset[str]


def load_skill_catalog(config: Config, runtime_paths: RuntimePaths, agent_name: str, skills_root: Path) -> SkillCatalog:
    """Return the skills the agent loads now, with the strict ownership check that edits use."""
    skills = build_agent_skills(agent_name, config, runtime_paths, workspace_skills_root=skills_root)
    entries: dict[str, _CatalogEntry] = {}
    for skill in skills.get_all_skills() if skills is not None else []:
        source = Path(skill.source_path)
        in_workspace = source.parent == skills_root
        entries[skill.name] = _CatalogEntry(
            name=skill.name,
            description=skill.description,
            directory=source.name if in_workspace else None,
            learned=in_workspace
            and (current := read_skill_file(skills_root, source.name)) is not None
            and current.learned,
            instructions=skill.instructions,
        )
    reserved = {name.lower() for name in entries} | {listing.name.lower() for listing in list_skill_listings()}
    return SkillCatalog(skills, entries, frozenset(reserved))


@dataclass
class ReviewProgress:
    """What a review changed so far, and its file work, which finishes even after a timeout or a stop."""

    changes: dict[str, str] = field(default_factory=dict)
    writes: set[asyncio.Future[Any]] = field(default_factory=set)

    def track[T](
        self,
        operation: Awaitable[T],
        on_done: Callable[[asyncio.Future[T]], None] | None = None,
    ) -> Awaitable[T]:
        """Run file work that a cancelled review must not abandon halfway."""
        future = asyncio.ensure_future(operation)
        if on_done is not None:
            future.add_done_callback(on_done)
        self.writes.add(future)
        return asyncio.shield(future)

    async def settled(self) -> None:
        """Wait until all file work the review started has landed or failed."""
        await asyncio.gather(*self.writes, return_exceptions=True)


@dataclass
class SkillTools:
    """One agent's workspace skill tools, acting for the learner when ``progress`` records a review."""

    skills_root: Path
    catalog: dict[str, _CatalogEntry]
    reserved_names: frozenset[str]
    progress: ReviewProgress | None = None
    _reads: dict[tuple[str, str], SkillFile] = field(default_factory=dict)
    # Agno runs the tool calls of one reply concurrently, and each write builds on the last read of its file;
    # like Hermes, which never runs skill_manage in parallel, reads and writes take turns.
    _turn: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def learner(self) -> bool:
        """Return whether these tools act for the learner's review."""
        return self.progress is not None

    async def get_skill_instructions(self, skill_name: str, mindroom_output_path: str | None = None) -> str:
        """Load a skill's full SKILL.md, its owner, and its support files."""
        return await self._view(skill_name, SKILL_FILENAME, mindroom_output_path)

    async def get_skill_reference(
        self,
        skill_name: str,
        reference_path: str | None = None,
        mindroom_output_path: str | None = None,
    ) -> str:
        """Load one file under a skill's references/."""
        return await self._view(skill_name, _support_path("references", reference_path), mindroom_output_path)

    async def get_skill_script(
        self,
        skill_name: str,
        script_path: str | None = None,
        execute: bool = False,
        args: list[str] | None = None,  # noqa: ARG002 - the agent's tool schema offers it
        timeout: int = 30,  # noqa: ARG002, ASYNC109 - the agent's tool schema offers it
        mindroom_output_path: str | None = None,
    ) -> str:
        """Load one file under a skill's scripts/; a review never runs scripts."""
        if execute:
            return _refusal("Scripts never run during a skill review; load the script without execute.")
        return await self._view(skill_name, _support_path("scripts", script_path), mindroom_output_path)

    async def _view(self, skill_name: str, relative_path: str | None, output_path: str | None) -> str:
        if output_path is not None:
            return _refusal("mindroom_output_path is not available during a skill review.")
        if relative_path is None:
            return _refusal("Name the support file; get_skill_instructions lists a skill's support files.")
        async with self._turn:
            entry = self.catalog.get(skill_name)
            if entry is None:
                return _refusal(f"Unknown skill {skill_name!r}.")
            try:
                payload = await self._load(entry, relative_path)
            except (OSError, ValueError) as exc:
                return _refusal(str(exc))
            return _reply(payload)

    async def _load(self, entry: _CatalogEntry, relative_path: str) -> dict[str, object]:
        if entry.directory is None:
            if relative_path != SKILL_FILENAME:
                msg = "Support files of configured skills are not available here."
                raise SkillEditError(msg)
            return {
                "name": entry.name,
                "owner": entry.owner,
                "description": entry.description,
                "content": entry.instructions,
            }
        loaded = await asyncio.to_thread(read_skill_file, self.skills_root, entry.directory, relative_path)
        if loaded is None:
            msg = f"{relative_path} does not exist in {entry.name!r}."
            raise SkillEditError(msg)
        self._reads[entry.directory, relative_path] = loaded
        return {
            "name": entry.name,
            "file_path": relative_path,
            "owner": "learner" if loaded.learned else "user",
            "support_files": await asyncio.to_thread(support_file_paths, self.skills_root, entry.directory),
            "content": loaded.content,
        }

    async def skill_manage(
        self,
        action: SkillAction,
        name: str,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """Apply one skill change; the agent-facing schema is ``SkillManageTools.skill_manage``."""
        async with self._turn:
            entry = self.catalog.get(name)
            if action != "create" and (refusal := self._edit_refusal(name, entry)) is not None:
                return refusal
            # An adopted skill may live in a directory named differently from its frontmatter name.
            directory = name if entry is None or entry.directory is None else entry.directory
            relative_path = file_path or SKILL_FILENAME
            try:
                if action == "create":
                    await self._create(name, _required(content, "content"))
                elif action == "patch":
                    read = await self._current(directory, relative_path)
                    patched = _patched(read, directory, relative_path, old_string, new_string, replace_all)
                    await self._write(name, directory, relative_path, patched, read)
                elif action == "edit":
                    edited = _required(content, "content")
                    await self._write(
                        name,
                        directory,
                        SKILL_FILENAME,
                        edited,
                        await self._current(directory, SKILL_FILENAME),
                    )
                elif action == "write_file":
                    target = _required(file_path, "file_path")
                    written = _required(file_content, "file_content")
                    await self._write(name, directory, target, written, await self._current(directory, target))
                else:
                    target = _required(file_path, "file_path")
                    await self._remove(name, directory, target, await self._current(directory, target))
            except (OSError, ValueError) as exc:
                return _refusal(str(exc))
            return _reply({"success": True, "action": action, "name": name, "file_path": relative_path})

    def _edit_refusal(self, name: str, entry: _CatalogEntry | None) -> str | None:
        if entry is None:
            return _refusal(f"Unknown skill {name!r}; create it or check the skill name.")
        if entry.directory is None:
            return _refusal(f"Skill {name!r} is a configured skill and read-only.")
        if self.learner and not entry.learned:
            return _refusal(f"Skill {name!r} is {entry.owner}-owned and read-only; mention the needed change instead.")
        return None

    async def _file(self, operation: Callable[[], None], *, name: str, action: str) -> None:
        """Run one file change in a thread; a review's change finishes even when a timeout cancels the review.

        A review records the change when the write lands, so a notice after a timeout still names it.
        """
        if self.progress is None:
            await asyncio.to_thread(operation)
            return
        progress = self.progress

        def record(done: asyncio.Future[None]) -> None:
            if not done.cancelled() and done.exception() is None:
                progress.changes.setdefault(name, action)

        await progress.track(asyncio.to_thread(operation), on_done=record)

    async def _current(self, directory: str, relative_path: str) -> SkillFile | None:
        """Return the version a write builds on: the review's last read of it, or in chat the file as it is now."""
        if self.learner:
            return self._reads.get((directory, relative_path))
        return await asyncio.to_thread(read_skill_file, self.skills_root, directory, relative_path)

    async def _create(self, name: str, content: str) -> None:
        await self._file(
            partial(
                create_skill,
                self.skills_root,
                name,
                content,
                reserved_names=self.reserved_names,
                learner=self.learner,
            ),
            name=name,
            action="created",
        )
        frontmatter, instructions = parse_skill_markdown(content)
        self.catalog[name] = _CatalogEntry(
            name,
            frontmatter["description"],
            name,
            learned=self.learner,
            instructions=instructions,
        )
        self.reserved_names |= {name}
        self._reads[name, SKILL_FILENAME] = SkillFile(content, content_digest(content), learned=self.learner, name=name)

    async def _remove(self, name: str, directory: str, relative_path: str, read: SkillFile | None) -> None:
        self._reads.pop((directory, relative_path), None)
        await self._file(
            partial(
                remove_skill_file,
                self.skills_root,
                directory,
                relative_path,
                expected_digest=read.digest if read else None,
                learner=self.learner,
            ),
            name=name,
            action="updated",
        )

    async def _write(
        self,
        name: str,
        directory: str,
        relative_path: str,
        content: str,
        read: SkillFile | None,
    ) -> None:
        await self._file(
            partial(
                write_skill_file,
                self.skills_root,
                directory,
                relative_path,
                content,
                expected_digest=read.digest if read else None,
                learner=self.learner,
            ),
            name=name,
            action="updated",
        )
        self._reads[directory, relative_path] = SkillFile(
            content,
            content_digest(content),
            learned=self.learner,
            name=name,
        )


def _patched(
    read: SkillFile | None,
    directory: str,
    relative_path: str,
    old_string: str | None,
    new_string: str | None,
    replace_all: bool,
) -> str:
    if read is None:
        msg = f"Load {relative_path} of {directory!r} with {_loader(relative_path)} before patching it."
        raise SkillEditError(msg)
    old, new = _required(old_string, "old_string"), _required(new_string, "new_string")
    matches = read.content.count(old) if old else 0
    if matches == 0 or (matches > 1 and not replace_all):
        problem = "was not found" if matches == 0 else f"occurs {matches} times; add context or set replace_all"
        msg = f"old_string {problem}. Current start of {relative_path}: {read.content[:500]!r}"
        raise SkillEditError(msg)
    return read.content.replace(old, new, -1 if replace_all else 1)


def _support_path(directory: str, filename: str | None) -> str | None:
    return f"{directory}/{filename}" if filename is not None else None


def _loader(relative_path: str) -> str:
    if relative_path == SKILL_FILENAME:
        return "get_skill_instructions"
    return "get_skill_reference" if relative_path.startswith("references/") else "get_skill_script"


def _reply(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _refusal(error: str) -> str:
    return _reply({"success": False, "error": error})


def _required(value: str | None, argument: str) -> str:
    if value is None:
        msg = f"{argument} is required for this action."
        raise SkillEditError(msg)
    return value
