"""End-to-end tests for native github_pr watcher: watch_pr tool, webhook fan-out, and ETag poller."""

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
    GitHubPRPoller,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    ReactiveTriggerService,
    ReactiveTriggerStore,
    TriggerEvent,
    TriggerTargetSpec,
    check_reactive_source_access,
    fan_out_external_event,
    verify_and_normalize_github_webhook,
)
from mindroom.reactive_triggers.management import set_trigger_enabled_sync
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
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


def _gh_source(
    allowed_repos: list[str],
    allowed_events: list[str],
    subscribers: list[str] | None = None,
) -> GitHubEventSourceConfig:
    return GitHubEventSourceConfig(
        secret_env=_SECRET_ENV_NAME,
        allowed_repos=allowed_repos,
        allowed_events=allowed_events,
        subscribers=[_OWNER] if subscribers is None else subscribers,
    )


def _paths(tmp_path: Path, *, env_values: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env_values or {},
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
    paths = _paths(tmp_path, env_values=env_values)
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
        assert room_id == _ROOM
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


def _tool_ctx(
    config: Config,
    paths: RuntimePaths,
    *,
    requester_id: str = _OWNER,
    thread_id: str | None = _THREAD,
) -> ToolRuntimeContext:
    client = _room_client()
    orch = SimpleNamespace(
        agent_bots={"dev": SimpleNamespace(running=True, client=client, agent_name="dev")},
        agent_reply_memberships=AgentReplyMembershipIndex(),
        reactive_triggers=SimpleNamespace(notify_work=MagicMock()),
    )
    return make_test_tool_runtime_context(
        agent_name="dev",
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=thread_id, reply_to_event_id=None),
        requester_id=requester_id,
        client=client,
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=orch,
    )


