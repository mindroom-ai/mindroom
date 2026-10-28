"""Tests for the `slack` toolkit's read functions in personal and project mode."""

# ruff: noqa: D102, D103

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest

from mindroom import constants
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.connection_agents import resolve_connection_agent, resolve_connection_user
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import slack as slack_toolkit
from mindroom.custom_tools.slack import SlackTools
from mindroom.mcp_gateway import tools as gateway
from mindroom.slack import app as slack_app
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ALICE = "@alice:example.org"
BOT_TOKEN = "xoxb-bot-token"  # noqa: S105
USER_TOKEN = "xoxp-alice-token"  # noqa: S105
CHANNEL = "C0123ABCD"
UNLISTED = "C0999ZZZZ"
ROOT_TS = "1714000000.000100"
REPLY_TS = "1714000100.000200"
FILE_ID = "F0123ABCD"
WORKSPACE_URL = "https://example.slack.com/"
AUTH_TEST = {"ok": True, "url": WORKSPACE_URL, "team_id": "T0123ABCD", "user_id": "U0BOT0000"}
DANA = {"id": "U0123ABCD", "name": "dana", "real_name": "Dana Example", "profile": {"display_name": "Dana"}}

type Answer = dict[str, Any] | Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeSlack:
    """Answer Slack Web API methods by name, and file downloads by URL, recording every request."""

    answers: dict[str, Answer] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        key = request.url.path.removeprefix("/api/") if request.url.host == "slack.com" else str(request.url)
        answer = self.answers.get(key, {"ok": False, "error": "unknown_method"})
        return httpx.Response(200, json=answer) if isinstance(answer, dict) else answer(request)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
        monkeypatch.setattr(
            slack_toolkit,
            "_client_factory",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle), follow_redirects=False),
        )
        return self

    def methods(self) -> list[str]:
        return [request.url.path.removeprefix("/api/") for request in self.requests]

    def calls(self, method: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == f"/api/{method}"]


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def _bearer(request: httpx.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


@pytest.fixture(autouse=True)
def _empty_identity_cache() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    yield
    slack_app._IDENTITIES.clear()


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.com"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("slack_oauth_client", {"client_id": "slack-client", "client_secret": "slack-secret"})
    manager.save_credentials("slack_app", {"bot_token": BOT_TOKEN, "signing_secret": "signing-secret"})
    return runtime_paths


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=ALICE,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )
    return resolve_worker_target("user", "assistant", execution_identity=identity)


def _connect_alice(paths: RuntimePaths) -> None:
    publish_oauth_credentials(
        slack_oauth_provider(),
        {
            "token": USER_TOKEN,
            "client_id": "slack-client",
            "scopes": ["search:read", "channels:history"],
            "_source": "oauth",
            "_oauth_provider": "slack",
            "_oauth_claims": {"sub": "U0ALICE00", "team_id": "T0123ABCD"},
            "_oauth_claims_verified": True,
        },
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=_worker_target(),
    )


def _tools(paths: RuntimePaths, **overrides: Any) -> SlackTools:  # noqa: ANN401
    return SlackTools(
        runtime_paths=paths,
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=_worker_target(),
        **overrides,
    )


def _member(channel_id: str = CHANNEL, **flags: bool) -> dict[str, Any]:
    return {"ok": True, "channel": {"id": channel_id, "name": "launch-eng", "is_member": True, **flags}}


