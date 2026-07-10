"""Authenticated Matrix to-device event types."""

from dataclasses import dataclass

import nio


@dataclass
class AuthenticatedToDeviceEvent(nio.UnknownToDeviceEvent):
    """Unknown custom event proven to come from one Olm device."""

    authenticated_device_id: str
