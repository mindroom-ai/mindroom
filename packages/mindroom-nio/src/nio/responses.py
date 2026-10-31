# Copyright © 2018 Damir Jelić <poljar@termina.org.uk>
# Copyright © 2020-2021 Famedly GmbH
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Any, Self

from jsonschema.exceptions import SchemaError, ValidationError

from ._json_copy import copy_json
from .event_builders import ToDeviceMessage
from .events import (
    AccountDataEvent,
    BadEventType,
    EphemeralEvent,
    Event,
    InviteEvent,
    ToDeviceEvent,
)
from .events.presence import PresenceEvent
from .http import TransportResponse
from .schemas import Schemas, validate_json

logger = logging.getLogger(__name__)


__all__ = [
    "ContentRepositoryConfigResponse",
    "ContentRepositoryConfigError",
    "FileResponse",
    "ChangePasswordResponse",
    "ChangePasswordError",
    "DeleteDevicesAuthResponse",
    "DeleteDevicesResponse",
    "DeleteDevicesError",
    "Device",
    "DeviceList",
    "DevicesResponse",
    "DevicesError",
    "DeviceOneTimeKeyCount",
    "DiscoveryInfoError",
    "DiscoveryInfoResponse",
    "DiskDownloadResponse",
    "DownloadResponse",
    "DownloadError",
    "ErrorResponse",
    "GetOpenIDTokenError",
    "GetOpenIDTokenResponse",
    "InviteInfo",
    "JoinResponse",
    "JoinError",
    "JoinedMembersResponse",
    "JoinedMembersError",
    "JoinedRoomsResponse",
    "JoinedRoomsError",
    "KeysClaimResponse",
    "KeysClaimError",
    "KeysQueryResponse",
    "KeysQueryError",
    "KeysUploadResponse",
    "KeysUploadError",
    "RegisterResponse",
    "LoginResponse",
    "LoginError",
    "LoginInfoResponse",
    "LoginInfoError",
    "LogoutResponse",
    "LogoutError",
    "MemoryDownloadResponse",
    "Response",
    "RoomCreateResponse",
    "RoomCreateError",
    "RoomInfo",
    "RoomInviteResponse",
    "RoomInviteError",
    "RoomKickResponse",
    "RoomKickError",
    "RoomLeaveResponse",
    "RoomLeaveError",
    "RoomForgetResponse",
    "RoomForgetError",
    "RoomMember",
    "RoomMessagesResponse",
    "RoomMessagesError",
    "RoomGetStateResponse",
    "RoomGetStateError",
    "RoomGetStateEventResponse",
    "RoomGetStateEventError",
    "RoomGetEventResponse",
    "RoomGetEventError",
    "RoomGetVisibilityResponse",
    "RoomGetVisibilityError",
    "RoomPutAliasResponse",
    "RoomPutStateResponse",
    "RoomPutStateError",
    "RoomRedactResponse",
    "RoomRedactError",
    "RoomResolveAliasResponse",
    "RoomResolveAliasError",
    "RoomSendResponse",
    "RoomSendError",
    "RoomSummary",
    "Rooms",
    "ShareGroupSessionResponse",
    "ShareGroupSessionError",
    "SlidingSyncError",
    "SlidingSyncHero",
    "SlidingSyncList",
    "SlidingSyncResponse",
    "SlidingSyncRoom",
    "SlidingSyncStateStub",
    "SyncResponse",
    "SyncError",
    "Timeline",
    "RoomTypingResponse",
    "RoomTypingError",
    "UploadResponse",
    "UploadError",
    "ProfileGetResponse",
    "ProfileGetError",
    "ProfileGetDisplayNameResponse",
    "ProfileGetDisplayNameError",
    "ProfileSetDisplayNameResponse",
    "ProfileSetDisplayNameError",
    "ProfileGetAvatarResponse",
    "ProfileGetAvatarError",
    "ProfileSetAvatarResponse",
    "ProfileSetAvatarError",
    "PresenceGetResponse",
    "PresenceGetError",
    "PresenceSetResponse",
    "PresenceSetError",
    "RoomKeyRequestResponse",
    "RoomKeyRequestError",
    "ThumbnailResponse",
    "ThumbnailError",
    "ToDeviceResponse",
    "ToDeviceError",
    "RoomContextResponse",
    "RoomContextError",
    "UploadFilterError",
    "UploadFilterResponse",
    "WhoamiError",
    "WhoamiResponse",
    "DirectRoomsResponse",
    "DirectRoomsErrorResponse",
]


def verify(schema, error_class, pass_arguments=True):
    def decorator(f):
        @wraps(f)
        def wrapper(cls, parsed_dict, *args, **kwargs):
            try:
                logger.debug("Validating %s", cls.__name__)
                validate_json(parsed_dict, schema)
            except (SchemaError, ValidationError) as e:
                # Exception messages and server error fields can contain payloads.
                logger.warning(
                    "Error validating %s: %s (rule=%s)",
                    cls.__name__,
                    type(e).__name__,
                    e.validator,
                )

                if pass_arguments:
                    return error_class.from_dict(parsed_dict, *args, **kwargs)
                else:
                    return error_class.from_dict(parsed_dict)

            return f(cls, parsed_dict, *args, **kwargs)

        return wrapper

    return decorator


@dataclass
class Rooms:
    invite: dict[str, InviteInfo] = field()
    join: dict[str, RoomInfo] = field()
    leave: dict[str, RoomInfo] = field()


@dataclass
class DeviceOneTimeKeyCount:
    curve25519: int | None = field()
    signed_curve25519: int | None = field()


@dataclass
class DeviceList:
    changed: list[str] = field()
    left: list[str] = field()


@dataclass
class Timeline:
    events: list = field()
    limited: bool = field()
    prev_batch: str | None = field()


@dataclass
class InviteInfo:
    invite_state: list = field()


@dataclass
class RoomSummary:
    invited_member_count: int | None = None
    joined_member_count: int | None = None
    heroes: list[str] | None = None


@dataclass
class UnreadNotifications:
    notification_count: int | None = None
    highlight_count: int | None = None


@dataclass
class RoomInfo:
    timeline: Timeline = field()
    state: list = field()
    ephemeral: list = field()
    account_data: list = field()
    summary: RoomSummary | None = None
    unread_notifications: UnreadNotifications | None = None
    unread_thread_notifications: dict[str, UnreadNotifications] | None = (
        None  # {event_id: UnreadNotifications}
    )

    @staticmethod
    def parse_account_data(event_dict):
        """Parse the account data dictionary and produce a list of events."""
        return [AccountDataEvent.parse_event(event) for event in event_dict]


@dataclass
class SlidingSyncList:
    count: int = field()

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> SlidingSyncList:
        return cls(parsed_dict["count"])


@dataclass
class SlidingSyncHero:
    user_id: str = field()
    displayname: str | None = None
    avatar_url: str | None = None

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> SlidingSyncHero:
        return cls(
            parsed_dict["user_id"],
            parsed_dict.get("displayname"),
            parsed_dict.get("avatar_url"),
        )


@dataclass
class SlidingSyncStateStub:
    type: str = field()
    state_key: str = field()

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> SlidingSyncStateStub:
        return cls(parsed_dict["type"], parsed_dict["state_key"])


