"""Rewrite the event journal when an agent's config key is renamed.

The agent keeps its Matrix account and device, so nothing Matrix shows changes. What changes is every journal value
keyed by the agent's name: its principal ``<agent>@<Matrix ID>``, its turn records, the columns naming it, and the
JSON fields MindRoom wrote with its name, including the team documents of replies it took part in.
Names match exactly, never as substrings, so renaming ``code`` leaves ``code_team`` alone.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from agno.utils.string import url_safe_string

from mindroom.agent_rename.journal import PRINCIPAL_COLUMNS, format_counts, principal_row_counts
from mindroom.agent_rename.sqlite_read import quote_identifier, read_only_connection, text_columns
from mindroom.agent_rename.steps import RenameRefusedError, SqliteEdit
from mindroom.agent_rename.teams import renamed_ad_hoc_scope_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

# Columns holding an agent's config name verbatim (event_journal/schema.py): the agent a turn record is scoped to,
# the entity owning a reply or a scoped approval card, and the agent a scheduled or paused call runs as.
_NAME_COLUMNS = (
    ("turn_records", "agent_name"),
    ("reply_messages", "entity_name"),
    ("scheduled_call_approvals", "agent_name"),
    ("approval_continuation_calls", "invoking_agent"),
    ("approval_grant_cards", "entity_name"),
    ("approval_grant_cards", "invoking_agent"),
)
# A grant's scope key hashes its invoking agent and responder principal (event_journal/approval_grants.py), so a grant
# naming the agent in either could never match a call again; its entity is the agent too.
_GRANT_MATCH = (
    "principal_id = :principal OR responder_principal_id = :principal OR entity_name = :name OR invoking_agent = :name"
)
# ToolExecutionIdentity's fields naming agents (tool_system/worker_routing.py).
_IDENTITY_FIELDS = ("agent_name", "transport_agent_name")


@dataclass(frozen=True)
class _EntityRename:
    old: str
    new: str
    user_id: str
    configured_teams: frozenset[str]
    database: Path

    @property
    def old_principal(self) -> str:
        return f"{self.old}@{self.user_id}"

    @property
    def new_principal(self) -> str:
        return f"{self.new}@{self.user_id}"

    def name(self, value: object) -> object:
        """Return the new name for a value that is the old name, and any other value unchanged."""
        return self.new if value == self.old else value


def journal_entity_steps(
    database: Path,
    *,
    old_name: str,
    new_name: str,
    user_id: str,
    configured_teams: frozenset[str],
) -> list[SqliteEdit]:
    """Return the one edit that renames agent ``old_name`` to ``new_name`` in the event journal, if it names the agent.

    The agent keeps its Matrix account ``user_id`` and its device, so its principal ``<old_name>@<user_id>`` becomes
    ``<new_name>@<user_id>`` in every principal column with all of its rows. That includes its sync consumer and
    producer membership positions: the device's store continues their stream. Its turn records move to the new name,
    and so do the columns and JSON fields naming the agent; an ad-hoc team scope listing it takes the id of the
    renamed member set, while the scopes of ``configured_teams`` keep the team's name. Approval grants bound to the
    agent are dropped, as their scope keys hash its name.

    Refuses when the journal already has rows for ``new_name``, a principal ``<new_name>@<any ID>`` or turn records,
    or an ad-hoc team scope listing both names; the edit checks again in its transaction.
    A journal that was never created, or that names the agent nowhere, needs no edit.
    """
    if not database.exists():
        return []
    rename = _EntityRename(old_name, new_name, user_id, configured_teams, database)
    try:
        with read_only_connection(database) as connection:
            changes = _entity_changes(connection, rename)
    except (sqlite3.Error, OSError) as exc:
        msg = f"Cannot read the event journal {database}: {exc}"
        raise RenameRefusedError(msg) from exc
    parts: list[str] = []
    if changes.principal_rows:
        parts.append(
            f"rename principal {rename.old_principal} to {rename.new_principal} "
            f"({format_counts(changes.principal_rows)}), keeping the sync stream state its device continues",
        )
    if changes.name_rows:
        renamed = ", ".join(f"{table}.{column} ({count})" for (table, column), count in changes.name_rows.items())
        parts.append(f"rename agent {old_name} to {new_name} in {renamed}")
    if changes.json_rows:
        rewritten = ", ".join(f"{table}.{column} ({len(rows)})" for (table, column), rows in changes.json_rows.items())
        parts.append(f"rewrite the fields naming agent {old_name} and the ad-hoc team scopes listing it in {rewritten}")
    if changes.grants:
        parts.append(f"drop the approval grants bound to agent {old_name} ({changes.grants})")
    if not parts:
        return []
    description = f"Rewrite the event journal {database}: {'; '.join(parts)}"
    return [SqliteEdit(database=database, edit=partial(_rename_entity, rename=rename), description=description)]


@dataclass(frozen=True)
class _EntityChanges:
    """What renaming the agent changes in a journal, counted per table or column."""

    columns: dict[str, tuple[str, ...]]
    principal_rows: dict[str, int]
    name_rows: dict[tuple[str, str], int]
    # The rowid and rewritten text of each row of a JSON column whose value the rename changes.
    json_rows: dict[tuple[str, str], list[tuple[int, str]]]
    grants: int


def _entity_changes(connection: sqlite3.Connection, rename: _EntityRename) -> _EntityChanges:
    """Read what renaming the agent changes, refusing a journal that already has rows for the new name."""
    columns = text_columns(connection)
    _refuse_new_name_rows(connection, columns, rename)
    name_rows: dict[tuple[str, str], int] = {}
    for table, column in _NAME_COLUMNS:
        if column in columns.get(table, ()):
            quoted = quote_identifier(column)
            query = f"SELECT count(*) FROM {quote_identifier(table)} WHERE {quoted} = ?"  # noqa: S608 - quoted schema identifiers
            (count,) = connection.execute(query, (rename.old,)).fetchone()
            if count:
                name_rows[table, column] = count
    json_rows: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for spec in _JSON_COLUMNS:
        if rewrites := _json_rewrites(connection, columns, spec, rename):
            json_rows[spec.table, spec.column] = rewrites
    grants = 0
    if "approval_grants" in columns:
        query = f"SELECT count(*) FROM approval_grants WHERE {_GRANT_MATCH}"  # noqa: S608 - a fixed condition
        (grants,) = connection.execute(query, {"principal": rename.old_principal, "name": rename.old}).fetchone()
    principal_rows = principal_row_counts(connection, columns, rename.old_principal)
    return _EntityChanges(
        columns=columns,
        # Every grant naming the principal is dropped rather than renamed.
        principal_rows={table: count for table, count in principal_rows.items() if table != "approval_grants"},
        name_rows=name_rows,
        json_rows=json_rows,
        grants=grants,
    )


def _refuse_new_name_rows(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    rename: _EntityRename,
) -> None:
    """Refuse when the journal has rows under a principal of the new name, for any Matrix ID, or turn records for it."""
    prefix = f"{rename.new}@"
    counts: dict[str, int] = {}
    for table, names in columns.items():
        # substr instead of LIKE: `_` in an agent name is a LIKE wildcard.
        match = " OR ".join(
            f"substr({quote_identifier(column)}, 1, :length) = :prefix"
            for column in names
            if column in PRINCIPAL_COLUMNS
        )
        if match:
            query = f"SELECT count(*) FROM {quote_identifier(table)} WHERE {match}"  # noqa: S608 - quoted schema identifiers
            (count,) = connection.execute(query, {"length": len(prefix), "prefix": prefix}).fetchone()
            if count:
                counts[table] = count
    if "agent_name" in columns.get("turn_records", ()):
        query = "SELECT count(*) FROM turn_records WHERE agent_name = ?"
        (count,) = connection.execute(query, (rename.new,)).fetchone()
        if count:
            counts["turn_records"] = count
    if counts:
        msg = (
            f"The event journal {rename.database} already has rows for agent {rename.new} ({format_counts(counts)}), "
            f"left by an agent of that name, which renaming {rename.old} would mix with its own. "
            "Choose another new name."
        )
        raise RenameRefusedError(msg)


@dataclass(frozen=True)
class _JsonColumn:
    """A JSON column whose values name agents, and how MindRoom writes it."""

    table: str
    column: str
    rewrite: Callable[[object, _EntityRename], object]
    dump: Callable[[object], str]


def _json_rewrites(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    spec: _JsonColumn,
    rename: _EntityRename,
) -> list[tuple[int, str]]:
    """Return the rowid and rewritten text of each row whose JSON value the rename changes."""
    if spec.column not in columns.get(spec.table, ()):
        return []
    quoted = quote_identifier(spec.column)
    # Every value the rename changes holds the old name verbatim, as agent names hold no character JSON escapes.
    query = f"SELECT rowid, {quoted} FROM {quote_identifier(spec.table)} WHERE instr({quoted}, ?) > 0"  # noqa: S608 - quoted schema identifiers
    rewrites: list[tuple[int, str]] = []
    for rowid, text in connection.execute(query, (rename.old,)).fetchall():
        value = _loaded(text)
        if value is None:
            continue
        renamed = spec.rewrite(value, rename)
        if renamed != value:
            rewrites.append((rowid, spec.dump(renamed)))
    return rewrites


def _loaded(text: object) -> object:
    """Return the JSON value of ``text``, or ``None`` when it holds none; MindRoom cannot read such a value either."""
    try:
        return json.loads(cast("str", text))
    except (RecursionError, TypeError, ValueError):
        return None


def _rename_entity(connection: sqlite3.Connection, *, rename: _EntityRename) -> None:
    """Rename the agent inside the caller's transaction, refusing again first; raising rolls everything back."""
    changes = _entity_changes(connection, rename)
    if changes.grants:
        connection.execute(
            f"DELETE FROM approval_grants WHERE {_GRANT_MATCH}",  # noqa: S608 - a fixed condition
            {"principal": rename.old_principal, "name": rename.old},
        )
    for (table, column), rewrites in changes.json_rows.items():
        update = f"UPDATE {quote_identifier(table)} SET {quote_identifier(column)} = ? WHERE rowid = ?"  # noqa: S608 - quoted schema identifiers
        connection.executemany(update, [(text, rowid) for rowid, text in rewrites])
    renames = [(table, column, rename.old, rename.new) for table, column in changes.name_rows]
    renames += [
        (table, column, rename.old_principal, rename.new_principal)
        for table, names in changes.columns.items()
        for column in names
        if column in PRINCIPAL_COLUMNS
    ]
    for table, column, old, new in renames:
        quoted = quote_identifier(column)
        update = f"UPDATE {quote_identifier(table)} SET {quoted} = ? WHERE {quoted} = ?"  # noqa: S608 - quoted schema identifiers
        connection.execute(update, (new, old))


