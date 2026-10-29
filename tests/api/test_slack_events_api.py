"""API tests for the public Slack Events API route."""

# ruff: noqa: D102, D103

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from mindroom import background_tasks, constants
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import config_lifecycle, slack_events
from mindroom.api import main as api_main
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.integrations import unbind_integrations
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.slack import app as slack_app
from mindroom.slack import identity as slack_identity
from mindroom.slack import turns
from tests.identity_helpers import persist_entity_accounts
from tests.slack.delivery_fakes import (
    AUTH_TEST,
    SLACK_IDS,
    FakeBot,
    FakeMatrix,
    FakeSlack,
    body_of,
    config_data,
    event_payload,
    install_slack_state,
    runtime_for,
    signed_headers,
    slack_user,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.slack.delivery import SlackRuntime

# Slack gives up on an answer after 3 s; the route must answer well before.
_SLACK_TIMEOUT_SECONDS = 3.0
# The budget the tests run with: a delivery still blocked when the route answers proves the route did not wait for it,
# and a fast failure still fits inside it on a loaded machine.
_TEST_BUDGET_SECONDS = 1.0


@dataclass
class EventsApi:
    """The bundled API app with the Slack app stored and a Slack runtime over one fake `launch` bot."""

    paths: constants.RuntimePaths
    slack: FakeSlack
    matrix: FakeMatrix
    runtime: SlackRuntime

    async def post(self, payload: object, *, headers: dict[str, str] | None = None) -> httpx.Response:
        body = payload if isinstance(payload, bytes) else body_of(payload)
        sent_headers = headers if headers is not None else signed_headers(body, timestamp=int(time.time()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app),
            base_url="http://testserver",
        ) as client:
            return await client.post("/api/slack/events", content=body, headers=sent_headers)

    async def timed_post(self, payload: dict[str, Any]) -> tuple[httpx.Response, float]:
        started = time.monotonic()
        response = await self.post(payload)
        return response, time.monotonic() - started


@pytest.fixture(autouse=True)
def _empty_slack_caches() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()
    yield
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()


@pytest.fixture
def events_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[EventsApi]:
    data = config_data()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
    )
    persist_entity_accounts(Config.model_validate(data), paths)
    install_slack_state(paths)
    api_main.initialize_api_app(api_main.app, paths)
    assert config_lifecycle.load_config_into_app(paths, api_main.app).loaded
    slack = FakeSlack(answers={"auth.test": AUTH_TEST}, users={SLACK_IDS["ada"]: slack_user("ada")})
    matrix = FakeMatrix()
    monkeypatch.setattr(turns, "send_matrix_message", matrix.send)
    monkeypatch.setattr(slack_events, "_ACK_BUDGET_SECONDS", _TEST_BUDGET_SECONDS)
    runtime = runtime_for({"launch": FakeBot(AsyncMock())}, AgentReplyMembershipIndex(), slack)
    config_lifecycle.app_state(api_main.app).integration_runtimes["slack"] = runtime
    try:
        yield EventsApi(paths, slack, matrix, runtime)
    finally:
        unbind_integrations(api_main.app)


def test_acknowledgement_budget_leaves_headroom_within_slacks_timeout() -> None:
    """The route waits at most 2.5 s for a delivery, so its answer reaches Slack before Slack's 3 s timeout."""
    assert slack_events._ACK_BUDGET_SECONDS == 2.5 < _SLACK_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_url_verification(events_api: EventsApi) -> None:
    response = await events_api.post({"type": "url_verification", "token": "unused", "challenge": "c0ffee"})

    assert response.status_code == 200
    assert response.json() == {"challenge": "c0ffee"}


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"not json"])
@pytest.mark.asyncio
async def test_body_that_is_not_a_json_object_is_400(events_api: EventsApi, body: bytes) -> None:
    response = await events_api.post(body)

    assert response.status_code == 400
    assert events_api.slack.requests == []


@pytest.mark.asyncio
async def test_bad_signature_401(events_api: EventsApi) -> None:
    body = body_of(event_payload())
    now = int(time.time())
    forged = signed_headers(body, timestamp=now, secret="another-secret")  # noqa: S106
    stale = signed_headers(body, timestamp=now - 301)
    unsigned = {"Content-Type": "application/json"}

    for headers in (forged, stale, unsigned):
        response = await events_api.post(body, headers=headers)
        assert response.status_code == 401

    assert events_api.slack.requests == []
    assert events_api.matrix.sent == []


