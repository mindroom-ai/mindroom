"""The Stripe webhook setup script tells operators how to deploy a new signing secret."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import dotenv
import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "cluster/scripts/setup-stripe-webhooks.py"


def _load_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
    # Importing the script must not read a developer's real cluster/.env.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    spec = importlib.util.spec_from_file_location("setup_stripe_webhooks", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