@dataclass
class SlidingSyncRoom:
    bump_stamp: int | None = None
    membership: str | None = None
    lists: list[str] | None = None
    name: str | None = None
    avatar: str | None = None
    # None when the field was absent from the response; an explicit empty
    # list means the server cleared the heroes.
    heroes: list[SlidingSyncHero] | None = None
    is_dm: bool | None = None
    initial: bool = False
    expanded_timeline: bool = False
    required_state: list[Event | BadEventType | SlidingSyncStateStub] = field(
        default_factory=list
    )
    timeline: list[Event | BadEventType] = field(default_factory=list)
    prev_batch: str | None = None
    limited: bool = False
    num_live: int | None = None
    joined_count: int | None = None
    invited_count: int | None = None
    notification_count: int | None = None
    highlight_count: int | None = None
    stripped_state: list[InviteEvent | BadEventType] = field(default_factory=list)

    @staticmethod
    def _get_required_state(
        parsed_dicts: list[dict[Any, Any]],
    ) -> list[Event | BadEventType | SlidingSyncStateStub]:
        events: list[Event | BadEventType | SlidingSyncStateStub] = []

        for event_dict in parsed_dicts:
            # MSC4186 uses StateStub responses without content to signal deleted state.
            if "content" not in event_dict:
                events.append(SlidingSyncStateStub.from_dict(event_dict))
            else:
                events.append(Event.parse_event(event_dict))

        return events

    @staticmethod
    def _get_stripped_state(
        parsed_dicts: list[dict[Any, Any]],
    ) -> list[InviteEvent | BadEventType]:
        events: list[InviteEvent | BadEventType] = []

        for event_dict in parsed_dicts:
            event = InviteEvent.parse_event(event_dict)

            if event:
                events.append(event)

        return events

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> SlidingSyncRoom:
        num_live = parsed_dict.get("num_live")
        if num_live is not None and type(num_live) is not int:
            raise TypeError("num_live must be an integer")

        return cls(
            bump_stamp=parsed_dict.get("bump_stamp"),
            membership=parsed_dict.get("membership"),
            lists=parsed_dict.get("lists"),
            name=parsed_dict.get("name"),
            avatar=parsed_dict.get("avatar"),
            heroes=(
                [SlidingSyncHero.from_dict(hero) for hero in parsed_dict["heroes"]]
                if parsed_dict.get("heroes") is not None
                else None
            ),
            is_dm=parsed_dict.get("is_dm"),
            initial=parsed_dict.get("initial", False),
            # Synapse serialises this flag with an unstable prefix.
            expanded_timeline=parsed_dict.get(
                "unstable_expanded_timeline",
                parsed_dict.get("expanded_timeline", False),
            ),
            required_state=cls._get_required_state(
                parsed_dict.get("required_state", [])
            ),
            timeline=SyncResponse._get_room_events(parsed_dict.get("timeline", [])),
            prev_batch=parsed_dict.get("prev_batch"),
            limited=parsed_dict.get("limited", False),
            num_live=num_live,
            joined_count=parsed_dict.get("joined_count"),
            invited_count=parsed_dict.get("invited_count"),
            # The current MSC4186 text drops these counts, but deployed
            # servers still include them on every room.
            notification_count=parsed_dict.get("notification_count"),
            highlight_count=parsed_dict.get("highlight_count"),
            stripped_state=cls._get_stripped_state(
                # The MSC4186 text renamed invite_state to stripped_state;
                # deployed servers still send invite_state.
                parsed_dict.get("stripped_state", parsed_dict.get("invite_state", []))
            ),
        )


@dataclass
class RoomMember:
    user_id: str = field()
    display_name: str = field()
    avatar_url: str = field()


@dataclass
class Device:
    id: str = field()
    display_name: str = field()
    last_seen_ip: str = field()
    last_seen_date: datetime = field()

    @classmethod
    def from_dict(cls, parsed_dict):
        date = None

        if parsed_dict["last_seen_ts"] is not None:
            date = datetime.fromtimestamp(parsed_dict["last_seen_ts"] / 1000)

        return cls(
            parsed_dict["device_id"],
            parsed_dict["display_name"],
            parsed_dict["last_seen_ip"],
            date,
        )


@dataclass
class Response:
    uuid: str = field(default="", init=False)
    start_time: float | None = field(default=None, init=False)
    end_time: float | None = field(default=None, init=False)
    timeout: int = field(default=0, init=False)
    transport_response: TransportResponse | None = field(
        init=False,
        default=None,
    )

    @property
    def elapsed(self):
        if not self.start_time or not self.end_time:
            return 0
        elapsed = self.end_time - self.start_time
        return max(0, elapsed - (self.timeout / 1000))


@dataclass
class FileResponse(Response):
    """A response representing a successful file content request.

    Attributes:
        body (bytes, os.PathLike): The file's content in bytes, or location on disk if provided.
        content_type (str): The content MIME type of the file,
            e.g. "image/png".
        filename (str, optional): The file's name returned by the server.
    """

    body: bytes | os.PathLike = field()
    content_type: str = field()
    filename: str | None = field()

    def __str__(self):
        if isinstance(self.body, bytes):
            return f"{len(self.body)} bytes, content type: {self.content_type}, filename: {self.filename}"
        else:
            return f"content type: {self.content_type}, location: {self.body}"

    @classmethod
    def from_data(
        cls,
        data: bytes | os.PathLike | dict[Any, Any],
        content_type,
        filename=None,
    ):
        """Create a FileResponse from file content returned by the server.

        Args:
            data (bytes, os.PathLike): The file's content in bytes.
            content_type (str): The content MIME type of the file,
                e.g. "image/png".
            filename (str, optional): The file's name returned by the server.
        """
        raise NotImplementedError


@dataclass
class MemoryFileResponse(FileResponse):
    """
    A response representing a successful file content request with the file content stored in memory.

    Attributes:
        body (bytes): The file's content in bytes.
    """

    body: bytes = field()


@dataclass
class DiskFileResponse(FileResponse):
    """A response representing a successful file content request with the file content stored on disk.

    This class is exactly the same as ``FileResponse`` but with the following difference

    Attributes:
        body (os.PathLike): The path to the file on disk.
    """

    body: os.PathLike = field()


@dataclass
class ErrorResponse(Response):
    message: str = field()
    status_code: str | None = None
    retry_after_ms: int | None = None
    soft_logout: bool = False

    def __str__(self) -> str:
        if self.status_code and self.message:
            e = f"{self.status_code} {self.message}"
        elif self.message:
            e = self.message
        elif self.status_code:
            e = f"{self.status_code} unknown error"
        else:
            e = "unknown error"

        if self.retry_after_ms:
            e = f"{e} - retry after {self.retry_after_ms}ms"

        return f"{self.__class__.__name__}: {e}"

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> Self:
        try:
            validate_json(parsed_dict, Schemas.error)
        except (SchemaError, ValidationError):
            return cls("unknown error")

        return cls(
            parsed_dict["error"],
            parsed_dict["errcode"],
            parsed_dict.get("retry_after_ms"),
            parsed_dict.get("soft_logout", False),
        )


@dataclass
class _ErrorWithRoomId(ErrorResponse):
    room_id: str = ""

    @classmethod
    def from_dict(cls, parsed_dict, room_id):
        try:
            validate_json(parsed_dict, Schemas.error)
        except (SchemaError, ValidationError):
            return cls("unknown error")

        return cls(
            parsed_dict["error"],
            parsed_dict["errcode"],
            parsed_dict.get("retry_after_ms"),
            parsed_dict.get("soft_logout", False),
            room_id,
        )


