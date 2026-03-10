"""MindRoom: A universal interface for AI agents with persistent memory."""

import os
from importlib.metadata import version

from mindroom.constants import patch_chromadb_for_python314

# MindRoom should never emit Agno network telemetry, even if a user .env enables it.
os.environ["AGNO_TELEMETRY"] = "false"

patch_chromadb_for_python314()

__version__ = version("mindroom")
