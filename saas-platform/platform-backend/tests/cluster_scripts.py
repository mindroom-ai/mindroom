"""Load a Stripe script from cluster/scripts as a module without touching the real Stripe key or .env files."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import dotenv
import stripe

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "cluster/scripts"


def load_stripe_script(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    """Import `cluster/scripts/<name>.py` with a placeholder Stripe key."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
    # Importing the script sets the shared Stripe client's key, so restore it afterwards.
    monkeypatch.setattr(stripe, "api_key", stripe.api_key)
    # Importing the script must not read a developer's real .env.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), _SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
