"""End-to-end tests for unified GitHub PR watching (ETag poller + webhook nudge)."""

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

from mindroom import github_client as ghc
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.approval.tool_approval import evaluate_tool_approval, tool_may_require_approval, toolkit_approval_alias
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig, GitHubEventSourceConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsUnreadableError
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.dispatch import check_reactive_source_access
from mindroom.reactive_triggers.external_sources import fan_out_external_event, verify_and_normalize_github_webhook
from mindroom.reactive_triggers.github_pr import GitHubPRPoller
from mindroom.reactive_triggers.models import (
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_OWNER, _OTHER = "@owner:example.org", "@other:example.org"
_ROOM, _THREAD = "!lobby:example.org", "$thread_root"
_GH_ENV_NAME, _GH_HMAC_KEY = "TEST_GH_WEBHOOK_SECRET", "super-secret-webhook-key"


def _gh_source(
    allowed_repos: list[str],
    *,
    subscribers: list[str] | None = None,
    allowed_events: list[str] | None = None,
) -> GitHubEventSourceConfig:
    subs = [_OWNER] if subscribers is None else subscribers
    kw: dict[str, Any] = {} if allowed_events is None else {"allowed_events": allowed_events}
    return GitHubEventSourceConfig(secret_env=_GH_ENV_NAME, allowed_repos=allowed_repos, subscribers=subs, **kw)


def _config(
    tmp_path: Path,
    *,
    event_sources: dict[str, Any] | None = None,
    github_pr_polling_enabled: bool = True,
    min_poll: int = 60,
    max_triggers: int = 50,
    max_watches: int = 20,
) -> tuple[Config, RuntimePaths]:
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={_GH_ENV_NAME: _GH_HMAC_KEY},
    )
    state = MatrixState.load(runtime_paths=paths)
    state.add_room("lobby", _ROOM, "#lobby:example.org", "Lobby")
    save_matrix_state(state, paths)
    policy = ExternalTriggerPolicyConfig(
        event_sources=event_sources or {},
        github_pr_polling_enabled=github_pr_polling_enabled,
        min_github_pr_poll_interval_seconds=min_poll,
        max_reactive_triggers_per_owner=max_triggers,
        max_github_pr_watches_per_owner=max_watches,
    )
    dev = {"display_name": "Dev", "model": "default", "rooms": ["lobby"], "access": {"users": [_OWNER, _OTHER]}}
    raw = {
        "models": {"default": {"provider": "openai", "id": "gpt-test"}},
        "agents": {"dev": dev},
        "rooms": {"lobby": {"display_name": "Lobby"}},
        "external_trigger_policy": policy.model_dump(mode="json"),
    }
    return bind_runtime_paths(Config.model_validate(raw), paths), paths


def _room_client(*, resolved_thread: bool = False) -> SimpleNamespace:
    rooms = {_ROOM: SimpleNamespace(room_id=_ROOM, users={_OWNER: SimpleNamespace(user_id=_OWNER)})}
    tag_state = {"resolved": resolved_thread}
    rkey = json.dumps([_THREAD, "resolved"], separators=(",", ":"))
    tag_c = {"set_by": _OWNER, "set_at": "2026-11-25T10:00:00Z"}

    async def get_state(room_id: str) -> object:
        evs = [
            {
                "type": "m.room.member",
                "state_key": _OWNER,
                "sender": _OWNER,
                "event_id": "$m1",
                "origin_server_ts": 1000,
                "content": {"membership": "join"},
            },
        ]
        if tag_state["resolved"]:
            evs.append(
                {
                    "type": THREAD_TAGS_EVENT_TYPE,
                    "state_key": rkey,
                    "sender": _OWNER,
                    "event_id": "$t1",
                    "origin_server_ts": 1001,
                    "content": tag_c,
                },
            )
        return nio.RoomGetStateResponse(events=evs, room_id=room_id)

    async def get_ev(room_id: str, event_type: str, state_key: str = "") -> object:
        if tag_state["resolved"] and event_type == THREAD_TAGS_EVENT_TYPE and state_key == rkey:
            return nio.RoomGetStateEventResponse(tag_c, event_type, state_key, room_id)
        if event_type == "m.room.power_levels":
            pl = {"users": {_OWNER: 100, "@mindroom_dev:example.org": 100}, "events_default": 0, "state_default": 50}
            return nio.RoomGetStateEventResponse(pl, event_type, "", room_id)
        if event_type == "m.room.create":
            return nio.RoomGetStateEventResponse({"creator": _OWNER, "room_version": "10"}, event_type, "", room_id)
        return nio.RoomGetStateEventError(message="not found", status_code="M_NOT_FOUND")

    async def put_state(room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> object:
        if event_type == THREAD_TAGS_EVENT_TYPE and state_key == rkey and not content:
            tag_state["resolved"] = False
        return nio.RoomPutStateResponse(event_id="$reopen_tag", room_id=room_id)

    members = nio.JoinedMembersResponse(members=[nio.RoomMember(_OWNER, None, None)], room_id=_ROOM)
    return SimpleNamespace(
        rooms=rooms,
        user_id="@mindroom_dev:example.org",
        tag_state=tag_state,
        room_get_state=AsyncMock(side_effect=get_state),
        room_get_state_event=AsyncMock(side_effect=get_ev),
        room_put_state=AsyncMock(side_effect=put_state),
        joined_members=AsyncMock(return_value=members),
    )


def _tool_ctx(config: Config, paths: RuntimePaths) -> ToolRuntimeContext:
    client = _room_client()
    orch = SimpleNamespace(
        agent_bots={"dev": SimpleNamespace(running=True, client=client, agent_name="dev")},
        agent_reply_memberships=AgentReplyMembershipIndex(),
        reactive_triggers=SimpleNamespace(notify_work=MagicMock(), event_sources=None),
    )
    return make_test_tool_runtime_context(
        agent_name="dev",
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=_THREAD, reply_to_event_id=None),
        requester_id=_OWNER,
        client=client,
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=orch,
    )


async def _watch(tool: TriggersTools, **kwargs: object) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(await cast("Any", tool).triggers_write(action="watch_pr", **kwargs)))


async def _trig(tool: TriggersTools, **kwargs: object) -> dict[str, Any]:
    action = kwargs.get("action")
    fn = cast("Any", tool).triggers_read if action in {"list", "history", "test"} else cast("Any", tool).triggers_write
    return cast("dict[str, Any]", json.loads(await fn(**kwargs)))


def _cp(store: ReactiveTriggerStore, tid: str) -> GitHubPRCheckpoint:
    cp = store.get_github_pr_checkpoint(tid)
    assert cp is not None
    return cp


def _rule(store: ReactiveTriggerStore, tid: str) -> ReactiveTriggerRecord:
    rec = store.get_trigger(tid)
    assert rec is not None
    return rec


