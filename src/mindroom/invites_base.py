"""Base classes for invitation management."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class BaseInvite(ABC):
    """Base class for all invitation types."""

    agent_name: str
    invited_by: str
    invited_at: datetime

    @abstractmethod
    def is_expired(self) -> bool:
        """Check if the invitation has expired."""
        pass


class BaseInviteManager[T: BaseInvite](ABC):
    """Base manager for invitation systems."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @abstractmethod
    async def add_invite(self, *args: Any, **kwargs: Any) -> T:
        """Add an invitation."""
        pass

    @abstractmethod
    async def remove_invite(self, *args: Any, **kwargs: Any) -> bool:
        """Remove an invitation."""
        pass

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Clean up expired invitations."""
        pass


def calculate_expiry(duration_hours: int | None) -> datetime | None:
    """Calculate expiration time from duration in hours."""
    if duration_hours is None:
        return None
    return datetime.now() + timedelta(hours=duration_hours)


def is_expired_by_time(expires_at: datetime | None) -> bool:
    """Check if an invitation has expired by time."""
    if expires_at is None:
        return False
    return datetime.now() > expires_at


def is_inactive_by_timeout(last_activity: datetime, timeout_hours: int) -> bool:
    """Check if an invitation is inactive based on timeout."""
    timeout = timedelta(hours=timeout_hours)
    return datetime.now() - last_activity > timeout
