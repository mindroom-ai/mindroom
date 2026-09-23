"""Origin-room publisher identity and typed authorization outcomes."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from mindroom.entity_resolution import (
    DuplicateManagedEntityIdentityError,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


class ReportAuthorizationReason(StrEnum):
    """Stable origin-room authorization outcome categories."""

    AUTHORIZED = "authorized"
    VIEWER_NOT_JOINED = "viewer_not_joined"
    PUBLISHER_NOT_JOINED = "publisher_not_joined"
    PUBLISHER_IDENTITY_MISMATCH = "publisher_identity_mismatch"
    AUTHORIZATION_BACKEND_UNAVAILABLE = "authorization_backend_unavailable"


def current_publisher_matrix_user_id(config: Config, runtime_paths: RuntimePaths, entity_name: str) -> str | None:
    """Return one configured entity's current Matrix ID, or None when it cannot publish or authorize reports."""
    try:
        return entity_identity_registry(config, runtime_paths).current_id(entity_name).full_id
    except (DuplicateManagedEntityIdentityError, KeyError, MissingManagedEntityAccountError):
        return None
