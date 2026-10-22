# ruff: noqa: D103
"""Tests for the Outlook mail toolkit."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx
import pytest
from agno.tools.function import Function

from mindroom.custom_tools import outlook_mail
from mindroom.custom_tools.outlook_mail import OutlookMailTools
from mindroom.oauth.microsoft import outlook_mail_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess
from tests.microsoft_graph_test_support import (
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MESSAGE_ID = "AAMkAGI2-msg_1="
ATTACHMENT_ID = "AAMkAGI2-att_1="
MESSAGE_PATH = f"/v1.0/me/messages/{MESSAGE_ID}"
DRAFT_ID = "AAMkAGI2-draft_1="
DRAFT_PATH = f"/v1.0/me/messages/{DRAFT_ID}"
GRAPH = "https://graph.microsoft.com/v1.0"
NEXT_MESSAGES = "/me/messages?$top=25&$skiptoken=page2"
NEXT_ATTACHMENTS = f"/me/messages/{MESSAGE_ID}/attachments?$skiptoken=page2"
SHARED = "support@contoso.com"
SHARED_PREFIX = f"/v1.0/users/{SHARED}/"
# Graph may name a mailbox in a next link by its object id instead of the address the request used.
SHARED_ID = "7f3c2b1a-0000-4000-8000-00000000abcd"
READ_FUNCTIONS = (
    "outlook_mail_search",
    "outlook_mail_get_message",
    "outlook_mail_download_attachment",
    "outlook_mail_get_automatic_replies",
)
WRITE_FUNCTIONS = (
    "outlook_mail_create_draft",
    "outlook_mail_send",
    "outlook_mail_reply",
    "outlook_mail_mark_read",
    "outlook_mail_move",
    "outlook_mail_set_automatic_replies",
)


def _message(**overrides: object) -> dict[str, object]:
    return {
        "id": MESSAGE_ID,
        "subject": "Quarterly plan",
        "from": {"emailAddress": {"name": "Alex Example", "address": "alex@contoso.com"}},
        "toRecipients": [{"emailAddress": {"name": "Sam Example", "address": "sam@contoso.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "team@contoso.com"}}],
        "bccRecipients": [{"emailAddress": {"address": "audit@contoso.com"}}],
        "replyTo": [{"emailAddress": {"name": "Plans Desk", "address": "plans@contoso.com"}}],
        "parentFolderId": "AAMk-inbox=",
        "receivedDateTime": "2026-10-20T10:00:00Z",
        "sentDateTime": "2026-10-20T09:59:58Z",
        "isRead": False,
        "isDraft": False,
        "hasAttachments": True,
        "conversationId": "AAQkConv=",
        "webLink": "https://outlook.office365.com/owa/?ItemID=AAMk",
        "bodyPreview": "Here is the plan",
        "body": {"contentType": "text", "content": "Here is the plan.\nThanks"},
        **overrides,
    }


def _attachment(kind: str = "fileAttachment", **overrides: object) -> dict[str, object]:
    return {
        "@odata.type": f"#microsoft.graph.{kind}",
        "id": ATTACHMENT_ID,
        "name": "plan.pdf",
        "size": 11,
        "contentType": "application/pdf",
        "isInline": False,
        **overrides,
    }


def _reply_draft(**overrides: object) -> httpx.Response:
    """Return Graph's answer to createReply: the new reply draft, addressed like the message unless overridden."""
    return httpx.Response(201, json=_message(**{"id": DRAFT_ID, "isDraft": True, "bccRecipients": [], **overrides}))


def _selected(resource: dict[str, object], request: httpx.Request) -> dict[str, object]:
    """Return only the fields the request's `$select` names, as Graph does."""
    select = request.url.params.get("$select")
    if select is None:
        return resource
    fields = set(select.split(","))
    return {key: value for key, value in resource.items() if key in fields or key.startswith("@odata.")}


class _Graph:
    """Answer messages, attachments, and writes with the selected fields; record every request."""

    def __init__(self) -> None:
        self.message = _message()
        self.attachment = _attachment()
        self.content = b"pdf content"
        self.write = httpx.Response(202)
        self.override: Callable[[httpx.Request], httpx.Response | None] = lambda _request: None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (answer := self.override(request)) is not None:
            return answer
        if request.method in {"POST", "PATCH"}:
            return self.write
        listing = httpx.Response(
            200,
            json={"value": [_selected(self.message, request)], "@odata.nextLink": f"{GRAPH}{NEXT_MESSAGES}"},
        )
        attachments = [self.attachment, _attachment("itemAttachment", id="item")]
        routes = {
            "/v1.0/me/messages": listing,
            "/v1.0/me/mailFolders/inbox/messages": listing,
            "/v1.0/me/mailFolders('inbox')/messages": listing,
            MESSAGE_PATH: httpx.Response(200, json=_selected(self.message, request)),
            f"{MESSAGE_PATH}/attachments": httpx.Response(
                200,
                json={"value": [_selected(attachment, request) for attachment in attachments]},
            ),
            f"{MESSAGE_PATH}/attachments/{ATTACHMENT_ID}": httpx.Response(
                200,
                json=_selected(self.attachment, request),
            ),
            f"{MESSAGE_PATH}/attachments/{ATTACHMENT_ID}/$value": httpx.Response(200, content=self.content),
        }
        not_found = httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        # A shared mailbox holds the same messages as the requester's own in this fake.
        path = unquote(request.url.path).replace(SHARED_PREFIX, "/v1.0/me/", 1)
        return routes.get(path, not_found) if request.method == "GET" else not_found