class LoginError(ErrorResponse):
    pass


class LogoutError(ErrorResponse):
    pass


class SyncError(ErrorResponse):
    pass


class RoomSendError(_ErrorWithRoomId):
    pass


class RoomGetStateError(_ErrorWithRoomId):
    """A response representing an unsuccessful room state query."""

    pass


class RoomGetStateEventError(_ErrorWithRoomId):
    """A response representing an unsuccessful room state query."""

    pass


class RoomGetEventError(ErrorResponse):
    """A response representing an unsuccessful room get event request."""

    pass


class RoomPutStateError(_ErrorWithRoomId):
    """A response representing an unsuccessful room state sending request."""

    pass


class RoomRedactError(_ErrorWithRoomId):
    pass


class RoomResolveAliasError(ErrorResponse):
    """A response representing an unsuccessful room alias query."""

    pass


class RoomPutAliasError(ErrorResponse):
    """A response representing an unsuccessful room alias put request."""

    pass


class RoomGetVisibilityError(ErrorResponse):
    """A response representing an unsuccessful room get visibility request."""

    pass


class RoomEventRelationsError(ErrorResponse):
    """A response representing an unsuccessful room event relations request."""

    pass


class RoomThreadsError(ErrorResponse):
    """A response representing an unsuccessful room threads request."""

    pass


class RoomTypingError(_ErrorWithRoomId):
    """A response representing an unsuccessful room typing request."""

    pass


class RoomKickError(ErrorResponse):
    pass


class RoomInviteError(ErrorResponse):
    pass


class RoomCreateError(ErrorResponse):
    """A response representing a unsuccessful create room request."""

    pass


class JoinError(ErrorResponse):
    pass


class RoomLeaveError(ErrorResponse):
    pass


class RoomForgetError(_ErrorWithRoomId):
    pass


class RoomMessagesError(_ErrorWithRoomId):
    pass


class GetOpenIDTokenError(ErrorResponse):
    pass


class KeysUploadError(ErrorResponse):
    pass


class KeysQueryError(ErrorResponse):
    pass


class KeysClaimError(_ErrorWithRoomId):
    pass


class ContentRepositoryConfigError(ErrorResponse):
    """A response for a unsuccessful content repository config request."""


class UploadError(ErrorResponse):
    """A response representing a unsuccessful upload request."""


class DownloadError(ErrorResponse):
    """A response representing a unsuccessful download request."""


class ThumbnailError(ErrorResponse):
    """A response representing a unsuccessful thumbnail request."""


@dataclass
class ShareGroupSessionError(_ErrorWithRoomId):
    """Response representing unsuccessful group sessions sharing request."""

    users_shared_with: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def from_dict(cls, parsed_dict, room_id, users_shared_with):
        try:
            validate_json(parsed_dict, Schemas.error)
        except (SchemaError, ValidationError):
            return cls("unknown error")

        return cls(
            parsed_dict["error"], parsed_dict["errcode"], room_id, users_shared_with
        )


class DevicesError(ErrorResponse):
    pass


class DeleteDevicesError(ErrorResponse):
    pass


class JoinedMembersError(_ErrorWithRoomId):
    pass


class JoinedRoomsError(ErrorResponse):
    """A response representing an unsuccessful joined rooms query."""

    pass


class ProfileGetError(ErrorResponse):
    pass


class ProfileGetDisplayNameError(ErrorResponse):
    pass


class ProfileSetDisplayNameError(ErrorResponse):
    pass


class ProfileGetAvatarError(ErrorResponse):
    pass


class PresenceGetError(ErrorResponse):
    """Response representing a unsuccessful get presence request."""

    pass


class PresenceSetError(ErrorResponse):
    """Response representing a unsuccessful set presence request."""

    pass


class ProfileSetAvatarError(ErrorResponse):
    pass


@dataclass
class DiscoveryInfoError(ErrorResponse):
    pass


@dataclass
class DiscoveryInfoResponse(Response):
    """A response for a successful discovery info request.

    Attributes:
        homeserver_url (str): The base URL of the homeserver corresponding to
            the requested domain.

        identity_server_url (str, optional): The base URL of the identity
            server corresponding to the requested domain, if any.
    """

    homeserver_url: str = field()
    identity_server_url: str | None = None

    @classmethod
    @verify(Schemas.discovery_info, DiscoveryInfoError)
    def from_dict(
        cls,
        parsed_dict: dict[str, Any],
    ) -> DiscoveryInfoResponse | DiscoveryInfoError:
        homeserver_url = parsed_dict["m.homeserver"]["base_url"].rstrip("/")

        identity_server_url = (
            parsed_dict
            .get(
                "m.identity_server",
                {},
            )
            .get("base_url", "")
            .rstrip("/")
            or None
        )

        return cls(homeserver_url, identity_server_url)


@dataclass
class RegisterErrorResponse(ErrorResponse):
    pass


@dataclass
class RegisterResponse(Response):
    user_id: str = field()
    device_id: str = field()
    access_token: str = field()

    def __str__(self) -> str:
        return f"Registered {self.user_id}, device id {self.device_id}."

    @classmethod
    @verify(Schemas.register, RegisterErrorResponse)
    def from_dict(cls, parsed_dict):
        return cls(
            parsed_dict["user_id"],
            parsed_dict["device_id"],
            parsed_dict["access_token"],
        )


@dataclass
class RegisterInteractiveError(ErrorResponse):
    pass


