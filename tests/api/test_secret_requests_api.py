"""The requester answers an agent's secret request from Chat: the key lands in the agent's Agent Keys scope.

Alice and Bob use ``code``, a personal Agent Keys agent, and ``team``, a shared one whose keys Alice manages. Each
request is Alice's, as the tool registers it, and only she may answer it while it is pending.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import nio
import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import main, secret_requests
from mindroom.api.client_sessions import client_session_store
from mindroom.api.validation_errors import validation_error_response
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.custom_tools import egress_credentials as egress_credentials_module
from mindroom.custom_tools.egress_credentials import EgressCredentialsTools
from mindroom.egress_broker.secrets import load_secret, save_secret
from mindroom.message_target import MessageTarget
from mindroom.secret_requests import SECRET_REQUEST_CONTENT_KEY, SECRET_REQUEST_TTL_SECONDS, secret_request_registry
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.api.test_plugin_room_agents import ALICE, BOB, ROOM, _Room, _room
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
)
from tests.egress_broker.conftest import worker_target

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.secret_requests import PendingSecretRequest
    from mindroom.tool_system.worker_routing import WorkerScope

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

SECRET = "ghp-test-secret-value"  # noqa: S105 - test credential
NOT_ACTIVE = "This request is no longer active."
CANNOT_SET = "You can no longer set keys for this agent."
# A static runner's URL places the agents' shell calls in a worker, and the broker's port turns Agent Keys on.
ENV = {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test", "MINDROOM_EGRESS_BROKER_PORT": "8768"}
GITHUB: dict[str, object] = {
    "display_name": "GitHub",
    "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}],
}
TOOLS = ["shell", "egress_credentials"]
# The scope each agent's tool calls run in, which the tool records with the request.
SCOPES: dict[str, WorkerScope | None] = {"code": "user_agent", "team": "shared", "old": None}


def _config(
    *,
    code_users: tuple[str, ...] = (ALICE, BOB),
    team_managers: tuple[str, ...] = (ALICE,),
    services: dict[str, object] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> Config:
    """``code`` for ``code_users``, and ``team`` for Alice and Bob, whose keys ``team_managers`` manage.

    Both use ``services``, by default the GitHub service the requests ask for; ``aliases`` maps people to their bridge
    accounts.
    """
    return Config(
        authorization={"aliases": aliases or {}},
        egress_broker={"services": {"github": GITHUB} if services is None else services},
        agents={
            "code": AgentConfig(
                display_name="Code",
                tools=TOOLS,
                include_default_tools=False,
                private=AgentPrivateConfig(per="user_agent"),
                access=ResponderAccessConfig(users=list(code_users)),
            ),
            "team": AgentConfig(
                display_name="Team",
                tools=TOOLS,
                include_default_tools=False,
                worker_scope="shared",
                credential_managers=list(team_managers),
                access=ResponderAccessConfig(users=[ALICE, BOB]),
            ),
        },
    )


def _answering_room(tmp_path: Path, config: Config, **env: str) -> _Room:
    """The router, Alice, Bob and Carol in ``ROOM`` with ``config``'s agents, on an app serving it and the route."""
    room = _room(tmp_path, dict(config.agents), **ENV, **env)
    room.serve(config)
    room.app.include_router(secret_requests.router)
    room.app.add_exception_handler(RequestValidationError, validation_error_response)
    return room


@pytest.fixture
def room(tmp_path: Path) -> _Room:
    """The router, Alice, Bob and Carol in ``ROOM`` with both agents, on an app serving the route."""
    return _answering_room(tmp_path, _config())


@pytest.fixture
def ask() -> Iterator[Callable[..., PendingSecretRequest]]:
    """Register Alice's requests in the process-wide registry as the tool does, and forget them after the test."""
    registry = secret_request_registry()
    registered: list[str] = []

    def ask(agent: str = "code", *, room_id: str = ROOM, now: float | None = None) -> PendingSecretRequest:
        request = registry.register(
            room_id=room_id,
            requester_id=ALICE,
            agent_name=agent,
            service="github",
            worker_target=worker_target(ALICE, agent, scope=SCOPES[agent]),
            hosts=("api.github.com",),
            now=time.time() if now is None else now,
        )
        registered.append(request.request_id)
        return request

    yield ask
    for request_id in registered:
        registry.discard(request_id)


