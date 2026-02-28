"""MindRoom: A universal interface for AI agents with persistent memory."""

from importlib.metadata import version

from .constants import patch_chromadb_for_python314

patch_chromadb_for_python314()

__version__ = version("mindroom")