def _pr(
    repo: str,
    number: int,
    *,
    state: str = "open",
    merged: bool = False,
    sha: str = "sha1",
    closed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "state": "closed" if merged else state,
        "merged": merged,
        "merged_at": "2026-11-25T12:00:00Z" if merged else None,
        "closed_at": closed_at,
        "title": f"PR {number}",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "head": {"sha": sha},
    }


def _chk(cid: int, name: str, conclusion: str | None, *, status: str = "completed") -> dict[str, Any]:
    return {"id": cid, "name": name, "status": status, "conclusion": conclusion}


def _cmt(cid: int, body: str, login: str = "alice", *, user_type: str = "User") -> dict[str, Any]:
    return {"id": cid, "event": "commented", "body": body, "user": {"login": login, "type": user_type}}


def _rc(
    cid: int,
    rid: int | None,
    path: str,
    body: str,
    login: str = "alice",
    updated_at: str = "2026-11-25T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": cid,
        "pull_request_review_id": rid,
        "path": path,
        "body": body,
        "updated_at": updated_at,
        "user": {"login": login},
    }


class _FakeGitHub:
    """In-memory GitHub REST handler exercising real github_get_json ETag, Link, and rate-limit parsing."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, object, dict[str, str]]] = {}
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.on_request: Callable[[str], Awaitable[None]] | None = None

    def set_json(
        self,
        path: str,
        payload: object,
        *,
        status: int = 200,
        etag: str | None = None,
        headers: dict[str, str] | None = None,
        next_path: str | None = None,
        last_path: str | None = None,
    ) -> None:
        hdrs = dict(headers or {})
        if etag is not None:
            hdrs["ETag"] = etag
        links = [
            f'<https://api.github.com{p}>; rel="{rel}"' for rel, p in (("next", next_path), ("last", last_path)) if p
        ]
        if links:
            hdrs["Link"] = ", ".join(links)
        self.routes[path] = (status, payload, hdrs)

    def seed_open(self, repo: str, number: int, *, sha: str = "sha1", tag: str = "e") -> None:
        self.set_json(f"/repos/{repo}/pulls/{number}", _pr(repo, number, sha=sha), etag=f'"{tag}p"')
        self.set_json(f"/repos/{repo}/issues/{number}/timeline?per_page=100&page=1", [], etag=f'"{tag}t"')
        rc_base = f"/repos/{repo}/pulls/{number}/comments?sort=updated"
        self.set_json(f"{rc_base}&direction=desc&per_page=100", [], etag=f'"{tag}cs"')
        self.set_json(f"{rc_base}&direction=asc&per_page=100", [], etag=f'"{tag}c"')
        chk_p = f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page=1"
        self.set_json(chk_p, {"check_runs": []}, etag=f'"{tag}k"')

    def client(self) -> httpx.AsyncClient:
        async def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode("ascii")
            if self.on_request is not None:
                await self.on_request(path)
            inm = request.headers.get("if-none-match")
            self.calls.append((path, inm, request.headers.get("authorization")))
            route = self.routes.get(path)
            if route is None and "&since=" in path:
                prefix, _, rest = path.partition("&since=")
                tail = f"&{rest.partition('&')[2]}" if "&" in rest else ""
                route = self.routes.get(f"{prefix}{tail}")
            status, payload, hdrs = route or (404, {"message": "Not Found"}, {})
            if status == 200 and inm and hdrs.get("ETag") == inm:
                return httpx.Response(304, headers=hdrs, request=request)
            return httpx.Response(status, json=payload, headers=hdrs, request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _patch_github(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeGitHub,
    *,
    token_fn: Callable[..., Awaitable[tuple[str | None, ghc.GitHubAuthMode]]] | None = None,
) -> httpx.AsyncClient:
    http, orig_get = fake.client(), ghc.github_get_json

    async def _get(
        path: str,
        *,
        token: str | None,
        etag: str | None = None,
        client: httpx.AsyncClient | None = None,  # noqa: ARG001
        timeout_seconds: float = 20.0,
    ) -> ghc.GitHubGetResult:
        return await orig_get(path, token=token, etag=etag, client=http, timeout_seconds=timeout_seconds)

    async def _default_tok(*_args: object) -> tuple[str | None, ghc.GitHubAuthMode]:
        return ("tok-owner", "token")

    monkeypatch.setattr(ghc, "github_get_json", _get)
    monkeypatch.setattr(ghc, "resolve_owner_github_token", token_fn or _default_tok)
    return http


def _deliver_gh(
    src_cfg: GitHubEventSourceConfig,
    cfg: Config,
    paths: RuntimePaths,
    store: ReactiveTriggerStore,
    gh_event: str,
    repo: str,
    delivery_id: str,
    now: float,
) -> tuple[list[str], int, bool]:
    payload: dict[str, Any] = {"repository": {"full_name": repo}}
    if gh_event == "pull_request":
        pr = {"number": 5, "title": "Fix", "head": {"ref": "f", "sha": "sha1"}, "base": {"ref": "main"}}
        payload |= {"action": "synchronize", "pull_request": pr}
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(_GH_HMAC_KEY.encode("utf-8"), body, hashlib.sha256).hexdigest()
    hdrs = {"X-GitHub-Delivery": delivery_id, "X-GitHub-Event": gh_event, "X-Hub-Signature-256": sig}
    ev = verify_and_normalize_github_webhook(
        source_id="gh",
        source=src_cfg,
        headers=hdrs,
        body=body,
        runtime_paths=paths,
        now=now,
    )
    nudged = False

    def _on_nudged() -> None:
        nonlocal nudged
        nudged = True

    matched = fan_out_external_event(
        source_id="gh",
        source=src_cfg,
        event=ev,
        store=store,
        config=cfg,
        runtime_paths=paths,
        now=now,
        on_github_pr_nudged=_on_nudged,
    )
    return [r.trigger_id for r, _ in store.list_due_github_pr_triggers(now=now)], matched, nudged


@pytest.mark.asyncio
async def test_watch_pr_delivery_mode_validation_limits_and_unreadable_pat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_pr binds covering webhooks with zero idle polling, falls back to polling, and enforces policy/quotas."""
    fake = _FakeGitHub()
    fake.seed_open("org/owned", 1, tag="o1")
    fake.set_json("/repos/org/owned/issues/1/timeline?per_page=100&page=1", [_cmt(1, "old")])
    fake.seed_open("ext/repo", 2, tag="e2")
    fake.set_json("/repos/ext/repo/pulls/3", _pr("ext/repo", 3, merged=True))
    _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path, event_sources={"gh": _gh_source(["org/owned"])}, min_poll=120, max_watches=2)
    tool, store = TriggersTools(), ReactiveTriggerStore(paths)

    with tool_runtime_context(_tool_ctx(cfg, paths)):
        wh = await _watch(tool, repo="org/owned", number=1, trigger_id="pr-wh")
        assert (wh["status"], wh["delivery_mode"], wh["webhook_source_id"], wh["auth_mode"]) == (
            "ok",
            "webhook",
            "gh",
            "token",
        )
        assert (wh["trigger"]["target"]["reopen_on_fire"], wh["trigger"]["target"]["thread_id"]) == (True, _THREAD)
        assert (wh["trigger"]["source"]["ignore_bots"], wh["trigger"]["source"]["auto_disable_on_close"]) == (
            True,
            True,
        )
        assert wh["trigger"]["expires_at"] is not None
        assert abs((wh["trigger"]["expires_at"] - wh["trigger"]["created_at"]) - 720.0 * 3600.0) < 1.0
        assert (_cp(store, "pr-wh").seeded, _cp(store, "pr-wh").next_poll_at) == (True, None)
        assert store.list_due_github_pr_triggers(now=time.time() + 1_000) == []

        polled = await _watch(tool, repo="ext/repo", number=2, poll_interval_seconds=30, trigger_id="pr-poll")
        assert (polled["status"], polled["delivery_mode"], polled["webhook_source_id"]) == ("ok", "poll", None)
        assert polled["trigger"]["source"]["poll_interval_seconds"] == 120
        assert _cp(store, "pr-poll").next_poll_at is not None

        for bad_kwargs, expected in (
            ({"number": 1}, "repo is required"),
            ({"repo": "   ", "number": 1}, "repo is required"),
            ({"repo": "bad-repo", "number": 1}, "Invalid GitHub repository"),
            ({"repo": "ext/repo"}, "number is required"),
            ({"repo": "ext/repo", "number": True}, "positive integer"),
            ({"repo": "ext/repo", "number": False}, "positive integer"),
            ({"repo": "ext/repo", "number": 0}, "positive integer"),
            ({"repo": "ext/repo", "number": -2}, "positive integer"),
            ({"repo": "ext/repo", "number": "2"}, "positive integer"),
            ({"repo": "ext/repo", "number": 2, "poll_interval_seconds": True}, "integer"),
            ({"repo": "ext/repo", "number": 2, "room_id": _ROOM}, "cannot be used with action='watch_pr'"),
            (
                {"repo": "ext/repo", "number": 2, "triage_instructions": "only prod"},
                "cannot be used with action='watch_pr'",
            ),
            ({"repo": "ext/repo", "number": 2, "thread_key": "k"}, "cannot be used with action='watch_pr'"),
        ):
            bad_res = await _watch(tool, **bad_kwargs)
            assert bad_res["status"] == "error", (bad_kwargs, bad_res)
            assert expected in bad_res["message"], (bad_kwargs, bad_res)

        merged_err = await _watch(tool, repo="ext/repo", number=3)
        assert (merged_err["status"], "already merged" in merged_err["message"]) == ("error", True)
        quota_err = await _watch(tool, repo="ext/repo", number=2, trigger_id="pr-over")
        assert (quota_err["status"], "GitHub PR watch limit" in quota_err["message"]) == ("error", True)

        assert (await _trig(tool, action="set_enabled", enabled=False, trigger_id="pr-poll"))["status"] == "ok"
        pr_2b = await _watch(
            tool,
            repo="ext/repo",
            number=2,
            trigger_id="pr-2b",
            include_bots=True,
            reopen_on_fire=False,
            auto_disable_on_close=False,
        )
        assert pr_2b["status"] == "ok"
        assert (
            pr_2b["trigger"]["source"]["ignore_bots"],
            pr_2b["trigger"]["source"]["auto_disable_on_close"],
            pr_2b["trigger"]["target"]["reopen_on_fire"],
        ) == (False, False, False)
        reen_err = await _trig(tool, action="set_enabled", enabled=True, trigger_id="pr-poll")
        assert (reen_err["status"], "GitHub PR watch limit" in reen_err["message"]) == ("error", True)

    np_cfg, np_paths = _config(
        tmp_path / "nopoll",
        event_sources={"gh": _gh_source(["org/owned"])},
        github_pr_polling_enabled=False,
    )
    with tool_runtime_context(_tool_ctx(np_cfg, np_paths)):
        assert (await _watch(tool, repo="ext/repo", number=2))["status"] == "error"
        assert (await _watch(tool, repo="org/owned", number=1))["delivery_mode"] == "webhook"

    fake.calls.clear()

    async def _unreadable(*_args: object) -> tuple[str | None, ghc.GitHubAuthMode]:
        msg = "corrupt credentials.json"
        raise CredentialsUnreadableError(msg)

    monkeypatch.setattr(ghc, "resolve_owner_github_token", _unreadable)
    with tool_runtime_context(_tool_ctx(np_cfg, np_paths)):
        err = await _watch(tool, repo="org/owned", number=1, trigger_id="pr-unreadable")
        assert (err["status"], "Unable to resolve GitHub credentials" in err["message"]) == ("error", True)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_webhook_nudge_end_to_end_concurrent_nudge_and_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed GitHub webhooks nudge matching watches, preserve concurrent nudges, and fail closed on revocation."""
    fake = _FakeGitHub()
    fake.seed_open("org/owned", 5, tag="w5")
    http = _patch_github(monkeypatch, fake)

    src_cfg = _gh_source(["org/owned", "org/other"])
    cfg, paths = _config(tmp_path, event_sources={"gh": src_cfg})
    store = ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        assert (await _watch(TriggersTools(), repo="org/owned", number=5, trigger_id="pr-wh-5"))[
            "delivery_mode"
        ] == "webhook"

    for tid, owner, enabled in (("pr-dis", _OWNER, False), ("pr-unauth", _OTHER, True)):
        rec = ReactiveTriggerRecord(
            trigger_id=tid,
            owner_user_id=owner,
            enabled=enabled,
            source=GitHubPRSourceSpec(repo="org/owned", number=9, webhook_source_id="gh"),
            target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
            created_at=1000.0,
            updated_at=1000.0,
        )
        store.create_trigger(rec, initial_github_pr_checkpoint=GitHubPRCheckpoint(seeded=True, updated_at=1000.0))

    assert _deliver_gh(src_cfg, cfg, paths, store, "ping", "org/owned", "d-ping", 2000.0) == ([], 0, False)
    assert _deliver_gh(src_cfg, cfg, paths, store, "pull_request", "org/other", "d-other", 2001.0) == ([], 0, False)

    fake.set_json("/repos/org/owned/issues/5/timeline?per_page=100&page=1", [_cmt(50, "LGTM!")], etag='"w5t-2"')
    assert _deliver_gh(src_cfg, cfg, paths, store, "pull_request", "org/owned", "d-hit1", 2010.0) == (
        ["pr-wh-5"],
        0,
        True,
    )

    nudged = False

    async def _inject_nudge(path: str) -> None:
        nonlocal nudged
        if not nudged and "/timeline" in path:
            nudged = True
            store.nudge_github_pr_checkpoints(["pr-wh-5"], now=2015.0)

    fake.on_request = _inject_nudge
    active_cfg = cfg
    poller = GitHubPRPoller(store=store, config_provider=lambda: active_cfg, runtime_paths=paths, client=http)
    assert await poller.poll_due_once(now=2010.0) == 1
    firings = store.list_firings(trigger_id="pr-wh-5")
    assert [f.event.summary for f in firings] == ["Comment by alice: LGTM!"]
    assert _cp(store, "pr-wh-5").next_poll_at == 2015.0

    fake.on_request = None
    assert await poller.poll_due_once(now=2015.0) == 0
    assert _cp(store, "pr-wh-5").next_poll_at is None
    assert store.list_due_github_pr_triggers(now=time.time() + 1_000) == []

    for expected, sources in (
        ("source_grant_revoked", {"gh": _gh_source(["org/other"])}),
        ("source_grant_revoked", {"gh": _gh_source(["org/owned"], allowed_events=["pull_request"])}),
        ("source_subscriber_revoked", {"gh": _gh_source(["org/owned"], subscribers=[_OTHER])}),
        ("source_removed", {}),
    ):
        store.nudge_github_pr_checkpoints(["pr-wh-5"], now=2020.0)
        active_cfg, _ = _config(tmp_path, event_sources=sources)
        rev_cfg = active_cfg
        fake.calls.clear()
        assert await poller.poll_due_once(now=2020.0) == 0
        assert fake.calls == []
        assert _cp(store, "pr-wh-5").next_poll_at is None
        access = await check_reactive_source_access(
            rule=_rule(store, "pr-wh-5"),
            event=firings[0].event,
            source_bot=None,
            config=rev_cfg,
            runtime_paths=paths,
            memberships=AgentReplyMembershipIndex(),
        )
        assert access is not None
        assert (access.status, access.reason) == ("skipped", expected)


@pytest.mark.asyncio
async def test_poller_etag_304_timeline_pagination_and_grouped_review_comments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeding jumps directly to rel='last', 304 PR still polls collections, and timeline/review pagination advances cleanly."""
    fake = _FakeGitHub()
    tl = "/repos/ext/repo/issues/10/timeline?per_page=100&page="
    rc_seed = "/repos/ext/repo/pulls/10/comments?sort=updated&direction=desc&per_page=100"
    rc1 = "/repos/ext/repo/pulls/10/comments?sort=updated&direction=asc&per_page=100&since=2026-11-25T10:00:00Z"
    rc2 = f"{rc1}&page=2"
    fake.set_json("/repos/ext/repo/pulls/10", _pr("ext/repo", 10), etag='"pr10"')
    fake.set_json(f"{tl}1", [_cmt(1, "old")], last_path=f"{tl}10")
    fake.set_json(f"{tl}10", [_cmt(100, "seed tail")], etag='"tl10"')
    fake.set_json(rc_seed, [_rc(500, None, "a.py", "old inline", "bob", "2026-11-25T10:00:00Z")], etag='"rc0"')
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        created = await _watch(
            TriggersTools(),
            repo="ext/repo",
            number=10,
            events=["activity"],
            ignore_actors=["muted"],
            include_bots=False,
            trigger_id="pr-10",
        )
        assert created["status"] == "ok"

    assert [p for p, _, _ in fake.calls if "/timeline" in p] == [f"{tl}1", f"{tl}10"]
    assert store.list_firings(trigger_id="pr-10") == []

    tl10 = [
        _cmt(100, "seed tail"),
        _cmt(101, "bot", "ci[bot]", user_type="Bot"),
        _cmt(102, "muted", "muted"),
        {"id": 103, "event": "reviewed", "state": "approved", "body": "Ship it", "user": {"login": "alice"}},
    ]
    fake.set_json(f"{tl}10", tl10, etag='"tl10-new"', next_path=f"{tl}11")
    fake.set_json(
        f"{tl}11",
        [{"id": 104, "event": "labeled", "label": {"name": "ready"}, "actor": {"login": "alice"}}],
        next_path=f"{tl}12",
    )
    fake.set_json(f"{tl}12", [_cmt(105, "page 12", "carol")], next_path=f"{tl}13")
    fake.set_json(f"{tl}13", [_cmt(106, "page 13", "dave")], etag='"tl13"')
    fake.set_json(
        rc1,
        [
            _rc(500, None, "a.py", "old inline", "bob", "2026-11-25T10:00:00Z"),
            _rc(501, None, "c.py", "Solo", "erin", "2026-11-25T10:01:00Z"),
        ],
        etag='"rc-new"',
        next_path=rc2,
    )
    fake.set_json(
        rc2,
        [
            _rc(502, 77, "a.py", "First", updated_at="2026-11-25T10:02:00Z"),
            _rc(503, 77, "b.py", "Second", updated_at="2026-11-25T10:03:00Z"),
        ],
    )
    fake.set_json(
        "/repos/ext/repo/pulls/10/comments?sort=updated&direction=asc&per_page=100&since=2026-11-25T10:03:00Z",
        [_rc(503, 77, "b.py", "Second", updated_at="2026-11-25T10:03:00Z")],
    )

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    t0 = time.time() + 1_000
    assert await poller.poll_due_once(now=t0) == 1
    items = store.list_firings(trigger_id="pr-10")[0].event.data["items"]
    assert [it["type"] for it in items] == ["review", "labeled", "comment", "review_comment", "review_comments"]
    grouped = next(it for it in items if it["type"] == "review_comments")
    assert (grouped["count"], [c["id"] for c in grouped["comments"]]) == (2, [502, 503])
    assert (_cp(store, "pr-10").state["timeline_page"], _cp(store, "pr-10").state["timeline_etag"]) == (13, None)

    assert await poller.poll_due_once(now=t0 + 10_000) == 1
    assert any(f.event.summary == "Comment by dave: page 13" for f in store.list_firings(trigger_id="pr-10"))
    assert (_cp(store, "pr-10").state["timeline_page"], _cp(store, "pr-10").state["timeline_etag"]) == (13, '"tl13"')


