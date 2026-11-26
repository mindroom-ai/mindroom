"""Exercise native comms dispatch and credential boundaries without contacting providers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tools.aws_ses import aws_ses_tools
from mindroom.tools.discord import discord_tools
from mindroom.tools.email import email_tools
from mindroom.tools.resend import resend_tools
from mindroom.tools.telegram import telegram_tools
from mindroom.tools.twilio import twilio_tools
from mindroom.tools.webex import webex_tools
from mindroom.tools.whatsapp import whatsapp_tools
from mindroom.tools.x import x_tools
from mindroom.tools.zoom import zoom_tools

if TYPE_CHECKING:
    from pathlib import Path


_CREDENTIAL = "fixture-credential"
_TELEGRAM_KEY = "0:fixture-credential"


def test_smtp_sends_only_to_configured_recipient() -> None:
    """SMTP constructs a plain-text message for the operator's recipient."""
    tools = email_tools()("reader@example.test", "Sender", "sender@example.test", "fake-passkey")
    with patch("smtplib.SMTP_SSL") as smtp_ssl:
        assert tools.smtp_send_email("Status", "Ready") == "Email sent."
    smtp = smtp_ssl.return_value.__enter__.return_value
    smtp.login.assert_called_once_with("sender@example.test", "fake-passkey")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "reader@example.test"
    assert message.get_content().strip() == "Ready"
    assert not email_tools()(enable_email_user=False).functions


def test_resend_uses_each_toolkits_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending for one toolkit does not change the next toolkit's bearer key."""
    pytest.importorskip("resend")
    monkeypatch.setenv("RESEND_API_KEY", "environment-example")
    first = resend_tools()(api_key="explicit-example", from_email="sender@example.test")
    second = resend_tools()(from_email="other@example.test")
    with patch("resend.RequestsClient.request", return_value=(b"{}", 200, {})) as request:
        assert first.send_email("reader@example.test", "Status", "<b>Ready</b>") == "Email sent."
        assert second.send_email("reader@example.test", "Status", "Hello") == "Email sent."
    assert [args.kwargs["headers"]["Authorization"] for args in request.call_args_list] == [
        "Bearer explicit-example",
        "Bearer environment-example",
    ]
    assert request.call_args_list[0].kwargs["json"]["html"] == "<b>Ready</b>"


def test_ses_sends_via_configured_region() -> None:
    """SES uses the configured region and the SDK's normal credential chain."""
    pytest.importorskip("boto3")
    tools = aws_ses_tools()(sender_email="sender@example.test", region_name="us-west-2")
    with patch("boto3.client") as client:
        assert tools.send_email("Status", "Ready", "reader@example.test") == "Email sent."
    client.assert_called_once_with("ses", region_name="us-west-2")
    assert client.return_value.send_email.call_args.kwargs["Destination"] == {"ToAddresses": ["reader@example.test"]}


def test_twilio_separates_sms_from_reads() -> None:
    """A disabled read action cannot call Twilio even through the consolidated method."""
    pytest.importorskip("twilio")
    tools = twilio_tools()(
        account_sid="ACexample",
        api_key="SKexample",
        api_secret=_CREDENTIAL,
        enable_list_messages=False,
    )
    with patch("twilio.rest.Client") as client:
        assert "error" in tools.twilio_read("list_messages")
        client.assert_not_called()
        client.return_value.calls.return_value.fetch.return_value = SimpleNamespace(
            to="+12025550101",
            _from="+12025550102",
            status="completed",
            duration="60",
            direction="inbound",
            price=None,
            start_time=None,
            end_time=None,
        )
        assert tools.twilio_read("get_call_details", call_sid="CAexample")["from"] == "+12025550102"
        client.assert_called_once_with("SKexample", _CREDENTIAL, "ACexample", region=None, edge=None)


def test_discord_routes_reads_sends_and_deletes_separately() -> None:
    """Read, send, and delete go to their own REST routes and approval names."""
    tools = discord_tools()(bot_token=_CREDENTIAL)
    calls: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bot {_CREDENTIAL}"
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            assert json.loads(request.content) == {"content": "Ready"}
            return httpx.Response(200, json={"id": "99"})
        return httpx.Response(200, json=[{"id": "99"}])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch.object(httpx, "request", client.request):
        assert json.loads(tools.discord_read("get_channel_messages", channel_id="12")) == [{"id": "99"}]
        tools.discord_send("12", "Ready")
        tools.discord_manage("12", "99")
    assert calls == [
        ("GET", "/api/v10/channels/12/messages"),
        ("POST", "/api/v10/channels/12/messages"),
        ("DELETE", "/api/v10/channels/12/messages/99"),
    ]


def test_telegram_opt_in_media_is_absent_from_default_schema() -> None:
    """Disabled media never reaches the Telegram bot, even with a direct action dispatch."""
    pytest.importorskip("telebot")
    with patch("telebot.TeleBot") as bot:
        tools = telegram_tools()(token=_TELEGRAM_KEY, chat_id="1234")
        [send] = adapt_toolkit(tools).functions
        assert send.name == "telegram_send"
        assert send.parameters["properties"]["action"]["enum"] == ["message"]
        assert set(send.parameters["properties"]) == {"action", "message"}
        assert "disabled" in tools.telegram_send("document", media_base64="SGVsbG8=", filename="example.txt")
        bot.return_value.send_document.assert_not_called()


