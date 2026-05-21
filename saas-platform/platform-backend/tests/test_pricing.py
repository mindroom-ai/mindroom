"""Tests for pricing module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from backend.pricing import (
    PricingConfig,
    get_plan_details,
    get_stripe_price_id,
    get_stripe_price_match,
    get_trial_days,
    is_trial_enabled_for_plan,
    load_pricing_config,
    load_pricing_config_model,
)


@pytest.fixture(autouse=True)
def clear_stripe_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pricing mode tests isolated from host Stripe configuration."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("STRIPE_PUBLISHABLE_KEY", raising=False)


class TestPricingConfig:
    """Test pricing configuration loading and validation."""

    def test_load_pricing_config(self) -> None:
        """Test loading pricing configuration from YAML."""
        config = load_pricing_config()

        assert "plans" in config
        assert "product" in config
        assert "trial" in config
        assert "discounts" in config

        # Check specific plan data
        assert "byok" in config["plans"]
        assert "hobby" in config["plans"]
        assert "pro" in config["plans"]
        assert "free" in config["plans"]
        assert "enterprise" in config["plans"]

    def test_load_pricing_config_model(self) -> None:
        """Test loading pricing configuration as Pydantic model."""
        model = load_pricing_config_model()

        assert isinstance(model, PricingConfig)
        assert model.product.name == "MindRoom Subscription"
        assert model.product.metadata.platform == "mindroom"

        # Check plans
        assert "byok" in model.plans
        assert "hobby" in model.plans
        assert "pro" in model.plans
        assert "free" in model.plans
        assert "enterprise" in model.plans

        # Check trial configuration
        assert model.trial.enabled is True
        assert model.trial.days == 3
        assert "byok" in model.trial.applicable_plans
        assert "hobby" in model.trial.applicable_plans
        assert "pro" in model.trial.applicable_plans

        # Check discounts
        assert model.discounts.annual_percentage == 20

    def test_plan_pricing_values(self) -> None:
        """Test that plan prices are correct."""
        model = load_pricing_config_model()

        # Free plan
        assert model.plans["free"].price_monthly == 0
        assert model.plans["free"].price_yearly == 0

        # BYOK plan - $10/month
        assert model.plans["byok"].price_monthly == 1000
        assert model.plans["byok"].price_yearly == 9600
        assert model.plans["byok"].included_ai_budget_usd == 0
        assert model.plans["byok"].requires_customer_provider_keys is True
        assert model.plans["byok"].resource_profile == "small"

        # Hobby plan - $20/month with $15 included AI usage
        assert model.plans["hobby"].price_monthly == 2000
        assert model.plans["hobby"].price_yearly == 19200
        assert model.plans["hobby"].included_ai_budget_usd == 15
        assert model.plans["hobby"].requires_customer_provider_keys is False
        assert model.plans["hobby"].resource_profile == "small"

        # Pro plan - $200/month with $150 included AI usage
        assert model.plans["pro"].price_monthly == 20000
        assert model.plans["pro"].price_yearly == 192000
        assert model.plans["pro"].included_ai_budget_usd == 150
        assert model.plans["pro"].requires_customer_provider_keys is False
        assert model.plans["pro"].resource_profile == "pro"

        # Enterprise plan
        assert model.plans["enterprise"].price_monthly == "custom"
        assert model.plans["enterprise"].price_yearly == "custom"

    def test_stripe_price_ids(self) -> None:
        """Test that Stripe price IDs are present."""
        model = load_pricing_config_model()

        for plan_key in ("byok", "hobby", "pro"):
            plan = model.plans[plan_key]
            assert plan.stripe_price_id_monthly is not None
            assert plan.stripe_price_id_monthly.startswith("price_")
            assert plan.stripe_price_id_yearly is not None
            assert plan.stripe_price_id_yearly.startswith("price_")

        byok = model.plans["byok"]
        assert byok.stripe_price_id_monthly_live == "price_1TZQHw3GVsrZHuzXeXWd2f3Z"
        assert byok.stripe_price_id_yearly_live == "price_1TZQJK3GVsrZHuzXuazROiIy"

        # Free and Enterprise should not have Stripe IDs
        assert model.plans["free"].stripe_price_id_monthly is None
        assert model.plans["enterprise"].stripe_price_id_monthly is None

    def test_plan_features_and_limits(self) -> None:
        """Test plan features and limits configuration."""
        model = load_pricing_config_model()

        # BYOK plan
        byok = model.plans["byok"]
        assert len(byok.features) == 7
        assert byok.limits.max_agents == 100
        assert byok.limits.max_messages_per_day == "unlimited"
        assert byok.limits.storage_gb == 5
        assert byok.limits.workflows is True

        # Hobby plan
        hobby = model.plans["hobby"]
        assert hobby.recommended is True
        assert "$15 included monthly AI usage" in hobby.features
        assert hobby.limits.storage_gb == 5

        # Pro plan
        pro = model.plans["pro"]
        assert "$150 included monthly AI usage" in pro.features
        assert pro.limits.max_agents == "unlimited"
        assert pro.limits.storage_gb == 25
        assert pro.limits.sla is True

        # Enterprise plan
        enterprise = model.plans["enterprise"]
        assert enterprise.limits.custom_development is True
        assert enterprise.limits.on_premise is True
        assert enterprise.limits.dedicated_infrastructure is True

    def test_missing_config_file(self) -> None:
        """Test behavior when config file is missing."""
        # Create a proper mock that raises FileNotFoundError when opened
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        mock_path.open.side_effect = FileNotFoundError(
            "Pricing configuration file not found. This file is required for the application to run."
        )

        with patch("backend.pricing.config_path", mock_path):
            # Should raise FileNotFoundError when config file is missing
            with pytest.raises(FileNotFoundError) as exc_info:
                load_pricing_config()

            assert "Pricing configuration file not found" in str(exc_info.value)
            assert "This file is required for the application to run" in str(exc_info.value)

            # Model should also fail without valid config
            with pytest.raises(FileNotFoundError):
                load_pricing_config_model()


class TestPricingHelperFunctions:
    """Test pricing helper functions."""

    def test_get_stripe_price_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test getting Stripe price IDs."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_mock")

        # BYOK monthly
        assert get_stripe_price_id("byok", "monthly") == "price_1TZQNK3GVsrZHuzX6EWO8kgD"

        # BYOK yearly
        assert get_stripe_price_id("byok", "yearly") == "price_1TZQNK3GVsrZHuzXqbwHwhph"

        # Free plan (no Stripe IDs)
        assert get_stripe_price_id("free", "monthly") is None
        assert get_stripe_price_id("free", "yearly") is None

        # Non-existent plan
        assert get_stripe_price_id("nonexistent", "monthly") is None

        # Invalid billing cycle
        assert get_stripe_price_id("byok", "weekly") is None

    def test_get_live_stripe_price_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test getting live Stripe price IDs in live mode."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_mock")

        assert get_stripe_price_id("byok", "monthly") == "price_1TZQHw3GVsrZHuzXeXWd2f3Z"
        assert get_stripe_price_id("byok", "yearly") == "price_1TZQJK3GVsrZHuzXuazROiIy"
        assert get_stripe_price_id("hobby", "monthly") == "price_1TZRzu3GVsrZHuzXUXJEQ6Ng"
        assert get_stripe_price_id("hobby", "yearly") == "price_1TZRzu3GVsrZHuzX77678390"
        assert get_stripe_price_id("pro", "monthly") == "price_1TZRzv3GVsrZHuzXFRd9cUgz"
        assert get_stripe_price_id("pro", "yearly") == "price_1TZRzv3GVsrZHuzXrXOhvBy0"

    def test_publishable_key_does_not_determine_stripe_price_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Server-side Stripe price IDs must match the server-side secret key."""
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_live_mock")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_mock")

        assert get_stripe_price_id("byok", "monthly") == "price_1TZQNK3GVsrZHuzX6EWO8kgD"

    def test_stripe_secret_file_determines_price_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Production file-mounted secrets select live price IDs."""
        secret_file = tmp_path / "stripe_secret_key"
        secret_file.write_text("sk_live_mock", encoding="utf-8")
        monkeypatch.setenv("STRIPE_SECRET_KEY_FILE", str(secret_file))

        assert get_stripe_price_id("byok", "monthly") == "price_1TZQHw3GVsrZHuzXeXWd2f3Z"

    def test_stripe_secret_env_takes_precedence_over_secret_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Environment secret precedence matches backend.config._get_secret."""
        secret_file = tmp_path / "stripe_secret_key"
        secret_file.write_text("sk_live_mock", encoding="utf-8")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_mock")
        monkeypatch.setenv("STRIPE_SECRET_KEY_FILE", str(secret_file))

        assert get_stripe_price_id("byok", "monthly") == "price_1TZQNK3GVsrZHuzX6EWO8kgD"

    def test_get_stripe_price_match_uses_configured_test_and_live_ids(self) -> None:
        """Configured Stripe price IDs map back to canonical plan metadata."""
        test_match = get_stripe_price_match("price_1TZQNK3GVsrZHuzX6EWO8kgD")
        assert test_match is not None
        assert test_match.tier == "byok"
        assert test_match.billing_cycle == "monthly"

        live_match = get_stripe_price_match("price_1TZQJK3GVsrZHuzXuazROiIy")
        assert live_match is not None
        assert live_match.tier == "byok"
        assert live_match.billing_cycle == "yearly"

        assert get_stripe_price_match("price_unknown") is None

    def test_get_plan_details(self) -> None:
        """Test getting plan details."""
        # BYOK plan
        byok = get_plan_details("byok")
        assert byok is not None
        assert byok.name == "Bring Your Own Keys"
        assert byok.price_monthly == 1000
        assert byok.price_yearly == 9600
        assert len(byok.features) == 7

        # Hobby plan
        hobby = get_plan_details("hobby")
        assert hobby is not None
        assert hobby.name == "Hobby"
        assert hobby.included_ai_budget_usd == 15

        # Non-existent plan
        assert get_plan_details("nonexistent") is None

    def test_get_trial_days(self) -> None:
        """Test getting trial days."""
        assert get_trial_days() == 3

    def test_is_trial_enabled_for_plan(self) -> None:
        """Test checking if trial is enabled for plans."""
        # Trial enabled for hosted paid plans
        assert is_trial_enabled_for_plan("byok") is True
        assert is_trial_enabled_for_plan("hobby") is True
        assert is_trial_enabled_for_plan("pro") is True

        # Trial not enabled for free and enterprise
        assert is_trial_enabled_for_plan("free") is False
        assert is_trial_enabled_for_plan("enterprise") is False

        # Non-existent plan
        assert is_trial_enabled_for_plan("nonexistent") is False

    def test_trial_disabled_globally(self) -> None:
        """Test behavior when trial is disabled globally."""
        # Temporarily modify the config to disable trials
        model = load_pricing_config_model()  # noqa: F841

        with patch("backend.pricing.PRICING_CONFIG_MODEL") as mock_model:
            # Create a mock with trial disabled
            mock_model.trial.enabled = False
            mock_model.trial.applicable_plans = ["byok", "hobby", "pro"]

            # Mock the function to use our patched model.
            with patch("backend.pricing.load_pricing_config_model", return_value=mock_model):
                assert is_trial_enabled_for_plan("byok") is False


