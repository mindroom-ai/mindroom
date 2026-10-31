"""Crypto facts and exact HTTP work sharing the sync transaction boundary."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..api import Api
from ..event_builders import DummyMessage, RoomKeyRequestMessage, ToDeviceMessage
from ..events import (
    MegolmEvent,
    RoomKeyRequest,
    RoomKeyRequestCancellation,
    ToDeviceEvent,
)
from ..exceptions import LocalProtocolError
from ..responses import (
    ErrorResponse,
    KeysClaimResponse,
    KeysQueryResponse,
    KeysUploadResponse,
    Response,
    ToDeviceResponse,
)
from .model import encode_json
from .store import DurableStore

if TYPE_CHECKING:
    from ..client.async_client import AsyncClient


@dataclass(frozen=True, slots=True)
class CryptoRequest:
    request_id: str
    kind: str
    method: str
    path: str
    body: str


class CryptoMaintenance:
    """Synchronous crypto work under an existing SQLite transaction.

    The owner must discard the live client after a transaction failure: SQLite
    rollback cannot undo mutations of the live Olm account and sessions.
    HTTP and callbacks belong outside this class and outside the transaction.
    """

    def __init__(self, client: AsyncClient, store: DurableStore):
        self.client = client
        self.store = store
        if client.olm is None:
            raise LocalProtocolError("durable crypto requires an Olm account")
        self.olm = client.olm
        self._messages: list[tuple[str, ToDeviceMessage]] = []

    def _read(self, kind: str) -> Any:
        row = self.store.database.execute_sql(
            "SELECT body FROM NioDurableCrypto WHERE kind=? AND key='current'",
            (kind,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, kind: str, body: Any) -> None:
        self.store.database.execute_sql(
            "INSERT OR REPLACE INTO NioDurableCrypto(kind,key,body) VALUES(?,'current',?)",
            (kind, encode_json(body)),
        )

    def restore(self) -> None:
        """Restore bounded facts; signed device and trust stores stay authoritative."""
        try:
            facts = self._read("facts")
            if facts is None:
                return
            olm = self.olm
            users = facts["users_for_key_query"]
            if not isinstance(users, list) or not all(
                isinstance(x, str) for x in users
            ):
                raise ValueError("invalid dirty users")
            count = facts["uploaded_key_count"]
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("invalid uploaded key count")
            olm.users_for_key_query = set(users)
            olm.uploaded_key_count = count
            for name in ("received_key_requests", "key_request_from_untrusted"):
                requests = self._decode_requests(facts[name])
                if name == "key_request_from_untrusted" and any(
                    not isinstance(event, RoomKeyRequest) for event in requests.values()
                ):
                    raise ValueError("invalid untrusted key request")
                setattr(olm, name, requests)
            olm.key_requests_waiting_for_session.clear()
            for target, requests in facts["waiting"]:
                decoded = self._decode_requests(requests)
                if not decoded:
                    self._device_key(target)
                    continue
                device = self._device(target)
                if any(
                    not isinstance(event, RoomKeyRequest)
                    or (event.sender, event.requesting_device_id)
                    != (device.user_id, device.device_id)
                    for event in decoded.values()
                ):
                    raise ValueError("invalid waiting key request target")
                olm.key_requests_waiting_for_session[tuple(target)] = decoded
            olm.wedged_devices = [self._device(x) for x in facts["wedged"]]
            olm.key_request_devices_no_session = [
                self._device(x) for x in facts["claim_targets"]
            ]
            olm.key_re_requests_events.clear()
            for target, sources in facts["rerequests"]:
                self._device(target)
                events = [MegolmEvent.from_dict(source) for source in sources]
                if any(not isinstance(event, MegolmEvent) for event in events):
                    raise ValueError("invalid key rerequest event")
                olm.key_re_requests_events[tuple(target)] = events
            classes = {
                "ToDeviceMessage": ToDeviceMessage,
                "DummyMessage": DummyMessage,
                "RoomKeyRequestMessage": RoomKeyRequestMessage,
            }
            self._messages = []
            for item in facts["messages"]:
                if not isinstance(item["id"], str) or not item["id"]:
                    raise ValueError("invalid retained message id")
                message = classes[item["class"]](**item["message"])
                if not all(
                    isinstance(value, str)
                    for value in (
                        message.type,
                        message.recipient,
                        message.recipient_device,
                    )
                ) or not isinstance(message.content, dict):
                    raise ValueError("invalid retained message")
                if isinstance(message, RoomKeyRequestMessage) and not all(
                    isinstance(value, str)
                    for value in (
                        message.request_id,
                        message.session_id,
                        message.room_id,
                        message.algorithm,
                    )
                ):
                    raise ValueError("invalid retained room key request")
                self._messages.append((item["id"], message))
            if len({key for key, _ in self._messages}) != len(self._messages):
                raise ValueError("duplicate retained message id")
            olm.outgoing_to_device_messages = [message for _, message in self._messages]
            self._pending()
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise LocalProtocolError("invalid stored durable crypto facts") from error

    @staticmethod
    def _device_key(target):
        if (
            not isinstance(target, list)
            or len(target) != 2
            or not all(isinstance(value, str) for value in target)
        ):
            raise ValueError("invalid device reference")
        return target[0], target[1]

    def _device(self, target):
        user_id, device_id = self._device_key(target)
        return self.olm.device_store[user_id][device_id]

    @staticmethod
    def _decode_requests(sources):
        if not isinstance(sources, list):
            raise ValueError("invalid key request list")
        requests = {}
        for source in sources:
            event = ToDeviceEvent.parse_event(source)
            if not isinstance(event, (RoomKeyRequest, RoomKeyRequestCancellation)):
                raise ValueError("invalid key request")
            if event.request_id in requests:
                raise ValueError("duplicate key request id")
            requests[event.request_id] = event
        return requests

    def _retain_messages(self) -> None:
        retained = []
        for message in self.olm.outgoing_to_device_messages:
            request_id = next(
                (key for key, previous in self._messages if previous is message),
                None,
            ) or str(uuid4())
            retained.append((request_id, message))
        self._messages = retained

    def capture(self) -> None:
        self.store._require_transaction()
        olm = self.olm
        self._retain_messages()
        self._write(
            "facts",
            {
                "users_for_key_query": sorted(olm.users_for_key_query),
                "uploaded_key_count": olm.uploaded_key_count,
                "received_key_requests": [
                    x.source for x in olm.received_key_requests.values()
                ],
                "key_request_from_untrusted": [
                    x.source for x in olm.key_request_from_untrusted.values()
                ],
                "waiting": [
                    [list(target), [x.source for x in requests.values()]]
                    for target, requests in olm.key_requests_waiting_for_session.items()
                    if requests
                ],
                "wedged": [[x.user_id, x.device_id] for x in olm.wedged_devices],
                "claim_targets": [
                    [x.user_id, x.device_id] for x in olm.key_request_devices_no_session
                ],
                "rerequests": [
                    [list(target), [dict(x.source, room_id=x.room_id) for x in events]]
                    for target, events in olm.key_re_requests_events.items()
                ],
                "messages": [
                    {
                        "id": key,
                        "class": type(message).__name__,
                        "message": asdict(message),
                    }
                    for key, message in self._messages
                ],
            },
        )
        olm.save_account()

    def _pending(self) -> tuple[CryptoRequest, str | None] | None:
        data = self._read("request")
        if data is None:
            return None
        request = CryptoRequest(**data["request"])
        fingerprint = self.encrypted_fingerprint()
        if (
            not all(isinstance(value, str) for value in asdict(request).values())
            or request.kind not in ("upload", "query", "claim", "to_device")
            or not isinstance(json.loads(request.body), dict)
        ):
            raise LocalProtocolError("invalid stored crypto request")
        if fingerprint is not None and request.kind != "to_device":
            raise LocalProtocolError(
                "encrypted fingerprint belongs to non-message request"
            )
        message_id = data["message_id"]
        if message_id is not None and (
            request.kind != "to_device"
            or not isinstance(message_id, str)
            or not any(key == message_id for key, _ in self._messages)
        ):
            raise LocalProtocolError("invalid stored crypto message reference")
        return request, message_id

    def encrypted_fingerprint(self) -> str | None:
        data = self._read("request")
        value = data.get("encrypted_fingerprint") if data else None
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise LocalProtocolError("invalid retained encrypted operation fingerprint")
        return value

    def _retain_request(
        self,
        kind,
        api_result,
        request_id=None,
        message_id=None,
        encrypted_fingerprint=None,
    ):
        method, path, body = api_result
        request = CryptoRequest(
            request_id or str(uuid4()), kind, method, path.split("?", 1)[0], body
        )
        self._write(
            "request",
            {
                "request": asdict(request),
                "message_id": message_id,
                "encrypted_fingerprint": encrypted_fingerprint,
            },
        )
        self.capture()
        return request

    def next_request(
        self, *, completed: set[str] | frozenset[str] = frozenset()
    ) -> CryptoRequest | None:
        self.store._require_transaction()
        if pending := self._pending():
            return pending[0]
        olm = self.olm
        self._retain_messages()
        if self._messages:
            return self.enqueue_message(self._messages[0][1])
        if "upload" not in completed and olm.should_upload_keys:
            return self.enqueue_upload()
        if "query" not in completed and olm.users_for_key_query:
            return self.enqueue_query()
        if "claim" not in completed and (
            olm.wedged_devices or olm.key_request_devices_no_session
        ):
            return self.enqueue_claim(dict(olm.get_users_for_key_claiming()))
        return None

    def enqueue_upload(self) -> CryptoRequest:
        self.store._require_transaction()
        return self._retain_request(
            "upload", Api.keys_upload("", self.olm.share_keys())
        )

    def enqueue_query(self, user_set: set[str] | None = None) -> CryptoRequest:
        self.store._require_transaction()
        users = sorted(self.olm.users_for_key_query if user_set is None else user_set)
        # New sync invalidations are distinguishable from this query.
        self.olm.users_for_key_query.difference_update(users)
        return self._retain_request(
            "query", Api.keys_query("", users, self.store.cursor)
        )

    def enqueue_claim(self, users: dict[str, Iterable[str]]) -> CryptoRequest:
        self.store._require_transaction()
        return self._retain_request("claim", Api.keys_claim("", users))

    def enqueue_message(
        self,
        message: ToDeviceMessage,
        *,
        request_id: str | None = None,
        encrypted_fingerprint: str | None = None,
    ) -> CryptoRequest:
        self.store._require_transaction()
        retained = next(
            (key for key, previous in self._messages if previous is message), None
        )
        if pending := self._pending():
            if pending[1] == retained and retained is not None:
                return pending[0]
            raise LocalProtocolError("another crypto request is pending")
        if retained is None:
            retained = request_id or str(uuid4())
            self._messages.append((retained, message))
            if not any(
                previous is message for previous in self.olm.outgoing_to_device_messages
            ):
                self.olm.outgoing_to_device_messages.append(message)
        return self._retain_request(
            "to_device",
            Api.to_device("", message.type, message.as_dict(), retained),
            retained,
            retained,
            encrypted_fingerprint,
        )

    def enqueue_to_device(
        self, event_type: str, content: dict[str, Any], *, request_id: str | None = None
    ) -> CryptoRequest:
        self.store._require_transaction()
        if self._pending():
            raise LocalProtocolError("another crypto request is pending")
        request_id = request_id or str(uuid4())
        return self._retain_request(
            "to_device", Api.to_device("", event_type, content, request_id), request_id
        )

    def apply(
        self, request: CryptoRequest, body: dict[str, Any]
    ) -> tuple[Response, list[ToDeviceEvent]]:
        self.store._require_transaction()
        pending = self._pending()
        if pending is None or pending[0] != request:
            raise LocalProtocolError("response does not match pending crypto request")
        if "errcode" in body:
            return ErrorResponse.from_dict(body), []
        if request.kind == "to_device":
            message = next(
                (message for key, message in self._messages if key == pending[1]), None
            )
            if pending[1] is not None and message is None:
                raise LocalProtocolError("pending crypto message is missing")
            response = ToDeviceResponse.from_dict(body, message)
        elif request.kind == "upload":
            response = KeysUploadResponse.from_dict(body)
        elif request.kind == "query":
            response = KeysQueryResponse.from_dict(
                body, set(json.loads(request.body)["device_keys"])
            )
        else:
            response = KeysClaimResponse.from_dict(body)
        if isinstance(response, ErrorResponse):
            return response, []
        dirty = set(self.olm.users_for_key_query)
        self.client._handle_olm_response(response)
        if isinstance(response, KeysQueryResponse):
            missing = (
                set(json.loads(request.body)["device_keys"])
                - response.device_keys.keys()
            )
            self.olm.users_for_key_query.update(dirty | missing)
        observations = self.olm.collect_key_requests()
        self.store.database.execute_sql(
            "DELETE FROM NioDurableCrypto WHERE kind='request'"
        )
        self.capture()
        return response, observations

    def change_key_share(self, event: RoomKeyRequest, *, cancel: bool = False) -> bool:
        self.store._require_transaction()
        changed = (
            self.olm.cancel_key_share(event)
            if cancel
            else self.olm.continue_key_share(event)
        )
        self.capture()
        return changed
