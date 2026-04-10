"""Tool-approval REST and WebSocket API."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette import status

from mindroom.logging_config import get_logger
from mindroom.tool_approval import get_approval_store

router = APIRouter(prefix="/api/approvals", tags=["approvals"])
websocket_router = APIRouter(tags=["approvals"])
logger = get_logger(__name__)


class DenyApprovalRequest(BaseModel):
    """Optional deny reason payload."""

    reason: str | None = None


def _resolved_by_user_id(request: Request) -> str | None:
    auth_user = request.scope.get("auth_user")
    if not isinstance(auth_user, dict):
        return None
    user_id = auth_user.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def _http_error_for_resolve_failure(
    approval_id: str,
    exc: LookupError | ValueError,
) -> HTTPException:
    store = get_approval_store()
    if isinstance(exc, LookupError) or store is None:
        return HTTPException(status_code=404, detail=f"Approval request '{approval_id}' was not found.")

    request = store.get_request(approval_id)
    if request is None:
        return HTTPException(status_code=404, detail=f"Approval request '{approval_id}' was not found.")
    return HTTPException(
        status_code=409,
        detail={
            "message": f"Approval request '{approval_id}' is already {request.status}.",
            "status": request.status,
        },
    )


@router.get("")
async def list_approvals() -> list[dict[str, Any]]:
    """Return pending approvals ordered by creation time."""
    store = get_approval_store()
    if store is None:
        return []
    return store.list_pending_records()


@router.post("/{approval_id}/approve")
async def approve_approval(
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    """Approve one pending tool call."""
    store = get_approval_store()
    if store is None:
        raise HTTPException(status_code=404, detail=f"Approval request '{approval_id}' was not found.")
    try:
        approval = await store.approve(approval_id, resolved_by=_resolved_by_user_id(request))
    except (LookupError, ValueError) as exc:
        raise _http_error_for_resolve_failure(approval_id, exc) from exc
    return approval.to_dict()


@router.post("/{approval_id}/deny")
async def deny_approval(
    approval_id: str,
    payload: DenyApprovalRequest,
    request: Request,
) -> dict[str, Any]:
    """Deny one pending tool call."""
    store = get_approval_store()
    if store is None:
        raise HTTPException(status_code=404, detail=f"Approval request '{approval_id}' was not found.")
    try:
        approval = await store.deny(
            approval_id,
            reason=payload.reason,
            resolved_by=_resolved_by_user_id(request),
        )
    except (LookupError, ValueError) as exc:
        raise _http_error_for_resolve_failure(approval_id, exc) from exc
    return approval.to_dict()


@websocket_router.websocket("/api/approvals/ws")
async def approvals_websocket(websocket: WebSocket) -> None:
    """Stream snapshots and live approval updates to authenticated dashboard clients."""
    from mindroom.api import main as api_main  # noqa: PLC0415

    try:
        await api_main.authenticate_websocket_user(websocket)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    except Exception:
        logger.exception("Approvals WebSocket authentication failed")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    store = get_approval_store()
    await websocket.accept()
    if store is None:
        await websocket.send_json({"type": "snapshot", "approvals": []})
        return

    queue = store.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "approvals": store.list_pending_records()})
        while True:
            queue_task = asyncio.create_task(queue.get())
            receive_task = asyncio.create_task(websocket.receive())
            done, pending = await asyncio.wait(
                {queue_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_task

            if receive_task in done:
                message = receive_task.result()
                if message["type"] == "websocket.disconnect":
                    return
                continue

            await websocket.send_json(queue_task.result())
    except WebSocketDisconnect:
        return
    finally:
        store.unsubscribe(queue)
