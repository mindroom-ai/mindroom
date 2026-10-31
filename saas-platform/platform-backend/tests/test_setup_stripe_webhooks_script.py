"""The Stripe webhook setup script tells operators how to deploy a new signing secret."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.cluster_scripts import load_stripe_script

if TYPE_CHECKING:
    from pathlib import Path


def _load_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    module = load_stripe_script(monkeypatch, "setup-stripe-webhooks")
    # The script writes the secret to the .env beside its parent directory, so point that at an empty tree.
    module.__file__ = str(tmp_path / "scripts" / "setup-stripe-webhooks.py")
    return module


def test_new_secret_guidance_names_the_values_the_platform_chart_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The printed guidance uses the Helm value and Secret key the chart reads, not a direct Secret patch."""
    script = _load_script(monkeypatch, tmp_path)

    script.save_webhook_secret(SimpleNamespace(secret="whsec_placeholder"))

    output = capsys.readouterr().out
    assert "stripe:" in output
    assert "webhookSecret: whsec_placeholder" in output
    assert "stripe_webhook_secret" in output
    assert "kubectl patch" not in output
    assert "STRIPE_WEBHOOK_SECRET" not in output
    assert not (tmp_path / ".env").exists()