def _tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    write: bool = True,
) -> tuple[OutlookMailTools, FakeGraph]:
    graph = FakeGraph(handler).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(outlook_mail_oauth_provider(), manager)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    tools = OutlookMailTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        runtime_config=None,
        tool_output_workspace_root=workspace,
        file_access="workspace",
        write=write,
    )
    return tools, graph


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _workspace_file(tmp_path: Path, relative: str, content: bytes) -> None:
    path = tmp_path / "workspace" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _description(tools: OutlookMailTools, name: str) -> str:
    """Return the description the model sees; deferred toolkits do not show toolkit instructions."""
    return " ".join(Function.from_callable(getattr(tools, name)).to_dict()["description"].split())


def test_registered_metadata_declares_a_primary_runtime_path_tool() -> None:
    metadata = TOOL_METADATA["outlook_mail"]

    assert metadata.display_name == "Outlook Mail"
    assert metadata.category == ToolCategory.EMAIL
    assert metadata.auth_provider == "outlook_mail"
    assert metadata.setup_type == SetupType.OAUTH
    assert metadata.requires_primary_runtime is True
    assert metadata.consumes_workspace_paths is True
    assert metadata.file_access == ToolFileAccess.AGENT
    assert metadata.function_names == (*READ_FUNCTIONS, *WRITE_FUNCTIONS)


def test_provider_requests_mail_scopes_on_the_shared_app() -> None:
    provider = outlook_mail_oauth_provider()

    assert provider.scopes == (
        "Mail.ReadWrite",
        "Mail.Send",
        "Mail.ReadWrite.Shared",
        "Mail.Send.Shared",
        "MailboxSettings.ReadWrite",
        "User.Read",
        "offline_access",
    )
    assert provider.credential_service == "outlook_mail_oauth"
    assert provider.tool_config_service == "outlook_mail"
    assert provider.shared_client_config_services == ("microsoft_oauth_client",)
    assert provider.requester_scoped_credentials is True


def test_a_connection_without_the_new_scopes_asks_to_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = FakeGraph(_Graph()).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(outlook_mail_oauth_provider(), manager, scopes=["Mail.ReadWrite", "Mail.Send", "User.Read"])
    tools = OutlookMailTools(runtime_paths=paths, credentials_manager=manager, worker_target=worker_target())

    result = _payload(tools.outlook_mail_search())

    assert result["oauth_connection_required"] is True
    assert graph.requests == []


def test_write_false_registers_only_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph(), write=False)

    assert set(tools.functions) == set(READ_FUNCTIONS)


def test_write_true_registers_every_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    assert set(tools.functions) == {*READ_FUNCTIONS, *WRITE_FUNCTIONS}


def test_write_descriptions_ask_to_tell_the_requester_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    for name in WRITE_FUNCTIONS:
        assert "Before calling, tell the requester" in _description(tools, name), name
    assert "untrusted" in _description(tools, "outlook_mail_get_message")
    assert "goes to the original's reply_to addresses when it has them" in _description(tools, "outlook_mail_reply")
    for name in ("outlook_mail_send", "outlook_mail_create_draft"):
        to = Function.from_callable(getattr(tools, name)).to_dict()["parameters"]["properties"]["to"]
        assert "Sam Example <sam@contoso.com>" in to["description"], name


def test_search_without_a_query_lists_the_newest_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_search())

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", "/v1.0/me/messages")
    assert request.url.params["$orderby"] == "receivedDateTime desc"
    assert request.url.params["$top"] == "25"
    assert "$search" not in request.url.params
    assert result["next_page_token"] == NEXT_MESSAGES
    assert result["messages"] == [
        {
            "id": MESSAGE_ID,
            "subject": "Quarterly plan",
            "from": "Alex Example <alex@contoso.com>",
            "to": ["Sam Example <sam@contoso.com>"],
            "received_at": "2026-10-20T10:00:00Z",
            "sent_at": "2026-10-20T09:59:58Z",
            "is_read": False,
            "is_draft": False,
            "has_attachments": True,
            "conversation_id": "AAQkConv=",
            "parent_folder_id": "AAMk-inbox=",
            "web_link": "https://outlook.office365.com/owa/?ItemID=AAMk",
            "preview": "Here is the plan",
        },
    ]


@pytest.mark.parametrize("mailbox_path", ["/me", "/me/mailFolders/inbox", "/me/mailFolders('inbox')"])
def test_search_follows_its_own_page_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mailbox_path: str) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())
    page_token = f"{mailbox_path}/messages?$top=25&$skiptoken=page2"

    result = _payload(tools.outlook_mail_search(query="ignored", folder="inbox", page_token=page_token))

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", f"/v1.0{mailbox_path}/messages")
    assert dict(request.url.params) == {"$top": "25", "$skiptoken": "page2"}
    assert result["next_page_token"] == NEXT_MESSAGES


