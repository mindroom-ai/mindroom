"""Compatibility shims for chromadb."""

from __future__ import annotations

import sys
from typing import cast

_CHROMADB_PY314_PATCHED = False


def patch_chromadb_for_python314() -> None:
    """Patch pydantic internals so chromadb works on Python 3.14+.

    chromadb currently relies on pydantic v1 `BaseSettings` behavior and defines
    untyped fields in its settings model. This runtime shim can be removed once
    chromadb ships an upstream fix.
    """
    global _CHROMADB_PY314_PATCHED
    if _CHROMADB_PY314_PATCHED or sys.version_info < (3, 14):
        return

    import pydantic  # noqa: PLC0415
    from pydantic._internal import _model_construction  # noqa: PLC0415
    from pydantic_settings import BaseSettings  # noqa: PLC0415

    # pydantic-settings v2 defaults to extra="forbid", but pydantic v1's
    # BaseSettings silently ignored env vars / .env keys that didn't match
    # any field. chromadb relies on that tolerance, so we must restore it.
    class _PermissiveBaseSettings(BaseSettings):
        model_config = BaseSettings.model_config.copy()
        model_config["extra"] = "ignore"

    pydantic.BaseSettings = _PermissiveBaseSettings

    original_inspect_namespace = _model_construction.inspect_namespace

    def _patched_inspect_namespace(*args: object, **kwargs: object) -> object:
        try:
            return original_inspect_namespace(*args, **kwargs)
        except pydantic.errors.PydanticUserError as exc:
            if "non-annotated attribute" not in str(exc):
                raise

            namespace = args[0] if args else kwargs.get("namespace")
            raw_annotations = args[1] if len(args) > 1 else kwargs.get("raw_annotations")
            if not isinstance(namespace, dict) or not isinstance(raw_annotations, dict):
                raise
            namespace_dict = cast("dict[str, object]", namespace)
            raw_annotations_dict = cast("dict[str, object]", raw_annotations)

            for field in (
                "chroma_coordinator_host",
                "chroma_logservice_host",
                "chroma_logservice_port",
            ):
                if field in namespace_dict and field not in raw_annotations_dict:
                    raw_annotations_dict[field] = type(namespace_dict[field])
            return original_inspect_namespace(*args, **kwargs)

    _model_construction.inspect_namespace = _patched_inspect_namespace
    _CHROMADB_PY314_PATCHED = True
