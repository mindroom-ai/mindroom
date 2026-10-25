"""Every side store keyed by an agent's name, written with MindRoom's own writers for code and its neighbors."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.agent_modes import set_agent_mode
from mindroom.automations.threads import automations_tracking_root
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import tracking_dir, workspace_avatar_path
from mindroom.custom_tools.matrix_message_idempotency import _digest
from mindroom.custom_tools.todo_poke import _scope_key, _TodoPokeScope
from mindroom.custom_tools.todo_state import locked_update_json, state_root, todos_path
from mindroom.delegation.records import DelegationMetadata, DelegationRecordOwner
from mindroom.delegation.state import DelegationChild
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.desktop.pairing import create_desktop_pairing
from mindroom.durable_write import write_json_file_durable
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerTarget
from mindroom.matrix.invited_rooms_store import invited_rooms_path, save_invited_rooms
from mindroom.matrix.personal_room_store import (
    PersonalRoomRecord,
    personal_room_record_path,
    write_personal_room,
)
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider
from mindroom.mcp_gateway.selection import GatewaySelections
from mindroom.mcp_gateway.types import GatewayOwner
from mindroom.memory.auto_flush import _flush_scopes
from mindroom.oauth.state import issue_opaque_oauth_state
from mindroom.report_publishing.store import PublishableReport, ReportPublishingStore
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore
from mindroom.shared_directories.store import (
    add_shared_directory_member,
    create_shared_directory,
)
from mindroom.thread_models import set_thread_model_override
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    serialize_tool_execution_identity,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.records import DelegationRecordHandle


OLD = "code"

# Its name extends the renamed agent's, so a substring match would wrongly touch it.
PREFIX = "code_team"
HELPER = "helper"
ALICE = "@alice:x"
ROOM = "!room:x"
THREAD = "$thread"
NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)

# Desktop pairing tokens live 15 minutes from this creation time.
PAIRED_AT = 1_800_000_000
OWNER = GatewayOwner(ALICE, ALICE)


def side_store_config(agent: str) -> Config:
    """Return the config naming the renamed agent ``agent`` beside ``code_team`` and ``helper``."""
    return Config(
        agents={name: AgentConfig(display_name=name.title()) for name in (agent, PREFIX, HELPER)},
        models={
            "default": ModelConfig(provider="test", id="test-model"),
            "fast": ModelConfig(provider="test", id="fast-model"),
        },
    )


def side_store_identity(agent: str) -> ToolExecutionIdentity:
    """Return the execution identity of ``agent`` serving alice in the thread."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id=ALICE,
        room_id=ROOM,
        thread_id=THREAD,
        resolved_thread_id=THREAD,
        session_id=f"session-{agent}",
    )


def workflow_spec(workflow_id: str, participant_agent: str) -> dict[str, object]:
    """Return a workflow whose one participant is the configured agent ``participant_agent``."""
    return {
        "schema_version": 1,
        "id": workflow_id,
        "name": f"Workflow {workflow_id}",
        "kind": "workflow",
        "inputs": {"type": "object", "properties": {"topic": {"type": "string"}}},
        "participants": [{"id": "member", "kind": "room_agent", "agent": participant_agent}],
        "workflow": [{"id": "ask", "type": "agent_step", "participant": "member", "prompt": "Answer."}],
        "outputs": [{"id": "report_html", "type": "html_report", "from_step": "ask"}],
        "permissions": {
            "max_runtime_seconds": 600,
            "max_concurrent_agents": 1,
            "max_total_agents": 1,
            "models": [],
            "tools": [],
            "data": {"matrix_history": "none", "attachments": "none", "knowledge_bases": []},
        },
    }


@dataclass(frozen=True)
class SideStoreInstall:
    """A storage tree holding every side store for ``code``, ``code_team`` and ``helper``, and handles into it."""

    runtime_paths: RuntimePaths
    # Delegation records by (caller, child).
    delegations: dict[tuple[str, str], DelegationRecordHandle]
    pairing_tokens: dict[str, str]
    oauth_tokens: dict[str, str]
    # Subagent handles by (caller, child).
    subagents: dict[tuple[str, str], str]
    # Public report slugs of each workflow owner's run report.
    published: dict[str, str]


