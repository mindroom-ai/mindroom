"""Tests for the `slack` toolkit's read functions in personal and project mode."""

# ruff: noqa: D102, D103

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import nio
import pytest

from mindroom import constants
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.connection_agents import AgentToolContext, resolve_connection_agent, resolve_connection_user
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import slack as slack_toolkit
from mindroom.custom_tools.slack import SlackTools
from mindroom.mcp_gateway import tools as gateway
from mindroom.message_target import MessageTarget
from mindroom.slack import app as slack_app
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.slack.watches import SlackWatch, SlackWatchStore, watch_id_for
from mindroom.tool_system.agno_toolkits import adapt_toolkit, with_offered_instructions
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ALICE = "@alice:example.org"
ALICE_BRIDGE = "@alice-bridge:example.org"
BOB = "@bob:example.org"
ROOM = "!room:example.org"
AGENT = "@mindroom_assistant:example.org"
BOT_TOKEN = "xoxb-bot-token"  # noqa: S105
APP_TOKEN = "xapp-app-token"  # noqa: S105
USER_TOKEN = "xoxp-alice-token"  # noqa: S105
CHANNEL = "C0123ABCD"
UNLISTED = "C0999ZZZZ"
ROOT_TS = "1714000000.000100"
REPLY_TS = "1714000100.000200"
FILE_ID = "F0123ABCD"
WORKSPACE_URL = "https://example.slack.com/"
TIMEOUT = "timed out"
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
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.com", "MATRIX_HOMESERVER": "https://example.org"},
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
        room_id=ROOM,
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


def _turn_context(
    paths: RuntimePaths,
    hook_source: str | None,
    client: AsyncMock | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="assistant",
        target=MessageTarget.resolve(ROOM, None, "$event", room_mode=True),
        requester_id=ALICE,
        client=client or AsyncMock(),
        config=Config.model_validate(
            {
                "models": {"default": {"provider": "ollama", "id": "test-model"}},
                "agents": {"assistant": {"display_name": "Assistant"}},
                "authorization": {"aliases": {ALICE: [ALICE_BRIDGE]}},
            },
        ),
        runtime_paths=paths,
        conversation_reader=MagicMock(),
        relations=MagicMock(),
        hook_source=hook_source,
    )


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


def _gateway_context(tmp_path: Path) -> AgentToolContext:
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
    return resolve_connection_agent(resolve_connection_user(snapshot, ALICE), "mind")


@pytest.mark.asyncio
async def test_mcp_gateway_lists_slack(tmp_path: Path) -> None:
    context = _gateway_context(tmp_path)

    result = await gateway.search_tools(context, toolkit="slack")
    schema = await gateway.get_tool(context, toolkit="slack", function="slack_read")

    assert "error" not in result
    assert [item["function"] for item in result["results"]] == [
        "slack_search",
        "slack_read",
        "slack_download",
        "slack_post",
        "slack_react",
    ]
    assert "error" not in schema
    assert set(schema["inputSchema"]["properties"]) >= {"target", "limit", "before", "cursor"}


@pytest.mark.asyncio
async def test_watch_not_registered_without_room(tmp_path: Path) -> None:
    context = _gateway_context(tmp_path)

    listed = await gateway.search_tools(context, toolkit="slack")
    schema = await gateway.get_tool(context, toolkit="slack", function="slack_watch")

    assert "error" not in listed
    assert "slack_watch" not in {item["function"] for item in listed["results"]}
    assert schema == {
        "error": {"code": "tool_not_found", "message": "This tool is not assigned to the selected agent."},
    }


def test_slack_tools_require_a_credentials_manager(paths: RuntimePaths) -> None:
    with pytest.raises(RuntimeError, match="credentials_manager"):
        SlackTools(runtime_paths=paths, credentials_manager=None)


def _write_names(tools: SlackTools) -> set[str]:
    return set(tools.get_async_functions()) & {"slack_post", "slack_react"}


