"""Plan moving an agent's state root and renaming the session and learning databases inside it.

An agent's state lives in ``agents/<name>/`` (``agent_state_root_path``), its conversation history in
``sessions/<name>.db`` with tables ``<name>_sessions`` and ``<name>_sessions_*``, and its learnings in
``learning/<name>.db``. History only loads runs whose agent id is the agent's name
(``history/run_visibility.py``'s ``run_in_scope``) and replays the summary rows of the scope ``agent:<name>``,
so after the move the tables, the agent ids and the scope keys inside the databases take the new name too.
The Matrix account keeps its username, device and token; only its key in ``matrix_state.yaml`` changes.
Names match exactly, never as substrings, so renaming ``code`` leaves ``code_team`` alone.
Building the steps only reads; every refusal happens here, before anything is written.
"""

from __future__ import annotations

import os
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom import yaml_io
from mindroom.agent_rename.session_store import StoreRename, database_steps, rename_session_database
from mindroom.agent_rename.steps import FileRewrite, RenameRefusedError
from mindroom.agent_rename.workers import move_steps
from mindroom.constants import matrix_state_file, resolve_session_state_root
from mindroom.history.types import HistoryScope
from mindroom.matrix.identity import managed_account_key
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from mindroom.agent_rename.steps import Step
    from mindroom.constants import RuntimePaths

# The learning store's own table (agno/db/base.py's learnings_table default).
_LEARNINGS_TABLE = "agno_learnings"


def entity_state_steps(runtime_paths: RuntimePaths, *, old_name: str, new_name: str) -> list[Step]:
    """Return the steps that move the agent's state root to the new name and rename its databases there.

    The state root ``agents/<OLD>`` moves to ``agents/<NEW>`` first, so the database steps address paths below
    the new root. With ``MINDROOM_SESSION_STORAGE_PATH`` set, the agent's root in that session root moves too.
    ``sessions/<OLD>.db`` and ``learning/<OLD>.db`` get their tables, indexes, schema-version rows, agent ids
    and history scope keys renamed in one transaction each, then become ``<NEW>.db``.
    An agent without a state root never ran and gets no steps. Refuses when a destination already exists.
    """
    storage_root = runtime_paths.storage_root
    old_root, new_root = agent_state_root_path(storage_root, old_name), agent_state_root_path(storage_root, new_name)
    old_session_root = resolve_session_state_root(old_root, runtime_paths)
    new_session_root = resolve_session_state_root(new_root, runtime_paths)
    steps: list[Step] = []
    if os.path.lexists(old_root):
        steps += move_steps(old_root, new_root, f"the state of agent {old_name}", discard_new_state=False)
    if old_session_root != old_root and os.path.lexists(old_session_root):
        steps += move_steps(
            old_session_root,
            new_session_root,
            f"the sessions of agent {old_name}",
            discard_new_state=False,
        )
    rename = StoreRename(
        old_name,
        new_name,
        old_scope=HistoryScope(kind="agent", scope_id=old_name),
        new_scope=HistoryScope(kind="agent", scope_id=new_name),
    )
    edit_label = f"Rename agent {old_name} to {new_name} in the tables, agent ids and history scopes of"
    steps += database_steps(
        old_session_root / "sessions",
        new_session_root / "sessions",
        old_stem=old_name,
        new_stem=new_name,
        edit=partial(
            rename_session_database,
            old_table=f"{old_name}_sessions",
            new_table=f"{new_name}_sessions",
            rename=rename,
        ),
        edit_label=edit_label,
        move_label=f"the session database of agent {old_name}",
    )
    steps += database_steps(
        old_root / "learning",
        new_root / "learning",
        old_stem=old_name,
        new_stem=new_name,
        edit=partial(
            rename_session_database,
            old_table=f"{old_name}_learning_sessions",
            new_table=f"{new_name}_learning_sessions",
            rename=rename,
            other_tables=(_LEARNINGS_TABLE,),
        ),
        edit_label=edit_label,
        move_label=f"the learning database of agent {old_name}",
    )
    return steps


def matrix_state_key_step(runtime_paths: RuntimePaths, *, old_name: str, new_name: str) -> FileRewrite | None:
    """Return the step that moves the agent's stored Matrix account to the new name's key in ``matrix_state.yaml``.

    MindRoom finds an agent's account under ``agent_<name>``; the username, password, device and token stay,
    so the agent keeps its Matrix ID and device.
    Returns ``None`` when the file holds no account for the old name: the agent never logged in, so there is
    no account to keep. Refuses when the file already holds an account for the new name.
    """
    path = matrix_state_file(runtime_paths)
    old_key, new_key = managed_account_key(old_name), managed_account_key(new_name)
    state = yaml_io.safe_load(path.read_bytes()) if path.exists() else None
    accounts = cast("dict[str, object]", state).get("accounts") if isinstance(state, dict) else None
    if not isinstance(accounts, dict):
        return None
    if new_key in accounts:
        msg = f"{path} already holds an account {new_key}."
        raise RenameRefusedError(msg)
    if old_key not in accounts:
        return None
    return FileRewrite(
        path,
        partial(_rename_account_key, old_key=old_key, new_key=new_key),
        f"Rename the account {old_key} in {path} to {new_key}; its username, device and access token stay",
    )


def _rename_account_key(content: bytes, *, old_key: str, new_key: str) -> bytes:
    state = yaml_io.safe_load(content)
    accounts = state.get("accounts") if isinstance(state, dict) else None
    if not isinstance(accounts, dict) or old_key not in accounts or new_key in accounts:
        msg = f"the accounts changed since planning: expected {old_key} and no {new_key}"
        raise RuntimeError(msg)
    # The account keeps its place among the others.
    state["accounts"] = {new_key if key == old_key else key: account for key, account in accounts.items()}
    # Dumped like matrix/state.py's _write_matrix_state_file.
    return yaml_io.safe_dump(state, default_flow_style=False, sort_keys=False).encode()
