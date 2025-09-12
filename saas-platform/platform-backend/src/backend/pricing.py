"""Pricing configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Load pricing configuration from YAML
# The config file is at the saas-platform root
config_path = Path(__file__).parent.parent.parent.parent / "pricing-config.yaml"


def load_pricing_config() -> dict[str, Any]:
    """Load pricing configuration from YAML file."""
    if not config_path.exists():
        # Return a minimal config if file doesn't exist
        return {
            "plans": {},
            "product": {"name": "MindRoom Subscription"},
            "trial": {"enabled": True, "days": 14},
        }

    with config_path.open() as f:
        return yaml.safe_load(f)


def get_stripe_price_id(plan: str, billing_cycle: str = "monthly") -> str | None:
    """Get Stripe price ID for a specific plan and billing cycle.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')
        billing_cycle: Either 'monthly' or 'yearly'

    Returns:
        Stripe price ID or None if not found

    """
    config = load_pricing_config()
    plan_data = config.get("plans", {}).get(plan, {})

    if billing_cycle == "monthly":
        return plan_data.get("stripe_price_id_monthly")
    if billing_cycle == "yearly":
        return plan_data.get("stripe_price_id_yearly")
    return None


def get_plan_details(plan: str) -> dict[str, Any] | None:
    """Get full details for a specific plan.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')

    Returns:
        Plan details dictionary or None if not found

    """
    config = load_pricing_config()
    return config.get("plans", {}).get(plan)


def get_trial_days() -> int:
    """Get the number of trial days from config."""
    config = load_pricing_config()
    return config.get("trial", {}).get("days", 14)


def is_trial_enabled_for_plan(plan: str) -> bool:
    """Check if trial is enabled for a specific plan."""
    config = load_pricing_config()
    trial_config = config.get("trial", {})

    if not trial_config.get("enabled", True):
        return False

    applicable_plans = trial_config.get("applicable_plans", [])
    return plan in applicable_plans


# Export pricing data for easy access
PRICING_CONFIG = load_pricing_config()
