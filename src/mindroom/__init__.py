"""MindRoom: A universal interface for AI agents with persistent memory."""

from __future__ import annotations

import sys
import threading
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING

from mindroom.vendor_telemetry import disable_vendor_telemetry as _disable_vendor_telemetry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

    __version__: str


class _ChromaDbPy314Hook(MetaPathFinder):
    _mindroom_chromadb_py314_hook = True
    _lock = threading.RLock()
    _applied = False

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        if fullname == "chromadb" or fullname.startswith("chromadb."):
            with self._lock:
                try:
                    if not self._applied:
                        self._applied = True
                        from mindroom.constants import patch_chromadb_for_python314  # noqa: PLC0415

                        patch_chromadb_for_python314()
                finally:
                    # Rebind so importlib's active loop over sys.meta_path never skips index 1.
                    sys.meta_path = [finder for finder in sys.meta_path if finder is not self]
        return None


def _install_chromadb_py314_hook() -> None:
    if (
        sys.version_info < (3, 14)
        or "chromadb" in sys.modules
        or getattr(sys.modules.get("mindroom.constants"), "_CHROMADB_PY314_PATCHED", False)
        or any(getattr(finder, "_mindroom_chromadb_py314_hook", False) for finder in sys.meta_path)
    ):
        return
    sys.meta_path.insert(0, _ChromaDbPy314Hook())


def __getattr__(name: str) -> str:
    if name == "__version__":
        from importlib.metadata import version  # noqa: PLC0415

        globals()["__version__"] = resolved = version("mindroom")
        return resolved
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted({*globals(), "__version__"})


_disable_vendor_telemetry()
_install_chromadb_py314_hook()
