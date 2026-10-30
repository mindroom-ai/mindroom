"""Large argument evidence remains usable after approval metadata is added."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.approval.transport import _offload_oversized_full_arguments
from mindroom.matrix.client_delivery import MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.large_messages import content_fits_normal_event


@pytest.mark.asyncio
async def test_timed_card_offloads_complete_arguments_before_receipt_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-limit original must leave space for durable timed decision evidence."""
    arguments = {"command": "x" * 52_000}
    content = {
        "arguments": {"command": "preview"},
        "arguments_truncated": True,
        "full_arguments": arguments,
        "auto_approve_options": [300, 600, 1800],
        "approvable": True,
    }
    assert content_fits_normal_event(content)
    upload = AsyncMock(return_value=("mxc://example.org/arguments", {"size": 53_000, "mimetype": "application/json"}))
    monkeypatch.setattr(
        "mindroom.approval.transport.resolve_room_encryption_outcome",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("mindroom.approval.transport.upload_json_sidecar", upload)
    client = MagicMock(spec=nio.AsyncClient)

    prepared = await _offload_oversized_full_arguments(client, "!room:example.org", content)

    upload.assert_awaited_once_with(client, "!room:example.org", arguments, room_encrypted=False)
    assert "full_arguments" not in prepared
    assert prepared["full_arguments_url"] == "mxc://example.org/arguments"
    assert prepared["approvable"] is True
    assert content_fits_normal_event({**prepared, "approval_provenance": {"evidence": "x" * 4_000}})


@pytest.mark.asyncio
async def test_unknown_room_encryption_keeps_arguments_off_the_media_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arguments are never uploaded when the room's encryption cannot be determined."""
    content = {"arguments": {"command": "preview"}, "full_arguments": {"command": "x" * 70_000}, "approvable": True}
    upload = AsyncMock()
    monkeypatch.setattr(
        "mindroom.approval.transport.resolve_room_encryption_outcome",
        AsyncMock(
            return_value=MatrixDeliveryFailure(MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE, "unknown"),
        ),
    )
    monkeypatch.setattr("mindroom.approval.transport.upload_json_sidecar", upload)

    prepared = await _offload_oversized_full_arguments(MagicMock(spec=nio.AsyncClient), "!room:example.org", content)

    upload.assert_not_awaited()
    assert prepared == {"arguments": {"command": "preview"}, "approvable": False}