def test_write_false_hides_write_functions(paths: RuntimePaths) -> None:
    assert _write_names(_tools(paths)) == {"slack_post", "slack_react"}
    assert _write_names(_tools(paths, write=False)) == set()
    assert set(_tools(paths, write=False).get_async_functions()) == {"slack_search", "slack_read", "slack_download"}


def test_instructions_name_the_mirrored_reply_only_for_a_project_agent_offered_slack_post(paths: RuntimePaths) -> None:
    offered = ["slack_search", "slack_read", "slack_post", "slack_react"]
    project = _tools(paths, channels=[CHANNEL], room="lobby")

    assert "do not call slack_post" in project.instructions_for(offered)
    assert (
        "do not call slack_post"
        in with_offered_instructions(
            project,
            adapt_toolkit(project),
            authored_instructions=False,
        ).instructions
    )
    assert project.instructions_for([name for name in offered if name != "slack_post"]) == ""
    assert _tools(paths, channels=[CHANNEL]).instructions_for(offered) == ""
    assert _tools(paths, room="lobby").instructions_for(offered) == ""


@pytest.mark.asyncio
async def test_post_reply_from_permalink_uses_thread_root(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": REPLY_TS}}).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"

    result = json.loads(await _tools(paths).slack_post(permalink, "I will review it tomorrow"))

    assert result == {"status": "ok", "tool": "slack", "channel_id": CHANNEL, "ts": REPLY_TS, "thread_ts": ROOT_TS}
    assert fake.methods() == ["chat.postMessage"]
    assert _form(fake.requests[0]) == {"channel": CHANNEL, "text": "I will review it tomorrow", "thread_ts": ROOT_TS}
    assert _bearer(fake.requests[0]) == USER_TOKEN


@pytest.mark.asyncio
async def test_post_to_a_conversation_starts_a_top_level_message(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_post(CHANNEL, "hello"))

    assert result["thread_ts"] is None
    assert result["ts"] == ROOT_TS
    assert _form(fake.requests[0]) == {"channel": CHANNEL, "text": "hello"}


@pytest.mark.asyncio
async def test_post_to_user_opens_dm_personal_only(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    dm = "D0123ABCD"
    fake = FakeSlack(
        {
            "conversations.open": {"ok": True, "channel": {"id": dm}},
            "chat.postMessage": {"ok": True, "channel": dm, "ts": ROOT_TS},
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_post(DANA["id"], "ping"))

    assert result["channel_id"] == dm
    assert fake.methods() == ["conversations.open", "chat.postMessage"]
    assert _form(fake.requests[0]) == {"users": DANA["id"]}
    assert _form(fake.requests[1]) == {"channel": dm, "text": "ping"}
    assert {_bearer(request) for request in fake.requests} == {USER_TOKEN}

    refusing = FakeSlack().install(monkeypatch)
    refused = json.loads(await _tools(paths, channels=[CHANNEL]).slack_post(DANA["id"], "ping"))

    assert refused["message"] == "Project agents post only in their channels"
    assert refusing.requests == []


@pytest.mark.asyncio
async def test_project_post_uses_the_bot_token_in_a_member_channel(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack(
        {"conversations.info": _member(), "chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": REPLY_TS}},
    ).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_post(f"{CHANNEL}:{ROOT_TS}", "on it"))

    assert result["thread_ts"] == ROOT_TS
    assert fake.methods() == ["conversations.info", "chat.postMessage"]
    assert _form(fake.requests[1]) == {"channel": CHANNEL, "text": "on it", "thread_ts": ROOT_TS}
    assert {_bearer(request) for request in fake.requests} == {BOT_TOKEN}


@pytest.mark.asyncio
async def test_project_post_outside_allowlist_refused(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSlack({"conversations.info": _member(UNLISTED)}).install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL])

    post = json.loads(await tools.slack_post(UNLISTED, "hello"))
    react = json.loads(await tools.slack_react(f"{UNLISTED}:{ROOT_TS}", "eyes"))

    assert post["message"] == react["message"] == "Not one of this agent's Slack channels"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_write_error_is_returned_with_the_slack_code_and_never_retried(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": False, "error": "is_archived"}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_post(CHANNEL, "hello"))

    assert result["message"] == "Slack refused the request: is_archived"
    assert fake.methods() == ["chat.postMessage"]


@pytest.mark.asyncio
async def test_react_adds_the_named_reaction_to_the_message(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"reactions.add": {"ok": True}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_react(f"{CHANNEL}:{ROOT_TS}", ":white_check_mark:"))

    assert result == {
        "status": "ok",
        "tool": "slack",
        "channel_id": CHANNEL,
        "ts": ROOT_TS,
        "emoji": "white_check_mark",
    }
    assert _form(fake.requests[0]) == {"channel": CHANNEL, "timestamp": ROOT_TS, "name": "white_check_mark"}
    assert _bearer(fake.requests[0]) == USER_TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [CHANNEL, DANA["id"]])
async def test_react_requires_message_target(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_react(target, "eyes"))

    assert result["message"] == "Use <conversation>:<ts> or a message link to react to a message"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_permalink_to_reply_posts_under_root_reacts_on_message(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(
        {
            "chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": "1714000200.000300"},
            "reactions.add": {"ok": True},
        },
    ).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"
    tools = _tools(paths)

    await tools.slack_post(permalink, "thanks")
    await tools.slack_react(permalink, "eyes")

    assert _form(fake.calls("chat.postMessage")[0])["thread_ts"] == ROOT_TS
    assert _form(fake.calls("reactions.add")[0]) == {"channel": CHANNEL, "timestamp": REPLY_TS, "name": "eyes"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["not a target", "https://example.com/archives/C0123ABCD/p1714000000000100"])
async def test_write_target_must_be_a_slack_target(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths)

    for result in (await tools.slack_post(target, "hello"), await tools.slack_react(target, "eyes")):
        assert (
            json.loads(result)["message"]
            == "Use a Slack conversation ID, <conversation>:<ts>, a message link, or a user ID"
        )
    assert fake.requests == []


# One call per write function; a write function without an entry fails `test_every_write_function_has_a_watch_guard_case`.
_WRITE_CALLS: dict[str, Callable[[SlackTools], Awaitable[str]]] = {
    "slack_post": lambda tools: tools.slack_post(CHANNEL, "hello"),
    "slack_react": lambda tools: tools.slack_react(f"{CHANNEL}:{ROOT_TS}", "eyes"),
}


def test_every_write_function_has_a_watch_guard_case() -> None:
    assert set(_WRITE_CALLS) == slack_toolkit._WRITE_FUNCTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_source", ["slack-watch/w-1", "slack-watch/"])
async def test_writes_refused_during_watch_turn(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    hook_source: str,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(
        {"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS}, "reactions.add": {"ok": True}},
    ).install(monkeypatch)
    tools = _tools(paths)
    writes = [_WRITE_CALLS[name] for name in sorted(slack_toolkit._WRITE_FUNCTIONS)]

    with tool_runtime_context(_turn_context(paths, hook_source)):
        refusals = [json.loads(await write(tools))["message"] for write in writes]

    assert refusals == ["Ask me in a new message to post to Slack"] * len(writes)
    assert fake.requests == []

    for context in (None, _turn_context(paths, None), _turn_context(paths, "plugin:message_received")):
        with tool_runtime_context(context):
            assert [json.loads(await write(tools))["status"] for write in writes] == ["ok"] * len(writes)
    assert fake.methods() == ["chat.postMessage", "reactions.add"] * 3


@pytest.mark.asyncio
async def test_watch_turn_refusal_comes_before_credentials_are_read(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack().install(monkeypatch)

    with tool_runtime_context(_turn_context(paths, "slack-watch/w-1")):
        result = json.loads(await _tools(paths).slack_post(CHANNEL, "hello"))

    assert result["message"] == "Ask me in a new message to post to Slack"
    assert "oauth_connection_required" not in result
    assert fake.requests == []


@pytest.mark.asyncio
async def test_unanswered_write_warns_it_may_have_been_sent_and_a_read_does_not(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TIMEOUT, request=request)

    fake = FakeSlack(
        {"chat.postMessage": timed_out, "reactions.add": timed_out, "conversations.history": timed_out},
    ).install(monkeypatch)
    tools = _tools(paths)

    results = [
        json.loads(await write(tools))["message"] for write in (_WRITE_CALLS[name] for name in sorted(_WRITE_CALLS))
    ]
    read = json.loads(await tools.slack_read(CHANNEL))["message"]

    assert (
        results
        == ["Slack did not answer; the message may have been sent, so check with slack_read before retrying"] * 2
    )
    assert read == "Slack could not be reached; try again"
    assert fake.methods() == ["chat.postMessage", "reactions.add", "conversations.history"]


def _save_app_token(paths: RuntimePaths, **app: str) -> None:
    get_runtime_credentials_manager(paths).save_credentials(
        "slack_app",
        {"bot_token": BOT_TOKEN, "signing_secret": "signing-secret", "app_token": APP_TOKEN, **app},
    )


def _watch_tools(paths: RuntimePaths) -> SlackTools:
    return _tools(paths, current_room_id=ROOM)


def _members(*user_ids: str) -> AsyncMock:
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, None, None) for user_id in user_ids],
        ROOM,
    )
    return client


async def _call_watch(
    tools: SlackTools,
    paths: RuntimePaths,
    *,
    members: tuple[str, ...] = (ALICE, AGENT),
    requester_id: str = ALICE,
    **arguments: Any,  # noqa: ANN401
) -> dict[str, Any]:
    context = replace(_turn_context(paths, None, _members(*members)), requester_id=requester_id)
    with tool_runtime_context(context):
        return json.loads(await tools.slack_watch(**arguments))


def _stored_watches(paths: RuntimePaths) -> tuple[SlackWatch, ...]:
    assert paths.control_state_root is not None
    return SlackWatchStore(paths.control_state_root).all()


ALICE_WATCH = SlackWatch(
    watch_id=watch_id_for(ALICE, "assistant", ROOM),
    owner_id=ALICE,
    agent_name="assistant",
    room_id=ROOM,
    team_id="T0123ABCD",
    slack_user_id="U0ALICE00",
    mentions=True,
    direct_messages=True,
    channels=(),
    created_at=1_800_000_000,
)
WATCHING = "Watching your Slack mentions and direct messages; they'll arrive here."


def test_watch_is_registered_only_in_personal_mode_with_a_room(paths: RuntimePaths) -> None:
    assert "slack_watch" in _watch_tools(paths).get_async_functions()
    assert "slack_watch" not in _tools(paths).get_async_functions()
    assert "slack_watch" not in _tools(paths, current_room_id=None).get_async_functions()
    # A watch never writes to Slack, so turning writes off keeps it.
    assert "slack_watch" in _tools(paths, current_room_id=ROOM, write=False).get_async_functions()


def test_watch_not_in_project_mode(paths: RuntimePaths) -> None:
    project = _tools(paths, channels=[CHANNEL], room="lobby", current_room_id=ROOM)

    assert "slack_watch" not in project.get_async_functions()


@pytest.mark.asyncio
async def test_start_watches_the_connected_slack_account(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, action="start")

    assert result == {"status": "ok", "tool": "slack", "message": WATCHING}
    (watch,) = _stored_watches(paths)
    assert replace(watch, created_at=ALICE_WATCH.created_at) == ALICE_WATCH
    assert watch.created_at > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "message"),
    [
        (
            {"mentions": True, "direct_messages": False},
            "Watching your Slack mentions; they'll arrive here.",
        ),
        (
            {"mentions": False, "direct_messages": True},
            "Watching your Slack direct messages; they'll arrive here.",
        ),
        (
            {"channels": [CHANNEL, f"{UNLISTED}:{ROOT_TS}", CHANNEL]},
            f"{WATCHING} Channel messages count only from {CHANNEL}, {UNLISTED}.",
        ),
    ],
)
async def test_start_words_follow_the_filters(paths: RuntimePaths, filters: dict[str, Any], message: str) -> None:
    _connect_alice(paths)
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, action="start", **filters)

    assert result["message"] == message
    (watch,) = _stored_watches(paths)
    assert (watch.mentions, watch.direct_messages, watch.channels) == (
        filters.get("mentions", True),
        filters.get("direct_messages", True),
        (CHANNEL, UNLISTED) if "channels" in filters else (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"mentions": False, "direct_messages": False}, "Watch mentions, direct messages, or both"),
        ({"channels": ["U0123ABCD"]}, "Use a Slack conversation ID, <conversation>:<ts>, or a message link"),
        ({"action": "pause"}, "action must be start, stop, or status"),
    ],
)
async def test_start_refuses_filters_that_watch_nothing_or_name_no_channel(
    paths: RuntimePaths,
    arguments: dict[str, Any],
    message: str,
) -> None:
    _connect_alice(paths)
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, **{"action": "start", **arguments})

    assert result["message"] == message
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_start_replaces_existing(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)
    await _call_watch(tools, paths, action="start")

    await _call_watch(tools, paths, action="start", mentions=False, channels=[CHANNEL])

    (watch,) = _stored_watches(paths)
    assert (watch.mentions, watch.direct_messages, watch.channels) == (False, True, (CHANNEL,))


