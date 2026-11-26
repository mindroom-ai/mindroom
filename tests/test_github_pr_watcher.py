"""Tests for native github_pr watcher: watch_pr tool, webhook fan-out, access checks, and ETag poller."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.approval.tool_approval import (
    evaluate_tool_approval,
    tool_may_require_approval,
    toolkit_approval_alias,
)
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig, GitHubEventSourceConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsUnreadableError
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.github_client import github_get_json
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers import (
    GitHubPRCheckpoint,
    GitHubPREventType,
    GitHubPRPoller,
    GitHubPRSourceSpec,
    ReactiveTriggerLimitError,
    ReactiveTriggerRecord,
    ReactiveTriggerService,
    ReactiveTriggerStore,
    TriggerEvent,
    TriggerTargetSpec,
    check_reactive_source_access,
    fan_out_external_event,
    verify_and_normalize_github_webhook,
)
from mindroom.reactive_triggers.github_pr import match_github_webhook_to_pr_rule
from mindroom.reactive_triggers.github_pr_poller import seed_github_pr_watch
from mindroom.reactive_triggers.management import set_trigger_enabled_sync
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_OWNER = "@alice:example.org"
_OTHER = "@bob:example.org"
_ROOM = "!lobby:example.org"
_THREAD = "$thread_root"
_SECRET_VALUE = "gh-webhook-secret"  # noqa: S105
_SECRET_ENV_NAME = "GH_SECRET"  # noqa: S105
_ALL_GH_EVENTS = [
    "pull_request",
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
    "check_run",
    "check_suite",
]


def _gh_source(
    allowed_repos: list[str],
    allowed_events: list[str] | None = None,
    subscribers: list[str] | None = None,
) -> GitHubEventSourceConfig:
    return GitHubEventSourceConfig(
        secret_env=_SECRET_ENV_NAME,
        allowed_repos=allowed_repos,
        allowed_events=list(_ALL_GH_EVENTS) if allowed_events is None else allowed_events,
        subscribers=[_OWNER] if subscribers is None else subscribers,
    )


def _config(
    tmp_path: Path,
    *,
    event_sources: dict[str, Any] | None = None,
    github_pr_polling_enabled: bool = True,
    min_github_pr_poll_interval_seconds: int = 60,
    max_github_pr_watches_per_owner: int = 20,
    env_values: dict[str, str] | None = None,
) -> tuple[Config, RuntimePaths]:
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env_values if env_values is not None else {_SECRET_ENV_NAME: _SECRET_VALUE},
    )
    state = MatrixState.load(runtime_paths=paths)
    state.add_room("lobby", _ROOM, "#lobby:example.org", "Lobby")
    save_matrix_state(state, paths)
    policy = ExternalTriggerPolicyConfig(
        event_sources=event_sources or {},
        github_pr_polling_enabled=github_pr_polling_enabled,
        min_github_pr_poll_interval_seconds=min_github_pr_poll_interval_seconds,
        max_github_pr_watches_per_owner=max_github_pr_watches_per_owner,
    )
    cfg = bind_runtime_paths(
        Config.model_validate(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-test"}},
                "agents": {
                    "dev": {
                        "display_name": "Dev",
                        "model": "default",
                        "rooms": ["lobby"],
                        "access": {"users": [_OWNER, _OTHER]},
                    },
                },
                "rooms": {"lobby": {"display_name": "Lobby"}},
                "external_trigger_policy": policy.model_dump(mode="json"),
            },
        ),
        paths,
    )
    return cfg, paths


def _room_client(*, resolved_thread: bool = False) -> SimpleNamespace:
    rooms = {_ROOM: SimpleNamespace(room_id=_ROOM, users={_OWNER: SimpleNamespace(user_id=_OWNER)})}
    tag_state = {"resolved": resolved_thread}
    resolved_key = json.dumps([_THREAD, "resolved"], separators=(",", ":"))
    tag_content = {"set_by": _OWNER, "set_at": "2026-11-25T10:00:00Z"}

    async def room_get_state(room_id: str) -> object:
        events: list[dict[str, Any]] = [
            {
                "type": "m.room.member",
                "state_key": _OWNER,
                "sender": _OWNER,
                "event_id": "$m1",
                "origin_server_ts": 1_000,
                "content": {"membership": "join"},
            },
        ]
        if tag_state["resolved"]:
            events.append(
                {
                    "type": THREAD_TAGS_EVENT_TYPE,
                    "state_key": resolved_key,
                    "sender": _OWNER,
                    "event_id": "$tag1",
                    "origin_server_ts": 1_001,
                    "content": tag_content,
                },
            )
        return nio.RoomGetStateResponse(events=events, room_id=room_id)

    async def room_get_state_event(room_id: str, event_type: str, state_key: str = "") -> object:
        if tag_state["resolved"] and event_type == THREAD_TAGS_EVENT_TYPE and state_key == resolved_key:
            return nio.RoomGetStateEventResponse(tag_content, event_type, state_key, room_id)
        if event_type == "m.room.power_levels":
            pl = {"users": {_OWNER: 100, "@mindroom_dev:example.org": 100}, "events_default": 0, "state_default": 50}
            return nio.RoomGetStateEventResponse(pl, event_type, "", room_id)
        if event_type == "m.room.create":
            return nio.RoomGetStateEventResponse({"creator": _OWNER, "room_version": "10"}, event_type, "", room_id)
        return nio.RoomGetStateEventError(message="not found", status_code="M_NOT_FOUND")

    async def room_put_state(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
        if event_type == THREAD_TAGS_EVENT_TYPE and state_key == resolved_key and not content:
            tag_state["resolved"] = False
        return nio.RoomPutStateResponse(event_id="$reopen_tag", room_id=room_id)

    return SimpleNamespace(
        user_id="@mindroom_dev:example.org",
        rooms=rooms,
        room_get_state=AsyncMock(side_effect=room_get_state),
        room_get_state_event=AsyncMock(side_effect=room_get_state_event),
        room_put_state=AsyncMock(side_effect=room_put_state),
        joined_members=AsyncMock(
            return_value=nio.JoinedMembersResponse(members=[nio.RoomMember(_OWNER, None, None)], room_id=_ROOM),
        ),
    )


def _tool_ctx(config: Config, paths: RuntimePaths, *, thread_id: str | None = _THREAD) -> ToolRuntimeContext:
    client = _room_client()
    orch = SimpleNamespace(
        agent_bots={"dev": SimpleNamespace(running=True, client=client, agent_name="dev")},
        agent_reply_memberships=AgentReplyMembershipIndex(),
        reactive_triggers=SimpleNamespace(notify_work=MagicMock()),
    )
    return make_test_tool_runtime_context(
        agent_name="dev",
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=thread_id, reply_to_event_id=None),
        requester_id=_OWNER,
        client=client,
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=orch,
    )


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _patch_github_http(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, httpx.Response],
    *,
    token_result: tuple[str | None, str] = ("ghp_alice", "token"),
) -> None:
    mock_client = _mock_client(lambda req: routes.get(req.url.path, httpx.Response(404)))
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=token_result),
    )

    async def _wrapped_get(
        path: str,
        *,
        token: str | None,
        etag: str | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = 2 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,  # noqa: ARG001
    ) -> object:
        return await github_get_json(
            path,
            token=token,
            etag=etag,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            client=mock_client,
        )

    monkeypatch.setattr("mindroom.reactive_triggers.github_pr_poller.github_get_json", _wrapped_get)


def _seeded_rule(
    store: ReactiveTriggerStore,
    trigger_id: str,
    *,
    repo: str = "mindroom-ai/mindroom",
    number: int = 10,
    events: list[GitHubPREventType] | None = None,
    webhook_source_id: str | None = "gh",
    owner_user_id: str = _OWNER,
    ignore_actors: list[str] | None = None,
    head_sha: str | None = "sha_v1",
    state: dict[str, Any] | None = None,
    poll_interval_seconds: int = 60,
    max_fires_per_hour: int = 10,
    auto_disable_on_close: bool = True,
) -> ReactiveTriggerRecord:
    rec = ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner_user_id,
        source=GitHubPRSourceSpec(
            repo=repo,
            number=number,
            events=list(events) if events is not None else ["activity", "ci", "merge", "close"],
            webhook_source_id=webhook_source_id,
            ignore_actors=list(ignore_actors or ()),
            auto_disable_on_close=auto_disable_on_close,
            poll_interval_seconds=poll_interval_seconds,
        ),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, thread_id=_THREAD, reopen_on_fire=True, prompt="Watch"),
        max_fires_per_hour=max_fires_per_hour,
        created_at=1000.0,
        updated_at=1000.0,
    )
    cp = GitHubPRCheckpoint(
        head_sha=head_sha,
        seeded=True,
        next_poll_at=1000.0,
        state=state or {},
        updated_at=1000.0,
    )
    store.create_trigger(rec, initial_github_pr_checkpoint=cp)
    return rec


def _send_webhook(
    store: ReactiveTriggerStore,
    config: Config,
    paths: RuntimePaths,
    *,
    source_id: str = "gh",
    source: GitHubEventSourceConfig,
    gh_event: str,
    delivery_id: str,
    payload: dict[str, Any],
    now: float = 1100.0,
) -> int:
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(_SECRET_VALUE.encode("utf-8"), body, hashlib.sha256).hexdigest()
    ev = verify_and_normalize_github_webhook(
        source_id=source_id,
        source=source,
        headers={"X-GitHub-Event": gh_event, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sig},
        body=body,
        runtime_paths=paths,
        now=now,
    )
    return fan_out_external_event(
        source_id=source_id,
        source=source,
        event=ev,
        store=store,
        config=config,
        runtime_paths=paths,
        now=now,
    )


def _gh_payload(
    gh_event: str,
    *,
    repo: str = "mindroom-ai/mindroom",
    number: int = 10,
    action: str = "created",
    actor: str = "bob",
    actor_type: str = "User",
    body: str = "LGTM",
    is_pr: bool = True,
    state: str = "approved",
    sha: str = "sha_v1",
    conclusion: str = "failure",
    pr_numbers: list[int] | None = None,
    merged: bool = False,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": actor, "type": actor_type},
    }
    user = {"login": actor, "type": actor_type}
    if gh_event == "issue_comment":
        issue: dict[str, Any] = {"number": number, "title": f"PR {number}"}
        if is_pr:
            issue["pull_request"] = {}
        return {**base, "issue": issue, "comment": {"id": 1, "body": body, "user": user}}
    if gh_event == "pull_request_review":
        return {
            **base,
            "action": "submitted",
            "pull_request": {"number": number, "title": f"PR {number}"},
            "review": {"id": 10, "state": state, "body": body, "user": user},
        }
    if gh_event == "pull_request_review_comment":
        return {
            **base,
            "pull_request": {"number": number, "title": f"PR {number}"},
            "comment": {"id": 12, "pull_request_review_id": 10, "path": "a.py", "body": body, "user": user},
        }
    if gh_event == "pull_request":
        return {
            **base,
            "pull_request": {"number": number, "merged": merged, "head": {"sha": sha}, "base": {"ref": "main"}},
        }
    prs = [{"number": n} for n in ([] if pr_numbers is None else pr_numbers)]
    if gh_event == "check_run":
        return {
            **base,
            "action": action,
            "check_run": {
                "id": 50,
                "name": "pytest",
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": sha,
                "pull_requests": prs,
            },
        }
    return {
        **base,
        "action": action,
        "check_suite": {
            "id": 60,
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": sha,
            "pull_requests": prs,
        },
    }


def _watch_pr_routes() -> dict[str, httpx.Response]:
    return {
        "/repos/mindroom-ai/mindroom/pulls/10": httpx.Response(
            200,
            json={"state": "open", "merged": False, "head": {"sha": "sha10"}, "title": "PR 10"},
        ),
        "/repos/mindroom-ai/private-repo/pulls/7": httpx.Response(404, json={"message": "Not Found"}),
        "/repos/matrix-org/synapse/pulls/99": httpx.Response(
            200,
            json={"state": "closed", "merged": True, "head": {"sha": "sha99"}, "title": "Merged"},
        ),
        "/repos/matrix-org/synapse/pulls/42": httpx.Response(
            200,
            json={"state": "open", "merged": False, "head": {"sha": "sha42"}, "title": "External PR"},
            headers={"ETag": '"pr-42"'},
        ),
        "/repos/matrix-org/synapse/issues/42/timeline": httpx.Response(
            200,
            json=[{"id": 1, "event": "commented", "user": {"login": "bob"}, "body": "old"}],
            headers={"ETag": '"tl-1"'},
        ),
        "/repos/matrix-org/synapse/pulls/42/comments": httpx.Response(
            200,
            json=[{"id": 5, "user": {"login": "bob"}, "body": "old inline"}],
        ),
        "/repos/matrix-org/synapse/commits/sha42/check-runs": httpx.Response(
            200,
            json={"check_runs": [{"id": 100, "name": "lint", "status": "completed", "conclusion": "success"}]},
            headers={"ETag": '"ci-1"'},
        ),
        "/repos/mindroom-ai/no-ci/pulls/3": httpx.Response(
            200,
            json={"state": "open", "merged": False, "head": {"sha": "sha3"}},
        ),
        "/repos/mindroom-ai/no-ci/commits/sha3/check-runs": httpx.Response(200, json={"check_runs": []}),
    }


@pytest.mark.asyncio
async def test_watch_pr_transport_selection_validation_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_pr picks webhook vs poll, rejects closed PRs, and enforces polling kill switch & owner quota."""
    gh_org = _gh_source(
        ["mindroom-ai/mindroom", "mindroom-ai/private-repo"],
        ["pull_request", "issue_comment", "pull_request_review", "check_run", "check_suite"],
    )
    gh_partial = _gh_source(["mindroom-ai/no-ci"], ["pull_request"])
    config, paths = _config(
        tmp_path,
        event_sources={"gh_org": gh_org, "gh_partial": gh_partial},
        min_github_pr_poll_interval_seconds=90,
        max_github_pr_watches_per_owner=3,
    )
    _patch_github_http(monkeypatch, _watch_pr_routes())
    tool = TriggersTools()

    with tool_runtime_context(_tool_ctx(config, paths)):
        wh = json.loads(await tool.watch_pr(repo="mindroom-ai/mindroom", number=10, trigger_id="pr_wh"))
        assert (wh["status"], wh["delivery_mode"], wh["webhook_source_id"], wh["head_sha"]) == (
            "ok",
            "webhook",
            "gh_org",
            "sha10",
        )
        assert (wh["trigger"]["target"]["thread_id"], wh["trigger"]["target"]["reopen_on_fire"]) == (_THREAD, True)
        assert any("pull_request_review_comment" in note for note in wh["coverage_notes"])

        priv = json.loads(
            await tool.watch_pr(repo="mindroom-ai/private-repo", number=7, target_thread_id="room", trigger_id="pr_p"),
        )
        assert (priv["status"], priv["delivery_mode"], priv["head_sha"], priv["trigger"]["target"]["thread_id"]) == (
            "ok",
            "webhook",
            None,
            None,
        )
        assert priv["coverage_notes"]

        closed = json.loads(await tool.watch_pr(repo="matrix-org/synapse", number=99))
        assert closed["status"] == "error"
        assert "already merged" in closed["message"]

        polled = json.loads(
            await tool.watch_pr(repo="matrix-org/synapse", number=42, poll_interval_seconds=30, trigger_id="pr_poll"),
        )
        assert (polled["status"], polled["delivery_mode"], polled["auth_mode"]) == ("ok", "poll", "token")
        assert polled["trigger"]["source"]["poll_interval_seconds"] == 90

        over_quota = json.loads(await tool.watch_pr(repo="mindroom-ai/no-ci", number=3, events=["ci"]))
        assert over_quota["status"] == "error"
        assert "limit" in over_quota["message"].lower()

        assert json.loads(await tool.triggers(action="delete", trigger_id="pr_p"))["status"] == "ok"
        fallback = json.loads(await tool.watch_pr(repo="mindroom-ai/no-ci", number=3, events=["ci"], trigger_id="fb"))
        assert fallback["delivery_mode"] == "poll"
        assert any("falling back to polling" in n for n in fallback["coverage_notes"])

    no_poll_cfg, no_poll_paths = _config(
        tmp_path / "nopoll",
        event_sources={"gh_org": gh_org},
        github_pr_polling_enabled=False,
    )
    with tool_runtime_context(_tool_ctx(no_poll_cfg, no_poll_paths)):
        blocked = json.loads(await tool.watch_pr(repo="matrix-org/synapse", number=42))
        assert blocked["status"] == "error"
        assert "polling is disabled" in blocked["message"].lower()
        allowed = json.loads(await tool.watch_pr(repo="mindroom-ai/mindroom", number=10))
        assert (allowed["status"], allowed["delivery_mode"]) == ("ok", "webhook")