def _with_fields(value: object, transforms: Mapping[str, Callable[[object], object]]) -> object:
    """Return the mapping ``value`` with each transform applied to the field it names; anything else as it is."""
    if not isinstance(value, dict):
        return value
    record = cast("dict[str, object]", value)
    return {**record, **{key: transform(record[key]) for key, transform in transforms.items() if key in record}}


def _field(value: object, key: str) -> object:
    return cast("dict[str, object]", value).get(key) if isinstance(value, dict) else None


def _names(rename: _EntityRename, *fields: str) -> dict[str, Callable[[object], object]]:
    """Return transforms renaming the agent in each of ``fields``."""
    return dict.fromkeys(fields, rename.name)


def _mapped(value: object, transform: Callable[[object], object]) -> object:
    return [transform(item) for item in cast("list[object]", value)] if isinstance(value, list) else value


def _renamed_pair(pair: object, rename: _EntityRename) -> object:
    """Rename the agent as the member of a ``[member, model]`` pair."""
    if not isinstance(pair, list) or not pair:
        return pair
    items = cast("list[object]", pair)
    return [rename.name(items[0]), *items[1:]]


def _renamed_bindings(value: object, rename: _EntityRename) -> object:
    """Move the agent's delegation storage binding to the new name; its display name is its name (delegation/storage.py)."""
    if not isinstance(value, dict):
        return value
    bindings = cast("dict[str, object]", value)
    display_name = partial(_with_fields, transforms=_names(rename, "display_name"))
    return {
        rename.new if key == rename.old else key: display_name(binding) if key == rename.old else binding
        for key, binding in bindings.items()
    }