@dataclass
class RegisterInteractiveResponse(Response):
    stages: list[str] = field()
    params: dict[str, Any] = field()
    session: str = field()
    completed: list[str] | None = field()
    user_id: str | None = field()
    device_id: str | None = field()
    access_token: str | None = field()

    @classmethod
    @verify(Schemas.register_flows, RegisterInteractiveError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> RegisterInteractiveResponse | RegisterInteractiveError:
        for flow in parsed_dict["flows"]:
            stages = list(flow["stages"])

        return cls(
            stages,
            parsed_dict["params"],
            parsed_dict["session"],
            parsed_dict.get("completed"),
            parsed_dict.get("user_id"),
            parsed_dict.get("device_id"),
            parsed_dict.get("access_token"),
        )


@dataclass
class LoginInfoError(ErrorResponse):
    pass


@dataclass
class LoginInfoResponse(Response):
    flows: list[str] = field()

    @classmethod
    @verify(Schemas.login_info, LoginInfoError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> LoginInfoResponse | ErrorResponse:
        flow_types = [flow["type"] for flow in parsed_dict["flows"]]
        return cls(flow_types)


@dataclass
class LoginResponse(Response):
    user_id: str = field()
    device_id: str = field()
    access_token: str = field()

    def __str__(self) -> str:
        return f"Logged in as {self.user_id}, device id: {self.device_id}."

    @classmethod
    @verify(Schemas.login, LoginError)
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> LoginResponse | ErrorResponse:
        return cls(
            parsed_dict["user_id"],
            parsed_dict["device_id"],
            parsed_dict["access_token"],
        )


@dataclass
class LogoutResponse(Response):
    def __str__(self) -> str:
        return "Logged out"

    @classmethod
    @verify(Schemas.empty, LogoutError)
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> LogoutResponse | ErrorResponse:
        """Create a response for logout response from server."""
        return cls()


@dataclass
class ChangePasswordError(ErrorResponse):
    pass


@dataclass
class ChangePasswordResponse(Response):
    """Response for a successful password change request."""

    @classmethod
    @verify(Schemas.empty, ChangePasswordError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> ChangePasswordResponse | ErrorResponse:
        return cls()


@dataclass
class JoinedMembersResponse(Response):
    members: list[RoomMember] = field()
    room_id: str = field()

    @classmethod
    @verify(Schemas.joined_members, JoinedMembersError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_id: str,
    ) -> JoinedMembersResponse | ErrorResponse:
        members = []

        for user_id, user_info in parsed_dict["joined"].items():
            user = RoomMember(
                user_id,
                user_info.get("display_name", None),
                user_info.get("avatar_url", None),
            )
            members.append(user)

        return cls(members, room_id)


@dataclass
class JoinedRoomsResponse(Response):
    """A response containing a list of joined rooms.

    Attributes:
        rooms (List[str]): The rooms joined by the account.
    """

    rooms: list[str] = field()

    @classmethod
    @verify(Schemas.joined_rooms, JoinedRoomsError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> JoinedRoomsResponse | ErrorResponse:
        return cls(parsed_dict["joined_rooms"])


@dataclass
class ContentRepositoryConfigResponse(Response):
    """A response for a successful content repository config request.

    Attributes:
        upload_size (Optional[int]): The maximum file size in bytes for an
            upload. If `None`, the limit is unknown.
    """

    upload_size: int | None = None

    @classmethod
    @verify(Schemas.content_repository_config, ContentRepositoryConfigError)
    def from_dict(
        cls,
        parsed_dict: dict,
    ) -> ContentRepositoryConfigResponse | ErrorResponse:
        return cls(parsed_dict.get("m.upload.size"))


@dataclass
class UploadResponse(Response):
    """A response representing a successful upload request."""

    content_uri: str = field()

    @classmethod
    @verify(Schemas.upload, UploadError)
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> UploadResponse | ErrorResponse:
        return cls(
            parsed_dict["content_uri"],
        )


@dataclass
class DownloadResponse(FileResponse):
    """A response representing a successful download request."""

    @classmethod
    def from_data(
        cls,
        data: os.PathLike | bytes | dict[Any, Any],
        content_type: str,
        filename: str | None = None,
    ) -> DownloadResponse | DownloadError:
        if isinstance(data, (bytes, os.PathLike)):
            return cls(body=data, content_type=content_type, filename=filename)

        if isinstance(data, dict):
            return DownloadError.from_dict(data)

        return DownloadError("invalid data")


@dataclass
class MemoryDownloadResponse(DownloadResponse, MemoryFileResponse):
    """A response representing a successful download request with the download content stored in-memory.

    Attributes:
        body (bytes): The content of the download.
        content_type (str): The content type of the download.
        filename (Optional[str]): The filename of the download.
    """


@dataclass
class DiskDownloadResponse(DownloadResponse, DiskFileResponse):
    """A response representing a successful download request with the download content stored on disk.

    Attributes:
        body (os.PathLike): The path to the downloaded file.
        content_type (str): The content type of the download.
        filename (Optional[str]): The filename of the download.
    """

    body: os.PathLike = field()


@dataclass
class ThumbnailResponse(FileResponse):
    """A response representing a successful thumbnail request."""

    @classmethod
    def from_data(
        cls,
        data: bytes | os.PathLike | dict[Any, Any],
        content_type: str,
        filename: str | None = None,
    ) -> ThumbnailResponse | ThumbnailError:
        if not content_type.startswith("image/"):
            return ThumbnailError(f"invalid content type: {content_type}")

        if isinstance(data, bytes):
            return cls(body=data, content_type=content_type, filename=filename)

        if isinstance(data, dict):
            return ThumbnailError.from_dict(data)

        return ThumbnailError("invalid data")


@dataclass
class RoomEventIdResponse(Response):
    event_id: str = field()
    room_id: str = field()

    @staticmethod
    def create_error(parsed_dict, _room_id):
        return ErrorResponse.from_dict(parsed_dict)

    @classmethod
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_id: str,
    ) -> RoomEventIdResponse | ErrorResponse:
        try:
            validate_json(parsed_dict, Schemas.room_event_id)
        except (SchemaError, ValidationError):
            return cls.create_error(parsed_dict, room_id)

        return cls(parsed_dict["event_id"], room_id)


class RoomSendResponse(RoomEventIdResponse):
    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomSendError.from_dict(parsed_dict, room_id)


class DirectRoomsErrorResponse(ErrorResponse):
    pass


@dataclass
class DirectRoomsResponse(Response):
    """A response containing a list of direct rooms.

    Attributes:
        rooms (List[str]): The rooms joined by the account.
    """

    rooms: dict[str, list[str]] = field()

    @classmethod
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> DirectRoomsResponse | DirectRoomsErrorResponse:
        if parsed_dict.get("errcode") is not None:
            # This user has no DM rooms that have been marked with m.direct.
            return DirectRoomsErrorResponse.from_dict(parsed_dict)
        return cls(parsed_dict)


@dataclass
class RoomGetStateResponse(Response):
    """A response containing the state of a room.

    Attributes:
        events (List): The events making up the room state.
        room_id (str): The ID of the room.
    """

    events: list = field()
    room_id: str = field()

    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomGetStateError.from_dict(parsed_dict, room_id)

    @classmethod
    def from_dict(
        cls,
        parsed_dict: list[dict[Any, Any]],
        room_id: str,
    ) -> RoomGetStateResponse | RoomGetStateError:
        try:
            validate_json(parsed_dict, Schemas.room_state)
        except (SchemaError, ValidationError):
            return cls.create_error(parsed_dict, room_id)

        return cls(parsed_dict, room_id)


@dataclass
class RoomGetStateEventResponse(Response):
    """A response containing the content of a specific bit of room state.

    Attributes:
        content (Dict): The content of the state event.
        event_type (str): The type of the state event.
        state_key (str): The key of the state event.
        room_id (str): The ID of the room that the state event comes from.
    """

    content: dict = field()
    event_type: str = field()
    state_key: str = field()
    room_id: str = field()

    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomGetStateEventError.from_dict(parsed_dict, room_id)

    @classmethod
    def from_dict(
        cls,
        parsed_dict: dict[str, Any],
        event_type: str,
        state_key: str,
        room_id: str,
    ) -> RoomGetStateEventResponse | RoomGetStateEventError:
        return cls(parsed_dict, event_type, state_key, room_id)


class RoomGetEventResponse(Response):
    """A response indicating successful room get event request.

    Attributes:
        event (Event): The requested event.
    """

    event: Event = field()

    @classmethod
    @verify(
        Schemas.room_event,
        RoomGetEventError,
        pass_arguments=False,
    )
    def from_dict(
        cls, parsed_dict: dict[str, Any]
    ) -> RoomGetEventResponse | RoomGetEventError:
        event = Event.parse_event(parsed_dict)
        resp = cls()
        resp.event = event
        return resp


class RoomPutStateResponse(RoomEventIdResponse):
    """A response indicating successful sending of room state."""

    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomPutStateError.from_dict(parsed_dict, room_id)


class RoomRedactResponse(RoomEventIdResponse):
    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomRedactError.from_dict(parsed_dict, room_id)


@dataclass
class RoomResolveAliasResponse(Response):
    """A response containing the result of resolving an alias.

    Attributes:
        room_alias (str): The alias of the room.
        room_id (str): The resolved id of the room.
        servers (List[str]): Servers participating in the room.
    """

    room_alias: str = field()
    room_id: str = field()
    servers: list[str] = field()

    @classmethod
    @verify(
        Schemas.room_resolve_alias,
        RoomResolveAliasError,
        pass_arguments=False,
    )
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_alias: str,
    ) -> RoomResolveAliasResponse | ErrorResponse:
        room_id = parsed_dict["room_id"]
        servers = parsed_dict["servers"]
        return cls(room_alias, room_id, servers)


@dataclass
class RoomPutAliasResponse(Response):
    """A response containing the result of adding an alias."""

    room_alias: str = field()
    room_id: str = field()

    @classmethod
    def from_dict(
        cls, parsed_dict: dict[Any, Any], room_alias: str, room_id: str
    ) -> RoomPutAliasResponse | ErrorResponse:
        return cls(room_alias, room_id)


@dataclass
class RoomGetVisibilityResponse(Response):
    """A response containing the result of a get visibility request."""

    room_id: str = field()
    visibility: str = field()

    @classmethod
    @verify(
        Schemas.room_get_visibility,
        RoomGetVisibilityError,
        pass_arguments=False,
    )
    def from_dict(
        cls, parsed_dict: dict[Any, Any], room_id: str
    ) -> RoomGetVisibilityResponse | ErrorResponse:
        visibility = parsed_dict["visibility"]
        return cls(room_id, visibility)


@dataclass
class RoomEventRelationsResponse(Response):
    """A response containing the results of an event relations request."""

    room_id: str
    parent_event_id: str
    events: list[Event]
    prev_batch: str | None
    next_batch: str | None
    recursion_depth: int | None = None

    @classmethod
    @verify(
        Schemas.room_get_chunked_messages, RoomEventRelationsError, pass_arguments=False
    )
    def from_dict(
        cls, parsed_dict: dict[Any, Any], room_id: str, event_id: str
    ) -> RoomEventRelationsResponse | RoomEventRelationsError:
        events = [Event.parse_event(e) for e in parsed_dict["chunk"]]
        prev_batch = parsed_dict.get("prev_batch")
        next_batch = parsed_dict.get("next_batch")
        recursion_depth = parsed_dict.get("recursion_depth")
        return cls(room_id, event_id, events, prev_batch, next_batch, recursion_depth)


@dataclass
class RoomThreadsResponse(Response):
    """A response containing the results of a get threads request."""

    room_id: str
    thread_roots: list[Event]
    next_batch: str | None

    @classmethod
    @verify(Schemas.room_get_chunked_messages, RoomThreadsError, pass_arguments=False)
    def from_dict(
        cls, parsed_dict: dict[Any, Any], room_id: str
    ) -> RoomThreadsResponse | RoomThreadsError:
        thread_roots = [Event.parse_event(e) for e in parsed_dict["chunk"]]
        next_batch = parsed_dict.get("next_batch")
        return cls(room_id, thread_roots, next_batch)


class EmptyResponse(Response):
    @staticmethod
    def create_error(parsed_dict):
        return ErrorResponse.from_dict(parsed_dict)

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> Any | ErrorResponse:
        try:
            validate_json(parsed_dict, Schemas.empty)
        except (SchemaError, ValidationError):
            return cls.create_error(parsed_dict)

        return cls()


@dataclass
class _EmptyResponseWithRoomId(Response):
    room_id: str = field()

    @staticmethod
    def create_error(parsed_dict, room_id):
        return _ErrorWithRoomId.from_dict(parsed_dict, room_id)

    @classmethod
    def from_dict(
        cls, parsed_dict: dict[Any, Any], room_id: str
    ) -> Any | ErrorResponse:
        try:
            validate_json(parsed_dict, Schemas.empty)
        except (SchemaError, ValidationError):
            return cls.create_error(parsed_dict, room_id)

        return cls(room_id)


class RoomKickResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return RoomKickError.from_dict(parsed_dict)


class RoomInviteResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return RoomInviteError.from_dict(parsed_dict)


@dataclass
class ShareGroupSessionResponse(Response):
    """Response representing a successful group sessions sharing request.

    Attributes:
        room_id (str): The room id of the group session.
        users_shared_with (Set[Tuple[str, str]]): A set containing a tuple of
            user id device id pairs with whom we shared the group session in
            this request.

    """

    room_id: str = field()
    users_shared_with: set = field()

    @classmethod
    @verify(Schemas.empty, ShareGroupSessionError)
    def from_dict(
        cls,
        _: dict[Any, Any],
        room_id: str,
        users_shared_with: set[tuple[str, str]],
    ) -> ShareGroupSessionResponse | ErrorResponse:
        """Create a response from the json dict the server returns.

        Args:
           parsed_dict (Dict): The dict containing the raw json response.
           room_id (str): The room id of the room to which the group session
               belongs to.
           users_shared_with (Set[Tuple[str, str]]): A set containing a tuple
               of user id device id pairs with whom we shared the group
               session in this request.
        """
        return cls(room_id, users_shared_with)


class RoomTypingResponse(_EmptyResponseWithRoomId):
    """A response representing a successful room typing request."""

    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomTypingError.from_dict(parsed_dict, room_id)


@dataclass
class DeleteDevicesAuthResponse(Response):
    session: str = field()
    flows: dict = field()
    params: dict = field()

    @classmethod
    @verify(Schemas.delete_devices, DeleteDevicesError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> DeleteDevicesAuthResponse | ErrorResponse:
        return cls(parsed_dict["session"], parsed_dict["flows"], parsed_dict["params"])


class DeleteDevicesResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return DeleteDevicesError.from_dict(parsed_dict)


@dataclass
class RoomMessagesResponse(Response):
    room_id: str = field()

    chunk: list[Event | BadEventType] = field()
    start: str = field()
    end: str | None = field(default=None)

    @classmethod
    @verify(Schemas.room_messages, RoomMessagesError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_id: str,
    ) -> RoomMessagesResponse | ErrorResponse:
        chunk: list[Event | BadEventType] = []
        chunk = SyncResponse._get_room_events(parsed_dict["chunk"])
        return cls(room_id, chunk, parsed_dict["start"], parsed_dict.get("end"))


@dataclass
class RoomIdResponse(Response):
    room_id: str = field()

    @staticmethod
    def create_error(parsed_dict):
        return ErrorResponse.from_dict(parsed_dict)

    @classmethod
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> RoomIdResponse | ErrorResponse:
        try:
            validate_json(parsed_dict, Schemas.room_id)
        except (SchemaError, ValidationError):
            return cls.create_error(parsed_dict)

        return cls(parsed_dict["room_id"])


@dataclass
class RoomCreateResponse(Response):
    """Response representing a successful create room request."""

    room_id: str = field()

    @classmethod
    @verify(
        Schemas.room_create_response,
        RoomCreateError,
        pass_arguments=False,
    )
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> RoomCreateResponse | RoomCreateError:
        return cls(parsed_dict["room_id"])


class JoinResponse(RoomIdResponse):
    @staticmethod
    def create_error(parsed_dict):
        return JoinError.from_dict(parsed_dict)


class RoomLeaveResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return RoomLeaveError.from_dict(parsed_dict)


class RoomForgetResponse(_EmptyResponseWithRoomId):
    """Response representing a successful forget room request."""

    @staticmethod
    def create_error(parsed_dict, room_id):
        return RoomForgetError.from_dict(parsed_dict, room_id)


@dataclass
class GetOpenIDTokenResponse(Response):
    access_token: str = field()
    expires_in: int = field()
    matrix_server_name: str = field()
    token_type: str = field()

    @classmethod
    @verify(Schemas.get_openid_token, GetOpenIDTokenError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> GetOpenIDTokenResponse | ErrorResponse:
        access_token = parsed_dict["access_token"]
        expires_in = parsed_dict["expires_in"]
        matrix_server_name = parsed_dict["matrix_server_name"]
        token_type = parsed_dict["token_type"]

        return cls(access_token, expires_in, matrix_server_name, token_type)


@dataclass
class KeysUploadResponse(Response):
    curve25519_count: int = field()
    signed_curve25519_count: int = field()

    @classmethod
    @verify(Schemas.keys_upload, KeysUploadError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> KeysUploadResponse | ErrorResponse:
        counts = parsed_dict["one_time_key_counts"]
        return cls(counts["curve25519"], counts["signed_curve25519"])


@dataclass
class KeysQueryResponse(Response):
    device_keys: dict = field()
    failures: dict = field()
    queried_users: set[str] | None = None
    changed: dict[str, dict[str, Any]] = field(
        init=False,
        default_factory=dict,
    )

    @classmethod
    @verify(Schemas.keys_query, KeysQueryError, pass_arguments=False)
    def from_dict(
        cls, parsed_dict: dict[Any, Any], user_set: set[str] | None = None
    ) -> KeysQueryResponse | ErrorResponse:
        # device_keys is optional; servers may omit it when every queried server failed.
        device_keys = parsed_dict.get("device_keys", {})
        failures = parsed_dict.get("failures", {})
        if user_set is not None:
            device_keys = {
                user: devices
                for user, devices in device_keys.items()
                if user in user_set and user.partition(":")[2] not in failures
            }

        return cls(device_keys, failures, user_set)


@dataclass
class KeysClaimResponse(Response):
    one_time_keys: dict[Any, Any] = field()
    failures: dict[Any, Any] = field()
    room_id: str = ""

    @classmethod
    @verify(Schemas.keys_claim, KeysClaimError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_id: str = "",
    ) -> KeysClaimResponse | ErrorResponse:
        one_time_keys = parsed_dict["one_time_keys"]
        failures = parsed_dict.get("failures", {})

        return cls(one_time_keys, failures, room_id)


@dataclass
class DevicesResponse(Response):
    devices: list[Device] = field()

    @classmethod
    @verify(Schemas.devices, DevicesError)
    def from_dict(cls, parsed_dict: dict[Any, Any]) -> DevicesResponse | ErrorResponse:
        devices = []
        for device_dict in parsed_dict["devices"]:
            try:
                device = Device.from_dict(device_dict)
            except ValueError:
                continue
            devices.append(device)

        return cls(devices)


@dataclass
class RoomKeyRequestError(ErrorResponse):
    """Response representing a failed room key request."""

    pass


@dataclass
class RoomKeyRequestResponse(Response):
    """Response representing a successful room key request.

    Attributes:
        request_id (str): The id of the that uniquely identifies this key
            request that was requested, if we receive a to_device event it will
            contain the same request id.
        session_id (str): The id of the session that we requested.
        room_id (str): The id of the room that the session belongs to.
        algorithm (str): The encryption algorithm of the session.

    """

    request_id: str = field()
    session_id: str = field()
    room_id: str = field()
    algorithm: str = field()

    @classmethod
    @verify(Schemas.empty, RoomKeyRequestError, False)
    def from_dict(cls, _, request_id, session_id, room_id, algorithm):
        """Create a RoomKeyRequestResponse from a json response.

        Args:
            parsed_dict (Dict): The dictionary containing the json response.
            request_id (str): The id of that uniquely identifies this key
                request that was requested, if we receive a to_device event
                it will contain the same request id.
            session_id (str): The id of the session that we requested.
            room_id (str): The id of the room that the session belongs to.
            algorithm (str): The encryption algorithm of the session.
        """
        return cls(request_id, session_id, room_id, algorithm)


@dataclass
class ProfileGetResponse(Response):
    """Response representing a successful get profile request.

    Attributes:
        displayname (str, optional): The display name of the user.
            None if the user doesn't have a display name.
        avatar_url (str, optional): The matrix content URI for the user's
            avatar. None if the user doesn't have an avatar.
        other_info (dict): Contains any other information returned for the
            user's profile.
    """

    displayname: str | None = None
    avatar_url: str | None = None
    other_info: dict[Any, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"Display name: {self.displayname}, avatar URL: {self.avatar_url}, other info: {self.other_info}"

    @classmethod
    @verify(Schemas.get_profile, ProfileGetError)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> ProfileGetResponse | ErrorResponse:
        return cls(
            parsed_dict.get("displayname"),
            parsed_dict.get("avatar_url"),
            {
                k: v
                for k, v in parsed_dict.items()
                if k not in ("displayname", "avatar_url")
            },
        )


@dataclass
class ProfileGetDisplayNameResponse(Response):
    """Response representing a successful get display name request.

    Attributes:
        displayname (str, optional): The display name of the user.
            None if the user doesn't have a display name.
    """

    displayname: str | None = None

    def __str__(self) -> str:
        return f"Display name: {self.displayname}"

    @classmethod
    @verify(Schemas.get_displayname, ProfileGetDisplayNameError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> ProfileGetDisplayNameResponse | ErrorResponse:
        return cls(parsed_dict.get("displayname"))


class ProfileSetDisplayNameResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return ProfileSetDisplayNameError.from_dict(parsed_dict)


@dataclass
class ProfileGetAvatarResponse(Response):
    """Response representing a successful get avatar request.

    Attributes:
        avatar_url (str, optional): The matrix content URI for the user's
            avatar. None if the user doesn't have an avatar.
    """

    avatar_url: str | None = None

    def __str__(self) -> str:
        return f"Avatar URL: {self.avatar_url}"

    @classmethod
    @verify(Schemas.get_avatar, ProfileGetAvatarError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> ProfileGetAvatarResponse | ErrorResponse:
        return cls(parsed_dict.get("avatar_url"))


class ProfileSetAvatarResponse(EmptyResponse):
    @staticmethod
    def create_error(parsed_dict):
        return ProfileSetAvatarError.from_dict(parsed_dict)


@dataclass
class PresenceGetResponse(Response):
    """Response representing a successful get presence request.

    Attributes:
        user_id (str): The user´s id
        presence (str): The user's presence state. One of: ["online",
            "offline", "unavailable"]
        last_active_ago (int, optional): The length of time in milliseconds
            since an action was performed by this user. None if not set.
        currently_active (bool, optional): Whether the user is currently
            active. None if not set.
        status_msg (str, optional): The state message for this user. None if
            not set.
    """

    user_id: str
    presence: str
    last_active_ago: int | None
    currently_active: bool | None
    status_msg: str | None

    @classmethod
    @verify(Schemas.get_presence, PresenceGetError, pass_arguments=False)
    def from_dict(
        cls, parsed_dict: dict[Any, Any], user_id: str
    ) -> PresenceGetResponse | PresenceGetError:
        return cls(
            user_id,
            parsed_dict.get("presence", "offline"),
            parsed_dict.get("last_active_ago"),
            parsed_dict.get("currently_active"),
            parsed_dict.get("status_msg"),
        )


class PresenceSetResponse(EmptyResponse):
    """Response representing a successful set presence request."""

    @staticmethod
    def create_error(parsed_dict):
        return PresenceSetError.from_dict(parsed_dict)


@dataclass
class ToDeviceError(ErrorResponse):
    """Response representing a unsuccessful room key request."""

    to_device_message: ToDeviceMessage | None = None

    @classmethod
    def from_dict(cls, parsed_dict, message):
        try:
            validate_json(parsed_dict, Schemas.error)
        except (SchemaError, ValidationError):
            return cls("unknown error", None, message)

        return cls(parsed_dict["error"], parsed_dict["errcode"], message)


@dataclass
class ToDeviceResponse(Response):
    """Response representing a successful room key request."""

    to_device_message: ToDeviceMessage = field()

    @classmethod
    @verify(Schemas.empty, ToDeviceError)
    def from_dict(cls, parsed_dict, message):
        """Create a ToDeviceResponse from a json response."""
        return cls(message)


@dataclass
class RoomContextError(_ErrorWithRoomId):
    """Response representing a unsuccessful room context request."""


@dataclass
class RoomContextResponse(Response):
    """Room event context response.

    This Response holds a number of events that happened just before and after
    a specified event.

    Attributes:
        room_id(str): The room id of the room which the events belong to.
        start(str): A token that can be used to paginate backwards with.
        end(str): A token that can be used to paginate forwards with.
        events_before(List[Event]): A list of room events that happened just
            before the requested event, in reverse-chronological order.
        event(Event): Details of the requested event.
        events_after(List[Event]): A list of room events that happened just
            after the requested event, in chronological order.
        state(List[Event]): The state of the room at the last event returned.

    """

    room_id: str = field()

    start: str | None = field()
    end: str | None = field()

    event: Event | BadEventType | None = field()

    events_before: list[Event | BadEventType] = field()
    events_after: list[Event | BadEventType] = field()

    state: list[Event | BadEventType] = field()

    @classmethod
    @verify(Schemas.room_context, RoomContextError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
        room_id: str,
    ) -> RoomContextResponse | ErrorResponse:
        events_before = SyncResponse._get_room_events(
            parsed_dict.get("events_before", [])
        )
        events_after = SyncResponse._get_room_events(
            parsed_dict.get("events_after", [])
        )
        event = Event.parse_event(parsed_dict["event"])

        state = SyncResponse._get_room_events(parsed_dict.get("state", {}))

        return cls(
            room_id,
            parsed_dict.get("start"),
            parsed_dict.get("end"),
            event,
            events_before,
            events_after,
            state,
        )


@dataclass
class SyncResponse(Response):
    """A response for a /sync request."""

    next_batch: str = field()
    rooms: Rooms = field()
    device_key_count: DeviceOneTimeKeyCount = field()
    device_list: DeviceList = field()
    to_device_events: list[ToDeviceEvent | BadEventType] = field()
    presence_events: list[PresenceEvent] = field()
    account_data_events: list[AccountDataEvent] = field(default_factory=list)

    def __str__(self) -> str:
        result = []
        for room_id, room_info in self.rooms.join.items():
            room_header = f"  Messages for room {room_id}:\n    "
            messages = (str(event) for event in room_info.timeline.events)

            room_message = room_header + "\n    ".join(messages)
            result.append(room_message)

        if len(self.to_device_events) > 0:
            result.append("  Device messages:")
            for event in self.to_device_events:
                result.append(f"    {event}")  # noqa: PERF401

        body = "\n".join(result)
        string = f"Sync response until batch: {self.next_batch}:\n{body}"
        return string

    @staticmethod
    def _get_room_events(
        parsed_dict: list[dict[Any, Any]],
    ) -> list[Event | BadEventType]:
        events: list[Event | BadEventType] = []

        for event_dict in parsed_dict:
            event = Event.parse_event(event_dict)

            if event:
                events.append(event)

        return events

    @staticmethod
    def _get_to_device(
        parsed_dict: dict[Any, Any],
    ) -> list[ToDeviceEvent | BadEventType]:
        return [
            ToDeviceEvent.parse_event(event_dict)
            for event_dict in parsed_dict.get("events", [])
        ]

    @staticmethod
    def _get_timeline(parsed_dict: dict[Any, Any]) -> Timeline:
        validate_json(parsed_dict, Schemas.room_timeline)

        events = SyncResponse._get_room_events(parsed_dict.get("events", []))

        return Timeline(
            events, parsed_dict.get("limited", False), parsed_dict.get("prev_batch")
        )

    @staticmethod
    def _get_state(parsed_dict: dict[Any, Any]) -> list[Event | BadEventType]:
        validate_json(parsed_dict, Schemas.sync_room_state)
        events = SyncResponse._get_room_events(
            parsed_dict.get("events", []),
        )

        return events

    @staticmethod
    def _get_invite_state(parsed_dict):
        validate_json(parsed_dict, Schemas.sync_room_state)
        events = []

        for event_dict in parsed_dict.get("events", []):
            event = InviteEvent.parse_event(event_dict)

            if event:
                events.append(event)

        return events

    @staticmethod
    def _get_ephemeral_events(parsed_dict):
        events = []
        for event_dict in parsed_dict:
            event = EphemeralEvent.parse_event(event_dict)

            if event:
                events.append(event)
        return events

    @staticmethod
    def _get_join_info(
        state_events: list[Any],
        timeline_events: list[Any],
        prev_batch: str | None,
        limited: bool,
        ephemeral_events: list[Any],
        summary_events: dict[str, Any],
        unread_notification_events: dict[str, Any],
        account_data_events: list[Any],
        unread_thread_notifications: dict[str, Any],
    ) -> RoomInfo:
        state = SyncResponse._get_room_events(state_events)

        events = SyncResponse._get_room_events(timeline_events)
        timeline = Timeline(events, limited, prev_batch)

        ephemeral_event_list = SyncResponse._get_ephemeral_events(ephemeral_events)

        summary = RoomSummary(
            summary_events.get("m.invited_member_count"),
            summary_events.get("m.joined_member_count"),
            summary_events.get("m.heroes"),
        )

        unread_notifications = UnreadNotifications(
            unread_notification_events.get("notification_count"),
            unread_notification_events.get("highlight_count"),
        )

        unread_thread_notifications_dict: dict[str, UnreadNotifications] = {
            event_id: UnreadNotifications(
                d.get("notification_count"), d.get("highlight_count")
            )
            for event_id, d in unread_thread_notifications.items()
        }

        account_data = RoomInfo.parse_account_data(account_data_events)

        return RoomInfo(
            timeline=timeline,
            state=state,
            ephemeral=ephemeral_event_list,
            account_data=account_data,
            summary=summary,
            unread_notifications=unread_notifications,
            unread_thread_notifications=unread_thread_notifications_dict,
        )

    @staticmethod
    def _get_room_info(parsed_dict: dict[Any, Any]) -> Rooms:
        joined_rooms: dict[str, RoomInfo] = {}
        invited_rooms: dict[str, InviteInfo] = {}
        left_rooms: dict[str, RoomInfo] = {}

        for room_id, room_dict in parsed_dict.get("invite", {}).items():
            state = SyncResponse._get_invite_state(room_dict.get("invite_state", {}))
            invite_info = InviteInfo(state)
            invited_rooms[room_id] = invite_info

        for room_id, room_dict in parsed_dict.get("leave", {}).items():
            state = SyncResponse._get_state(room_dict.get("state", {}))
            timeline = SyncResponse._get_timeline(room_dict.get("timeline", {}))
            account_data = RoomInfo.parse_account_data(
                room_dict.get("account_data", {}).get("events", [])
            )
            leave_info = RoomInfo(timeline, state, [], account_data)
            left_rooms[room_id] = leave_info

        for room_id, room_dict in parsed_dict.get("join", {}).items():
            join_info = SyncResponse._get_join_info(
                state_events=room_dict.get("state", {}).get("events", []),
                timeline_events=room_dict.get("timeline", {}).get("events", []),
                prev_batch=room_dict.get("timeline", {}).get("prev_batch"),
                limited=room_dict.get("timeline", {}).get("limited", False),
                ephemeral_events=room_dict.get("ephemeral", {}).get("events", []),
                summary_events=room_dict.get("summary", {}),
                unread_notification_events=room_dict.get("unread_notifications", {}),
                account_data_events=room_dict.get("account_data", {}).get("events", []),
                unread_thread_notifications=room_dict.get(
                    "unread_thread_notifications", {}
                ),
            )

            joined_rooms[room_id] = join_info

        return Rooms(invited_rooms, joined_rooms, left_rooms)

    @staticmethod
    def _get_presence(parsed_dict) -> list[PresenceEvent]:
        presence_dicts = parsed_dict.get("presence", {}).get("events", [])
        return [
            PresenceEvent.from_dict(presence_dict) for presence_dict in presence_dicts
        ]

    @staticmethod
    def _get_account_data(
        parsed_dict: dict[str, Any],
    ) -> Generator[AccountDataEvent, None, None]:
        for ev_dict in parsed_dict.get("account_data", {}).get("events", []):
            yield AccountDataEvent.parse_event(ev_dict)

    @classmethod
    @verify(Schemas.sync, SyncError, False)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> SyncResponse | ErrorResponse:
        to_device = cls._get_to_device(parsed_dict.get("to_device", {}))

        key_count_dict = parsed_dict.get("device_one_time_keys_count", {})
        key_count = DeviceOneTimeKeyCount(
            key_count_dict.get("curve25519"), key_count_dict.get("signed_curve25519")
        )

        devices = DeviceList(
            parsed_dict.get("device_lists", {}).get("changed", []),
            parsed_dict.get("device_lists", {}).get("left", []),
        )

        presence_events = SyncResponse._get_presence(parsed_dict)

        rooms = SyncResponse._get_room_info(parsed_dict.get("rooms", {}))

        return SyncResponse(
            parsed_dict["next_batch"],
            rooms,
            key_count,
            devices,
            to_device,
            presence_events,
            list(SyncResponse._get_account_data(parsed_dict)),
        )


class SlidingSyncError(SyncError):
    pass


@dataclass
class SlidingSyncResponse(Response):
    """A Simplified Sliding Sync response, containing protocol data only.

    ``pos`` tracks this connection. ``to_device_next_batch`` independently
    tracks device delivery and is sent as ``extensions.to_device.since``.
    Room ``prev_batch`` tokens are pagination boundaries. ``extensions``
    retains the wire dictionary even when event parsers consume their input.
    """

    pos: str = field()
    lists: dict[str, SlidingSyncList] = field(default_factory=dict)
    rooms: dict[str, SlidingSyncRoom] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    to_device_events: list[ToDeviceEvent | BadEventType] = field(default_factory=list)
    to_device_next_batch: str | None = None
    device_key_count: DeviceOneTimeKeyCount = field(
        default_factory=lambda: DeviceOneTimeKeyCount(None, None)
    )
    device_list: DeviceList = field(default_factory=lambda: DeviceList([], []))
    account_data_events: list[AccountDataEvent] = field(default_factory=list)
    room_account_data: dict[str, list[AccountDataEvent]] = field(default_factory=dict)

    @classmethod
    @verify(Schemas.sliding_sync, SlidingSyncError, False)
    def from_dict(
        cls, parsed_dict: dict[Any, Any]
    ) -> SlidingSyncResponse | ErrorResponse:
        try:
            lists = {
                name: SlidingSyncList.from_dict(data)
                for name, data in parsed_dict.get("lists", {}).items()
            }
            rooms = {
                room_id: SlidingSyncRoom.from_dict(data)
                for room_id, data in parsed_dict.get("rooms", {}).items()
            }
            extensions = parsed_dict.get("extensions", {})
            # Event parsers can remove content or add defaults to nested dictionaries.
            parsed_extensions = copy_json(extensions)
            to_device = parsed_extensions.get("to_device", {})
            to_device_events = [
                event
                for data in to_device.get("events", [])
                if (event := ToDeviceEvent.parse_event(data)) is not None
            ]
            e2ee = parsed_extensions.get("e2ee", {})
            counts = e2ee.get("device_one_time_keys_count", {})
            devices = e2ee.get("device_lists", {})
            account_data = parsed_extensions.get("account_data", {})
            return cls(
                pos=parsed_dict["pos"],
                lists=lists,
                rooms=rooms,
                extensions=extensions,
                to_device_events=to_device_events,
                to_device_next_batch=to_device.get("next_batch"),
                device_key_count=DeviceOneTimeKeyCount(
                    counts.get("curve25519"), counts.get("signed_curve25519")
                ),
                device_list=DeviceList(
                    devices.get("changed", []), devices.get("left", [])
                ),
                account_data_events=RoomInfo.parse_account_data(
                    account_data.get("global", [])
                ),
                room_account_data={
                    room_id: RoomInfo.parse_account_data(events)
                    for room_id, events in account_data.get("rooms", {}).items()
                },
            )
        except (KeyError, TypeError, AttributeError) as exc:
            # Some event-specific parsers assume fields beyond the envelope schema.
            logger.warning("Invalid sliding sync event payload: %s", type(exc).__name__)
            return SlidingSyncError("Invalid sliding sync event payload")


class UploadFilterError(ErrorResponse):
    pass


@dataclass
class UploadFilterResponse(Response):
    """Response representing a successful filter upload request.

    Attributes:
        filter_id (str): A filter ID that may be used in
            future requests to restrict which events are returned to the
            client.
    """

    filter_id: str = field()

    @classmethod
    @verify(Schemas.upload_filter, UploadFilterError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> UploadFilterResponse | UploadFilterError:
        return cls(parsed_dict["filter_id"])


class WhoamiError(ErrorResponse):
    pass


@dataclass
class WhoamiResponse(Response):
    user_id: str = field()
    device_id: str | None = field()
    is_guest: bool | None = field()

    @classmethod
    @verify(Schemas.whoami, WhoamiError)
    def from_dict(
        cls,
        parsed_dict: dict[Any, Any],
    ) -> WhoamiResponse | WhoamiError:
        return cls(
            parsed_dict["user_id"],
            parsed_dict.get("device_id"),
            parsed_dict.get("is_guest", False),
        )