@pytest.mark.asyncio
async def test_webhook_seeding_skips_pr_get_on_unreadable_pat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable PAT during webhook-only watch_pr fails closed without anonymous PR GET fallback."""
    gh_org = _gh_source(["mindroom-ai/mindroom"])
    config, paths = _config(tmp_path, event_sources={"gh_org": gh_org})
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(side_effect=CredentialsUnreadableError("corrupt PAT")),
    )
    get_mock = AsyncMock(side_effect=AssertionError("github_get_json must not be called"))
    monkeypatch.setattr("mindroom.reactive_triggers.github_pr_poller.github_get_json", get_mock)

    with tool_runtime_context(_tool_ctx(config, paths)):
        res = json.loads(await TriggersTools().watch_pr(repo="mindroom-ai/mindroom", number=10, trigger_id="pr_badpat"))

    assert (res["status"], res["delivery_mode"], res["head_sha"], res["pr_state"]) == ("ok", "webhook", None, "open")
    assert any("could not be read" in note.lower() for note in res["coverage_notes"])
    assert get_mock.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "actor", "text", "sample_data", "expected"),
    [
        ("activity", "dep[bot]", "cov", None, False),
        ("activity", "bob", "hi", {"number": 999}, False),
        ("activity", "bob", "hi", {"repo": "other/repo"}, False),
        ("ci", "actions[bot]", None, None, True),
        ("check_run.created", "actions[bot]", None, {"conclusion": "failure"}, False),
        ("check_run.completed", "actions[bot]", None, {"conclusion": "failure"}, True),
        ("check_run.completed", "actions[bot]", None, {"conclusion": "cancelled"}, False),
        ("check_run.completed", "actions[bot]", None, {"conclusion": "failure", "number": 999}, False),
        ("check_run.completed", "actions[bot]", None, {"conclusion": "failure", "sha": "stale_sha"}, False),
        ("check_suite.completed", "actions[bot]", None, {"conclusion": "success"}, False),
        ("issue_comment.created", "bob", "LGTM", None, True),
        ("issue_comment.created", "bob", "LGTM", {"number": 999}, False),
        ("issue_comment.created", "bob", "LGTM", {"is_pull_request": False}, False),
        ("pull_request.synchronize", "bob", None, {"head_sha": "sha_new"}, False),
        ("pull_request.merged", "mergify[bot]", None, {"merged": True}, True),
        ("github_pr.merged", "mergify[bot]", None, None, True),
        ("github_pr.closed", "alice", None, None, True),
    ],
)
async def test_triggers_management_and_accurate_dry_run_for_github_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    actor: str | None,
    text: str | None,
    sample_data: dict[str, Any] | None,
    expected: bool,
) -> None:
    """triggers(action='test') dry-run matches live github_pr rules for webhook kinds, numbers, SHAs, and categories."""
    config, paths = _config(tmp_path)
    _patch_github_http(monkeypatch, _watch_pr_routes())
    tool = TriggersTools()

    with tool_runtime_context(_tool_ctx(config, paths)):
        created = json.loads(
            await tool.watch_pr(repo="matrix-org/synapse", number=42, ignore_actors=["Alice"], trigger_id="pr_42"),
        )
        assert created["status"] == "ok"
        if kind == "activity" and actor == "dep[bot]":
            listed = json.loads(await tool.triggers(action="list"))
            assert [t["trigger_id"] for t in listed["reactive_triggers"]] == ["pr_42"]
            updated = json.loads(
                await tool.triggers(action="update", trigger_id="pr_42", prompt="New prompt", reopen_on_fire=False),
            )
            assert (updated["trigger"]["target"]["prompt"], updated["trigger"]["target"]["reopen_on_fire"]) == (
                "New prompt",
                False,
            )
            assert json.loads(await tool.triggers(action="history", trigger_id="pr_42"))["status"] == "ok"

        res = json.loads(
            await tool.triggers(
                action="test",
                trigger_id="pr_42",
                kind=kind,
                actor=actor,
                text=text,
                sample_data=sample_data,
            ),
        )
        assert res["status"] == "ok"
        assert res["matched"] is expected
        assert res["would_fire"] is expected


@pytest.mark.asyncio
async def test_webhook_activity_reviews_and_terminal_disable(tmp_path: Path) -> None:
    """Webhook fan-out filters comments/bots/review wrappers, tracks head_sha, and disables unsubscribed closes."""
    gh = _gh_source(["mindroom-ai/mindroom", "mindroom-ai/other"])
    config, paths = _config(tmp_path, event_sources={"gh": gh})
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_10", ignore_actors=["MutedUser"])
    _seeded_rule(store, "pr_other", repo="mindroom-ai/other", head_sha="sha_v2")
    _seeded_rule(store, "pr_act_only", number=11, events=["activity"], head_sha="sha11")

    def send(ev: str, did: str, payload: dict[str, Any]) -> int:
        return _send_webhook(store, config, paths, source=gh, gh_event=ev, delivery_id=did, payload=payload)

    assert send("issue_comment", "d1", _gh_payload("issue_comment", body="LGTM!")) == 1
    assert send("issue_comment", "d2", _gh_payload("issue_comment", is_pr=False)) == 0
    assert send("issue_comment", "d3", _gh_payload("issue_comment", actor="codecov[bot]", actor_type="Bot")) == 0
    assert send("issue_comment", "d4", _gh_payload("issue_comment", actor="muteduser")) == 0
    assert send("pull_request_review", "d5", _gh_payload("pull_request_review", state="commented", body="")) == 0
    assert send("pull_request_review", "d6", _gh_payload("pull_request_review", state="approved", body="")) == 1
    assert send("pull_request_review_comment", "d7", _gh_payload("pull_request_review_comment", body="nit")) == 1
    assert send("pull_request", "d8", _gh_payload("pull_request", action="synchronize", sha="sha_v2")) == 0
    cp = store.get_github_pr_checkpoint("pr_10")
    assert cp is not None
    assert cp.head_sha == "sha_v2"
    close_payload = _gh_payload(
        "pull_request",
        action="closed",
        actor="mergify[bot]",
        actor_type="Bot",
        merged=True,
        sha="sha_v2",
    )
    assert send("pull_request", "d9", close_payload) == 1
    latest = store.list_firings(trigger_id="pr_10", limit=1)[0]
    assert (latest.event.kind, latest.event.data["terminal"], latest.event.data["github_event"]) == (
        "github_pr.merged",
        True,
        "pull_request",
    )
    assert store.list_firings(trigger_id="pr_other") == []

    # Unsubscribed terminal close disables immediately; review wrapper without inline event fires
    unsub_close = _gh_payload("pull_request", number=11, action="closed", merged=False, sha="sha11")
    assert send("pull_request", "d10", unsub_close) == 0
    disabled = store.get_trigger("pr_act_only")
    assert disabled is not None
    assert (disabled.enabled, disabled.disabled_reason) == (False, "pr_closed")

    gh_no_inline = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "pull_request_review"])
    _seeded_rule(store, "pr_wrap", number=20, webhook_source_id="gh_ni", head_sha="sha20")
    cfg_ni, _ = _config(tmp_path, event_sources={"gh_ni": gh_no_inline})
    assert (
        _send_webhook(
            store,
            cfg_ni,
            paths,
            source_id="gh_ni",
            source=gh_no_inline,
            gh_event="pull_request_review",
            delivery_id="d_wrap",
            payload=_gh_payload("pull_request_review", number=20, state="commented", body=""),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_webhook_ci_stale_sha_cancelled_dedup_and_no_per_suite_all_green(tmp_path: Path) -> None:
    """Webhook CI rejects stale SHAs and cancelled runs, avoids duplicate check_suite failures, and never emits ci_passing."""
    gh_both = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "check_run", "check_suite"])
    gh_suite_only = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "check_suite"])
    config, paths = _config(tmp_path, event_sources={"gh": gh_both, "gh_suite": gh_suite_only})
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_10", webhook_source_id="gh", head_sha="sha_v2")
    _seeded_rule(store, "pr_no_sha", number=12, webhook_source_id="gh", head_sha=None)
    _seeded_rule(store, "pr_suite", number=13, webhook_source_id="gh_suite", head_sha="sha_s")

    def send(
        ev: str,
        did: str,
        *,
        source_id: str = "gh",
        source: GitHubEventSourceConfig = gh_both,
        sha: str = "sha_v1",
        conclusion: str = "failure",
        pr_numbers: list[int] | None = None,
    ) -> int:
        payload = _gh_payload(
            ev,
            action="completed",
            actor="actions[bot]",
            actor_type="Bot",
            sha=sha,
            conclusion=conclusion,
            pr_numbers=pr_numbers,
        )
        return _send_webhook(
            store,
            config,
            paths,
            source_id=source_id,
            source=source,
            gh_event=ev,
            delivery_id=did,
            payload=payload,
        )

    # Stale SHA rejected even when pr_numbers includes 10; number matching still works when head_sha is None
    assert send("check_run", "c_stale_cr", sha="sha_v1", pr_numbers=[10], conclusion="failure") == 0
    assert (
        send("check_suite", "c_stale_cs", source_id="gh_suite", source=gh_suite_only, sha="old", pr_numbers=[13]) == 0
    )
    assert send("check_run", "c_no_sha", sha="any_sha", pr_numbers=[12], conclusion="failure") == 1

    # Fork PR current SHA fires on failure; cancelled ignored; no duplicate check_suite failure when check_run allowed
    assert send("check_run", "c_fork_fail", sha="sha_v2", pr_numbers=[], conclusion="failure") == 1
    assert send("check_run", "c_cancelled", sha="sha_v2", pr_numbers=[10], conclusion="cancelled") == 0
    assert send("check_suite", "c_dup_fail", sha="sha_v2", pr_numbers=[10], conclusion="failure") == 0
    assert send("check_suite", "c_green", sha="sha_v2", pr_numbers=[10], conclusion="success") == 0

    # check_suite-only source fires on failure, ignores cancelled and success (no per-suite ci_passing)
    assert send("check_suite", "cs_fail", source_id="gh_suite", source=gh_suite_only, sha="sha_s", pr_numbers=[13]) == 1
    assert (
        send(
            "check_suite",
            "cs_cancel",
            source_id="gh_suite",
            source=gh_suite_only,
            sha="sha_s",
            pr_numbers=[13],
            conclusion="cancelled",
        )
        == 0
    )
    assert (
        send(
            "check_suite",
            "cs_ok",
            source_id="gh_suite",
            source=gh_suite_only,
            sha="sha_s",
            pr_numbers=[13],
            conclusion="success",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_webhook_allowed_events_and_subscriber_revocation(tmp_path: Path) -> None:
    """Revoking allowed_events blocks queued dispatch and hides history; subscriber/source revocation fails closed."""
    gh = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "issue_comment", "pull_request_review"])
    config, paths = _config(tmp_path, event_sources={"gh": gh})
    store = ReactiveTriggerStore(paths)
    rule = _seeded_rule(store, "pr_10")

    assert (
        _send_webhook(
            store,
            config,
            paths,
            source=gh,
            gh_event="issue_comment",
            delivery_id="d_c",
            payload=_gh_payload("issue_comment"),
        )
        == 1
    )
    assert (
        _send_webhook(
            store,
            config,
            paths,
            source=gh,
            gh_event="pull_request_review",
            delivery_id="d_r",
            payload=_gh_payload("pull_request_review", state="approved", body="ship it"),
        )
        == 1
    )
    firings = {f.dedupe_key: f for f in store.list_firings(trigger_id="pr_10")}

    # Revoke issue_comment from allowed_events while keeping pull_request_review
    gh_no_comment = gh.model_copy(update={"allowed_events": ["pull_request", "pull_request_review"]})
    cfg_no_comment, _ = _config(tmp_path, event_sources={"gh": gh_no_comment})
    blocked = await check_reactive_source_access(
        rule=rule,
        event=firings["d_c"].event,
        source_bot=None,
        config=cfg_no_comment,
        runtime_paths=paths,
        memberships=AgentReplyMembershipIndex(),
    )
    assert blocked is not None
    assert (blocked.status, blocked.reason) == ("skipped", "source_grant_revoked")
    assert (
        await check_reactive_source_access(
            rule=rule,
            event=firings["d_r"].event,
            source_bot=None,
            config=cfg_no_comment,
            runtime_paths=paths,
            memberships=AgentReplyMembershipIndex(),
        )
        is None
    )
    with tool_runtime_context(_tool_ctx(cfg_no_comment, paths)):
        hist = json.loads(await TriggersTools().triggers(action="history", trigger_id="pr_10"))
    assert hist["status"] == "ok"
    assert [f["dedupe_key"] for f in hist["firings"]] == ["d_r"]

    # Revoke subscriber -> fan-out blocked, dispatch skipped, never falls back to polling
    gh_no_sub = gh.model_copy(update={"subscribers": []})
    cfg_no_sub, _ = _config(tmp_path, event_sources={"gh": gh_no_sub})
    assert (
        _send_webhook(
            store,
            cfg_no_sub,
            paths,
            source=gh_no_sub,
            gh_event="issue_comment",
            delivery_id="d_after",
            payload=_gh_payload("issue_comment"),
        )
        == 0
    )
    sub_blocked = await check_reactive_source_access(
        rule=rule,
        event=firings["d_r"].event,
        source_bot=None,
        config=cfg_no_sub,
        runtime_paths=paths,
        memberships=AgentReplyMembershipIndex(),
    )
    assert sub_blocked is not None
    assert (sub_blocked.status, sub_blocked.reason) == ("skipped", "source_subscriber_revoked")
    assert store.list_due_github_pr_triggers(now=9_999_999.0) == []

    # Removed source denies history
    cfg_removed, _ = _config(tmp_path, event_sources={})
    with tool_runtime_context(_tool_ctx(cfg_removed, paths)):
        denied = json.loads(await TriggersTools().triggers(action="history", trigger_id="pr_10"))
    assert denied["status"] == "error"
    assert "no longer have access" in denied["message"]


def test_terminal_pr_close_auto_disables_on_skip_and_triage_no_not_retry(tmp_path: Path) -> None:
    """Busy webhook watch auto-disables on terminal mark_skipped, rate-limited claim, and mark_triaged_no, not defer."""
    _, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)

    def _terminal_event(did: str) -> TriggerEvent:
        return TriggerEvent(
            source_type="github_pr",
            source_id="mindroom-ai/mindroom",
            kind="github_pr.merged",
            event_id=did,
            timestamp=1100.0,
            data={"repo": "mindroom-ai/mindroom", "number": 10, "terminal": True},
        )

    # 1. defer_pending_firing must NOT disable prematurely; mark_skipped DOES disable atomically
    _seeded_rule(store, "pr_skip")
    assert store.enqueue_firing("pr_skip", "d_term_skip", _terminal_event("d_term_skip"))
    fid_skip = store.list_firings(trigger_id="pr_skip")[0].id
    assert store.defer_pending_firing(fid_skip, "target_bot_unavailable", now=1101.0) == "retrying"
    still_on = store.get_trigger("pr_skip")
    assert still_on is not None
    assert still_on.enabled is True

    assert store.mark_skipped(fid_skip, "rate_limited", now=1102.0) is True
    after_skip = store.get_trigger("pr_skip")
    assert after_skip is not None
    assert (after_skip.enabled, after_skip.disabled_reason) == (False, "pr_closed")

    # 2. mark_triaged_no on terminal firing disables with pr_closed
    _seeded_rule(store, "pr_triage")
    assert store.enqueue_firing("pr_triage", "d_term_tr", _terminal_event("d_term_tr"))
    fid_tr = store.list_firings(trigger_id="pr_triage")[0].id
    assert store.mark_triaged_no(fid_tr, "not actionable") is True
    after_tr = store.get_trigger("pr_triage")
    assert after_tr is not None
    assert (after_tr.enabled, after_tr.disabled_reason) == (False, "pr_closed")

    # 3. claim_firing blocked by rate limit still disables terminal watch
    _seeded_rule(store, "pr_rl", max_fires_per_hour=1)
    normal_ev = TriggerEvent(
        source_type="github_pr",
        source_id="mindroom-ai/mindroom",
        kind="github_pr.update",
        event_id="d_norm",
        timestamp=1100.0,
        data={"repo": "mindroom-ai/mindroom", "number": 10, "terminal": False},
    )
    assert store.enqueue_firing("pr_rl", "d_norm", normal_ev)
    assert store.claim_firing(store.list_firings(trigger_id="pr_rl")[0].id, now=1100.0) == "claimed"
    assert store.enqueue_firing("pr_rl", "d_merge", _terminal_event("d_merge"))
    fid_merge = store.list_firings(trigger_id="pr_rl", limit=1)[0].id
    assert store.claim_firing(fid_merge, now=1105.0) == "rate_limited"
    after_rl = store.get_trigger("pr_rl")
    assert after_rl is not None
    assert (after_rl.enabled, after_rl.disabled_reason) == (False, "pr_closed")


def _synapse_timeline_response(step: int, page: int) -> httpx.Response:
    if step == 1 and page == 1:
        entries = [{"id": i, "event": "commented", "user": {"login": "old"}, "body": "old"} for i in range(1, 101)]
        link = '<https://api.github.com/repos/matrix-org/synapse/issues/42/timeline?per_page=100&page=2>; rel="next"'
        return httpx.Response(200, json=entries, headers={"ETag": '"tl-p1-full"', "Link": link})
    if step == 1:
        return httpx.Response(
            200,
            json=[
                {"id": 101, "event": "commented", "user": {"login": "reviewer", "type": "User"}, "body": "Fix"},
                {"id": 102, "event": "commented", "user": {"login": "bot[bot]", "type": "Bot"}, "body": "bot"},
                {"id": 103, "event": "commented", "user": {"login": "quiethuman", "type": "User"}, "body": "no"},
                {"id": 104, "event": "committed", "actor": {"login": "reviewer"}},
            ],
            headers={"ETag": '"tl-p2"'},
        )
    return httpx.Response(304, headers={"ETag": '"tl-p2"'})


def _synapse_check_runs_response(path: str, step: int) -> httpx.Response:
    if path.endswith("/sha_b/check-runs"):
        return httpx.Response(
            200,
            json={"check_runs": [{"id": 3, "name": "pytest", "status": "completed", "conclusion": "success"}]},
        )
    runs_by_step = {
        1: [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "failure"},
            {"id": 2, "name": "lint", "status": "in_progress"},
            {"id": 9, "name": "old_job", "status": "completed", "conclusion": "cancelled"},
        ],
        2: [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "failure"},
            {"id": 2, "name": "lint", "status": "completed", "conclusion": "success"},
        ],
    }
    default_runs = [
        {"id": 1, "name": "pytest", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "lint", "status": "completed", "conclusion": "success"},
    ]
    return httpx.Response(
        200,
        json={"check_runs": runs_by_step.get(step, default_runs)},
        headers={"ETag": f'"ci-{step}"'},
    )


@pytest.mark.asyncio
async def test_etag_poller_pagination_review_comments_and_ci_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 304 polls timeline + single-page desc comments + CI; cancelled ignored; SHA resets CI."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_ext",
        repo="matrix-org/synapse",
        number=42,
        events=["activity", "ci"],
        webhook_source_id=None,
        ignore_actors=["quiethuman"],
        head_sha="sha_a",
        state={
            "pr_etag": '"pr-1"',
            "pr_title": "Synapse fix",
            "timeline_page": 1,
            "timeline_etag": '"tl-p1"',
            "seen_timeline_ids": [f"commented:{i}" for i in range(1, 101)],
            "max_review_comment_id": 10,
            "review_comments_etag": '"rc-0"',
            "ci_head_sha": "sha_a",
            "checks_etag": '"ci-a"',
            "notified_failed_checks": [],
            "last_ci_overall": "pending",
        },
    )
    phase = {"step": 1}
    rc_queries: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path, step = req.url.path, phase["step"]
        if path == "/repos/matrix-org/synapse/pulls/42":
            if step in {1, 2, 3}:
                assert req.headers.get("if-none-match") == '"pr-1"'
                return httpx.Response(304, headers={"ETag": '"pr-1"'})
            return httpx.Response(
                200,
                json={"state": "open", "merged": False, "head": {"sha": "sha_b"}, "title": "Synapse fix"},
                headers={"ETag": f'"pr-{step}"'},
            )
        if path == "/repos/matrix-org/synapse/issues/42/timeline":
            return _synapse_timeline_response(step, int(req.url.params.get("page", "1")))
        if path == "/repos/matrix-org/synapse/pulls/42/comments":
            rc_queries.append(req.url.query.decode())
            if step == 1:
                comments = [
                    {
                        "id": 13,
                        "pull_request_review_id": 78,
                        "path": "c.py",
                        "body": "bot",
                        "user": {"login": "l", "type": "Bot"},
                    },
                    {
                        "id": 12,
                        "pull_request_review_id": 77,
                        "path": "b.py",
                        "body": "c2",
                        "user": {"login": "reviewer", "type": "User"},
                    },
                    {
                        "id": 11,
                        "pull_request_review_id": 77,
                        "path": "a.py",
                        "body": "c1",
                        "user": {"login": "reviewer", "type": "User"},
                    },
                ]
                return httpx.Response(200, json=comments, headers={"ETag": '"rc-1"'})
            return httpx.Response(304, headers={"ETag": '"rc-1"'})
        return _synapse_check_runs_response(path, step)

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    # Step 1: PR 304 + page-2 comment + grouped desc review comments (reversed to chronological) + fail-fast (not cancelled)
    assert await poller.poll_due_once(now=1100.0) == 1
    assert rc_queries[0] == "sort=created&direction=desc&per_page=100"
    items = store.list_firings(trigger_id="pr_ext")[0].event.data["items"]
    assert [it["type"] for it in items] == ["comment", "review_comments", "ci_failure"]
    assert [c["id"] for c in items[1]["comments"]] == [11, 12]
    cp_after_step1 = store.get_github_pr_checkpoint("pr_ext")
    assert cp_after_step1 is not None
    assert "checks_by_id" not in cp_after_step1.state

    # Step 2: unchanged failure across fresh poller -> 0; Step 3: all green -> 1 then 0; Step 4: sha_b -> 1
    phase["step"] = 2
    poller2 = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )
    assert await poller2.poll_due_once(now=1300.0) == 0
    phase["step"] = 3
    assert await poller2.poll_due_once(now=1500.0) == 1
    assert store.list_firings(trigger_id="pr_ext", limit=1)[0].event.data["items"][0]["type"] == "ci_passing"
    assert await poller2.poll_due_once(now=1700.0) == 0
    phase["step"] = 4
    assert await poller2.poll_due_once(now=1900.0) == 1
    assert store.list_firings(trigger_id="pr_ext", limit=1)[0].event.data["head_sha"] == "sha_b"


@pytest.mark.asyncio
async def test_poller_disable_reenable_reseeds_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling and re-enabling a polled PR watch resets seeded=False so the next poll reseeds silently."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_reseed",
        repo="matrix-org/synapse",
        number=42,
        events=["activity"],
        webhook_source_id=None,
        head_sha="sha_b",
    )
    phase = {"step": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        path, step = req.url.path, phase["step"]
        if path == "/repos/matrix-org/synapse/pulls/42":
            return httpx.Response(200, json={"state": "open", "merged": False, "head": {"sha": "sha_b"}})
        if path == "/repos/matrix-org/synapse/issues/42/timeline":
            entries = [{"id": 200, "event": "commented", "user": {"login": "reviewer"}, "body": "paused"}]
            if step == 2:
                entries.append({"id": 201, "event": "commented", "user": {"login": "reviewer"}, "body": "resumed"})
            return httpx.Response(200, json=entries, headers={"ETag": f'"tl-{step}"'})
        if step == 1:
            return httpx.Response(200, json=[{"id": 20, "user": {"login": "reviewer"}, "body": "paused inline"}])
        return httpx.Response(304)

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    set_trigger_enabled_sync(
        paths,
        config,
        "pr_reseed",
        enabled=False,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    set_trigger_enabled_sync(
        paths,
        config,
        "pr_reseed",
        enabled=True,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    cp_paused = store.get_github_pr_checkpoint("pr_reseed")
    assert cp_paused is not None
    assert cp_paused.seeded is False

    assert await poller.poll_due_once(now=cp_paused.next_poll_at + 1.0) == 0
    cp_reseeded = store.get_github_pr_checkpoint("pr_reseed")
    assert cp_reseeded is not None
    assert cp_reseeded.seeded is True

    phase["step"] = 2
    assert await poller.poll_due_once(now=cp_reseeded.next_poll_at + 1.0) == 1
    assert [it["id"] for it in store.list_firings(trigger_id="pr_reseed", limit=1)[0].event.data["items"]] == [201]


@pytest.mark.asyncio
async def test_poller_truncated_check_runs_suppresses_all_green_but_emits_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check-runs pagination capped at 3 pages suppresses false ci_passing while still emitting observed ci_failure."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_trunc", repo="org/big", number=5, events=["ci"], webhook_source_id=None, head_sha="sha5")
    fail_page2 = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/big/pulls/5":
            return httpx.Response(200, json={"state": "open", "merged": False, "head": {"sha": "sha5"}})
        page = int(req.url.params.get("page", "1"))
        conc = "failure" if (page == 2 and fail_page2["fail"]) else "success"
        headers = (
            {
                "Link": f'<https://api.github.com/repos/org/big/commits/sha5/check-runs?per_page=100&page={page + 1}>; rel="next"',
            }
            if page <= 3
            else {}
        )
        return httpx.Response(
            200,
            json={"check_runs": [{"id": page, "name": f"job-{page}", "status": "completed", "conclusion": conc}]},
            headers=headers,
        )

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    # All 3 fetched pages green, but page 4 still advertised -> truncated, MUST NOT emit ci_passing
    assert await poller.poll_due_once(now=1100.0) == 0
    cp = store.get_github_pr_checkpoint("pr_trunc")
    assert cp is not None
    assert cp.state["last_ci_overall"] == "pending"

    # Failure observed on page 2 while still truncated -> fail-fast ci_failure fires, no ci_passing
    fail_page2["fail"] = True
    assert await poller.poll_due_once(now=cp.next_poll_at + 1.0) == 1
    items = store.list_firings(trigger_id="pr_trunc", limit=1)[0].event.data["items"]
    assert [it["type"] for it in items] == ["ci_failure"]


@pytest.mark.asyncio
async def test_poller_rate_limit_buckets_and_unreadable_pat_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 pauses the shared owner/token bucket, unreadable PAT backs off, and 403 isolates anonymous from authed."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_rl_1", repo="org/repo", number=1, webhook_source_id=None, head_sha="s1")
    _seeded_rule(store, "pr_rl_2", repo="org/repo", number=2, webhook_source_id=None, head_sha="s2")

    # 1. 429 pauses the owner/token bucket for both rules after 1 request
    calls: list[str] = []
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )

    def _rate_limit_handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"})

    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(_rate_limit_handler),
        max_concurrency=1,
    )
    assert await poller.poll_due_once(now=2000.0) == 0
    assert len(calls) == 1
    for tid in ("pr_rl_1", "pr_rl_2"):
        cp = store.get_github_pr_checkpoint(tid)
        assert cp is not None
        assert cp.next_poll_at == pytest.approx(2120.0)

    # 2. Unreadable PAT backs off with consecutive_errors incremented
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(side_effect=CredentialsUnreadableError("corrupt PAT")),
    )
    assert await poller.poll_due_once(now=2200.0) == 0
    cp1_err = store.get_github_pr_checkpoint("pr_rl_1")
    assert cp1_err is not None
    assert cp1_err.consecutive_errors == 1
    assert cp1_err.next_poll_at == pytest.approx(2320.0)

    # 3. 403 pauses anonymous bucket only, leaving authenticated user unblocked
    store.delete_trigger("pr_rl_1")
    store.delete_trigger("pr_rl_2")
    _seeded_rule(
        store,
        "pr_anon",
        repo="ext/anon",
        number=1,
        events=["merge"],
        webhook_source_id=None,
        owner_user_id=_OWNER,
    )
    _seeded_rule(
        store,
        "pr_authed",
        repo="ext/authed",
        number=2,
        events=["merge"],
        webhook_source_id=None,
        owner_user_id=_OTHER,
    )

    async def _token_by_owner(owner: str, _cfg: object, _rp: object) -> tuple[str | None, str]:
        return (None, "anonymous") if owner == _OWNER else ("ghp_other", "token")

    monkeypatch.setattr("mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token", _token_by_owner)
    hit_paths: list[str] = []
    poll_now = time.time()
    reset_epoch = int(poll_now) + 300

    def _bucket_handler(req: httpx.Request) -> httpx.Response:
        hit_paths.append(req.url.path)
        if req.url.path == "/repos/ext/anon/pulls/1":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)})
        return httpx.Response(200, json={"state": "open", "merged": False, "head": {"sha": "s"}})

    bucket_poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(_bucket_handler),
        max_concurrency=1,
    )
    assert await bucket_poller.poll_due_once(now=poll_now) == 0
    assert hit_paths == ["/repos/ext/anon/pulls/1", "/repos/ext/authed/pulls/2"]
    cp_anon = store.get_github_pr_checkpoint("pr_anon")
    assert cp_anon is not None
    assert cp_anon.next_poll_at >= poll_now + 250.0


@pytest.mark.asyncio
async def test_poller_terminal_merge_survives_retry_reopens_thread_and_respects_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal merge survives pr_closed across transient Matrix failure + retry, reopens thread, and respects policy."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_merge", repo="org/repo", number=1, webhook_source_id=None, head_sha="s1")
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    merge_poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(
            lambda req: (
                httpx.Response(
                    200,
                    json={"state": "closed", "merged": True, "head": {"sha": "s1"}, "title": "Merged PR"},
                    headers={"X-RateLimit-Remaining": "50"},
                )
                if req.url.path == "/repos/org/repo/pulls/1"
                else httpx.Response(200, json=[])
            ),
        ),
    )
    assert await merge_poller.poll_due_once(now=3000.0) == 1
    cp_merged = store.get_github_pr_checkpoint("pr_merge")
    assert cp_merged is not None
    assert 3100.0 <= cp_merged.next_poll_at <= 3140.0

    client = _room_client(resolved_thread=True)
    bot = SimpleNamespace(
        running=True,
        client=client,
        agent_name="dev",
        conversation_reader=SimpleNamespace(latest_thread_event_id=AsyncMock(return_value="$latest")),
    )
    send_mock = AsyncMock(side_effect=[None, DeliveredMatrixEvent(event_id="$delivered_merge", content_sent={})])
    monkeypatch.setattr("mindroom.reactive_triggers.dispatch.send_matrix_message", send_mock)
    service = ReactiveTriggerService(
        runtime_paths=paths,
        config_provider=lambda: config,
        bots=lambda: {"dev": cast("Any", bot)},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    await service.drain_due_firings_once()
    rule_after_claim = store.get_trigger("pr_merge")
    assert rule_after_claim is not None
    assert (rule_after_claim.enabled, rule_after_claim.disabled_reason) == (False, "pr_closed")

    firing = store.list_firings(trigger_id="pr_merge")[0]
    assert firing.status == "claimed"
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET next_retry_at = 0.0 WHERE id = ?", (firing.id,))
    await service.drain_due_firings_once()
    delivered = store.list_firings(trigger_id="pr_merge")[0]
    assert (delivered.status, delivered.matrix_event_id) == ("delivered", "$delivered_merge")
    assert client.room_put_state.await_count >= 1

    no_poll_cfg, _ = _config(tmp_path / "np2", github_pr_polling_enabled=False)
    blocked = await check_reactive_source_access(
        rule=rule_after_claim,
        event=delivered.event,
        source_bot=None,
        config=no_poll_cfg,
        runtime_paths=paths,
        memberships=AgentReplyMembershipIndex(),
    )
    assert blocked is not None
    assert (blocked.status, blocked.reason) == ("skipped", "github_pr_polling_disabled")


@pytest.mark.asyncio
async def test_service_stop_cancels_inflight_github_pr_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service stop() cancels an in-flight background GitHub PR poll task cleanly."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        runtime_paths=paths,
        config_provider=lambda: config,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    started, cancelled = asyncio.Event(), False

    async def _slow_poll() -> int:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return 0

    monkeypatch.setattr(service, "poll_github_prs_once", _slow_poll)
    service._schedule_github_pr_poll()
    await started.wait()
    await service.stop()
    assert cancelled is True
    assert service._github_pr_poll_task is None


@pytest.mark.asyncio
async def test_round2_webhook_sha_fallback_and_reenable_clears_stale_sha(tmp_path: Path) -> None:
    """Explicit mismatched PR number never falls back to head_sha; webhook re-enable clears stale head_sha."""
    gh = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "check_run", "check_suite"])
    config, paths = _config(tmp_path, event_sources={"gh": gh})
    store = ReactiveTriggerStore(paths)
    rule = _seeded_rule(store, "pr_42", number=42, webhook_source_id="gh", head_sha="sha_old")
    cp = store.get_github_pr_checkpoint("pr_42")

    # Explicit number=43 + matching head_sha must NOT match watched PR #42.
    other_pr_check = TriggerEvent(
        source_type="external_event",
        source_id="gh",
        kind="check_run.completed",
        event_id="ev_other_pr",
        timestamp=1100.0,
        actor="actions[bot]",
        data={
            "repo": "mindroom-ai/mindroom",
            "number": 43,
            "pr_numbers": [],
            "sha": "sha_old",
            "name": "pytest",
            "conclusion": "failure",
        },
    )
    assert match_github_webhook_to_pr_rule(rule, cp, other_pr_check, allowed_events=gh.allowed_events).matched is False

    # Disabling and re-enabling a webhook watch clears stale head_sha so commits pushed while paused match by PR number.
    set_trigger_enabled_sync(
        paths,
        config,
        "pr_42",
        enabled=False,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    set_trigger_enabled_sync(
        paths,
        config,
        "pr_42",
        enabled=True,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    refreshed_cp = store.get_github_pr_checkpoint("pr_42")
    assert refreshed_cp is not None
    assert (refreshed_cp.head_sha, refreshed_cp.pr_state, refreshed_cp.seeded) == (None, "open", True)

    payload = _gh_payload(
        "check_run",
        action="completed",
        actor="actions[bot]",
        actor_type="Bot",
        sha="sha_pushed_while_paused",
        conclusion="failure",
        pr_numbers=[42],
    )
    assert (
        _send_webhook(
            store,
            config,
            paths,
            source=gh,
            gh_event="check_run",
            delivery_id="d_resumed_sha",
            payload=payload,
        )
        == 1
    )
    assert store.list_firings(trigger_id="pr_42", limit=1)[0].event.data["items"][0]["type"] == "ci_failure"


@pytest.mark.asyncio
async def test_round2_poller_cancelled_ci_never_reports_all_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check run transitioning pending -> completed/cancelled emits 0 firings (neither ci_passing nor ci_failure)."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_cancel_ci",
        repo="org/repo",
        number=7,
        events=["ci"],
        webhook_source_id=None,
        head_sha="sha_c1",
        state={"ci_head_sha": "sha_c1", "last_ci_overall": "pending"},
    )
    phase = {"step": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/pulls/7":
            return httpx.Response(304)
        if phase["step"] == 1:
            runs = [{"id": 1, "name": "pytest", "status": "completed", "conclusion": "cancelled"}]
        else:
            runs = [{"id": 2, "name": "pytest", "status": "completed", "conclusion": "success"}]
        return httpx.Response(200, json={"check_runs": runs}, headers={"ETag": f'"c-{phase["step"]}"'})

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    assert await poller.poll_due_once(now=2000.0) == 0
    assert store.list_firings(trigger_id="pr_cancel_ci") == []
    cp1 = store.get_github_pr_checkpoint("pr_cancel_ci")
    assert cp1 is not None
    assert cp1.state["last_ci_overall"] == "non_passing"

    # Once a subsequent run succeeds, ci_passing emits normally.
    phase["step"] = 2
    assert await poller.poll_due_once(now=cp1.next_poll_at + 1.0) == 1
    assert store.list_firings(trigger_id="pr_cancel_ci", limit=1)[0].event.data["items"][0]["type"] == "ci_passing"


@pytest.mark.asyncio
async def test_round2_poller_pending_merge_survives_subsequent_poll_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending terminal merge firing survives another poll_due_once() tick before delivery."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_merge_survive", repo="org/repo", number=9, webhook_source_id=None, head_sha="s9")
    hits = {"pr": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/pulls/9":
            hits["pr"] += 1
            return httpx.Response(200, json={"state": "closed", "merged": True, "head": {"sha": "s9"}})
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    client = _mock_client(handler)
    poller = GitHubPRPoller(store=store, config_provider=lambda: config, runtime_paths=paths, client=client)

    assert await poller.poll_due_once(now=3000.0) == 1
    cp_merged = store.get_github_pr_checkpoint("pr_merge_survive")
    assert cp_merged is not None
    assert cp_merged.pr_state == "merged"

    # Second poll tick before delivery must not re-poll or disable the watch.
    assert await poller.poll_due_once(now=cp_merged.next_poll_at + 10.0) == 0
    assert hits["pr"] == 1
    rule_still_enabled = store.get_trigger("pr_merge_survive")
    assert rule_still_enabled is not None
    assert rule_still_enabled.enabled is True

    # Even if _poll_rule is invoked directly on the already-merged checkpoint, it must not disable the rule.
    assert (
        await poller._poll_rule(rule_still_enabled, cp_merged, config, client, cp_merged.next_poll_at + 20.0) is False
    )
    rule_after_direct = store.get_trigger("pr_merge_survive")
    assert rule_after_direct is not None
    assert rule_after_direct.enabled is True

    # Pending merge firing remains claimable and disables the watch only upon claim.
    firing = store.list_due_firings(now=3000.0)[0]
    assert store.claim_firing(firing.id, now=3050.0) == "claimed"
    rule_after_claim = store.get_trigger("pr_merge_survive")
    assert rule_after_claim is not None
    assert (rule_after_claim.enabled, rule_after_claim.disabled_reason) == (False, "pr_closed")


@pytest.mark.asyncio
async def test_round2_poller_timeline_advances_past_page_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeline polling advances past _MAX_POLL_PAGES=3 without looping on page 3 304."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_tl4",
        repo="org/repo",
        number=4,
        events=["activity"],
        webhook_source_id=None,
        head_sha="sha4",
        state={"timeline_page": 1, "seen_timeline_ids": []},
    )
    pages_fetched: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/pulls/4":
            return httpx.Response(304)
        if req.url.path == "/repos/org/repo/pulls/4/comments":
            return httpx.Response(304)
        page = int(req.url.params.get("page", "1"))
        pages_fetched.append(page)
        if req.headers.get("If-None-Match") == f'"tl-{page}"':
            return httpx.Response(304)
        headers: dict[str, str] = {"ETag": f'"tl-{page}"'}
        if page < 4:
            headers["Link"] = (
                f'<https://api.github.com/repos/org/repo/issues/4/timeline?per_page=100&page={page + 1}>; rel="next"'
            )
        return httpx.Response(
            200,
            json=[{"id": page * 10, "event": "commented", "user": {"login": "alice"}, "body": f"page {page}"}],
            headers=headers,
        )

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    # Tick 1 fetches pages 1..3 and persists timeline_page=4, timeline_etag=None.
    assert await poller.poll_due_once(now=2000.0) == 1
    cp1 = store.get_github_pr_checkpoint("pr_tl4")
    assert cp1 is not None
    assert (cp1.state["timeline_page"], cp1.state["timeline_etag"]) == (4, None)
    assert pages_fetched == [1, 2, 3]

    # Tick 2 fetches page 4 cleanly instead of looping on page 3's 304.
    assert await poller.poll_due_once(now=cp1.next_poll_at + 1.0) == 1
    assert pages_fetched == [1, 2, 3, 4]
    latest_items = store.list_firings(trigger_id="pr_tl4", limit=1)[0].event.data["items"]
    assert [it["id"] for it in latest_items] == [40]


@pytest.mark.asyncio
async def test_round2_poller_timeline_seed_tail_drains_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When seeding hits _MAX_SEED_PAGES, remaining tail pages drain silently and then normal polling resumes."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    monkeypatch.setattr("mindroom.reactive_triggers.github_pr_poller._MAX_SEED_PAGES", 2)
    extra_tail = {"added": False}

    def seed_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/pulls/5":
            return httpx.Response(200, json={"state": "open", "merged": False, "head": {"sha": "sha5"}})
        if req.url.path == "/repos/org/repo/pulls/5/comments":
            return httpx.Response(200, json=[])
        page = int(req.url.params.get("page", "1"))
        headers: dict[str, str] = {"ETag": f'"st-{page}"'}
        if page < 3:
            headers["Link"] = (
                f'<https://api.github.com/repos/org/repo/issues/5/timeline?per_page=100&page={page + 1}>; rel="next"'
            )
        entries = [{"id": page * 100, "event": "commented", "user": {"login": "alice"}, "body": f"hist {page}"}]
        if page == 3 and extra_tail["added"]:
            entries.append({"id": 301, "event": "commented", "user": {"login": "alice"}, "body": "brand new"})
        return httpx.Response(200, json=entries, headers=headers)

    seed_client = _mock_client(seed_handler)
    seeded = await seed_github_pr_watch(
        "org/repo",
        5,
        ["activity"],
        owner_user_id=_OWNER,
        config=config,
        runtime_paths=paths,
        poll_interval_seconds=60,
        now=1000.0,
        client=seed_client,
    )
    assert (seeded.checkpoint.state["timeline_page"], seeded.checkpoint.state["seed_tail_pending"]) == (3, True)
    seed_rule = ReactiveTriggerRecord(
        trigger_id="pr_tl_seed",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(repo="org/repo", number=5, events=["activity"], webhook_source_id=None),
        target=TriggerTargetSpec(agent="dev", room_id="!room:example.org", prompt="Watch"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(seed_rule, initial_github_pr_checkpoint=seeded.checkpoint)
    seed_poller = GitHubPRPoller(store=store, config_provider=lambda: config, runtime_paths=paths, client=seed_client)

    assert await seed_poller.poll_due_once(now=seeded.checkpoint.next_poll_at + 1.0) == 0
    cp_drained = store.get_github_pr_checkpoint("pr_tl_seed")
    assert cp_drained is not None
    assert cp_drained.state["seed_tail_pending"] is False

    extra_tail["added"] = True
    assert await seed_poller.poll_due_once(now=cp_drained.next_poll_at + 1.0) == 1
    assert [it["id"] for it in store.list_firings(trigger_id="pr_tl_seed", limit=1)[0].event.data["items"]] == [301]


@pytest.mark.asyncio
async def test_round2_poller_multipage_review_comments_and_error_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-page inline review comments deliver page 2 without skipping; one failing rule does not abort others."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_rc_multi",
        repo="org/repo",
        number=42,
        events=["activity"],
        webhook_source_id=None,
        head_sha="sha42",
        state={"max_review_comment_id": 100, "review_comments_etag": '"rc-old"'},
    )
    _seeded_rule(
        store,
        "pr_broken",
        repo="org/broken",
        number=99,
        events=["activity"],
        webhook_source_id=None,
        head_sha="sha99",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/org/repo/pulls/42":
            return httpx.Response(304)
        if req.url.path == "/repos/org/repo/issues/42/timeline":
            return httpx.Response(304)
        if req.url.path == "/repos/org/repo/pulls/42/comments":
            page = int(req.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 203,
                            "pull_request_review_id": 7,
                            "path": "c.py",
                            "body": "c3",
                            "user": {"login": "bob"},
                        },
                        {
                            "id": 202,
                            "pull_request_review_id": 7,
                            "path": "b.py",
                            "body": "c2",
                            "user": {"login": "bob"},
                        },
                    ],
                    headers={
                        "ETag": '"rc-p1"',
                        "Link": '<https://api.github.com/repos/org/repo/pulls/42/comments?sort=created&direction=desc&per_page=100&page=2>; rel="next"',
                    },
                )
            return httpx.Response(
                200,
                json=[
                    {"id": 201, "pull_request_review_id": 7, "path": "a.py", "body": "c1", "user": {"login": "bob"}},
                    {"id": 100, "pull_request_review_id": 6, "path": "old.py", "body": "old", "user": {"login": "bob"}},
                ],
                headers={"ETag": '"rc-p2"'},
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    orig_poll_rule = poller._poll_rule

    async def _poll_with_one_crash(
        rule: ReactiveTriggerRecord,
        cp: GitHubPRCheckpoint | None,
        cfg: Config,
        client: httpx.AsyncClient,
        now: float,
    ) -> bool:
        if rule.trigger_id == "pr_broken":
            msg = "unexpected boom"
            raise RuntimeError(msg)
        return await orig_poll_rule(rule, cp, cfg, client, now)

    monkeypatch.setattr(poller, "_poll_rule", _poll_with_one_crash)

    assert await poller.poll_due_once(now=2500.0) == 1
    cp_after = store.get_github_pr_checkpoint("pr_rc_multi")
    assert cp_after is not None
    assert (cp_after.state["max_review_comment_id"], cp_after.state["review_comments_etag"]) == (203, '"rc-p1"')
    items = store.list_firings(trigger_id="pr_rc_multi", limit=1)[0].event.data["items"]
    assert len(items) == 1
    assert [c["id"] for c in items[0]["comments"]] == [201, 202, 203]


@pytest.mark.asyncio
async def test_watch_pr_approval_alias_governed_by_triggers_rules(tmp_path: Path) -> None:
    """A tool_approval rule matching `triggers` also requires approval for `watch_pr` via its ApprovalAlias."""
    base_cfg, paths = _config(tmp_path)
    require_cfg = bind_runtime_paths(
        base_cfg.model_copy(
            update={
                "tool_approval": ToolApprovalConfig(
                    rules=[ApprovalRuleConfig(match="triggers", action="require_approval")],
                ),
            },
        ),
        paths,
    )
    auto_cfg = bind_runtime_paths(
        base_cfg.model_copy(
            update={
                "tool_approval": ToolApprovalConfig(
                    rules=[ApprovalRuleConfig(match="triggers", action="auto_approve")],
                ),
            },
        ),
        paths,
    )

    alias = toolkit_approval_alias(require_cfg, builtin_tool_registry(), "triggers", "watch_pr")
    assert alias is not None
    assert alias.judged_as == "triggers"
    assert tool_may_require_approval(require_cfg, "watch_pr", alias) is True
    requires, _timeout = await evaluate_tool_approval(
        require_cfg,
        paths,
        "watch_pr",
        {"repo": "mindroom-ai/mindroom", "number": 42},
        "dev",
        alias,
    )
    assert requires is True

    assert tool_may_require_approval(auto_cfg, "watch_pr", alias) is False
    auto_requires, _ = await evaluate_tool_approval(
        auto_cfg,
        paths,
        "watch_pr",
        {"repo": "mindroom-ai/mindroom", "number": 42},
        "dev",
        alias,
    )
    assert auto_requires is False


@pytest.mark.asyncio
async def test_auto_disable_on_close_false_stays_due_and_observes_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll-backed watch with auto_disable_on_close=False stays due after close and observes reopen/activity."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(
        store,
        "pr_keep_polling",
        repo="org/repo",
        number=77,
        events=["activity", "ci", "close"],
        webhook_source_id=None,
        auto_disable_on_close=False,
        head_sha="sha_v1",
        state={"pr_etag": '"pr-1"', "timeline_etag": '"tl-1"', "review_comments_etag": '"rc-1"'},
    )
    _seeded_rule(
        store,
        "pr_stop_polling",
        repo="org/repo",
        number=78,
        events=["activity", "close"],
        webhook_source_id=None,
        auto_disable_on_close=True,
        head_sha="sha_v1",
        state={"pr_etag": '"pr-1"', "timeline_etag": '"tl-1"', "review_comments_etag": '"rc-1"'},
    )

    phase = {"step": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path in {"/repos/org/repo/pulls/77", "/repos/org/repo/pulls/78"}:
            if phase["step"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "state": "closed",
                        "merged": False,
                        "head": {"sha": "sha_v1"},
                        "title": "T",
                        "html_url": "https://gh/77",
                    },
                    headers={"ETag": '"pr-closed"'},
                )
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "merged": False,
                    "head": {"sha": "sha_v2"},
                    "title": "T",
                    "html_url": "https://gh/77",
                },
                headers={"ETag": '"pr-reopened"'},
            )
        if req.url.path == "/repos/org/repo/issues/77/timeline":
            if phase["step"] == 1:
                return httpx.Response(304)
            return httpx.Response(
                200,
                json=[
                    {"id": 901, "event": "reopened", "actor": {"login": "bob", "type": "User"}},
                    {"id": 902, "event": "commented", "user": {"login": "bob", "type": "User"}, "body": "Back open"},
                ],
                headers={"ETag": '"tl-2"'},
            )
        if req.url.path == "/repos/org/repo/commits/sha_v2/check-runs":
            return httpx.Response(
                200,
                json={"check_runs": [{"id": 50, "name": "ci", "status": "completed", "conclusion": "success"}]},
                headers={"ETag": '"ci-2"'},
            )
        return httpx.Response(304)

    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr_poller.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    # Phase 1: both PRs close; keep_polling remains due while stop_polling is excluded as seeded closed.
    assert await poller.poll_due_once(now=2000.0) == 2
    keep_close_firing = store.list_firings(trigger_id="pr_keep_polling", limit=1)[0]
    assert keep_close_firing.event.kind == "github_pr.closed"
    assert store.claim_firing(keep_close_firing.id, now=2005.0) == "claimed"
    keep_rule = store.get_trigger("pr_keep_polling")
    assert keep_rule is not None
    assert keep_rule.enabled is True

    due_ids = [rule.trigger_id for rule, _ in store.list_due_github_pr_triggers(now=3000.0)]
    assert due_ids == ["pr_keep_polling"]

    # Phase 2: PR reopens with new activity + passing CI -> checkpoint returns to "open" and firing enqueues.
    phase["step"] = 2
    assert await poller.poll_due_once(now=3000.0) == 1
    cp_reopened = store.get_github_pr_checkpoint("pr_keep_polling")
    assert cp_reopened is not None
    assert (cp_reopened.pr_state, cp_reopened.head_sha) == ("open", "sha_v2")
    latest = store.list_firings(trigger_id="pr_keep_polling", limit=1)[0]
    assert latest.event.kind == "github_pr.update"
    item_types = {item["type"] for item in latest.event.data["items"]}
    assert {"reopened", "comment", "ci_passing"} <= item_types


def test_defer_pending_firing_retires_pr_closed_only_when_retries_exhausted(tmp_path: Path) -> None:
    """Pending terminal merge firing stays enabled on retryable defers and disables with pr_closed on failure."""
    _, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    _seeded_rule(store, "pr_defer_exhaust")
    terminal_ev = TriggerEvent(
        source_type="github_pr",
        source_id="mindroom-ai/mindroom",
        kind="github_pr.merged",
        event_id="d_merge_exhaust",
        timestamp=1100.0,
        data={"repo": "mindroom-ai/mindroom", "number": 10, "terminal": True},
    )
    assert store.enqueue_firing("pr_defer_exhaust", "d_merge_exhaust", terminal_ev)
    fid = store.list_firings(trigger_id="pr_defer_exhaust")[0].id

    # Retryable attempts keep the firing pending and the watch enabled.
    assert store.defer_pending_firing(fid, "target_bot_unavailable", now=1101.0, max_attempts=3) == "retrying"
    assert store.defer_pending_firing(fid, "target_bot_unavailable", now=1110.0, max_attempts=3) == "retrying"
    still_on = store.get_trigger("pr_defer_exhaust")
    still_pending = store.get_firing(fid)
    assert still_on is not None
    assert (still_on.enabled, still_on.disabled_reason) == (True, None)
    assert still_pending is not None
    assert still_pending.status == "pending"

    # Exhausting max_attempts transitions the firing to failed and retires the watch with pr_closed.
    assert store.defer_pending_firing(fid, "target_bot_unavailable", now=1130.0, max_attempts=3) == "failed"
    retired = store.get_trigger("pr_defer_exhaust")
    failed = store.get_firing(fid)
    assert retired is not None
    assert (retired.enabled, retired.disabled_reason) == (False, "pr_closed")
    assert failed is not None
    assert failed.status == "failed"


@pytest.mark.asyncio
async def test_reenabling_github_pr_watch_enforces_max_watches_per_owner(tmp_path: Path) -> None:
    """Re-enabling a paused github_pr watch via set_enabled or update enforces max_github_pr_watches_per_owner."""
    config, paths = _config(tmp_path, max_github_pr_watches_per_owner=1)
    store = ReactiveTriggerStore(paths)
    watch_a = _seeded_rule(store, "pr_a", repo="org/repo", number=1, webhook_source_id=None)

    # Pause watch A, then create watch B to fill the owner's 1-watch quota.
    paused_a = store.set_enabled("pr_a", False, expected_rule_uid=watch_a.rule_uid)
    assert paused_a is not None
    assert paused_a.enabled is False
    watch_b = _seeded_rule(store, "pr_b", repo="org/repo", number=2, webhook_source_id=None)

    # Store-level set_enabled and update_trigger both reject re-enabling A.
    with pytest.raises(ReactiveTriggerLimitError, match="GitHub PR watch limit"):
        store.set_enabled("pr_a", True, expected_rule_uid=watch_a.rule_uid, max_github_pr_per_owner=1)
    with pytest.raises(ReactiveTriggerLimitError, match="GitHub PR watch limit"):
        store.update_trigger(
            paused_a.model_copy(update={"enabled": True, "disabled_reason": None, "updated_at": time.time()}),
            expected_rule_uid=watch_a.rule_uid,
            update_enabled=True,
            max_github_pr_per_owner=1,
        )

    # Tool-level set_enabled and update also return structured error payloads and keep A disabled.
    with tool_runtime_context(_tool_ctx(config, paths)):
        tool = TriggersTools()
        err_enable = json.loads(await tool.triggers(action="set_enabled", trigger_id="pr_a", enabled=True))
        err_update = json.loads(await tool.triggers(action="update", trigger_id="pr_a", enabled=True))
        assert err_enable["status"] == "error"
        assert "GitHub PR watch limit" in err_enable["message"]
        assert err_update["status"] == "error"
        assert "GitHub PR watch limit" in err_update["message"]

        still_disabled = store.get_trigger("pr_a")
        assert still_disabled is not None
        assert still_disabled.enabled is False

        # Once watch B is paused, re-enabling watch A succeeds.
        paused_b = json.loads(await tool.triggers(action="set_enabled", trigger_id=watch_b.trigger_id, enabled=False))
        assert paused_b["status"] == "ok"
        reenabled_a = json.loads(await tool.triggers(action="set_enabled", trigger_id="pr_a", enabled=True))
        assert reenabled_a["status"] == "ok"
        assert reenabled_a["trigger"]["enabled"] is True