@pytest.mark.parametrize(
    "page_token",
    [
        "/me/events?$skiptoken=abc",
        "/chats/19:2da4c29f6d7041eca70b638b43d45437@thread.v2/messages?$top=1",
        f"{GRAPH}{NEXT_MESSAGES}",
        "/me/messages/../../me/events/messages?x=1",
        "/me/messagesX?$skiptoken=abc",
        "/me/messages",
        "",
    ],
)
def test_search_refuses_a_foreign_page_token_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_search(page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


def test_search_quotes_the_query_and_drops_the_sort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    tools.outlook_mail_search(query=' from:alex subject:"quarterly plan" ', folder="inbox", limit=5)

    [request] = graph.requests
    assert request.url.path == "/v1.0/me/mailFolders/inbox/messages"
    assert request.url.params["$search"] == '"from:alex subject:\\"quarterly plan\\""'
    assert "$orderby" not in request.url.params
    assert request.url.params["$top"] == "5"


def test_search_escapes_a_backslash_in_the_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    tools.outlook_mail_search(query="C:\\Reports")

    assert graph.requests[0].url.params["$search"] == '"C:\\\\Reports"'


def test_search_escapes_a_folder_id_as_one_path_segment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    tools.outlook_mail_search(folder="AAMk folder+1=")

    assert graph.requests[0].url.raw_path.split(b"?")[0] == b"/v1.0/me/mailFolders/AAMk%20folder%2B1=/messages"


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"query": "   "},
        {"query": "x" * 1001},
        {"folder": "../inbox"},
        {"folder": "inbox/child"},
        {"folder": ""},
    ],
)
def test_search_refuses_malformed_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_search(**arguments))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_get_message_reads_the_text_body_and_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    message = _payload(tools.outlook_mail_get_message(MESSAGE_ID))["message"]

    get, attachments = graph.requests
    assert unquote(get.url.path) == MESSAGE_PATH
    assert get.headers["prefer"] == 'outlook.body-content-type="text"'
    assert get.url.params["$select"] == (
        "id,subject,from,toRecipients,receivedDateTime,sentDateTime,isRead,isDraft,hasAttachments,conversationId,"
        "parentFolderId,webLink,ccRecipients,bccRecipients,replyTo,body"
    )
    assert unquote(attachments.url.path) == f"{MESSAGE_PATH}/attachments"
    assert attachments.url.params["$select"] == "id,name,size,contentType,isInline"
    assert message["from"] == "Alex Example <alex@contoso.com>"
    assert message["reply_to"] == ["Plans Desk <plans@contoso.com>"]
    assert message["cc"] == ["team@contoso.com"]
    assert message["bcc"] == ["audit@contoso.com"]
    assert message["parent_folder_id"] == "AAMk-inbox="
    assert message["attachments_truncated"] is False
    assert message["body"] == "Here is the plan.\nThanks"
    assert message["truncated"] is False
    assert message["attachments"] == [
        {
            "id": ATTACHMENT_ID,
            "name": "plan.pdf",
            "size": 11,
            "content_type": "application/pdf",
            "is_inline": False,
            "type": "file",
        },
        {
            "id": "item",
            "name": "plan.pdf",
            "size": 11,
            "content_type": "application/pdf",
            "is_inline": False,
            "type": "item",
        },
    ]


def test_get_message_follows_attachment_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    first = {"value": [_attachment(id="first")], "@odata.nextLink": f"{GRAPH}{NEXT_ATTACHMENTS}"}
    second = {"value": [_attachment(id="second")]}
    mail.override = lambda request: (
        httpx.Response(200, json=second if "$skiptoken" in request.url.params else first)
        if unquote(request.url.path) == f"{MESSAGE_PATH}/attachments"
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    message = _payload(tools.outlook_mail_get_message(MESSAGE_ID))["message"]

    assert [attachment["id"] for attachment in message["attachments"]] == ["first", "second"]
    assert message["attachments_truncated"] is False
    continued = graph.requests[-1]
    assert unquote(continued.url.path) == f"{MESSAGE_PATH}/attachments"
    assert dict(continued.url.params) == {"$skiptoken": "page2"}


def test_get_message_stops_listing_attachments_at_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    page = {
        "value": [_attachment(id=f"a{index}") for index in range(outlook_mail._MAX_ATTACHMENTS)],
        "@odata.nextLink": f"{GRAPH}{NEXT_ATTACHMENTS}",
    }
    mail.override = lambda request: (
        httpx.Response(200, json=page) if unquote(request.url.path) == f"{MESSAGE_PATH}/attachments" else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    message = _payload(tools.outlook_mail_get_message(MESSAGE_ID))["message"]

    assert len(message["attachments"]) == outlook_mail._MAX_ATTACHMENTS
    assert message["attachments_truncated"] is True
    assert len(graph.requests) == 2


def test_get_message_truncates_a_long_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    mail.message = _message(body={"contentType": "text", "content": "x" * (outlook_mail._MAX_BODY_CHARS + 5)})
    tools, _graph = _tools(tmp_path, monkeypatch, mail)

    message = _payload(tools.outlook_mail_get_message(MESSAGE_ID))["message"]

    assert len(message["body"]) == outlook_mail._MAX_BODY_CHARS
    assert message["truncated"] is True


@pytest.mark.parametrize("message_id", ["", "..", "a/b", "a?b", "a#b", "a\\b", "a\nb"])
@pytest.mark.parametrize(
    "call",
    [
        lambda tools, message_id: tools.outlook_mail_get_message(message_id),
        lambda tools, message_id: tools.outlook_mail_download_attachment(message_id, ATTACHMENT_ID),
        lambda tools, message_id: tools.outlook_mail_download_attachment(MESSAGE_ID, message_id),
        lambda tools, message_id: tools.outlook_mail_reply(message_id, ["alex@contoso.com"], "Thanks"),
        lambda tools, message_id: tools.outlook_mail_mark_read(message_id),
        lambda tools, message_id: tools.outlook_mail_move(message_id, "archive"),
        lambda tools, message_id: tools.outlook_mail_move(MESSAGE_ID, message_id),
    ],
)
def test_ids_that_would_change_the_path_are_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message_id: str,
    call: Callable[[OutlookMailTools, str], str],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(call(tools, message_id))["code"] == "invalid_argument"
    assert graph.requests == []


def test_download_saves_a_file_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID))

    target = tmp_path / "workspace" / "outlook-mail-attachments" / "plan.pdf"
    assert target.read_bytes() == b"pdf content"
    assert result["path"] == str(target)
    assert result["attachment"]["name"] == "plan.pdf"
    metadata, content = graph.requests
    assert metadata.url.params["$select"] == "id,name,size,contentType,isInline"
    assert unquote(content.url.path) == f"{MESSAGE_PATH}/attachments/{ATTACHMENT_ID}/$value"


@pytest.mark.parametrize("kind", ["itemAttachment", "referenceAttachment"])
def test_download_refuses_attachments_that_are_not_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    mail = _Graph()
    mail.attachment = _attachment(kind)
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID))

    assert result["code"] == "not_a_file_attachment"
    assert len(graph.requests) == 1


