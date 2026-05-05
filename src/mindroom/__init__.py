"""MindRoom: A universal interface for AI agents with persistent memory."""

from importlib.metadata import version as _version

from mindroom.constants import patch_chromadb_for_python314 as _patch_chromadb_for_python314
from mindroom.vendor_telemetry import disable_vendor_telemetry as _disable_vendor_telemetry

_disable_vendor_telemetry()

_patch_chromadb_for_python314()

__version__ = _version("mindroom")
