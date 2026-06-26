"""Built-in per-thread todo tools for MindRoom agents."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import yaml
from agno.agent import Agent
from agno.team.team import Team  # noqa: TC002 - Agno resolves tool annotations at runtime.
from agno.tools import Toolkit
from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment, SecurityError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mindroom.file_locks import advisory_file_lock
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.constants import RuntimePaths

_T = TypeVar("_T")


_VALID_PRIORITIES = {"low", "medium", "high", "critical"}
_TERMINAL_STATUSES = {"done", "cancelled"}
_PRIORITY_EMOJI: dict[str, str] = {
    "critical": "red",
    "high": "orange",
    "medium": "yellow",
    "low": "green",
}
_PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_TEMPLATE_RECURSION_LIMIT = 3
_WORKSPACE_TEMPLATE_RELATIVE_DIR = Path("todo/templates")
_JINJA_ENV = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)


class MindroomDevParams(BaseModel):
    """Parameters for the built-in MindRoom development todo template."""

    model_config = ConfigDict(extra="forbid")

    ISSUE_REF: str
    REPO: Literal["mindroom", "cinny", "nixos", "tuwunel"]
    BRANCH: str = ""
    N_REVIEWERS: int = Field(default=8, ge=1)
    IMPLEMENTER_AGENT: str = ""
    IS_PR: bool = True
    BASE: Literal["origin/main", "main"] = "origin/main"

    @model_validator(mode="after")
    def apply_derived_defaults(self) -> MindroomDevParams:
        """Fill fields that depend on other parameters."""
        if self.BRANCH == "":
            self.BRANCH = self.ISSUE_REF.lower()
        if "BASE" not in self.model_fields_set:
            self.BASE = "origin/main" if self.IS_PR else "main"
        return self


class ParallelReviewLoopParams(BaseModel):
    """Parameters for the built-in parallel review loop todo template."""

    model_config = ConfigDict(extra="forbid")

    N_REVIEWERS: int = Field(default=8, ge=1)


class TemplateTodo(BaseModel):
    """One template todo entry or a nested sub-template reference."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    sub_template: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    depends_on: list[int] = Field(default_factory=list)
    assigned_agent: str | None = None

    @model_validator(mode="after")
    def exactly_one_of_title_or_sub_template(self) -> TemplateTodo:
        """Require each entry to be either a todo or a sub-template include."""
        if (self.title is None) == (self.sub_template is None):
            msg = "Each todo must have exactly one of `title` or `sub_template`"
            raise ValueError(msg)
        if self.title is not None:
            invalid_fields = self.model_fields_set - {"title", "priority", "depends_on", "assigned_agent"}
            if invalid_fields:
                invalid = ", ".join(f"`{field}`" for field in sorted(invalid_fields))
                msg = f"Todo with `title` cannot use {invalid}"
                raise ValueError(msg)
        else:
            invalid_fields = self.model_fields_set - {"sub_template", "params", "depends_on"}
            if invalid_fields:
                invalid = ", ".join(f"`{field}`" for field in sorted(invalid_fields))
                msg = f"Todo with `sub_template` cannot use {invalid}"
                raise ValueError(msg)
        return self


