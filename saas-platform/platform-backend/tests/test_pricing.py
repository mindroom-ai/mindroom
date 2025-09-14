"""Tests for pricing module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from backend.pricing import (
    PricingConfig,
    get_plan_details,
    get_stripe_price_id,
    get_trial_days,
    is_trial_enabled_for_plan,
    load_pricing_config,
    load_pricing_config_model,
)


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
        assert "starter" in config["plans"]
        assert "professional" in config["plans"]
        assert "free" in config["plans"]
        assert "enterprise" in config["plans"]

    def test_load_pricing_config_model(self) -> None:
        """Test loading pricing configuration as Pydantic model."""
        model = load_pricing_config_model()

        assert isinstance(model, PricingConfig)
        assert model.product.name == "MindRoom Subscription"
        assert model.product.metadata.platform == "mindroom"

        # Check plans
        assert "starter" in model.plans
        assert "professional" in model.plans
        assert "free" in model.plans
        assert "enterprise" in model.plans

        # Check trial configuration
        assert model.trial.enabled is True
        assert model.trial.days == 14
        assert "starter" in model.trial.applicable_plans
        assert "professional" in model.trial.applicable_plans

        # Check discounts
        assert model.discounts.annual_percentage == 20

    def test_plan_pricing_values(self) -> None:
        """Test that plan prices are correct."""
        model = load_pricing_config_model()

        # Free plan
        assert model.plans["free"].price_monthly == 0
        assert model.plans["free"].price_yearly == 0

        # Starter plan - $10/month
        assert model.plans["starter"].price_monthly == 1000  # $10.00 in cents
        assert model.plans["starter"].price_yearly == 9600  # $96.00 in cents (20% discount)

        # Professional plan - $8/user/month
        assert model.plans["professional"].price_monthly == 800  # $8.00 in cents
        assert model.plans["professional"].price_yearly == 7680  # $76.80 in cents (20% discount)
        assert model.plans["professional"].price_model == "per_user"

        # Enterprise plan
        assert model.plans["enterprise"].price_monthly == "custom"
        assert model.plans["enterprise"].price_yearly == "custom"

    def test_stripe_price_ids(self) -> None:
        """Test that Stripe price IDs are present."""
        model = load_pricing_config_model()

        # Starter plan IDs
        starter = model.plans["starter"]
        assert starter.stripe_price_id_monthly == "price_1S6FvF3GVsrZHuzXrDZ5H7EW"
        assert starter.stripe_price_id_yearly == "price_1S6FvF3GVsrZHuzXDjv76gwE"

        # Professional plan IDs
        professional = model.plans["professional"]
        assert professional.stripe_price_id_monthly == "price_1S6FvG3GVsrZHuzXBwljASJB"
        assert professional.stripe_price_id_yearly == "price_1S6FvG3GVsrZHuzXQV9y2VEo"

        # Free and Enterprise should not have Stripe IDs
        assert model.plans["free"].stripe_price_id_monthly is None
        assert model.plans["enterprise"].stripe_price_id_monthly is None

    def test_plan_features_and_limits(self) -> None:
        """Test plan features and limits configuration."""
        model = load_pricing_config_model()

        # Starter plan
        starter = model.plans["starter"]
        assert len(starter.features) == 7
        assert starter.limits.max_agents == 100
        assert starter.limits.max_messages_per_day == "unlimited"
        assert starter.limits.storage_gb == 5
        assert starter.limits.workflows is True
        assert starter.recommended is True

        # Professional plan
        professional = model.plans["professional"]
        assert len(professional.features) == 8
        assert professional.limits.max_agents == "unlimited"
        assert professional.limits.storage_gb == 10
        assert professional.limits.sso is True
        assert professional.limits.sla is True
        assert professional.limits.training is True

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
            "Pricing configuration file not found. This file is required for the application to run.",
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

    def test_get_stripe_price_id(self) -> None:
        """Test getting Stripe price IDs."""
        # Starter monthly
        assert get_stripe_price_id("starter", "monthly") == "price_1S6FvF3GVsrZHuzXrDZ5H7EW"

        # Starter yearly
        assert get_stripe_price_id("starter", "yearly") == "price_1S6FvF3GVsrZHuzXDjv76gwE"

        # Professional monthly
        assert get_stripe_price_id("professional", "monthly") == "price_1S6FvG3GVsrZHuzXBwljASJB"

        # Professional yearly
        assert get_stripe_price_id("professional", "yearly") == "price_1S6FvG3GVsrZHuzXQV9y2VEo"

        # Free plan (no Stripe IDs)
        assert get_stripe_price_id("free", "monthly") is None
        assert get_stripe_price_id("free", "yearly") is None

        # Non-existent plan
        assert get_stripe_price_id("nonexistent", "monthly") is None

        # Invalid billing cycle
        assert get_stripe_price_id("starter", "weekly") is None

    def test_get_plan_details(self) -> None:
        """Test getting plan details."""
        # Starter plan
        starter = get_plan_details("starter")
        assert starter is not None
        assert starter.name == "Starter"
        assert starter.price_monthly == 1000
        assert starter.price_yearly == 9600
        assert len(starter.features) == 7

        # Professional plan
        professional = get_plan_details("professional")
        assert professional is not None
        assert professional.name == "Professional"
        assert professional.price_monthly == 800
        assert professional.price_model == "per_user"

        # Non-existent plan
        assert get_plan_details("nonexistent") is None

    def test_get_trial_days(self) -> None:
        """Test getting trial days."""
        assert get_trial_days() == 14

    def test_is_trial_enabled_for_plan(self) -> None:
        """Test checking if trial is enabled for plans."""
        # Trial enabled for starter and professional
        assert is_trial_enabled_for_plan("starter") is True
        assert is_trial_enabled_for_plan("professional") is True

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
            mock_model.trial.applicable_plans = ["starter", "professional"]

            # Mock the function to use our patched model
            with patch("backend.pricing.load_pricing_config_model", return_value=mock_model):
                # Even though starter is in applicable_plans, trial should be False
                assert is_trial_enabled_for_plan("starter") is False


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
        expected_plans = {"free", "starter", "professional", "enterprise"}
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
        """Test that Stripe price IDs are populated for paid plans."""
        config_path = Path(__file__).parent.parent.parent / "pricing-config.yaml"

        with config_path.open() as f:
            yaml_data = yaml.safe_load(f)

        # Check Stripe IDs are present for paid plans
        starter = yaml_data["plans"]["starter"]
        assert "stripe_price_id_monthly" in starter
        assert "stripe_price_id_yearly" in starter
        assert starter["stripe_price_id_monthly"].startswith("price_")
        assert starter["stripe_price_id_yearly"].startswith("price_")

        professional = yaml_data["plans"]["professional"]
        assert "stripe_price_id_monthly" in professional
        assert "stripe_price_id_yearly" in professional
        assert professional["stripe_price_id_monthly"].startswith("price_")
        assert professional["stripe_price_id_yearly"].startswith("price_")

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

        # Check starter plan consistency
        assert config_dict["plans"]["starter"]["price_monthly"] == 1000
        assert config_model.plans["starter"].price_monthly == 1000
        assert yaml_data["plans"]["starter"]["price_monthly"] == 1000

        # Check professional plan consistency
        assert config_dict["plans"]["professional"]["price_monthly"] == 800
        assert config_model.plans["professional"].price_monthly == 800
        assert yaml_data["plans"]["professional"]["price_monthly"] == 800

        # Check Stripe IDs consistency
        starter_monthly_id = "price_1S6FvF3GVsrZHuzXrDZ5H7EW"
        assert get_stripe_price_id("starter", "monthly") == starter_monthly_id
        assert config_model.plans["starter"].stripe_price_id_monthly == starter_monthly_id
        assert yaml_data["plans"]["starter"]["stripe_price_id_monthly"] == starter_monthly_id
