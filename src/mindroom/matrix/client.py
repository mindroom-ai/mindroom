"""Thin Matrix client facade exposing the curated public seam."""

from __future__ import annotations

from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    MatrixExpectedDeliveryPolicyError,
    cached_room,  # noqa: F401
    edit_message_result,
    is_expected_matrix_delivery_policy_error,
    send_file_message,
    send_message_result,
)
from mindroom.matrix.client_room_admin import (
    add_room_to_space,
    create_room,
    create_space,  # noqa: F401
    ensure_room_directory_visibility,  # noqa: F401
    ensure_room_join_rule,  # noqa: F401
    ensure_room_name,  # noqa: F401
    ensure_thread_tags_power_level,
    get_joined_rooms,
    get_room_members,
    get_room_name,
    invite_to_room,
    join_room,
    leave_room,
)
from mindroom.matrix.client_session import (
    PermanentMatrixStartupError,
    login,
    matrix_client,
    matrix_startup_error,
    restore_login,
)
from mindroom.matrix.client_thread_history import RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, replace_visible_message

__all__ = [
    "DeliveredMatrixEvent",
    "MatrixExpectedDeliveryPolicyError",
    "PermanentMatrixStartupError",
    "ResolvedVisibleMessage",
    "RoomThreadsPageError",
    "add_room_to_space",
    "create_room",
    "edit_message_result",
    "ensure_thread_tags_power_level",
    "get_joined_rooms",
    "get_room_members",
    "get_room_name",
    "get_room_threads_page",
    "invite_to_room",
    "is_expected_matrix_delivery_policy_error",
    "join_room",
    "leave_room",
    "login",
    "matrix_client",
    "matrix_startup_error",
    "replace_visible_message",
    "restore_login",
    "send_file_message",
    "send_message_result",
]