def test_download_checks_the_size_before_fetching_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    mail.attachment = _attachment(size=outlook_mail._MAX_DOWNLOAD_BYTES + 1)
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    assert _payload(tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID))["code"] == "too_large"
    assert len(graph.requests) == 1


@pytest.mark.parametrize(
    ("name", "saved"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\alex\\report.pdf", "report.pdf"),
        ("D:notes.txt", "D_notes.txt"),
        ("..hidden", "hidden"),
        ("line\nbreak.txt", "linebreak.txt"),
        ("", "attachment"),
        (None, "attachment"),
        ("é" * 300 + ".pdf", "é" * 98 + ".pdf"),
    ],
)
def test_download_makes_the_attachment_name_a_safe_file_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str | None,
    saved: str,
) -> None:
    mail = _Graph()
    mail.attachment = _attachment(name=name)
    tools, _graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID))

    assert result["path"] == str(tmp_path / "workspace" / "outlook-mail-attachments" / saved)


def test_create_draft_posts_a_text_message_with_inline_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace_file(tmp_path, "out/plan.pdf", b"%PDF-1.7")
    mail = _Graph()
    mail.write = httpx.Response(201, json=_message(isDraft=True, isRead=True))
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(
        tools.outlook_mail_create_draft(
            ["sam@contoso.com"],
            "Quarterly plan",
            "Hi Sam,\nattached.",
            cc=[" team@contoso.com "],
            attachments=["out/plan.pdf"],
        ),
    )

    [request] = graph.requests
    assert (request.method, request.url.path) == ("POST", "/v1.0/me/messages")
    assert _body(request) == {
        "subject": "Quarterly plan",
        "body": {"contentType": "Text", "content": "Hi Sam,\nattached."},
        "toRecipients": [{"emailAddress": {"address": "sam@contoso.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "team@contoso.com"}}],
        "attachments": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "plan.pdf",
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(b"%PDF-1.7").decode("ascii"),
            },
        ],
    }
    assert result["message"]["id"] == MESSAGE_ID
    assert result["message"]["is_draft"] is True


def test_an_attachment_over_the_inline_limit_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "big.bin", b"x" * (outlook_mail._MAX_ATTACHMENT_BYTES + 1))
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_create_draft(["sam@contoso.com"], "s", "b", attachments=["big.bin"]))

    assert result["code"] == "too_large"
    assert graph.requests == []


def test_attachments_over_the_request_limit_are_refused_before_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace_file(tmp_path, "a.bin", b"x" * (2 * 1024 * 1024))
    _workspace_file(tmp_path, "b.bin", b"x" * (2 * 1024 * 1024))
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())
    encoded: list[int] = []
    monkeypatch.setattr(outlook_mail.base64, "b64encode", lambda content: encoded.append(len(content)) or b"")

    result = _payload(tools.outlook_mail_send(["sam@contoso.com"], "s", "b", attachments=["a.bin", "b.bin"]))

    assert result["code"] == "too_large"
    assert "OneDrive" in result["message"]
    assert encoded == []
    assert graph.requests == []


def test_a_non_ascii_body_is_measured_as_the_bytes_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # "é" is two bytes in the UTF-8 request body, so this message is just under the request limit.
    body = "é" * ((outlook_mail._MAX_REQUEST_BYTES - 1000) // 2)
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_send(["sam@contoso.com"], "Notes", body))

    assert result == {"sent": True, "status": "ok", "tool": "outlook_mail"}
    [request] = graph.requests
    assert _body(request)["message"]["body"]["content"] == body
    assert int(request.headers["content-length"]) <= outlook_mail._MAX_REQUEST_BYTES

    over = _payload(tools.outlook_mail_send(["sam@contoso.com"], "Notes", body + "é" * 1000))

    assert over["code"] == "too_large"
    assert len(graph.requests) == 1


def test_an_attachment_outside_the_workspace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "secret.txt").write_bytes(b"secret")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_send(["sam@contoso.com"], "s", "b", attachments=["../secret.txt"]))

    assert result["code"] == "invalid_local_path"
    assert graph.requests == []


@pytest.mark.parametrize(
    ("to", "cc"),
    [
        ([], None),
        (["no-at-sign"], None),
        (["a@b@contoso.com"], None),
        (["sam smith@contoso.com"], None),
        (["sam@contoso.com\x00"], None),
        (["x" * 250 + "@contoso.com"], None),
        (["Sam Example <not-an-email>"], None),
        (["Sam Example <sam@contoso.com> extra"], None),
        (["Sam Example <a@b@contoso.com>"], None),
        (["Sam Example <" + "x" * 250 + "@contoso.com>"], None),
        (["Sam\x00Example <sam@contoso.com>"], None),
        ("sam@contoso.com", None),
        ([1], None),
        (["sam@contoso.com"], ["bad"]),
        ([f"user{index}@contoso.com" for index in range(40)], [f"cc{index}@contoso.com" for index in range(11)]),
    ],
)
def test_invalid_recipients_are_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    to: object,
    cc: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    for result in (
        tools.outlook_mail_send(to, "s", "b", cc=cc),  # type: ignore[arg-type]
        tools.outlook_mail_create_draft(to, "s", "b", cc=cc),  # type: ignore[arg-type]
    ):
        assert _payload(result)["code"] == "invalid_argument"
    assert graph.requests == []


