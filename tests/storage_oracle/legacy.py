"""The legacy inputs of the oracle: the databases today's legacy tests build, and synthetic shapes of the current layout.

The legacy tests build their older databases inline, so their inputs are captured rather than rebuilt: the legacy
test files run under a plugin that dumps each database a migration is about to change, before it changes it. The
synthetic shapes are current-layout databases holding rows the conversion must handle and no current writer makes.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
import tempfile
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom import constants
from mindroom.history import legacy_compaction_state, summary_rows
from mindroom.history.storage import new_scope_session
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage import agent_storage
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import TableNames, table_names
from tests.storage_oracle.determinism import BASE_TIME, deterministic

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore

LEGACY_TEST_MODULES = (
    "tests.test_legacy_compaction_state",
    "tests.test_archive_migration",
    "tests.session_storage.test_legacy_agno2_runs",
)
_SESSION = "session"
# Runs a synthetic shape's folds name that have no row, whose folded tool calls the recording reads too.
EXTRA_RUN_IDS: dict[str, dict[str, list[str]]] = {
    "synthetic__orphan_segment": {_SESSION: ["orphan"]},
}


def _dump(connection: sqlite3.Connection) -> str:
    return "\n".join(connection.iterdump()) + "\n"


def _needs_migration(path: Path) -> bool:
    names = TableNames.of_database(path)
    with closing(sqlite3.connect(path)) as connection:
        tables = table_names(connection)
    return names.sessions in tables and not {names.summaries, names.usage} <= tables


class _Capture:
    """A pytest plugin that keeps each distinct database a legacy test hands to a migration, as it was before."""

    def __init__(self) -> None:
        self.dumps: dict[str, str] = {}
        self._test = "unknown"
        self._lock = threading.Lock()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        self._test = item.nodeid
        return (yield)

    def keep(self, dump: str) -> None:
        with self._lock:
            if dump in self.dumps.values():
                return
            module, _, test = self._test.split("/")[-1].partition("::")
            stem = re.sub(
                r"[^A-Za-z0-9_]+",
                "_",
                f"{module.removesuffix('.py').removeprefix('test_')}__{test.removeprefix('test_')}",
            )
            name, number = stem, 1
            while name in self.dumps:
                number += 1
                name = f"{stem}_{number}"
            self.dumps[name] = dump

    def patches(self) -> list[AbstractContextManager[object]]:
        usage_migration = agent_storage.migrate_usage_database
        create_summary_rows = legacy_compaction_state._create_summary_rows

        def migrate_usage_database(path: Path, **options: object) -> None:
            if path.is_file() and _needs_migration(path):
                with closing(sqlite3.connect(path)) as connection:
                    self.keep(_dump(connection))
            usage_migration(path, **options)  # type: ignore[arg-type]

        def create_summary_rows_after_capture(connection: sqlite3.Connection, names: TableNames) -> None:
            self.keep(_dump(connection))
            create_summary_rows(connection, names)

        return [
            patch.object(agent_storage, "migrate_usage_database", migrate_usage_database),
            patch.object(legacy_compaction_state, "_create_summary_rows", create_summary_rows_after_capture),
        ]


def capture_legacy_test_inputs() -> dict[str, str]:
    """Run the legacy test files and return every distinct database they handed to a migration, by test name."""
    capture = _Capture()
    # The archive tests stamp their generations with ``time.time()``.
    modules = [importlib.import_module(name) for name in LEGACY_TEST_MODULES]
    time_modules = [module for module in modules if isinstance(vars(module).get("time"), ModuleType)]
    with ExitStack() as stack, deterministic(time_modules=time_modules):
        for active in capture.patches():
            stack.enter_context(active)
        exit_code = pytest.main(
            [
                *(str(module.__file__) for module in modules),
                "-o",
                "addopts=",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:tach",
            ],
            plugins=[capture],
        )
    # A test that no longer runs would silently drop its input.
    assert exit_code == pytest.ExitCode.OK, exit_code
    return capture.dumps


def _run(run_id: str, *, status: RunStatus = RunStatus.completed, **fields: object) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        session_id=_SESSION,
        status=status,
        content=f"answer {run_id}",
        created_at=BASE_TIME,
        messages=[Message(role="user", content=f"question {run_id}", id=f"{run_id}.u", created_at=BASE_TIME)],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
        },
        **fields,  # type: ignore[arg-type]
    )


def _agent_store(root: Path, *runs: AnyRunRecord) -> ConversationStore:
    store = create_state_storage("code", root)
    session = new_scope_session(session_id=_SESSION, scope_id="code", is_team=False)
    store.ensure_session_row(session)
    store.save_runs(session, list(runs))
    return store


def _fold_row(store: ConversationStore, segment: summary_rows.Segment) -> None:
    session = new_scope_session(session_id=_SESSION, scope_id="code", is_team=False)
    latest = summary_rows.latest_row(store, session_id=_SESSION, scope_key="agent:code")
    assert summary_rows.append_summary(
        store,
        session=session,
        scope_key="agent:code",
        based_on=latest.id if latest is not None else None,
        covered=[],
        summary=f"fold of {segment.run_id}",
        summary_model="summary-model",
        segment=segment,
    )


def _raw_run(store: ConversationStore, run_id: str, run_type: str, run_data: str, *, parent: str | None = None) -> None:
    with closing(sqlite3.connect(store.path)) as connection, connection:
        (next_index,) = connection.execute(
            'SELECT COALESCE(MAX(run_index), -1) + 1 FROM "code_sessions_runs"',
        ).fetchone()
        connection.execute(
            'INSERT INTO "code_sessions_runs" (run_id, session_id, run_type, agent_id, parent_run_id, status, '
            "run_index, run_data, created_at) VALUES (?, ?, ?, 'code', ?, 'COMPLETED', ?, ?, ?)",
            (run_id, _SESSION, run_type, parent, next_index, run_data, BASE_TIME),
        )


def _orphan_segment(root: Path) -> ConversationStore:
    """A fold whose run never stored a row, with folded messages, a tool, and an ended member."""
    store = _agent_store(root, _run("r0"))
    call = Message(
        role="assistant",
        id="orphan.b0",
        created_at=BASE_TIME,
        tool_calls=[{"id": "orphan.c0", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
    )
    result = Message(
        role="tool",
        id="orphan.t0",
        tool_call_id="orphan.c0",
        content="probe output",
        created_at=BASE_TIME,
    )
    member = _run("orphan-member", parent_run_id="orphan")
    _fold_row(
        store,
        summary_rows.Segment(
            run_id="orphan",
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: "$orphan",
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$ctx-orphan"],
            },
            messages=[call.to_dict(), result.to_dict()],
            tools=[ToolExecution(tool_call_id="orphan.c0", tool_name="probe", created_at=BASE_TIME).to_dict()],
            member_responses=[member.to_dict()],
        ),
    )
    return store


def _segment_tool_without_message(root: Path) -> ConversationStore:
    """A fold of a stored paused run whose segment holds a tool call no folded message names."""
    store = _agent_store(root, _run("r0"), _run("live", status=RunStatus.paused))
    call = Message(
        role="assistant",
        id="live.b0",
        created_at=BASE_TIME,
        tool_calls=[{"id": "live.c0", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
    )
    _fold_row(
        store,
        summary_rows.Segment(
            run_id="live",
            metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$live"},
            messages=[call.to_dict()],
            tools=[ToolExecution(tool_call_id="execution-only", tool_name="probe", created_at=BASE_TIME).to_dict()],
            member_responses=[],
        ),
    )
    return store


def _embedded_paused_member_advanced(root: Path) -> ConversationStore:
    """A paused team run embedding its paused member, whose own row completed before the team row's next save."""
    store = create_state_storage("code", root)
    session = new_scope_session(session_id=_SESSION, scope_id="squad", is_team=True)
    store.ensure_session_row(session)
    tool = ToolExecution(tool_call_id="m0.q", tool_name="dangerous", requires_confirmation=True, created_at=BASE_TIME)
    member = _run("m0", status=RunStatus.paused, parent_run_id="t0", tools=[tool])
    member.requirements = [RunRequirement(tool_execution=tool, id="m0.req")]
    team = TeamRunRecord(
        run_id="t0",
        team_id="squad",
        session_id=_SESSION,
        status=RunStatus.paused,
        created_at=BASE_TIME,
        messages=[Message(role="user", content="team task", id="t0.u", created_at=BASE_TIME)],
        requirements=[
            RunRequirement(tool_execution=tool, id="m0.req", member_agent_id="code", member_run_id="m0"),
        ],
        member_responses=[member],
    )
    store.save_runs(session, [member, team])
    advanced = _run("m0", parent_run_id="t0", tools=[tool])
    store.save_runs(session, [advanced])
    return store