@pytest.mark.asyncio
async def test_without_the_slack_app_answers_404(events_api: EventsApi) -> None:
    get_runtime_credentials_manager(events_api.paths).delete_credentials("slack_app")

    response = await events_api.post(event_payload())

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_oversized_body_is_413(events_api: EventsApi) -> None:
    body = b'{"type": "event_callback", "pad": "' + b"x" * 1_048_576 + b'"}'

    response = await events_api.post(body)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_unbound_runtime_answers_503(events_api: EventsApi) -> None:
    unbind_integrations(api_main.app)

    response = await events_api.post(event_payload())

    assert response.status_code == 503
    assert events_api.slack.requests == []


@pytest.mark.asyncio
async def test_delivery_answers_200_and_a_fast_failure_503(events_api: EventsApi) -> None:
    events_api.matrix.fail = 1

    assert (await events_api.post(event_payload())).status_code == 503
    assert (await events_api.post(event_payload())).status_code == 200
    assert (await events_api.post(event_payload())).status_code == 200

    assert len(events_api.matrix.sent) == 1
    assert await wait_for_background_tasks(5.0, owner=events_api.runtime)


@pytest.mark.asyncio
async def test_slow_delivery_answers_200_within_budget(events_api: EventsApi) -> None:
    events_api.matrix.gate = asyncio.Event()

    response, elapsed = await events_api.timed_post(event_payload())

    assert response.status_code == 200
    assert elapsed >= _TEST_BUDGET_SECONDS
    assert events_api.matrix.sent == []
    events_api.matrix.gate.set()
    assert await wait_for_background_tasks(5.0, owner=events_api.runtime)
    assert len(events_api.matrix.sent) == 1


@pytest.mark.asyncio
async def test_cold_identity_lookup_stays_within_ack_budget(events_api: EventsApi) -> None:
    """A slow `auth.test` on a cold cache runs inside the timed delivery, so the route still answers in time."""
    released = asyncio.Event()

    async def slow_auth_test(_request: httpx.Request) -> dict[str, Any]:
        await released.wait()
        return AUTH_TEST

    events_api.slack.answers["auth.test"] = slow_auth_test

    response, elapsed = await events_api.timed_post(event_payload())

    assert response.status_code == 200
    assert elapsed >= _TEST_BUDGET_SECONDS
    assert events_api.matrix.sent == []
    released.set()
    assert await wait_for_background_tasks(5.0, owner=events_api.runtime)
    assert len(events_api.matrix.sent) == 1


@pytest.mark.asyncio
async def test_shutdown_drains_pending_delivery(events_api: EventsApi) -> None:
    """Unbinding refuses new events; the drain awaits an acknowledged delivery and cancels one that stays stuck."""
    events_api.matrix.gate = asyncio.Event()
    assert (await events_api.post(event_payload())).status_code == 200
    owned = {task.get_name() for task in background_tasks._tasks_for_owner(events_api.runtime)}
    assert owned == {"slack_event_delivery", "slack_event_retry"}

    assert unbind_integrations(api_main.app) == [("slack", events_api.runtime)]
    runtime = events_api.runtime
    assert (await events_api.post(event_payload(ts="1700000001.000100"))).status_code == 503
    drain = asyncio.create_task(wait_for_background_tasks(5.0, owner=runtime, shutdown_intent=ORDERLY_SHUTDOWN))
    await asyncio.sleep(0)
    events_api.matrix.gate.set()
    assert await drain is True
    assert len(events_api.matrix.sent) == 1

    events_api.matrix.gate = asyncio.Event()
    config_lifecycle.app_state(api_main.app).integration_runtimes["slack"] = runtime
    assert (await events_api.post(event_payload(ts="1700000002.000100"))).status_code == 200
    unbind_integrations(api_main.app)
    with capture_logs() as logs:
        # The orchestrator waits 5 s; the cancellation that follows does not depend on how long.
        assert await wait_for_background_tasks(0.1, owner=runtime, shutdown_intent=ORDERLY_SHUTDOWN) is False

    assert len(events_api.matrix.sent) == 1
    assert any(log["event"] == "background_tasks_wait_timeout" for log in logs)
    assert await wait_for_background_tasks(1.0, owner=runtime)