@pytest.mark.asyncio
async def test_personal_mode_uses_requester_token(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    message = {"type": "message", "ts": ROOT_TS, "user": DANA["id"], "text": "beta is blocked", "reply_count": 2}
    fake = FakeSlack(
        {
            "conversations.history": {"ok": True, "messages": [message], "response_metadata": {"next_cursor": "c2"}},
            "users.info": {"ok": True, "user": DANA},
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read(CHANNEL, limit=10))

    assert result["status"] == "ok"
    assert result["next_cursor"] == "c2"
    assert result["messages"] == [
        {
            "channel_id": CHANNEL,
            "ts": ROOT_TS,
            "thread_ts": None,
            "user_id": DANA["id"],
            "user_name": "Dana",
            "text": "beta is blocked",
            "permalink": f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000000000100",
            "files": [],
            "reply_count": 2,
        },
    ]
    assert _form(fake.calls("conversations.history")[0]) == {"channel": CHANNEL, "limit": "10"}
    assert {method: _bearer(request) for method, request in zip(fake.methods(), fake.requests, strict=True)} == {
        "conversations.history": USER_TOKEN,
        "users.info": USER_TOKEN,
        "auth.test": BOT_TOKEN,
    }


@pytest.mark.asyncio
async def test_personal_mode_without_connection_returns_connect_link(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_search("launch"))

    assert result["status"] == "error"
    assert result["oauth_connection_required"] is True
    assert result["provider"] == "slack"
    assert result["connect_url"].startswith("https://mindroom.example.com/")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_personal_mode_without_slack_app_is_not_configured(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    get_runtime_credentials_manager(paths).delete_credentials("slack_app")
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read(CHANNEL))

    assert result == {"status": "error", "tool": "slack", "message": "The MindRoom Slack app is not configured"}
    assert fake.requests == []


@pytest.mark.asyncio
async def test_project_mode_refuses_unlisted_channel_without_api_call(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack({"conversations.info": _member(UNLISTED), "auth.test": AUTH_TEST}).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_read(f"{UNLISTED}:{ROOT_TS}"))

    assert result["message"] == "Not one of this agent's Slack channels"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_project_mode_requires_bot_membership(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSlack({"conversations.info": _member(is_member=False)}).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_read(CHANNEL))

    assert result["message"] == f"Invite the MindRoom app to <#{CHANNEL}>"
    assert fake.methods() == ["conversations.info"]
    assert _bearer(fake.requests[0]) == BOT_TOKEN


@pytest.mark.asyncio
async def test_project_mode_refuses_group_dm_even_if_listed(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_dm = "G0123ABCD"
    fake = FakeSlack({"conversations.info": _member(group_dm, is_mpim=True)}).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[group_dm]).slack_read(group_dm))

    assert result["message"] == "Not one of this agent's Slack channels"
    assert fake.methods() == ["conversations.info"]


@pytest.mark.asyncio
async def test_project_download_requires_allowlisted_share(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/plan.pdf"
    file_info = {
        "ok": True,
        "file": {
            "id": FILE_ID,
            "name": "plan.pdf",
            "size": 3,
            "url_private_download": download_url,
            "shares": {"public": {UNLISTED: [{"ts": ROOT_TS}]}},
        },
    }
    fake = FakeSlack({"files.info": file_info, download_url: lambda _request: httpx.Response(200)}).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tools = _tools(paths, channels=[CHANNEL], tool_output_workspace_root=workspace)
    result = json.loads(await tools.slack_download(FILE_ID))

    assert result["message"] == "This file is not in one of this agent's Slack channels"
    assert fake.methods() == ["files.info"]
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_project_download_from_a_member_channel_uses_the_bot_token(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/plan.pdf"
    file_info = {
        "ok": True,
        "file": {
            "id": FILE_ID,
            "name": "plan.pdf",
            "size": 4,
            "url_private_download": download_url,
            "shares": {"public": {UNLISTED: [{"ts": ROOT_TS}], CHANNEL: [{"ts": ROOT_TS}]}},
        },
    }
    fake = FakeSlack(
        {
            "files.info": file_info,
            "conversations.info": _member(),
            download_url: lambda _request: httpx.Response(200, content=b"plan"),
        },
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tools = _tools(paths, channels=[CHANNEL], tool_output_workspace_root=workspace)
    result = json.loads(await tools.slack_download(FILE_ID))

    saved = workspace / "slack-downloads" / f"{FILE_ID}-plan.pdf"
    assert result["path"] == str(saved)
    assert saved.read_bytes() == b"plan"
    assert [str(request.url) for request in fake.requests] == [
        "https://slack.com/api/files.info",
        "https://slack.com/api/conversations.info",
        download_url,
    ]
    assert _form(fake.calls("conversations.info")[0]) == {"channel": CHANNEL}
    assert {_bearer(request) for request in fake.requests} == {BOT_TOKEN}


@pytest.mark.asyncio
async def test_download_stops_once_the_stream_passes_the_cap(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/big.bin"
    # Slack reports a size within the cap, so only the streamed bytes can catch the oversized body.
    file_info = {
        "ok": True,
        "file": {"id": FILE_ID, "name": "big.bin", "size": 4, "url_private_download": download_url},
    }
    FakeSlack(
        {"files.info": file_info, download_url: lambda _request: httpx.Response(200, content=b"x" * 9)},
    ).install(monkeypatch)
    monkeypatch.setattr(slack_toolkit, "_MAX_DOWNLOAD_BYTES", 8)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(await _tools(paths, tool_output_workspace_root=workspace).slack_download(FILE_ID))

    assert result["message"] == "The file is larger than 8 bytes"
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_download_link_off_slack_is_refused_without_the_token(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    elsewhere = f"https://files.example.com/{FILE_ID}/x"
    file_info = {"ok": True, "file": {"id": FILE_ID, "name": "x", "size": 1, "url_private_download": elsewhere}}
    fake = FakeSlack(
        {"files.info": file_info, elsewhere: lambda _request: httpx.Response(200, content=b"x")},
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(await _tools(paths, tool_output_workspace_root=workspace).slack_download(FILE_ID))

    assert result["message"] == "Slack returned no download link for this file"
    assert [request.url.host for request in fake.requests] == ["slack.com"]
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_read_thread_from_permalink(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    replies = [
        {"ts": ROOT_TS, "thread_ts": ROOT_TS, "user": DANA["id"], "text": "beta?", "reply_count": 1},
        {
            "ts": REPLY_TS,
            "thread_ts": ROOT_TS,
            "username": "deploy-bot",
            "text": "blocked on review",
            "files": [{"id": FILE_ID, "name": "log.txt", "size": 12, "url_private": "https://files.slack.com/x"}],
        },
    ]
    fake = FakeSlack(
        {
            "conversations.replies": {"ok": True, "messages": replies},
            "users.info": {"ok": True, "user": DANA},
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"

    result = json.loads(await _tools(paths).slack_read(permalink))

    assert _form(fake.calls("conversations.replies")[0]) == {"channel": CHANNEL, "ts": ROOT_TS, "limit": "50"}
    assert result["thread_ts"] == ROOT_TS
    assert [(message["user_name"], message["permalink"]) for message in result["messages"]] == [
        ("Dana", f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000000000100"),
        ("deploy-bot", permalink),
    ]
    assert result["messages"][1]["files"] == [{"id": FILE_ID, "name": "log.txt", "size": 12}]


@pytest.mark.asyncio
async def test_search_people_by_email_uses_lookup(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    dana = {**DANA, "profile": {**DANA["profile"], "email": "dana@example.com"}}
    fake = FakeSlack({"users.lookupByEmail": {"ok": True, "user": dana}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_search("dana@example.com", kind="people"))

    assert fake.methods() == ["users.lookupByEmail"]
    assert _form(fake.requests[0]) == {"email": "dana@example.com"}
    assert result["people"] == [
        {
            "id": DANA["id"],
            "user_name": "Dana",
            "real_name": "Dana Example",
            "email": "dana@example.com",
            "is_bot": False,
            "deleted": False,
        },
    ]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_search_people_by_name_stops_after_five_pages(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)

    def users_list(request: httpx.Request) -> httpx.Response:
        page = int(_form(request).get("cursor", "0"))
        members = [DANA] if page == 2 else [{"id": f"U0OTHER0{page}", "name": "someone"}]
        return httpx.Response(
            200,
            json={"ok": True, "members": members, "response_metadata": {"next_cursor": str(page + 1)}},
        )

    fake = FakeSlack({"users.list": users_list}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_search("DANA", kind="people"))

    assert [_form(request).get("limit") for request in fake.requests] == ["200"] * 5
    assert [person["id"] for person in result["people"]] == [DANA["id"]]
    assert result["complete"] is False
    assert "next_cursor" not in result


@pytest.mark.asyncio
async def test_download_stays_in_workspace(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/x"
    file_info = {
        "ok": True,
        "file": {"id": FILE_ID, "name": "../../x", "size": 5, "url_private_download": download_url},
    }
    fake = FakeSlack(
        {"files.info": file_info, download_url: lambda _request: httpx.Response(200, content=b"hello")},
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(await _tools(paths, tool_output_workspace_root=workspace).slack_download(FILE_ID))

    saved = workspace / "slack-downloads" / f"{FILE_ID}-.._.._x"
    assert result["path"] == str(saved)
    assert saved.read_bytes() == b"hello"
    assert [path.name for path in workspace.rglob("*")] == ["slack-downloads", saved.name]
    assert not (tmp_path / "x").exists()
    assert _bearer(fake.requests[-1]) == USER_TOKEN


@pytest.mark.asyncio
async def test_project_search_messages_refused(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL])

    for kind in ("messages", "people"):
        result = json.loads(await tools.slack_search("launch", kind=kind))
        assert result["message"] == "Message and people search need a personal Slack connection"
    assert fake.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "message"),
    [("30", "Slack rate limit, retry after 30s"), ("soon", "Slack rate limit, retry later")],
)
async def test_rate_limit_reports_retry_after(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
    message: str,
) -> None:
    _connect_alice(paths)
    FakeSlack(
        {"search.messages": lambda _request: httpx.Response(429, headers={"Retry-After": retry_after})},
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_search("launch"))

    assert result["message"] == message


@pytest.mark.asyncio
async def test_mcp_gateway_lists_slack(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_CONNECTIONS_AGENT": "mind",
            "MATRIX_HOMESERVER": "https://example.org",
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.com",
        },
    )
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "tools": ["slack"],
                    "private": {"per": "user"},
                    "access": {"users": [ALICE]},
                },
            },
        },
    )
    snapshot = ApiSnapshot(
        generation=1,
        runtime_paths=runtime_paths,
        config_data=config.model_dump(),
        runtime_config=config,
    )
    context = resolve_connection_agent(resolve_connection_user(snapshot, ALICE), "mind")

    result = await gateway.search_tools(context, toolkit="slack")
    schema = await gateway.get_tool(context, toolkit="slack", function="slack_read")

    assert "error" not in result
    assert [item["function"] for item in result["results"]] == ["slack_search", "slack_read", "slack_download"]
    assert "error" not in schema
    assert set(schema["inputSchema"]["properties"]) >= {"target", "limit", "before", "cursor"}


def test_slack_tools_require_a_credentials_manager(paths: RuntimePaths) -> None:
    with pytest.raises(RuntimeError, match="credentials_manager"):
        SlackTools(runtime_paths=paths, credentials_manager=None)