def poke_scope(agent: str, requester_id: str | None) -> _TodoPokeScope:
    """Return the todo poke scope of ``agent`` for a requester, or for none."""
    return _TodoPokeScope(
        assigned_agent=agent,
        requester_id=requester_id,
        room_id=ROOM,
        thread_id=THREAD,
        actionable_items=(),
        latest_actionable_update=NOW,
        fingerprint="",
    )


def todo_item(item_id: str, agent: str) -> dict[str, object]:
    """Return an open todo assigned to ``agent``."""
    return {
        "id": item_id,
        "title": f"Work for {agent}",
        "status": "open",
        "priority": "medium",
        "depends_on": [],
        "assigned_agent": agent,
        "requester_id": ALICE,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "completed_at": None,
    }


def trigger_record(trigger_id: str, *, created_by: str, target: str) -> ExternalTriggerRecord:
    """Return an external trigger created by one agent that targets another."""
    return ExternalTriggerRecord(
        trigger_id=trigger_id,
        uid=f"uid-{trigger_id}",
        version=1,
        auth_epoch=1,
        enabled=True,
        owner_user_id=ALICE,
        created_by_agent_name=created_by,
        created_in_room_id=ROOM,
        target=ExternalTriggerTarget(room_id=ROOM, agent=target),
        auth="capability",
        capability_token_hash="a" * 64,
        delivery_mode="single_use",
        replay_window_seconds=300,
        max_body_bytes=4096,
        created_at=1,
        updated_at=1,
    )


def subagent_handle(caller: str, child: str, subagent_id: str, config: Config) -> dict[str, object]:
    """Return a subagent handle file as ``reserve_subagent_turn`` writes it."""
    record = DelegationChild(
        delegation_id=f"delegation-{subagent_id}",
        parent_tool_call_id="call",
        caller_agent_name=caller,
        child_agent_name=child,
        task="Investigate",
        session_id=f"session-{child}",
        run_id="run",
        model_name="default",
        depth=1,
        execution_identity=serialize_tool_execution_identity(side_store_identity(child)),
        subagent_id=subagent_id,
        storage_bindings=freeze_delegation_storage(config, [caller, child]),
        status="completed",
    )
    return {"owner": serialize_tool_execution_identity(side_store_identity(caller)), "child": asdict(record)}


async def write_async_stores(
    runtime_paths: RuntimePaths,
    config: Config,
) -> dict[tuple[str, str], DelegationRecordHandle]:
    """Write a settled delegation record for each pairing of callers and children, and the MCP gateway selections."""
    owner = DelegationRecordOwner(config, runtime_paths)
    handles: dict[tuple[str, str], DelegationRecordHandle] = {}
    for caller, child in ((HELPER, OLD), (OLD, HELPER), (PREFIX, HELPER)):
        metadata = DelegationMetadata(
            caller_agent_name=caller,
            child_agent_name=child,
            requester_id=ALICE,
            parent_run_id="parent-run",
            parent_tool_call_id="parent-call",
            source_room_id=ROOM,
            source_thread_id=THREAD,
            model_name="test-model",
            task=f"{caller} asks {child}",
        )
        handle = await owner.start(
            metadata,
            caller_execution_identity=side_store_identity(caller),
            child_execution_identity=side_store_identity(child),
        )
        await owner.finish(handle, status="completed", output="Done")
        handles[caller, child] = handle
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    await GatewaySelections(provider.store).set(OWNER, {OLD: None, PREFIX: ("calculator",), HELPER: None})
    return handles