class TestPricingIntegration:
    """Integration tests for pricing system."""

    def test_pricing_yaml_structure(self) -> None:
        """Test that the YAML file has the expected structure."""
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"

        with config_path.open() as f:
            yaml_data = yaml.safe_load(f)

        # Check top-level keys
        required_keys = {"product", "plans", "trial", "discounts"}
        assert set(yaml_data.keys()) == required_keys

        # Check product structure
        assert "name" in yaml_data["product"]
        assert "description" in yaml_data["product"]
        assert "metadata" in yaml_data["product"]

        # Check plans structure
        expected_plans = {"free", "byok", "hobby", "pro", "enterprise"}
        assert set(yaml_data["plans"].keys()) == expected_plans

        # Check each plan has required fields
        for plan_data in yaml_data["plans"].values():
            assert "name" in plan_data
            assert "price_monthly" in plan_data
            assert "price_yearly" in plan_data
            assert "description" in plan_data
            assert "features" in plan_data
            assert "limits" in plan_data

    def test_stripe_price_ids_populated(self) -> None:
        """Test that Stripe price IDs are populated for synced paid plans."""
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"

        with config_path.open() as f:
            yaml_data = yaml.safe_load(f)

        byok = yaml_data["plans"]["byok"]
        assert "stripe_price_id_monthly" in byok
        assert "stripe_price_id_yearly" in byok
        assert byok["stripe_price_id_monthly"].startswith("price_")
        assert byok["stripe_price_id_yearly"].startswith("price_")

    def test_pricing_consistency(self) -> None:
        """Test that pricing is consistent across different access methods."""
        # Load via dict
        config_dict = load_pricing_config()

        # Load via model
        config_model = load_pricing_config_model()

        # Load directly from YAML
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"
        with config_path.open() as f:
            yaml_data = yaml.safe_load(f)

        # Check hosted plan consistency.
        assert config_dict["plans"]["byok"]["price_monthly"] == 1000
        assert config_model.plans["byok"].price_monthly == 1000
        assert yaml_data["plans"]["byok"]["price_monthly"] == 1000

        assert config_dict["plans"]["hobby"]["included_ai_budget_usd"] == 15
        assert config_model.plans["hobby"].included_ai_budget_usd == 15
        assert yaml_data["plans"]["hobby"]["included_ai_budget_usd"] == 15

        assert config_dict["plans"]["pro"]["included_ai_budget_usd"] == 150
        assert config_model.plans["pro"].included_ai_budget_usd == 150
        assert yaml_data["plans"]["pro"]["included_ai_budget_usd"] == 150

        # Check Stripe IDs consistency
        byok_monthly_id = "price_1TZQNK3GVsrZHuzX6EWO8kgD"
        assert get_stripe_price_id("byok", "monthly") == byok_monthly_id
        assert config_model.plans["byok"].stripe_price_id_monthly == byok_monthly_id