def _signed_gh_event(
    *,
    source_id: str,
    source: GitHubEventSourceConfig,
    gh_event: str,
    delivery_id: str,
    payload: dict[str, Any],
    paths: RuntimePaths,
    now: float = 1_764_000_000.0,
) -> TriggerEvent:
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(_SECRET_VALUE.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return verify_and_normalize_github_webhook(
        source_id=source_id,
        source=source,
        headers={"X-GitHub-Event": gh_event, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sig},
        body=body,
        runtime_paths=paths,
        now=now,
    )


def _rate_limit_handler(calls: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def _handle(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"})

    return _handle


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
        "mindroom.reactive_triggers.github_pr.resolve_owner_github_token",
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

    monkeypatch.setattr("mindroom.reactive_triggers.github_pr.github_get_json", _wrapped_get)


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
        env_values={_SECRET_ENV_NAME: _SECRET_VALUE},
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
        assert wh["trigger"]["target"]["thread_id"] == _THREAD
        assert wh["trigger"]["target"]["reopen_on_fire"] is True
        assert any("pull_request_review_comment" in note for note in wh["coverage_notes"])

        priv = json.loads(
            await tool.watch_pr(repo="mindroom-ai/private-repo", number=7, target_thread_id="room", trigger_id="pr_p"),
        )
        assert (priv["status"], priv["delivery_mode"], priv["head_sha"]) == ("ok", "webhook", None)
        assert priv["trigger"]["target"]["thread_id"] is None
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
        assert allowed["status"] == "ok"
        assert allowed["delivery_mode"] == "webhook"


@pytest.mark.asyncio
async def test_triggers_management_actions_support_github_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triggers list/update/test/history/delete operate cleanly on github_pr rules."""
    config, paths = _config(tmp_path)
    _patch_github_http(monkeypatch, _watch_pr_routes())
    tool = TriggersTools()

    with tool_runtime_context(_tool_ctx(config, paths)):
        created = json.loads(
            await tool.watch_pr(repo="matrix-org/synapse", number=42, ignore_actors=["Alice"], trigger_id="pr_42"),
        )
        assert created["status"] == "ok"
        listed = json.loads(await tool.triggers(action="list"))
        assert [t["trigger_id"] for t in listed["reactive_triggers"]] == ["pr_42"]

        updated = json.loads(
            await tool.triggers(action="update", trigger_id="pr_42", prompt="New prompt", reopen_on_fire=False),
        )
        assert updated["status"] == "ok"
        assert updated["trigger"]["target"]["prompt"] == "New prompt"
        assert updated["trigger"]["target"]["reopen_on_fire"] is False

        bot_act = json.loads(await tool.triggers(action="test", trigger_id="pr_42", kind="activity", actor="dep[bot]"))
        assert bot_act["matched"] is False
        bot_ci = json.loads(await tool.triggers(action="test", trigger_id="pr_42", kind="ci", actor="actions[bot]"))
        assert bot_ci["matched"] is True
        assert json.loads(await tool.triggers(action="history", trigger_id="pr_42"))["status"] == "ok"


@pytest.mark.asyncio
async def test_webhook_fanout_comments_reviews_fork_ci_terminal_and_revocation(tmp_path: Path) -> None:
    """Webhook fan-out covers comments/reviews/inline/merge/close, fork CI via head_sha, and fail-closed revocation."""
    gh_source = _gh_source(
        ["mindroom-ai/mindroom", "mindroom-ai/other"],
        [
            "pull_request",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
            "check_run",
            "check_suite",
        ],
    )
    config, paths = _config(tmp_path, event_sources={"gh": gh_source}, env_values={_SECRET_ENV_NAME: _SECRET_VALUE})
    store = ReactiveTriggerStore(paths)
    rule = ReactiveTriggerRecord(
        trigger_id="pr_10",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(
            repo="mindroom-ai/mindroom",
            number=10,
            webhook_source_id="gh",
            ignore_actors=["MutedUser"],
            ignore_bots=True,
        ),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, thread_id=_THREAD, reopen_on_fire=True, prompt="Watch"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(
        rule,
        initial_github_pr_checkpoint=GitHubPRCheckpoint(head_sha="sha_v1", seeded=True, updated_at=1000.0),
    )
    other_rule = ReactiveTriggerRecord(
        trigger_id="pr_other",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(repo="mindroom-ai/other", number=10, webhook_source_id="gh"),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Other"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(
        other_rule,
        initial_github_pr_checkpoint=GitHubPRCheckpoint(head_sha="sha_v2", seeded=True, updated_at=1000.0),
    )

    def _send(
        gh_event: str,
        delivery_id: str,
        payload: dict[str, Any],
        *,
        src: GitHubEventSourceConfig = gh_source,
    ) -> int:
        ev = _signed_gh_event(
            source_id="gh",
            source=src,
            gh_event=gh_event,
            delivery_id=delivery_id,
            payload=payload,
            paths=paths,
        )
        return fan_out_external_event(
            source_id="gh",
            source=src,
            event=ev,
            store=store,
            config=config,
            runtime_paths=paths,
            now=1100.0,
        )

    repo_obj = {"full_name": "mindroom-ai/mindroom"}
    # PR comment fires; non-PR issue comment, bot comment, and ignored actor are skipped
    assert (
        _send(
            "issue_comment",
            "d1",
            {
                "action": "created",
                "repository": repo_obj,
                "sender": {"login": "bob", "type": "User"},
                "issue": {"number": 10, "title": "PR 10", "pull_request": {}},
                "comment": {"id": 1, "body": "LGTM!", "user": {"login": "bob", "type": "User"}},
            },
        )
        == 1
    )
    assert (
        _send(
            "issue_comment",
            "d2",
            {
                "action": "created",
                "repository": repo_obj,
                "sender": {"login": "bob", "type": "User"},
                "issue": {"number": 10, "title": "Issue 10"},
                "comment": {"id": 2, "body": "issue", "user": {"login": "bob", "type": "User"}},
            },
        )
        == 0
    )
    assert (
        _send(
            "issue_comment",
            "d3",
            {
                "action": "created",
                "repository": repo_obj,
                "sender": {"login": "codecov[bot]", "type": "Bot"},
                "issue": {"number": 10, "pull_request": {}},
                "comment": {"id": 3, "body": "cov", "user": {"login": "codecov[bot]", "type": "Bot"}},
            },
        )
        == 0
    )
    assert (
        _send(
            "issue_comment",
            "d4",
            {
                "action": "created",
                "repository": repo_obj,
                "sender": {"login": "muteduser", "type": "User"},
                "issue": {"number": 10, "pull_request": {}},
                "comment": {"id": 4, "body": "x", "user": {"login": "muteduser", "type": "User"}},
            },
        )
        == 0
    )

    # Empty commented wrapper suppressed when pull_request_review_comment is enabled; approved & inline fire
    assert (
        _send(
            "pull_request_review",
            "d5",
            {
                "action": "submitted",
                "repository": repo_obj,
                "sender": {"login": "carol"},
                "pull_request": {"number": 10, "title": "PR 10"},
                "review": {"id": 10, "state": "commented", "body": "", "user": {"login": "carol", "type": "User"}},
            },
        )
        == 0
    )
    assert (
        _send(
            "pull_request_review",
            "d6",
            {
                "action": "submitted",
                "repository": repo_obj,
                "sender": {"login": "carol"},
                "pull_request": {"number": 10, "title": "PR 10"},
                "review": {"id": 11, "state": "approved", "body": "", "user": {"login": "carol", "type": "User"}},
            },
        )
        == 1
    )
    assert (
        _send(
            "pull_request_review_comment",
            "d7",
            {
                "action": "created",
                "repository": repo_obj,
                "sender": {"login": "carol"},
                "pull_request": {"number": 10, "title": "PR 10"},
                "comment": {
                    "id": 12,
                    "pull_request_review_id": 10,
                    "path": "a.py",
                    "body": "nit",
                    "user": {"login": "carol", "type": "User"},
                },
            },
        )
        == 1
    )

    # pull_request.synchronize refreshes head_sha -> fork CI matches sha_v2 without cross-repo leak
    assert (
        _send(
            "pull_request",
            "d8",
            {
                "action": "synchronize",
                "repository": repo_obj,
                "sender": {"login": "bob"},
                "pull_request": {"number": 10, "head": {"sha": "sha_v2"}, "base": {"ref": "main"}},
            },
        )
        == 0
    )
    cp = store.get_github_pr_checkpoint("pr_10")
    assert cp is not None
    assert cp.head_sha == "sha_v2"
    assert (
        _send(
            "check_run",
            "d9",
            {
                "action": "completed",
                "repository": repo_obj,
                "sender": {"login": "actions[bot]", "type": "Bot"},
                "check_run": {
                    "id": 50,
                    "name": "pytest",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": "sha_v2",
                    "pull_requests": [],
                },
            },
        )
        == 1
    )
    assert (
        _send(
            "check_suite",
            "d10",
            {
                "action": "completed",
                "repository": repo_obj,
                "sender": {"login": "actions[bot]", "type": "Bot"},
                "check_suite": {
                    "id": 60,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "sha_v2",
                    "pull_requests": [],
                },
            },
        )
        == 1
    )
    assert store.list_firings(trigger_id="pr_other") == []

    # Bot merge fires with terminal=True; unsubscribed terminal close disables immediately
    assert (
        _send(
            "pull_request",
            "d11",
            {
                "action": "closed",
                "repository": repo_obj,
                "sender": {"login": "mergify[bot]", "type": "Bot"},
                "pull_request": {"number": 10, "merged": True, "head": {"sha": "sha_v2"}},
            },
        )
        == 1
    )
    latest = store.list_firings(trigger_id="pr_10", limit=1)[0]
    assert latest.event.kind == "github_pr.merged"
    assert latest.event.data["terminal"] is True

    act_only = ReactiveTriggerRecord(
        trigger_id="pr_act_only",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(repo="mindroom-ai/mindroom", number=11, events=["activity"], webhook_source_id="gh"),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Activity only"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(
        act_only,
        initial_github_pr_checkpoint=GitHubPRCheckpoint(head_sha="sha11", seeded=True, updated_at=1000.0),
    )
    assert (
        _send(
            "pull_request",
            "d12",
            {
                "action": "closed",
                "repository": repo_obj,
                "sender": {"login": "bob"},
                "pull_request": {"number": 11, "merged": False, "head": {"sha": "sha11"}},
            },
        )
        == 0
    )
    disabled = store.get_trigger("pr_act_only")
    assert disabled is not None
    assert (disabled.enabled, disabled.disabled_reason) == (False, "pr_closed")

    # Revocation blocks delivery and NEVER falls back to polling
    revoked_source = gh_source.model_copy(update={"subscribers": []})
    revoked_cfg, _ = _config(
        tmp_path,
        event_sources={"gh": revoked_source},
        env_values={_SECRET_ENV_NAME: _SECRET_VALUE},
    )
    ev_after = _signed_gh_event(
        source_id="gh",
        source=revoked_source,
        gh_event="issue_comment",
        delivery_id="d13",
        payload={
            "action": "created",
            "repository": repo_obj,
            "sender": {"login": "bob"},
            "issue": {"number": 10, "pull_request": {}},
            "comment": {"id": 99, "body": "after", "user": {"login": "bob"}},
        },
        paths=paths,
    )
    assert (
        fan_out_external_event(
            source_id="gh",
            source=revoked_source,
            event=ev_after,
            store=store,
            config=revoked_cfg,
            runtime_paths=paths,
            now=1200.0,
        )
        == 0
    )
    access = await check_reactive_source_access(
        rule=rule,
        event=latest.event,
        source_bot=None,
        config=revoked_cfg,
        runtime_paths=paths,
        memberships=AgentReplyMembershipIndex(),
    )
    assert access is not None
    assert (access.status, access.reason) == ("skipped", "source_subscriber_revoked")
    assert store.list_due_github_pr_triggers(now=9_999_999.0) == []


_ETAG_PR_PATH = "/repos/matrix-org/synapse/pulls/42"
_ETAG_TL_PATH = "/repos/matrix-org/synapse/issues/42/timeline"
_ETAG_RC_PATH = "/repos/matrix-org/synapse/pulls/42/comments"
_ETAG_CI_A = "/repos/matrix-org/synapse/commits/sha_a/check-runs"
_ETAG_CI_B = "/repos/matrix-org/synapse/commits/sha_b/check-runs"


def _etag_timeline_response(step: int, page: int) -> httpx.Response:
    if step == 1 and page == 1:
        entries = [{"id": i, "event": "commented", "user": {"login": "old"}, "body": "old"} for i in range(1, 101)]
        link = f'<https://api.github.com{_ETAG_TL_PATH}?per_page=100&page=2>; rel="next"'
        return httpx.Response(200, json=entries, headers={"ETag": '"tl-p1-full"', "Link": link})
    if step == 1:
        return httpx.Response(
            200,
            json=[
                {"id": 101, "event": "commented", "user": {"login": "reviewer", "type": "User"}, "body": "Fix lint"},
                {"id": 102, "event": "commented", "user": {"login": "bot[bot]", "type": "Bot"}, "body": "bot"},
                {"id": 103, "event": "commented", "user": {"login": "quiethuman", "type": "User"}, "body": "skip"},
                {"id": 104, "event": "committed", "actor": {"login": "reviewer"}},
            ],
            headers={"ETag": '"tl-p2"'},
        )
    if step == 5:
        return httpx.Response(
            200,
            json=[{"id": 200, "event": "commented", "user": {"login": "reviewer"}, "body": "while paused"}],
            headers={"ETag": '"tl-reseed"'},
        )
    if step == 6:
        return httpx.Response(
            200,
            json=[
                {"id": 200, "event": "commented", "user": {"login": "reviewer"}, "body": "while paused"},
                {"id": 201, "event": "commented", "user": {"login": "reviewer"}, "body": "after resume"},
            ],
            headers={"ETag": '"tl-after"'},
        )
    return httpx.Response(304, headers={"ETag": '"tl-p2"'})


def _etag_comments_response(step: int) -> httpx.Response:
    if step == 1:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 11,
                    "pull_request_review_id": 77,
                    "path": "a.py",
                    "body": "c1",
                    "user": {"login": "reviewer", "type": "User"},
                },
                {
                    "id": 12,
                    "pull_request_review_id": 77,
                    "path": "b.py",
                    "body": "c2",
                    "user": {"login": "reviewer", "type": "User"},
                },
                {
                    "id": 13,
                    "pull_request_review_id": 78,
                    "path": "c.py",
                    "body": "bot",
                    "user": {"login": "linter", "type": "Bot"},
                },
            ],
            headers={"ETag": '"rc-1"'},
        )
    if step == 5:
        return httpx.Response(200, json=[{"id": 20, "user": {"login": "reviewer"}, "body": "paused inline"}])
    return httpx.Response(304, headers={"ETag": '"rc-1"'})


def _etag_ci_response(path: str, step: int) -> httpx.Response:
    if path == _ETAG_CI_B:
        return httpx.Response(
            200,
            json={"check_runs": [{"id": 3, "name": "pytest", "status": "completed", "conclusion": "success"}]},
        )
    by_step = {
        1: [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "failure"},
            {"id": 2, "name": "lint", "status": "in_progress"},
        ],
        2: [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "failure"},
            {"id": 2, "name": "lint", "status": "completed", "conclusion": "success"},
        ],
    }
    runs = by_step.get(
        step,
        [
            {"id": 1, "name": "pytest", "status": "completed", "conclusion": "success"},
            {"id": 2, "name": "lint", "status": "completed", "conclusion": "success"},
        ],
    )
    return httpx.Response(200, json={"check_runs": runs}, headers={"ETag": f'"ci-a-{step}"'})


def _etag_poller_handler(phase: dict[str, int]) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(req: httpx.Request) -> httpx.Response:
        path, step = req.url.path, phase["step"]
        if path == _ETAG_PR_PATH:
            if step in {1, 2, 3}:
                assert req.headers.get("if-none-match") == '"pr-1"'
                return httpx.Response(304, headers={"ETag": '"pr-1"'})
            return httpx.Response(
                200,
                json={"state": "open", "merged": False, "head": {"sha": "sha_b"}, "title": "Synapse fix"},
                headers={"ETag": f'"pr-{step}"'},
            )
        if path == _ETAG_TL_PATH:
            return _etag_timeline_response(step, int(req.url.params.get("page", "1")))
        if path == _ETAG_RC_PATH:
            return _etag_comments_response(step)
        if path in {_ETAG_CI_A, _ETAG_CI_B}:
            return _etag_ci_response(path, step)
        return httpx.Response(404)

    return _handler


@pytest.mark.asyncio
async def test_etag_poller_304_pagination_batching_ci_and_silent_reseed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 304 still polls activity/CI; paginated items batch into 1 firing; CI resets on head_sha; re-enable re-seeds silently."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    rule = ReactiveTriggerRecord(
        trigger_id="pr_ext",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(
            repo="matrix-org/synapse",
            number=42,
            ignore_actors=["QuietHuman"],
            ignore_bots=True,
            poll_interval_seconds=60,
        ),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, thread_id=_THREAD, reopen_on_fire=True, prompt="Inspect"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(
        rule,
        initial_github_pr_checkpoint=GitHubPRCheckpoint(
            head_sha="sha_a",
            pr_state="open",
            seeded=True,
            next_poll_at=1000.0,
            state={
                "pr_etag": '"pr-1"',
                "pr_title": "Synapse fix",
                "timeline_page": 1,
                "timeline_etag": '"tl-p1"',
                "seen_timeline_ids": [f"commented:{i}" for i in range(1, 101)],
                "max_review_comment_id": 10,
                "review_comments_since": "2026-11-25T09:59:00Z",
                "ci_head_sha": "sha_a",
                "checks_etag": '"ci-a"',
                "notified_failed_checks": [],
                "last_ci_overall": "pending",
            },
            updated_at=1000.0,
        ),
    )

    phase = {"step": 1}
    handler = _etag_poller_handler(phase)
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )

    # Step 1: PR 304 + page-2 comment + grouped review comments + fail-fast CI -> 1 batched firing
    assert await poller.poll_due_once(now=1100.0) == 1
    firings = store.list_firings(trigger_id="pr_ext")
    assert len(firings) == 1
    items = firings[0].event.data["items"]
    assert [it["type"] for it in items] == ["comment", "review_comments", "ci_failure"]
    assert items[1]["count"] == 2

    # Step 2: restart / unchanged failure -> 0 firings
    phase["step"] = 2
    poller2 = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(handler),
    )
    assert await poller2.poll_due_once(now=1300.0) == 0

    # Step 3: all green on sha_a -> 1 wake; repeat -> 0
    phase["step"] = 3
    assert await poller2.poll_due_once(now=1500.0) == 1
    assert store.list_firings(trigger_id="pr_ext", limit=1)[0].event.data["items"][0]["type"] == "ci_passing"
    assert await poller2.poll_due_once(now=1700.0) == 0

    # Step 4: head_sha -> sha_b resets CI tracking and emits fresh all-green wake
    phase["step"] = 4
    assert await poller2.poll_due_once(now=1900.0) == 1
    assert store.list_firings(trigger_id="pr_ext", limit=1)[0].event.data["head_sha"] == "sha_b"

    # Step 5 & 6: disable + re-enable resets seeded=False; next poll re-seeds silently without replaying comment #200
    set_trigger_enabled_sync(
        paths,
        config,
        "pr_ext",
        enabled=False,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    set_trigger_enabled_sync(
        paths,
        config,
        "pr_ext",
        enabled=True,
        actor_user_id=_OWNER,
        authorize_reactive=lambda _r: None,
    )
    cp_paused = store.get_github_pr_checkpoint("pr_ext")
    assert cp_paused is not None
    assert cp_paused.seeded is False
    phase["step"] = 5
    assert await poller2.poll_due_once(now=cp_paused.next_poll_at + 1.0) == 0
    cp_reseeded = store.get_github_pr_checkpoint("pr_ext")
    assert cp_reseeded is not None
    assert cp_reseeded.seeded is True
    phase["step"] = 6
    assert await poller2.poll_due_once(now=cp_reseeded.next_poll_at + 1.0) == 1
    assert [it["id"] for it in store.list_firings(trigger_id="pr_ext", limit=1)[0].event.data["items"]] == [201]


@pytest.mark.asyncio
async def test_poller_rate_limit_backoff_and_terminal_retry_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate limit pauses bucket; unreadable PAT backs off safely; terminal merge survives pr_closed across retry."""
    config, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    for idx in (1, 2):
        rec = ReactiveTriggerRecord(
            trigger_id=f"pr_rl_{idx}",
            owner_user_id=_OWNER,
            source=GitHubPRSourceSpec(repo="org/repo", number=idx, poll_interval_seconds=60),
            target=TriggerTargetSpec(agent="dev", room_id=_ROOM, thread_id=_THREAD, reopen_on_fire=True, prompt="PR"),
            created_at=1000.0,
            updated_at=1000.0,
        )
        store.create_trigger(
            rec,
            initial_github_pr_checkpoint=GitHubPRCheckpoint(
                head_sha=f"s{idx}",
                seeded=True,
                next_poll_at=1000.0,
                updated_at=1000.0,
            ),
        )

    calls: list[str] = []
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr.resolve_owner_github_token",
        AsyncMock(return_value=("ghp_tok", "token")),
    )
    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(_rate_limit_handler(calls)),
        max_concurrency=1,
    )
    assert await poller.poll_due_once(now=2000.0) == 0
    assert len(calls) == 1
    for tid in ("pr_rl_1", "pr_rl_2"):
        cp = store.get_github_pr_checkpoint(tid)
        assert cp is not None
        assert cp.next_poll_at == pytest.approx(2120.0)

    # Unreadable PAT backs off with consecutive_errors incremented without falling back to anonymous
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr.resolve_owner_github_token",
        AsyncMock(side_effect=CredentialsUnreadableError("corrupt PAT")),
    )
    assert await poller.poll_due_once(now=2200.0) == 0
    cp1_err = store.get_github_pr_checkpoint("pr_rl_1")
    assert cp1_err is not None
    assert cp1_err.consecutive_errors == 1
    assert cp1_err.next_poll_at == pytest.approx(2320.0)

    # Terminal merge firing survives pr_closed auto-disable across transient Matrix failure + retry & reopens thread
    store.delete_trigger("pr_rl_2")
    store.upsert_github_pr_checkpoint(
        "pr_rl_1",
        GitHubPRCheckpoint(head_sha="s1", pr_state="open", seeded=True, next_poll_at=3000.0, updated_at=2500.0),
    )
    monkeypatch.setattr(
        "mindroom.reactive_triggers.github_pr.resolve_owner_github_token",
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
    cp_merged = store.get_github_pr_checkpoint("pr_rl_1")
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
    rule_after_claim = store.get_trigger("pr_rl_1")
    assert rule_after_claim is not None
    assert (rule_after_claim.enabled, rule_after_claim.disabled_reason) == (False, "pr_closed")

    firing = store.list_firings(trigger_id="pr_rl_1")[0]
    assert firing.status == "claimed"
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET next_retry_at = 0.0 WHERE id = ?", (firing.id,))
    await service.drain_due_firings_once()
    delivered = store.list_firings(trigger_id="pr_rl_1")[0]
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
async def test_review_wrapper_without_inline_event_403_anon_bucket_and_service_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty commented review fires without inline webhook event; 403 pauses anon bucket only; stop() cancels poll."""
    gh_no_inline = _gh_source(["mindroom-ai/mindroom"], ["pull_request", "pull_request_review"])
    config, paths = _config(tmp_path, event_sources={"gh": gh_no_inline}, env_values={_SECRET_ENV_NAME: _SECRET_VALUE})
    store = ReactiveTriggerStore(paths)
    wh_rule = ReactiveTriggerRecord(
        trigger_id="pr_no_inline",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(repo="mindroom-ai/mindroom", number=20, webhook_source_id="gh"),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(
        wh_rule,
        initial_github_pr_checkpoint=GitHubPRCheckpoint(head_sha="sha20", seeded=True, updated_at=1000.0),
    )
    ev = _signed_gh_event(
        source_id="gh",
        source=gh_no_inline,
        gh_event="pull_request_review",
        delivery_id="d_wrap",
        payload={
            "action": "submitted",
            "repository": {"full_name": "mindroom-ai/mindroom"},
            "sender": {"login": "carol", "type": "User"},
            "pull_request": {"number": 20, "title": "PR 20"},
            "review": {"id": 30, "state": "commented", "body": "", "user": {"login": "carol", "type": "User"}},
        },
        paths=paths,
    )
    assert (
        fan_out_external_event(
            source_id="gh",
            source=gh_no_inline,
            event=ev,
            store=store,
            config=config,
            runtime_paths=paths,
            now=1100.0,
        )
        == 1
    )

    revoked_cfg, _ = _config(tmp_path, event_sources={}, env_values={_SECRET_ENV_NAME: _SECRET_VALUE})
    with tool_runtime_context(_tool_ctx(revoked_cfg, paths)):
        denied = json.loads(await TriggersTools().triggers(action="history", trigger_id="pr_no_inline"))
        assert denied["status"] == "error"
        assert "no longer have access" in denied["message"]

    for tid, owner, repo, num in (
        ("pr_anon", _OWNER, "ext/anon", 1),
        ("pr_authed", _OTHER, "ext/authed", 2),
    ):
        store.create_trigger(
            ReactiveTriggerRecord(
                trigger_id=tid,
                owner_user_id=owner,
                source=GitHubPRSourceSpec(repo=repo, number=num, events=["merge"], poll_interval_seconds=60),
                target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
                created_at=1000.0,
                updated_at=1000.0,
            ),
            initial_github_pr_checkpoint=GitHubPRCheckpoint(
                head_sha="s",
                seeded=True,
                next_poll_at=1000.0,
                updated_at=1000.0,
            ),
        )

    async def _token_by_owner(owner: str, _cfg: object, _rp: object) -> tuple[str | None, str]:
        return (None, "anonymous") if owner == _OWNER else ("ghp_other", "token")

    monkeypatch.setattr("mindroom.reactive_triggers.github_pr.resolve_owner_github_token", _token_by_owner)
    hit_paths: list[str] = []
    poll_now = time.time()
    reset_epoch = int(poll_now) + 300

    def _bucket_handler(req: httpx.Request) -> httpx.Response:
        hit_paths.append(req.url.path)
        if req.url.path == "/repos/ext/anon/pulls/1":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)})
        return httpx.Response(200, json={"state": "open", "merged": False, "head": {"sha": "s"}})

    poller = GitHubPRPoller(
        store=store,
        config_provider=lambda: config,
        runtime_paths=paths,
        client=_mock_client(_bucket_handler),
        max_concurrency=1,
    )
    assert await poller.poll_due_once(now=poll_now) == 0
    assert hit_paths == ["/repos/ext/anon/pulls/1", "/repos/ext/authed/pulls/2"]
    cp_anon = store.get_github_pr_checkpoint("pr_anon")
    assert cp_anon is not None
    assert cp_anon.next_poll_at >= poll_now + 250.0

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

    service = ReactiveTriggerService(
        runtime_paths=paths,
        config_provider=lambda: config,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    monkeypatch.setattr(service, "poll_github_prs_once", _slow_poll)
    service._schedule_github_pr_poll()
    await started.wait()
    await service.stop()
    assert cancelled is True
    assert service._github_pr_poll_task is None