def _renamed_scope(value: object, rename: _EntityRename) -> object:
    """Rename the agent's history scope, or an ad-hoc team scope listing it; a configured team's scope is its name."""
    scope_id = _field(value, "scope_id")
    if not isinstance(scope_id, str):
        return value
    scope = cast("dict[str, object]", value)
    if scope.get("kind") == "agent":
        return {**scope, "scope_id": rename.name(scope_id)}
    if scope.get("kind") != "team" or scope_id in rename.configured_teams:
        return value
    where = f"the event journal {rename.database}"
    new_scope_id = renamed_ad_hoc_scope_id(scope_id, old_name=rename.old, new_name=rename.new, where=where)
    return value if new_scope_id is None else {**scope, "scope_id": new_scope_id}


def _renamed_turn_record(value: object, rename: _EntityRename) -> object:
    """Rename the agent as a turn's response owner and in its history scope (handled_turns.py's TurnRecordCodec)."""
    return _with_fields(value, {"response_owner": rename.name, "history_scope": partial(_renamed_scope, rename=rename)})


def _renamed_continuation(value: object, rename: _EntityRename) -> object:
    """Rename the agent in the context a paused response stores (approval_continuations.py's ``_context``)."""
    names = partial(_mapped, transform=rename.name)
    # A CLI approval call's requirements name the team member that raised them (agent_cli/approval.py).
    requirement = partial(_with_fields, transforms=_names(rename, "member_agent_id"))
    cli_call = {"requirements": partial(_mapped, transform=requirement), "external_requirement": requirement}
    return _with_fields(
        value,
        {
            "execution_identity": partial(_with_fields, transforms=_names(rename, *_IDENTITY_FIELDS)),
            "origin": partial(_with_fields, transforms=_names(rename, "sender_entity_name", "requester_entity_name")),
            "team_member_names": names,
            "team_member_model_names": partial(_mapped, transform=partial(_renamed_pair, rename=rename)),
            "mentioned_agents": names,
            "delegation_storage_bindings": partial(_renamed_bindings, rename=rename),
            "history_scope": partial(_renamed_scope, rename=rename),
            "cli_call": partial(_with_fields, transforms=cli_call),
        },
    )


