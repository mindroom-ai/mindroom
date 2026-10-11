"""TLS trust configuration for the packaged desktop helper."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import certifi
import pytest

from mindroom.desktop import native_entry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("override", [None, "SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_entry_configures_bundled_trust_only_for_frozen_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frozen: bool,
    override: str | None,
) -> None:
    """Packaged HTTPS gets trusted roots while explicit trust settings remain authoritative."""
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["helper", "--config", str(tmp_path / "config.yaml")])
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.setenv(name, "unused")
        monkeypatch.delenv(name)
    if override:
        monkeypatch.setenv(override, "custom-trust")
    observed: dict[str, str | None] = {}

    async def serve(*_args: object, **_kwargs: object) -> None:
        observed.update({name: os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")})

    monkeypatch.setattr(native_entry, "run_native_stdio", serve)
    native_entry._main()

    expected_file = "custom-trust" if override == "SSL_CERT_FILE" else None
    if frozen and override is None:
        expected_file = certifi.where()
    assert observed == {
        "SSL_CERT_FILE": expected_file,
        "SSL_CERT_DIR": "custom-trust" if override == "SSL_CERT_DIR" else None,
    }