@pytest.mark.asyncio
async def test_poller_ci_check_runs_transitions_cancelled_truncation_and_sha_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI check-runs fail fast, keep cancelled silent, suppress truncated all-green, emit all-green once, and reset on new SHA."""
    fake = _FakeGitHub()
    fake.seed_open("ext/repo", 20, sha="shaA", tag="c20")
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        assert (await _watch(TriggersTools(), repo="ext/repo", number=20, events=["ci"], trigger_id="pr-ci"))[
            "status"
        ] == "ok"

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    t0 = time.time() + 1_000
    c_a = "/repos/ext/repo/commits/shaA/check-runs?per_page=100&page="
    runs_a1 = [
        _chk(1, "pytest", "failure"),
        _chk(2, "cancel", "cancelled"),
        _chk(3, "lint", None, status="in_progress"),
    ]
    fake.set_json(f"{c_a}1", {"check_runs": runs_a1}, etag='"k1"')
    assert await poller.poll_due_once(now=t0) == 1
    assert [it["type"] for it in store.list_firings(trigger_id="pr-ci")[0].event.data["items"]] == ["ci_failure"]
    runs_a2 = [_chk(1, "pytest", "failure"), _chk(2, "cancel", "cancelled"), _chk(3, "lint", "success")]
    fake.set_json(f"{c_a}1", {"check_runs": runs_a2}, etag='"k2"')
    assert await poller.poll_due_once(now=t0 + 10_000) == 0

    fake.set_json("/repos/ext/repo/pulls/20", _pr("ext/repo", 20, sha="shaB"), etag='"pB"')
    c_b = "/repos/ext/repo/commits/shaB/check-runs?per_page=100&page="
    for idx in (1, 2, 3):
        fake.set_json(f"{c_b}{idx}", {"check_runs": [_chk(idx, f"ok-{idx}", "success")]}, next_path=f"{c_b}{idx + 1}")
    assert await poller.poll_due_once(now=t0 + 20_000) == 0
    fake.set_json(f"{c_b}2", {"check_runs": [_chk(22, "build", "timed_out")]}, next_path=f"{c_b}3")
    assert await poller.poll_due_once(now=t0 + 30_000) == 1
    assert store.list_firings(trigger_id="pr-ci")[0].event.data["items"][0]["conclusion"] == "timed_out"

    fake.set_json("/repos/ext/repo/pulls/20", _pr("ext/repo", 20, sha="shaC"), etag='"pC"')
    c_c = "/repos/ext/repo/commits/shaC/check-runs?per_page=100&page=1"
    fake.set_json(c_c, {"check_runs": [_chk(31, "unit", "success"), _chk(32, "opt", "cancelled")]}, etag='"kC1"')
    assert await poller.poll_due_once(now=t0 + 40_000) == 0
    fake.set_json(c_c, {"check_runs": [_chk(31, "unit", "success"), _chk(32, "opt", "skipped")]}, etag='"kC2"')
    assert await poller.poll_due_once(now=t0 + 50_000) == 1
    first_pass = store.list_firings(trigger_id="pr-ci")[0].event
    assert first_pass.data["items"][0]["type"] == "ci_passing"
    assert "GitHub Check Runs passed" in first_pass.data["items"][0]["summary"]
    assert await poller.poll_due_once(now=t0 + 60_000) == 0
    # Pass -> fail -> pass recovery on the same SHA must not be dropped by event dedupe.
    fake.set_json(c_c, {"check_runs": [_chk(31, "unit", "success"), _chk(33, "opt", "failure")]}, etag='"kC3"')
    assert await poller.poll_due_once(now=t0 + 70_000) == 1
    assert store.list_firings(trigger_id="pr-ci")[0].event.data["items"][0]["type"] == "ci_failure"
    fake.set_json(c_c, {"check_runs": [_chk(31, "unit", "success"), _chk(34, "opt", "success")]}, etag='"kC4"')
    assert await poller.poll_due_once(now=t0 + 80_000) == 1
    recovery_pass = store.list_firings(trigger_id="pr-ci")[0].event
    assert recovery_pass.data["items"][0]["type"] == "ci_passing"
    assert recovery_pass.event_id != first_pass.event_id


@pytest.mark.asyncio
async def test_poller_rate_limit_buckets_adaptive_backoff_and_unreadable_pat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate limits pause only the affected bucket, low remaining doubles delay, and unreadable PATs fail closed."""
    fake = _FakeGitHub()
    fake.set_json("/repos/ext/r1/pulls/1", {}, status=429, headers={"Retry-After": "180"})
    fake.set_json("/repos/ext/r2/pulls/2", _pr("ext/r2", 2), headers={"x-ratelimit-remaining": "5"})
    fake.set_json("/repos/ext/r2/issues/2/timeline?per_page=100&page=1", [])
    fake.set_json("/repos/ext/r2/pulls/2/comments?sort=updated&direction=asc&per_page=100", [])

    async def _tok(owner: str, _cfg: Config, _paths: RuntimePaths) -> tuple[str | None, ghc.GitHubAuthMode]:
        if owner == "@broken:example.org":
            msg = "bad creds"
            raise CredentialsUnreadableError(msg)
        return ("tok1", "token") if owner == _OWNER else (None, "anonymous")

    http = _patch_github(monkeypatch, fake, token_fn=_tok)
    cfg, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    base_cp = GitHubPRCheckpoint(head_sha="sha1", seeded=True, next_poll_at=1000.0, updated_at=900.0)
    for tid, owner, repo, num in (
        ("r-rl", _OWNER, "ext/r1", 1),
        ("r-anon", _OTHER, "ext/r2", 2),
        ("r-bad", "@broken:example.org", "ext/r3", 3),
    ):
        rec = ReactiveTriggerRecord(
            trigger_id=tid,
            owner_user_id=owner,
            source=GitHubPRSourceSpec(repo=repo, number=num, events=["activity"], poll_interval_seconds=100),
            target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
            created_at=900.0,
            updated_at=900.0,
        )
        store.create_trigger(rec, initial_github_pr_checkpoint=base_cp)

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    assert await poller.poll_due_once(now=1000.0) == 0
    assert _cp(store, "r-rl").next_poll_at == 1180.0
    anon_nxt = _cp(store, "r-anon").next_poll_at
    assert anon_nxt is not None
    assert anon_nxt >= 1180.0
    assert (_cp(store, "r-bad").consecutive_errors, _cp(store, "r-bad").next_poll_at) == (1, 1200.0)
    assert all("/ext/r3/" not in p for p, _, _ in fake.calls)

    # Raising min_github_pr_poll_interval_seconds takes effect immediately and jitter never dips below the floor.
    fake.set_json("/repos/ext/r2/pulls/2", _pr("ext/r2", 2), headers={"x-ratelimit-remaining": "500"})
    raised_cfg, _ = _config(tmp_path, min_poll=240)
    monkeypatch.setattr("mindroom.reactive_triggers.github_pr.random.uniform", lambda _a, _b: 0.9)
    raised_poller = GitHubPRPoller(store=store, config_provider=lambda: raised_cfg, runtime_paths=paths, client=http)
    assert await raised_poller.poll_due_once(now=2000.0) == 0
    assert _cp(store, "r-anon").next_poll_at == 2240.0


