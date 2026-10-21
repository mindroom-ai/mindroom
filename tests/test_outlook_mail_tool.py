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
GRAPH = "https://graph.microsoft.com/v1.0"
NEXT_MESSAGES = "/me/messages?$top=25&$skiptoken=page2"
NEXT_ATTACHMENTS = f"/me/messages/{MESSAGE_ID}/attachments?$skiptoken=page2"
READ_FUNCTIONS = (
    "outlook_mail_search",
    "outlook_mail_get_message",
    "outlook_mail_download_attachment",
)
WRITE_FUNCTIONS = (
    "outlook_mail_create_draft",
    "outlook_mail_send_draft",
    "outlook_mail_send",
    "outlook_mail_reply",
    "outlook_mail_mark_read",
    "outlook_mail_move",
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
        return routes.get(unquote(request.url.path), not_found) if request.method == "GET" else not_found


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

    assert provider.scopes == ("Mail.ReadWrite", "Mail.Send", "User.Read", "offline_access")
    assert provider.credential_service == "outlook_mail_oauth"
    assert provider.tool_config_service == "outlook_mail"
    assert provider.shared_client_config_services == ("microsoft_oauth_client",)
    assert provider.requester_scoped_credentials is True


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
    assert "read the draft with outlook_mail_get_message" in _description(tools, "outlook_mail_send_draft")
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


def test_search_follows_its_own_page_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_search(query="ignored", folder="inbox", page_token=NEXT_MESSAGES))

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", "/v1.0/me/messages")
    assert dict(request.url.params) == {"$top": "25", "$skiptoken": "page2"}
    assert result["next_page_token"] == NEXT_MESSAGES


@pytest.mark.parametrize(
    "page_token",
    [
        "/me/events?$skiptoken=abc",
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
        lambda tools, message_id: tools.outlook_mail_send_draft(message_id),
        lambda tools, message_id: tools.outlook_mail_reply(message_id, "Thanks"),
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


def test_send_draft_posts_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_send_draft(MESSAGE_ID))

    [request] = graph.requests
    assert (request.method, unquote(request.url.path)) == ("POST", f"{MESSAGE_PATH}/send")
    assert request.headers["content-length"] == "0"
    assert result == {"message_id": MESSAGE_ID, "sent": True, "status": "ok", "tool": "outlook_mail"}


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


@pytest.mark.parametrize(("reply_all", "action"), [(False, "reply"), (True, "replyAll")])
def test_reply_sends_the_body_as_an_escaped_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply_all: bool,
    action: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.outlook_mail_reply(MESSAGE_ID, "Thanks <team> & all\r\nSee you", reply_all=reply_all))

    [request] = graph.requests
    assert (request.method, unquote(request.url.path)) == ("POST", f"{MESSAGE_PATH}/{action}")
    assert _body(request) == {"comment": "Thanks &lt;team&gt; &amp; all<br>See you"}
    assert result["sent"] is True


@pytest.mark.parametrize(("body", "reply_all"), [("", False), ("  ", False), (None, False), ("ok", "yes")])
def test_reply_refuses_malformed_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: object,
    reply_all: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.outlook_mail_reply(MESSAGE_ID, body, reply_all=reply_all))["code"] == "invalid_argument"  # type: ignore[arg-type]
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