@pytest.mark.parametrize(
    ("recipient", "email_address"),
    [
        ("sam@contoso.com", {"address": "sam@contoso.com"}),
        ("Sam Example <sam@contoso.com>", {"name": "Sam Example", "address": "sam@contoso.com"}),
        (" Sam Example  < sam@contoso.com > ", {"name": "Sam Example", "address": "sam@contoso.com"}),
        ("<sam@contoso.com>", {"address": "sam@contoso.com"}),
        ('"Smith, Sam" <sam@contoso.com>', {"name": "Smith, Sam", "address": "sam@contoso.com"}),
    ],
)
def test_recipients_accept_bare_addresses_and_the_form_read_results_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipient: str,
    email_address: dict[str, str],
) -> None:
    mail = _Graph()
    mail.write = httpx.Response(201, json=_message(isDraft=True))
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    assert _payload(tools.outlook_mail_send([recipient], "s", "b", cc=[recipient]))["status"] == "ok"
    assert _payload(tools.outlook_mail_create_draft([recipient], "s", "b", cc=[recipient]))["status"] == "ok"

    sent, draft = (_body(request) for request in graph.requests)
    for message in (sent["message"], draft):
        assert message["toRecipients"] == [{"emailAddress": email_address}]
        assert message["ccRecipients"] == [{"emailAddress": email_address}]


@pytest.mark.parametrize(("subject", "body"), [(None, "b"), ("s", None), ("s", 3)])
def test_send_refuses_a_subject_or_body_that_is_not_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subject: object,
    body: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_send(["sam@contoso.com"], subject, body))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_send_posts_send_mail_and_keeps_a_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_send(["sam@contoso.com"], "Lunch", "Noon?"))

    [request] = graph.requests
    assert (request.method, request.url.path) == ("POST", "/v1.0/me/sendMail")
    assert _body(request) == {
        "message": {
            "subject": "Lunch",
            "body": {"contentType": "Text", "content": "Noon?"},
            "toRecipients": [{"emailAddress": {"address": "sam@contoso.com"}}],
        },
        "saveToSentItems": True,
    }
    assert result["sent"] is True


@pytest.mark.parametrize(("reply_all", "action"), [(False, "createReply"), (True, "createReplyAll")])
def test_reply_sends_the_body_as_an_escaped_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply_all: bool,
    action: str,
) -> None:
    mail = _Graph()
    mail.write = _reply_draft()
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    # Case does not matter, and naming an address the draft leaves out, such as the requester's own, still sends it.
    result = _payload(
        tools.outlook_mail_reply(
            MESSAGE_ID,
            ["Sam Example <sam@contoso.com>", "TEAM@contoso.com", "alex@contoso.com"],
            "Thanks <team> & all\r\nSee you",
            reply_all=reply_all,
        ),
    )

    create, send = graph.requests
    assert (create.method, unquote(create.url.path)) == ("POST", f"{MESSAGE_PATH}/{action}")
    assert _body(create) == {"comment": "Thanks &lt;team&gt; &amp; all<br>See you"}
    assert (send.method, unquote(send.url.path)) == ("POST", f"{DRAFT_PATH}/send")
    assert (result["sent"], result["recipients"]) == (True, ["Sam Example <sam@contoso.com>", "team@contoso.com"])


@pytest.mark.parametrize(
    ("field", "recipient", "listed"),
    [
        # An outside address the caller did not name, such as the original's reply_to.
        ("toRecipients", {"emailAddress": {"address": "desk@outside.example"}}, "desk@outside.example"),
        ("bccRecipients", {"emailAddress": {"address": "desk@outside.example"}}, "desk@outside.example"),
        # A recipient without an address cannot be matched against the named ones.
        ("ccRecipients", {"emailAddress": {"name": "Desk"}}, "Desk"),
        # An address that differs from a named one by more than case, here ss for ß.
        ("toRecipients", {"emailAddress": {"address": "desk@strasse.example"}}, "desk@strasse.example"),
    ],
)
def test_reply_to_a_recipient_not_named_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    recipient: dict[str, object],
    listed: str,
) -> None:
    mail = _Graph()
    mail.write = _reply_draft(**{"toRecipients": [], "ccRecipients": [], field: [recipient]})
    mail.override = lambda request: httpx.Response(204) if request.method == "DELETE" else None
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    named = ["alex@contoso.com", "desk@straße.example"]
    refused = _payload(tools.outlook_mail_reply(MESSAGE_ID, recipients=named, body="Thanks"))

    assert (refused["code"], refused["recipients"]) == ("recipients_mismatch", [listed])
    assert [(request.method, unquote(request.url.path)) for request in graph.requests] == [
        ("POST", f"{MESSAGE_PATH}/createReply"),
        ("DELETE", DRAFT_PATH),
    ]


@pytest.mark.parametrize(
    ("recipients", "body", "reply_all"),
    [
        (["alex@contoso.com"], "", False),
        (["alex@contoso.com"], "  ", False),
        (["alex@contoso.com"], None, False),
        (["alex@contoso.com"], "ok", "yes"),
        ("alex@contoso.com", "ok", False),
        (["alex"], "ok", False),
    ],
)
def test_reply_refuses_malformed_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipients: object,
    body: object,
    reply_all: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_reply(MESSAGE_ID, recipients, body, reply_all=reply_all))  # type: ignore[arg-type]

    assert result["code"] == "invalid_argument"
    assert graph.requests == []


def test_mark_read_patches_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    mail.write = httpx.Response(200, json=_message(isRead=False))
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_mark_read(MESSAGE_ID, is_read=False))

    [request] = graph.requests
    assert (request.method, unquote(request.url.path)) == ("PATCH", MESSAGE_PATH)
    assert _body(request) == {"isRead": False}
    assert (result["message_id"], result["is_read"]) == (MESSAGE_ID, False)


def test_mark_read_refuses_a_flag_that_is_not_a_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_mark_read(MESSAGE_ID, is_read="false"))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_move_posts_the_destination_and_returns_the_new_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    mail.write = httpx.Response(201, json=_message(id="AAMk-moved="))
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_move(MESSAGE_ID, "archive"))

    [request] = graph.requests
    assert (request.method, unquote(request.url.path)) == ("POST", f"{MESSAGE_PATH}/move")
    assert _body(request) == {"destinationId": "archive"}
    assert result["message"]["id"] == "AAMk-moved="


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}), "not_found"),
        (httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}), "forbidden"),
        (httpx.Response(429, headers={"Retry-After": "7"}), "throttled"),
    ],
)
def test_graph_errors_keep_their_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    code: str,
) -> None:
    mail = _Graph()
    mail.override = lambda _request: response
    tools, _graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_get_message(MESSAGE_ID))

    assert result["status"] == "error"
    assert result["code"] == code
    if code == "throttled":
        assert result["retry_after_seconds"] == 7
    else:
        assert result["graph_code"] == response.json()["error"]["code"]