@pytest.mark.asyncio
async def test_terminal_pr_close_lifecycle_reopen_thread_and_auto_disable_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal PR events survive retries, reopen resolved threads, auto-disable on settle, and honor auto_disable_on_close=False."""
    fake = _FakeGitHub()
    for n in (30, 31, 32, 33, 34):
        fake.seed_open("ext/repo", n, tag=f"p{n}")
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        tools = TriggersTools()
        for kwargs in (
            {"number": 30, "events": ["merge"], "trigger_id": "pr-merge"},
            {"number": 31, "events": ["activity"], "trigger_id": "pr-unsub"},
            {"number": 32, "events": ["close"], "auto_disable_on_close": False, "trigger_id": "pr-keep"},
            {"number": 33, "events": ["close"], "trigger_id": "pr-skip"},
            {"number": 34, "events": ["close"], "trigger_id": "pr-exhaust"},
        ):
            assert (await _watch(tools, repo="ext/repo", **kwargs))["status"] == "ok"

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    fake.set_json("/repos/ext/repo/pulls/30", _pr("ext/repo", 30, merged=True), etag='"p30-m"')
    for n in (31, 32, 33, 34):
        fake.set_json(
            f"/repos/ext/repo/pulls/{n}",
            _pr("ext/repo", n, state="closed", sha="sha1", closed_at="2026-11-25T12:00:00Z"),
            etag=f'"p{n}-c"',
        )
    t0 = time.time() + 1_000
    assert await poller.poll_due_once(now=t0) == 4

    assert (_rule(store, "pr-unsub").enabled, _rule(store, "pr-unsub").disabled_reason) == (False, "pr_closed")
    assert store.list_firings(trigger_id="pr-unsub") == []
    assert store.mark_skipped(store.list_firings(trigger_id="pr-skip")[0].id, "triage_no", now=t0 + 1)
    assert (_rule(store, "pr-skip").enabled, _rule(store, "pr-skip").disabled_reason) == (False, "pr_closed")
    ex_id = store.list_firings(trigger_id="pr-exhaust")[0].id
    assert store.defer_pending_firing(ex_id, "err", now=t0 + 1, max_attempts=2) == "retrying"
    assert _rule(store, "pr-exhaust").enabled is True
    assert store.defer_pending_firing(ex_id, "err", now=t0 + 2, max_attempts=2) == "failed"
    assert (_rule(store, "pr-exhaust").enabled, _rule(store, "pr-exhaust").disabled_reason) == (False, "pr_closed")

    keep_f = store.list_firings(trigger_id="pr-keep")[0]
    assert store.claim_firing(keep_f.id, now=1.0) == "claimed"
    assert store.mark_delivered(keep_f.id, "$k_del", now=2.0)

    room_cli = _room_client(resolved_thread=True)
    reader = SimpleNamespace(latest_thread_event_id=AsyncMock(return_value="$latest"))
    bot = SimpleNamespace(running=True, client=room_cli, agent_name="dev", conversation_reader=reader)
    send_mock = AsyncMock(side_effect=[None, DeliveredMatrixEvent(event_id="$delivered_merge", content_sent={})])
    monkeypatch.setattr("mindroom.reactive_triggers.dispatch.send_matrix_message", send_mock)
    service = ReactiveTriggerService(
        runtime_paths=paths,
        config_provider=lambda: cfg,
        bots=lambda: {"dev": cast("Any", bot)},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    await service.drain_due_firings_once()
    assert (_rule(store, "pr-merge").enabled, _rule(store, "pr-merge").disabled_reason) == (False, "pr_closed")
    merge_f = store.list_firings(trigger_id="pr-merge")[0]
    assert merge_f.status == "claimed"
    with store._write_transaction() as conn:
        conn.execute("UPDATE trigger_firings SET next_retry_at = 0.0 WHERE id = ?", (merge_f.id,))
    await service.drain_due_firings_once()
    delivered = store.list_firings(trigger_id="pr-merge")[0]
    assert (delivered.status, delivered.matrix_event_id, room_cli.tag_state["resolved"]) == (
        "delivered",
        "$delivered_merge",
        False,
    )

    assert _rule(store, "pr-keep").enabled is True
    fake.set_json("/repos/ext/repo/pulls/32", _pr("ext/repo", 32, state="open", sha="sha1"), etag='"p32-o"')
    assert await poller.poll_due_once(now=t0 + 10_000) == 0
    fake.set_json(
        "/repos/ext/repo/pulls/32",
        _pr("ext/repo", 32, state="closed", sha="sha1", closed_at="2026-11-25T13:00:00Z"),
        etag='"p32-c2"',
    )
    assert await poller.poll_due_once(now=t0 + 20_000) == 1
    assert len(store.list_firings(trigger_id="pr-keep")) == 2


@pytest.mark.asyncio
async def test_disable_reenable_reseeds_silently_and_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-enable resets checkpoint to silently reseed back to idle, and dry-run filters actors/categories."""
    fake = _FakeGitHub()
    fake.seed_open("org/owned", 40, tag="r40")
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path, event_sources={"gh": _gh_source(["org/owned"])})
    store, tools = ReactiveTriggerStore(paths), TriggersTools()
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        created = await _watch(
            tools,
            repo="org/owned",
            number=40,
            events=["activity", "merge"],
            ignore_actors=["noisy"],
            trigger_id="pr-40",
        )
        assert created["status"] == "ok"
        assert (await _trig(tools, action="test", trigger_id="pr-40", kind="merge"))["matched"] is True
        assert (await _trig(tools, action="test", trigger_id="pr-40", kind="ci"))["matched"] is False
        assert (await _trig(tools, action="test", trigger_id="pr-40", kind="ci_failure"))["matched"] is False
        ign_dry = await _trig(tools, action="test", trigger_id="pr-40", kind="activity", actor="noisy")
        assert (ign_dry["matched"], "ignored" in ign_dry["note"]) == (False, True)
        upd_ok = await _trig(
            tools,
            action="test",
            trigger_id="pr-40",
            kind="github_pr.update",
            sample_data={"items": [{"type": "comment", "actor": "alice"}]},
        )
        assert (upd_ok["status"], upd_ok["matched"], upd_ok["would_fire"]) == ("ok", True, True)
        upd_ci = await _trig(
            tools,
            action="test",
            trigger_id="pr-40",
            kind="github_pr.update",
            sample_data={"items": [{"type": "ci_failure", "name": "pytest"}]},
        )
        assert upd_ci["matched"] is False
        upd_ign = await _trig(
            tools,
            action="test",
            trigger_id="pr-40",
            kind="github_pr.update",
            sample_data={"items": [{"type": "comment", "actor": "noisy"}]},
        )
        assert (upd_ign["matched"], "ignored" in upd_ign["note"]) == (False, True)

        assert (await _trig(tools, action="set_enabled", enabled=False, trigger_id="pr-40"))["status"] == "ok"
        fake.set_json("/repos/org/owned/issues/40/timeline?per_page=100&page=1", [_cmt(999, "paused")], etag='"r40t-p"')
        assert (await _trig(tools, action="set_enabled", enabled=True, trigger_id="pr-40"))["status"] == "ok"

    assert _cp(store, "pr-40").seeded is False
    assert _cp(store, "pr-40").next_poll_at is not None
    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    assert await poller.poll_due_once(now=time.time() + 1_000) == 0
    assert store.list_firings(trigger_id="pr-40") == []
    assert (_cp(store, "pr-40").seeded, _cp(store, "pr-40").next_poll_at) == (True, None)


