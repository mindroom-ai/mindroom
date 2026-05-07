"""Import behavior tests for OAuth client helpers."""

from __future__ import annotations

import builtins
import importlib
import sys


def test_oauth_client_import_does_not_import_google_oauth_credentials() -> None:
    """OAuth client module import should not require Google user credential helpers."""
    sys.modules.pop("mindroom.oauth.client", None)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,  # noqa: A002
        locals: dict[str, object] | None = None,  # noqa: A002
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "google.oauth2" and "credentials" in fromlist:
            msg = "package-level google.oauth2 credentials import is not allowed"
            raise AssertionError(msg)
        if name == "google.oauth2.credentials":
            msg = "google.oauth2.credentials should be imported lazily"
            raise AssertionError(msg)
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import
    try:
        importlib.import_module("mindroom.oauth.client")
    finally:
        builtins.__import__ = original_import
