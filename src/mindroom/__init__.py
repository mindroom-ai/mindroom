"""MindRoom: A universal interface for AI agents with persistent memory."""

from importlib.metadata import version

from mindroom.constants import patch_chromadb_for_python314
from mindroom.vendor_telemetry import disable_vendor_telemetry

disable_vendor_telemetry()

patch_chromadb_for_python314()

__version__ = version("mindroom")