async def _answer(
    room: _Room,
    user_id: str,
    request_id: str,
    body: dict[str, object],
) -> httpx.Response:
    """POST ``body`` as ``user_id``'s answer to ``request_id`` in ``ROOM``, signed in with a client session."""
    token = client_session_store(room.app).create(user_id).token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=room.app), base_url="http://test") as http:
        return await http.post(
            f"/api/rooms/{ROOM}/secret-requests/{request_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


def _stored(room: _Room, request: PendingSecretRequest) -> str | None:
    """The GitHub key stored in the request's Agent Keys scope, if any."""
    stored = load_scoped_credentials(
        "egress_github",
        credentials_manager=get_runtime_credentials_manager(room.paths),
        worker_target=request.worker_target,
        primary_built_tool=True,
    )
    return None if stored is None else stored["secret"]


def test_the_api_serves_the_route() -> None:
    """The main app mounts the route Chat calls."""
    paths = {route.path for route in main.app.routes if isinstance(route, APIRoute)}

    assert "/api/rooms/{room_id}/secret-requests/{request_id}" in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["code", "team"])
async def test_saving_stores_the_key_in_the_agents_scope(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    agent: str,
) -> None:
    """The secret becomes the agent's GitHub key in the request's scope, and the tool learns it was saved."""
    request = ask(agent)

    response = await _answer(room, ALICE, request.request_id, {"secret": SECRET})

    assert response.status_code == 204
    assert _stored(room, request) == SECRET
    assert request.outcome.result() == "saved"


@pytest.mark.asyncio
async def test_decline_resolves_without_storing(room: _Room, ask: Callable[..., PendingSecretRequest]) -> None:
    """A decline stores nothing and tells the tool the requester declined."""
    request = ask()

    response = await _answer(room, ALICE, request.request_id, {"decline": True})

    assert response.status_code == 204
    assert request.outcome.result() == "declined"
    assert _stored(room, request) is None


@pytest.mark.asyncio
async def test_another_member_gets_403(room: _Room, ask: Callable[..., PendingSecretRequest]) -> None:
    """Bob may use the agent but was not asked: his key is refused and Alice can still answer."""
    request = ask()

    response = await _answer(room, BOB, request.request_id, {"secret": "bobs-key"})

    assert (response.status_code, response.json()) == (403, {"detail": "Only the person the agent asked can answer."})
    assert _stored(room, request) is None
    assert not request.outcome.done()
    assert (await _answer(room, ALICE, request.request_id, {"secret": SECRET})).status_code == 204
    assert _stored(room, request) == SECRET


@pytest.mark.asyncio
async def test_unknown_or_expired_request_gets_404(room: _Room, ask: Callable[..., PendingSecretRequest]) -> None:
    """An unknown request, an expired one, and one asked in another room are not active here."""
    expired = ask(now=time.time() - SECRET_REQUEST_TTL_SECONDS - 1)
    elsewhere = ask(room_id="!other:localhost")

    for request_id in ("no-such-request", expired.request_id, elsewhere.request_id):
        response = await _answer(room, ALICE, request_id, {"secret": SECRET})
        assert (response.status_code, response.json()) == (404, {"detail": NOT_ACTIVE})
    assert _stored(room, expired) is None


@pytest.mark.asyncio
async def test_second_answer_gets_404(room: _Room, ask: Callable[..., PendingSecretRequest]) -> None:
    """Once answered, a request takes no other key and no decline."""
    request = ask()
    assert (await _answer(room, ALICE, request.request_id, {"secret": SECRET})).status_code == 204

    for body in ({"secret": "another-key"}, {"decline": True}):
        response = await _answer(room, ALICE, request.request_id, body)
        assert (response.status_code, response.json()) == (404, {"detail": NOT_ACTIVE})
    assert _stored(room, request) == SECRET
    assert request.outcome.result() == "saved"