@pytest.mark.asyncio
async def test_stop_removes(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)
    await _call_watch(tools, paths, action="start")

    stopped = await _call_watch(tools, paths, action="stop")
    again = await _call_watch(tools, paths, action="stop")

    assert stopped["message"] == "Stopped watching your Slack in this room"
    assert again["message"] == "You were not watching Slack in this room"
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_stop_needs_neither_a_slack_connection_nor_an_app_token(paths: RuntimePaths) -> None:
    assert paths.control_state_root is not None
    SlackWatchStore(paths.control_state_root).put(ALICE_WATCH)

    result = await _call_watch(_watch_tools(paths), paths, action="stop")

    assert result["message"] == "Stopped watching your Slack in this room"
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_status_reports_the_filters_and_no_problem(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)

    assert await _call_watch(tools, paths) == {
        "status": "ok",
        "tool": "slack",
        "watching": False,
        "message": "You are not watching Slack in this room",
    }

    await _call_watch(tools, paths, action="start", direct_messages=False, channels=[CHANNEL])
    result = await _call_watch(tools, paths, action="status")

    assert result == {
        "status": "ok",
        "tool": "slack",
        "watching": True,
        "mentions": True,
        "direct_messages": False,
        "channels": [CHANNEL],
        "problem": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("members", "problem"),
    [
        ((ALICE, AGENT, BOB), "paused: another person is in this room"),
        ((AGENT,), "paused: you are not in this room"),
        ((ALICE, AGENT), None),
    ],
)
async def test_status_reports_pause_reason(
    paths: RuntimePaths,
    members: tuple[str, ...],
    problem: str | None,
) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)
    await _call_watch(tools, paths, action="start")

    result = await _call_watch(tools, paths, members=members, action="status")

    assert result["problem"] == problem


