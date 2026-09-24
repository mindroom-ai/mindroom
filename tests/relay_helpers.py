"""Stamp fabricated relay content the way runtime relay senders do.

Ingress honours ``com.mindroom.original_sender`` only with this runtime's
authorship proof, so a test event standing in for a hook, scheduled fire,
external trigger, voice relay, or router handoff must carry that proof too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.relay_proof import sign_relay_metadata
from tests.conftest import runtime_paths_for

if TYPE_CHECKING:
    from mindroom.config.main import Config


def signed_relay_content(content: dict[str, Any], config: Config) -> dict[str, Any]:
    """Return *content* stamped with the runtime authorship proof ingress requires."""
    sign_relay_metadata(content, runtime_paths_for(config))
    return content