def install_side_stores(tmp_path: Path) -> SideStoreInstall:  # noqa: PLR0915
    """Write every side store keyed by an agent's name with MindRoom's own writers, for ``code`` and its neighbors."""
    runtime_paths = test_runtime_paths(tmp_path)
    storage = runtime_paths.storage_root
    config = side_store_config(OLD)
    for agent in (OLD, PREFIX):
        root = agent_state_root_path(storage, agent)
        set_agent_mode(runtime_paths, root, agent, "s1", "minimal")
        save_invited_rooms(invited_rooms_path(runtime_paths, agent), {f"!{agent}-invited:x"})
        write_personal_room(
            personal_room_record_path(runtime_paths, agent, ALICE),
            PersonalRoomRecord(
                user_id=ALICE,
                alias=f"#{agent}-alice:x",
                source_room_id="!lobby:x",
                room_id=f"!{agent}-alice:x",
            ),
        )
        state_dir = automations_tracking_root(runtime_paths) / agent
        state_dir.mkdir(parents=True)
        (state_dir / "dreaming.json").write_text(json.dumps({"latest_run": f"{agent}-run"}))
        SyncContinuityStore(storage, agent).update_join_fences(add=[f"!{agent}-fence:x"])
        (storage / "calls" / agent).mkdir(parents=True)
        (storage / "calls" / agent / "2026-10-20_call.md").write_text(f"# Call with {agent}\n")
        avatar = workspace_avatar_path("agents", agent, runtime_paths)
        avatar.parent.mkdir(parents=True, exist_ok=True)
        avatar.write_bytes(f"png of {agent}".encode())

    set_thread_model_override(
        runtime_paths,
        thread_id="$t1",
        model_name="fast",
        entity_names=[OLD, PREFIX],
        config=config,
    )
    set_thread_model_override(runtime_paths, thread_id="$t2", model_name="fast", entity_names=[PREFIX], config=config)

    flush_keys = {scope.agent_name: scope.key for scope in _flush_scopes(config, runtime_paths, [OLD, PREFIX])}
    conversation = {"updated_at": 5, "marker": [5, "run"], "failures": 0, "failing_since": 0, "retry_at": 0}
    write_json_file_durable(
        tracking_dir(runtime_paths) / "memory_auto_flush.json",
        {
            "started_at": 1,
            "agents": {OLD: 2, PREFIX: 3},
            "scopes": {flush_keys[OLD]: {"s1": conversation}, flush_keys[PREFIX]: {"s2": conversation}},
        },
    )

    create_shared_directory(runtime_paths, owner=OLD, name="notes")
    (storage / "shared_directories" / OLD / "notes" / "plan.md").write_text("plan")
    for owner, name, members in (
        (OLD, "notes", (HELPER, PREFIX)),
        (HELPER, "specs", (OLD, PREFIX)),
        (PREFIX, "drafts", (HELPER,)),
    ):
        if owner != OLD:
            create_shared_directory(runtime_paths, owner=owner, name=name)
        for member in members:
            add_shared_directory_member(runtime_paths, owner=owner, name=name, member=member)

    todo_root = state_root(runtime_paths)
    items = [todo_item("a1", OLD), todo_item("a2", PREFIX), todo_item("a3", HELPER)]
    locked_update_json(
        todos_path(todo_root, ROOM, THREAD),
        lambda data: data.update(room_id=ROOM, thread_id=THREAD, items=items, updated_at=NOW.isoformat()),
    )
    poke = {"last_poked_at": 1.0, "last_fingerprint": "fingerprint", "unchanged_repoke_count": 1}
    scopes = {_scope_key(poke_scope(agent, requester)): poke for agent in (OLD, PREFIX) for requester in (None, ALICE)}
    locked_update_json(todo_root / "poke_state.json", lambda data: data.update(scopes=scopes))

    store = DynamicWorkflowStore(storage)
    published = {}
    for owner, participant in ((OLD, HELPER), (HELPER, OLD), (PREFIX, HELPER)):
        workflow_id = f"{owner.replace('_', '-')}-flow"
        store.create_workflow(
            spec=workflow_spec(workflow_id, participant),
            scope="agent",
            owner_id=owner,
            created_by=owner,
        )
        store.update_workflow(
            workflow_id=workflow_id,
            scope="agent",
            owner_id=owner,
            patch={"description": "second revision"},
            updated_by=owner,
            reason="test",
        )
        run = store.start_workflow_run(
            workflow_id=workflow_id,
            scope="agent",
            owner_id=owner,
            input_data={"topic": "x"},
            requested_by=ALICE,
            base_url="https://mindroom.example",
        )
        failed = store.fail_workflow_run(run, error="no input")
        report = PublishableReport(
            source_type="dynamic_workflow_run",
            source={"workflow_id": workflow_id, "run_id": failed.run_id, "scope": "agent"},
            artifact_path=store.run_report_html_artifact_path(failed),
            title=store.run_report_title(failed),
            requested_by=ALICE,
        )
        published[owner] = ReportPublishingStore(storage).publish_report(source=report, published_by=ALICE).slug

    triggers = {
        record.trigger_id: record.model_dump(mode="json")
        for record in (
            trigger_record("from-code", created_by=OLD, target=HELPER),
            trigger_record("to-code", created_by=HELPER, target=OLD),
            trigger_record("from-prefix", created_by=PREFIX, target=PREFIX),
        )
    }
    assert runtime_paths.control_state_root is not None
    write_json_file_durable(
        runtime_paths.control_state_root / "external_triggers" / "triggers.json",
        {"version": 1, "triggers": triggers},
        indent=2,
        sort_keys=True,
    )

    script_runs = ScriptRunStore(runtime_paths)
    for run_id, agent, state in (
        ("run-code", OLD, ScriptRunState.RUNNING),
        ("run-code-done", OLD, ScriptRunState.EXITED),
        ("run-prefix", PREFIX, ScriptRunState.RUNNING),
    ):
        script_runs.create_run(
            ScriptRunRecord(
                run_id=run_id,
                agent_name=agent,
                owner_user_id=ALICE,
                room_id=ROOM,
                source_digest="digest",
                grants=(ScriptToolGrant("website", "read_url"),),
                token_hash=f"token-{run_id}",
            ),
        )
        if state is not ScriptRunState.STARTING:
            script_runs.transition_run(run_id, state=state)

    sends = runtime_paths.control_state_root / "matrix_message_sends"
    sends.mkdir(parents=True)
    for agent in (OLD, PREFIX):
        scope = (ALICE, agent, ROOM)
        (sends / f"{_digest(scope)}.json").write_text(json.dumps({"version": 1, "scope": list(scope), "sends": {}}))

    subagents = {}
    for index, (caller, child) in enumerate(((OLD, HELPER), (HELPER, OLD), (PREFIX, HELPER))):
        subagent_id = f"{index:032x}"
        path = storage / "subagent_sessions" / f"{subagent_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(subagent_handle(caller, child, subagent_id, config)))
        subagents[caller, child] = subagent_id

    pairing_tokens = {
        agent: create_desktop_pairing(runtime_paths, requester_id=ALICE, agent_name=agent, now=PAIRED_AT).token
        for agent in (OLD, PREFIX)
    }
    oauth_tokens = {
        agent: issue_opaque_oauth_state(
            runtime_paths,
            kind="pending_oauth",
            ttl_seconds=600,
            data={"service": "google", "agent_name": agent, "worker_key": f"v1:default:shared:{agent}"},
        )
        for agent in (OLD, PREFIX)
    }
    oauth_tokens["by-key"] = issue_opaque_oauth_state(
        runtime_paths,
        kind="oauth_connect",
        ttl_seconds=600,
        data={"provider": "google", "agent_name": "", "worker_key": f"v1:default:shared:{OLD}"},
    )

    delegations = asyncio.run(write_async_stores(runtime_paths, config))
    return SideStoreInstall(runtime_paths, delegations, pairing_tokens, oauth_tokens, subagents, published)
