"""Tests for the `slack` toolkit's read functions in personal and project mode."""

# ruff: noqa: D102, D103

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from mindroom import constants
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.connection_agents import AgentToolContext, resolve_connection_agent, resolve_connection_user
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import slack as slack_toolkit
from mindroom.custom_tools.slack import SlackTools
from mindroom.mcp_gateway import tools as gateway
from mindroom.message_target import MessageTarget
from mindroom.slack import files as slack_files
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.toolkits import adapt_toolkit, with_offered_instructions
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.agent_tool_helpers import toolkit_offers
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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
AUTH_TEST = {"ok": True, "url": WORKSPACE_URL, "team_id": "T0123ABCD", "user_id": "U0BOT0000", "user": "mindroom"}
DANA = {"id": "U0123ABCD", "name": "dana", "real_name": "Dana Example", "profile": {"display_name": "Dana"}}
UPLOAD_URL = "https://files.slack.com/upload/v1/CwABAAAAXwoAAZnrUWIRYH9X"
UPLOAD_FILE_ID = "F0UPLOAD00"
UPLOAD_ANSWERS: dict[str, Answer] = {
    "files.getUploadURLExternal": {"ok": True, "upload_url": UPLOAD_URL, "file_id": UPLOAD_FILE_ID},
    UPLOAD_URL: lambda _request: httpx.Response(200, text="OK - 5"),
    "files.completeUploadExternal": {"ok": True, "files": [{"id": UPLOAD_FILE_ID, "title": "notes.txt"}]},
}

type Answer = dict[str, Any] | Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeSlack:
    """Answer Slack Web API methods by name, and file downloads by URL, recording every request."""

    answers: dict[str, Answer] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    @staticmethod
    def _key(request: httpx.Request) -> str:
        return request.url.path.removeprefix("/api/") if request.url.host == "slack.com" else str(request.url)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        answer = self.answers.get(self._key(request), {"ok": False, "error": "unknown_method"})
        return httpx.Response(200, json=answer) if isinstance(answer, dict) else answer(request)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
        monkeypatch.setattr(
            slack_toolkit,
            "slack_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle), follow_redirects=False),
        )
        return self

    def methods(self) -> list[str]:
        """Return each request's Slack method, or its full URL for a request off the Web API."""
        return [self._key(request) for request in self.requests]

    def calls(self, method: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == f"/api/{method}"]


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def _bearer(request: httpx.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


@pytest.fixture
def notes_workspace(tmp_path: Path) -> Path:
    """Return an agent workspace holding `notes.txt`, five bytes long."""
    root = tmp_path / "notes-workspace"
    root.mkdir()
    (root / "notes.txt").write_bytes(b"hello")
    return root


def slack_runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return runtime paths whose credential store holds the Slack app and its OAuth client."""
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.com", "MATRIX_HOMESERVER": "https://example.org"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("slack_oauth_client", {"client_id": "slack-client", "client_secret": "slack-secret"})
    manager.save_credentials("slack_app", {"bot_token": BOT_TOKEN, "signing_secret": "signing-secret"})
    return runtime_paths


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return slack_runtime_paths(tmp_path)


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
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {"assistant": {"display_name": "Assistant"}},
            "authorization": {"aliases": {ALICE: [ALICE_BRIDGE]}},
        },
    )
    return ToolRuntimeContext(
        agent_name="assistant",
        target=MessageTarget.resolve(ROOM, None, "$event", room_mode=True),
        requester_id=ALICE,
        client=client or AsyncMock(),
        config=config,
        runtime_paths=paths,
        plugins=config_plugins(config, paths),
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

    result = json.loads(await _tools(paths).slack_read("read", target=CHANNEL, limit=10))

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
@pytest.mark.parametrize(
    ("auth_test", "error"),
    [
        ({"ok": False, "error": "invalid_auth"}, "The MindRoom Slack app is not configured"),
        (lambda _request: httpx.Response(503), "Slack could not be reached; try again"),
    ],
    ids=["token-rejected", "slack-server-error"],
)
async def test_read_names_why_the_bot_lookup_failed(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    auth_test: Answer,
    error: str,
) -> None:
    """A failed lookup of the bot's workspace reads as a missing app only when Slack rejected the bot token."""
    _connect_alice(paths)
    message = {"type": "message", "ts": ROOT_TS, "user": DANA["id"], "text": "beta is blocked"}
    FakeSlack(
        {
            "conversations.history": {"ok": True, "messages": [message]},
            "users.info": {"ok": True, "user": DANA},
            "auth.test": auth_test,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("read", target=CHANNEL))

    assert result == {"status": "error", "tool": "slack", "message": error}


@pytest.mark.asyncio
async def test_personal_mode_without_connection_returns_connect_link(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("search", query="launch"))

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

    result = json.loads(await _tools(paths).slack_read("read", target=CHANNEL))

    assert result == {"status": "error", "tool": "slack", "message": "The MindRoom Slack app is not configured"}
    assert fake.requests == []


@pytest.mark.asyncio
async def test_project_mode_refuses_unlisted_channel_without_api_call(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSlack({"conversations.info": _member(UNLISTED), "auth.test": AUTH_TEST}).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_read("read", target=f"{UNLISTED}:{ROOT_TS}"))

    assert result["message"] == "Not one of this agent's Slack channels"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_project_mode_requires_bot_membership(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSlack({"conversations.info": _member(is_member=False)}).install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_read("read", target=CHANNEL))

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

    result = json.loads(await _tools(paths, channels=[group_dm]).slack_read("read", target=group_dm))

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
    result = json.loads(await tools.slack_read("download", file_id=FILE_ID))

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
    result = json.loads(await tools.slack_read("download", file_id=FILE_ID))

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
    assert fake.requests[-1].headers["accept-encoding"] == "identity"


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
    monkeypatch.setattr(slack_files, "MAX_FILE_BYTES", 8)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

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

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

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

    result = json.loads(await _tools(paths).slack_read("read", target=permalink))

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

    result = json.loads(await _tools(paths).slack_read("search", query="dana@example.com", kind="people"))

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

    result = json.loads(await _tools(paths).slack_read("search", query="DANA", kind="people"))

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

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

    saved = workspace / "slack-downloads" / f"{FILE_ID}-.._.._x"
    assert result["path"] == str(saved)
    assert saved.read_bytes() == b"hello"
    assert [path.name for path in workspace.rglob("*")] == ["slack-downloads", saved.name]
    assert not (tmp_path / "x").exists()
    assert _bearer(fake.requests[-1]) == USER_TOKEN


@pytest.mark.parametrize(
    ("mimetype", "size", "response"),
    [
        ("application/pdf", None, httpx.Response(200, html="<html>Sign in</html>")),
        ("application/pdf", 4, httpx.Response(200, content=b"plan and more")),
    ],
    ids=["an HTML page for a PDF", "a size other than Slack's"],
)
@pytest.mark.asyncio
async def test_download_refuses_a_page_instead_of_the_file(
    mimetype: str,
    size: int | None,
    response: httpx.Response,
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/plan.pdf"
    file = {"id": FILE_ID, "name": "plan.pdf", "mimetype": mimetype, "url_private_download": download_url}
    if size is not None:
        file["size"] = size
    FakeSlack({"files.info": {"ok": True, "file": file}, download_url: lambda _request: response}).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

    assert result["message"] == "Slack returned a page instead of the file; reconnect Slack and try again"
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_download_keeps_an_html_file(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/page.html"
    page = "<html>notes</html>"
    file = {"id": FILE_ID, "name": "page.html", "mimetype": "text/html", "size": len(page), "url_private": download_url}
    FakeSlack(
        {"files.info": {"ok": True, "file": file}, download_url: lambda _request: httpx.Response(200, html=page)},
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

    assert (workspace / "slack-downloads" / f"{FILE_ID}-page.html").read_text() == page
    assert result["file"]["size"] == len(page)


_DOCUMENT_REFUSAL = "Slack canvases and posts can't be downloaded as files; open them in Slack"
_DOCUMENT_KINDS = pytest.mark.parametrize(
    ("mimetype", "filetype"),
    [
        ("application/vnd.slack-docs", "quip"),
        ("application/vnd.slack-docs", None),
        ("text/plain", "canvas"),
        ("text/plain", "space"),
        ("text/plain", "post"),
    ],
    ids=[
        "canvas by mimetype and quip",
        "by mimetype",
        "canvas by filetype",
        "space by filetype",
        "legacy post by filetype",
    ],
)


def _document(mimetype: str, filetype: str | None, download_url: str) -> dict[str, object]:
    file: dict[str, object] = {
        "id": FILE_ID,
        "name": "Launch plan",
        "mimetype": mimetype,
        "size": 7,
        "url_private_download": download_url,
    }
    if filetype is not None:
        file["filetype"] = filetype
    return file


@_DOCUMENT_KINDS
@pytest.mark.asyncio
async def test_download_refuses_a_slack_document_without_fetching_it(
    mimetype: str,
    filetype: str | None,
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canvas or post has no file to download, so no request reaches the file host and nothing is written."""
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/canvas"
    fake = FakeSlack(
        {
            "files.info": {"ok": True, "file": _document(mimetype, filetype, download_url)},
            download_url: lambda _request: httpx.Response(200, html="<html>Sign in</html>"),
        },
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

    assert result["message"] == _DOCUMENT_REFUSAL
    assert fake.methods() == ["files.info"]
    assert not any(request.url.host == "files.slack.com" for request in fake.requests)
    assert list(workspace.iterdir()) == []


@_DOCUMENT_KINDS
@pytest.mark.asyncio
async def test_download_of_a_slack_document_leaves_an_earlier_download_untouched(
    mimetype: str,
    filetype: str | None,
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    download_url = f"https://files.slack.com/files-pri/T0123ABCD-{FILE_ID}/canvas"
    FakeSlack(
        {
            "files.info": {"ok": True, "file": _document(mimetype, filetype, download_url)},
            download_url: lambda _request: httpx.Response(200, html="<html>Sign in</html>"),
        },
    ).install(monkeypatch)
    workspace = tmp_path / "workspace"
    earlier = workspace / "slack-downloads" / f"{FILE_ID}-Launch plan"
    earlier.parent.mkdir(parents=True)
    earlier.write_text("<html>Launch plan</html>")

    result = json.loads(
        await _tools(paths, tool_output_workspace_root=workspace).slack_read("download", file_id=FILE_ID),
    )

    assert result["message"] == _DOCUMENT_REFUSAL
    assert earlier.read_text() == "<html>Launch plan</html>"
    assert [path.name for path in earlier.parent.iterdir()] == [earlier.name]


@pytest.mark.asyncio
async def test_project_search_messages_refused(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL])

    for kind in ("messages", "people"):
        result = json.loads(await tools.slack_read("search", query="launch", kind=kind))
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

    result = json.loads(await _tools(paths).slack_read("search", query="launch"))

    assert result["message"] == message


def _ago(seconds: int) -> str:
    """Return the ts of a message sent this many seconds ago."""
    return f"{int(time.time()) - seconds}.000100"


def _latest(messages: dict[str, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer `conversations.history` with each conversation's one latest message, or none."""

    def history(request: httpx.Request) -> httpx.Response:
        message = messages.get(_form(request)["channel"])
        return httpx.Response(200, json={"ok": True, "messages": [message] if message else []})

    return history


DM_DANA = {"id": "D0123ABCD", "is_im": True, "user": DANA["id"]}
DM_OLD = {"id": "D0456EFGH", "is_im": True, "user": "U0OLD0000"}
GROUP_DM = {"id": "G0123ABCD", "is_mpim": True, "name": "mpdm-alice--dana--sam-1"}


@pytest.mark.asyncio
async def test_recent_lists_direct_messages_newest_first_with_the_other_persons_name(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    dana_ts, group_ts = _ago(3_600), _ago(60)
    long_text = "x" * 250
    fake = FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": [DM_DANA, DM_OLD, GROUP_DM]},
            "conversations.history": _latest(
                {
                    DM_DANA["id"]: {"ts": dana_ts, "user": DANA["id"], "text": "are you free at 3?"},
                    # Older than the default 30 days, so it is left out.
                    DM_OLD["id"]: {"ts": _ago(31 * 86_400), "user": "U0OLD0000", "text": "old news"},
                    GROUP_DM["id"]: {"ts": group_ts, "thread_ts": ROOT_TS, "user": DANA["id"], "text": long_text},
                },
            ),
            "users.info": {"ok": True, "user": DANA},
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent"))

    assert result == {
        "status": "ok",
        "tool": "slack",
        "kind": "dms",
        "conversations": [
            {
                "channel_id": GROUP_DM["id"],
                "kind": "group_dm",
                "name": "mpdm-alice--dana--sam-1",
                "latest_ts": group_ts,
                "latest_permalink": (
                    f"{WORKSPACE_URL}archives/{GROUP_DM['id']}/p{group_ts.replace('.', '')}"
                    f"?thread_ts={ROOT_TS}&cid={GROUP_DM['id']}"
                ),
                "latest_text": "x" * 200,
            },
            {
                "channel_id": DM_DANA["id"],
                "kind": "dm",
                "name": "Dana",
                "latest_ts": dana_ts,
                "latest_permalink": f"{WORKSPACE_URL}archives/{DM_DANA['id']}/p{dana_ts.replace('.', '')}",
                "latest_text": "are you free at 3?",
            },
        ],
        "complete": True,
    }
    assert _form(fake.calls("users.conversations")[0]) == {
        "types": "im,mpim",
        "exclude_archived": "true",
        "limit": "200",
    }
    assert [_form(request) for request in fake.calls("conversations.history")] == [
        {"channel": conversation["id"], "limit": "1"} for conversation in (DM_DANA, DM_OLD, GROUP_DM)
    ]
    # Only the partner of a direct message that is listed is looked up.
    assert [_form(request) for request in fake.calls("users.info")] == [{"user": DANA["id"]}]
    assert {_bearer(request) for request in fake.requests if request.url.path != "/api/auth.test"} == {USER_TOKEN}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("days", "listed"),
    [(1, ["C0000000B"]), (3, ["C0000000B", "C0000000A"]), (None, ["C0000000B", "C0000000A", "C0000000C"])],
)
async def test_recent_keeps_conversations_active_within_the_window(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    days: int | None,
    listed: list[str],
) -> None:
    _connect_alice(paths)
    channels = [
        {"id": channel_id, "name": channel_id.lower()} for channel_id in ("C0000000A", "C0000000B", "C0000000C")
    ]
    fake = FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": channels},
            "conversations.history": _latest(
                {
                    "C0000000A": {"ts": _ago(2 * 86_400), "text": "a"},
                    "C0000000B": {"ts": _ago(30 * 60), "text": "b"},
                    # Inside the default 30 days.
                    "C0000000C": {"ts": _ago(29 * 86_400), "text": "c"},
                },
            ),
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    window = {} if days is None else {"days": days}
    result = json.loads(await _tools(paths).slack_read("recent", kind="channels", **window))

    assert [conversation["channel_id"] for conversation in result["conversations"]] == listed
    assert {conversation["kind"] for conversation in result["conversations"]} == {"channel"}
    assert _form(fake.calls("users.conversations")[0])["types"] == "public_channel,private_channel"


@pytest.mark.asyncio
async def test_recent_returns_the_newest_limit_conversations(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    channels = [{"id": f"C000000{index}A", "name": f"c{index}"} for index in range(5)]
    FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": channels},
            "conversations.history": _latest(
                {channel["id"]: {"ts": _ago(60 * (index + 1)), "text": "hi"} for index, channel in enumerate(channels)},
            ),
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent", kind="channels", limit=2))

    assert [conversation["name"] for conversation in result["conversations"]] == ["c0", "c1"]
    assert result["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, -1, 366])
async def test_recent_refuses_a_window_it_cannot_read(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    days: int,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent", days=days))

    assert result["message"] == "days must be an integer from 1 to 365"
    assert fake.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101])
async def test_listings_refuse_a_limit_outside_one_to_a_hundred(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths)

    for result in (
        await tools.slack_read("search", query="launch", limit=limit),
        await tools.slack_read("recent", limit=limit),
    ):
        assert json.loads(result)["message"] == "limit must be an integer from 1 to 100"
    assert fake.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("slack_read", {"action": "recent", "days": 1.5}),
        ("slack_read", {"action": "recent", "kind": "groups"}),
        ("slack_read", {"action": "recent", "limit": "many"}),
        ("slack_read", {"action": "search", "query": "launch", "kind": "files"}),
        ("slack_read", {"action": "search", "query": "launch", "limit": 2.5}),
        ("slack_read", {"action": "bogus"}),
        ("slack_send", {"action": "bogus", "target": CHANNEL}),
        ("slack_manage", {"action": "bogus", "target": f"{CHANNEL}:{ROOT_TS}"}),
    ],
)
async def test_malformed_arguments_fail_call_validation_before_any_request(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
) -> None:
    """Arguments that disagree with a function's signature fail call validation before its body runs."""
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)

    with pytest.raises(ValidationError):
        await toolkit_offers(_tools(paths), name).entrypoint(**arguments)

    assert fake.requests == []


def _paged_conversations(pages: dict[str | None, tuple[list[dict[str, Any]], str | None]]) -> Answer:
    """Answer `users.conversations` with the page each cursor names, and its next cursor."""

    def listing(request: httpx.Request) -> httpx.Response:
        channels, following = pages[_form(request).get("cursor")]
        metadata = {"next_cursor": following or ""}
        return httpx.Response(200, json={"ok": True, "channels": channels, "response_metadata": metadata})

    return listing


def _history_reads(fake: FakeSlack) -> list[str]:
    return [_form(request)["channel"] for request in fake.calls("conversations.history")]


@pytest.mark.asyncio
async def test_recent_continues_after_a_rate_limit_with_the_conversations_it_did_not_read(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 stops the reads not yet sent, never waits, and next_cursor resumes at the first unread conversation."""
    _connect_alice(paths)
    channels = [{"id": f"C000000{letter}A", "name": letter} for letter in "abcd"]
    limited = {"c": True}
    sent = _latest({channel["id"]: {"ts": _ago(60), "text": channel["name"]} for channel in channels})

    def history(request: httpx.Request) -> httpx.Response:
        if _form(request)["channel"] == "C000000cA" and limited.pop("c", False):
            return httpx.Response(429, headers={"Retry-After": "30"})
        return sent(request)

    monkeypatch.setattr(slack_toolkit, "_MAX_READS_IN_FLIGHT", 1)
    fake = FakeSlack(
        {
            # The rate limit falls on the second page, so resuming needs that page's cursor.
            "users.conversations": _paged_conversations(
                {None: (channels[:2], "page-2"), "page-2": (channels[2:], None)},
            ),
            "conversations.history": history,
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)
    tools = _tools(paths)

    first = json.loads(await tools.slack_read("recent", kind="channels"))

    assert sorted(conversation["name"] for conversation in first["conversations"]) == ["a", "b"]
    assert (first["complete"], first["skipped"], first["retry_after"]) == (False, 2, 30)
    assert _history_reads(fake) == ["C000000aA", "C000000bA", "C000000cA"]

    fake.requests.clear()
    second = json.loads(await tools.slack_read("recent", kind="channels", cursor=first["next_cursor"]))

    assert sorted(conversation["name"] for conversation in second["conversations"]) == ["c", "d"]
    assert second["complete"] is True
    assert "next_cursor" not in second
    assert [_form(request).get("cursor") for request in fake.calls("users.conversations")] == ["page-2"]
    assert _history_reads(fake) == ["C000000cA", "C000000dA"]


@pytest.mark.asyncio
async def test_recent_batches_never_overlap(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads that finish after a rate limit stopped the rest are made again in the next batch, never listed twice."""
    _connect_alice(paths)
    channels = [{"id": f"C{index:08d}", "name": f"c{index}"} for index in range(12)]
    limited = {channels[2]["id"]}

    async def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        if request.url.path == "/api/users.conversations":
            return httpx.Response(200, json={"ok": True, "channels": channels})
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json=AUTH_TEST)
        # Every read in flight is sent before any is answered, so reads after the limited one finish.
        await asyncio.sleep(0)
        channel_id = _form(request)["channel"]
        if channel_id in limited:
            limited.discard(channel_id)
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True, "messages": [{"ts": _ago(60), "text": channel_id}]})

    monkeypatch.setattr(
        slack_toolkit,
        "slack_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False),
    )
    tools = _tools(paths)

    first = json.loads(await tools.slack_read("recent", kind="channels", limit=100))
    second = json.loads(await tools.slack_read("recent", kind="channels", limit=100, cursor=first["next_cursor"]))

    first_ids = {entry["channel_id"] for entry in first["conversations"]}
    second_ids = {entry["channel_id"] for entry in second["conversations"]}
    assert not first_ids & second_ids
    assert first_ids | second_ids == {channel["id"] for channel in channels}
    assert first["skipped"] == len(channels) - len(first_ids)
    assert second["complete"] is True


@pytest.mark.asyncio
async def test_recent_stops_its_reads_when_slack_cannot_be_reached(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    channels = [{"id": f"C000000{letter}A", "name": letter} for letter in "abc"]
    sent = _latest({channel["id"]: {"ts": _ago(60), "text": channel["name"]} for channel in channels})

    def history(request: httpx.Request) -> httpx.Response:
        if _form(request)["channel"] == "C000000bA":
            raise httpx.ConnectError(TIMEOUT, request=request)
        return sent(request)

    monkeypatch.setattr(slack_toolkit, "_MAX_READS_IN_FLIGHT", 1)
    fake = FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": channels},
            "conversations.history": history,
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)
    tools = _tools(paths)

    result = json.loads(await tools.slack_read("recent", kind="channels"))

    assert [conversation["name"] for conversation in result["conversations"]] == ["a"]
    assert (result["complete"], result["skipped"], result["retry_after"]) == (False, 2, None)
    assert _history_reads(fake) == ["C000000aA", "C000000bA"]

    fake.answers["conversations.history"] = sent
    fake.requests.clear()
    resumed = json.loads(await tools.slack_read("recent", kind="channels", cursor=result["next_cursor"]))

    assert sorted(conversation["name"] for conversation in resumed["conversations"]) == ["b", "c"]
    assert _history_reads(fake) == ["C000000bA", "C000000cA"]


@pytest.mark.asyncio
async def test_recent_skips_a_conversation_slack_refuses_and_reads_the_rest(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    channels = [{"id": f"C000000{letter}A", "name": letter} for letter in "abc"]
    sent = _latest({channel["id"]: {"ts": _ago(60), "text": channel["name"]} for channel in channels})

    def history(request: httpx.Request) -> httpx.Response:
        if _form(request)["channel"] == "C000000bA":
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        return sent(request)

    monkeypatch.setattr(slack_toolkit, "_MAX_READS_IN_FLIGHT", 1)
    fake = FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": channels},
            "conversations.history": history,
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent", kind="channels"))

    assert sorted(conversation["name"] for conversation in result["conversations"]) == ["a", "c"]
    # Reading it again would meet the same refusal, so there is nothing to continue with.
    assert (result["complete"], result["skipped"], result["next_cursor"]) == (False, 1, None)
    assert _history_reads(fake) == ["C000000aA", "C000000bA", "C000000cA"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "expected"),
    [
        (
            {"ok": False, "error": "missing_scope", "needed": "mpim:history"},
            {"message": "Slack needs the mpim:history permission; reconnect Slack to grant it"},
        ),
        ({"ok": False, "error": "token_revoked"}, {"oauth_connection_required": True}),
    ],
)
async def test_recent_reports_a_missing_permission_or_a_revoked_connection(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    refusal: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    _connect_alice(paths)
    FakeSlack(
        {"users.conversations": {"ok": True, "channels": [DM_DANA, GROUP_DM]}, "conversations.history": refusal},
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent"))

    assert result["status"] == "error"
    assert {key: result.get(key) for key in expected} == expected


@pytest.mark.asyncio
async def test_recent_never_reads_a_direct_message_with_a_deleted_person(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    deleted = {"id": "D0999ZZZZ", "is_im": True, "user": "U0GONE000", "is_user_deleted": True}
    fake = FakeSlack(
        {
            "users.conversations": {"ok": True, "channels": [deleted, DM_DANA]},
            "conversations.history": _latest({DM_DANA["id"]: {"ts": _ago(60), "text": "hi"}}),
            "users.info": {"ok": True, "user": DANA},
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent"))

    assert [conversation["channel_id"] for conversation in result["conversations"]] == [DM_DANA["id"]]
    assert result["complete"] is True
    assert _history_reads(fake) == [DM_DANA["id"]]


@pytest.mark.asyncio
async def test_recent_continues_past_its_page_cap(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    channels = [{"id": f"C0000000{index}", "name": f"c{index}"} for index in range(7)]
    pages = {
        None if index == 0 else f"page-{index}": ([channel], f"page-{index + 1}" if index < 6 else None)
        for index, channel in enumerate(channels)
    }
    fake = FakeSlack(
        {
            "users.conversations": _paged_conversations(pages),
            "conversations.history": _latest({channel["id"]: {"ts": _ago(60), "text": "hi"} for channel in channels}),
            "auth.test": AUTH_TEST,
        },
    ).install(monkeypatch)
    tools = _tools(paths)

    first = json.loads(await tools.slack_read("recent", kind="channels", limit=100))

    assert len(fake.calls("users.conversations")) == 5
    assert (len(first["conversations"]), first["complete"], first["skipped"], first["retry_after"]) == (
        5,
        False,
        0,
        None,
    )

    fake.requests.clear()
    second = json.loads(await tools.slack_read("recent", kind="channels", limit=100, cursor=first["next_cursor"]))

    assert [_form(request).get("cursor") for request in fake.calls("users.conversations")] == ["page-5", "page-6"]
    assert sorted(conversation["name"] for conversation in second["conversations"]) == ["c5", "c6"]
    assert second["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", "page-2", "x:page-2", "-1:", "12345:"])
async def test_recent_refuses_a_cursor_it_did_not_give(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    cursor: str,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths).slack_read("recent", cursor=cursor))

    assert result["message"] == 'cursor must be the next_cursor of the previous slack_read(action="recent") call'
    assert fake.requests == []


@pytest.mark.asyncio
async def test_recent_reads_at_most_eight_conversations_at_once(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    channels = [{"id": f"C{index:08d}", "name": f"c{index}"} for index in range(20)]
    in_flight, peak = 0, 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        if request.url.path == "/api/users.conversations":
            return httpx.Response(200, json={"ok": True, "channels": channels})
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json=AUTH_TEST)
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={"ok": True, "messages": [{"ts": _ago(60), "text": "hi"}]})

    monkeypatch.setattr(
        slack_toolkit,
        "slack_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False),
    )

    result = json.loads(await _tools(paths).slack_read("recent", kind="channels", limit=100))

    assert len(result["conversations"]) == 20
    assert peak == 8


@pytest.mark.asyncio
async def test_project_recent_needs_a_personal_connection(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project agent lists its channels with `slack_search(kind="conversations")` instead."""
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL])

    for kind in ("channels", "dms"):
        result = json.loads(await tools.slack_read("recent", kind=kind))
        assert result["message"] == "Recent conversations need a personal Slack connection"
    assert fake.requests == []


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
        runtime_config=config,
    )
    return resolve_connection_agent(resolve_connection_user(snapshot, ALICE), "mind")


@pytest.mark.asyncio
async def test_mcp_gateway_lists_slack(tmp_path: Path) -> None:
    context = _gateway_context(tmp_path)

    # The gateway answers at most 10 results per search.
    result = await gateway.search_tools(context, toolkit="slack", limit=10)
    schema = await gateway.get_tool(context, toolkit="slack", function="slack_read")

    assert "error" not in result
    assert [item["function"] for item in result["results"]] == [
        "slack_read",
        "slack_send",
        "slack_manage",
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
    return set(tools.functions) & slack_toolkit._WRITE_FUNCTIONS


def test_write_false_hides_write_functions(paths: RuntimePaths) -> None:
    assert _write_names(_tools(paths)) == {"slack_send", "slack_manage"}
    assert _write_names(_tools(paths, write=False)) == set()
    assert set(_tools(paths, write=False).functions) == {"slack_read"}


def test_instructions_name_the_mirrored_reply_only_for_a_project_agent_offered_slack_send(paths: RuntimePaths) -> None:
    offered = ["slack_read", "slack_send", "slack_manage"]
    project = _tools(paths, channels=[CHANNEL], room="lobby")

    assert 'do not call slack_send(action="post", ...)' in project.instructions_for(offered)
    assert (
        'do not call slack_send(action="post", ...)'
        in with_offered_instructions(project, adapt_toolkit(project)).instructions
    )
    assert project.instructions_for([name for name in offered if name != "slack_send"]) == ""
    assert _tools(paths, channels=[CHANNEL]).instructions_for(offered) == ""
    assert _tools(paths, room="lobby").instructions_for(offered) == ""


@pytest.mark.asyncio
async def test_post_reply_from_permalink_uses_thread_root(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": REPLY_TS}}).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"

    result = json.loads(await _tools(paths).slack_send("post", permalink, text="I will review it tomorrow"))

    assert result == {"status": "ok", "tool": "slack", "channel_id": CHANNEL, "ts": REPLY_TS, "thread_ts": ROOT_TS}
    assert fake.methods() == ["chat.postMessage"]
    assert _form(fake.requests[0]) == {"channel": CHANNEL, "text": "I will review it tomorrow", "thread_ts": ROOT_TS}
    assert _bearer(fake.requests[0]) == USER_TOKEN


@pytest.mark.asyncio
async def test_post_cannot_notify_a_whole_channel(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Broadcast mentions in an agent's text reach Slack defused, so a post never notifies everyone."""
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS}}).install(monkeypatch)

    await _tools(paths).slack_send("post", CHANNEL, text="<!channel> <!here> <!everyone> <!subteam^S0123ABCD> hi")

    (sent,) = (_form(request) for request in fake.requests)
    assert all("<!" not in value for value in sent.values())
    assert sent["text"].endswith(" hi")
    assert sent["text"].count("!") == 4


@pytest.mark.asyncio
async def test_post_to_a_conversation_starts_a_top_level_message(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_send("post", CHANNEL, text="hello"))

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

    result = json.loads(await _tools(paths).slack_send("post", DANA["id"], text="ping"))

    assert result["channel_id"] == dm
    assert fake.methods() == ["conversations.open", "chat.postMessage"]
    assert _form(fake.requests[0]) == {"users": DANA["id"]}
    assert _form(fake.requests[1]) == {"channel": dm, "text": "ping"}
    assert {_bearer(request) for request in fake.requests} == {USER_TOKEN}

    refusing = FakeSlack().install(monkeypatch)
    refused = json.loads(await _tools(paths, channels=[CHANNEL]).slack_send("post", DANA["id"], text="ping"))

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

    result = json.loads(
        await _tools(paths, channels=[CHANNEL]).slack_send("post", f"{CHANNEL}:{ROOT_TS}", text="on it"),
    )

    assert result["thread_ts"] == ROOT_TS
    assert fake.methods() == ["conversations.info", "chat.postMessage"]
    assert _form(fake.requests[1]) == {"channel": CHANNEL, "text": "on it", "thread_ts": ROOT_TS}
    assert {_bearer(request) for request in fake.requests} == {BOT_TOKEN}


@pytest.mark.asyncio
async def test_project_post_outside_allowlist_refused(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    fake = FakeSlack({"conversations.info": _member(UNLISTED)}).install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL], tool_output_workspace_root=notes_workspace)

    results = [
        await tools.slack_send("post", UNLISTED, text="hello"),
        await tools.slack_manage("react", f"{UNLISTED}:{ROOT_TS}", emoji="eyes"),
        await tools.slack_manage("edit", f"{UNLISTED}:{ROOT_TS}", text="fixed"),
        await tools.slack_manage("delete", f"{UNLISTED}:{ROOT_TS}"),
        await tools.slack_send("upload", f"{UNLISTED}:{ROOT_TS}", path="notes.txt"),
    ]

    assert {json.loads(result)["message"] for result in results} == {"Not one of this agent's Slack channels"}
    assert fake.requests == []


@pytest.mark.asyncio
async def test_write_error_is_returned_with_the_slack_code_and_never_retried(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.postMessage": {"ok": False, "error": "is_archived"}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_send("post", CHANNEL, text="hello"))

    assert result["message"] == "Slack refused the request: is_archived"
    assert fake.methods() == ["chat.postMessage"]


@pytest.mark.asyncio
async def test_react_adds_the_named_reaction_to_the_message(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"reactions.add": {"ok": True}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_manage("react", f"{CHANNEL}:{ROOT_TS}", emoji=":white_check_mark:"))

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
async def test_message_functions_require_a_message_target(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)
    tools = _tools(paths)

    results = [
        await tools.slack_manage("react", target, emoji="eyes"),
        await tools.slack_manage("edit", target, text="fixed"),
        await tools.slack_manage("delete", target),
    ]

    assert [json.loads(result)["message"] for result in results] == [
        "Use <conversation>:<ts> or a message link to react to a message",
        "Use <conversation>:<ts> or a message link to edit a message",
        "Use <conversation>:<ts> or a message link to delete a message",
    ]
    assert fake.requests == []


@pytest.mark.asyncio
async def test_edit_replaces_the_linked_message_text_defused(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit names the linked reply itself, not its thread, and its new text cannot notify a whole channel."""
    _connect_alice(paths)
    fake = FakeSlack({"chat.update": {"ok": True, "channel": CHANNEL, "ts": REPLY_TS}}).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"

    result = json.loads(
        await _tools(paths).slack_manage("edit", permalink, text="<!here> <!subteam^S0123ABCD> beta ships Friday"),
    )

    assert result == {"status": "ok", "tool": "slack", "channel_id": CHANNEL, "ts": REPLY_TS}
    assert fake.methods() == ["chat.update"]
    (sent,) = (_form(request) for request in fake.requests)
    assert sent == {
        "channel": CHANNEL,
        "ts": REPLY_TS,
        "text": "<\u2060!here> <\u2060!subteam^S0123ABCD> beta ships Friday",
    }
    assert _bearer(fake.requests[0]) == USER_TOKEN


@pytest.mark.asyncio
async def test_delete_removes_the_named_message(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect_alice(paths)
    fake = FakeSlack({"chat.delete": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS}}).install(monkeypatch)

    result = json.loads(await _tools(paths).slack_manage("delete", f"{CHANNEL}:{ROOT_TS}"))

    assert result == {"status": "ok", "tool": "slack", "channel_id": CHANNEL, "ts": ROOT_TS}
    assert fake.methods() == ["chat.delete"]
    assert _form(fake.requests[0]) == {"channel": CHANNEL, "ts": ROOT_TS}
    assert _bearer(fake.requests[0]) == USER_TOKEN


@pytest.mark.asyncio
async def test_upload_shares_a_workspace_file_in_a_thread_and_never_sends_the_token_to_the_upload_link(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(dict(UPLOAD_ANSWERS)).install(monkeypatch)
    permalink = f"{WORKSPACE_URL}archives/{CHANNEL}/p1714000100000200?thread_ts={ROOT_TS}&cid={CHANNEL}"
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(
        await tools.slack_send("upload", permalink, path="notes.txt", comment="<!channel> notes from today"),
    )

    assert result == {
        "status": "ok",
        "tool": "slack",
        "channel_id": CHANNEL,
        "thread_ts": ROOT_TS,
        "file": {"id": UPLOAD_FILE_ID, "name": "notes.txt", "title": "notes.txt", "size": 5},
    }
    assert fake.methods() == ["files.getUploadURLExternal", UPLOAD_URL, "files.completeUploadExternal"]
    asked, uploaded, completed = fake.requests
    assert _form(asked) == {"filename": "notes.txt", "length": "5"}
    assert uploaded.method == "POST"
    assert uploaded.content == b"hello"
    assert "authorization" not in uploaded.headers
    assert _form(completed) == {
        "files": json.dumps([{"id": UPLOAD_FILE_ID, "title": "notes.txt"}]),
        "channel_id": CHANNEL,
        "thread_ts": ROOT_TS,
        "initial_comment": "<\u2060!channel> notes from today",
    }
    assert [_bearer(request) for request in (asked, completed)] == [USER_TOKEN, USER_TOKEN]


@pytest.mark.asyncio
async def test_upload_to_a_person_opens_a_direct_message_with_the_given_title(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    dm = "D0123ABCD"
    fake = FakeSlack({**UPLOAD_ANSWERS, "conversations.open": {"ok": True, "channel": {"id": dm}}}).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", DANA["id"], path="notes.txt", title="Standup notes"))

    assert (result["channel_id"], result["thread_ts"], result["file"]["title"]) == (dm, None, "Standup notes")
    assert fake.methods()[0] == "conversations.open"
    assert _form(fake.calls("files.completeUploadExternal")[0]) == {
        "files": json.dumps([{"id": UPLOAD_FILE_ID, "title": "Standup notes"}]),
        "channel_id": dm,
    }


@pytest.mark.asyncio
async def test_project_upload_uses_the_bot_token_in_a_member_channel_and_never_to_a_person(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    fake = FakeSlack({**UPLOAD_ANSWERS, "conversations.info": _member()}).install(monkeypatch)
    tools = _tools(paths, channels=[CHANNEL], tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", CHANNEL, path="notes.txt"))

    assert result["status"] == "ok"
    assert fake.methods() == [
        "conversations.info",
        "files.getUploadURLExternal",
        UPLOAD_URL,
        "files.completeUploadExternal",
    ]
    assert {_bearer(request) for request in fake.calls("conversations.info")} == {BOT_TOKEN}
    assert _bearer(fake.calls("files.completeUploadExternal")[0]) == BOT_TOKEN

    refusing = FakeSlack().install(monkeypatch)
    refused = json.loads(await tools.slack_send("upload", DANA["id"], path="notes.txt"))

    assert refused["message"] == "Project agents post only in their channels"
    assert refusing.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "message"),
    [
        (UNLISTED, "Not one of this agent's Slack channels"),
        (DANA["id"], "Project agents post only in their channels"),
    ],
)
async def test_project_upload_checks_its_channel_before_reading_the_file(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    message: str,
) -> None:
    """A project agent without a workspace still gets the channel refusal, so the file was never looked at."""
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await _tools(paths, channels=[CHANNEL]).slack_send("upload", target, path="missing.txt"))

    assert result["message"] == message
    assert fake.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upload_url",
    [
        "https://uploads.example.com/upload/v1/x",
        "http://files.slack.com/upload/v1/x",
        "https://files.slack.com.example.com/upload/v1/x",
    ],
)
async def test_upload_link_off_slack_is_refused_before_the_file_is_sent(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
    upload_url: str,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(
        {
            "files.getUploadURLExternal": {"ok": True, "upload_url": upload_url, "file_id": UPLOAD_FILE_ID},
            upload_url: lambda _request: httpx.Response(200),
        },
    ).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", CHANNEL, path="notes.txt"))

    assert result["message"] == "Slack returned an upload link outside slack.com, so the file was not sent"
    assert fake.methods() == ["files.getUploadURLExternal"]


@pytest.mark.asyncio
async def test_upload_the_link_refuses_shares_nothing(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack({**UPLOAD_ANSWERS, UPLOAD_URL: lambda _request: httpx.Response(500)}).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", CHANNEL, path="notes.txt"))

    assert result["message"] == "Slack refused the upload with HTTP 500"
    assert fake.methods() == ["files.getUploadURLExternal", UPLOAD_URL]


@pytest.mark.asyncio
async def test_upload_refuses_a_file_over_the_cap_before_any_request(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(dict(UPLOAD_ANSWERS)).install(monkeypatch)
    monkeypatch.setattr(slack_toolkit, "MAX_FILE_BYTES", 4)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", CHANNEL, path="notes.txt"))

    assert result["message"] == f"{(notes_workspace / 'notes.txt').resolve()} is 5 bytes; the limit is 4 bytes."
    assert fake.requests == []


@pytest.mark.asyncio
async def test_upload_refuses_a_path_outside_the_workspace(
    tmp_path: Path,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    (tmp_path / "secret.txt").write_text("secret")
    fake = FakeSlack(dict(UPLOAD_ANSWERS)).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    result = json.loads(await tools.slack_send("upload", CHANNEL, path="../secret.txt"))

    assert result["message"].startswith("path '../secret.txt' must be an existing file inside the agent workspace")
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

    await tools.slack_send("post", permalink, text="thanks")
    await tools.slack_manage("react", permalink, emoji="eyes")

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

    for result in (
        await tools.slack_send("post", target, text="hello"),
        await tools.slack_manage("react", target, emoji="eyes"),
        await tools.slack_manage("edit", target, text="fixed"),
        await tools.slack_manage("delete", target),
    ):
        assert (
            json.loads(result)["message"]
            == "Use a Slack conversation ID, <conversation>:<ts>, a message link, or a user ID"
        )
    assert fake.requests == []


# One call per write function and the Slack methods it calls; a write function without an entry fails
# `test_every_write_function_has_a_watch_guard_case`.
_WRITE_CALLS: tuple[tuple[str, Callable[[SlackTools], Awaitable[str]], tuple[str, ...]], ...] = (
    ("slack_send", lambda tools: tools.slack_send("post", CHANNEL, text="hello"), ("chat.postMessage",)),
    (
        "slack_manage",
        lambda tools: tools.slack_manage("react", f"{CHANNEL}:{ROOT_TS}", emoji="eyes"),
        ("reactions.add",),
    ),
    ("slack_manage", lambda tools: tools.slack_manage("edit", f"{CHANNEL}:{ROOT_TS}", text="fixed"), ("chat.update",)),
    ("slack_manage", lambda tools: tools.slack_manage("delete", f"{CHANNEL}:{ROOT_TS}"), ("chat.delete",)),
    (
        "slack_send",
        lambda tools: tools.slack_send("upload", CHANNEL, path="notes.txt"),
        ("files.getUploadURLExternal", UPLOAD_URL, "files.completeUploadExternal"),
    ),
)
_WRITE_ANSWERS: dict[str, Answer] = {
    "chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS},
    "reactions.add": {"ok": True},
    "chat.update": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS},
    "chat.delete": {"ok": True, "channel": CHANNEL, "ts": ROOT_TS},
    **UPLOAD_ANSWERS,
}


def test_every_write_function_has_a_watch_guard_case() -> None:
    assert {name for name, _, _ in _WRITE_CALLS} == slack_toolkit._WRITE_FUNCTIONS


@pytest.mark.asyncio
async def test_writes_refused_during_watch_turn(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)
    fake = FakeSlack(dict(_WRITE_ANSWERS)).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)
    writes = [(call, methods) for _, call, methods in _WRITE_CALLS]

    for hook_source in ("event_source:slack", "slack-watch/w-1", "slack-watch/"):
        with tool_runtime_context(_turn_context(paths, hook_source)):
            refusals = [json.loads(await write(tools))["message"] for write, _ in writes]
        assert refusals == ["Ask me in a new message to post to Slack"] * len(writes)
    assert fake.requests == []

    for context in (None, _turn_context(paths, None), _turn_context(paths, "plugin:message_received")):
        with tool_runtime_context(context):
            assert [json.loads(await write(tools))["status"] for write, _ in writes] == ["ok"] * len(writes)
    assert fake.methods() == [method for _, methods in writes for method in methods] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_source", ["event_source:slack", "slack-watch/w-1"])
async def test_watch_turn_refusal_comes_before_credentials_are_read(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    hook_source: str,
) -> None:
    fake = FakeSlack().install(monkeypatch)

    with tool_runtime_context(_turn_context(paths, hook_source)):
        result = json.loads(await _tools(paths).slack_send("post", CHANNEL, text="hello"))

    assert result["message"] == "Ask me in a new message to post to Slack"
    assert "oauth_connection_required" not in result
    assert fake.requests == []


@pytest.mark.asyncio
async def test_unanswered_write_warns_it_may_have_been_sent_and_a_read_does_not(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    notes_workspace: Path,
) -> None:
    _connect_alice(paths)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TIMEOUT, request=request)

    writes = [(call, methods) for _, call, methods in _WRITE_CALLS]
    first_methods = [methods[0] for _, methods in writes]
    fake = FakeSlack(dict.fromkeys([*first_methods, "conversations.history"], timed_out)).install(monkeypatch)
    tools = _tools(paths, tool_output_workspace_root=notes_workspace)

    results = [json.loads(await write(tools))["message"] for write, _ in writes]
    read = json.loads(await tools.slack_read("read", target=CHANNEL))["message"]

    assert results == [
        'Slack did not answer; the message may have been sent, so check with slack_read(action="read") before retrying',
    ] * len(writes)
    assert read == "Slack could not be reached; try again"
    assert fake.methods() == [*first_methods, "conversations.history"]


def test_slack_meta_schema_opt_outs_and_project_mode_narrowing(paths: RuntimePaths) -> None:
    """slack_read opts into meta-schemas (and hides recent/days in project mode); write tools opt out."""
    personal = {fn.name: fn for fn in adapt_toolkit(_tools(paths)).functions}
    assert personal["slack_read"].offer_background_after is True
    assert personal["slack_read"].offer_output_path is True
    assert personal["slack_read"].parameters["properties"]["action"]["enum"] == ["search", "recent", "read", "download"]
    assert "days" in personal["slack_read"].parameters["properties"]
    for write_name in ("slack_send", "slack_manage"):
        assert personal[write_name].offer_background_after is False
        assert personal[write_name].offer_output_path is False

    project = {fn.name: fn for fn in adapt_toolkit(_tools(paths, channels=[CHANNEL])).functions}
    read_fn = project["slack_read"]
    assert read_fn.offer_background_after is True
    assert read_fn.offer_output_path is True
    assert read_fn.parameters["properties"]["action"]["enum"] == ["search", "read", "download"]
    assert "days" not in read_fn.parameters["properties"]
    assert read_fn.description is not None
    assert "- `recent`:" not in read_fn.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected_message"),
    [
        (lambda t: t.slack_read("search", kind="dms"), "For search, kind must be messages, conversations, or people"),
        (lambda t: t.slack_read("recent", kind="messages"), "For recent, kind must be dms or channels"),
        (lambda t: t.slack_read("recent", days=True), "days must be an integer from 1 to 365"),
        (lambda t: t.slack_read("read"), "target is required for read"),
        (lambda t: t.slack_read("download"), "file_id must be a Slack file ID such as F0123ABCD"),
        (lambda t: t.slack_send("post", CHANNEL, comment="hi"), "Use text, not comment, to post a message"),
        (lambda t: t.slack_send("post", CHANNEL), "text is required to post a message"),
        (
            lambda t: t.slack_send("upload", CHANNEL, path="notes.txt", text="hi"),
            "Use comment, not text, for an upload message",
        ),
        (lambda t: t.slack_send("upload", CHANNEL), "path is required to upload a file"),
        (
            lambda t: t.slack_manage("edit", f"{CHANNEL}:{ROOT_TS}", text="x", emoji="eyes"),
            "emoji is only for react, not edit",
        ),
        (lambda t: t.slack_manage("edit", f"{CHANNEL}:{ROOT_TS}"), "text is required to edit a message"),
        (
            lambda t: t.slack_manage("delete", f"{CHANNEL}:{ROOT_TS}", text="x"),
            "text and emoji are not used with delete",
        ),
        (
            lambda t: t.slack_manage("react", f"{CHANNEL}:{ROOT_TS}", text="x", emoji="eyes"),
            "text is not used with react",
        ),
        (lambda t: t.slack_manage("react", f"{CHANNEL}:{ROOT_TS}"), "emoji is required to react to a message"),
    ],
)
async def test_action_argument_mismatches_are_refused_before_any_slack_call(
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[SlackTools], Awaitable[str]],
    expected_message: str,
) -> None:
    """Missing or cross-action arguments fail cleanly without issuing any Slack request."""
    _connect_alice(paths)
    fake = FakeSlack().install(monkeypatch)

    result = json.loads(await call(_tools(paths)))

    assert result["status"] == "error"
    assert result["message"] == expected_message
    assert fake.requests == []
