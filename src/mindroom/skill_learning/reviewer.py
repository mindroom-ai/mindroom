"""One background skill review: Hermes' review fork rebuilt as a bounded Agno run with only skill tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from agno.agent import Agent

from mindroom import model_loading
from mindroom.agent_storage import create_session_storage
from mindroom.helper_usage import HelperUsageOwner, record_helper_usage
from mindroom.provider_tool_policy import without_provider_tools
from mindroom.skill_learning.library import (
    SkillEditError,
    SkillFile,
    content_digest,
    create_skill,
    is_learned,
    read_skill_file,
    remove_skill_file,
    support_file_paths,
    write_skill_file,
)
from mindroom.skill_learning.transcript import render_transcript
from mindroom.tool_call_budget import install_model_call_cap
from mindroom.tool_system.skills import build_agent_skills, list_skill_listings
from mindroom.tool_system.workspace_skills import SKILL_FILENAME, parse_skill_markdown

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.message import Message

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

# Hermes caps one review fork at 16 iterations and 75% of the review model's context window, at most 600k input
# tokens across all of its requests, falling back to 120k when the window is unknown.
_REVIEW_TOOL_CALL_LIMIT = 16
_INPUT_CONTEXT_FRACTION = 0.75
_MAX_INPUT_TOKENS = 600_000
_FALLBACK_INPUT_TOKENS = 120_000
_CHARS_PER_TOKEN = 4
# The transcript is replayed on every request of the review, so it may use a quarter of the aggregate budget.
_TRANSCRIPT_BUDGET_SHARE = 4

type _SkillAction = Literal["create", "patch", "edit", "write_file", "remove_file"]


@dataclass(frozen=True)
class _CatalogEntry:
    """One skill the reviewed agent can use."""

    name: str
    description: str
    directory: str | None
    learned: bool
    instructions: str

    @property
    def owner(self) -> str:
        if self.directory is None:
            return "configured"
        return "learner" if self.learned else "user"


@dataclass
class _ReviewTools:
    """Skill-only tools with Hermes' ownership, read-before-write, and input-budget guards."""

    skills_root: Path
    catalog: dict[str, _CatalogEntry]
    reserved_names: frozenset[str]
    budget_chars: int
    changes: dict[str, str]
    _reads: dict[tuple[str, str], SkillFile] = field(default_factory=dict)
    _context_chars: int = 0
    _spent_chars: int = 0

    def charge(self, chars: int) -> None:
        """Account one more request that replays the grown context."""
        self._context_chars += chars
        self._spent_chars += self._context_chars

    def _exhausted(self) -> bool:
        return self._spent_chars > self.budget_chars

    def _reply(self, payload: dict[str, object], *arguments: str | None) -> str:
        reply = json.dumps(payload, ensure_ascii=False)
        self.charge(len(reply) + sum(len(argument or "") for argument in arguments))
        return reply

    def _refusal(self, error: str, *arguments: str | None) -> str:
        return self._reply({"success": False, "error": error}, *arguments)

    async def skills_list(self) -> str:
        """List every skill this agent can use, with its owner.

        Only skills whose owner is "learner" can be changed; "user" and "configured" skills are read-only.
        """
        if self._exhausted():
            return self._refusal(_BUDGET_EXHAUSTED)
        skills = [
            {"name": entry.name, "description": entry.description, "owner": entry.owner}
            for entry in sorted(self.catalog.values(), key=lambda item: item.name)
        ]
        return self._reply({"skills": skills})

    async def skill_view(self, name: str, file_path: str | None = None) -> str:
        """Load a skill's SKILL.md, or one support file of a workspace skill.

        Args:
            name: Skill name from skills_list.
            file_path: Optional support file such as "references/topic.md"; omit it for SKILL.md.

        """
        if self._exhausted():
            return self._refusal(_BUDGET_EXHAUSTED, name, file_path)
        entry = self.catalog.get(name)
        if entry is None:
            return self._refusal(f"Unknown skill {name!r}; call skills_list.", name, file_path)
        try:
            payload = await self._view(entry, file_path)
        except (OSError, ValueError) as exc:
            return self._refusal(str(exc), name, file_path)
        return self._reply(payload, name, file_path)

    async def _view(self, entry: _CatalogEntry, file_path: str | None) -> dict[str, object]:
        if entry.directory is None:
            if file_path is not None:
                msg = "Support files of configured skills are not available here."
                raise SkillEditError(msg)
            return {
                "name": entry.name,
                "owner": entry.owner,
                "description": entry.description,
                "content": entry.instructions,
            }
        directory, relative_path = entry.directory, file_path or SKILL_FILENAME
        loaded = await asyncio.to_thread(read_skill_file, self.skills_root, directory, relative_path)
        if loaded is None:
            msg = f"{relative_path} does not exist in {entry.name!r}."
            raise SkillEditError(msg)
        self._reads[directory, relative_path] = loaded
        return {
            "name": entry.name,
            "file_path": relative_path,
            "owner": "learner" if loaded.learned else "user",
            "support_files": await asyncio.to_thread(support_file_paths, self.skills_root, directory),
            "content": loaded.content,
        }

    async def skill_manage(
        self,
        action: _SkillAction,
        name: str,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """Create or change a learner-owned skill.

        Args:
            action: "create" a new skill from content, "patch" old_string to new_string in SKILL.md or file_path,
                "edit" replaces SKILL.md with content, "write_file" writes file_content to file_path, and
                "remove_file" deletes file_path.
            name: Skill directory name, lowercase and hyphenated.
            content: Complete SKILL.md for create or edit.
            old_string: Exact text to replace for patch; it must occur once unless replace_all is true.
            new_string: Replacement text for patch; an empty string deletes the match.
            file_path: Support file such as "references/topic.md"; omit it to patch SKILL.md.
            file_content: Complete support file content for write_file.
            replace_all: Replace every occurrence of old_string instead of exactly one.

        """
        arguments = (name, content, old_string, new_string, file_path, file_content)
        if self._exhausted():
            return self._refusal(_BUDGET_EXHAUSTED, *arguments)
        relative_path = file_path or SKILL_FILENAME
        try:
            if action == "create":
                await self._create(name, _required(content, "content"))
            elif action == "patch":
                await self._write(
                    name,
                    relative_path,
                    self._patched(name, relative_path, old_string, new_string, replace_all),
                )
            elif action == "edit":
                await self._write(name, SKILL_FILENAME, _required(content, "content"))
            elif action == "write_file":
                await self._write(name, _required(file_path, "file_path"), _required(file_content, "file_content"))
            elif action == "remove_file":
                await self._remove(name, _required(file_path, "file_path"))
            else:
                return self._refusal(f"Unknown action {action!r}.", *arguments)
        except (SkillEditError, OSError) as exc:
            return self._refusal(str(exc), *arguments)
        self.changes.setdefault(name, "updated")
        return self._reply({"success": True, "action": action, "name": name, "file_path": relative_path}, *arguments)

    async def _create(self, name: str, content: str) -> None:
        await asyncio.to_thread(create_skill, self.skills_root, name, content, reserved_names=self.reserved_names)
        frontmatter, instructions = parse_skill_markdown(content)
        self.catalog[name] = _CatalogEntry(
            name,
            frontmatter["description"],
            name,
            learned=True,
            instructions=instructions,
        )
        self.reserved_names |= {name}
        self._reads[name, SKILL_FILENAME] = SkillFile(content, content_digest(content), learned=True)
        self.changes[name] = "created"

    def _patched(
        self,
        name: str,
        relative_path: str,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool,
    ) -> str:
        read = self._reads.get((name, relative_path))
        if read is None:
            msg = f"Call skill_view for {relative_path} of {name!r} before patching it."
            raise SkillEditError(msg)
        old, new = _required(old_string, "old_string"), _required(new_string, "new_string")
        matches = read.content.count(old) if old else 0
        if matches == 0 or (matches > 1 and not replace_all):
            problem = "was not found" if matches == 0 else f"occurs {matches} times; add context or set replace_all"
            msg = f"old_string {problem}. Current start of {relative_path}: {read.content[:500]!r}"
            raise SkillEditError(msg)
        return read.content.replace(old, new, -1 if replace_all else 1)

    async def _remove(self, name: str, relative_path: str) -> None:
        read = self._reads.pop((name, relative_path), None)
        await asyncio.to_thread(
            remove_skill_file,
            self.skills_root,
            name,
            relative_path,
            expected_digest=read.digest if read else None,
        )

    async def _write(self, name: str, relative_path: str, content: str) -> None:
        read = self._reads.get((name, relative_path))
        await asyncio.to_thread(
            write_skill_file,
            self.skills_root,
            name,
            relative_path,
            content,
            expected_digest=read.digest if read else None,
        )
        self._reads[name, relative_path] = SkillFile(content, content_digest(content), learned=True)


_BUDGET_EXHAUSTED = "The review input budget is exhausted. Stop calling tools and reply with the changes you made."


def _required(value: str | None, argument: str) -> str:
    if value is None:
        msg = f"{argument} is required for this action."
        raise SkillEditError(msg)
    return value


def _review_input_budget_chars(config: Config, model_name: str) -> int:
    """Return the aggregate input budget of one review in characters."""
    context_window = config.models[model_name].context_window
    tokens = (
        min(_MAX_INPUT_TOKENS, int(context_window * _INPUT_CONTEXT_FRACTION))
        if context_window is not None
        else _FALLBACK_INPUT_TOKENS
    )
    return tokens * _CHARS_PER_TOKEN


def _skill_catalog(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    skills_root: Path,
) -> tuple[dict[str, _CatalogEntry], frozenset[str]]:
    """Return the agent's effective skills and every name a new skill must not shadow."""
    skills = build_agent_skills(agent_name, config, runtime_paths, workspace_skills_root=skills_root)
    catalog: dict[str, _CatalogEntry] = {}
    for skill in skills.get_all_skills() if skills is not None else []:
        source = Path(skill.source_path)
        in_workspace = source.parent == skills_root
        catalog[skill.name] = _CatalogEntry(
            name=skill.name,
            description=skill.description,
            directory=source.name if in_workspace else None,
            learned=in_workspace and is_learned({"metadata": skill.metadata}, path=str(source)),
            instructions=skill.instructions,
        )
    reserved = {name.lower() for name in catalog} | {listing.name.lower() for listing in list_skill_listings()}
    return catalog, frozenset(reserved)


async def review_conversation(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
    identity: ToolExecutionIdentity | None,
    skills_root: Path,
    messages: Sequence[Message],
    changes: dict[str, str],
) -> None:
    """Run one review, recording each skill it creates or updates in ``changes`` as the write lands."""
    model_name = config.agents[agent_name].skill_learning.model or config.resolve_entity(agent_name).model_name
    budget_chars = _review_input_budget_chars(config, model_name)
    transcript = await asyncio.to_thread(
        render_transcript,
        messages,
        budget_chars=budget_chars // _TRANSCRIPT_BUDGET_SHARE,
    )
    catalog, reserved_names = await asyncio.to_thread(_skill_catalog, config, runtime_paths, agent_name, skills_root)
    tools = _ReviewTools(skills_root, catalog, reserved_names, budget_chars, changes)
    instructions = config.get_prompt("SKILL_REVIEW_PROMPT")
    review_input = f"<conversation>\n{transcript}\n</conversation>"
    tools.charge(len(instructions) + len(review_input))
    model = model_loading.get_model_instance(config, runtime_paths, model_name, execution_identity=identity)
    install_model_call_cap(model, entity_name=agent_name)
    reviewer = Agent(
        name="SkillReviewer",
        model=model,
        instructions=instructions,
        tools=[tools.skills_list, tools.skill_view, tools.skill_manage],
        tool_call_limit=_REVIEW_TOOL_CALL_LIMIT,
        telemetry=False,
    )
    invocation_id = uuid4().hex
    with without_provider_tools():
        response = await reviewer.arun(
            review_input,
            run_id=invocation_id,
            session_id=f"skill_learning:{agent_name}:{session_id}",
        )
    await record_helper_usage(
        response,
        owner=HelperUsageOwner(
            storage_factory=partial(
                create_session_storage,
                agent_name,
                config,
                runtime_paths,
                execution_identity=identity,
            ),
            session_id=session_id,
        ),
        invocation_id=invocation_id,
        kind="skill_learning",
        requester_id=identity.requester_id if identity is not None else None,
    )