@pytest.mark.asyncio
async def test_watch_pr_action_governed_by_triggers_approval_rules(tmp_path: Path) -> None:
    """`triggers_write(action="watch_pr", ...)` is governed natively by `triggers_write` rules and action-scoped grants."""
    cfg, paths = _config(tmp_path)
    req_rules = ToolApprovalConfig(rules=[ApprovalRuleConfig(match="triggers_write", action="require_approval")])
    auto_rules = ToolApprovalConfig(rules=[ApprovalRuleConfig(match="triggers_write", action="auto_approve")])
    req_cfg = bind_runtime_paths(cfg.model_copy(update={"tool_approval": req_rules}), paths)
    auto_cfg = bind_runtime_paths(cfg.model_copy(update={"tool_approval": auto_rules}), paths)

    registry = builtin_tool_registry()
    meta = registry.entries["triggers"]
    assert meta.function_names == ("triggers_read", "triggers_write")
    assert set(meta.approval_aliases) == {"triggers_write"}
    triggers_alias = meta.approval_aliases["triggers_write"]
    assert triggers_alias.judged_as == "script_write"
    assert toolkit_approval_alias(req_cfg, registry, "triggers", "watch_pr") is None

    args: dict[str, object] = {"action": "watch_pr", "repo": "org/owned", "number": 40}
    assert triggers_alias.judge(args) == []
    assert tool_may_require_approval(req_cfg, "triggers_write") is True
    assert (await evaluate_tool_approval(req_cfg, paths, "triggers_write", args, "dev", triggers_alias))[0] is True
    assert tool_may_require_approval(auto_cfg, "triggers_write") is False
    assert (await evaluate_tool_approval(auto_cfg, paths, "triggers_write", args, "dev", triggers_alias))[0] is False

    watch_pr_op = grant_operation(req_cfg, "triggers_write", args, tools=registry, toolkit_name="triggers")
    list_op = grant_operation(req_cfg, "triggers_read", {"action": "list"}, tools=registry, toolkit_name="triggers")
    assert watch_pr_op is not None
    assert list_op is not None
    assert watch_pr_op.mcp_server_id == "triggers"
    assert watch_pr_op.mcp_tool_name == "watch_pr"
    assert watch_pr_op != list_op