@pytest.mark.asyncio
async def test_an_answer_while_the_first_saves_gets_404(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second answer sent while the first is still being stored is not active; the key is stored once."""
    request = ask()
    saving, release = threading.Event(), threading.Event()
    saved: list[str] = []
    save_secret = secret_requests.save_secret

    def slow_save(*args: object, **kwargs: object) -> None:
        saved.append(str(args[3]))
        saving.set()
        release.wait(timeout=30)
        save_secret(*args, **kwargs)

    monkeypatch.setattr(secret_requests, "save_secret", slow_save)
    first = asyncio.create_task(_answer(room, ALICE, request.request_id, {"secret": SECRET}))
    try:
        assert await asyncio.to_thread(saving.wait, 30)
        second = await _answer(room, ALICE, request.request_id, {"secret": "another-key"})
    finally:
        release.set()
    first_response = await first

    assert first_response.status_code == 204
    assert (second.status_code, second.json()) == (404, {"detail": NOT_ACTIVE})
    assert saved == [SECRET]
    assert _stored(room, request) == SECRET
    assert request.outcome.result() == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "revised"),
    [
        pytest.param("code", _config(code_users=(BOB,)), id="no-longer-uses-the-agent"),
        pytest.param("team", _config(team_managers=()), id="no-longer-manages-shared-keys"),
    ],
)
async def test_revoked_permission_gets_403_and_stores_nothing(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    agent: str,
    revised: Config,
) -> None:
    """The route checks the config it serves now; a decline still ends the request."""
    request = ask(agent)
    room.serve(revised)

    response = await _answer(room, ALICE, request.request_id, {"secret": SECRET})

    assert (response.status_code, response.json()) == (403, {"detail": CANNOT_SET})
    assert _stored(room, request) is None
    assert not request.outcome.done()
    assert (await _answer(room, ALICE, request.request_id, {"decline": True})).status_code == 204
    assert request.outcome.result() == "declined"


@pytest.mark.asyncio
async def test_a_card_for_a_scope_its_agent_left_takes_no_key(
    tmp_path: Path,
    ask: Callable[..., PendingSecretRequest],
) -> None:
    """Alice may set her personal keys now, but the card was for the global ones, which another agent still uses."""

    def config(old: dict[str, object]) -> Config:
        """``old`` and ``other``, two agents without a worker scope for Alice and Bob; Bob manages ``other``'s keys."""
        agent = {"tools": TOOLS, "include_default_tools": False, "access": {"users": [ALICE, BOB]}}
        return Config(
            egress_broker={"services": {"github": GITHUB}},
            agents={
                "old": AgentConfig.model_validate({**agent, "display_name": "Old", **old}),
                "other": AgentConfig.model_validate({**agent, "display_name": "Other", "credential_managers": [BOB]}),
            },
        )

    room = _answering_room(tmp_path, config({"credential_managers": [ALICE]}))
    manager = get_runtime_credentials_manager(room.paths)
    save_secret(manager, None, "github", "bobs-global-key")
    request = ask("old")
    room.serve(config({"worker_scope": "user_agent"}))

    response = await _answer(room, ALICE, request.request_id, {"secret": SECRET})

    assert (response.status_code, response.json()) == (404, {"detail": NOT_ACTIVE})
    assert request.outcome.result() == "expired"
    assert load_secret(manager, None, "github") == "bobs-global-key"
    assert load_secret(manager, worker_target(ALICE, "old", scope="user_agent"), "github") is None
    assert (await _answer(room, ALICE, request.request_id, {"secret": SECRET})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "services",
    [
        pytest.param({}, id="deleted"),
        pytest.param({"github": {**GITHUB, "admin_key": True}}, id="admin-key"),
        pytest.param(
            {
                "github": {
                    **GITHUB,
                    "github_app": {"credentials_service": "acme_app", "repositories": {"acme/api": "read"}},
                },
            },
            id="keyless",
        ),
        pytest.param({"github": {**GITHUB, "agents": ["team"]}}, id="agent-excluded"),
        pytest.param(
            {"github": {**GITHUB, "rules": [{"host": "api.other.test", "auth": {"type": "bearer"}}]}},
            id="hosts-changed",
        ),
    ],
)
async def test_a_card_for_a_service_that_changed_takes_no_key(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    services: dict[str, object],
) -> None:
    """The key would no longer go where the card said, so the request ends and the agent can ask again."""
    request = ask()
    room.serve(_config(services=services))

    with capture_logs() as logs:
        response = await _answer(room, ALICE, request.request_id, {"secret": SECRET})

    assert (response.status_code, response.json()) == (404, {"detail": NOT_ACTIVE})
    assert request.outcome.result() == "expired"
    assert _stored(room, request) is None
    assert [entry["status"] for entry in logs if entry["event"] == "secret_request_answered"] == ["stale"]
    assert (await _answer(room, ALICE, request.request_id, {"decline": True})).status_code == 404


@pytest.mark.asyncio
async def test_a_turn_from_a_bridge_alias_asks_the_person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn from Alice's bridge account posts a card for Alice herself, and her answer stores the key in her scope."""
    alias = "@telegram_alice:localhost"
    config = _config(aliases={ALICE: [alias]})
    room = _answering_room(tmp_path, config, MINDROOM_COMPUTER_ALLOWED_ORIGINS='["https://chat.example.org"]')
    monkeypatch.setattr(egress_credentials_module, "homeserver_openid_binding", AsyncMock(return_value=True))
    bound = bind_runtime_paths(config, room.paths)
    client = make_matrix_client_mock(user_id=room.ids["code"])
    client.room_send.return_value = nio.RoomSendResponse("$card", ROOM)
    context = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id=ROOM, thread_id="$thread", reply_to_event_id="$request"),
        requester_id=alias,
        client=client,
        config=bound,
        runtime_paths=room.paths,
        plugins=config_plugins(bound, room.paths),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    tool = EgressCredentialsTools(runtime_paths=room.paths, worker_target=worker_target(alias, scope="user_agent"))

    with tool_runtime_context(context):
        task = asyncio.create_task(tool.request_secret("github", "to open the pull request"))
    while not client.room_send.await_count and not task.done():  # noqa: ASYNC110 - the tool sends from its own task
        await asyncio.sleep(0)
    card = client.room_send.await_args_list[0].kwargs["content"][SECRET_REQUEST_CONTENT_KEY]
    response = await _answer(room, ALICE, card["request_id"], {"secret": SECRET})
    result = json.loads(await task)

    assert card["requester_id"] == ALICE
    assert response.status_code == 204
    assert result["status"] == "saved"
    manager = get_runtime_credentials_manager(room.paths)
    assert load_secret(manager, worker_target(ALICE, scope="user_agent"), "github") == SECRET
    assert load_secret(manager, worker_target(alias, scope="user_agent"), "github") is None