@pytest.mark.asyncio
async def test_status_reports_a_member_list_that_cannot_be_read(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)
    await _call_watch(tools, paths, action="start")
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersError("boom")

    with tool_runtime_context(_turn_context(paths, None, client)):
        result = json.loads(await tools.slack_watch(action="status"))

    assert result["problem"] == "paused: room members could not be checked"


@pytest.mark.asyncio
async def test_status_reports_that_slack_is_not_connected(paths: RuntimePaths) -> None:
    assert paths.control_state_root is not None
    SlackWatchStore(paths.control_state_root).put(ALICE_WATCH)

    result = await _call_watch(_watch_tools(paths), paths, action="status")

    assert result["watching"] is True
    assert result["problem"] == "paused: Slack is not connected"
    assert "oauth_connection_required" not in result


@pytest.mark.asyncio
async def test_status_reports_that_slack_was_reconnected_as_another_account(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    assert paths.control_state_root is not None
    SlackWatchStore(paths.control_state_root).put(replace(ALICE_WATCH, slack_user_id="U0OTHER00"))

    result = await _call_watch(_watch_tools(paths), paths, action="status")

    assert result["problem"] == "paused: Slack account changed; start the watch again"


@pytest.mark.asyncio
async def test_start_refused_when_other_human_joined(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, members=(ALICE, AGENT, BOB), action="start")

    assert result["status"] == "error"
    assert result["message"] == "Cannot watch Slack here: another person is in this room"
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_start_refused_without_app_token(paths: RuntimePaths) -> None:
    _connect_alice(paths)

    result = await _call_watch(_watch_tools(paths), paths, action="start")

    assert result["status"] == "error"
    assert result["message"] == "The MindRoom Slack app needs an app-level token for watches"
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_start_refused_without_the_slack_app(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    get_runtime_credentials_manager(paths).delete_credentials("slack_app")

    result = await _call_watch(_watch_tools(paths), paths, action="start")

    assert result["message"] == "The MindRoom Slack app is not configured"


@pytest.mark.asyncio
async def test_start_without_a_slack_connection_returns_the_connect_link(paths: RuntimePaths) -> None:
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, action="start")

    assert result["oauth_connection_required"] is True
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_start_needs_the_verified_slack_account_of_the_connection(paths: RuntimePaths) -> None:
    publish_oauth_credentials(
        slack_oauth_provider(),
        {"token": USER_TOKEN, "client_id": "slack-client", "scopes": ["search:read"], "_source": "oauth"},
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=_worker_target(),
    )
    _save_app_token(paths)

    result = await _call_watch(_watch_tools(paths), paths, action="start")

    assert result["oauth_connection_required"] is True
    assert _stored_watches(paths) == ()


@pytest.mark.asyncio
async def test_a_bridge_alias_owns_the_watch_as_its_person(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)

    await _call_watch(tools, paths, requester_id=ALICE_BRIDGE, action="start")

    (watch,) = _stored_watches(paths)
    assert (watch.owner_id, watch.watch_id) == (ALICE, ALICE_WATCH.watch_id)


@pytest.mark.asyncio
async def test_a_person_only_stops_and_reads_their_own_watch(paths: RuntimePaths) -> None:
    assert paths.control_state_root is not None
    bob_watch = replace(ALICE_WATCH, watch_id=watch_id_for(BOB, "assistant", ROOM), owner_id=BOB)
    SlackWatchStore(paths.control_state_root).put(bob_watch)
    tools = _watch_tools(paths)

    status = await _call_watch(tools, paths, action="status")
    stopped = await _call_watch(tools, paths, action="stop")

    assert status["watching"] is False
    assert stopped["message"] == "You were not watching Slack in this room"
    assert _stored_watches(paths) == (bob_watch,)


@pytest.mark.asyncio
async def test_a_watch_is_one_per_agent_and_room(paths: RuntimePaths) -> None:
    _connect_alice(paths)
    _save_app_token(paths)
    tools = _watch_tools(paths)
    await _call_watch(tools, paths, action="start")
    elsewhere = replace(ALICE_WATCH, watch_id=watch_id_for(ALICE, "assistant", "!other:example.org"))
    assert paths.control_state_root is not None
    SlackWatchStore(paths.control_state_root).put(elsewhere)

    await _call_watch(tools, paths, action="stop")

    assert _stored_watches(paths) == (elsewhere,)


@pytest.mark.asyncio
async def test_watches_need_a_matrix_conversation(paths: RuntimePaths) -> None:
    result = json.loads(await _watch_tools(paths).slack_watch(action="status"))

    assert result["message"] == "Slack watches work only in a Matrix conversation"


@pytest.mark.asyncio
async def test_watches_need_the_control_state(paths: RuntimePaths) -> None:
    tools = SlackTools(
        runtime_paths=replace(paths, control_state_root=None),
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=_worker_target(),
        current_room_id=ROOM,
    )

    result = await _call_watch(tools, paths, action="status")

    assert result["message"] == "Slack watches need the primary runtime's control state"