@pytest.mark.asyncio
async def test_service_notify_work_repoll_and_stop_cancels_inflight_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_work queues a repoll only when poll_github_prs=True, and stop() cancels in-flight PR polls."""
    cfg, paths = _config(tmp_path)
    service = ReactiveTriggerService(
        runtime_paths=paths,
        config_provider=lambda: cfg,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=ReactiveTriggerStore(paths),
    )
    started1, release1, done2, calls = asyncio.Event(), asyncio.Event(), asyncio.Event(), 0

    async def _controlled_poll() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            started1.set()
            await release1.wait()
        else:
            done2.set()
        return 0

    monkeypatch.setattr(service, "poll_github_prs_once", _controlled_poll)
    monkeypatch.setattr(service, "poll_room_messages_once", AsyncMock(return_value=0))
    service.start()
    try:
        await asyncio.wait_for(started1.wait(), timeout=5.0)
        service.notify_work(poll_github_prs=False)
        await asyncio.sleep(0.05)
        assert service._pr_poll_requested is False
        service.notify_work(poll_github_prs=True)
        await asyncio.sleep(0.05)
        assert service._pr_poll_requested is True
        release1.set()
        await asyncio.wait_for(done2.wait(), timeout=5.0)
        assert calls == 2

        started2, cancelled = asyncio.Event(), False

        async def _slow_poll() -> int:
            nonlocal cancelled
            started2.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return 0

        monkeypatch.setattr(service, "poll_github_prs_once", _slow_poll)
        service.notify_work(poll_github_prs=True)
        await asyncio.wait_for(started2.wait(), timeout=5.0)
    finally:
        await service.stop()
    assert cancelled is True
    assert service._github_pr_poll_task is None


@pytest.mark.asyncio
async def test_webhook_allowed_events_coverage_and_owner_quota_on_pr_closed_revive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook binding requires allowed_events coverage, and reviving pr_closed enforces max_reactive_triggers_per_owner."""
    fake = _FakeGitHub()
    for n in (1, 2, 3):
        fake.seed_open("org/owned", n, tag=f"ev{n}")
    _patch_github(monkeypatch, fake)

    narrow_src = _gh_source(["org/owned"], allowed_events=["pull_request"])
    cfg, paths = _config(tmp_path, event_sources={"gh": narrow_src}, max_triggers=1, max_watches=5)
    tool, store = TriggersTools(), ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        m_only = await _watch(tool, repo="org/owned", number=1, events=["merge", "close"], trigger_id="pr-narrow-wh")
        assert (m_only["status"], m_only["delivery_mode"], m_only["webhook_source_id"]) == ("ok", "webhook", "gh")
        assert (await _trig(tool, action="delete", trigger_id="pr-narrow-wh"))["status"] == "ok"

        act_poll = await _watch(tool, repo="org/owned", number=2, events=["activity"], trigger_id="pr-act-poll")
        assert (act_poll["status"], act_poll["delivery_mode"], act_poll["webhook_source_id"]) == ("ok", "poll", None)
        assert (await _trig(tool, action="delete", trigger_id="pr-act-poll"))["status"] == "ok"

        ci_poll = await _watch(tool, repo="org/owned", number=3, events=["ci"], trigger_id="pr-ci-poll")
        assert (ci_poll["status"], ci_poll["delivery_mode"], ci_poll["webhook_source_id"]) == ("ok", "poll", None)

        cp = _cp(store, "pr-ci-poll").model_copy(update={"pr_state": "closed", "updated_at": time.time()})
        store.update_github_pr_checkpoint_and_enqueue("pr-ci-poll", cp, disable_reason="pr_closed")
        assert (await _trig(tool, action="watch_room", trigger_id="room-1", prompt="Watch"))["status"] == "ok"
        rev1 = await _trig(tool, action="set_enabled", trigger_id="pr-ci-poll", enabled=True)
        assert (rev1["status"], "reactive trigger limit" in rev1["message"]) == ("error", True)
        rev2 = await _trig(tool, action="update", trigger_id="pr-ci-poll", enabled=True)
        assert (rev2["status"], "reactive trigger limit" in rev2["message"]) == ("error", True)

    np_cfg, np_paths = _config(tmp_path / "np", event_sources={"gh": narrow_src}, github_pr_polling_enabled=False)
    with tool_runtime_context(_tool_ctx(np_cfg, np_paths)):
        assert (await _watch(tool, repo="org/owned", number=1, events=["activity"]))["status"] == "error"
        assert (await _watch(tool, repo="org/owned", number=1, events=["ci"]))["status"] == "error"
        assert (await _watch(tool, repo="org/owned", number=1, events=["merge"]))["delivery_mode"] == "webhook"

    full_src = _gh_source(["org/owned"])
    f_cfg, f_paths = _config(tmp_path / "full", event_sources={"gh": full_src}, github_pr_polling_enabled=False)
    f_store = ReactiveTriggerStore(f_paths)
    with tool_runtime_context(_tool_ctx(f_cfg, f_paths)):
        all_wh = await _watch(tool, repo="org/owned", number=1, trigger_id="pr-all")
        assert (all_wh["status"], all_wh["delivery_mode"], all_wh["webhook_source_id"]) == ("ok", "webhook", "gh")
    assert _deliver_gh(full_src, f_cfg, f_paths, f_store, "issue_comment", "org/owned", "d-ic1", 2050.0) == (
        ["pr-all"],
        0,
        True,
    )


