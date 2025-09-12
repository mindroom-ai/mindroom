"""Pricing configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel


class ProductMetadata(BaseModel):
    """Product metadata."""

    platform: str


class Product(BaseModel):
    """Product configuration."""

    name: str
    description: str
    metadata: ProductMetadata


class PlanLimits(BaseModel):
    """Plan limits and capabilities."""

    max_agents: int | Literal["unlimited"]
    max_messages_per_day: int | Literal["unlimited"]
    storage_gb: int | Literal["unlimited"]
    support: str
    integrations: str
    workflows: bool
    analytics: str
    sla: bool
    training: bool
    sso: bool
    custom_development: bool
    on_premise: bool
    dedicated_infrastructure: bool


class Plan(BaseModel):
    """Pricing plan configuration."""

    name: str
    price_monthly: int | Literal["custom"]
    price_yearly: int | Literal["custom"]
    description: str
    features: list[str]
    limits: PlanLimits
    stripe_price_id_monthly: str | None = None
    stripe_price_id_yearly: str | None = None
    recommended: bool = False
    price_model: Literal["per_user"] | None = None


class Trial(BaseModel):
    """Trial configuration."""

    enabled: bool
    days: int
    applicable_plans: list[str]


class Discounts(BaseModel):
    """Discount configuration."""

    annual_percentage: int


class PricingConfig(BaseModel):
    """Complete pricing configuration."""

    product: Product
    plans: dict[str, Plan]
    trial: Trial
    discounts: Discounts


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


def load_pricing_config_model() -> PricingConfig:
    """Load pricing configuration as a Pydantic model.

    Returns:
        PricingConfig: Validated pricing configuration model

    """
    config_dict = load_pricing_config()

    # Provide defaults for missing fields if config is minimal
    if not config_dict.get("product") or "description" not in config_dict.get("product", {}):
        # Ensure complete product information
        product = config_dict.get("product", {})
        config_dict["product"] = {
            "name": product.get("name", "MindRoom Subscription"),
            "description": product.get("description", "AI-powered team collaboration platform"),
            "metadata": product.get("metadata", {"platform": "mindroom"}),
        }

    if not config_dict.get("discounts"):
        config_dict["discounts"] = {"annual_percentage": 20}

    if not config_dict.get("trial") or "applicable_plans" not in config_dict.get("trial", {}):
        # Ensure complete trial information
        trial = config_dict.get("trial", {})
        config_dict["trial"] = {
            "enabled": trial.get("enabled", True),
            "days": trial.get("days", 14),
            "applicable_plans": trial.get("applicable_plans", []),
        }

    return PricingConfig(**config_dict)


def get_stripe_price_id(plan: str, billing_cycle: str = "monthly") -> str | None:
    """Get Stripe price ID for a specific plan and billing cycle.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')
        billing_cycle: Either 'monthly' or 'yearly'

    Returns:
        Stripe price ID or None if not found

    """
    config = load_pricing_config_model()
    plan_obj = config.plans.get(plan)

    if not plan_obj:
        return None

    if billing_cycle == "monthly":
        return plan_obj.stripe_price_id_monthly
    if billing_cycle == "yearly":
        return plan_obj.stripe_price_id_yearly
    return None


def get_plan_details(plan: str) -> Plan | None:
    """Get full details for a specific plan.

    Args:
        plan: Plan key (e.g., 'starter', 'professional')

    Returns:
        Plan object or None if not found

    """
    config = load_pricing_config_model()
    return config.plans.get(plan)


def get_trial_days() -> int:
    """Get the number of trial days from config."""
    config = load_pricing_config_model()
    return config.trial.days


def is_trial_enabled_for_plan(plan: str) -> bool:
    """Check if trial is enabled for a specific plan."""
    config = load_pricing_config_model()

    if not config.trial.enabled:
        return False

    return plan in config.trial.applicable_plans


# Export pricing data for easy access
PRICING_CONFIG = load_pricing_config()
PRICING_CONFIG_MODEL = load_pricing_config_model()
