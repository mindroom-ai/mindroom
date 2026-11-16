"""An agent asks for a temporary secret with a card: the answer reaches its shell commands, never the tool or the model."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import secret_request_cards
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.temporary_secrets import TemporarySecretsTools
from mindroom.message_target import MessageTarget
from mindroom.secret_requests import (
    SECRET_REQUEST_CONTENT_KEY,
    TemporaryTarget,
    _SecretHolder,
    secret_holder,
    secret_request_registry,
)
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock, make_relation_lookup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.secret_requests import SecretKeep

_ALICE = "@alice:example.org"
_ROOM = "!room:example.org"
_CARD = "$card"
_REASON = "to restart the web server you asked about"
# MindRoom Chat may call this MindRoom to answer a card.
_ENV = {
    "MINDROOM_NAMESPACE": "",
    "MINDROOM_COMPUTER_ALLOWED_ORIGINS": '["https://chat.example.org"]',
    "MINDROOM_PUBLIC_URL": "https://mindroom.example.org",
}


@pytest.fixture(autouse=True)
def _homeserver_binds_openid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The homeserver binds OpenID tokens, so Chat can sign in to this MindRoom with a client session."""
    monkeypatch.setattr(secret_request_cards, "homeserver_openid_binding", AsyncMock(return_value=True))


@pytest.fixture(autouse=True)
def _forget_secret_requests() -> Iterator[None]:
    """Leave the process-wide registry as each test found it."""
    before = set(secret_request_registry()._requests)
    yield
    for request_id in set(secret_request_registry()._requests) - before:
        secret_request_registry().discard(request_id)


