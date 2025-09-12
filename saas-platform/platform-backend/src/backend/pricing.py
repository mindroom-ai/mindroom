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


def find_pricing_config_path() -> Path:
    """Find the pricing configuration file in expected locations.

    Returns:
        Path to the pricing configuration file.

    Raises:
        FileNotFoundError: If the config file is not found in any expected location.

    """
    possible_paths = [
        Path("/app/pricing-config.yaml"),  # Docker container path
        Path(__file__).parent.parent.parent.parent / "pricing-config.yaml",  # Development path
        Path("pricing-config.yaml"),  # Current directory
    ]

    for path in possible_paths:
        if path.exists():
            return path

    msg = (
        "pricing-config.yaml not found in any expected location. "
        "Looked in: /app/, development path, and current directory"
    )
    raise FileNotFoundError(msg)


# Load the config path at module initialization
config_path = find_pricing_config_path()


def load_pricing_config() -> dict[str, Any]:
    """Load pricing configuration from YAML file."""
    with config_path.open() as f:
        return yaml.safe_load(f)


def load_pricing_config_model() -> PricingConfig:
    """Load pricing configuration as a Pydantic model.

    Returns:
        PricingConfig: Validated pricing configuration model

    """
    config_dict = load_pricing_config()
    # Pydantic will validate all required fields automatically
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


def get_plan_limits_from_metadata(tier: str) -> dict[str, Any]:
    """Get plan limits for a specific tier.

    Args:
        tier: Plan tier (e.g., 'free', 'starter', 'professional', 'enterprise')

    Returns:
        Dictionary of plan limits with keys like 'max_agents', 'max_messages_per_day'

    """
    plan = get_plan_details(tier)

    if not plan:
        available_plans = ", ".join(load_pricing_config_model().plans.keys())
        msg = f"Plan '{tier}' not found in pricing configuration. Available plans: {available_plans}"
        raise ValueError(msg)

    # Convert Pydantic model to dict, handling "unlimited" values
    limits = {}

    # Handle unlimited as a very large number for database storage
    if plan.limits.max_agents == "unlimited":
        limits["max_agents"] = 999999
    else:
        limits["max_agents"] = plan.limits.max_agents

    if plan.limits.max_messages_per_day == "unlimited":
        limits["max_messages_per_day"] = 999999
    else:
        limits["max_messages_per_day"] = plan.limits.max_messages_per_day

    if plan.limits.storage_gb == "unlimited":
        limits["max_storage_gb"] = 999999
    else:
        limits["max_storage_gb"] = plan.limits.storage_gb

    return limits


# Export pricing data for easy access
PRICING_CONFIG = load_pricing_config()
PRICING_CONFIG_MODEL = load_pricing_config_model()
