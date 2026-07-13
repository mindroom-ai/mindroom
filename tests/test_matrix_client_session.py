"""Regression coverage for MindRoom's Matrix client session extensions."""

from __future__ import annotations

import nio

from mindroom.matrix.client_session import _MindRoomAsyncClient


def test_explicit_zero_one_time_key_count_requests_replenishment(tmp_path) -> None:  # noqa: ANN001
    """A drained server OTK pool must make nio upload replacement keys."""
    user_id = "@agent:example.org"
    client = _MindRoomAsyncClient(
        "https://example.org",
        user_id,
        device_id="AGENTDEVICE",
        store_path=str(tmp_path),
    )
    client.restore_login(user_id, "AGENTDEVICE", "access-token")
    client.load_store()
    assert client.olm is not None
    client.olm.account.shared = True
    client.olm.uploaded_key_count = 50

    response = nio.SyncResponse(
        next_batch="next",
        rooms=nio.Rooms(invite={}, join={}, leave={}),
        device_key_count=nio.DeviceOneTimeKeyCount(0, 0),
        device_list=nio.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
        account_data_events=[],
    )
    client._handle_olm_events(response)

    assert client.olm.uploaded_key_count == 0
    assert client.should_upload_keys
