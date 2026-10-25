"""Plan moving the team history that includes a renamed agent.

Each team history scope keeps a session store of its own (``history/session_context.py``): the state root
``teams/<store name>`` holding ``sessions/<store name>.db`` with the tables ``<store name>_sessions*``, where the
store name derives from the scope id. A team run is stored with the scope id as its team id, and each member's
run with the member's agent id, in its own row and nested in the team run.
A configured team's scope id is its name, so its store stays where it is; only the runs the agent wrote there
as a member, and the requirements it raised, take the new name.
An ad-hoc team's scope id lists its members, ``team_<members sorted, joined by +>``, followed by a suffix hashing
the requester when a member is private (``history/team_scope.py``). A scope with the agent therefore gets a new id
with the members sorted again and the suffix kept: its store moves to the new store name, and its tables, team ids
and history scope keys follow.
Names match exactly, never as substrings, so renaming ``code`` leaves ``team_code_team+helper`` alone.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import re
import sqlite3
from functools import partial
from typing import TYPE_CHECKING

from mindroom.agent_rename.session_store import StoreRename, database_steps, rename_session_database
from mindroom.agent_rename.sqlite_read import quote_identifier, read_only_connection
from mindroom.agent_rename.steps import RenameRefusedError, SqliteEdit
from mindroom.agent_rename.workers import move_steps
from mindroom.constants import resolve_session_state_root
from mindroom.history.session_context import TEAM_STATE_ROOT_DIRNAME, scope_session_storage_name
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# The directory of a team scope's state root holding its database (create_scope_session_storage's subdir).
_SESSIONS_DIRECTORY = "sessions"
# The head of an ad-hoc scope id, before its members, and the requester suffix after them (history/team_scope.py).
_AD_HOC_PREFIX = "team_"
_REQUESTER_SUFFIX = re.compile(r"_requester_[0-9a-f]{12}\Z")


def team_steps(runtime_paths: RuntimePaths, *, config: Config, old_name: str, new_name: str) -> list[Step]:
    """Return the steps that carry the agent's team history over to its new name.

    Configured teams with the agent as a member get their member runs renamed in place. Each stored ad-hoc scope
    with the agent as a member moves to its new scope id and has its tables, team ids and scope keys renamed with
    the member runs. With ``MINDROOM_SESSION_STORAGE_PATH`` set, the stores are found in that root.
    Refuses when a new store name is taken, or when a scope already has the new name as a member beside the old.
    """
    teams_root = resolve_session_state_root(runtime_paths.storage_root / TEAM_STATE_ROOT_DIRNAME, runtime_paths)
    steps: list[Step] = []
    for team_name, team in config.teams.items():
        if old_name in team.agents:
            steps += _configured_team_steps(teams_root, team_name, old_name=old_name, new_name=new_name)
    stores = sorted(teams_root.iterdir()) if teams_root.is_dir() else []
    for store in stores:
        scope_id = _stored_scope_id(store)
        if scope_id is None or scope_id in config.teams:
            continue
        new_scope_id = renamed_ad_hoc_scope_id(scope_id, old_name=old_name, new_name=new_name, where=str(store))
        if new_scope_id is not None:
            steps += _ad_hoc_team_steps(teams_root, scope_id, new_scope_id, old_name=old_name, new_name=new_name)
    return steps


def _configured_team_steps(teams_root: Path, team_name: str, *, old_name: str, new_name: str) -> list[Step]:
    """Rename the agent in the store of the configured team ``team_name``, which keeps its name and place."""
    storage_name = scope_session_storage_name(HistoryScope(kind="team", scope_id=team_name))
    database = teams_root / storage_name / _SESSIONS_DIRECTORY / f"{storage_name}.db"
    if not database.is_file():
        return []
    sessions_table = f"{storage_name}_sessions"
    return [
        SqliteEdit(
            database,
            partial(
                rename_session_database,
                old_table=sessions_table,
                new_table=sessions_table,
                rename=StoreRename(old_name, new_name),
            ),
            f"Rename agent {old_name} to {new_name} in the member runs of team {team_name} in {database}",
        ),
    ]


def _stored_scope_id(store: Path) -> str | None:
    """Return the scope id of the team store ``store``, or ``None`` for a directory holding no team history.

    The store's sessions carry their scope id as team id, and the store's name derives from it.
    """
    storage_name = store.name
    database = store / _SESSIONS_DIRECTORY / f"{storage_name}.db"
    if not database.is_file():
        return None
    sessions_table = f"{storage_name}_sessions"
    try:
        with read_only_connection(database) as connection:
            query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
            if connection.execute(query, (sessions_table,)).fetchone() is None:
                return None
            rows = connection.execute(
                f"SELECT DISTINCT team_id FROM {quote_identifier(sessions_table)} WHERE team_id IS NOT NULL",  # noqa: S608
            ).fetchall()
    except sqlite3.Error as error:
        msg = f"Cannot read the team history scope of {database}: {error}."
        raise RenameRefusedError(msg) from error
    return next(
        (
            team_id
            for (team_id,) in rows
            if isinstance(team_id, str)
            and team_id
            and scope_session_storage_name(HistoryScope(kind="team", scope_id=team_id)) == storage_name
        ),
        None,
    )


def renamed_ad_hoc_scope_id(scope_id: str, *, old_name: str, new_name: str, where: str) -> str | None:
    """Return the id the ad-hoc scope ``scope_id`` gets, or ``None`` when it has no member named ``old_name``.

    Refuses when the scope already has ``new_name`` as a member, since renaming would merge the two;
    ``where`` names the store holding the scope in that refusal.
    """
    if not scope_id.startswith(_AD_HOC_PREFIX):
        return None
    suffix_match = _REQUESTER_SUFFIX.search(scope_id)
    suffix = suffix_match.group() if suffix_match is not None else ""
    members = scope_id[len(_AD_HOC_PREFIX) : len(scope_id) - len(suffix)].split("+")
    if old_name not in members:
        return None
    if new_name in members:
        msg = f"Cannot rename the team history scope {scope_id} in {where}: {new_name} is already one of its members."
        raise RenameRefusedError(msg)
    # Without agent configs no member counts as private, so this is the id before the requester suffix.
    renamed = ad_hoc_team_scope_id([new_name if member == old_name else member for member in members], {})
    return None if renamed is None else renamed + suffix


def _ad_hoc_team_steps(
    teams_root: Path,
    scope_id: str,
    new_scope_id: str,
    *,
    old_name: str,
    new_name: str,
) -> list[Step]:
    """Move the store of the ad-hoc scope ``scope_id`` to the store name of ``new_scope_id`` and rename it there."""
    old_scope, new_scope = (
        HistoryScope(kind="team", scope_id=scope_id),
        HistoryScope(kind="team", scope_id=new_scope_id),
    )
    old_storage, new_storage = scope_session_storage_name(old_scope), scope_session_storage_name(new_scope)
    old_store, new_store = teams_root / old_storage, teams_root / new_storage
    steps = move_steps(old_store, new_store, f"the history of team scope {scope_id}", discard_new_state=False)
    steps += database_steps(
        old_store / _SESSIONS_DIRECTORY,
        new_store / _SESSIONS_DIRECTORY,
        old_stem=old_storage,
        new_stem=new_storage,
        edit=partial(
            rename_session_database,
            old_table=f"{old_storage}_sessions",
            new_table=f"{new_storage}_sessions",
            rename=StoreRename(old_name, new_name, old_scope=old_scope, new_scope=new_scope),
        ),
        edit_label=(
            f"Rename agent {old_name} to {new_name} and team scope {scope_id} to {new_scope_id} "
            "in the tables, team ids and history scopes of"
        ),
        move_label=f"the session database of team scope {scope_id}",
    )
    return steps