def _asking(tmp_path: Path, **env: str) -> tuple[TemporarySecretsTools, ToolRuntimeContext]:
    """Return the tool and Alice's turn on agent `code` in thread `$thread`."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={**_ENV, **env},
    )
    agent = AgentConfig(display_name="Code", tools=["shell", "temporary_secrets"], include_default_tools=False)
    config = Config(agents={"code": agent})
    client = make_matrix_client_mock(user_id="@mindroom_code:example.org")
    client.room_send.return_value = nio.RoomSendResponse(_CARD, _ROOM)
    context = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id=_ROOM, thread_id="$thread", reply_to_event_id="$request"),
        requester_id=_ALICE,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        plugins=config_plugins(config, runtime_paths),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return TemporarySecretsTools(runtime_paths=runtime_paths), context


def _start(
    tool: TemporarySecretsTools,
    context: ToolRuntimeContext | None,
    name: str = "SUDO_PASSWORD",
    reason: str = _REASON,
    **shown: str,
) -> asyncio.Task[str]:
    with tool_runtime_context(context):
        return asyncio.create_task(tool.request_temporary_secret(name, reason, **shown))


def _sent(context: ToolRuntimeContext) -> list[dict[str, object]]:
    """Return the content of every event the tool sent, in order."""
    return [call.kwargs["content"] for call in context.client.room_send.await_args_list]


async def _posted_card(context: ToolRuntimeContext, task: asyncio.Task[str]) -> dict[str, object]:
    """Return the card once the tool posted it and waits for the answer (it logs `secret_request_posted` then)."""
    with capture_logs() as logs:
        while not task.done() and not any(entry["event"] == "secret_request_posted" for entry in logs):  # noqa: ASYNC110 - the tool logs from its own task
            await asyncio.sleep(0)
    assert not task.done(), task.result()
    [card] = _sent(context)
    return card


def _request_id(card: dict[str, object]) -> str:
    request = card[SECRET_REQUEST_CONTENT_KEY]
    assert isinstance(request, dict)
    return request["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("keep", "minutes"), [("memory", 15), ("disk", 1440)])
async def test_asks_with_a_card_and_says_how_long_the_secret_is_kept(
    tmp_path: Path,
    keep: SecretKeep,
    minutes: int,
) -> None:
    """The card names the variable and the conversation; the agent learns the variable and its lifetime, no value."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context)
    card = await _posted_card(context, task)
    request_id = _request_id(card)
    pending = secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time())
    assert pending is not None
    # The route records the requester's choice before it tells the tool.
    pending.kept = keep
    secret_request_registry().resolve(request_id, "saved")
    raw = await task
    result = json.loads(raw)

    holder = _SecretHolder(agent_name="code", room_id=_ROOM, thread_id="$thread", requester_id=_ALICE)
    assert pending.target == TemporaryTarget(name="SUDO_PASSWORD", holder=holder)
    assert holder == secret_holder(context.execution_identity)
    assert (pending.requester_id, pending.agent_name) == (_ALICE, "code")
    assert card["msgtype"] == "m.notice"
    assert card["body"] == "🔑 Code asks for a secret (SUDO_PASSWORD). Open MindRoom Chat to enter it."
    assert card["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": "$request"},
    }
    content = card[SECRET_REQUEST_CONTENT_KEY]
    assert content == {
        "request_id": request_id,
        "requester_id": _ALICE,
        "env": "SUDO_PASSWORD",
        "reason": _REASON,
        "status": "pending",
        "expires_at": int(pending.expires_at * 1000),
        "keep": {"memory": 900, "disk": 86400},
    }
    _card, edit = _sent(context)
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": _CARD}
    assert edit["m.new_content"]["body"] == "🔑 Secret received."
    assert edit["m.new_content"][SECRET_REQUEST_CONTENT_KEY] == {**content, "status": "saved", "kept": keep}
    assert raw == json.dumps(result, sort_keys=True)
    assert set(result) == {"status", "name", "kept", "minutes", "note"}
    assert (result["status"], result["name"], result["kept"], result["minutes"]) == (
        "provided",
        "SUDO_PASSWORD",
        keep,
        minutes,
    )
    for part in ("$SUDO_PASSWORD", f"{minutes} minutes", "printf '%s\\n' \"$SUDO_PASSWORD\"", "Never print"):
        assert part in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "note"),
    [("declined", "do not ask again"), ("expired", "No answer came in 15 minutes")],
)
async def test_a_decline_or_no_answer_gives_the_agent_nothing(tmp_path: Path, outcome: str, note: str) -> None:
    """The agent learns the requester declined or never answered, and the card says so."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context)
    request_id = _request_id(await _posted_card(context, task))
    secret_request_registry().resolve(request_id, outcome)
    result = json.loads(await task)

    assert set(result) == {"status", "name", "note"}
    assert (result["status"], result["name"]) == (outcome, "SUDO_PASSWORD")
    assert note in result["note"]
    _card, edit = _sent(context)
    assert edit["m.new_content"][SECRET_REQUEST_CONTENT_KEY]["status"] == outcome
    assert "kept" not in edit["m.new_content"][SECRET_REQUEST_CONTENT_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "name", "reason", "message"),
    [
        ("no-conversation", "SUDO_PASSWORD", _REASON, "No conversation"),
        ("not-a-person", "SUDO_PASSWORD", _REASON, "Only a person can answer a card"),
        ("empty-name", "", _REASON, "environment variable"),
        ("starts-with-a-digit", "1PASSWORD", _REASON, "environment variable"),
        ("dash", "SUDO-PASSWORD", _REASON, "environment variable"),
        ("space", "SUDO PASSWORD", _REASON, "environment variable"),
        ("assignment", "A=B", _REASON, "environment variable"),
        ("too-long", "A" * 65, _REASON, "environment variable"),
        ("trailing-newline", "SUDO_PASSWORD\n", _REASON, "environment variable"),
        ("empty-reason", "SUDO_PASSWORD", "  \n", "reason"),
    ],
)
async def test_refuses_without_posting(tmp_path: Path, case: str, name: str, reason: str, message: str) -> None:
    """No person to ask, a name no shell variable can have, or no reason posts no card."""
    tool, context = _asking(tmp_path)
    if case == "not-a-person":
        # A bot account asks: no person could answer its card.
        context = replace(
            context,
            requester_id="@bridge-bot:example.org",
            config=context.config.model_copy(update={"bot_accounts": ["@bridge-bot:example.org"]}),
        )

    result = json.loads(await _start(tool, None if case == "no-conversation" else context, name, reason))

    assert set(result) == {"status", "message"}
    assert result["status"] == "error"
    assert message in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_card_shows_a_label_a_link_and_a_field_hint(tmp_path: Path) -> None:
    """What the agent calls the secret, where the person gets it and what the empty field hints are on the card."""
    tool, context = _asking(tmp_path)

    task = _start(
        tool,
        context,
        "GITHUB_TOKEN",
        label="  GitHub\n token ",
        url=" https://github.com/settings/tokens?type=beta ",
        placeholder="ghp_…",
    )
    card = await _posted_card(context, task)
    secret_request_registry().resolve(_request_id(card), "declined")
    await task

    content = card[SECRET_REQUEST_CONTENT_KEY]
    assert (content["label"], content["url"], content["placeholder"]) == (
        "GitHub token",
        "https://github.com/settings/tokens?type=beta",
        "ghp_…",
    )
    # A client without the card reads what is asked and where to get it in the plain body.
    assert card["body"] == (
        "🔑 Code asks for a secret (GitHub token, GITHUB_TOKEN). Open MindRoom Chat to enter it. "
        "Where to get it: https://github.com/settings/tokens?type=beta"
    )
    _card, edit = _sent(context)
    assert edit["m.new_content"][SECRET_REQUEST_CONTENT_KEY] == {**content, "status": "declined"}
    assert edit["m.new_content"]["body"] == "🔑 Secret request declined."


@pytest.mark.asyncio
async def test_a_long_label_or_field_hint_is_cut(tmp_path: Path) -> None:
    """A label and a hint are one short line each, so a card stays a card."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context, label="L" * 200, placeholder="P" * 200)
    content = (await _posted_card(context, task))[SECRET_REQUEST_CONTENT_KEY]
    secret_request_registry().resolve(content["request_id"], "declined")
    await task

    assert (content["label"], content["placeholder"]) == ("L" * 80, "P" * 80)
    assert "url" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/keys",
        "https://[::1]:8443/token",
        "https://例え.example/トークン?kind=new#form",
    ],
)
async def test_a_card_takes_any_http_or_https_page(tmp_path: Path, url: str) -> None:
    """`http`, a port, an IPv6 host and a name outside ASCII are all pages a card's button can open."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context, url=url)
    content = (await _posted_card(context, task))[SECRET_REQUEST_CONTENT_KEY]
    secret_request_registry().resolve(content["request_id"], "declined")
    await task

    assert content["url"] == url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "github.com/settings/tokens",
        "ftp://github.com/settings/tokens",
        "javascript:alert(1)",
        "https://",
        "https:///settings/tokens",
        "https://ada:secret@github.com/",
        "https://github.com/settings tokens",
        "https://github.com/\x00",
        "https://github.com:99999/",
        pytest.param("https://github.com/" + "a" * 2000, id="too-long"),
    ],
)
async def test_refuses_a_link_no_card_can_show(tmp_path: Path, url: str) -> None:
    """A link is an `http` or `https` page with a host and no sign-in part: anything else posts no card."""
    tool, context = _asking(tmp_path)

    result = json.loads(await _start(tool, context, url=url))

    assert set(result) == {"status", "message"}
    assert result["status"] == "error"
    assert "`url`" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("no-chat-origin", "MINDROOM_COMPUTER_ALLOWED_ORIGINS"),
        ("no-openid-audience", "MINDROOM_PUBLIC_URL"),
        ("no-openid-binding", "homeserver binds OpenID tokens"),
    ],
)
async def test_refuses_when_chat_cannot_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    """Without Chat allowed to call this MindRoom with a client session, nobody could answer, so no card is posted."""
    if case == "no-openid-binding":
        monkeypatch.setattr(secret_request_cards, "homeserver_openid_binding", AsyncMock(return_value=False))
    if case == "no-openid-audience":
        monkeypatch.setattr(secret_request_cards, "openid_audience", lambda _paths: None)
    origins = "[]" if case == "no-chat-origin" else '["https://chat.example.org"]'
    tool, context = _asking(tmp_path, MINDROOM_COMPUTER_ALLOWED_ORIGINS=origins)

    result = json.loads(await _start(tool, context))

    assert result["status"] == "error"
    assert message in result["message"]
    assert "Agent Keys" not in result["message"]
    assert ("send a secret here" in result["message"]) is (case != "no-chat-origin")
    context.client.room_send.assert_not_awaited()


def test_the_tool_is_one_an_agent_can_list(tmp_path: Path) -> None:
    """`temporary_secrets` is a built-in tool with the one function, run in the primary where the answer arrives."""
    _tool, context = _asking(tmp_path)

    toolkit = get_tool_by_name(
        "temporary_secrets",
        context.runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert isinstance(toolkit, TemporarySecretsTools)
    functions = adapt_toolkit(toolkit, key="temporary_secrets").functions
    assert [function.name for function in functions] == ["request_temporary_secret"]