def test_telegram_download_stays_in_configured_directory(tmp_path: Path) -> None:
    """An untrusted remote path cannot overwrite a local file or expose the bot token."""
    pytest.importorskip("telebot")
    existing = tmp_path / "example.txt"
    existing.write_text("untouched", encoding="utf-8")
    with patch("telebot.TeleBot") as bot:
        bot.return_value.get_file.return_value = SimpleNamespace(
            file_path="../example.txt",
            file_id="abc",
            file_size=5,
        )
        bot.return_value.download_file.return_value = b"Hello"
        tools = telegram_tools()(token=_TELEGRAM_KEY, output_directory=str(tmp_path), enable_get_file=True)
        result = json.loads(tools.telegram_read("get_file", file_id="abc"))
    assert existing.read_text(encoding="utf-8") == "untouched"
    saved = tmp_path / result["saved_filename"]
    assert saved.parent == tmp_path
    assert saved.read_bytes() == b"Hello"
    assert _TELEGRAM_KEY not in json.dumps(result)


def test_whatsapp_enabled_actions_constrain_schema_and_send() -> None:
    """Default schemas hide opt-in actions and parameters; calls block them too."""
    tools = whatsapp_tools()(access_token=_CREDENTIAL, phone_number_id="123", recipient_waid="12025550101")
    [send] = adapt_toolkit(tools).functions
    props = send.parameters["properties"]
    assert props["action"]["enum"] == ["text", "template"]
    assert set(props) == {"action", "recipient", "text", "preview_url", "template_name", "language_code", "components"}
    assert "media_id" not in (send.description or "")
    with patch.object(httpx, "post") as post:
        assert "disabled" in tools.whatsapp_send("image", media_id="example")
        post.assert_not_called()
        post.return_value = httpx.Response(
            200,
            json={"messages": [{"id": "wamid.example"}]},
            request=httpx.Request("POST", "https://graph.facebook.com"),
        )
        assert json.loads(tools.whatsapp_send("text", text="Ready"))["message_id"] == "wamid.example"
        assert post.call_args.kwargs["json"]["to"] == "12025550101"


def test_webex_read_and_send_use_separate_functions() -> None:
    """Listing room IDs does not send a message."""
    pytest.importorskip("webexpythonsdk")
    with patch("webexpythonsdk.WebexAPI") as api:
        api.return_value.rooms.list.return_value = [
            SimpleNamespace(id="room-id", title="Example", type="group", isPublic=False, isReadOnly=False),
        ]
        tools = webex_tools()(access_token=_CREDENTIAL)
        assert json.loads(tools.webex_read())["rooms"][0]["id"] == "room-id"
        api.return_value.messages.create.assert_not_called()
        api.return_value.messages.create.return_value = SimpleNamespace(json_data={"id": "message-id"})
        assert json.loads(tools.webex_send("room-id", "Ready"))["id"] == "message-id"
        api.return_value.messages.create.assert_called_once_with(roomId="room-id", text="Ready")


def test_zoom_mints_private_token_and_reuses_it_for_meetings() -> None:
    """Zoom OAuth never appears as a callable or in a successful tool result."""
    tools = zoom_tools()("account-id", "client-id", "fake-secret")
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.path == "/oauth/token":
            assert request.headers["Authorization"].startswith("Basic ")
            assert b"account_id=account-id" in request.content
            return httpx.Response(200, json={"access_token": "fake-zoom-bearer", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fake-zoom-bearer"
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": 12, "topic": "Review", "access_token": "fake-zoom-bearer"})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        patch.object(httpx, "post", client.post),
        patch.object(httpx, "request", client.request),
    ):
        meeting = tools.zoom_read("get_meeting", meeting_id="12")
        deleted = tools.zoom_manage("delete_meeting", meeting_id="12")
    assert json.loads(meeting)["topic"] == "Review"
    assert json.loads(deleted)["meeting_id"] == "12"
    assert "fake-zoom-bearer" not in meeting + deleted
    assert hosts == ["zoom.us", "api.zoom.us", "api.zoom.us"]
    assert set(tools.functions) == {"zoom_read", "zoom_manage"}


def test_x_acknowledges_post_and_direct_message() -> None:
    """X reports successful writes without a second API call to look up the sender."""
    pytest.importorskip("tweepy")
    with patch("tweepy.Client") as api:
        api.return_value.create_tweet.return_value = SimpleNamespace(data={"id": "5"})
        api.return_value.create_direct_message.return_value = SimpleNamespace(data={"dm_event_id": "8"})
        tools = x_tools()()
        assert json.loads(tools.x_write("create_post", text="Ready"))["url"].endswith("/5")
        assert json.loads(tools.x_write("send_dm", text="Ready", recipient="42"))["dm_id"] == "8"
    api.return_value.get_me.assert_not_called()
    api.return_value.create_direct_message.assert_called_once_with(participant_id="42", text="Ready")
