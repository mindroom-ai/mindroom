"""Large argument evidence remains usable after approval metadata is added."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.approval_transport import _offload_oversized_full_arguments
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
        "mindroom.approval_transport.resolve_room_encryption_for_delivery",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("mindroom.approval_transport.upload_json_sidecar", upload)
    client = MagicMock(spec=nio.AsyncClient)

    prepared = await _offload_oversized_full_arguments(client, "!room:example.org", content)

    upload.assert_awaited_once_with(client, "!room:example.org", arguments, room_encrypted=False)
    assert "full_arguments" not in prepared
    assert prepared["full_arguments_url"] == "mxc://example.org/arguments"
    assert prepared["approvable"] is True
    assert content_fits_normal_event({**prepared, "approval_provenance": {"evidence": "x" * 4_000}})