def _duplicate_message_id(root: Path) -> ConversationStore:
    """A run whose stored messages hold one id twice."""
    store = _agent_store(root, _run("r0"))
    duplicated = _run("dup")
    duplicated.messages.append(Message(role="assistant", content="answer dup", id="dup.u", created_at=BASE_TIME))
    _raw_run(store, "dup", "agent", json.dumps(duplicated.to_dict()))
    return store


def _members_of_members(root: Path) -> ConversationStore:
    """A team run's member whose own member run names it as parent."""
    store = create_state_storage("code", root)
    session = new_scope_session(session_id=_SESSION, scope_id="squad", is_team=True)
    store.ensure_session_row(session)
    team = TeamRunRecord(
        run_id="t0",
        team_id="squad",
        session_id=_SESSION,
        status=RunStatus.completed,
        created_at=BASE_TIME,
    )
    store.save_runs(session, [_run("m0", parent_run_id="t0"), _run("mm0", parent_run_id="m0"), team])
    return store


def _workflow_rows(root: Path) -> ConversationStore:
    """An Agno workflow run row beside an agent run."""
    store = _agent_store(root, _run("r0"))
    _raw_run(store, "w0", "workflow", json.dumps({"run_id": "w0", "workflow_id": "flow", "status": "COMPLETED"}))
    return store