@pytest.mark.asyncio
async def test_invalid_secret_gets_422_and_stays_pending(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
) -> None:
    """A value the key store refuses is answered with its reason, never the value, and Alice can try again."""
    request = ask()

    response = await _answer(room, ALICE, request.request_id, {"secret": "a\nb"})

    assert (response.status_code, response.json()) == (
        422,
        {"detail": "Secret contains invalid control character (ord 10)"},
    )
    assert "a\nb" not in response.text
    assert "a\\nb" not in response.text
    assert not request.outcome.done()
    assert (await _answer(room, ALICE, request.request_id, {"secret": SECRET})).status_code == 204
    assert request.outcome.result() == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"secret": SECRET, "decline": True},
        {},
        {"decline": False},
        {"secret": SECRET, "note": "extra"},
    ],
    ids=["both", "neither", "decline-false", "extra-field"],
)
async def test_body_needs_exactly_one_of_secret_or_decline(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    body: dict[str, object],
) -> None:
    """Any other body is a 422 that repeats no value and leaves the request pending."""
    request = ask()

    response = await _answer(room, ALICE, request.request_id, body)

    assert response.status_code == 422
    assert SECRET not in response.text
    assert not request.outcome.done()
    assert _stored(room, request) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "status"), [(SECRET, 204), (f"{SECRET}\x7f", 422)], ids=["saved", "refused"])
async def test_value_never_logged(
    room: _Room,
    ask: Callable[..., PendingSecretRequest],
    value: str,
    status: int,
) -> None:
    """The answer logs one line naming the request, never the value, whether it was saved or refused."""
    request = ask()

    with capture_logs() as logs:
        response = await _answer(room, ALICE, request.request_id, {"secret": value})

    assert response.status_code == status
    assert [entry for entry in logs if SECRET in repr(entry)] == []
    answered = [entry for entry in logs if entry["event"] == "secret_request_answered"]
    assert answered == [
        {
            "event": "secret_request_answered",
            "log_level": "info",
            "request_id": request.request_id,
            "agent": "code",
            "service": "github",
            "status": "saved" if status == 204 else "invalid",
            "user_id": ALICE,
        },
    ]