def _shared_reply_graph() -> _Graph:
    mail = _Graph()
    mail.write = _reply_draft()
    mail.override = lambda request: httpx.Response(204) if request.method == "DELETE" else None
    return mail


@pytest.mark.parametrize(
    ("call", "requests"),
    [
        (lambda tools: tools.outlook_mail_search(mailbox=SHARED), [("GET", "messages")]),
        (
            lambda tools: tools.outlook_mail_search(query="plan", folder="inbox", mailbox=SHARED),
            [("GET", "mailFolders/inbox/messages")],
        ),
        (
            lambda tools: tools.outlook_mail_get_message(MESSAGE_ID, mailbox=SHARED),
            [("GET", f"messages/{MESSAGE_ID}"), ("GET", f"messages/{MESSAGE_ID}/attachments")],
        ),
        (
            lambda tools: tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID, mailbox=SHARED),
            [
                ("GET", f"messages/{MESSAGE_ID}/attachments/{ATTACHMENT_ID}"),
                ("GET", f"messages/{MESSAGE_ID}/attachments/{ATTACHMENT_ID}/$value"),
            ],
        ),
        (
            lambda tools: tools.outlook_mail_create_draft(["sam@contoso.com"], "s", "b", mailbox=SHARED),
            [("POST", "messages")],
        ),
        (
            lambda tools: tools.outlook_mail_send(["sam@contoso.com"], "s", "b", mailbox=SHARED),
            [("POST", "sendMail")],
        ),
        (
            lambda tools: tools.outlook_mail_reply(
                MESSAGE_ID,
                ["sam@contoso.com", "team@contoso.com"],
                "Thanks",
                mailbox=SHARED,
            ),
            [("POST", f"messages/{MESSAGE_ID}/createReply"), ("POST", f"messages/{DRAFT_ID}/send")],
        ),
        (
            lambda tools: tools.outlook_mail_mark_read(MESSAGE_ID, mailbox=SHARED),
            [("PATCH", f"messages/{MESSAGE_ID}")],
        ),
        (
            lambda tools: tools.outlook_mail_move(MESSAGE_ID, "archive", mailbox=SHARED),
            [("POST", f"messages/{MESSAGE_ID}/move")],
        ),
    ],
)
def test_every_mail_function_acts_in_the_mailbox_it_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[OutlookMailTools], str],
    requests: list[tuple[str, str]],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _shared_reply_graph())

    result = _payload(call(tools))

    assert result["status"] == "ok"
    assert result["mailbox"] == SHARED
    assert [(request.method, unquote(request.url.path)) for request in graph.requests] == [
        (method, f"{SHARED_PREFIX}{path}") for method, path in requests
    ]


def test_results_name_no_mailbox_for_the_requesters_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    assert "mailbox" not in _payload(tools.outlook_mail_send(["sam@contoso.com"], "s", "b"))
    assert "mailbox" not in _payload(tools.outlook_mail_search())


def test_a_guest_sign_in_name_is_escaped_as_one_path_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    tools.outlook_mail_search(mailbox="alex_fabrikam.com#EXT#@contoso.onmicrosoft.com")

    [request] = graph.requests
    assert (
        request.url.raw_path.split(b"?")[0]
        == b"/v1.0/users/alex_fabrikam.com%23EXT%23@contoso.onmicrosoft.com/messages"
    )


