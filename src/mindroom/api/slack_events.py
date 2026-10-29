"""Public `POST /api/slack/events` route: Slack's signed Events API requests, answered within Slack's 3 s limit."""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.background_tasks import create_background_task
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.slack.app import load_slack_app
from mindroom.slack.delivery import SlackRuntime, deliver_slack_event, retry_slack_event
from mindroom.slack.events import verify_slack_signature

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.slack.app import SlackAppCredentials
    from mindroom.slack.turns import DeliveryResult

router = APIRouter(prefix="/api/slack", tags=["slack"])
_MAX_BODY_BYTES = 1_048_576
# Slack drops an answer slower than 3 s; a delivery still running by then finishes in the background.
_ACK_BUDGET_SECONDS = 2.5
_UNAVAILABLE = "Slack events are not available"
# The integration's name, which keys its runtime in the app state.
_RUNTIME_KEY = "slack"


def _bound_slack_runtime(api_app: FastAPI) -> SlackRuntime | None:
    """Return the runtime Slack events reach, or None while it is unbound."""
    runtime = config_lifecycle.app_state(api_app).integration_runtimes.get(_RUNTIME_KEY)
    return runtime if isinstance(runtime, SlackRuntime) else None


def bind_slack_runtime(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot | TeamBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
) -> None:
    """Let Slack events reach the orchestrator's live bots; binding the same bots again keeps the runtime."""
    runtime = _bound_slack_runtime(api_app)
    if runtime is None or runtime.bots is not bots:
        config_lifecycle.app_state(api_app).integration_runtimes[_RUNTIME_KEY] = SlackRuntime(
            bots,
            agent_reply_memberships,
        )


def unbind_slack_runtime(api_app: FastAPI) -> object | None:
    """Refuse Slack events, so Slack resends them after the restart, and return the runtime that owns the deliveries."""
    return config_lifecycle.app_state(api_app).integration_runtimes.pop(_RUNTIME_KEY, None)


@router.post("/events")
async def post_slack_event(request: Request) -> Response:
    """Verify one Slack request, answer `url_verification`, and deliver an event to the agents it is for.

    A `503` makes Slack send the event again; anything delivered by then is not delivered twice.
    """
    body = await _read_body(request)
    try:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    app = await asyncio.to_thread(load_slack_app, credentials_manager)
    if app is None:
        raise HTTPException(status_code=404, detail="Not Found")
    runtime = _bound_slack_runtime(request.app)
    if runtime is None:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    if not verify_slack_signature(
        app.signing_secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
        now=int(time.time()),
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    payload = _json_object(body)
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    attempt = create_background_task(
        _deliver_now(payload, runtime, config, runtime_paths, credentials_manager, app),
        name="slack_event_delivery",
        owner=runtime,
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(attempt), _ACK_BUDGET_SECONDS)
    except TimeoutError:
        create_background_task(
            retry_slack_event(
                attempt,
                payload,
                runtime=runtime,
                current_config=partial(_committed_config, request.app),
                runtime_paths=runtime_paths,
                credentials_manager=credentials_manager,
            ),
            name="slack_event_retry",
            owner=runtime,
        )
        return Response(status_code=200)
    if result == "retry":
        raise HTTPException(status_code=503, detail="Slack event delivery will be retried")
    return Response(status_code=200)


async def _deliver_now(
    payload: dict[str, Any],
    runtime: SlackRuntime,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    app: SlackAppCredentials,
) -> DeliveryResult:
    """Deliver with a Slack client of the task's own, which outlives the request."""
    async with runtime.http_client() as client:
        return await deliver_slack_event(
            payload,
            runtime=runtime,
            config=config,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            app=app,
            client=client,
            now=int(time.time()),
        )


def _committed_config(api_app: FastAPI) -> Config | None:
    """Return the config committed now, which a retry long after the request must follow, or None without one."""
    try:
        return config_lifecycle.read_app_committed_runtime_config(api_app)[0]
    except HTTPException:
        return None


async def _read_body(request: Request) -> bytes:
    try:
        return await collect_bounded_bytes(request.stream(), max_bytes=_MAX_BODY_BYTES)
    except ByteLimitExceededError as exc:
        raise HTTPException(status_code=413, detail="Slack event body is too large") from exc


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Slack event body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Slack event body must be a JSON object")
    return payload
