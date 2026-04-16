"""Tests for package-level import side effects."""

import importlib
import os

import pytest

import mindroom


def test_package_init_disables_vendor_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """MindRoom should force vendor telemetry off at import time."""
    monkeypatch.setenv("AGNO_TELEMETRY", "true")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "true")
    monkeypatch.setenv("MEM0_TELEMETRY", "true")

    importlib.reload(mindroom)

    assert os.environ["AGNO_TELEMETRY"] == "false"
    assert os.environ["ANONYMIZED_TELEMETRY"] == "false"
    assert os.environ["MEM0_TELEMETRY"] == "false"