@pytest.mark.parametrize(
    "mailbox",
    [
        "",
        "me",
        "support",
        "a@b@contoso.com",
        "support@contoso.com/../me",
        "x" * 250 + "@contoso.com",
        "a?b@c.com",
        f" {SHARED}",
        f"{SHARED}\u200b",
        3,
    ],
)
@pytest.mark.parametrize(
    "call",
    [
        lambda tools, mailbox: tools.outlook_mail_search(mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_get_message(MESSAGE_ID, mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_download_attachment(MESSAGE_ID, ATTACHMENT_ID, mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_create_draft(["sam@contoso.com"], "s", "b", mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_send(["sam@contoso.com"], "s", "b", mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_reply(MESSAGE_ID, ["sam@contoso.com"], "Thanks", mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_mark_read(MESSAGE_ID, mailbox=mailbox),
        lambda tools, mailbox: tools.outlook_mail_move(MESSAGE_ID, "archive", mailbox=mailbox),
    ],
)
def test_a_malformed_mailbox_is_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mailbox: object,
    call: Callable[[OutlookMailTools, object], str],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(call(tools, mailbox))

    assert (result["code"], "mailbox" in result["message"]) == ("invalid_argument", True)
    assert "mailbox" not in result
    assert graph.requests == []


@pytest.mark.parametrize(
    "call",
    [
        lambda tools: tools.outlook_mail_search(limit=0, mailbox=SHARED),
        lambda tools: tools.outlook_mail_search(page_token=NEXT_MESSAGES, mailbox=SHARED),
        lambda tools: tools.outlook_mail_get_message("a/b", mailbox=SHARED),
        lambda tools: tools.outlook_mail_download_attachment(MESSAGE_ID, "a/b", mailbox=SHARED),
        lambda tools: tools.outlook_mail_create_draft([], "s", "b", mailbox=SHARED),
        lambda tools: tools.outlook_mail_send(["not-an-email"], "s", "b", mailbox=SHARED),
        lambda tools: tools.outlook_mail_reply(MESSAGE_ID, ["sam@contoso.com"], " ", mailbox=SHARED),
        lambda tools: tools.outlook_mail_mark_read("a/b", mailbox=SHARED),
        lambda tools: tools.outlook_mail_move(MESSAGE_ID, "a/b", mailbox=SHARED),
    ],
)
def test_a_refused_call_with_a_mailbox_names_the_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[OutlookMailTools], str],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(call(tools))

    assert result["code"] in {"invalid_argument", "invalid_page_token"}
    assert result["mailbox"] == SHARED
    assert graph.requests == []


def test_a_graph_error_in_a_shared_mailbox_names_the_mailbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_get_message("AAMk-missing=", mailbox=SHARED))

    assert (result["code"], result["graph_code"], result["mailbox"]) == ("not_found", "ErrorItemNotFound", SHARED)


def test_a_shared_mailbox_reply_to_a_recipient_not_named_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail = _shared_reply_graph()
    mail.write = _reply_draft(toRecipients=[{"emailAddress": {"address": "desk@outside.example"}}], ccRecipients=[])
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    refused = _payload(tools.outlook_mail_reply(MESSAGE_ID, ["alex@contoso.com"], "Thanks", mailbox=SHARED))

    assert (refused["code"], refused["recipients"]) == ("recipients_mismatch", ["desk@outside.example"])
    assert refused["mailbox"] == SHARED
    assert [(request.method, unquote(request.url.path)) for request in graph.requests] == [
        ("POST", f"{SHARED_PREFIX}messages/{MESSAGE_ID}/createReply"),
        ("DELETE", f"{SHARED_PREFIX}messages/{DRAFT_ID}"),
    ]


@pytest.mark.parametrize(
    "graph_owner",
    [
        f"/users/{SHARED}",
        f"/users/{SHARED.replace('@', '%40')}",
        f"/users('{SHARED_ID}')",
        f"/users({SHARED_ID})",
        f"/users/{SHARED_ID}",
    ],
)
def test_a_shared_mailbox_page_token_names_that_mailbox_and_continues_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_owner: str,
) -> None:
    mail = _Graph()
    next_link = f"{GRAPH}{graph_owner}/mailFolders('inbox')/messages?$top=25&$skiptoken=page2"
    mail.override = lambda request: (
        httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})
        if request.url.path.endswith("/inbox/messages") and "$skiptoken" not in request.url.params
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    token = _payload(tools.outlook_mail_search(folder="inbox", mailbox=SHARED))["next_page_token"]
    tools.outlook_mail_search(mailbox=SHARED, page_token=token)

    assert token == f"/users/{SHARED}/mailFolders('inbox')/messages?$top=25&$skiptoken=page2"
    continued = graph.requests[-1]
    assert unquote(continued.url.path) == f"{SHARED_PREFIX}mailFolders('inbox')/messages"
    assert dict(continued.url.params) == {"$top": "25", "$skiptoken": "page2"}


def test_an_own_mailbox_page_token_that_graph_names_by_user_id_comes_back_as_me(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail = _Graph()
    mail.override = lambda _request: httpx.Response(
        200,
        json={"value": [], "@odata.nextLink": f"{GRAPH}/users/{SHARED_ID}/messages?$skip=25"},
    )
    tools, _graph = _tools(tmp_path, monkeypatch, mail)

    next_page = _payload(tools.outlook_mail_search())["next_page_token"]

    assert next_page == "/me/messages?$skip=25"


@pytest.mark.parametrize(
    ("page_token", "mailbox"),
    [
        (f"/users/{SHARED}/messages?$skip=25", None),
        (f"/users/{SHARED}/messages?$skip=25", "billing@contoso.com"),
        (f"/users/{SHARED}.evil/messages?$skip=25", SHARED),
        (f"/users('{SHARED}')/messages?$skip=25", None),
        (f"/users/{SHARED_ID}/messages?$skip=25", None),
        (f"/users/{SHARED_ID}/messages?$skip=25", SHARED),
        ("/me/messages?$skip=25", SHARED),
    ],
)
def test_a_page_token_of_another_mailbox_is_refused_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
    mailbox: str | None,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_search(mailbox=mailbox, page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


def test_shared_mailbox_attachment_pages_stay_in_that_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail = _Graph()
    next_link = f"{GRAPH}/users('{SHARED_ID}')/messages('{MESSAGE_ID}')/attachments?$skiptoken=page2"
    first = {"value": [_attachment(id="first")], "@odata.nextLink": next_link}
    second = {"value": [_attachment(id="second")]}
    mail.override = lambda request: (
        httpx.Response(200, json=second if "$skiptoken" in request.url.params else first)
        if request.url.path.endswith("/attachments")
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    message = _payload(tools.outlook_mail_get_message(MESSAGE_ID, mailbox=SHARED))["message"]

    assert [attachment["id"] for attachment in message["attachments"]] == ["first", "second"]
    assert unquote(graph.requests[-1].url.path) == f"{SHARED_PREFIX}messages('{MESSAGE_ID}')/attachments"


def _automatic_replies(**overrides: object) -> dict[str, object]:
    return {
        "status": "scheduled",
        "externalAudience": "all",
        "scheduledStartDateTime": {"dateTime": "2026-11-02T08:00:00.0000000", "timeZone": "UTC"},
        "scheduledEndDateTime": {"dateTime": "2026-11-06T17:00:00.0000000", "timeZone": "UTC"},
        "internalReplyMessage": "<html>\n<body>\n<p>Out until Friday.<br>\nAsk Sam.</p></body>\n</html>\n",
        "externalReplyMessage": "<html>\n<body>\n<p>I reply on my return.</p></body>\n</html>\n",
        **overrides,
    }


def test_get_automatic_replies_reads_the_requesters_own_setting_as_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail = _Graph()
    mail.override = lambda _request: httpx.Response(200, json=_automatic_replies())
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_get_automatic_replies())

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", "/v1.0/me/mailboxSettings/automaticRepliesSetting")
    assert result["automatic_replies"] == {
        "status": "scheduled",
        "external_audience": "all",
        "scheduled_start": "2026-11-02T08:00:00Z",
        "scheduled_end": "2026-11-06T17:00:00Z",
        "internal_message": "Out until Friday.\nAsk Sam.",
        "external_message": "I reply on my return.",
    }


@pytest.mark.parametrize(
    ("graph_status", "graph_audience", "status", "audience"),
    [
        ("alwaysEnabled", "none", "alwaysEnabled", "internal_only"),
        ("Scheduled", "None", "scheduled", "internal_only"),
        ("disabled", "ContactsOnly", "disabled", "contactsOnly"),
        ("AlwaysEnabled", "All", "alwaysEnabled", "all"),
    ],
)
def test_get_automatic_replies_reports_values_the_setter_takes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_status: str,
    graph_audience: str,
    status: str,
    audience: str,
) -> None:
    # Graph's examples write these values in either case.
    mail = _Graph()
    setting = _automatic_replies(status=graph_status, externalAudience=graph_audience)
    mail.override = lambda _request: httpx.Response(200, json=setting)
    tools, _graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(tools.outlook_mail_get_automatic_replies())["automatic_replies"]

    assert (result["status"], result["external_audience"]) == (status, audience)


def test_set_automatic_replies_schedules_plain_text_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mail = _Graph()
    mail.write = httpx.Response(200, json={"automaticRepliesSetting": _automatic_replies()})
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    result = _payload(
        tools.outlook_mail_set_automatic_replies(
            "scheduled",
            internal_message="Out until Friday.\nAsk <Sam> & co.",
            external_message="I reply on my return.",
            start="2026-11-02T09:00:00",
            end="2026-11-06T18:00",
            time_zone="W. Europe Standard Time",
            external_audience="contactsOnly",
        ),
    )

    [request] = graph.requests
    assert (request.method, request.url.path) == ("PATCH", "/v1.0/me/mailboxSettings")
    assert _body(request) == {
        "automaticRepliesSetting": {
            "status": "scheduled",
            "internalReplyMessage": "Out until Friday.<br>Ask &lt;Sam&gt; &amp; co.",
            "externalReplyMessage": "I reply on my return.",
            "externalAudience": "contactsOnly",
            "scheduledStartDateTime": {"dateTime": "2026-11-02T09:00:00", "timeZone": "W. Europe Standard Time"},
            "scheduledEndDateTime": {"dateTime": "2026-11-06T18:00:00", "timeZone": "W. Europe Standard Time"},
        },
    }
    assert result["automatic_replies"]["status"] == "scheduled"
    assert result["automatic_replies"]["internal_message"] == "Out until Friday.\nAsk Sam."


@pytest.mark.parametrize(
    ("arguments", "setting"),
    [
        ({"status": "disabled"}, {"status": "disabled"}),
        ({"status": "disabled", "external_message": ""}, {"status": "disabled", "externalReplyMessage": ""}),
        (
            {"status": "alwaysEnabled", "internal_message": "Away", "external_audience": "internal_only"},
            {"status": "alwaysEnabled", "internalReplyMessage": "Away", "externalAudience": "none"},
        ),
        (
            {"status": "alwaysEnabled", "internal_message": "", "external_message": "Away", "external_audience": "all"},
            {
                "status": "alwaysEnabled",
                "internalReplyMessage": "",
                "externalReplyMessage": "Away",
                "externalAudience": "all",
            },
        ),
    ],
)
def test_set_automatic_replies_sends_only_the_fields_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    setting: dict[str, object],
) -> None:
    mail = _Graph()
    mail.write = httpx.Response(200, json={"automaticRepliesSetting": _automatic_replies(status=arguments["status"])})
    tools, graph = _tools(tmp_path, monkeypatch, mail)

    assert _payload(tools.outlook_mail_set_automatic_replies(**arguments))["status"] == "ok"  # type: ignore[arg-type]

    [request] = graph.requests
    assert _body(request) == {"automaticRepliesSetting": setting}


# Arguments that turn automatic replies on and name everything senders get.
ON = {"status": "alwaysEnabled", "internal_message": "Away", "external_message": "Away", "external_audience": "all"}
PERIOD = {"start": "2026-11-02T09:00:00", "end": "2026-11-06T18:00:00"}


@pytest.mark.parametrize(
    "arguments",
    [
        {**ON, "status": "on"},
        {**ON, "status": ["alwaysEnabled"]},
        {**ON, **PERIOD, "status": "Scheduled"},
        {**ON, "status": "scheduled"},
        {**ON, "status": "scheduled", "start": PERIOD["start"]},
        {**ON, "status": "scheduled", "start": PERIOD["start"], "end": PERIOD["start"]},
        {**ON, **PERIOD, "status": "scheduled", "start": "2026-11-02T09:00:00+01:00"},
        {**ON, **PERIOD, "status": "scheduled", "start": "next monday"},
        {**ON, **PERIOD, "status": "scheduled", "time_zone": "Mars/Base;"},
        {**ON, **PERIOD},
        {"status": "disabled", "end": PERIOD["end"]},
        {**ON, "external_audience": "everyone"},
        {**ON, "external_audience": "none"},
        {**ON, "external_audience": ["all"]},
        {**ON, "internal_message": 3},
        {**ON, "external_message": ["Away"]},
        # Turning replies on names everything senders get, so an approval card shows it.
        {"status": "alwaysEnabled"},
        {**ON, "internal_message": None},
        {**ON, "external_audience": None},
        {**ON, "external_message": None},
        {**ON, "external_audience": "contactsOnly", "external_message": None},
        {**ON, **PERIOD, "status": "scheduled", "internal_message": None},
    ],
)
def test_set_automatic_replies_refuses_malformed_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_set_automatic_replies(**arguments))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []
