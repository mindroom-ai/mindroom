"""`mindroom agents rename`: rename an agent's config key and carry its state over to the new name."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path  # noqa: TC003 - Typer reads the option annotations at runtime
from typing import TYPE_CHECKING, cast

import typer

from mindroom.constants import matrix_state_file

from .agents import (
    apply_rename_plan,
    confirm_plan,
    exit_on_rename_errors,
    load_config_or_exit,
    print_applied,
    print_leftovers,
    print_plan,
    say,
    stored_accounts,
)
from .config import activate_cli_runtime

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.agent_rename.steps import FileRewrite, RenamePlan
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# The .env variable naming the agent that serves the Connections portal.
_CONNECTIONS_AGENT_VARIABLE = "MINDROOM_CONNECTIONS_AGENT"


@dataclass(frozen=True)
class _AgentRename:
    """One agent's config key and the key it is renamed to, with the Matrix ID it keeps."""

    old: str
    new: str
    # The stored account's ID, or the ID MindRoom generates for the agent when nothing is stored.
    user_id: str
    has_account: bool


def rename(
    old: str = typer.Argument(..., help="The agent's name in the config."),
    new: str = typer.Argument(..., help="The agent's new name."),
    apply: bool = typer.Option(False, "--apply", help="Make the changes; without it the plan is only printed."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking for confirmation."),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data.",
    ),
) -> None:
    """Rename an agent in the config and carry its state over to the new name.

    Run it with MindRoom stopped. It moves the agent's state, history, memory,
    team history, workers, credentials and tracking records to the new name,
    renames it in the event journal and matrix_state.yaml, and renames it in the
    config last. Its Matrix account and device stay.
    """
    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    config = load_config_or_exit(runtime_paths)
    with exit_on_rename_errors():
        _rename(runtime_paths, config, old=old, new=new, apply=apply, yes=yes)


def _rename(runtime_paths: RuntimePaths, config: Config, *, old: str, new: str, apply: bool, yes: bool) -> None:
    from mindroom.agent_rename.guard import require_supported_backends, stopped_runtime  # noqa: PLC0415

    rename = _agent_rename(runtime_paths, config, old=old, new=new)
    require_supported_backends(config, runtime_paths)
    if not apply:
        # Taking MindRoom's locks would create lock files, so a dry run plans without them.
        print_plan(_rename_header(rename), _checked_rename_plan(runtime_paths, config, rename))
        say("Dry run: nothing was changed. Rerun with --apply to make these changes.")
        return
    with stopped_runtime(runtime_paths):
        plan = _checked_rename_plan(runtime_paths, config, rename)
        print_plan(_rename_header(rename), plan)
        confirm_plan(yes=yes)
        result = apply_rename_plan(plan, kind="rename")
        say(f"agent {old} is now {new}.", label="[green]Renamed:[/green] ")
        print_applied(result)
        storage = runtime_paths.storage_root
        print_leftovers(storage, f"agent {old}", names=(old,), expected=frozenset(plan.staying))


def _agent_rename(runtime_paths: RuntimePaths, config: Config, *, old: str, new: str) -> _AgentRename:
    """Check that agent ``old`` can be renamed to ``new``, and return the rename with the Matrix ID the agent keeps."""
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.matrix.identity import MatrixID, managed_account_key  # noqa: PLC0415
    from mindroom.matrix_identifiers import agent_username_localpart  # noqa: PLC0415

    _require_renamable_agent(runtime_paths, config, old)
    _require_new_name(config, old=old, new=new)
    accounts = stored_accounts(runtime_paths)
    domain = config.get_domain(runtime_paths)
    account = accounts.get(managed_account_key(old))
    if account is None:
        username, account_domain = agent_username_localpart(old, runtime_paths), domain
    else:
        username, account_domain = account.username, account.domain or domain
    rename = _AgentRename(old, new, MatrixID.from_username(username, account_domain).full_id, account is not None)
    new_account = managed_account_key(new) in accounts
    # A run killed before its config edit has already written the new account, journal principal or state root.
    _refuse_unfinished_rename(runtime_paths, rename, new_account=new_account)
    if new_account:
        msg = (
            f"{matrix_state_file(runtime_paths)} already holds an account {managed_account_key(new)}, "
            f"left by an agent named {new}. Choose another new name."
        )
        raise RenameRefusedError(msg)
    if not rename.has_account:
        _refuse_journal_without_account(runtime_paths, old)
    return rename


def _require_renamable_agent(runtime_paths: RuntimePaths, config: Config, old: str) -> None:
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.constants import ROUTER_AGENT_NAME  # noqa: PLC0415

    if old == ROUTER_AGENT_NAME:
        msg = f"{old} is MindRoom's router, which cannot be renamed."
    elif old in config.teams:
        msg = f"{old} is a team; renaming teams is not supported yet."
    elif old not in config.agents:
        msg = f"{old} is not a configured agent in {runtime_paths.config_path}."
    elif config.agents[old].private is not None:
        msg = f"agent {old} is private (agents.{old}.private is set); renaming private agents is not supported yet."
    else:
        return
    raise RenameRefusedError(msg)


def _require_new_name(config: Config, *, old: str, new: str) -> None:
    """Refuse a new name the config would reject, or one MindRoom would mistake for another agent's.

    Worker keys spell an agent with ``normalize_worker_key_part``, and team runs tell members apart by Agno's
    ``url_safe_string`` id, so a name that matches the old one or another agent there would share their state.
    """
    from agno.utils.string import url_safe_string  # noqa: PLC0415

    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.config.main import AGENT_NAME_PATTERN, RESERVED_ENTITY_NAMES  # noqa: PLC0415
    from mindroom.tool_system.worker_routing import normalize_worker_key_part  # noqa: PLC0415

    worker_spelling = normalize_worker_key_part(new)
    worker_twin = next((name for name in config.agents if normalize_worker_key_part(name) == worker_spelling), None)
    agent_id = url_safe_string(new)
    id_twin = next((name for name in config.agents if name != old and url_safe_string(name) == agent_id), None)
    if new == old:
        msg = f"{new} is the agent's name already."
    elif not AGENT_NAME_PATTERN.fullmatch(new):
        msg = f"{new} is not a valid agent name; use letters, digits and _ only."
    elif new in RESERVED_ENTITY_NAMES:
        msg = f"{new} is reserved for MindRoom."
    elif new in config.agents:
        msg = f"{new} is already a configured agent."
    elif new in config.teams:
        msg = f"{new} is already a configured team."
    elif worker_twin is not None:
        msg = (
            f"{new} is written {worker_spelling} in worker keys, like agent {worker_twin}, so they would share workers."
        )
    elif id_twin is not None:
        msg = f"{new} has the id {agent_id}, like agent {id_twin}; team runs tell their members apart by that id."
    else:
        return
    raise RenameRefusedError(msg)


def _refuse_journal_without_account(runtime_paths: RuntimePaths, old: str) -> None:
    """Refuse when the journal has principals of agent ``old`` although no account of it is stored.

    The rename keeps the agent's Matrix account, so it cannot tell which principal the agent keeps.
    """
    from mindroom.agent_rename.journal import principal_user_ids  # noqa: PLC0415
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415

    database = event_journal_sqlite_path(runtime_paths.storage_root)
    found = principal_user_ids(database, agent=old)
    if not found:
        return
    principals = "; ".join(f"{old}@{user_id} ({counts})" for user_id, counts in found.items())
    state_path = matrix_state_file(runtime_paths)
    msg = (
        f"{state_path} holds no account agent_{old}, but the event journal {database} has rows for {principals}. "
        f"The rename keeps the agent's Matrix account; restore agent_{old} in {state_path} before renaming."
    )
    raise RenameRefusedError(msg)


def _manifest_of_this_rename(storage_root: Path, rename: _AgentRename) -> Path | None:
    """Return the newest backup manifest of a run that renamed ``rename.old`` to ``rename.new``, if any.

    ``apply_plan`` writes the manifest before the first step and lists every planned step, so a killed run's manifest
    holds the config edit renaming the agent, and the move of its state root when it has one. A manifest of any other
    rename is not this one's: restoring it would undo that rename. Backup names start with their UTC time.
    """
    from mindroom.agent_rename.steps import BACKUPS_DIRECTORY_NAME  # noqa: PLC0415
    from mindroom.tool_system.worker_routing import agent_state_root_path  # noqa: PLC0415

    # config_edit.config_entity_steps describes its edit like this.
    config_step = f"Rename the agent {rename.old} to {rename.new} in "
    state_root_move = (
        str(agent_state_root_path(storage_root, rename.old)),
        str(agent_state_root_path(storage_root, rename.new)),
    )
    for manifest in sorted((storage_root / BACKUPS_DIRECTORY_NAME).glob("*-rename/manifest.json"), reverse=True):
        try:
            recorded = json.loads(manifest.read_bytes())
        except (OSError, ValueError):
            continue
        steps = recorded.get("steps") if isinstance(recorded, dict) else None
        if isinstance(steps, list) and any(_is_step_of(step, config_step, state_root_move) for step in steps):
            return manifest
    return None


def _is_step_of(step: object, config_step: str, state_root_move: tuple[str, str]) -> bool:
    """Whether a manifest step is the given config edit or state root move."""
    if not isinstance(step, dict):
        return False
    entry = cast("dict[str, object]", step)
    moved = (entry.get("source"), entry.get("destination"))
    return moved == state_root_move or str(entry.get("description")).startswith(config_step)


def _unfinished_rename_hint(rename: _AgentRename, manifest: Path) -> str:
    return (
        f"Agent {rename.old} is still configured, so if an earlier rename to {rename.new} stopped before it finished, "
        f"restore what it changed from {manifest} by hand before running again."
    )


def _refuse_unfinished_rename(runtime_paths: RuntimePaths, rename: _AgentRename, *, new_account: bool) -> None:
    """Refuse when this rename looks killed before its config edit, naming what it left and its manifest.

    Such a run leaves the old name configured while the journal principal, state root or account of the new name
    exists already; its backup manifest lists what it changed. Without a manifest of this rename, the state under the
    new name is someone else's, and the usual refusals name it.
    """
    from mindroom.agent_rename.journal import principal_user_ids  # noqa: PLC0415
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415
    from mindroom.matrix.identity import managed_account_key  # noqa: PLC0415
    from mindroom.tool_system.worker_routing import agent_state_root_path  # noqa: PLC0415

    storage = runtime_paths.storage_root
    manifest = _manifest_of_this_rename(storage, rename)
    if manifest is None:
        return
    found: list[str] = []
    database = event_journal_sqlite_path(storage)
    counts = principal_user_ids(database, agent=rename.new).get(rename.user_id)
    if counts is not None:
        found.append(f"the event journal {database} has rows for {rename.new}@{rename.user_id} ({counts})")
    state_root = agent_state_root_path(storage, rename.new)
    if state_root.exists() or state_root.is_symlink():
        found.append(f"{state_root} exists")
    if new_account:
        found.append(f"{matrix_state_file(runtime_paths)} holds the account {managed_account_key(rename.new)}")
    if not found:
        return
    msg = (
        f"Agent {rename.old} is still configured, but {'; '.join(found)}, so an earlier rename to {rename.new} "
        f"stopped before it finished. Restore what it changed from {manifest} by hand before running again."
    )
    raise RenameRefusedError(msg)


def _rename_header(rename: _AgentRename) -> str:
    if rename.has_account:
        detail = f"it keeps its Matrix account {rename.user_id} and device:"
    else:
        detail = "it has no stored Matrix account yet:"
    return f"Plan to rename agent {rename.old} to {rename.new}; {detail}"


def _checked_rename_plan(runtime_paths: RuntimePaths, config: Config, rename: _AgentRename) -> RenamePlan:
    """Plan the rename; a destination that exists already points at this rename's manifest, as a killed run leaves one."""
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.agent_rename.workers import DestinationExistsError  # noqa: PLC0415

    try:
        return _rename_plan(runtime_paths, config, rename)
    except DestinationExistsError as exc:
        manifest = _manifest_of_this_rename(runtime_paths.storage_root, rename)
        if manifest is None:
            raise
        msg = f"{exc} {_unfinished_rename_hint(rename, manifest)}"
        raise RenameRefusedError(msg) from exc


