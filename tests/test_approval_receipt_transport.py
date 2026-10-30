"""Large argument evidence remains usable after approval metadata is added."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.approval.transport import _offload_oversized_full_arguments
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
    encrypted_file = {
        "url": "mxc://example.org/arguments",
        "key": {"k": "key"},
        "iv": "iv",
        "hashes": {"sha256": "hash"},
        "v": "v2",
        "size": 53_000,
        "mimetype": "application/json",
    }
    upload = AsyncMock(return_value=("mxc://example.org/arguments", encrypted_file))
    monkeypatch.setattr("mindroom.approval.transport.upload_json_sidecar", upload)
    client = MagicMock(spec=nio.AsyncClient)

    prepared = await _offload_oversized_full_arguments(client, "!room:example.org", content)

    # Encrypted in every room: the card is sent later, perhaps after the room turned encrypted.
    upload.assert_awaited_once_with(client, "!room:example.org", arguments, room_encrypted=True)
    assert "full_arguments" not in prepared
    assert prepared["full_arguments_file"] == encrypted_file
    assert "full_arguments_url" not in prepared
    assert prepared["approvable"] is True
    assert content_fits_normal_event({**prepared, "approval_provenance": {"evidence": "x" * 4_000}})