class TemplateDocument(BaseModel):
    """Validated todo template document."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    todos: list[TemplateTodo] = Field(min_length=1)


_PARAMS_SCHEMAS: dict[str, type[BaseModel]] = {
    "mindroom-dev": MindroomDevParams,
    "parallel-review-loop": ParallelReviewLoopParams,
}


@dataclass(frozen=True, slots=True)
class _TemplateRoot:
    """One visible source of todo templates."""

    path: Path
    source: str


@dataclass(frozen=True, slots=True)
class _ExpandedTemplateIndex:
    """Expanded roots and terminal leaves for one authored template item."""

    roots: tuple[int, ...]
    terminals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _NoWriteResult:
    """Mutation result that should not persist a state file."""

    value: Any


def _no_write(value: _T) -> _NoWriteResult:
    return _NoWriteResult(value)


def _safe_slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", value)).strip("_")


def _thread_key(room_id: str, thread_id: str | None) -> str:
    resolved = thread_id or "main"
    digest = hashlib.sha256(f"{room_id}\0{resolved}".encode()).hexdigest()[:16]
    room_slug = _safe_slug(room_id) or "room"
    thread_slug = _safe_slug(resolved) or "thread"
    return f"{room_slug}_{thread_slug}_{digest}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_id(existing_ids: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with advisory_file_lock(_lock_path(path), exclusive=False):
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def _locked_update_json(path: Path, mutate: Callable[[dict[str, Any]], _T | _NoWriteResult]) -> _T:
    with advisory_file_lock(_lock_path(path), exclusive=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        result = mutate(data)
        if isinstance(result, _NoWriteResult):
            return result.value
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)
        return result


def _state_root(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / "todo"


def _todos_path(state_root: Path, room_id: str, thread_id: str | None) -> Path:
    key = _thread_key(room_id, thread_id)
    return state_root / "threads" / key / "todos.json"


def _ensure_thread_state(data: dict[str, Any], room_id: str, thread_id: str | None) -> None:
    resolved = thread_id or "main"
    if "items" not in data:
        now = _now_iso()
        data["room_id"] = room_id
        data["thread_id"] = resolved
        data["created_at"] = now
        data["updated_at"] = now
        data["items"] = []


def _is_blocked(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    """Return whether one open item is blocked by unfinished dependencies."""
    for dep_id in item.get("depends_on", []):
        dep = items_by_id.get(dep_id)
        if dep is None:
            continue
        if dep["status"] not in _TERMINAL_STATUSES:
            return True
    return False


def _is_actionable(item: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    """Return whether one todo is open and unblocked."""
    return item["status"] == "open" and not _is_blocked(item, items_by_id)


def _would_create_cycle(items_by_id: dict[str, dict[str, Any]], item_id: str, new_dep_id: str) -> bool:
    stack = [new_dep_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == item_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        current_item = items_by_id.get(current)
        if current_item is not None:
            stack.extend(current_item.get("depends_on", []))
    return False


def _newly_unblocked(items: list[dict[str, Any]], changed_id: str) -> list[dict[str, Any]]:
    items_by_id = {item["id"]: item for item in items}
    unblocked: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "open":
            continue
        if changed_id not in item.get("depends_on", []):
            continue
        if _is_actionable(item, items_by_id):
            unblocked.append(item)
    return unblocked


def _templates_dir() -> Path:
    return Path(__file__).with_name("todo_templates")


def _validate_template_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or ".." in name or Path(name).is_absolute():
        msg = f"invalid template name: '{name}'"
        raise ValueError(msg)


def _template_path(name: str, template_dir: Path | None = None) -> Path:
    _validate_template_name(name)
    root = (template_dir or _templates_dir()).resolve()
    path = (root / f"{name}.yaml.j2").resolve()
    if not path.is_relative_to(root):
        msg = f"invalid template name: '{name}'"
        raise ValueError(msg)
    return path


def _agent_config_key(agent: Agent | Team | None, configured_agents: set[str]) -> str | None:
    if not isinstance(agent, Agent):
        return None
    if isinstance(agent.id, str) and agent.id in configured_agents:
        return agent.id
    if agent.name in configured_agents:
        return agent.name
    return None


def _current_agent_workspace_root(agent: Agent | Team | None = None) -> Path | None:
    ctx = get_tool_runtime_context()
    if ctx is None:
        return None

    agent_name = ctx.agent_name
    member_agent_name = _agent_config_key(agent, set(ctx.config.agents))
    if member_agent_name is not None:
        agent_name = member_agent_name

    agent_config = ctx.config.agents.get(agent_name)
    if agent_config is None:
        return None

    if agent_config.private is not None:
        execution_identity = build_execution_identity_from_runtime_context(ctx)
        agent_runtime = resolve_agent_runtime(
            agent_name,
            ctx.config,
            ctx.runtime_paths,
            execution_identity=execution_identity,
            create=True,
        )
        if agent_runtime.workspace is None:
            return None
        return agent_runtime.workspace.root

    return agent_workspace_root_path(ctx.runtime_paths.storage_root, agent_name)


def _visible_template_roots(agent: Agent | Team | None = None) -> tuple[_TemplateRoot, ...]:
    roots: list[_TemplateRoot] = []
    workspace_root = _current_agent_workspace_root(agent)
    if workspace_root is not None:
        resolved_workspace_root = workspace_root.resolve()
        workspace_template_root = (resolved_workspace_root / _WORKSPACE_TEMPLATE_RELATIVE_DIR).resolve()
        if not workspace_template_root.is_relative_to(resolved_workspace_root):
            msg = "Workspace todo template directory escapes workspace"
            raise ValueError(msg)
        roots.append(_TemplateRoot(path=workspace_template_root, source="workspace"))
    roots.append(_TemplateRoot(path=_templates_dir(), source="builtin"))
    return tuple(roots)


def _resolve_template_path(name: str, template_roots: Sequence[_TemplateRoot]) -> tuple[Path, _TemplateRoot]:
    _validate_template_name(name)
    for template_root in template_roots:
        path = _template_path(name, template_root.path)
        if path.is_file():
            return path, template_root
    msg = f"Unknown template: '{name}'"
    raise ValueError(msg)


def _template_value_error(path: Path, message: str) -> ValueError:
    return ValueError(f"Invalid template '{path.name}': {message}")


def _render_jinja_template(template_text: str, params: Mapping[str, Any], *, path: Path) -> str:
    try:
        return _JINJA_ENV.from_string(template_text).render(**params)
    except SecurityError as exc:
        raise _template_value_error(path, f"unsafe template expression: {exc}") from exc
    except UndefinedError as exc:
        raise _template_value_error(path, f"undefined variable: {exc}") from exc
    except TemplateSyntaxError as exc:
        raise _template_value_error(path, f"syntax error: {exc}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _load_template_document(path: Path, text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _template_value_error(path, str(exc)) from exc
    if not isinstance(document, dict):
        raise _template_value_error(path, "top level must be a mapping")
    return document


def _validate_template_document(template: Mapping[str, Any], path: Path) -> TemplateDocument:
    try:
        document = TemplateDocument.model_validate(template)
    except ValidationError as exc:
        raise _template_value_error(path, f"document validation failed: {_format_validation_error(exc)}") from exc
    expected_name = path.name.removesuffix(".yaml.j2")
    if document.name != expected_name:
        raise _template_value_error(path, f"name must match filename stem '{expected_name}'")
    return document


def _validate_dependency_cycle(template_name: str, todos: list[dict[str, Any]]) -> None:
    states: dict[int, int] = {}
    stack: list[int] = []

    def visit(node_id: int) -> None:
        state = states.get(node_id, 0)
        if state == 1:
            cycle_start = stack.index(node_id)
            cycle = [*stack[cycle_start:], node_id]
            cycle_str = ", ".join(str(node) for node in cycle)
            msg = f"template '{template_name}' has dependency cycle: {cycle_str}"
            raise ValueError(msg)
        if state == 2:
            return
        states[node_id] = 1
        stack.append(node_id)
        for dep_id in todos[node_id - 1].get("depends_on", []):
            visit(dep_id)
        stack.pop()
        states[node_id] = 2

    for node_id in range(1, len(todos) + 1):
        visit(node_id)


def _load_template_metadata(path: Path) -> dict[str, str]:
    template = _load_template_document(path, path.read_text(encoding="utf-8"))
    expected_name = path.name.removesuffix(".yaml.j2")
    name = template.get("name")
    version = template.get("version")
    description = template.get("description")
    if not isinstance(name, str) or name != expected_name:
        raise _template_value_error(path, f"name must match filename stem '{expected_name}'")
    if not isinstance(version, str):
        raise _template_value_error(path, "version must be a string")
    if not isinstance(description, str):
        raise _template_value_error(path, "description must be a string")
    return {"name": name, "version": version, "description": description}


def _validate_depends_on_indexes(todos: list[dict[str, Any]], *, path: Path) -> None:
    total_items = len(todos)
    for entry in todos:
        for dep in entry.get("depends_on", []):
            if dep < 1 or dep > total_items:
                raise _template_value_error(path, f"depends_on index {dep} is out of range 1..{total_items}")


def _expanded_template_index(
    expanded: list[dict[str, Any]],
    *,
    start_index: int,
    end_index: int,
) -> _ExpandedTemplateIndex:
    indexes = set(range(start_index, end_index + 1))
    roots: list[int] = []
    referenced: set[int] = set()
    for index in range(start_index, end_index + 1):
        internal_deps = [dep for dep in expanded[index - 1].get("depends_on", []) if dep in indexes]
        if not internal_deps:
            roots.append(index)
        referenced.update(internal_deps)
    terminals = tuple(index for index in range(start_index, end_index + 1) if index not in referenced)
    return _ExpandedTemplateIndex(roots=tuple(roots), terminals=terminals)


def _render_template_definition(
    name: str,
    params: dict[str, Any],
    *,
    template_roots: Sequence[_TemplateRoot],
    depth: int = 1,
) -> dict[str, Any]:
    if depth > _TEMPLATE_RECURSION_LIMIT:
        msg = f"Template recursion depth exceeded while expanding '{name}'"
        raise ValueError(msg)

    path, template_root = _resolve_template_path(name, template_roots)
    raw_text = path.read_text(encoding="utf-8")
    raw_template = _load_template_document(path, raw_text)
    _validate_template_document(raw_template, path)
    schema = _PARAMS_SCHEMAS.get(name) if template_root.source == "builtin" else None
    if schema is None:
        resolved_params = dict(params)
    else:
        try:
            resolved_params = schema.model_validate(params).model_dump(mode="python")
        except ValidationError as exc:
            raise _template_value_error(path, f"params validation failed: {_format_validation_error(exc)}") from exc

    rendered_text = _render_jinja_template(raw_text, resolved_params, path=path)
    rendered_template = _load_template_document(path, rendered_text)
    rendered_document = _validate_template_document(rendered_template, path)
    rendered_todos = rendered_document.model_dump(mode="python", exclude_none=True)["todos"]
    _validate_depends_on_indexes(rendered_todos, path=path)
    expanded_todos = _expand_template_todos(rendered_todos, template_roots=template_roots, depth=depth)
    _validate_dependency_cycle(rendered_document.name, expanded_todos)

    return {
        "name": rendered_document.name,
        "version": rendered_document.version,
        "description": rendered_document.description,
        "resolved_params": resolved_params,
        "todos": expanded_todos,
    }


def _expand_template_todos(
    todos: list[dict[str, Any]],
    *,
    template_roots: Sequence[_TemplateRoot],
    depth: int,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    index_map: dict[int, _ExpandedTemplateIndex] = {}

    for original_index, entry in enumerate(todos, start=1):
        if entry.get("title") is not None:
            expanded.append(
                {
                    "title": entry["title"],
                    "priority": entry.get("priority", "medium"),
                    "assigned_agent": entry.get("assigned_agent", ""),
                    "depends_on": [],
                    "_parent_depends_on": list(entry.get("depends_on", [])),
                },
            )
            flat_index = len(expanded)
            index_map[original_index] = _ExpandedTemplateIndex(roots=(flat_index,), terminals=(flat_index,))
            continue

        child_template = _render_template_definition(
            entry["sub_template"],
            entry.get("params", {}),
            template_roots=template_roots,
            depth=depth + 1,
        )
        offset = len(expanded)
        expanded.extend(
            {
                "title": child["title"],
                "priority": child.get("priority", "medium"),
                "assigned_agent": child.get("assigned_agent", ""),
                "depends_on": [dep + offset for dep in child.get("depends_on", [])],
                "_parent_depends_on": [],
            }
            for child in child_template["todos"]
        )
        child_index = _expanded_template_index(expanded, start_index=offset + 1, end_index=len(expanded))
        index_map[original_index] = child_index
        if entry.get("depends_on"):
            for root_index in child_index.roots:
                expanded[root_index - 1]["_parent_depends_on"].extend(entry["depends_on"])

    for item in expanded:
        for dep in item.pop("_parent_depends_on"):
            item["depends_on"].extend(index_map[dep].terminals)

    return expanded


def _format_param_value(value: object) -> str:
    return f"`{value}`"


def _format_template_preview(
    template_name: str,
    version: str,
    resolved_params: Mapping[str, Any],
    todos: list[dict[str, Any]],
) -> str:
    lines = [f"Template `{template_name}` v{version}", ""]
    if resolved_params:
        lines.append("Resolved params:")
        for key, value in resolved_params.items():
            lines.append(f"- `{key}`: {_format_param_value(value)}")
        lines.append("")
    lines.append("Preview:")
    for index, item in enumerate(todos, start=1):
        deps = item.get("depends_on", [])
        dep_suffix = f" (depends on {', '.join(str(dep) for dep in deps)})" if deps else ""
        lines.append(f"- {index}. [{item.get('priority', 'medium')}] {item['title']}{dep_suffix}")
    return "\n".join(lines)


def _format_template_apply_result(
    template_name: str,
    version: str,
    resolved_params: Mapping[str, Any],
    created_items: list[dict[str, Any]],
) -> str:
    lines = [f"Applied template `{template_name}` v{version}: created {len(created_items)} todo(s).", ""]
    if resolved_params:
        lines.append("Resolved params:")
        for key, value in resolved_params.items():
            lines.append(f"- `{key}`: {_format_param_value(value)}")
        lines.append("")
    lines.append("Created:")
    lines.extend(f"- `{item['id']}` {item['title']}" for item in created_items)
    return "\n".join(lines)


def _format_templates_table(templates: list[dict[str, Any]]) -> str:
    lines = ["| source | name | version | description | json schema |", "| --- | --- | --- | --- | --- |"]
    for template in templates:
        json_schema = template["json_schema"] or "-"
        lines.append(
            f"| `{template['source']}` | `{template['name']}` | `{template['version']}` | "
            f"{template['description']} | {json_schema} |",
        )
    return "\n".join(lines)


def _current_scope() -> tuple[Path, str, str, str]:
    ctx = get_tool_runtime_context()
    if ctx is None:
        msg = "todo requires an active tool runtime context"
        raise RuntimeError(msg)
    thread_id = ctx.resolved_thread_id or ctx.thread_id or "main"
    return _state_root(ctx.runtime_paths), ctx.room_id, thread_id, ctx.agent_name


def _configured_agent_names() -> set[str]:
    ctx = get_tool_runtime_context()
    if ctx is None:
        return set()
    return set((ctx.config.agents or {}).keys())


def _unknown_assigned_agent_message(agent_name: str, configured: set[str]) -> str | None:
    if agent_name and configured and agent_name not in configured:
        available = ", ".join(sorted(configured)) or "none"
        return f"Unknown agent '{agent_name}'. Available: {available}"
    return None


def _default_assignee(agent: Agent | Team, context_agent_name: str) -> str:
    configured = _configured_agent_names()
    if agent_config_key := _agent_config_key(agent, configured):
        return agent_config_key
    if context_agent_name in configured:
        return context_agent_name
    return ""


class TodoTools(Toolkit):
    """Tools for managing per-thread work plans with dependencies."""

    def __init__(self) -> None:
        super().__init__(
            name="todo",
            instructions=(
                "Use these tools to manage a per-thread work plan with dependencies. "
                "Create plans, add tasks, complete items, update priorities, and use templates for repeated workflows. "
                "Items are scoped to the current conversation thread."
            ),
            tools=[
                self.plan,
                self.add_todo,
                self.list_todos,
                self.update_todo,
                self.apply_template,
                self.list_templates,
            ],
        )

    def plan(self, agent: Agent | Team, tasks: str) -> str:
        """Create a multi-step work plan for the current thread."""
        state_root, room_id, thread_id, agent_name = _current_scope()
        assigned_agent = _default_assignee(agent, agent_name)
        path = _todos_path(state_root, room_id, thread_id)

        lines = [line.strip() for line in tasks.strip().splitlines() if line.strip()]
        if not lines:
            return "No tasks provided. Write one task per line."

        parsed: list[tuple[str, str]] = []
        for line in lines:
            priority = "medium"
            title = re.sub(r"^(\d+[\.)]\s*|[-*]\s+)", "", line).strip()
            for candidate in _VALID_PRIORITIES:
                prefix = f"[{candidate}] "
                if title.lower().startswith(prefix):
                    priority = candidate
                    title = title[len(prefix) :]
                    break
            if title:
                parsed.append((title, priority))

        if not parsed:
            return "No valid tasks found after parsing."

        def create_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {item["id"] for item in data["items"]}
            created: list[dict[str, Any]] = []
            now = _now_iso()
            for title, priority in parsed:
                new_id = _short_id(existing_ids)
                existing_ids.add(new_id)
                item = {
                    "id": new_id,
                    "title": title,
                    "status": "open",
                    "priority": priority,
                    "depends_on": [],
                    "assigned_agent": assigned_agent,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                data["items"].append(item)
                created.append(item)
            data["updated_at"] = now
            return created

        created = _locked_update_json(path, create_plan)
        result_lines = [f"Created {len(created)} item(s) in thread work plan:\n"]
        for item in created:
            marker = _PRIORITY_EMOJI.get(item["priority"], "")
            result_lines.append(f"- {marker} `{item['id']}` {item['title']} [{item['priority']}]")
        return "\n".join(result_lines)

    def add_todo(  # noqa: C901
        self,
        agent: Agent | Team,
        title: str,
        depends_on: str = "",
        priority: str = "medium",
        assigned_agent: str = "",
    ) -> str:
        """Add a single todo item to the current thread's work plan."""
        state_root, room_id, thread_id, agent_name = _current_scope()
        path = _todos_path(state_root, room_id, thread_id)
        clean_title = title.strip()
        if not clean_title:
            return "Title cannot be empty."

        priority = priority.lower()
        if priority not in _VALID_PRIORITIES:
            return f"Invalid priority '{priority}'. Must be: low, medium, high, critical."

        dep_ids = [dep.strip() for dep in depends_on.split(",") if dep.strip()] if depends_on else []
        resolved_agent = assigned_agent.strip() or _default_assignee(agent, agent_name)
        unknown_agent = _unknown_assigned_agent_message(resolved_agent, _configured_agent_names())
        if unknown_agent is not None:
            return unknown_agent

        def create_item(data: dict[str, Any]) -> dict[str, Any] | str | _NoWriteResult:
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {item["id"]: item for item in data["items"]}
            for dep_id in dep_ids:
                if dep_id not in items_by_id:
                    return _no_write(f"Dependency `{dep_id}` not found.")

            existing_ids = {item["id"] for item in data["items"]}
            new_id = _short_id(existing_ids)
            now = _now_iso()
            item = {
                "id": new_id,
                "title": clean_title,
                "status": "open",
                "priority": priority,
                "depends_on": dep_ids,
                "assigned_agent": resolved_agent,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }

            data["items"].append(item)
            data["updated_at"] = now
            return item

        if dep_ids and not path.exists():
            return f"Dependency `{dep_ids[0]}` not found."

        result = _locked_update_json(path, create_item)
        if isinstance(result, str):
            return result

        marker = _PRIORITY_EMOJI.get(priority, "")
        message = f"Created: {marker} `{result['id']}` **{clean_title}** [{priority}]"
        if dep_ids:
            message += f" (depends on {', '.join(f'`{dep_id}`' for dep_id in dep_ids)})"
        if resolved_agent:
            message += f" assigned to {resolved_agent}"
        return message

    def list_todos(self, agent: Agent | Team, show_all: bool = False) -> str:
        """List todo items in the current thread's work plan."""
        del agent
        state_root, room_id, thread_id, _agent_name = _current_scope()
        path = _todos_path(state_root, room_id, thread_id)
        state = _read_json(path)
        items = state.get("items", [])

        if not items:
            return "No items in this thread's work plan."

        items_by_id = {item["id"]: item for item in items}
        actionable = [item for item in items if _is_actionable(item, items_by_id)]
        blocked = [item for item in items if item["status"] == "open" and _is_blocked(item, items_by_id)]
        done = [item for item in items if item["status"] in _TERMINAL_STATUSES]
        actionable.sort(key=lambda item: _PRIORITY_ORDER.get(item.get("priority", "medium"), 9))

        result_lines = [f"Work plan: {len(done)}/{len(items)} complete.\n"]
        if actionable:
            result_lines.append("**Actionable:**")
            for item in actionable:
                marker = _PRIORITY_EMOJI.get(item.get("priority", "medium"), "")
                assigned = f" @{item['assigned_agent']}" if item.get("assigned_agent") else ""
                result_lines.append(
                    f"- {marker} `{item['id']}` {item['title']} [{item.get('priority', 'medium')}]{assigned}",
                )
        if blocked:
            result_lines.append("\n**Blocked:**")
            for item in blocked:
                waiting = [
                    dep_id
                    for dep_id in item.get("depends_on", [])
                    if items_by_id.get(dep_id, {}).get("status") not in _TERMINAL_STATUSES
                ]
                waiting_str = ", ".join(f"`{dep_id}`" for dep_id in waiting)
                result_lines.append(f"- `{item['id']}` {item['title']} waiting on {waiting_str}")
        if show_all and done:
            result_lines.append("\n**Done/Cancelled:**")
            for item in done:
                mark = "done" if item["status"] == "done" else "cancelled"
                result_lines.append(f"- {mark} `{item['id']}` {item['title']}")
        return "\n".join(result_lines)

    def update_todo(  # noqa: C901
        self,
        agent: Agent | Team,
        todo_id: str,
        title: str = "",
        priority: str = "",
        status: str = "",
        depends_on: str | None = None,
        assigned_agent: str = "",
    ) -> str:
        """Update fields on an existing todo item."""
        del agent
        state_root, room_id, thread_id, _agent_name = _current_scope()
        path = _todos_path(state_root, room_id, thread_id)

        if priority and priority.lower() not in _VALID_PRIORITIES:
            return f"Invalid priority '{priority}'. Must be: low, medium, high, critical."
        if status and status.lower() not in {"open", "done", "cancelled"}:
            return f"Invalid status '{status}'. Must be: open, done, cancelled."
        if assigned_agent and assigned_agent.strip():
            unknown_agent = _unknown_assigned_agent_message(assigned_agent.strip(), _configured_agent_names())
            if unknown_agent is not None:
                return unknown_agent
        clean_title = title.strip() if title else ""
        if title and not clean_title:
            return "Title cannot be empty."
        if not path.exists():
            return f"Todo `{todo_id}` not found."

        def do_update(data: dict[str, Any]) -> str | _NoWriteResult:  # noqa: C901, PLR0912
            _ensure_thread_state(data, room_id, thread_id)
            items_by_id = {item["id"]: item for item in data["items"]}
            if todo_id not in items_by_id:
                return _no_write(f"Todo `{todo_id}` not found.")

            item = items_by_id[todo_id]
            dep_ids: list[str] | None = None
            now = _now_iso()
            if depends_on is not None:
                dep_ids = [dep.strip() for dep in depends_on.split(",") if dep.strip()]
                for dep_id in dep_ids:
                    if dep_id not in items_by_id:
                        return _no_write(f"Dependency `{dep_id}` not found.")
                    if dep_id == todo_id:
                        return _no_write("Cannot depend on itself.")
                    if _would_create_cycle(items_by_id, todo_id, dep_id):
                        return _no_write(f"Adding dependency `{dep_id}` would create a cycle.")

            changes: list[str] = []
            if clean_title:
                item["title"] = clean_title
                changes.append(f"title='{clean_title}'")
            if priority:
                item["priority"] = priority.lower()
                changes.append(f"priority={priority.lower()}")
            if status:
                item["status"] = status.lower()
                item["completed_at"] = now if item["status"] == "done" else None
                changes.append(f"status={item['status']}")
            if dep_ids is not None:
                item["depends_on"] = dep_ids
                changes.append(f"depends_on={dep_ids}")
            if assigned_agent:
                item["assigned_agent"] = assigned_agent.strip()
                changes.append(f"assigned={assigned_agent.strip()}")
            if not changes:
                return _no_write("No fields to update.")

            item["updated_at"] = now
            data["updated_at"] = now
            unblocked_message = ""
            if status and status.lower() in _TERMINAL_STATUSES:
                unblocked = _newly_unblocked(data["items"], todo_id)
                if unblocked:
                    names = ", ".join(
                        f"`{unblocked_item['id']}` {unblocked_item['title']}" for unblocked_item in unblocked
                    )
                    unblocked_message = f"\nNow unblocked: {names}"
            return f"Updated `{todo_id}`: {', '.join(changes)}{unblocked_message}"

        return _locked_update_json(path, do_update)

    def apply_template(
        self,
        agent: Agent | Team,
        name: str,
        params: dict[str, str | int | bool],
        dry_run: bool = False,
    ) -> str:
        """Apply a named todo template to the current thread's work plan."""
        template_roots = _visible_template_roots(agent)
        rendered_template = _render_template_definition(name, params, template_roots=template_roots)
        if dry_run:
            return _format_template_preview(
                rendered_template["name"],
                rendered_template["version"],
                rendered_template["resolved_params"],
                rendered_template["todos"],
            )

        state_root, room_id, thread_id, agent_name = _current_scope()
        path = _todos_path(state_root, room_id, thread_id)
        default_assignee = _default_assignee(agent, agent_name)
        configured_agents = _configured_agent_names()
        for template_todo in rendered_template["todos"]:
            resolved_agent = template_todo.get("assigned_agent") or default_assignee
            unknown_agent = _unknown_assigned_agent_message(resolved_agent, configured_agents)
            if unknown_agent is not None:
                return unknown_agent

        def apply_template(data: dict[str, Any]) -> list[dict[str, Any]]:
            _ensure_thread_state(data, room_id, thread_id)
            existing_ids = {item["id"] for item in data["items"]}
            created: list[dict[str, Any]] = []
            now = _now_iso()
            for template_todo in rendered_template["todos"]:
                new_id = _short_id(existing_ids)
                existing_ids.add(new_id)
                item = {
                    "id": new_id,
                    "title": template_todo["title"],
                    "status": "open",
                    "priority": template_todo.get("priority", "medium"),
                    "depends_on": [],
                    "assigned_agent": template_todo.get("assigned_agent") or default_assignee,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                created.append(item)

            for item, template_todo in zip(created, rendered_template["todos"], strict=True):
                item["depends_on"] = [created[dep_index - 1]["id"] for dep_index in template_todo.get("depends_on", [])]

            data["items"].extend(created)
            data["updated_at"] = now
            return created

        created_items = _locked_update_json(path, apply_template)
        return _format_template_apply_result(
            rendered_template["name"],
            rendered_template["version"],
            rendered_template["resolved_params"],
            created_items,
        )

    def list_templates(self, agent: Agent | Team) -> str:
        """List available todo templates."""
        templates: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for template_root in _visible_template_roots(agent):
            templates_root = template_root.path.resolve()
            if not templates_root.is_dir():
                continue
            for path in sorted(templates_root.glob("*.yaml.j2")):
                resolved_path = path.resolve()
                if not resolved_path.is_relative_to(templates_root):
                    msg = f"Template '{path.name}' escapes templates dir via symlink"
                    raise ValueError(msg)
                try:
                    metadata = _load_template_metadata(path)
                except (OSError, ValueError):
                    if template_root.source == "workspace":
                        continue
                    raise
                if metadata["name"] in seen_names:
                    continue
                seen_names.add(metadata["name"])
                schema = _PARAMS_SCHEMAS.get(metadata["name"]) if template_root.source == "builtin" else None
                templates.append(
                    {
                        "source": template_root.source,
                        "name": metadata["name"],
                        "version": metadata["version"],
                        "description": metadata["description"],
                        "json_schema": json.dumps(schema.model_json_schema(), sort_keys=True) if schema else None,
                    },
                )
        return _format_templates_table(templates)