def _running_row(root: Path) -> ConversationStore:
    """A run an older release stored as ``RUNNING``."""
    return _agent_store(root, _run("r0"), _run("r1", status=RunStatus.running))


def _undecodable_run_data(root: Path) -> ConversationStore:
    """A run row whose ``run_data`` is not JSON."""
    store = _agent_store(root, _run("r0"))
    _raw_run(store, "bad", "agent", "{not json")
    return store


SYNTHETIC_SHAPES: dict[str, Callable[[Path], ConversationStore]] = {
    "orphan_segment": _orphan_segment,
    "segment_tool_without_message": _segment_tool_without_message,
    "embedded_paused_member_advanced": _embedded_paused_member_advanced,
    "duplicate_message_id": _duplicate_message_id,
    "members_of_members": _members_of_members,
    "workflow_rows": _workflow_rows,
    "running_row": _running_row,
    "undecodable_run_data": _undecodable_run_data,
}


def synthetic_inputs() -> dict[str, str]:
    """Return the dump of every synthetic shape, by name."""
    dumps: dict[str, str] = {}
    for name, build in SYNTHETIC_SHAPES.items():
        with tempfile.TemporaryDirectory() as root, deterministic():
            store = build(Path(root))
            with closing(sqlite3.connect(store.path)) as connection:
                dumps[f"synthetic__{name}"] = _dump(connection)
    return dumps


def write_legacy_inputs(fixtures: Path) -> None:
    """Replace the legacy input dumps under ``fixtures``: every captured test input and every synthetic shape."""
    directory = fixtures / "legacy"
    directory.mkdir(parents=True, exist_ok=True)
    for stale in [*directory.glob("*.sql"), *directory.glob("*.json")]:
        stale.unlink()
    inputs = {
        **{f"captured__{name}": dump for name, dump in capture_legacy_test_inputs().items()},
        **synthetic_inputs(),
    }
    for name, dump in inputs.items():
        (directory / f"{name}.sql").write_text(dump, encoding="utf-8")
