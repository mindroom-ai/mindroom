"""Shared compatibility identifiers for durable background script workers."""

# Bump when the worker launch/status/cancel protocol or script SDK gateway contract
# becomes incompatible with an already-running worker from an earlier image.
SCRIPT_PROTOCOL_VERSION = 1

__all__ = ["SCRIPT_PROTOCOL_VERSION"]