@pytest.mark.asyncio
async def test_poll_only_rows_do_not_starve_webhook_watches_when_polling_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When github_pr_polling_enabled=False, >50 due poll-only watches do not starve a nudged webhook watch."""
    fake = _FakeGitHub()
    fake.seed_open("org/owned", 7, tag="wh7")
    fake.set_json("/repos/org/owned/issues/7/timeline?per_page=100&page=1", [_cmt(70, "ping!")], etag='"wh7t-2"')
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path, event_sources={"gh": _gh_source(["org/owned"])}, github_pr_polling_enabled=False)
    store = ReactiveTriggerStore(paths)
    for idx in range(55):
        rec = ReactiveTriggerRecord(
            trigger_id=f"poll-{idx:02d}",
            owner_user_id=_OWNER,
            source=GitHubPRSourceSpec(repo="ext/repo", number=idx + 1, events=["activity"]),
            target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
            created_at=100.0,
            updated_at=100.0,
        )
        cp = GitHubPRCheckpoint(head_sha="sha1", seeded=True, next_poll_at=100.0 + idx, updated_at=100.0)
        store.create_trigger(rec, initial_github_pr_checkpoint=cp)

    wh_rec = ReactiveTriggerRecord(
        trigger_id="wh-target",
        owner_user_id=_OWNER,
        source=GitHubPRSourceSpec(repo="org/owned", number=7, events=["activity"], webhook_source_id="gh"),
        target=TriggerTargetSpec(agent="dev", room_id=_ROOM, prompt="Watch"),
        created_at=100.0,
        updated_at=100.0,
    )
    wh_cp = GitHubPRCheckpoint(head_sha="sha1", seeded=True, next_poll_at=999.0, updated_at=100.0)
    store.create_trigger(wh_rec, initial_github_pr_checkpoint=wh_cp)

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    assert await poller.poll_due_once(now=1000.0) == 1
    assert [f.event.summary for f in store.list_firings(trigger_id="wh-target")] == ["Comment by alice: ping!"]
    assert all("/ext/repo/" not in p for p, _, _ in fake.calls)


@pytest.mark.asyncio
async def test_review_comment_updated_cursor_draft_id_pagination_cap_and_full_timeline_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review comments handle out-of-order draft IDs and 3-page caps via updated_at, and 100-item timeline seeds advance."""
    fake = _FakeGitHub()
    tl = "/repos/ext/repo/issues/60/timeline?per_page=100&page="
    rc_base = "/repos/ext/repo/pulls/60/comments?sort=updated"
    fake.set_json("/repos/ext/repo/pulls/60", _pr("ext/repo", 60), etag='"pr60"')
    fake.set_json(f"{tl}1", [_cmt(i, f"seed-{i}") for i in range(1, 101)], etag='"tl1-full"')
    fake.set_json(
        f"{rc_base}&direction=desc&per_page=100",
        [_rc(500, None, "a.py", "baseline", "bob", "2026-11-25T10:00:00Z")],
        etag='"rc-seed"',
    )
    http = _patch_github(monkeypatch, fake)

    cfg, paths = _config(tmp_path)
    store = ReactiveTriggerStore(paths)
    with tool_runtime_context(_tool_ctx(cfg, paths)):
        assert (await _watch(TriggersTools(), repo="ext/repo", number=60, events=["activity"], trigger_id="pr-60"))[
            "status"
        ] == "ok"

    cp0 = _cp(store, "pr-60")
    assert (cp0.state["timeline_page"], cp0.state["timeline_etag"]) == (2, None)
    assert (cp0.state["review_comments_since"], cp0.state["review_comments_etag"]) == ("2026-11-25T10:00:00Z", None)

    fake.set_json(f"{tl}2", [_cmt(101, "on page 2", "carol")], etag='"tl2"')
    s0 = f"{rc_base}&direction=asc&per_page=100&since=2026-11-25T10:00:00Z"
    fake.set_json(
        s0,
        [
            _rc(500, None, "a.py", "baseline", "bob", "2026-11-25T10:00:00Z"),
            _rc(400, None, "draft.py", "published draft", "alice", "2026-11-25T10:01:00Z"),
        ],
        etag='"rc-p1"',
        next_path=f"{s0}&page=2",
    )
    fake.set_json(
        f"{s0}&page=2",
        [_rc(502, None, "b.py", "p2", "alice", "2026-11-25T10:02:00Z")],
        next_path=f"{s0}&page=3",
    )
    fake.set_json(
        f"{s0}&page=3",
        [_rc(503, None, "c.py", "p3", "alice", "2026-11-25T10:03:00Z")],
        next_path=f"{s0}&page=4",
    )
    fake.set_json(f"{s0}&page=4", [_rc(504, None, "d.py", "p4", "alice", "2026-11-25T10:04:00Z")])

    poller = GitHubPRPoller(store=store, config_provider=lambda: cfg, runtime_paths=paths, client=http)
    t0 = time.time() + 1_000
    assert await poller.poll_due_once(now=t0) == 1
    items1 = store.list_firings(trigger_id="pr-60")[0].event.data["items"]
    assert [it["id"] for it in items1] == [101, 400, 502, 503]
    cp1 = _cp(store, "pr-60")
    assert (cp1.state["review_comments_since"], cp1.state["review_comments_etag"]) == ("2026-11-25T10:03:00Z", None)

    s3 = f"{rc_base}&direction=asc&per_page=100&since=2026-11-25T10:03:00Z"
    fake.set_json(
        s3,
        [
            _rc(503, None, "c.py", "p3", "alice", "2026-11-25T10:03:00Z"),
            _rc(504, None, "d.py", "p4", "alice", "2026-11-25T10:04:00Z"),
        ],
        etag='"rc-s3"',
    )
    assert await poller.poll_due_once(now=t0 + 10_000) == 1
    items2 = store.list_firings(trigger_id="pr-60")[0].event.data["items"]
    assert [it["id"] for it in items2] == [504]
    cp2 = _cp(store, "pr-60")
    assert (cp2.state["review_comments_since"], cp2.state["review_comments_etag"]) == ("2026-11-25T10:04:00Z", None)

    s4 = f"{rc_base}&direction=asc&per_page=100&since=2026-11-25T10:04:00Z"
    fake.set_json(s4, [_rc(504, None, "d.py", "p4", "alice", "2026-11-25T10:04:00Z")], etag='"rc-s4"')
    assert await poller.poll_due_once(now=t0 + 20_000) == 0
    assert _cp(store, "pr-60").state["review_comments_etag"] == '"rc-s4"'
    fake.calls.clear()
    assert await poller.poll_due_once(now=t0 + 30_000) == 0
    assert any(p == s4 and inm == '"rc-s4"' for p, inm, _ in fake.calls)