def _renamed_job(value: object, rename: _EntityRename) -> object:
    """Rename the agent as owner of a background tool job, and as caller or child of a delegation job.

    A job's owner is an execution identity (tool_jobs/runtime.py's BackgroundJob). A delegation job is known by the
    agent it runs and snapshots its child (delegation/background.py, delegation/state.py's DelegationChild).
    """
    identity = partial(_with_fields, transforms=_names(rename, *_IDENTITY_FIELDS))
    job = _with_fields(value, {"owner": identity})
    if _field(job, "kind") != "delegation":
        return job
    child = {
        **_names(rename, "caller_agent_name", "child_agent_name"),
        "execution_identity": identity,
        "storage_bindings": partial(_renamed_bindings, rename=rename),
    }
    adapter = partial(_with_fields, transforms={"child": partial(_with_fields, transforms=child)})
    return _with_fields(job, {**_names(rename, "tool_name"), "adapter": adapter})


def _renamed_team_segment(segment: object, rename: _EntityRename) -> object:
    """Rename the agent as a member of the team document a reply segment shows.

    A team document freezes each member as ``{"id", "config_name", ...}``, the id being the config name made URL safe,
    and names a member's slot ``agent:<id>`` in its hidden-tool separators and its tool calls' scope keys
    (teams.py's ``_TeamStreamPresentation``). An approval resume restores the document only when those agree with the
    continuation's member names, so the agent's entry, separators and scope keys all take the new name.
    """
    members = _field(_field(segment, "team_state"), "members")
    if not isinstance(members, list):
        return segment
    old_ids = [
        _field(member, "id") for member in cast("list[object]", members) if _field(member, "config_name") == rename.old
    ]
    if not old_ids:
        return segment
    new_id = url_safe_string(rename.new)
    old_slot, new_slot = f"agent:{old_ids[0]}", f"agent:{new_id}"

    def slot(value: object) -> object:
        return new_slot if value == old_slot else value

    def member(value: object) -> object:
        if _field(value, "config_name") != rename.old:
            return value
        return {**cast("dict[str, object]", value), "id": new_id, "config_name": rename.new}

    state = {"members": partial(_mapped, transform=member), "separate_next_scopes": partial(_mapped, transform=slot)}
    trace_entry = partial(_with_fields, transforms={"scope_key": slot})
    return _with_fields(
        segment,
        {"team_state": partial(_with_fields, transforms=state), "tool_trace": partial(_mapped, transform=trace_entry)},
    )


def _renamed_presentation(value: object, rename: _EntityRename) -> object:
    """Rename the agent in the team documents of a stored reply presentation (reply_presentation.py's codec)."""
    segment = partial(_renamed_team_segment, rename=rename)
    return _with_fields(value, {"segments": partial(_mapped, transform=segment), "trailing_note": segment})


def _renamed_rollback(value: object, rename: _EntityRename) -> object:
    """Rename the agent in the presentation a span's rollback keeps as encoded text (reply_spans.py's _rollback_json)."""

    def presentation(text: object) -> object:
        stored = _loaded(text)
        renamed = _renamed_presentation(stored, rename)
        return text if stored is None or renamed == stored else _compact_json(renamed)

    return _with_fields(value, {"presentation": presentation})


def _compact_json(value: object) -> str:
    """Encode like approval_continuations.py's ``_json`` and reply_presentation.py's ``encode_presentation``."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


# Turn records and tool jobs are written with json.dumps' defaults (handled_turns.py, tool_jobs/runtime.py).
# A reply's presentation and what it may have shown are presentations; reply_spans.py encodes a rollback compactly.
# The tool calls a span records (reply_tool_calls.entry_json) carry no scope key (reply_scope.py's _entry_json).
_JSON_COLUMNS = (
    _JsonColumn("turn_records", "record_json", _renamed_turn_record, json.dumps),
    _JsonColumn("approval_continuations", "context_json", _renamed_continuation, _compact_json),
    _JsonColumn("tool_jobs", "job_json", _renamed_job, json.dumps),
    _JsonColumn("reply_messages", "presentation_json", _renamed_presentation, _compact_json),
    _JsonColumn("reply_messages", "possibly_shown_json", _renamed_presentation, _compact_json),
    _JsonColumn("reply_spans", "rollback_json", _renamed_rollback, _compact_json),
)