def _rename_plan(runtime_paths: RuntimePaths, config: Config, rename: _AgentRename) -> RenamePlan:
    from mindroom.agent_rename.config_edit import config_entity_steps, env_value_steps  # noqa: PLC0415
    from mindroom.agent_rename.entity_files import (  # noqa: PLC0415
        entity_credential_steps,
        rewrite_worker_key_agent,
        unknown_worker_reports,
    )
    from mindroom.agent_rename.entity_state import entity_state_steps, matrix_state_key_step  # noqa: PLC0415
    from mindroom.agent_rename.indexes import index_steps  # noqa: PLC0415
    from mindroom.agent_rename.journal_entity import journal_entity_steps  # noqa: PLC0415
    from mindroom.agent_rename.links import link_steps  # noqa: PLC0415
    from mindroom.agent_rename.memory import memory_steps  # noqa: PLC0415
    from mindroom.agent_rename.side_stores import entity_file_steps  # noqa: PLC0415
    from mindroom.agent_rename.steps import FinalAction, RenamePlan  # noqa: PLC0415
    from mindroom.agent_rename.teams import team_steps  # noqa: PLC0415
    from mindroom.agent_rename.workers import worker_move_steps  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415

    old, new = rename.old, rename.new
    storage = runtime_paths.storage_root
    # Without a stored account the journal holds no principal of the agent (_agent_rename refuses otherwise),
    # so only its name columns change.
    journal_steps = journal_entity_steps(
        event_journal_sqlite_path(storage),
        old_name=old,
        new_name=new,
        user_id=rename.user_id,
        configured_teams=frozenset(config.teams),
    )
    side_steps, side_reports, side_staying = entity_file_steps(runtime_paths, config=config, old_name=old, new_name=new)
    worker_steps, worker_reports = worker_move_steps(
        runtime_paths,
        config=config,
        rewrite=partial(rewrite_worker_key_agent, old_name=old, new_name=new),
        discard_new_state=False,
    )
    index, index_reports, index_staying = index_steps(runtime_paths, config=config, old_name=old, new_name=new)
    worker_root_reports, unknown_roots = unknown_worker_reports(runtime_paths, old_name=old)
    account_step = matrix_state_key_step(runtime_paths, old_name=old, new_name=new)
    env_steps = env_value_steps(
        runtime_paths.env_path,
        variable=_CONNECTIONS_AGENT_VARIABLE,
        old_value=old,
        new_value=new,
    )
    config_steps = config_entity_steps(
        runtime_paths.config_path.expanduser(),
        old_name=old,
        new_name=new,
        storage_root=storage,
    )
    _require_writable((*(() if account_step is None else (account_step,)), *env_steps, *config_steps))
    # The journal goes first: its edit checks again in its transaction, and a failing first step leaves nothing to undo.
    # The state root moves before the stores inside it are edited and before the links into it follow it there,
    # matrix_state.yaml and .env follow every store, and the config edit is the last reversible step, so a run killed
    # before it still has the old name configured. The providers read the config that still names the old agent.
    ordered = (
        *journal_steps,
        *entity_state_steps(runtime_paths, old_name=old, new_name=new),
        *link_steps(runtime_paths, old_name=old, new_name=new),
        *memory_steps(runtime_paths, config=config, old_name=old, new_name=new),
        *team_steps(runtime_paths, config=config, old_name=old, new_name=new),
        *side_steps,
        *entity_credential_steps(runtime_paths, old_name=old, new_name=new),
        *worker_steps,
        *index,
        *(() if account_step is None else (account_step,)),
        *env_steps,
        *config_steps,
    )
    # Final actions always run after every reversible step; listing them last shows that.
    steps = (
        *(step for step in ordered if not isinstance(step, FinalAction)),
        *(step for step in ordered if isinstance(step, FinalAction)),
    )
    return RenamePlan(
        storage_root=storage,
        steps=steps,
        reports=(*side_reports, *worker_reports, *worker_root_reports, *index_reports),
        staying=(*side_staying, *unknown_roots, *index_staying),
    )


def _require_writable(steps: Iterable[FileRewrite]) -> None:
    """Refuse when this user cannot write a file the rename edits after every store moved, or the file's directory.

    Failing on such a file would undo the whole rename, so the dry run already refuses.
    A ``FileRewrite`` replaces its file with a new file written beside it, which needs the directory to be writable.
    """
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415

    for step in steps:
        if not os.access(step.path, os.W_OK):
            msg = f"Cannot write {step.path}, which the rename edits. Make it writable for this user and run again."
        elif not os.access(step.path.parent, os.W_OK | os.X_OK):
            msg = (
                f"Cannot write in {step.path.parent}, where the rename replaces {step.path}. "
                "Make the directory writable for this user and run again."
            )
        else:
            continue
        raise RenameRefusedError(msg)
